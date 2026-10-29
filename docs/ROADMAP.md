# Roadmap

## Next architecture: each sensor does what it sees best

```
phone gyro             → fast head rotation
phone rear camera      → head position from a marker straight ahead (it sees what you see)
webcam, aimed down     → hands, knees, reach
PC                     → timestamped fusion of all three
```

The two cameras deliberately look in different directions. Trying to make one webcam see both a
marker ahead and the hands below is what blocked the first mount calibration.

## Measure before trusting it

1. The phone camera's lens, with the same chessboard routine.
2. The marker's real black-edge size.
3. The webcam-to-head mount from several deliberate head poses, not one nod.

## Then one combined proof run

Marker visible to the phone camera, both hands visible to the webcam, gyro on. In one capture:
head turns, a small lean, hands resting on the knees, reaching, pinch and fist.

## Hardware

- **Raspberry Pi Ethernet bridge** for the webcam, plus a USB-C Ethernet adapter for the phone,
  over a small switch. That replaces fragile USB-C cables and frees the cable length.
  Needed for it: the tracker reads a network stream instead of a local webcam, the phone app
  broadcasts on `eth` too, and adb goes over the network.
- **Counterweight:** the Pi and a battery pack at the back of the head strap.

## Software

- **A standalone Godot receiver**, ideally as an `XRInterfaceExtension` plugin: Godot renders
  both eyes itself (with lens distortion built in), and a game uses the standard XR nodes, so
  the same code runs on real headsets later. To check first: whether it can be written in
  GDScript, and whether webcam hands can feed Godot's `XRHandTracker`.
- **Rest pose and reach-depth calibrations** for the hands: a known-size card at several
  reach distances, validated against one position not used in the fit.
