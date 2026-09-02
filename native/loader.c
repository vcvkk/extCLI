/* SPDX-License-Identifier: Apache-2.0
 *
 * extCLI's ELF loader: does the kernel's job, because the kernel will not.
 *
 * The device forbids execve of anything the app can write, and bionic's linker
 * will not start a foreign libc's programs — it maps Alpine's busybox happily
 * enough and then musl crashes, because musl expects to have been started as a
 * program interpreter and to have done its own initialisation. Both of those
 * are the same complaint from different directions: nobody will set up a guest
 * binary the way it expects.
 *
 * So this does. It is started by /system/bin/linker64, which is allowed, and
 * then it performs an ordinary ELF load by hand: map the segments, load the
 * program's interpreter if it names one, build a fresh stack carrying argc,
 * argv, envp and an auxiliary vector, and branch to the entry point. From the
 * guest's first instruction onward nothing is unusual — it was started the way
 * Linux starts programs, so its own libc initialises normally, whichever libc
 * that is.
 *
 * Freestanding: no libc, raw syscalls, no relocations to speak of. That is not
 * minimalism for its own sake. A loader that used a libc would have that
 * libc's startup already done and its own idea of the stack, and the whole
 * point here is to control both.
 *
 * Called as:
 *
 *   linker64 <loader> extcli-loader-v1 <host path of the ELF> <argv0> [args...]
 *
 * The sentinel is there because how many arguments arrive ahead of ours is not
 * ours to decide: bionic's linker rewrites the stack before jumping here, and
 * whether it removes itself and the loader from argv is its business and has
 * changed between releases. Searching for a word we put there ourselves works
 * whichever way it went; counting from argv[1] worked on a desktop and opened
 * the wrong argument on the phone.
 *
 * The rootfs, for resolving the interpreter, comes from EXTCLI_ROOT.
 */

#if !defined(__aarch64__)
#error "extCLI's loader is aarch64 only for now"
#endif

typedef unsigned long u64;
typedef long s64;
typedef unsigned int u32;
typedef unsigned short u16;

#define NULL ((void *)0)

/* ------------------------------------------------------------- syscalls */

#define SYS_openat 56
#define SYS_close 57
#define SYS_read 63
#define SYS_write 64
#define SYS_pread64 67
#define SYS_exit_group 94
#define SYS_mmap 222
#define SYS_mprotect 226
#define SYS_rt_sigaction 134
#define SYS_ptrace 117
#define SYS_clone 220
#define SYS_wait4 260
#define SYS_kill 129
#define SYS_getpid 172

#define AT_FDCWD (-100)
#define O_RDONLY 0

#define PROT_READ 1
#define PROT_WRITE 2
#define PROT_EXEC 4
#define MAP_PRIVATE 2
#define MAP_FIXED 16
#define MAP_ANONYMOUS 32

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

#define sys1(n, a) syscall6((n), (long)(a), 0, 0, 0, 0, 0)
#define sys3(n, a, b, c) syscall6((n), (long)(a), (long)(b), (long)(c), 0, 0, 0)
#define sys4(n, a, b, c, d) \
	syscall6((n), (long)(a), (long)(b), (long)(c), (long)(d), 0, 0)
#define sys6(n, a, b, c, d, e, f)                                    \
	syscall6((n), (long)(a), (long)(b), (long)(c), (long)(d), \
		 (long)(e), (long)(f))

static void put(const char *text, u64 length)
{
	sys3(SYS_write, 2, text, length);
}

static u64 string_length(const char *text)
{
	u64 n = 0;
	while (text[n])
		n++;
	return n;
}

/* Dies loudly. The console shows stderr, so this is how a failure is read.
 *
 * `detail` is not decoration. "cannot open the program" without saying which
 * path it tried is a message that needs a round trip through a phone to act
 * on, and this loader has no debugger and no logs. */
static void fail2(const char *what, const char *detail)
{
	put("extcli-loader: ", 15);
	put(what, string_length(what));
	if (detail) {
		put(": ", 2);
		put(detail, string_length(detail));
	}
	put("\n", 1);
	sys1(SYS_exit_group, 1);
	__builtin_unreachable();
}

static void fail(const char *what)
{
	fail2(what, NULL);
}

/* Prints what the loader was actually handed. Which argument is the program
 * depends on how bionic's linker rewrites the stack before jumping here, and
 * that is not something to guess at from the other side of a device. */
static void show_arguments(int argc, char **argv)
{
	char count[2];

	count[0] = (char)('0' + (argc > 9 ? 9 : argc));
	count[1] = '\n';
	put("extcli-loader: argc ", 20);
	put(count, 2);
	for (int i = 0; i < argc && i < 6; i++) {
		put("extcli-loader:   [", 18);
		count[0] = (char)('0' + i);
		put(count, 1);
		put("] ", 2);
		put(argv[i], string_length(argv[i]));
		put("\n", 1);
	}
}

/* The word Python puts in front of the loader's own arguments. */
static const char sentinel[] = "extcli-loader-v1";
/* And the one the loader puts there itself, when it turns a guest's exec into
 * a start of its own: it means a supervisor is already stepping this process,
 * so do not fork another. Two of them would translate every path twice. */
static const char sentinel_child[] = "extcli-loader-child-v1";

