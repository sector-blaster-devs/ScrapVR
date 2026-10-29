# Calibration

Every number that places your head and hands should be measured, not guessed. Status of each:

| What | How | Status |
|---|---|---|
| Webcam lens | `--calibrate` | **done**: reprojection error 0.106 px on our Lenovo 510 (86° horizontal FOV, not the 70° we guessed) |
| Marker size | measure, or compute from the screen | inferred (131 mm on a 12.9" tablet), not yet measured |
| Phone camera lens | same chessboard routine, phone frames | to do |
| Webcam → eyes (mount) | `--calibrate-mount` | first attempt failed, multi-pose version planned |
| Gestures | `--calibrate-hands` | built, not yet run on a person |

## Lens: a chessboard on a tablet works

No printer? Show `assets/calibration_screen.png` full screen on a tablet. Lens calibration only
needs a flat board with square squares: **the board's true size does not matter for the lens**
(it only scales the board's distance, not the focal length or the distortion). Hold the camera
still, move the tablet (or the other way round), and pause at each spot so frames are sharp:

```sh
python tracker/head_tracker.py --calibrate
```

## Marker size

The head position scales directly with the marker size you pass. Measure the black square's
edge with a ruler. On a screen you can also compute it: the image's pixel size on screen ÷ the
screen's pixels per inch × 25.4. A 1800 px image with a 1200 px marker, shown full height on a
264 ppi screen 2048 px tall, gives 2048 × 1200/1800 ÷ 264 × 25.4 = 131 mm.

## Mount: where the webcam sits relative to your eyes

The phone's rear camera looks exactly where your eyes look, and the gyro, the screen and that
camera share the phone's axes. So the phone camera *is* the eye view, and comparing the two
cameras' views of one board gives the webcam's real rotation and position relative to the eyes.

**Attempt 1 (built, failed on hardware):** both cameras photograph the chessboard at the same
moment. It passes a synthetic test to 0.22° / 2.2 mm. But with the webcam aimed down at the
hands and the phone camera looking ahead, the two views barely overlap: the board was never in
both at once.

**Next:** move the head deliberately between several poses (nod, turn, tilt). The phone camera
sees the board in some, the webcam in others, and the gyro measures every turn in between. One
nod is not enough: rotations about different axes are needed to pin the mount down fully.

A planned addition is a measured floor reference around the chair (sheets of known size with a
few hand-drawn black squares), so the down-facing webcam always has a fixed target.

## Gestures

```sh
python tracker/head_tracker.py --calibrate-hands   # OPEN, FIST, PINCH, ~7 s each, one hand in view
```

Gestures are ratios to your own palm length (wrist to middle knuckle), so they do not depend on
the hand model's assumed hand size. The calibration puts the on/off thresholds at 40% / 60% between
your measured poses. It changes gesture state only, never where the hand is placed.
