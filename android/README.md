# Android development environment

The Android application has not been initialized. Environment setup does not
authorize implementing it, and no APK build or application tests have run.

Use the existing SDK under `$ANDROID_HOME` and Android Studio's bundled JDK 21.
The host environment fragment is `$HOME/.config/agentping/env.sh`.

Initial supported build baseline: API 36, Build Tools 36.0.0, AGP 9.2.x,
Gradle wrapper 9.4.1. Recheck the current official compatibility matrix when
initializing the Android project; pin Kotlin/Compose compiler, KSP, Room, and
Firebase dependencies together. Do not install global Gradle or Kotlin.

Use a Google Play ARM64 image for FCM testing. A physical Android phone must
have Google Play services, USB debugging enabled, and this Mac authorized.
Use `adb devices -l`, `adb logcat`, and `adb reverse tcp:8000 tcp:8000` as needed.
Doze, reboot, permission revocation, and ringing tests belong to implementation
and must target a dedicated test device, never an unspecified default device.

Registered application IDs: `com.errorfactory.agentping` and
`com.errorfactory.agentping.debug`. Firebase configuration is stored in ignored
`config/firebase/release/google-services.json` and
`config/firebase/debug/google-services.json`. When implementing the Android
project, copy these into the appropriate `app/src/release/` and `app/src/debug/`
locations and use `applicationIdSuffix = ".debug"` for development.

Keep debug and production device tokens and backend base URLs separate. Never
put backend database credentials, the development API token, or service-account
keys in an Android build. Public Firebase configuration is expected in an APK.