static int same_string(const char *a, const char *b)
{
	u64 i = 0;
	while (a[i] && a[i] == b[i])
		i++;
	return a[i] == 0 && b[i] == 0;
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

/* ---------------------------------------------------- seccomp, made legible
 *
 * Android filters an app's syscalls, and a refused one arrives as SIGSYS with
 * no indication of which. The signal handler survives into the guest, because
 * a userland exec is not an exec — nothing resets the disposition — so a guest
 * that trips the filter reports the syscall number instead of dying mute.
 *
 * Which is the whole difficulty here: "killed by SIGSYS" is a fact about the
 * sandbox, and the number is the fact that can be acted on.
 */

#define SIGSYS 31
#define SA_SIGINFO 4

struct kernel_sigaction {
	void *handler;
	unsigned long flags;
	void *restorer;
	unsigned long mask;
};

static void on_sigsys(int number, void *info, void *context)
{
	(void)number;
	(void)context;
	/* siginfo_t for SIGSYS: si_signo, si_errno, si_code, then the union at
	 * offset 16 — _call_addr, then _syscall as an int at 24. */
	int refused = info ? ((int *)info)[6] : -1;

	put("extcli-loader: the sandbox refused syscall ", 43);
	put_number((u64)(refused < 0 ? 0 : refused));
	put("\n", 1);
	sys1(SYS_exit_group, 1);
}

static void watch_for_seccomp(void)
{
	struct kernel_sigaction action;

	action.handler = (void *)on_sigsys;
	action.flags = SA_SIGINFO;
	action.restorer = NULL;
	action.mask = 0;
	/* the last argument is the size of the signal mask, not of the struct */
	sys4(SYS_rt_sigaction, SIGSYS, &action, 0, sizeof(action.mask));
}

/* --------------------------------------------------- supervising the guest
 *
 * Android's filter answers a refused syscall with SECCOMP_RET_KILL_PROCESS.
 * Not EPERM — death, with no chance to react: a SIGSYS handler never runs, and
 * the number cannot be read from inside the process that asked. `rootfs
 * syscalls` therefore asks from outside, one forked child per number, and
 * `rootfs trace` steps a guest to see which of them it reaches.
 *
 * Alpine's busybox reaches setgid, and then setuid. It drops privileges at
 * startup by calling them with the ids it already has — asking to become the
 * user it already is — and on this device that is fatal. Nothing is wrong with
 * busybox and nothing is wrong with the loader; an ordinary Linux kernel would
 * have returned 0 to both.
 *
 * So the loader stops handing the guest's syscalls to the filter. It forks,
 * the child asks to be traced and loads the guest as usual, and the parent
 * steps it. At each syscall-entry stop the parent looks the number up in the
 * list it was given, and a refused one is *replaced* — the guest's x8 is
 * rewritten to getpid before the kernel goes any further, and the answer the
 * guest wanted is written over the result at the exit stop: 0 for a call that
 * would have succeeded anyway, -EPERM for one that genuinely cannot happen
 * here. The guest gets an answer instead of a bullet, which is what an
 * unprivileged process on any other Linux gets.
 *
 * Replaced rather than cancelled. A tracer can also set the number to -1,
 * which means "do not run this one", and that is what this did first — and the
 * guest died at exactly the same place. The device's own scan says why: it
 * refuses 240 numbers, and most of them are numbers this kernel has no syscall
 * for at all. A filter that kills unknown numbers kills -1 too. getpid is a
 * number it knows, takes no arguments, and touches nothing.
 *
 * The list arrives in EXTCLI_BLOCKED as `number[:value]`, comma separated,
 * because which numbers a device refuses is measured on the device and is not
 * for this file to guess. Without it the loader does not supervise at all and
 * costs nothing. EXTCLI_TRACE turns on the reporting side.
 */

#define PTRACE_TRACEME 0
#define PTRACE_CONT 7
#define PTRACE_SYSCALL 24
#define PTRACE_SETOPTIONS 0x4200
#define PTRACE_GETREGSET 0x4204
#define PTRACE_SETREGSET 0x4205
#define PTRACE_O_TRACESYSGOOD 1
#define PTRACE_O_TRACEFORK 2
#define PTRACE_O_TRACEVFORK 4
#define PTRACE_O_TRACECLONE 8
#define PTRACE_O_TRACEEXEC 0x10
#define PTRACE_O_EXITKILL 0x100000
#define PTRACE_EVENT_EXEC 4
/* says outright whether a stop is a syscall's entry or its exit, so the
 * alternation does not have to be assumed to have started on the right foot */
#define PTRACE_GET_SYSCALL_INFO 0x420e
#define SYSCALL_INFO_ENTRY 1
#define SYSCALL_INFO_EXIT 2
#define NT_PRSTATUS 1
/* the one register set that lets a tracer change *which* syscall runs; on
 * arm64 the number does not live in x8 once the kernel has read it */
#define NT_ARM_SYSTEM_CALL 0x404

/* children of the guest are ordinary processes, but a thread would be
 * invisible to wait4 without this */
#define WALL 0x40000000

#define SIGSTOP 19
#define SIGTRAP 5
/* what TRACESYSGOOD adds so a syscall stop is not confused with a breakpoint */
#define SYSCALL_STOP (SIGTRAP | 0x80)

#define WIFEXITED(s) (((s) & 0x7f) == 0)
#define WEXITSTATUS(s) (((s) >> 8) & 0xff)
#define WIFSIGNALED(s) (((s) & 0x7f) != 0x7f && ((s) & 0x7f) != 0)
#define WTERMSIG(s) ((s) & 0x7f)
#define WIFSTOPPED(s) (((s) & 0xff) == 0x7f)
#define WSTOPSIG(s) (((s) >> 8) & 0xff)

/* enough to see what a libc was doing when it died */
#define REMEMBERED 12
/* Indexed by syscall number rather than searched. The first version held
 * sixty-four rules in a list, which was written before the device answered:
 * it refuses 240 numbers, and the ones past the sixty-fourth would have been
 * quietly dropped. */
#define MAX_SYSCALL 512
/* the guest, and whatever it starts */
/* How many processes and threads may be supervised at once.
 *
 * It was 64, and a tracee that did not fit was resumed with its paths
 * untranslated — `uv tool install` failed with "No such file or directory"
 * against a path that was plainly there, because the thread that opened it was
 * the sixty-fifth. A Rust program with a blocking pool has hundreds of
 * threads, so the number is now one nobody reaches by working normally, and
 * running out is counted and reported rather than passed over.
 */
#define MAX_TRACEES 512

/* A syscall the filter allows, that takes no arguments and changes nothing.
 * A refused call is turned into this one rather than into no call at all. */
#define SYSCALL_HARMLESS 172 /* getpid */

/* An unnamed file, and the answer a filesystem gives when it has none.
 *
 * apk creates its downloads with O_TMPFILE and links them into place through
 * /proc/self/fd — the modern way, and the right one everywhere else. Here that
 * link crosses a mount boundary and comes back EXDEV, measured by `rootfs
 * writes`, so apk fetches an index it can never put anywhere.
 *
 * Every program that uses O_TMPFILE has a fallback for a filesystem that does
 * not support it, and this device is that filesystem in every way that matters.
 * So the loader says so, in the words the fallback is written for. Turned on
 * from outside, because whether a link works here is measured rather than
 * assumed.
 */
#define O_TMPFILE_BIT 020000000
#define EOPNOTSUPP 95

/* A second name for a file that already has one.
 *
 * `apk add unzip` installed every file in the package except usr/bin/zipinfo,
 * which is the one hardlink in it — unzip and zipinfo are the same program
 * under two names. The call comes back refused, and which refusal depends on
 * where the rootfs happens to sit: an app's own storage is ext4 and allows it,
 * emulated storage is FUSE and has never supported hardlinks at all.
 *
 * A hardlink asks for one thing — this content, under that name too — and a
 * copy delivers exactly that. The two differ in ways that matter to a
 * filesystem and, for the contents of a package, to nobody: the link count is
 * not two, and writing through one name does not show through the other.
 * Nothing in a rootfs writes to its own programs.
 *
 * So when the kernel refuses, the supervisor copies instead. It runs as the
 * same uid on the same filesystem, so what the guest could not do it can do
 * plainly, and the guest is told the link worked. This is what proot does, for
 * the same reason.
 */
#define SYS_linkat 37
#define SYS_unlinkat 35
#define SYS_fstat 80
#define EPERM 1
#define EACCES 13
#define EXDEV 18
#define EMLINK 31
#define ENOSYS 38
#define O_RDONLY 0
#define O_WRONLY 1
#define O_CREAT 0100
#define O_EXCL 0200
#define O_TRUNC 01000

/* One page at a time. A program in a rootfs is a couple of hundred kilobytes,
 * and this runs once per hardlink in a package — which for most packages is
 * never. */
#define COPY_CHUNK 65536
static char copy_buffer[COPY_CHUNK];

/* The kernel's `struct stat` for a 64-bit asm-generic architecture, which
 * aarch64 is. Written out rather than reached into by offset: the one field
 * wanted is the mode, and getting its position wrong would copy a program
 * without its execute bit — a failure that would look like anything but this.
 * Spelled the way the kernel spells it so it can be checked against
 * include/uapi/asm-generic/stat.h by reading, not by counting.
 */
struct kernel_stat {
	u64 st_dev;
	u64 st_ino;
	u32 st_mode;
	u32 st_nlink;
	u32 st_uid;
	u32 st_gid;
	u64 st_rdev;
	u64 __pad1;
	s64 st_size;
	int st_blksize;
	int __pad2;
	s64 st_blocks;
	s64 st_atime_sec;
	u64 st_atime_nsec;
	s64 st_mtime_sec;
	u64 st_mtime_nsec;
	s64 st_ctime_sec;
	u64 st_ctime_nsec;
	u32 __unused[2];
};

static int copy_file(const char *from, const char *to)
{
	struct kernel_stat info;
	long source;
	long target;
	unsigned int mode = 0755;
	int failed = 0;

	source = sys4(SYS_openat, AT_FDCWD, from, O_RDONLY, 0);
	if (source < 0)
		return 0;
	if (sys3(SYS_fstat, source, &info, 0) == 0) {
		/* the permission bits only; the type bits belong to the
		 * original and a copy is always an ordinary file */
		if (info.st_mode & 07777)
			mode = info.st_mode & 07777;
	}
	/* O_EXCL: a hardlink that would have overwritten something is a
	 * different question, and answering it by truncating somebody's file
	 * is not this function's business */
	target = sys4(SYS_openat, AT_FDCWD, to,
		      O_WRONLY | O_CREAT | O_EXCL | O_TRUNC, mode);
	if (target < 0) {
		sys1(SYS_close, source);
		return 0;
	}
	for (;;) {
		s64 got = sys3(SYS_read, source, copy_buffer, COPY_CHUNK);
		s64 done = 0;

		if (got < 0) {
			failed = 1;
			break;
		}
		if (got == 0)
			break;
		while (done < got) {
			s64 wrote = sys3(SYS_write, target,
					 copy_buffer + done, got - done);
			if (wrote <= 0) {
				failed = 1;
				break;
			}
			done += wrote;
		}
		if (failed)
			break;
	}
	sys1(SYS_close, source);
	sys1(SYS_close, target);
	if (failed) {
		/* a half-written program is worse than a missing one: it would
		 * run, and fail somewhere with nothing to do with this */
		sys3(SYS_unlinkat, AT_FDCWD, to, 0);
		return 0;
	}
	return 1;
}

/* Is this the kind of refusal a copy can stand in for? */
static int link_refused(long result)
{
	return result == -EPERM || result == -EACCES || result == -EXDEV ||
	       result == -EMLINK || result == -ENOSYS;
}
#define SYS_OPENAT 56

struct iovec {
	void *base;
	u64 length;
};

static int no_tmpfile;
static unsigned char rule_set[MAX_SYSCALL];
static long rule_value[MAX_SYSCALL];
static int rule_count;

/* Mapped before the fork, so the guest has the same page at the same address
 * without ever having been asked for it. */
static u64 scratch;

static long tracee_pid[MAX_TRACEES];
/* where each one keeps the scratch page. Per tracee because an exec moves it:
 * what comes back is a different image with a mapping of its own. This is the
 * start of the whole area; the part a tracee may write to is its slot's, and
 * `scratch_area` works that out. */
static u64 tracee_scratch[MAX_TRACEES];
static unsigned char tracee_entering[MAX_TRACEES];
static unsigned char tracee_greeted[MAX_TRACEES];
static unsigned char tracee_cancelled[MAX_TRACEES];
static long tracee_value[MAX_TRACEES];
static int tracee_count;

/* how often there was no room to supervise a tracee. Not a statistic: every
 * one of those ran with its paths as the guest wrote them. */
static u64 unsupervised;

/* `146,40:-1` — a number on its own means the guest should be told the call
 * succeeded, and a value after the colon is what it is told instead. */
static void read_rules(const char *text)
{
	while (*text) {
		long number = 0;
		long value = 0;
		int digits = 0;
		int negative = 0;

		while (*text == ',' || *text == ' ')
			text++;
		while (*text >= '0' && *text <= '9') {
			number = number * 10 + (*text++ - '0');
			digits++;
		}
		if (!digits)
			break;
		if (*text == ':') {
			text++;
			if (*text == '-') {
				negative = 1;
				text++;
			}
			while (*text >= '0' && *text <= '9')
				value = value * 10 + (*text++ - '0');
			if (negative)
				value = -value;
		}
		if (number < 0 || number >= MAX_SYSCALL)
			continue;
		if (!rule_set[number])
			rule_count++;
		rule_set[number] = 1;
		rule_value[number] = value;
	}
}

static int rule_for(int number, long *value)
{
	if (number < 0 || number >= MAX_SYSCALL || !rule_set[number])
		return 0;
	*value = rule_value[number];
	return 1;
}

/* Everything a slot remembers, forgotten. Written further down, once the rest
 * of the per-tracee tables exist; a slot is reused, so nothing of whoever had
 * it before may be left in it. */
static void clear_slot(int slot);

static int slot_for(long pid)
{
	int free_slot = -1;

	for (int i = 0; i < tracee_count; i++) {
		if (tracee_pid[i] == pid)
			return i;
		if (!tracee_pid[i] && free_slot < 0)
			free_slot = i;
	}
	if (free_slot < 0) {
		if (tracee_count == MAX_TRACEES) {
			unsupervised++;
			return -1;
		}
		free_slot = tracee_count++;
	}
	clear_slot(free_slot);
	tracee_pid[free_slot] = pid;
	tracee_scratch[free_slot] = scratch;
	return free_slot;
}

/* A slot is emptied rather than filled in from the end.
 *
 * Moving the last tracee into the gap would move its scratch stretch out from
 * under it, and a tracee that is between the entry stop and the syscall itself
 * is still using the one it was given. An empty slot is picked up by the next
 * tracee to appear, by which time this one is gone. */
static void forget_tracee(long pid)
{
	for (int i = 0; i < tracee_count; i++) {
		if (tracee_pid[i] != pid)
			continue;
		tracee_pid[i] = 0;
		clear_slot(i);
		while (tracee_count > 0 && !tracee_pid[tracee_count - 1])
			tracee_count--;
		return;
	}
}

/* ------------------------------------------------ the guest's own /
 *
 * A guest started this way is handed real host paths and has no idea where its
 * rootfs begins, so `/` means the phone's root — which an app is not even
 * allowed to list. Under chroot the kernel would answer that; here nobody
 * does, so the supervisor does it: at each syscall that takes a path, the
 * guest's `/foo` is read out of its memory, turned into `<root>/foo`, written
 * into a scratch page it already owns, and the register pointed at that.
 *
 * The turning is not a prefix. A rootfs is full of absolute symlinks —
 * Alpine's /bin/sh points at /bin/busybox — and inside the rootfs that is
 * correct; resolved by the host the same link leads out of the rootfs to a
 * file that does not exist. So each component is followed here, from the
 * rootfs rather than from /, exactly as the guest's own kernel would.
 *
 * Three things are deliberately not translated:
 *
 *   /proc /sys /dev   the guest needs the real ones. A rootfs has empty
 *                     directories there and musl reads /proc/self/fd,
 *                     /dev/null and /dev/urandom before it does anything else
 *   relative paths    they resolve against a directory the guest is already
 *                     in, which is already inside the rootfs
 *   the last component of a call that means to act on a link itself —
 *                     readlink, unlink, rename — or one whose flags say
 *                     AT_SYMLINK_NOFOLLOW
 *
 * The registers are put back at the syscall's exit stop. On arm64 a syscall
 * clobbers x0 and nothing else, so a compiler is free to assume x1 still holds
 * the pointer it put there — and after this it would hold ours.
 */

#define SYS_readlinkat 78
#define SYS_getcwd 17
#define SYS_process_vm_readv 270
#define SYS_process_vm_writev 271

#define PTRACE_PEEKDATA 2
#define PTRACE_POKEDATA 5

#define AT_SYMLINK_NOFOLLOW 0x100

/* Long enough for a rootfs buried in /data/user/0/<package>/files/... plus
 * anything inside it. PATH_MAX above is the loader's own, and smaller. */
/* PAGE is defined further down, with the ELF loading */
#define MEMORY_PAGE 4096
#define GUEST_PATH_MAX 1024
#define MAX_LINKS 40
/* how many paths one syscall can take: renameat and linkat take two */
#define SCRATCH_SLOTS 2
/* and room after them for an exec: the words of a new command line, and the
 * array of pointers into them */
#define SCRATCH_EXTRA 8192
#define SCRATCH_BYTES (SCRATCH_SLOTS * GUEST_PATH_MAX + SCRATCH_EXTRA)
/* One of those per tracee, because threads share an address space and would
 * otherwise share the page.
 *
 * Two threads stopped at the same moment each wrote their translated path to
 * the same address, and whichever was let go second handed the kernel the
 * other one's path: a file created under another file's name. Rare, and only
 * under something that opens files from many threads at once — which is why
 * it took uv unpacking wheels to find it, and why it looked like a corrupt
 * download rather than a bug in here. */
#define SCRATCH_TOTAL ((u64)MAX_TRACEES * SCRATCH_BYTES)

/* At a fixed address, so it survives an exec.
 *
 * An exec replaces the address space and takes the page with it, and the
 * supervisor went on writing translated paths to where it used to be — so
 * every path after the first exec was handed over untranslated. The loader
 * that comes up maps it again in the same place, and the supervisor's idea of
 * where it is stays true across as many execs as the guest cares to make.
 *
 * Low enough for a 39-bit address space, which is what Android usually gives,
 * and far from where anything else is put. NOREPLACE rather than FIXED: if
 * something is there, the answer is a different address rather than somebody
 * else's memory quietly overwritten. */
#define SCRATCH_ADDRESS 0x600000000UL
#define MAP_FIXED_NOREPLACE 0x100000

/* The stretch of the scratch area this slot may write to.
 *
 * Every tracee gets its own, and a slot keeps its number for as long as the
 * tracee lives — see `forget_tracee`. Two threads of the same process share
 * the memory but not the stretch, so neither can overwrite the path the other
 * has just handed to the kernel. */
static u64 scratch_area(int slot)
{
	if (!tracee_scratch[slot])
		return 0;
	return tracee_scratch[slot] + (u64)slot * SCRATCH_BYTES;
}

static char map_root[GUEST_PATH_MAX];
static u64 map_root_length;
static char pass_through[GUEST_PATH_MAX];

/* Counted while a tracee has no page to be written into.
 *
 * An exec does not bring the page back with it. What comes up is
 * /system/bin/linker64, and it opens this loader and the libraries it needs
 * before a line of our code runs — only then is the page mapped again. The
 * supervisor used to believe the page was there from the moment the exec was
 * announced, and every path in that window was written to memory that did not
 * exist: 488 of them in one `uv tool install`, the first being the loader's own
 * file being opened by the linker.
 *
 * Nothing was lost — those are the linker's host paths and translating them
 * would have been the bug — but it was luck, not design. Now the window is
 * explicit, and what falls in it is counted rather than attempted.
 */
static u64 before_page;
static char before_page_first[GUEST_PATH_MAX];

/* what was in the registers before they were pointed at the scratch page */
static unsigned char tracee_changed[MAX_TRACEES];
static unsigned char tracee_changed_reg[MAX_TRACEES][SCRATCH_SLOTS];
static u64 tracee_changed_value[MAX_TRACEES][SCRATCH_SLOTS];
/* where getcwd was told to write, so the answer can be shortened */
static u64 tracee_cwd[MAX_TRACEES];
/* which call is in flight, so the exit stop can say what failed */
static int tracee_number[MAX_TRACEES];

static void clear_slot(int slot)
{
	tracee_scratch[slot] = 0;
	tracee_entering[slot] = 1;
	tracee_greeted[slot] = 0;
	tracee_cancelled[slot] = 0;
	tracee_value[slot] = 0;
	tracee_changed[slot] = 0;
	tracee_cwd[slot] = 0;
	tracee_number[slot] = 0;
	for (int i = 0; i < SCRATCH_SLOTS; i++) {
		tracee_changed_reg[slot][i] = 0;
		tracee_changed_value[slot][i] = 0;
	}
}

struct path_rule {
	short number;
	unsigned char args;    /* a bit per register, x0 first */
	signed char flags_reg; /* where AT_SYMLINK_NOFOLLOW would be, or -1 */
	unsigned char follow;  /* does it follow a link in the last component */
};

#define ARG0 1
#define ARG1 2
#define ARG2 4
#define ARG3 8

/* An array of numbers, not of pointers: this file has no dynamic linker of its
 * own, so anything needing a relocation never gets one. */
static const struct path_rule path_rules[] = {
	{ 5, ARG0, -1, 1 },    /* setxattr */
	{ 6, ARG0, -1, 0 },    /* lsetxattr */
	{ 8, ARG0, -1, 1 },    /* getxattr */
	{ 9, ARG0, -1, 0 },    /* lgetxattr */
	{ 11, ARG0, -1, 1 },   /* listxattr */
	{ 12, ARG0, -1, 0 },   /* llistxattr */
	{ 14, ARG0, -1, 1 },   /* removexattr */
	{ 15, ARG0, -1, 0 },   /* lremovexattr */
	{ 33, ARG1, -1, 0 },   /* mknodat */
	{ 34, ARG1, -1, 0 },   /* mkdirat */
	{ 35, ARG1, -1, 0 },   /* unlinkat */
	{ 36, ARG2, -1, 0 },   /* symlinkat: arg0 is the link's text, not a path */
	{ 37, ARG1 | ARG3, -1, 0 },  /* linkat */
	{ 38, ARG1 | ARG3, -1, 0 },  /* renameat */
	{ 43, ARG0, -1, 1 },   /* statfs */
	{ 45, ARG0, -1, 1 },   /* truncate */
	{ 48, ARG1, -1, 1 },   /* faccessat */
	{ 49, ARG0, -1, 1 },   /* chdir */
	{ 51, ARG0, -1, 1 },   /* chroot */
	{ 53, ARG1, -1, 1 },   /* fchmodat */
	{ 54, ARG1, 4, 1 },    /* fchownat */
	{ 56, ARG1, -1, 1 },   /* openat */
	{ 78, ARG1, -1, 0 },   /* readlinkat */
	{ 79, ARG1, 3, 1 },    /* newfstatat */
	{ 88, ARG1, 3, 1 },    /* utimensat */
	{ 221, ARG0, -1, 1 },  /* execve */
	{ 276, ARG1 | ARG3, -1, 0 }, /* renameat2 */
	{ 281, ARG1, 4, 1 },   /* execveat */
	{ 291, ARG1, 2, 1 },   /* statx */
	{ 437, ARG1, -1, 1 },  /* openat2 */
	{ 439, ARG1, 3, 1 },   /* faccessat2 */
	{ 452, ARG1, 3, 1 },   /* fchmodat2 */
};

#define PATH_RULE_COUNT (int)(sizeof(path_rules) / sizeof(path_rules[0]))

static const struct path_rule *path_rule_for(int number)
{
	for (int i = 0; i < PATH_RULE_COUNT; i++)
		if (path_rules[i].number == number)
			return &path_rules[i];
	return 0;
}

/* readlink for pathmap.c, which does the resolving and is shared with a test
 * harness on the desktop — the supervisor and the guest see the same
 * filesystem, so the supervisor can follow the links itself. */
static long platform_readlink(const char *path, char *out, u64 size)
{
	return sys4(SYS_readlinkat, AT_FDCWD, path, out, size);
}

#include "pathmap.c"

/* --------------------------------------------- the guest's memory */

static int read_string(long pid, u64 address, char *out, u64 size)
{
	u64 done = 0;

	while (done + 1 < size) {
		struct iovec here;
		struct iovec there;
		u64 chunk = MEMORY_PAGE -
			    ((address + done) & (u64)(MEMORY_PAGE - 1));
		long got;

		if (chunk > size - 1 - done)
			chunk = size - 1 - done;
		here.base = out + done;
		here.length = chunk;
		there.base = (void *)(address + done);
		there.length = chunk;
		got = sys6(SYS_process_vm_readv, pid, &here, 1, &there, 1, 0);
		if (got <= 0)
			break;
		for (long i = 0; i < got; i++)
			if (!out[done + i])
				return 1;
		done += (u64)got;
	}
	/* process_vm_readv can be unavailable where ptrace is not; a word at a
	 * time is slow and always works */
	done = 0;
	while (done + 8 <= size) {
		u64 word = 0;

		if (sys4(SYS_ptrace, PTRACE_PEEKDATA, pid, address + done,
			 &word) < 0)
			return 0;
		for (int i = 0; i < 8; i++) {
			out[done + i] = ((char *)&word)[i];
			if (!out[done + i])
				return 1;
		}
		done += 8;
	}
	return 0;
}

static int write_bytes(long pid, u64 address, const char *data, u64 size)
{
	struct iovec here;
	struct iovec there;

	here.base = (void *)data;
	here.length = size;
	there.base = (void *)address;
	there.length = size;
	if (sys6(SYS_process_vm_writev, pid, &here, 1, &there, 1, 0) ==
	    (long)size)
		return 1;
	for (u64 done = 0; done < size; done += 8) {
		u64 word = 0;
		u64 chunk = size - done < 8 ? size - done : 8;

		if (chunk < 8 &&
		    sys4(SYS_ptrace, PTRACE_PEEKDATA, pid, address + done,
			 &word) < 0)
			word = 0;
		for (u64 i = 0; i < chunk; i++)
			((char *)&word)[i] = data[done + i];
		if (syscall6(SYS_ptrace, PTRACE_POKEDATA, pid,
			     (long)(address + done), (long)word, 0, 0) < 0)
			return 0;
	}
	return 1;
}

/* ------------------------------------------ doing it, at the stop */

static u64 translated;
static u64 untranslated;
/* by reason, because "failed on 488" is a number and not evidence: a path that
 * did not fit wants a bigger buffer, one with no root wants a mount, and one
 * the supervisor could not write into the scratch page is the guest's memory
 * going away underneath it. The first path of each kind is kept whole — a
 * count says how often, a path says what. */
static u64 untranslated_by[4];
static char untranslated_first[4][GUEST_PATH_MAX];

static void note_untranslated(int reason, const char *path)
{
	if (reason < 0 || reason > 3)
		reason = 0;
	untranslated++;
	untranslated_by[reason]++;
	if (!untranslated_first[reason][0])
		copy_string(untranslated_first[reason], path, GUEST_PATH_MAX);
}

/* A path syscall made while there was nowhere to write a translation.
 *
 * Only the ones that would have been translated are counted. The window is the
 * linker starting the loader, and its paths are the host's own — /dev/urandom
 * was the first of 486 — so counting those would report a number that is large
 * and means nothing. What matters is whether a *guest* path ever falls in
 * here, because that one would have gone to the kernel as written. A count of
 * nought is the answer this should give.
 */
static void note_before_page(long pid, const struct path_rule *rule)
{
	char guest[GUEST_PATH_MAX];
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };

	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return;
	for (int i = 0; i < 6; i++) {
		if (!(rule->args & (1 << i)) || !registers[i])
			continue;
		if (!read_string(pid, registers[i], guest, sizeof(guest)) ||
		    guest[0] != '/' || passed_through(guest) ||
		    already_host(guest))
			continue;
		before_page++;
		if (!before_page_first[0])
			copy_string(before_page_first, guest, GUEST_PATH_MAX);
	}
}

static void translate_paths(long pid, int slot, int number)
{
	const struct path_rule *rule = path_rule_for(number);
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };
	int used = 0;
	int follow;

	if (number == SYS_getcwd)
		tracee_cwd[slot] = 0;
	if (!mount_count)
		return;
	if (!tracee_scratch[slot]) {
		/* between an exec and the new loader mapping its page. What
		 * asks for a path in here is the linker starting that loader,
		 * and its paths are the host's — but say what went past, in
		 * case one day it is something else */
		if (rule)
			note_before_page(pid, rule);
		return;
	}
	if (number == SYS_getcwd) {
		where.base = registers;
		where.length = sizeof(registers);
		if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS,
			 &where) == 0)
			tracee_cwd[slot] = registers[0];
		return;
	}
	if (!rule)
		return;
	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return;

	follow = rule->follow;
	if (rule->flags_reg >= 0 &&
	    (registers[(int)rule->flags_reg] & AT_SYMLINK_NOFOLLOW))
		follow = 0;

	for (int i = 0; i < 6 && used < SCRATCH_SLOTS; i++) {
		char guest[GUEST_PATH_MAX];
		char host[GUEST_PATH_MAX];
		u64 address;

		if (!(rule->args & (1 << i)) || !registers[i])
			continue;
		if (!read_string(pid, registers[i], guest, sizeof(guest)) ||
		    guest[0] != '/' || passed_through(guest) ||
		    already_host(guest))
			continue;
		if (!map_path(guest, follow, host)) {
			note_untranslated(map_failure, guest);
			continue;
		}
		address = scratch_area(slot) + (u64)used * GUEST_PATH_MAX;
		if (!write_bytes(pid, address, host,
				 string_length(host) + 1)) {
			note_untranslated(0, guest);
			continue;
		}
		tracee_changed_reg[slot][used] = (unsigned char)i;
		tracee_changed_value[slot][used] = registers[i];
		registers[i] = address;
		used++;
		translated++;
	}
	if (!used)
		return;
	where.base = registers;
	where.length = sizeof(registers);
	if (sys4(SYS_ptrace, PTRACE_SETREGSET, pid, NT_PRSTATUS, &where) == 0)
		tracee_changed[slot] = (unsigned char)used;
}

