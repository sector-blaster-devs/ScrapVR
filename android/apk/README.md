# Android APK

[Download ScrapVR Pose](ScrapVR-Pose.apk).

Package: `studio.roundup.scrapvr`; application label: **ScrapVR Pose**; Android 8.0+ (API 26).

This development APK is built from the source in `android/` and signed with a generated development key. The key is not published. APK signature verification passed for v2 and v3. The rebranded APK has not yet been installed or tested on the phone.

It sends phone orientation over UDP while Moonlight displays the stream. The camera permission is for the optional calibration camera server.

Install and launch:

```sh
adb install -r ScrapVR-Pose.apk
adb shell am start -n studio.roundup.scrapvr/.MainActivity
```

The previous app used a different package; this installs alongside it. Run only one pose sender at a time to avoid duplicate orientation packets.
