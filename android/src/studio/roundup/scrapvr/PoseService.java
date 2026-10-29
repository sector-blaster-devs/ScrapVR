package studio.roundup.scrapvr;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.SystemClock;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.SocketException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;

/**
 * Head pose for a PC-rendered VR view (UDP 16061, see docs/PROTOCOL.md).
 * A foreground service, so it keeps sending while Moonlight is the app on screen.
 * Broadcasts on the USB-tether subnet, so the PC's address never has to be typed in.
 * yaw = heading of the view (clockwise), pitch = negative looking up, both radians.
 */
public class PoseService extends Service implements SensorEventListener {
    static final int PORT = 16061;
    static volatile String status = "Stopped";

    private SensorManager sensors;
    private HandlerThread thread;
    private HandlerThread cameraThread;
    private CameraServer cameraServer;
    private DatagramSocket socket;
    private final List<InetAddress> targets = new ArrayList<>();
    private long lastScanMs = -10000;
    private long sent = 0;
    private int recenterId;
    private final float[] rotation = new float[9];
    private final float[] view = new float[9];
    private final float[] angles = new float[3];

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationChannel channel = new NotificationChannel("pose", "Gyro to the PC", NotificationManager.IMPORTANCE_LOW);
        getSystemService(NotificationManager.class).createNotificationChannel(channel);
        Notification note = new Notification.Builder(this, "pose")
                .setContentTitle("Sending gyro to the PC")
                .setSmallIcon(android.R.drawable.ic_menu_compass)
                .setOngoing(true)
                .build();
        startForeground(1, note);

        // Calibration only (CameraServer): idle until the PC connects.
        cameraThread = new HandlerThread("camera");
        cameraThread.start();
        cameraServer = new CameraServer(this, new Handler(cameraThread.getLooper()));
        cameraServer.start();

        recenterId = (int) (SystemClock.elapsedRealtime() / 1000);
        sensors = getSystemService(SensorManager.class);
        Sensor sensor = sensors.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR);
        if (sensor == null) {
            status = "This phone has no game rotation sensor";
            return;
        }
        thread = new HandlerThread("pose");
        thread.start();
        sensors.registerListener(this, sensor, SensorManager.SENSOR_DELAY_GAME, new Handler(thread.getLooper()));
        status = "Waiting for USB tethering";
    }

    @Override
    public void onSensorChanged(SensorEvent event) {
        long now = SystemClock.elapsedRealtime();
        if (now - lastScanMs > 2000) {
            lastScanMs = now;
            scanTargets();
        }
        if (targets.isEmpty()) {
            return;
        }
        SensorManager.getRotationMatrixFromVector(rotation, event.values);
        // The eyes look out of the back of the phone: remap so the view axis is "forward".
        SensorManager.remapCoordinateSystem(rotation, SensorManager.AXIS_X, SensorManager.AXIS_Z, view);
        SensorManager.getOrientation(view, angles);
        byte[] data = String.format(Locale.US,
                "{\"type\":\"scrapvr_pose\",\"yaw\":%.5f,\"pitch\":%.5f,\"roll\":%.5f,\"recenter_id\":%d}",
                angles[0], angles[1], angles[2], recenterId).getBytes(StandardCharsets.UTF_8);
        for (InetAddress target : targets) {
            try {
                socket.send(new DatagramPacket(data, data.length, target, PORT));
            } catch (IOException ignored) {
            }
        }
        sent++;
        if (sent % 50 == 0) {
            status = "Sending to " + targets.get(0).getHostAddress() + " - " + sent + " packets";
        }
    }

    /** The USB-tether interface comes and goes with the toggle, so look again every 2 s. */
    private void scanTargets() {
        targets.clear();
        try {
            if (socket == null) {
                socket = new DatagramSocket();
                socket.setBroadcast(true);
            }
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                String name = ni.getName();
                if (!ni.isUp() || !(name.startsWith("rndis") || name.startsWith("usb") || name.startsWith("ncm"))) {
                    continue;
                }
                for (InterfaceAddress address : ni.getInterfaceAddresses()) {
                    if (address.getBroadcast() != null) {
                        targets.add(address.getBroadcast());
                    }
                }
            }
        } catch (SocketException e) {
            status = "Network error: " + e.getMessage();
            return;
        }
        if (targets.isEmpty()) {
            status = "Waiting for USB tethering";
        }
    }

    @Override public void onAccuracyChanged(Sensor sensor, int accuracy) { }
    @Override public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onDestroy() {
        if (sensors != null) {
            sensors.unregisterListener(this);
        }
        if (thread != null) {
            thread.quitSafely();
        }
        if (socket != null) {
            socket.close();
        }
        if (cameraServer != null) {
            cameraServer.stop();
        }
        if (cameraThread != null) {
            cameraThread.quitSafely();
        }
        status = "Stopped";
        super.onDestroy();
    }
}
