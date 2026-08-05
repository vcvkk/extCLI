/* SPDX-License-Identifier: Apache-2.0
 *
 * Can a binary we built ourselves be started from the plugin's own directory?
 *
 * The device has already said that execve of anything we write is refused, and
 * that /system/bin/linker64 will nonetheless map and run one of our files. What
 * it has not said is whether that still holds for a binary that came from us
 * rather than from /system — everything measured so far used a copy of toybox.
 *
 * This is the smallest possible answer to that. No libc, no relocations worth
 * the name, two syscalls: write the marker, exit. If the linker runs it, then
 * the loader that comes next — the one that maps a guest ELF and jumps to it —
 * is a bigger program of exactly this shape, and the approach is sound.
 *
 * Freestanding on purpose. A loader needs raw syscalls anyway, and building
 * this way needs no NDK sysroot at all: clang can target aarch64 by itself.
 */

#if defined(__aarch64__)
#define SYS_write 64
#define SYS_exit_group 94

static long sys3(long number, long a, long b, long c)
{
	register long x8 __asm__("x8") = number;
	register long x0 __asm__("x0") = a;
	register long x1 __asm__("x1") = b;
	register long x2 __asm__("x2") = c;

	__asm__ volatile("svc #0"
			 : "+r"(x0)
			 : "r"(x8), "r"(x1), "r"(x2)
			 : "memory", "cc");
	return x0;
}

#elif defined(__arm__)
/* 32-bit ARM has its own numbers and passes the call in r7 */
#define SYS_write 4
#define SYS_exit_group 248

static long sys3(long number, long a, long b, long c)
{
	register long r7 __asm__("r7") = number;
	register long r0 __asm__("r0") = a;
	register long r1 __asm__("r1") = b;
	register long r2 __asm__("r2") = c;

	__asm__ volatile("svc #0"
			 : "+r"(r0)
			 : "r"(r7), "r"(r1), "r"(r2)
			 : "memory", "cc");
	return r0;
}

#else
#error "extCLI's native probe has syscalls for aarch64 and arm only"
#endif

static const char marker[] = "extcli-native-ok\n";

/* The linker jumps here. There is no libc to return to, so it never returns. */
void _start(void)
{
	sys3(SYS_write, 1, (long)marker, sizeof(marker) - 1);
	sys3(SYS_exit_group, 0, 0, 0);
	__builtin_unreachable();
}
