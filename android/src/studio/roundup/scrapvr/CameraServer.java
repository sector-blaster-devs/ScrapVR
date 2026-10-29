package studio.roundup.scrapvr;

import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.ImageFormat;
import android.graphics.Rect;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.util.Size;
import android.util.SizeF;
import android.view.WindowManager;

import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Calibration only: the rear camera is the eye view in the headset, so the PC compares it
 * with the headset webcam (tracker/head_tracker.py --calibrate-mount). TCP 16062 over the USB
 * tether: "INFO\n" -> one JSON line (lens data, sensor orientation, display rotation);
 * "SNAP\n" -> 4-byte big-endian length + the newest JPEG (0 = none). The camera is open
 * only while the PC is connected.
 */
class CameraServer {
    static final int PORT = 16062;

    private final Context context;
    private final Handler handler;
    private volatile ServerSocket server;
    private volatile boolean running;
    private CameraDevice device;
    private ImageReader reader;
    private String cameraId;
    private final Object frameLock = new Object();
    private byte[] latest;
    private long latestSeq;

    CameraServer(Context context, Handler handler) {
        this.context = context;
        this.handler = handler;
    }

    void start() {
        running = true;
        Thread thread = new Thread(new Runnable() {
            @Override public void run() {
                serve();
            }
        }, "camera-server");
        thread.setDaemon(true);
        thread.start();
    }

    void stop() {
        running = false;
        try {
            if (server != null) {
                server.close();
            }
        } catch (IOException ignored) {
        }
    }

    private void serve() {
        try {
            server = new ServerSocket(PORT);
        } catch (IOException e) {
            return;
        }
        while (running) {
            try (Socket client = server.accept()) {
                client.setTcpNoDelay(true);
                BufferedReader in = new BufferedReader(new InputStreamReader(client.getInputStream(), StandardCharsets.UTF_8));
                DataOutputStream out = new DataOutputStream(new BufferedOutputStream(client.getOutputStream()));
                String opened = openCamera();
                String line;
                while ((line = in.readLine()) != null) {
                    if (line.equals("INFO")) {
                        out.write(((opened == null ? info() : "{\"error\":\"" + opened + "\"}") + "\n").getBytes(StandardCharsets.UTF_8));
                    } else if (line.equals("SNAP")) {
                        byte[] jpeg = opened == null ? nextFrame(3000) : null;
                        out.writeInt(jpeg == null ? 0 : jpeg.length);
                        if (jpeg != null) {
                            out.write(jpeg);
                        }
                    }
                    out.flush();
                }
            } catch (IOException ignored) {
            } finally {
                closeCamera();
            }
        }
    }

