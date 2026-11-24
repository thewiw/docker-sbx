# syntax=docker/dockerfile:1
#
# Build-time only. The final stage is `scratch`, so Debian, apt lists, curl and
# the downloaded archives never reach the sandbox. The result is three
# self-contained trees: /opt/flutter, /opt/java/openjdk and /opt/android-sdk.
# Debian 13 (trixie) no longer ships OpenJDK 17, so take the JDK from the
# official Temurin image instead of pinning an apt package that does not exist.
FROM eclipse-temurin:17-jdk-jammy AS jdk

FROM debian:trixie-slim AS build

ARG TARGETARCH
ARG FLUTTER_VERSION
ARG ANDROID_PLATFORM
ARG BUILD_TOOLS_VERSION
ARG CMDLINE_TOOLS_VERSION
ARG FLUTTER_CHANNEL=stable

# Google publishes the Android command-line tools for Linux x86_64 only, and
# Flutter has no official linux-arm64 release tarball. Fail the build instead of
# producing a sandbox whose SDKs cannot run.
RUN [ "$TARGETARCH" = "amd64" ] || { \
      echo "flutter-sdk kit: only linux/amd64 is supported (got $TARGETARCH)" >&2; \
      exit 1; \
    }

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git unzip xz-utils libatomic1 libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /out/opt
# Keep Temurin's own /opt/java/openjdk layout: any absolute symlink inside the
# JDK keeps pointing at a valid path in the final image.
COPY --from=jdk /opt/java/openjdk/ /out/opt/java/openjdk/

# The release archive contains the Dart SDK and the flutter tool.
RUN set -eux; \
    curl -fsSL \
      "https://storage.googleapis.com/flutter_infra_release/releases/${FLUTTER_CHANNEL}/linux/flutter_linux_${FLUTTER_VERSION}-${FLUTTER_CHANNEL}.tar.xz" \
      | tar -xJ -C /out/opt

# sdkmanager only works from the cmdline-tools/latest layout.
RUN set -eux; \
    curl -fsSL \
      "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS_VERSION}_latest.zip" \
      -o /tmp/cmdline-tools.zip; \
    mkdir -p /tmp/cmdline-tools /out/opt/android-sdk/cmdline-tools; \
    unzip -q /tmp/cmdline-tools.zip -d /tmp/cmdline-tools; \
    mv /tmp/cmdline-tools/cmdline-tools /out/opt/android-sdk/cmdline-tools/latest; \
    rm -rf /tmp/cmdline-tools /tmp/cmdline-tools.zip

ENV JAVA_HOME=/out/opt/java/openjdk
ENV PATH="/out/opt/java/openjdk/bin:/out/opt/android-sdk/cmdline-tools/latest/bin:${PATH}"

# Accept the SDK licences once, then install only what an Android Flutter build
# needs. No emulator image, NDK, CMake or sources: they are multi-GB, and the
# emulator cannot be hardware-accelerated inside a local sbx microVM anyway.
RUN set -eux; \
    yes | sdkmanager --sdk_root=/out/opt/android-sdk --licenses >/dev/null; \
    sdkmanager --sdk_root=/out/opt/android-sdk \
      "platform-tools" \
      "platforms;android-${ANDROID_PLATFORM}" \
      "build-tools;${BUILD_TOOLS_VERSION}"

# Run the SDK caches as the sandbox agent (uid 1000), so Flutter can write to
# its own bin/cache later without root. No passwd entry is needed: the composed
# workload already provides the `agent` user, and a build-time uid is enough.
RUN mkdir -p /home/agent \
 && chown -R 1000:1000 /out/opt /home/agent

USER 1000:1000
ENV HOME=/home/agent
ENV PATH="/out/opt/flutter/bin:/out/opt/java/openjdk/bin:/out/opt/android-sdk/cmdline-tools/latest/bin:/out/opt/android-sdk/platform-tools:${PATH}"

# Precache the Android engine artifacts: `flutter test` and `flutter build apk`
# should not need a first-run download just to start. The version checks make
# the pins honest before the kit claims them in `provides`.
RUN set -eux; \
    flutter --version | grep -q "Flutter ${FLUTTER_VERSION}"; \
    flutter config --no-analytics; \
    flutter precache --android; \
    dart --version; \
    sdkmanager --list_installed | grep -q "platforms;android-${ANDROID_PLATFORM}"

FROM scratch
COPY --from=build /out/ /
ENV JAVA_HOME=/opt/java/openjdk \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    PATH=/opt/flutter/bin:/opt/java/openjdk/bin:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH
