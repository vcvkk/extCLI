#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Builds extCLI's own native binaries.
#
# No NDK. The programs here are freestanding — no libc, raw syscalls — so
# nothing but a clang that can emit aarch64 is needed, which is every clang.
# That matters beyond convenience: a loader has to make its own syscalls
# anyway, so the thing we would use a sysroot for is the thing we cannot use.
#
# Two shapes of the same program are built, because the device has to say
# which one bionic's linker will start:
#
#   probe        static PIE, no PT_INTERP
#   probe-interp the same with PT_INTERP naming the linker, which is what an
#                ordinary Android executable looks like
#
# musl's own loader was refused for want of a PT_PHDR; both of these have one.

set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/extcli/res/native"
CC=${CC:-clang}
LINKER64=/system/bin/linker64
LINKER32=/system/bin/linker

build() {
	abi=$1
	target=$2
	interp=$3
	dir="$OUT/$abi"
	mkdir -p "$dir"

	# -fvisibility=hidden matters: a global symbol makes the linker route a
	# branch through the PLT, and a PLT entry needs a relocation applied by
	# a dynamic linker these programs do not have. The loader crashed on its
	# first instruction until this was here.
	common="-nostdlib -nostdinc -fPIC -Os -fno-stack-protector
		-fno-builtin -fvisibility=hidden -Wall -Wextra
		-Wl,-e,_start -Wl,--build-id=none -Wl,-z,noexecstack"

	# shellcheck disable=SC2086
	$CC --target="$target" $common -shared \
		-o "$dir/probe" "$ROOT/native/probe.c"

	# -pie, not -shared: lld ignores --dynamic-linker for a shared object, so
	# the "with an interpreter" variant has to be linked as an executable or
	# it comes out identical to the other one — which it silently did once
	# shellcheck disable=SC2086
	$CC --target="$target" $common -pie \
		-Wl,--dynamic-linker="$interp" \
		-o "$dir/probe-interp" "$ROOT/native/probe.c"

	# The loader is aarch64-only for now: its syscall numbers and its jump
	# are written for that instruction set, and it says so at compile time.
	if [ "$abi" = "arm64-v8a" ]; then
		for tool in loader syscalls; do
			# shellcheck disable=SC2086
			$CC --target="$target" $common -shared \
				-o "$dir/$tool" "$ROOT/native/$tool.c"
			printf '%-14s %-8s %s bytes\n' "$abi" "$tool" \
				"$(wc -c < "$dir/$tool")"
		done
	fi

	for name in probe probe-interp; do
		printf '%-14s %-8s %s bytes\n' "$abi" "$name" \
			"$(wc -c < "$dir/$name")"
	done

	for name in "$dir"/*; do
		check_no_relocations "$name"
	done
}

# These programs are started by bionic's linker but have no dynamic linker of
# their own applying relocations to them, so any relocation left in one is a
# pointer that still holds its link-time value — a crash on first use. It has
# happened twice: a branch routed through the PLT, and an array of string
# pointers. A build that produces one should not reach a device.
check_no_relocations() {
	file=$1
	count=$(${READELF:-readelf} -r "$file" 2>/dev/null |
		grep -c "R_AARCH64\|R_ARM" || true)
	if [ "$count" -ne 0 ]; then
		echo "error: $file needs $count relocations that nothing will apply" >&2
		${READELF:-readelf} -r "$file" >&2
		exit 1
	fi
}

build arm64-v8a aarch64-linux-android21 "$LINKER64"
build armeabi-v7a armv7a-linux-androideabi21 "$LINKER32"

echo "built into $OUT"
