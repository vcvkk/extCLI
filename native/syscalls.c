/* SPDX-License-Identifier: Apache-2.0
 *
 * Which syscalls does this app's sandbox allow?
 *
 * The loader gets Alpine as far as running, and then the process dies with
 * SIGSYS — seccomp. The obvious next move, a SIGSYS handler that prints the
 * offending number, does not work: Android's filter answers with
 * SECCOMP_RET_KILL_PROCESS, so the kernel kills outright and nothing of ours
 * runs afterwards. The number cannot be read from inside the process that
 * asked for it.
 *
 * So it is asked from outside. For each number this forks, makes the syscall
 * in the child, and watches how the child ended: exited means the filter let
 * it through — whatever the syscall itself returned — and killed by SIGSYS
 * means it did not.
 *
 * Every number in a range, not a list of guesses. A curated list of what a
 * libc "should" call came back with three refusals, none of which musl makes
 * at startup, which only proved the list was the wrong instrument. The whole
 * range is barely slower and cannot miss.
 *
 * Nothing here can damage the device. The child closes its file descriptors
 * before asking, so every fd-taking syscall gets EBADF and has nothing to act
 * on, and every other one is called with null arguments, which is an error for
 * anything that would otherwise do something.
 */

#if !defined(__aarch64__)
#error "the syscall map is aarch64 only"
#endif

typedef unsigned long u64;
typedef long s64;

#define SYS_close 57
#define SYS_write 64
#define SYS_exit 93
#define SYS_exit_group 94
#define SYS_clock_nanosleep 115
#define SYS_kill 129
#define SYS_clone 220
#define SYS_wait4 260

#define SIGCHLD 17
#define SIGKILL 9
#define SIGSYS 31

#define WNOHANG 1

/* aarch64's table ends well below this; the tail is there so a syscall added
 * by a newer kernel is not missed. */
#define FIRST_NUMBER 0
#define LAST_NUMBER 462

/* Several syscalls wait forever when handed nothing — ppoll with no timeout
 * waits for an event that will never come. A stuck child is a fact to report,
 * not a reason for the scan to stop. */
#define PATIENCE_STEPS 60
#define PATIENCE_STEP_NS (5 * 1000 * 1000)

/* The scan forks once per number, and these are the only ones it must not make
 * in the child: they would end the child in a way that looks like an answer.
 * exit and exit_group end it quietly, which reads as "allowed" — true, and
 * they are, but measuring them means measuring the harness. */
#define SKIP_EXIT 93
#define SKIP_EXIT_GROUP 94
/* clone in the child forks again; harmless, but there is nothing to learn from
 * it that the outer clone has not already shown. */
#define SKIP_CLONE 220
#define SKIP_CLONE3 435
/* rt_sigreturn unwinds onto a signal frame that is not there and kills the
 * child with SIGSEGV, which is a fact about our arguments, not the filter. */
#define SKIP_SIGRETURN 139

static s64 syscall6(long n, long a, long b, long c, long d, long e, long f)
{
	register long x8 __asm__("x8") = n;
	register long x0 __asm__("x0") = a;
	register long x1 __asm__("x1") = b;
	register long x2 __asm__("x2") = c;
	register long x3 __asm__("x3") = d;
	register long x4 __asm__("x4") = e;
	register long x5 __asm__("x5") = f;

	__asm__ volatile("svc #0"
			 : "+r"(x0)
			 : "r"(x8), "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5)
			 : "memory", "cc");
	return x0;
}

static void put(const char *text, u64 length)
{
	syscall6(SYS_write, 1, (long)text, (long)length, 0, 0, 0);
}

static void put_number(u64 value)
{
	char digits[24];
	u64 i = sizeof(digits);

	if (!value)
		digits[--i] = '0';
	while (value) {
		digits[--i] = (char)('0' + value % 10);
		value /= 10;
	}
	put(digits + i, sizeof(digits) - i);
}

#define WIFSIGNALED(status) (((status) & 0x7f) != 0x7f && ((status) & 0x7f) != 0)
#define WTERMSIG(status) ((status) & 0x7f)

struct timespec {
	long seconds;
	long nanoseconds;
};

static void pause_briefly(void)
{
	struct timespec wanted = { 0, PATIENCE_STEP_NS };

	syscall6(SYS_clock_nanosleep, 0, 0, (long)&wanted, 0, 0, 0);
}

#define ANSWER_ALLOWED 1
#define ANSWER_REFUSED 0
#define ANSWER_CANNOT_ASK (-1)
#define ANSWER_DIED (-2)
#define ANSWER_HUNG (-3)

static int skipped(long number)
{
	return number == SKIP_EXIT || number == SKIP_EXIT_GROUP ||
	       number == SKIP_CLONE || number == SKIP_CLONE3 ||
	       number == SKIP_SIGRETURN;
}

static int ask(long number)
{
	long child = syscall6(SYS_clone, SIGCHLD, 0, 0, 0, 0, 0);
	if (child == 0) {
		/* Nothing to act on: no file descriptors, null arguments. */
		syscall6(SYS_close, 0, 0, 0, 0, 0, 0);
		syscall6(SYS_close, 1, 0, 0, 0, 0, 0);
		syscall6(SYS_close, 2, 0, 0, 0, 0, 0);
		syscall6(number, 0, 0, 0, 0, 0, 0);
		syscall6(SYS_exit, 0, 0, 0, 0, 0, 0);
		__builtin_unreachable();
	}
	if (child < 0)
		return ANSWER_CANNOT_ASK;

	int status = 0;
	int hung = 1;
	for (int i = 0; i < PATIENCE_STEPS; i++) {
		long done = syscall6(SYS_wait4, child, (long)&status, WNOHANG,
				     0, 0, 0);
		if (done < 0)
			return ANSWER_CANNOT_ASK;
		if (done == child) {
			hung = 0;
			break;
		}
		pause_briefly();
	}
	if (hung) {
		syscall6(SYS_kill, child, SIGKILL, 0, 0, 0, 0);
		syscall6(SYS_wait4, child, (long)&status, 0, 0, 0, 0);
		/* it got as far as waiting, so the filter let it through */
		return ANSWER_HUNG;
	}
	if (WIFSIGNALED(status))
		return WTERMSIG(status) == SIGSYS ? ANSWER_REFUSED : ANSWER_DIED;
	return ANSWER_ALLOWED;
}

void loader_main(u64 *sp);

__attribute__((visibility("hidden"))) void loader_main(u64 *sp)
{
	(void)sp;

	for (long number = FIRST_NUMBER; number <= LAST_NUMBER; number++) {
		int answer;

		if (skipped(number))
			continue;
		answer = ask(number);
		/* Only the refusals are printed. Four hundred lines of "allowed"
		 * is a wall to scroll past on a phone, and the question was
		 * never which calls work. */
		if (answer != ANSWER_REFUSED)
			continue;
		put_number((u64)number);
		put("\n", 1);
	}
	put("end\n", 4);
	syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
	__builtin_unreachable();
}

__asm__(".global _start\n"
	"_start:\n"
	"	mov x0, sp\n"
	"	b loader_main\n");
