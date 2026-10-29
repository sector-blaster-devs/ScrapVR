# Protocol

Everything the receiver (the program rendering the VR view on the PC) gets, as JSON over
**UDP port 16061**. Each message has a `type`, so all senders share the one port.

## Axes

- **Head axes:** x right, y up, z back (towards you). Same as Godot's camera space.
- **Camera axes** (webcam, OpenCV): x right, y down, z forward out of the lens.

## `scrapvr_pose`: the phone's gyro

Sent by the phone app as a broadcast on the USB-tether subnet (no IP to configure), ~50 Hz.

```json
{"type": "scrapvr_pose", "yaw": 0.1234, "pitch": -0.0567, "roll": 0.0012, "recenter_id": 815}
```

- `yaw`: heading of the view, radians, clockwise. No fixed zero (no compass): use the change.
- `pitch`: radians, **negative looking up**. Gravity-referenced, so it can be used absolute.
- `recenter_id`: changes when the app restarts; reset your yaw baseline when it does.

## `scrapvr_head_pos`: where the marker is, from the webcam

```json
{"type": "scrapvr_head_pos", "tx": 0.02, "ty": -0.11, "tz": 0.97, "tracked": true, "tilt": 20.0}
```

- `tx, ty, tz`: the marker's centre in **camera axes**, metres. Only the position is sent: a
  single flat marker seen head-on has an ambiguous tilt, so its rotation is not trusted.
- `tracked: false` while the marker is out of view: hold the last position.
- The receiver turns this into the eyes' position with the gyro's rotation (see
  `eye_in_room()` in [tracker/head_tracker.py](../tracker/head_tracker.py)):
  `eyes = -(R_head · cam_to_head · t) + R_head · eyes_from_lens`.

## `scrapvr_hands`: hands from the webcam, ~20 Hz

```json
{"type": "scrapvr_hands", "tilt": 20.0, "hands": [
  {"side": "right", "pinch": false, "grab": true, "p": [x0, y0, z0, "...", x20, y20, z20]}]}
```

- `p`: the 21 MediaPipe joints, camera axes, metres. An empty `hands` list = no hands.
- `pinch`, `grab`: gesture state with hysteresis (see `--calibrate-hands`).

## The mount, in every tracker message

- `tilt`: degrees the webcam points below the eye line (from `--tilt-down`).
- When measured by `--calibrate-mount`, also:
  - `cam`: the camera axes in head axes, 9 numbers, row-major 3×3
  - `eyes`: the eyes relative to the lens, head axes, metres

  Use them instead of `tilt`.

## TCP 16062: the phone's camera (calibration only)

Served by the phone app on the tether. One command per line:

- `INFO` → one JSON line: image size, focal length, sensor size, Android lens calibration if
  the phone has one, `sensor_orientation`, `display_rotation`.
- `SNAP` → 4-byte big-endian length, then a JPEG (length 0 = no frame).

The camera is open only while a client is connected.
