# Flutter + Dart + Android SDK kit

This directory is the build context for the `flutter-sdk` profile. It is a
Docker Sandboxes v3 **mixin** kit, passed to `sbx create --kit` by
`docker-sbx-create-sandbox.py` when the project selects:

```bash
python3 docker-sbx-create-sandbox.py -n mobile -p /absolute/project -profile-directory flutter-sdk
```

The profile also applies the same network rules as `profiles/flutter.yaml`, so
the agent can resolve pub packages and Gradle/Maven dependencies.

## What is installed

| Path | Content |
|---|---|
| `/opt/flutter` | Flutter SDK (default 3.50.1, stable channel) and its Dart SDK |
| `/opt/java/openjdk` | Eclipse Temurin JDK 17, required by AGP 9 / Flutter 3.50 |

Debian 13 (trixie), the build stage's base, no longer packages OpenJDK 17, so
the JDK is copied from the official `eclipse-temurin:17-jdk-jammy` image
instead of installed with apt.
| `/opt/android-sdk` | command-line tools, platform-tools, `platforms;android-36`, `build-tools;36.0.0` |
| system packages | clang, CMake, Ninja, pkg-config, GTK 3 development files, mesa-utils, libGLU — installed by the create-time hook when missing |

The kit sets `JAVA_HOME`, `ANDROID_HOME`, `ANDROID_SDK_ROOT` and prepends the
three `bin` directories to `PATH`. Android SDK licences are accepted during the
build. The built-in Claude workload does not ship a JDK; if another kit or the
workload already sets one of those variables to a different value, kit
composition is refused rather than silently shadowing it.

`flutter precache --android` bakes the Android engine artifacts, so the agent's
first `flutter test` or `flutter build apk` does not need to download them.

Versions are the kit's `args`, so they can be changed without editing the
Dockerfile:

```yaml
args:
  flutter_version: 3.50.1
  android_platform: "36"
  android_build_tools: 36.0.0
  cmdline_tools_version: "15859902"
```

## Supported environment

- **x86_64 Linux only.** Google ships no official Linux arm64 build of the
  Android command-line tools, and Flutter has no official linux-arm64 release
  tarball. The build fails with a clear message on `linux/arm64`.
- **Build, unit and widget tests.** `flutter analyze`, `flutter test`,
  `flutter build apk`, `flutter build appbundle` and `flutter build linux` are
  supported; the Linux desktop toolchain (clang, CMake, Ninja, pkg-config,
  GTK 3) is installed at creation when the base image lacks it.
- **No Android emulator.** Local `sbx` sandboxes are microVMs without nested
  KVM, so the emulator cannot be hardware-accelerated. Instrumented tests need
  a physical device or a device farm.
- **No iOS builds.** They require macOS and Xcode.
- **No web toolchain.** Chrome/Chromium is not installed.

The first sandbox creation builds the kit and downloads several GB. Builds are
cached by `sbx`; later sandboxes reuse them. The default sandbox root is 20 GB;
the toolchain plus Gradle and pub caches fit, but `sbx df` is worth watching.

## Licences

The Flutter SDK is distributed under the BSD 3-Clause licence. The Android SDK
and command-line tools are subject to Google's Android SDK terms; accepting the
licences happens inside the image. This kit is intended for private use, not
for redistribution as a public image without checking those terms.