/* A refused hardlink, made good with a copy.
 *
 * Runs on the way out, once the kernel has said no. Doing it on the way in
 * would mean copying where a link would have worked, which is slower and — for
 * a program that later counts links — different for no reason.
 *
 * The registers still hold what the entry side put there: aarch64 hands back
 * the result in x0 and leaves x1..x5 alone, so the two paths are still the
 * translated ones. They are read back rather than remembered, and translated
 * here if the entry side did not, so this is right whether or not there were
 * mounts to rewrite through.
 */
static u64 mended_links;

/* Does this path mean the same thing to the supervisor as to the guest?
 *
 * /proc/self does not, and that is the whole reason to ask. apk links an
 * unnamed file into place through /proc/self/fd/N; read in this process that
 * names *our* descriptor N, and copying from it would put some file of the
 * supervisor's where the guest wanted its download. A refusal here leaves the
 * guest with the error it already had, which is the honest outcome — and one
 * it has a fallback for, because `rootfs writes` measures that link and the
 * loader tells the guest to stop using it when it fails.
 */
static int means_the_same_here(const char *path)
{
	static const char proc[] = "/proc/";

	for (int i = 0; i < 6; i++)
		if (path[i] != proc[i])
			return 1;
	return 0;
}

static int host_path_of(long pid, u64 address, char *out)
{
	char guest[GUEST_PATH_MAX];

	if (!address || !read_string(pid, address, guest, GUEST_PATH_MAX))
		return 0;
	if (guest[0] != '/')
		return 0;                    /* relative: the guest's own cwd */
	if (!means_the_same_here(guest))
		return 0;
	if (already_host(guest) || passed_through(guest)) {
		copy_string(out, guest, GUEST_PATH_MAX);
		return 1;
	}
	return map_path(guest, 0, out);
}

static void mend_link(long pid, int slot)
{
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };
	char from[GUEST_PATH_MAX];
	char to[GUEST_PATH_MAX];

	(void)slot;
	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return;
	if (!link_refused((long)registers[0]))
		return;
	/* linkat(olddirfd, oldpath, newdirfd, newpath, flags): x1 and x3 are
	 * the paths. A relative one is refused by host_path_of, which is the
	 * right answer — it would be relative to a directory fd this has no
	 * way to stand in for. An absolute path ignores the fd anyway. */
	if (!host_path_of(pid, registers[1], from) ||
	    !host_path_of(pid, registers[3], to))
		return;
	if (!copy_file(from, to))
		return;
	/* the registers are already here, so the answer goes back the same way
	 * rather than through another pair of ptrace calls */
	registers[0] = 0;
	where.base = registers;
	where.length = sizeof(registers);
	sys4(SYS_ptrace, PTRACE_SETREGSET, pid, NT_PRSTATUS, &where);
	mended_links++;
}

/* getcwd hands back where the guest is, in host terms. The guest has never
 * heard of that directory. */
static void shorten_cwd(long pid, int slot)
{
	char text[GUEST_PATH_MAX];
	char guest[GUEST_PATH_MAX];
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };
	u64 address = tracee_cwd[slot];
	const char *tail;
	u64 size;

	tracee_cwd[slot] = 0;
	if (!address || !mount_count)
		return;
	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return;
	if ((long)registers[0] <= 0)
		return;
	if (!read_string(pid, address, text, sizeof(text)))
		return;
	if (!unmap_path(text, guest))
		return;
	tail = guest;
	size = string_length(tail) + 1;
	if (!write_bytes(pid, address, tail, size))
		return;
	registers[0] = size;
	where.base = registers;
	where.length = sizeof(registers);
	sys4(SYS_ptrace, PTRACE_SETREGSET, pid, NT_PRSTATUS, &where);
}

/* ---------------------------------------------- the guest's own exec
 *
 * The device will not exec a file the app can write. That is why this loader
 * exists at all, and the guest runs into the same wall from the inside: apk
 * installs a package, runs its trigger, and gets "execve: Permission denied".
 *
 * So the guest's exec is turned into the one that is allowed. Where it asked
 * for a program, it now asks for /system/bin/linker64 — a system file, which
 * may be executed — with this loader and the program it wanted behind it. The
 * supervisor stays attached across an exec, so the new program is stepped and
 * its paths translated exactly as the old one's were.
 *
 * The new command line carries the child sentinel. Without it the loader that
 * comes up would see EXTCLI_MOUNTS in the inherited environment and fork a
 * supervisor of its own, and two of them would translate every path twice.
 */

#define SYS_EXECVE 221
#define SYS_EXECVEAT 281
#define MAX_EXEC_ARGS 128

static char exec_linker[GUEST_PATH_MAX];
static char exec_loader[GUEST_PATH_MAX];

/* "<linker>|<loader>" — both are host paths this process was started with, and
 * neither is anything the loader could work out for itself. */
static void read_exec(const char *text)
{
	u64 n = 0;

	if (!text)
		return;
	while (text[n] && text[n] != '|')
		n++;
	if (text[n] != '|' || n + 1 >= GUEST_PATH_MAX)
		return;
	for (u64 i = 0; i < n; i++)
		exec_linker[i] = text[i];
	exec_linker[n] = 0;
	copy_string(exec_loader, text + n + 1, GUEST_PATH_MAX);
}

