# Lessons: what broke, and how we found it

The useful part of building in public. Each of these cost real time, and most were found by a
test designed to fail first.

## OpenCV's default undistortion does not converge for a wide lens

`cv2.undistortPoints` stops after 5 iterations. For our webcam (strong 6th-order term,
k3 = 1.44), points near the frame edges came back up to **60 px** off, and `solvePnP` uses the
same default internally. With 200 iterations
(`criteria=(COUNT|EPS, 200, 1e-12)`) the round trip is 0.00 px across the whole frame. This
silently broke both the marker and the hands near the edges.

## A single flat marker seen head-on flips

Solving the marker's full pose (IPPE) gave head positions up to 40 cm off, because a square seen
nearly head-on has two near-equal solutions for its tilt. The fix: trust only the marker's
**position** in the camera, and take rotation from the gyro. Our self-test caught it before
any hardware did.

## The hand model's 3D shape is not a physical scale

`solvePnP` between MediaPipe's world landmarks and the picture put a hand resting on a touchpad
at 13 cm (truly ~45), with 35–80 px of error. Distance now comes from the palm's apparent size
through the calibrated lens, with each joint placed on its own pixel's ray. The joints then sit
exactly on the picture.

## OpenCV 5 on Windows ignored MJPG

`CAP_PROP_FOURCC = MJPG` was ignored over DirectShow, and the camera fell back to YUY2: 7.5 fps at
720p. The Media Foundation backend hung on open. Reading the webcam through
`ffmpeg -f dshow -vcodec mjpeg` gives the full 30 fps, and keeping only the newest frame
avoids any lag build-up.

## A USB tether can look up and be dead

After a cable blip, the phone's `rndis0` kept its address but had sent **0 packets**. Nothing
reached the PC. Toggling the USB function (`svc usb setFunctions` then `… rndis`) over adb
fixes it, and the watchdog now tests reachability from the phone side.

## Smaller ones

- **Synthetic tests need anti-aliased renders.** A plain perspective warp aliased the board's
  edges and faked a 0.86° error in the mount test. Rendering at 4× and averaging down gives 0.22°.
- **SQPnP raises instead of returning "not ok"** on degenerate input. Uncaught, that would have
  ended the hand thread for good.
- **Two hands can get the same label.** With two in view, side now comes from wrist position
  (an outward camera sees your left hand on the left).
- **Fixed gesture thresholds latch.** A half-closed holding grip sat right at the grab threshold,
  so thresholds are now palm-relative ratios with per-person calibration.
- **Moonlight's OK button has invisible bidi marks** in its label, and its app tiles have no text
  at all (tap them by resource id).
- **Android 13 only gives the camera to the app on screen**, so the calibration brings the phone
  app to the front over adb first.
- **A preview window on the PC gets streamed into the headset.** Run with `--no-preview` when wearing it.
