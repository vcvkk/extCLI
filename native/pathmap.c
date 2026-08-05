/* SPDX-License-Identifier: Apache-2.0
 *
 * Turning a guest path into a host path, the way a kernel under chroot would.
 *
 * Included by the loader rather than linked, because the loader is freestanding
 * and has no linker of its own to join two objects. Included by a test harness
 * too, which is the reason it is a file at all: this is the piece most likely
 * to be subtly wrong, it cannot be tried on the device without a round trip,
 * and it is pure — a path in, a path out, and readlink in between.
 *
 * The includer provides:
 *
 *   u64                     an unsigned 64-bit type
 *   string_length(s)
 *   map_root, map_root_length, pass_through   where the rootfs is, and what
 *                                             is not in it
 *   platform_readlink(path, out, size)        the length, or negative
 *
 * GUEST_PATH_MAX and MAX_LINKS come from the includer as well, so the loader
 * and the harness agree about the sizes they are testing.
 */

static void copy_string(char *out, const char *text, u64 size)
{
	u64 i = 0;
	while (text[i] && i + 1 < size) {
		out[i] = text[i];
		i++;
	}
	out[i] = 0;
}

/* Is this one of the paths that means the host's, not the guest's? Matched by
 * whole component, so /devices is not /dev. */
static int passed_through(const char *path)
{
	const char *at = pass_through;

	while (*at) {
		const char *p = path;

		while (*at && *at != ':' && *at == *p) {
			at++;
			p++;
		}
		if ((*at == 0 || *at == ':') && (*p == 0 || *p == '/'))
			return 1;
		while (*at && *at != ':')
			at++;
		while (*at == ':')
			at++;
	}
	return 0;
}

static long read_link(const char *path, char *out, u64 size)
{
	long n = platform_readlink(path, out, size - 1);

	if (n < 0)
		return n;
	out[n] = 0;
	return n;
}

/* --------------------------------------------------------------- mounts
 *
 * The rootfs is the guest's `/`, and three more host directories are grafted
 * onto names of their own — the phone's storage, the client's files, this
 * plugin's — so that a shell inside the rootfs can still reach the phone it is
 * running on. Which ones are there is settled in the plugin's settings and
 * arrives as EXTCLI_MOUNTS: `/=<rootfs>|/sdcard=<host>|...`.
 *
 * Only the rootfs gets its symlinks followed here. Its links are guest paths —
 * Alpine's /bin/sh points at /bin/busybox and means the rootfs's — while a
 * link under /sdcard is an ordinary host link that means what it says, so the
 * kernel is left to follow those itself.
 */

#define MAX_MOUNTS 8

static char mount_guest[MAX_MOUNTS][GUEST_PATH_MAX];
static char mount_host[MAX_MOUNTS][GUEST_PATH_MAX];
static int mount_count;

static void read_mounts(const char *text)
{
	mount_count = 0;
	if (!text)
		return;
	while (*text && mount_count < MAX_MOUNTS) {
		u64 n = 0;

		while (*text == '|')
			text++;
		while (text[n] && text[n] != '=' && text[n] != '|')
			n++;
		if (!n || text[n] != '=')
			break;
		if (n + 1 >= GUEST_PATH_MAX)
			return;
		for (u64 i = 0; i < n; i++)
			mount_guest[mount_count][i] = text[i];
		mount_guest[mount_count][n] = 0;
		text += n + 1;

		n = 0;
		while (text[n] && text[n] != '|')
			n++;
		if (!n || n + 1 >= GUEST_PATH_MAX)
			return;
		for (u64 i = 0; i < n; i++)
			mount_host[mount_count][i] = text[i];
		mount_host[mount_count][n] = 0;
		/* a trailing slash would double up on every path built from it */
		while (n > 1 && mount_host[mount_count][n - 1] == '/')
			mount_host[mount_count][--n] = 0;
		text += n;
		mount_count++;
	}
	/* the rootfs is the one every other path falls back to */
	for (int i = 0; i < mount_count; i++) {
		if (mount_guest[i][0] != '/' || mount_guest[i][1])
			continue;
		copy_string(map_root, mount_host[i], GUEST_PATH_MAX);
		map_root_length = string_length(map_root);
		return;
	}
}

/* Which mount a path is in, by longest name, `/` excluded — it matches
 * everything and is the answer only when nothing else is. */
static int mount_for(const char *path)
{
	int best = -1;
	u64 best_length = 0;

	for (int i = 0; i < mount_count; i++) {
		const char *guest = mount_guest[i];
		u64 n = string_length(guest);
		u64 j = 0;

		if (n < 2)
			continue;
		while (j < n && path[j] == guest[j])
			j++;
		if (j != n || (path[j] != 0 && path[j] != '/'))
			continue;
		if (n > best_length) {
			best = i;
			best_length = n;
		}
	}
	return best;
}

/* Is this a path on the host already?
 *
 * A path that begins where a mount really is has been through this once. It
 * happens whenever a real path finds its way back into the guest — the loader
 * is handed one when it is exec'd, /proc/self/exe answers with one — and
 * translating it a second time buries it inside the rootfs, where nothing is.
 *
 * A guest that names a host path on purpose gets it left alone, which is what
 * it asked for anyway.
 */