/* A number into a buffer, for building a /proc path. */
static u64 write_number(char *out, u64 value, u64 size)
{
	char digits[24];
	u64 i = sizeof(digits);
	u64 at = 0;

	if (!value)
		digits[--i] = '0';
	while (value) {
		digits[--i] = (char)('0' + value % 10);
		value /= 10;
	}
	while (i < sizeof(digits) && at + 1 < size)
		out[at++] = digits[i++];
	out[at] = 0;
	return at;
}

/* Where a relative exec resolves from.
 *
 * apk runs a trigger by its place inside the rootfs — `lib/apk/exec/...`, with
 * no leading slash — so the path means nothing without the directory it is
 * relative to, and only the guest knows that. /proc says: its own cwd, or the
 * directory a dirfd names. The answer is a host path already, so nothing needs
 * translating afterwards.
 */
static int directory_of(long pid, long dirfd, char *out)
{
	char link[GUEST_PATH_MAX];
	u64 at = 6;

	copy_string(link, "/proc/", GUEST_PATH_MAX);
	at += write_number(link + at, (u64)pid, GUEST_PATH_MAX - at);
	if (dirfd == AT_FDCWD) {
		copy_string(link + at, "/cwd", GUEST_PATH_MAX - at);
	} else {
		copy_string(link + at, "/fd/", GUEST_PATH_MAX - at);
		at += 4;
		write_number(link + at, (u64)dirfd, GUEST_PATH_MAX - at);
	}
	return read_link(link, out, GUEST_PATH_MAX) > 0;
}

/* Writes one word into the guest's scratch page and says where it landed. */
static u64 put_word(long pid, u64 *at, u64 limit, const char *word)
{
	u64 where = *at;
	u64 length = string_length(word) + 1;

	if (where + length > limit)
		return 0;
	if (!write_bytes(pid, where, word, length))
		return 0;
	*at = where + length;
	return where;
}

/* The guest's own argv, so the program is called what it expects to be. */
static int read_pointers(long pid, u64 address, u64 *out, int max)
{
	int count = 0;

	if (!address)
		return 0;
	while (count < max) {
		u64 word = 0;

		if (sys4(SYS_ptrace, PTRACE_PEEKDATA, pid,
			 address + (u64)count * 8, &word) < 0)
			break;
		if (!word)
			break;
		out[count++] = word;
	}
	return count;
}

static int rewrite_exec(long pid, int slot, int number)
{
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };
	char guest[GUEST_PATH_MAX];
	char host[GUEST_PATH_MAX];
	u64 original[MAX_EXEC_ARGS];
	u64 pointers[MAX_EXEC_ARGS];
	int path_index = number == SYS_EXECVEAT ? 1 : 0;
	int argv_index = number == SYS_EXECVEAT ? 2 : 1;
	u64 at, array, limit;
	u64 p_linker, p_loader, p_sentinel, p_target;
	int originals, count = 0;

	if (!exec_loader[0] || !exec_linker[0] || !tracee_scratch[slot] ||
	    !mount_count)
		return 0;
	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return 0;
	if (!registers[path_index] ||
	    !read_string(pid, registers[path_index], guest, sizeof(guest)))
		return 0;
	/* The loader is handed the path in the guest's own terms, not the
	 * host's. It is about to be started under this same supervisor, so its
	 * opens are translated like anybody else's — and a host path handed to
	 * it would be translated a second time, into the rootfs, where nothing
	 * is. That is what "cannot open: /data/.../rootfs/lib/apk/exec/..."
	 * was. */
	if (guest[0] == '/') {
		copy_string(host, guest, GUEST_PATH_MAX);
	} else {
		char directory[GUEST_PATH_MAX];
		char real[GUEST_PATH_MAX];
		long dirfd = number == SYS_EXECVEAT
			? (long)(int)registers[0] : AT_FDCWD;
		u64 end;

		/* a relative path means nothing without the directory it is
		 * relative to, and only the guest knows that one; /proc says
		 * what it is, in the host's terms, so the answer is turned
		 * back into the guest's */
		if (!directory_of(pid, dirfd, directory))
			return 0;
		if (string_length(directory) + string_length(guest) + 2 >=
		    GUEST_PATH_MAX)
			return 0;
		copy_string(real, directory, GUEST_PATH_MAX);
		end = string_length(real);
		real[end++] = '/';
		copy_string(real + end, guest, GUEST_PATH_MAX - end);
		if (!unmap_path(real, host))
			copy_string(host, real, GUEST_PATH_MAX);
	}

	originals = read_pointers(pid, registers[argv_index], original,
				  MAX_EXEC_ARGS - 8);
	at = scratch_area(slot) + (u64)SCRATCH_SLOTS * GUEST_PATH_MAX;
	limit = scratch_area(slot) + SCRATCH_BYTES;
	p_linker = put_word(pid, &at, limit, exec_linker);
	p_loader = put_word(pid, &at, limit, exec_loader);
	p_sentinel = put_word(pid, &at, limit, sentinel_child);
	p_target = put_word(pid, &at, limit, host);
	if (!p_linker || !p_loader || !p_sentinel || !p_target)
		return 0;

	pointers[count++] = p_linker;
	pointers[count++] = p_loader;
	pointers[count++] = p_sentinel;
	pointers[count++] = p_target;
	if (!originals)
		/* a program with no argv still has to answer to something */
		pointers[count++] = p_target;
	for (int i = 0; i < originals && count < MAX_EXEC_ARGS - 1; i++)
		pointers[count++] = original[i];
	pointers[count] = 0;

	array = (at + 7) & ~(u64)7;
	if (array + (u64)(count + 1) * 8 >
	    scratch_area(slot) + SCRATCH_BYTES)
		return 0;
	if (!write_bytes(pid, array, (const char *)pointers,
			 (u64)(count + 1) * 8))
		return 0;

	tracee_changed_reg[slot][0] = (unsigned char)path_index;
	tracee_changed_value[slot][0] = registers[path_index];
	tracee_changed_reg[slot][1] = (unsigned char)argv_index;
	tracee_changed_value[slot][1] = registers[argv_index];
	registers[path_index] = p_linker;
	registers[argv_index] = array;
	if (number == SYS_EXECVEAT) {
		/* the path is absolute now, so the directory it was relative to
		 * is not wanted and neither are the flags that chose it */
		registers[0] = (u64)(long)AT_FDCWD;
		registers[4] = 0;
	}
	where.base = registers;
	where.length = sizeof(registers);
	if (sys4(SYS_ptrace, PTRACE_SETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return 0;
	tracee_changed[slot] = 2;
	return 1;
}

/* Errors, as they happen.
 *
 * The last-syscalls report answers "what killed it"; this answers "what went
 * wrong", which is a different question and the one a program that keeps
 * running leaves behind. `apk update` said "Permission denied" about a URL —
 * a message that names neither the call that was refused nor the thing it was
 * refused for, and no amount of reading it will say more.
 *
 * Only failures, and only so many: a shell start makes hundreds of calls that
 * fail on purpose, and a wall of them is not a report.
 */

/* Enough that the one that matters is in there. A short command makes a few;
 * a package manager unpacking a wheel makes thousands of misses on purpose,
 * every one of them a path it was checking for and did not expect to find. The
 * report is read from the other end anyway — this number is only here so a
 * runaway cannot fill the screen forever. */
#define MAX_FAILURES 4000

static int failures_shown;

static void report_failure(long pid, int slot)
{
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };
	long value;

	if (failures_shown >= MAX_FAILURES)
		return;
	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return;
	value = (long)registers[0];
	/* the kernel returns a small negative number for an error and anything
	 * else for an answer */
	if (value >= 0 || value < -4095)
		return;
	failures_shown++;
	put("extcli-loader: failed ", 22);
	put_number((u64)tracee_number[slot]);
	put(" errno ", 7);
	put_number((u64)(-value));
	if (tracee_changed[slot]) {
		char text[GUEST_PATH_MAX];

		if (read_string(pid, tracee_changed_value[slot][0], text,
				sizeof(text))) {
			put(" ", 1);
			put(text, string_length(text));
		}
	}
	put("\n", 1);
}

/* x1 and up survive a syscall on arm64, so a compiler may still believe one of
 * them holds the guest's own pointer. x0 is the return value and is not put
 * back. */
static void restore_registers(long pid, int slot)
{
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };
	int count = tracee_changed[slot];

	tracee_changed[slot] = 0;
	if (!count)
		return;
	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return;
	for (int i = 0; i < count; i++) {
		int index = tracee_changed_reg[slot][i];

		if (index == 0)
			continue;
		registers[index] = tracee_changed_value[slot][i];
	}
	where.base = registers;
	where.length = sizeof(registers);
	sys4(SYS_ptrace, PTRACE_SETREGSET, pid, NT_PRSTATUS, &where);
}

static int syscall_of(long pid)
{
	int number = -1;
	struct iovec where = { &number, sizeof(number) };

	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_ARM_SYSTEM_CALL,
		 &where) < 0)
		return -1;
	return number;
}

/* Puts a different syscall in the guest's place. Returns whether the kernel
 * agreed: a diversion that did not take is the difference between a guest that
 * runs and a guest that dies, and it is not something to assume. */
static int divert_syscall(long pid, int to)
{
	int number = to;
	struct iovec where = { &number, sizeof(number) };

	if (sys4(SYS_ptrace, PTRACE_SETREGSET, pid, NT_ARM_SYSTEM_CALL,
		 &where) < 0)
		return 0;
	return syscall_of(pid) == to;
}

/* Is this the open of an unnamed file that could never be linked anywhere? */
static int unnameable_open(long pid, int number)
{
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };

	if (!no_tmpfile || number != SYS_OPENAT)
		return 0;
	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return 0;
	return (registers[2] & O_TMPFILE_BIT) != 0;
}

/* 1 entering, 2 leaving, 0 if the kernel is too old to say. */
static int stop_kind(long pid)
{
	/* the header is fixed; the union after it is longer than anything read
	 * here, and the kernel writes only as much as it is given room for */
	unsigned char info[88];

	for (u64 i = 0; i < sizeof(info); i++)
		info[i] = 0;
	if (sys4(SYS_ptrace, PTRACE_GET_SYSCALL_INFO, pid, sizeof(info),
		 info) < 0)
		return 0;
	return info[0];
}

