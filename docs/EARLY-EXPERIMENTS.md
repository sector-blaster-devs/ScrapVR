# Early wired-display experiment

An earlier Android wired VR display prototype was built in several iterations. It never passed its audit, as reported by the developer, and is not presented here as a working or audited component. Its original application binaries are excluded from this standalone repository.

The current route uses Sunshine on the PC, Moonlight on the phone, USB tethering, and a separate gyro service. The tracker and ADB watchdog are separate PC-side tools.

## ADB heartbeat and recovery

`tracker/phone_watchdog.py` polls every three seconds while an authorized ADB device is connected. It restores `rndis` USB tethering, probes Sunshine from the phone, cycles a dead tether, dismisses Moonlight connection errors and navigates back to a streaming session.

The watchdog does not cold-launch a stopped Moonlight app. Start Moonlight over ADB before running it:

```sh
adb shell monkey -p com.limelight -c android.intent.category.LAUNCHER 1
python tracker/phone_watchdog.py --host MYPC
```

`MYPC` is the paired computer name shown by Moonlight. Pairing and USB debugging authorization must already be completed. The watchdog waits for ADB to return after an unplug; it cannot restore a physically disconnected cable or authorize a new computer.

See [SETUP.md](SETUP.md) for persistent tether configuration and setup instructions.