static int already_host(const char *path)
{
	for (int i = 0; i < mount_count; i++) {
		const char *host = mount_host[i];
		u64 n = string_length(host);
		u64 j = 0;

		if (n < 2)
			continue;
		while (j < n && path[j] == host[j])
			j++;
		if (j == n && (path[j] == 0 || path[j] == '/'))
			return 1;
	}
	return 0;
}

/* Why the last map_path said no. A refusal is otherwise a bare zero, and the
 * three reasons want different fixes — a longer buffer, a broken link, or a
 * mount that is not there at all — so the trace has to be able to tell them
 * apart instead of reporting one number for all three.
 */
#define MAP_FAIL_LONG 1		/* the host path does not fit in the buffer */
#define MAP_FAIL_LOOP 2		/* symlinks going round in a circle */
#define MAP_FAIL_NOROOT 3	/* nothing this path could be inside */

static int map_failure;

/* A guest path as the host must write it. Returns 0 if it does not fit or the
 * links go round in a circle. */
static int map_path(const char *guest, int follow_last, char *out)
{
	char buffer_a[GUEST_PATH_MAX];
	char buffer_b[GUEST_PATH_MAX];
	char target[GUEST_PATH_MAX];
	char *remaining = buffer_a;
	int in_a = 1;
	u64 length = map_root_length;
	int links = 0;

	int found = mount_for(guest);

	map_failure = MAP_FAIL_LONG;
	if (string_length(guest) + map_root_length + 2 >= GUEST_PATH_MAX)
		return 0;
	if (found >= 0) {
		/* A host directory grafted onto a name of its own. Its links
		 * are the host's and mean what they say, so they are left to
		 * the kernel; only the rootfs has links written for a guest. */
		const char *rest = guest + string_length(mount_guest[found]);
		u64 at = string_length(mount_host[found]);

		if (at + string_length(rest) + 1 >= GUEST_PATH_MAX)
			return 0;
		copy_string(out, mount_host[found], GUEST_PATH_MAX);
		copy_string(out + at, rest, GUEST_PATH_MAX - at);
		map_failure = 0;
		return 1;
	}
	if (!map_root_length) {
		map_failure = MAP_FAIL_NOROOT;
		return 0;
	}
	copy_string(buffer_a, guest, GUEST_PATH_MAX);
	copy_string(out, map_root, GUEST_PATH_MAX);
	out[length] = 0;

	for (;;) {
		u64 n = 0;
		u64 mark;
		const char *rest;
		int last;

		while (*remaining == '/')
			remaining++;
		if (!*remaining)
			break;
		while (remaining[n] && remaining[n] != '/')
			n++;
		rest = remaining + n;
		while (*rest == '/')
			rest++;
		last = *rest == 0;

		if (n == 1 && remaining[0] == '.') {
			remaining += n;
			continue;
		}
		if (n == 2 && remaining[0] == '.' && remaining[1] == '.') {
			while (length > map_root_length &&
			       out[length - 1] != '/')
				length--;
			if (length > map_root_length)
				length--;
			out[length] = 0;
			remaining += n;
			continue;
		}
		mark = length;
		if (length + n + 2 >= GUEST_PATH_MAX)
			return 0;
		out[length++] = '/';
		for (u64 i = 0; i < n; i++)
			out[length + i] = remaining[i];
		length += n;
		out[length] = 0;

		if (!last || follow_last) {
			long got = read_link(out, target, sizeof(target));

			if (got > 0) {
				/* the other buffer, whole: `remaining` has
				 * been walking through this one and writing
				 * from where it stands would run off the end */
				char *into = in_a ? buffer_b : buffer_a;
				u64 at;

				if (++links > MAX_LINKS) {
					map_failure = MAP_FAIL_LOOP;
					return 0;
				}
				if (string_length(target) +
				    string_length(rest) + 2 >= GUEST_PATH_MAX)
					return 0;
				/* an absolute target is absolute inside the
				 * rootfs, which is the whole difficulty */
				length = target[0] == '/' ? map_root_length
							  : mark;
				out[length] = 0;

				copy_string(into, target, GUEST_PATH_MAX);
				at = string_length(into);
				into[at++] = '/';
				copy_string(into + at, rest,
					    GUEST_PATH_MAX - at);
				remaining = into;
				in_a = !in_a;
				continue;
			}
		}
		remaining += n;
	}
	map_failure = 0;
	return 1;
}

/* The other direction, for getcwd: the guest has never heard of the host
 * directory it is standing in. Longest host name wins, so a mount that lives
 * inside the rootfs still answers with its own name rather than the rootfs's.
 */
static int unmap_path(const char *host, char *out)
{
	int best = -1;
	u64 best_length = 0;
	const char *rest;
	u64 at;

	for (int i = 0; i < mount_count; i++) {
		u64 n = string_length(mount_host[i]);
		u64 j = 0;

		while (j < n && host[j] == mount_host[i][j])
			j++;
		if (j != n || (host[j] != 0 && host[j] != '/'))
			continue;
		if (best < 0 || n > best_length) {
			best = i;
			best_length = n;
		}
	}
	if (best < 0)
		return 0;
	rest = host + best_length;
	at = string_length(mount_guest[best]);
	copy_string(out, mount_guest[best], GUEST_PATH_MAX);
	/* the root mount is "/" already, and "/" plus "/bin" is "//bin" */
	if (at == 1 && out[0] == '/')
		at = 0;
	copy_string(out + at, rest, GUEST_PATH_MAX - at);
	if (!out[0]) {
		out[0] = '/';
		out[1] = 0;
	}
	return 1;
}