/* What a syscall answered, read at its exit stop. */
static long result_of(long pid)
{
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };

	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return -1;
	return (long)registers[0];
}

static void set_result(long pid, long value)
{
	u64 registers[34];
	struct iovec where = { registers, sizeof(registers) };

	if (sys4(SYS_ptrace, PTRACE_GETREGSET, pid, NT_PRSTATUS, &where) < 0)
		return;
	registers[0] = (u64)value;
	where.base = registers;
	where.length = sizeof(registers);
	sys4(SYS_ptrace, PTRACE_SETREGSET, pid, NT_PRSTATUS, &where);
}

static u64 diverted;
static u64 undiverted;
static int kinds_known;

static void report_trace(long *recent, int count, u64 total, int status)
{
	put("extcli-loader: ", 15);
	put_number(total);
	put(" syscalls, last:", 16);
	for (int i = 0; i < count; i++) {
		put(" ", 1);
		put_number((u64)recent[i]);
	}
	put("\n", 1);
	if (rule_count) {
		put("extcli-loader: answered ", 24);
		put_number(diverted);
		put(" of them, failed to answer ", 27);
		put_number(undiverted);
		if (!kinds_known)
			put(", stop kinds guessed", 20);
		put("\n", 1);
	}
	if (mount_count) {
		/* laid out, not pointed at: an array of pointers into .rodata
		 * is four relocations, and nothing here applies those */
		static const char why[4][20] = {
			"could not write", "too long", "link loop", "no mount",
		};

		put("extcli-loader: translated ", 26);
		put_number(translated);
		put(" paths, failed on ", 18);
		put_number(untranslated);
		put("\n", 1);
		for (int i = 0; i < 4; i++) {
			if (!untranslated_by[i])
				continue;
			put("extcli-loader:   ", 17);
			put_number(untranslated_by[i]);
			put(" ", 1);
			put(why[i], string_length(why[i]));
			put(", first ", 8);
			put(untranslated_first[i],
			    string_length(untranslated_first[i]));
			put("\n", 1);
		}
		if (unsupervised) {
			put("extcli-loader:   ", 17);
			put_number(unsupervised);
			put(" stops went unsupervised — no room for the "
			    "tracee", 51);
			put("\n", 1);
		}
		if (before_page) {
			put("extcli-loader:   ", 17);
			put_number(before_page);
			put(" guest paths went past while an exec'd "
			    "loader had no page", 57);
			if (before_page_first[0]) {
				put(", first ", 8);
				put(before_page_first,
				    string_length(before_page_first));
			}
			put("\n", 1);
		}
	}
	if (mended_links) {
		put("extcli-loader: copied ", 22);
		put_number(mended_links);
		put(" hardlink(s) the kernel refused\n", 32);
	}
	if (WIFSIGNALED(status)) {
		put("extcli-loader: the guest was killed by signal ", 46);
		put_number((u64)WTERMSIG(status));
		put("\n", 1);
	} else if (WIFEXITED(status)) {
		put("extcli-loader: the guest exited with ", 37);
		put_number((u64)WEXITSTATUS(status));
		put("\n", 1);
	} else {
		put("extcli-loader: the guest vanished between stops\n", 48);
	}
}

static void remember(long *recent, int *count, long number)
{
	if (*count == REMEMBERED) {
		for (int i = 1; i < REMEMBERED; i++)
			recent[i - 1] = recent[i];
		(*count)--;
	}
	recent[(*count)++] = number;
}

/* Ends the loader the way the guest ended, so a caller reading an exit status
 * learns about the guest and not about the supervisor. */
static void end_like(int status)
{
	if (WIFSIGNALED(status)) {
		struct kernel_sigaction action;
		int signal = WTERMSIG(status);

		/* our own SIGSYS handler would otherwise print over this */
		action.handler = NULL; /* SIG_DFL */
		action.flags = 0;
		action.restorer = NULL;
		action.mask = 0;
		sys4(SYS_rt_sigaction, signal, &action, 0,
		     sizeof(action.mask));
		sys3(SYS_kill, syscall6(SYS_getpid, 0, 0, 0, 0, 0, 0), signal,
		     0);
	}
	sys1(SYS_exit_group, WIFEXITED(status) ? WEXITSTATUS(status) : 1);
	__builtin_unreachable();
}

#define TRACE_OPTIONS                                                    \
	(PTRACE_O_TRACESYSGOOD | PTRACE_O_TRACEFORK | PTRACE_O_TRACEVFORK | \
	 PTRACE_O_TRACECLONE | PTRACE_O_TRACEEXEC | PTRACE_O_EXITKILL)

/* Steps the guest and everything it starts. Never returns. */
static void supervise(long first, int tracing)
{
	long recent[REMEMBERED];
	int count = 0;
	u64 total = 0;
	int status = 0;
	int last = 0;
	int slot;

	/* The child stopped itself on purpose; without that the first
	 * PTRACE_SYSCALL would race the guest's first instructions and the
	 * trace would begin somewhere in the middle of a startup it was
	 * written to watch from the beginning. */
	if (syscall6(SYS_wait4, first, (long)&status, WALL, 0, 0, 0) < 0)
		sys1(SYS_exit_group, 1);
	if (!WIFSTOPPED(status)) {
		/* It never stopped, so ptrace did not take — under an emulator,
		 * say. Then the guest has already run unsupervised and this is
		 * its ending; nothing was neutralised and nothing was seen. */
		if (tracing)
			report_trace(recent, 0, 0, status);
		end_like(status);
	}
	sys4(SYS_ptrace, PTRACE_SETOPTIONS, first, 0, TRACE_OPTIONS);
	slot = slot_for(first);
	if (slot >= 0)
		tracee_greeted[slot] = 1;
	sys4(SYS_ptrace, PTRACE_SYSCALL, first, 0, 0);

	for (;;) {
		long pid = syscall6(SYS_wait4, -1, (long)&status, WALL, 0, 0, 0);
		int signal;
		long deliver = 0;

		if (pid < 0)
			break;
		if (!WIFSTOPPED(status)) {
			forget_tracee(pid);
			if (pid == first) {
				last = status;
				break;
			}
			continue;
		}
		signal = WSTOPSIG(status);
		slot = slot_for(pid);
		if (slot < 0) {
			sys4(SYS_ptrace, PTRACE_SYSCALL, pid, 0, 0);
			continue;
		}
		if (signal == SYSCALL_STOP) {
			int kind = stop_kind(pid);
			int entering = kind ? kind == SYSCALL_INFO_ENTRY
					    : tracee_entering[slot];

			if (kind)
				kinds_known = 1;
			if (entering) {
				int number = syscall_of(pid);
				long value = 0;
				int answer = rule_for(number, &value);

				if (!answer && unnameable_open(pid, number)) {
					value = -EOPNOTSUPP;
					answer = 1;
				}
				total++;
				tracee_number[slot] = number;
				if (tracing)
					remember(recent, &count, number);
				if (answer) {
					if (divert_syscall(pid,
							   SYSCALL_HARMLESS)) {
						tracee_cancelled[slot] = 1;
						tracee_value[slot] = value;
						diverted++;
					} else {
						undiverted++;
					}
				} else if (number == SYS_EXECVE ||
					   number == SYS_EXECVEAT) {
					/* both would translate the same path,
					 * and the rewrite has already done it */
					if (!rewrite_exec(pid, slot, number))
						translate_paths(pid, slot,
								number);
				} else {
					translate_paths(pid, slot, number);
				}
			} else {
				/* the answer first, then the registers back:
				 * putting them back reads x0, and the answer is
				 * what x0 is for */
				if (tracee_cancelled[slot]) {
					set_result(pid, tracee_value[slot]);
					tracee_cancelled[slot] = 0;
				} else if (tracee_number[slot] == SYS_linkat) {
					mend_link(pid, slot);
				}
				if (tracee_number[slot] == SYS_EXECVE ||
				    tracee_number[slot] == SYS_EXECVEAT) {
					/* whether it worked or not, what comes
					 * back is a fresh image with no page in
					 * it yet */
					tracee_scratch[slot] = 0;
				} else if (!tracee_scratch[slot] &&
					   tracee_number[slot] == SYS_mmap &&
					   result_of(pid) ==
						(long)SCRATCH_ADDRESS) {
					/* and there it is: the loader that came
					 * up has asked for the address both of
					 * us agreed on. Before this call there
					 * was nowhere to write a path, and the
					 * linker's own opens went past us —
					 * which is right, they are the host's */
					tracee_scratch[slot] = SCRATCH_ADDRESS;
				}
				if (tracee_cwd[slot])
					shorten_cwd(pid, slot);
				if (tracing)
					report_failure(pid, slot);
				restore_registers(pid, slot);
			}
			tracee_entering[slot] = !entering;
		} else if (signal == SIGTRAP) {
			/* a fork, a clone or an exec being announced, not a
			 * signal for the guest */
			if ((status >> 16) == PTRACE_EVENT_EXEC) {
				/* a fresh image: no page in it until the loader
				 * that came up maps one, and its next syscall
				 * stop is an entry whatever came before */
				tracee_scratch[slot] = 0;
				tracee_entering[slot] = 1;
			}
		} else if (signal == SIGSTOP && !tracee_greeted[slot]) {
			/* a new child stopping so it can be noticed */
		} else {
			deliver = signal;
		}
		tracee_greeted[slot] = 1;
		sys4(SYS_ptrace, PTRACE_SYSCALL, pid, 0, deliver);
	}
	if (tracing)
		report_trace(recent, count, total, last);
	end_like(last);
}

static void copy(char *to, const char *from, u64 length)
{
	for (u64 i = 0; i < length; i++)
		to[i] = from[i];
}

static void zero(char *at, u64 length)
{
	for (u64 i = 0; i < length; i++)
		at[i] = 0;
}

/* --------------------------------------------------------------- the ELF */

#define ET_EXEC 2
#define ET_DYN 3
#define EM_AARCH64 183
#define PT_LOAD 1
#define PT_DYNAMIC 2
#define PT_INTERP 3
#define PT_PHDR 6
#define PF_X 1
#define PF_W 2
#define PF_R 4

