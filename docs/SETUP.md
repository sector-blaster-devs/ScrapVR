# Setup

## 1. Stream the PC to the phone

1. Install [Sunshine](https://github.com/LizardByte/Sunshine) on the PC and [Moonlight](https://moonlight-stream.org/) on the phone.
2. Phone: **Settings → Connections → Mobile Hotspot and Tethering → USB tethering** on. The PC gets an
   RNDIS network adapter with an address like `192.168.x.y`.
3. Moonlight: add the PC by that address, pair with the PIN shown in Sunshine's web UI
   (`https://localhost:47990/pin`).
4. Render your game full screen on the PC in side-by-side stereo; Moonlight shows it on the phone.

### Keep the link up without touching the phone

With the phone inside the headset you cannot tap anything. Over adb, once:

```sh
adb shell svc usb setScreenUnlockedFunctions rndis   # tethering comes back by itself after a USB reconnect
adb shell settings put global stay_on_while_plugged_in 15   # screen stays on (and unlocked) while plugged in
```

Turn on **USB debugging** and allow the PC permanently, so a reconnect does not ask again.

Then run the watchdog on the PC. It fixes a dead tether and taps Moonlight back into the
stream after a drop (error 22 → OK → your PC → Resume Session):

```sh
python tracker/phone_watchdog.py --host MYPC    # MYPC = the PC's name in Moonlight
```

## 2. The phone app (gyro + calibration camera)

Build without Gradle (Android SDK with `build-tools;35.0.0` and `platforms;android-29`, JDK 17):

```powershell
android\build.ps1 -Sdk C:\path\to\android-sdk -Jdk C:\path\to\jdk-17
adb install -r android\build\ScrapVR-Pose.apk
adb shell pm grant studio.roundup.scrapvr android.permission.CAMERA   # only for the calibration camera
```

Open **ScrapVR Pose** once: it starts a foreground service that keeps broadcasting the gyro on
the tether while Moonlight is on screen.

## 3. The tracker (webcam on the headset)

```powershell
pip install -r requirements.txt
python tools/download_models.py
python tracker/make_markers.py assets/print.pdf     # or show assets/*.png full screen on a tablet
python tracker/head_tracker.py --calibrate           # lens, once: show the chessboard from 25 angles
python tracker/head_tracker.py --marker-mm 131 --tilt-down 20 --no-preview
```

- `--marker-mm`: the measured black edge of the marker as you show it (see
  [CALIBRATION.md](CALIBRATION.md)).
- `--tilt-down`: how far the webcam points below your eye line, until the mount is measured.
- `--no-preview` while you wear the headset: a preview window on the PC would be streamed into it.
- `--snapshot view.png` saves what the camera sees every 2 s, handy when you cannot see it yourself.

Windows note: the tracker reads the webcam through ffmpeg (`ffmpeg` on PATH), see
[LESSONS.md](LESSONS.md) for why.
