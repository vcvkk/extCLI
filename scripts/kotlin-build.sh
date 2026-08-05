#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Compiles kotlin/src into extcli/dex/<name>.dex.
#
# The dexes are shipped prebuilt inside the .eaf, so this only has to run when
# Kotlin sources change — a plugin build (elyb build) does not need Java at all.
#
# Toolchain discovery, in order:
#   1. env vars: KOTLINC, D8_JAR / R8_JAR, ANDROID_JAR
#   2. EXTCLI_TOOLCHAIN (default /opt/extcli-toolchain) holding
#      kotlinc/, r8.jar, android-all.jar
#   3. a local Android SDK (ANDROID_HOME / ANDROID_SDK_ROOT)
#
# android-all.jar (org.robolectric:android-all) works as the compile classpath
# and is a single download, unlike a full SDK install.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SRC_DIR="$REPO_ROOT/kotlin/src"
OUT_DIR="$REPO_ROOT/kotlin-build"
DEX_OUT="$REPO_ROOT/extcli/dex"
MIN_API=24

# "<dexName>=<keepClassFqn>"
PACKAGES=(
  "terminal=dev.vcvkk.extcli.terminal.TerminalNative"
)

TOOLCHAIN="${EXTCLI_TOOLCHAIN:-/opt/extcli-toolchain}"

die() { echo "error: $*" >&2; exit 1; }
info() { echo "[kotlin-build] $*"; }

# ------------------------------------------------------------------- kotlinc
KOTLINC="${KOTLINC:-}"
if [[ -z "$KOTLINC" ]]; then
  for cand in "$TOOLCHAIN/kotlinc/bin/kotlinc" "$(command -v kotlinc || true)"; do
    [[ -x "$cand" ]] && { KOTLINC="$cand"; break; }
  done
fi
[[ -n "$KOTLINC" && -x "$KOTLINC" ]] || die "kotlinc not found.
  Install it under $TOOLCHAIN/kotlinc or export KOTLINC=/path/to/kotlinc"

# ------------------------------------------------------------------- d8 / r8
D8_JAR="${D8_JAR:-${R8_JAR:-}}"
if [[ -z "$D8_JAR" ]]; then
  for cand in "$TOOLCHAIN/r8.jar" "$TOOLCHAIN/d8.jar"; do
    [[ -f "$cand" ]] && { D8_JAR="$cand"; break; }
  done
fi
if [[ -z "$D8_JAR" ]]; then
  SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
  if [[ -n "$SDK" && -d "$SDK/build-tools" ]]; then
    BT="$(ls -1 "$SDK/build-tools" | sort -V | tail -n1)"
    [[ -f "$SDK/build-tools/$BT/lib/d8.jar" ]] && D8_JAR="$SDK/build-tools/$BT/lib/d8.jar"
  fi
fi
[[ -n "$D8_JAR" && -f "$D8_JAR" ]] || die "d8 not found.
  Put r8.jar in $TOOLCHAIN or export D8_JAR=/path/to/d8.jar
  (r8.jar: https://maven.google.com/com/android/tools/r8/)"

# ---------------------------------------------------------------- android.jar
ANDROID_JAR="${ANDROID_JAR:-}"
if [[ -z "$ANDROID_JAR" ]]; then
  for cand in "$TOOLCHAIN/android-all.jar" "$TOOLCHAIN/android.jar"; do
    [[ -f "$cand" ]] && { ANDROID_JAR="$cand"; break; }
  done
fi
if [[ -z "$ANDROID_JAR" ]]; then
  SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
  if [[ -n "$SDK" ]]; then
    PLAT="$(ls -1d "$SDK"/platforms/android-* 2>/dev/null | sort -V | tail -n1 || true)"
    [[ -n "$PLAT" && -f "$PLAT/android.jar" ]] && ANDROID_JAR="$PLAT/android.jar"
  fi
fi
[[ -n "$ANDROID_JAR" && -f "$ANDROID_JAR" ]] || die "android.jar not found.
  Put android-all.jar in $TOOLCHAIN or export ANDROID_JAR=/path/to/android.jar
  (android-all: https://repo1.maven.org/maven2/org/robolectric/android-all/)"

KOTLIN_STDLIB="$(dirname "$KOTLINC")/../lib/kotlin-stdlib.jar"
[[ -f "$KOTLIN_STDLIB" ]] || die "kotlin-stdlib.jar not found next to kotlinc"

info "kotlinc      $KOTLINC"
info "d8           $D8_JAR"
info "android.jar  $ANDROID_JAR"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR" "$DEX_OUT"

for entry in "${PACKAGES[@]}"; do
  name="${entry%%=*}"
  keep="${entry##*=}"
  pkg_path="$(echo "${keep%.*}" | tr '.' '/')"
  classes_dir="$OUT_DIR/$name/classes"
  mkdir -p "$classes_dir"

  info "compiling $name ($keep)"
  "$KOTLINC" \
    -classpath "$ANDROID_JAR" \
    -jvm-target 1.8 \
    -nowarn \
    -d "$classes_dir" \
    "$SRC_DIR/$pkg_path"/*.kt

  # R8 in dex mode: tree-shakes kotlin-stdlib so the shipped dex stays small
  cat > "$OUT_DIR/$name/keep.pro" <<EOF
-keep class $keep { *; }
-dontwarn **
-dontobfuscate
-dontoptimize
EOF

  info "dexing $name"
  java -cp "$D8_JAR" com.android.tools.r8.R8 \
    --release \
    --min-api "$MIN_API" \
    --lib "$ANDROID_JAR" \
    --pg-conf "$OUT_DIR/$name/keep.pro" \
    --output "$OUT_DIR/$name" \
    $(find "$classes_dir" -name '*.class') \
    "$KOTLIN_STDLIB"

  cp "$OUT_DIR/$name/classes.dex" "$DEX_OUT/$name.dex"
  info "wrote $DEX_OUT/$name.dex ($(du -h "$DEX_OUT/$name.dex" | cut -f1))"
done

info "done"