struct elf_header {
	unsigned char ident[16];
	u16 type;
	u16 machine;
	u32 version;
	u64 entry;
	u64 phoff;
	u64 shoff;
	u32 flags;
	u16 ehsize;
	u16 phentsize;
	u16 phnum;
	u16 shentsize;
	u16 shnum;
	u16 shstrndx;
};

struct program_header {
	u32 type;
	u32 flags;
	u64 offset;
	u64 vaddr;
	u64 paddr;
	u64 filesz;
	u64 memsz;
	u64 align;
};

#define MAX_PHDRS 48
#define PAGE 4096
#define PATH_MAX 512

#define page_down(x) ((x) & ~(u64)(PAGE - 1))
#define page_up(x) page_down((x) + PAGE - 1)

struct image {
	u64 base;      /* where it was put; 0 for a fixed-address program */
	u64 entry;     /* its entry point, already biased */
	u64 phdr;      /* the program headers, as the guest must see them */
	u64 phnum;
	u64 phent;
	char interp[PATH_MAX];
	int has_interp;
};

static int protection_of(u32 flags)
{
	int prot = 0;
	if (flags & PF_R)
		prot |= PROT_READ;
	if (flags & PF_W)
		prot |= PROT_WRITE;
	if (flags & PF_X)
		prot |= PROT_EXEC;
	/* A segment with no permissions at all cannot be mapped usefully, and
	 * some linkers emit one; read is the harmless default. */
	return prot ? prot : PROT_READ;
}