    /** null when the camera is streaming JPEGs into the reader, else why not. */
    private String openCamera() {
        if (context.checkSelfPermission(android.Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            return "no camera permission";
        }
        try {
            CameraManager manager = context.getSystemService(CameraManager.class);
            CameraCharacteristics chars = null;
            for (String id : manager.getCameraIdList()) {
                CameraCharacteristics c = manager.getCameraCharacteristics(id);
                Integer facing = c.get(CameraCharacteristics.LENS_FACING);
                if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) {
                    cameraId = id;
                    chars = c;
                    break;
                }
            }
            if (chars == null) {
                return "no rear camera";
            }
            // 4:3 is the sensor's full field of view; capped so a JPEG crosses the tether quickly.
            StreamConfigurationMap map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            Size best = null;
            for (Size s : map.getOutputSizes(ImageFormat.JPEG)) {
                if (s.getWidth() * 3 == s.getHeight() * 4 && s.getWidth() <= 2048
                        && (best == null || s.getWidth() > best.getWidth())) {
                    best = s;
                }
            }
            if (best == null) {
                return "no 4:3 JPEG size";
            }
            reader = ImageReader.newInstance(best.getWidth(), best.getHeight(), ImageFormat.JPEG, 2);
            reader.setOnImageAvailableListener(new ImageReader.OnImageAvailableListener() {
                @Override public void onImageAvailable(ImageReader r) {
                    Image image = r.acquireLatestImage();
                    if (image == null) {
                        return;
                    }
                    ByteBuffer buffer = image.getPlanes()[0].getBuffer();
                    byte[] bytes = new byte[buffer.remaining()];
                    buffer.get(bytes);
                    image.close();
                    synchronized (frameLock) {
                        latest = bytes;
                        latestSeq++;
                        frameLock.notifyAll();
                    }
                }
            }, handler);
            final CountDownLatch ready = new CountDownLatch(1);
            manager.openCamera(cameraId, new CameraDevice.StateCallback() {
                @Override public void onOpened(final CameraDevice d) {
                    device = d;
                    try {
                        d.createCaptureSession(Collections.singletonList(reader.getSurface()),
                                new CameraCaptureSession.StateCallback() {
                                    @Override public void onConfigured(CameraCaptureSession session) {
                                        try {
                                            CaptureRequest.Builder request = d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                                            request.addTarget(reader.getSurface());
                                            request.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);
                                            session.setRepeatingRequest(request.build(), null, handler);
                                        } catch (Exception ignored) {
                                        }
                                        ready.countDown();
                                    }

                                    @Override public void onConfigureFailed(CameraCaptureSession session) {
                                        ready.countDown();
                                    }
                                }, handler);
                    } catch (Exception e) {
                        ready.countDown();
                    }
                }

                @Override public void onDisconnected(CameraDevice d) {
                    d.close();
                    ready.countDown();
                }

                @Override public void onError(CameraDevice d, int error) {
                    d.close();
                    ready.countDown();
                }
            }, handler);
            ready.await(5, TimeUnit.SECONDS);
            return device == null ? "camera did not open" : null;
        } catch (Exception e) {
            return "camera: " + e.getClass().getSimpleName();
        }
    }

    private byte[] nextFrame(long timeoutMs) {
        synchronized (frameLock) {
            long seen = latestSeq;
            long until = System.currentTimeMillis() + timeoutMs;
            while (latestSeq == seen && System.currentTimeMillis() < until) {
                try {
                    frameLock.wait(Math.max(1, until - System.currentTimeMillis()));
                } catch (InterruptedException e) {
                    return null;
                }
            }
            return latestSeq == seen ? null : latest;
        }
    }

    private void closeCamera() {
        if (device != null) {
            device.close();
            device = null;
        }
        if (reader != null) {
            reader.close();
            reader = null;
        }
        synchronized (frameLock) {
            latest = null;
        }
    }

    private String info() {
        try {
            CameraCharacteristics c = context.getSystemService(CameraManager.class).getCameraCharacteristics(cameraId);
            float[] focal = c.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS);
            SizeF sensor = c.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE);
            Size pixels = c.get(CameraCharacteristics.SENSOR_INFO_PIXEL_ARRAY_SIZE);
            Rect active = c.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE);
            float[] intrinsics = c.get(CameraCharacteristics.LENS_INTRINSIC_CALIBRATION);
            float[] distortion = c.get(CameraCharacteristics.LENS_DISTORTION);
            Integer orientation = c.get(CameraCharacteristics.SENSOR_ORIENTATION);
            int rotation = ((WindowManager) context.getSystemService(Context.WINDOW_SERVICE)).getDefaultDisplay().getRotation();
            return String.format(Locale.US,
                    "{\"width\":%d,\"height\":%d,\"focal_mm\":%.4f,\"sensor_mm\":[%.4f,%.4f],\"pixel_array\":[%d,%d],"
                            + "\"active\":[%d,%d,%d,%d],\"intrinsics\":%s,\"distortion\":%s,\"sensor_orientation\":%d,"
                            + "\"display_rotation\":%d}",
                    reader.getWidth(), reader.getHeight(), focal[0], sensor.getWidth(), sensor.getHeight(),
                    pixels.getWidth(), pixels.getHeight(), active.left, active.top, active.right, active.bottom,
                    array(intrinsics), array(distortion), orientation == null ? -1 : orientation, rotation);
        } catch (Exception e) {
            return "{\"error\":\"info: " + e.getClass().getSimpleName() + "\"}";
        }
    }

    private static String array(float[] values) {
        if (values == null) {
            return "null";
        }
        StringBuilder out = new StringBuilder("[");
        for (int i = 0; i < values.length; i++) {
            out.append(i == 0 ? "" : ",").append(String.format(Locale.US, "%.6f", values[i]));
        }
        return out.append("]").toString();
    }
}