static void map_elf(const char *path, struct image *out)
{
	struct elf_header header;
	struct program_header phdrs[MAX_PHDRS];

	int fd = (int)sys4(SYS_openat, AT_FDCWD, path, O_RDONLY, 0);
	if (fd < 0)
		fail2("cannot open", path);

	if (sys4(SYS_pread64, fd, &header, sizeof(header), 0) !=
	    (s64)sizeof(header))
		fail("cannot read the ELF header");
	if (header.ident[0] != 0x7f || header.ident[1] != 'E' ||
	    header.ident[2] != 'L' || header.ident[3] != 'F')
		fail("not an ELF file");
	if (header.ident[4] != 2)
		fail("not a 64-bit ELF");
	if (header.machine != EM_AARCH64)
		fail("not an aarch64 ELF");
	if (header.type != ET_EXEC && header.type != ET_DYN)
		fail("not an executable ELF");
	if (header.phnum == 0 || header.phnum > MAX_PHDRS)
		fail("implausible program header count");

	u64 table = (u64)header.phnum * header.phentsize;
	if (sys4(SYS_pread64, fd, phdrs, table, header.phoff) != (s64)table)
		fail("cannot read the program headers");

	/* The whole span first, as one reservation, so the segments land at the
	 * offsets from each other that they were linked for. Mapping them one
	 * by one and hoping the kernel keeps them together is the classic way
	 * to get a program that works until it does not. */
	u64 low = ~(u64)0, high = 0;
	for (int i = 0; i < header.phnum; i++) {
		if (phdrs[i].type != PT_LOAD)
			continue;
		if (phdrs[i].vaddr < low)
			low = phdrs[i].vaddr;
		if (phdrs[i].vaddr + phdrs[i].memsz > high)
			high = phdrs[i].vaddr + phdrs[i].memsz;
	}
	if (low == ~(u64)0)
		fail("nothing to load");
	low = page_down(low);
	high = page_up(high);

	u64 bias = 0;
	if (header.type == ET_DYN) {
		s64 reserved = sys6(SYS_mmap, 0, high - low, 0 /* PROT_NONE */,
				    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (reserved < 0 && reserved > -4096)
			fail("cannot reserve address space");
		bias = (u64)reserved - low;
	}

	for (int i = 0; i < header.phnum; i++) {
		struct program_header *p = &phdrs[i];
		if (p->type != PT_LOAD || p->memsz == 0)
			continue;

		u64 start = page_down(p->vaddr + bias);
		u64 file_end = p->vaddr + bias + p->filesz;
		u64 mem_end = p->vaddr + bias + p->memsz;
		int prot = protection_of(p->flags);

		if (p->filesz) {
			s64 got = sys6(SYS_mmap, start, page_up(file_end) - start,
				       prot | PROT_WRITE,
				       MAP_PRIVATE | MAP_FIXED, fd,
				       page_down(p->offset));
			if (got < 0 && got > -4096)
				fail("cannot map a segment");
		}

		/* .bss: the tail of the last file page has to be cleared, and
		 * whole pages past it mapped anonymously. */
		if (mem_end > file_end) {
			u64 tail = page_up(file_end);
			if (tail > file_end)
				zero((char *)file_end, tail - file_end);
			if (page_up(mem_end) > tail) {
				s64 got = sys6(SYS_mmap, tail,
					       page_up(mem_end) - tail,
					       prot | PROT_WRITE,
					       MAP_PRIVATE | MAP_FIXED |
						       MAP_ANONYMOUS,
					       -1, 0);
				if (got < 0 && got > -4096)
					fail("cannot map a bss segment");
			}
		}

		if (!(prot & PROT_WRITE))
			sys3(SYS_mprotect, start, page_up(mem_end) - start,
			     prot);
	}

	out->base = bias;
	out->entry = header.entry + bias;
	out->phnum = header.phnum;
	out->phent = header.phentsize;
	out->phdr = 0;
	out->has_interp = 0;

	for (int i = 0; i < header.phnum; i++) {
		struct program_header *p = &phdrs[i];
		if (p->type == PT_PHDR)
			out->phdr = p->vaddr + bias;
		if (p->type == PT_INTERP && p->filesz &&
		    p->filesz < PATH_MAX) {
			if (sys4(SYS_pread64, fd, out->interp, p->filesz,
				 p->offset) != (s64)p->filesz)
				fail("cannot read the interpreter name");
			out->interp[p->filesz] = 0;
			out->has_interp = 1;
		}
	}
	/* No PT_PHDR: find the segment the headers fell inside. A guest's libc
	 * walks these to find its own dynamic section, so getting this wrong
	 * produces a crash a long way from here. */
	if (!out->phdr) {
		for (int i = 0; i < header.phnum; i++) {
			struct program_header *p = &phdrs[i];
			if (p->type != PT_LOAD)
				continue;
			if (header.phoff >= p->offset &&
			    header.phoff + table <= p->offset + p->filesz) {
				out->phdr = p->vaddr + bias +
					    (header.phoff - p->offset);
				break;
			}
		}
	}
	if (!out->phdr)
		fail("cannot place the program headers");

	sys1(SYS_close, fd);
}

/* ---------------------------------------------------------- the new stack */

#define AT_NULL 0
#define AT_PHDR 3
#define AT_PHENT 4
#define AT_PHNUM 5
#define AT_PAGESZ 6
#define AT_BASE 7
#define AT_FLAGS 8
#define AT_ENTRY 9
#define AT_SECURE 23

/* Values we do not invent: they describe the process, which has not changed,
 * so they are copied from our own auxiliary vector. AT_RANDOM matters most —
 * a libc reads sixteen bytes through it to seed its stack guard, and a null
 * one is a crash before main. */
static const u64 inherited[] = { 11 /* AT_UID */,   12 /* AT_EUID */,
				 13 /* AT_GID */,   14 /* AT_EGID */,
				 16 /* AT_HWCAP */, 17 /* AT_CLKTCK */,
				 25 /* AT_RANDOM */, 26 /* AT_HWCAP2 */,
				 15 /* AT_PLATFORM */,
				 33 /* AT_SYSINFO_EHDR, the vdso */ };

#define INHERITED_COUNT (sizeof(inherited) / sizeof(inherited[0]))
/* AT_PHDR, AT_PHENT, AT_PHNUM, AT_PAGESZ, AT_BASE, AT_ENTRY, AT_FLAGS,
 * AT_SECURE, AT_NULL. Counted rather than eyeballed: it was eyeballed as
 * seven, the reservation came out four words short, and the last pushes ran
 * off the end of the stack region into unmapped memory. */
#define FIXED_AUXV_PAIRS 9
#define STACK_BYTES (256 * 1024)

static u64 auxv_value(u64 *auxv, u64 wanted, int *found)
{
	for (u64 *at = auxv; at[0] != AT_NULL; at += 2) {
		if (at[0] == wanted) {
			*found = 1;
			return at[1];
		}
	}
	*found = 0;
	return 0;
}

static u64 *build_stack(int argc, char **argv, char **envp, u64 *auxv,
			struct image *program, struct image *interp)
{
	s64 region = sys6(SYS_mmap, 0, STACK_BYTES, PROT_READ | PROT_WRITE,
			  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (region < 0 && region > -4096)
		fail("cannot allocate a stack");

	int envc = 0;
	while (envp[envc])
		envc++;

	/* argc, argv + NULL, envp + NULL, then the auxiliary vector */
	u64 words = 1 + (u64)argc + 1 + (u64)envc + 1 +
		    2 * (INHERITED_COUNT + FIXED_AUXV_PAIRS);
	u64 *top = (u64 *)((u64)region + STACK_BYTES);
	if (words * 8 + 64 > STACK_BYTES)
		fail("the argument block does not fit in a stack");
	u64 *stack = top - words;
	stack = (u64 *)((u64)stack & ~(u64)15); /* the ABI wants 16 bytes */

	u64 *at = stack;
	*at++ = (u64)argc;
	for (int i = 0; i < argc; i++)
		*at++ = (u64)argv[i];
	*at++ = 0;
	for (int i = 0; i < envc; i++)
		*at++ = (u64)envp[i];
	*at++ = 0;

	for (u64 i = 0; i < INHERITED_COUNT; i++) {
		int found = 0;
		u64 value = auxv_value(auxv, inherited[i], &found);
		if (!found)
			continue;
		*at++ = inherited[i];
		*at++ = value;
	}
	*at++ = AT_PHDR;
	*at++ = program->phdr;
	*at++ = AT_PHENT;
	*at++ = program->phent;
	*at++ = AT_PHNUM;
	*at++ = program->phnum;
	*at++ = AT_PAGESZ;
	*at++ = PAGE;
	*at++ = AT_BASE;
	*at++ = interp ? interp->base : 0;
	*at++ = AT_ENTRY;
	*at++ = program->entry;
	*at++ = AT_FLAGS;
	*at++ = 0;
	*at++ = AT_SECURE;
	*at++ = 0;
	*at++ = AT_NULL;
	*at++ = 0;

	/* Cheap, and it turns a miscounted reservation into a message instead
	 * of a write into whatever happens to be mapped next. */
	if (at > top)
		fail("the argument block overran the stack");
	return stack;
}

/* --------------------------------------------------------------- starting */

static void resolve_interpreter(const char *root, const char *guest,
				char *out)
{
	u64 root_length = root ? string_length(root) : 0;
	u64 guest_length = string_length(guest);
	if (root_length + guest_length + 1 >= PATH_MAX)
		fail("interpreter path is too long");
	copy(out, root ? root : "", root_length);
	copy(out + root_length, guest, guest_length);
	out[root_length + guest_length] = 0;
}

/* A script is not an ELF, and the kernel is what usually notices.
 *
 * Nothing does here. An exec that reaches this loader has already been turned
 * into "start the loader on this file", so the `#!` line is the loader's to
 * read — and apk's triggers are shell scripts, which is where it first
 * mattered. Returns whether it was a script; the interpreter and its one
 * optional argument come back in the buffers.
 */
static int read_shebang(const char *path, char *interp, char *argument)
{
	char head[256];
	long n;
	u64 i, at;
	int fd = (int)sys4(SYS_openat, AT_FDCWD, path, O_RDONLY, 0);

	interp[0] = 0;
	argument[0] = 0;
	if (fd < 0)
		return 0;
	n = sys3(SYS_read, fd, head, sizeof(head) - 1);
	sys1(SYS_close, fd);
	if (n < 3 || head[0] != '#' || head[1] != '!')
		return 0;
	head[n] = 0;
	i = 2;
	while (head[i] == ' ' || head[i] == '\t')
		i++;
	at = 0;
	while (head[i] && head[i] != '\n' && head[i] != ' ' && head[i] != '\t')
		interp[at++] = head[i++];
	interp[at] = 0;
	if (!at)
		return 0;
	while (head[i] == ' ' || head[i] == '\t')
		i++;
	at = 0;
	/* one argument, whole, spaces and all — which is what Linux does */
	while (head[i] && head[i] != '\n')
		argument[at++] = head[i++];
	while (at && (argument[at - 1] == ' ' || argument[at - 1] == '\t'))
		at--;
	argument[at] = 0;
	return 1;
}

static char *environment_value(char **envp, const char *name)
{
	u64 length = string_length(name);
	for (char **at = envp; *at; at++) {
		char *entry = *at;
		u64 i = 0;
		while (i < length && entry[i] == name[i])
			i++;
		if (i == length && entry[i] == '=')
			return entry + length + 1;
	}
	return NULL;
}

/* Hidden on purpose. A global symbol makes the linker route _start's branch
 * through the PLT, and a PLT entry is a relocation somebody has to apply — the
 * loader has no dynamic linker of its own to do it, so the branch lands in an
 * unfilled stub and the process dies before its first useful instruction. */
__attribute__((visibility("hidden"))) void loader_main(u64 *sp);

void loader_main(u64 *sp)
{
	int argc = (int)sp[0];
	char **argv = (char **)&sp[1];
	char **envp = argv + argc + 1;
	u64 *auxv = (u64 *)envp;
	while (*auxv)
		auxv++;
	auxv++;

	watch_for_seccomp();

	/* Read before anything is loaded: a script's `#!` line names its
	 * interpreter in the guest's terms, and turning that into a real path
	 * needs the mounts. */
	const char *blocked = environment_value(envp, "EXTCLI_BLOCKED");
	const char *mounts = environment_value(envp, "EXTCLI_MOUNTS");
	const char *pass = environment_value(envp, "EXTCLI_PASS");
	int tracing = environment_value(envp, "EXTCLI_TRACE") != NULL;

	no_tmpfile = environment_value(envp, "EXTCLI_NO_TMPFILE") != NULL;
	read_exec(environment_value(envp, "EXTCLI_EXEC"));
	if (blocked)
		read_rules(blocked);
	if (mounts && *mounts) {
		read_mounts(mounts);
		copy_string(pass_through, pass ? pass : "",
			    sizeof(pass_through));
	}

	int first = -1;
	int supervised = 0;
	for (int i = 0; i < argc; i++) {
		if (same_string(argv[i], sentinel)) {
			first = i + 1;
			break;
		}
		if (same_string(argv[i], sentinel_child)) {
			first = i + 1;
			supervised = 1;
			break;
		}
	}
	if (first < 0 || argc - first < 2) {
		show_arguments(argc, argv);
		fail("no arguments after the sentinel");
	}

	/* Before anything is opened, and that is the whole point.
	 *
	 * A loader started by an exec is being traced from the moment it
	 * begins, and the supervisor writes every path it translates into this
	 * page. Mapping it after the program had been opened meant the first
	 * opens — the program itself, and the interpreter a `#!` line names —
	 * had nowhere to be translated into, so they went through as written
	 * and the loader answered "cannot open: /bin/busybox". Twice fixed
	 * elsewhere and twice unchanged, because the order was the fault. */
	if (mount_count || supervised) {
		/* Mapped here, before the fork, so the guest owns the same page
		 * at the same address without ever having asked for it — and
		 * the supervisor knows where to write a translated path.
		 *
		 * `supervised` on its own is enough. A loader started by an
		 * exec has whatever environment the program doing the exec
		 * chose to pass, and apk passes triggers a short one with no
		 * EXTCLI_MOUNTS in it — so this loader has no mounts of its
		 * own and needs none. What it does need is the page, because
		 * the supervisor above it is still translating its paths and
		 * has nowhere to write them otherwise. Without this the guest
		 * path from a `#!` line reached openat untranslated, and the
		 * loader answered "cannot open: /bin/busybox". */
		scratch = (u64)sys6(SYS_mmap, SCRATCH_ADDRESS, SCRATCH_TOTAL,
				    PROT_READ | PROT_WRITE,
				    MAP_PRIVATE | MAP_ANONYMOUS |
				    MAP_FIXED_NOREPLACE, -1, 0);
		if (scratch != SCRATCH_ADDRESS)
			/* somewhere else will do until the first exec, after
			 * which the supervisor looks at the fixed address */
			scratch = (u64)sys6(SYS_mmap, 0, SCRATCH_TOTAL,
					    PROT_READ | PROT_WRITE,
					    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if ((s64)scratch < 0 && (s64)scratch > -4096) {
			scratch = 0;
			mount_count = 0;
		}
	}

	const char *elf = argv[first];
	char **guest_argv = argv + first + 1;
	int guest_argc = argc - first - 1;

	char script_interp[GUEST_PATH_MAX];
	char script_argument[GUEST_PATH_MAX];
	char interp_path[GUEST_PATH_MAX];
	char *rebuilt[MAX_EXEC_ARGS];

	if (read_shebang(elf, script_interp, script_argument)) {
		int at = 0;

		/* argv becomes the interpreter, its one optional argument, the
		 * script, and then what the script was called with — which is
		 * what the kernel would have built.
		 *
		 * The script goes in by its path, not by the name it was called
		 * with. The interpreter has to open it, and a name is not
		 * something to open: `pip3 install x` handed python3 the word
		 * `pip3`, which it looked for in the directory it happened to
		 * be standing in and reported as /root/pip3. */
		rebuilt[at++] = script_interp;
		if (script_argument[0])
			rebuilt[at++] = script_argument;
		rebuilt[at++] = (char *)elf;
		for (int i = 1; i < guest_argc && at < MAX_EXEC_ARGS - 1; i++)
			rebuilt[at++] = guest_argv[i];
		rebuilt[at] = NULL;
		guest_argv = rebuilt;
		guest_argc = at;

		if (mount_count) {
			if (!map_path(script_interp, 1, interp_path))
				fail2("cannot find the interpreter",
				      script_interp);
			elf = interp_path;
		} else {
			elf = script_interp;
		}
	}

	struct image program;
	struct image interp;
	map_elf(elf, &program);

	u64 entry = program.entry;
	struct image *interp_image = NULL;
	if (program.has_interp) {
		char path[PATH_MAX];
		resolve_interpreter(environment_value(envp, "EXTCLI_ROOT"),
				    program.interp, path);
		map_elf(path, &interp);
		interp_image = &interp;
		entry = interp.entry;
	}

	/* The guest's argv begins with the name it should answer to, which the
	 * caller chose: busybox is a different program depending on it. */
	u64 *stack = build_stack(guest_argc, guest_argv, envp, auxv, &program,
				 interp_image);

	/* Supervision costs two stops per syscall, so it happens only when the
	 * device has given us something to neutralise, or when someone is
	 * watching — and never when a supervisor is already stepping this
	 * process, which is what the child sentinel says. A failed fork runs
	 * the guest unsupervised rather than not at all. */
	if (!supervised && (tracing || rule_count || mount_count || no_tmpfile)) {
		long child = syscall6(SYS_clone, 17 /* SIGCHLD */, 0, 0, 0, 0, 0);
		if (child > 0)
			supervise(child, tracing);
		if (child == 0 &&
		    syscall6(SYS_ptrace, PTRACE_TRACEME, 0, 0, 0, 0, 0) == 0) {
			/* stop here, where the parent knows to look for us,
			 * rather than racing it into the guest */
			sys3(SYS_kill, syscall6(SYS_getpid, 0, 0, 0, 0, 0, 0),
			     SIGSTOP, 0);
		}
	}

	/* Pinned to registers that are not among the ones being cleared. Left to
	 * the compiler, the stack pointer landed in x0 and was zeroed by the
	 * line meant to tidy up before the jump — a segfault before the guest's
	 * first instruction, blamed for a while on the mapping. */
	register u64 target __asm__("x9") = entry;
	register u64 new_sp __asm__("x10") = (u64)stack;

	__asm__ volatile(
		"mov sp, x10\n"
		"mov x0, #0\n"
		"mov x1, #0\n"
		"mov x2, #0\n"
		"mov x3, #0\n"
		"mov x29, #0\n"
		"mov x30, #0\n"
		"br x9\n"
		:
		: "r"(target), "r"(new_sp)
		: "memory");
	__builtin_unreachable();
}

/* The linker jumps here with the stack the kernel built; loader_main needs to
 * see it, and C will not hand over the stack pointer. */
__asm__(".global _start\n"
	"_start:\n"
	"	mov x0, sp\n"
	"	b loader_main\n");
