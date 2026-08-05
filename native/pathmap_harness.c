/* SPDX-License-Identifier: Apache-2.0
 *
 * Builds pathmap.c for whatever machine the tests run on, so the resolving can
 * be tried against a real directory tree with real symlinks.
 *
 * The loader itself cannot be run here — it is aarch64, and the emulator does
 * not implement ptrace — so without this the one piece most likely to be
 * subtly wrong would only ever be tested by sending a build to a phone. It is
 * the same file, not a copy: a second implementation would agree with itself
 * and prove nothing.
 *
 *   pathmap-harness <mounts> <pass-through> <path> [nofollow]
 *
 * where <mounts> is what the loader reads out of EXTCLI_MOUNTS:
 * `/=<rootfs>|/sdcard=<host>|...`.
 */

#include <stdio.h>
#include <string.h>
#include <unistd.h>

typedef unsigned long u64;

#define GUEST_PATH_MAX 1024
#define MAX_LINKS 40

static char map_root[GUEST_PATH_MAX];
static u64 map_root_length;
static char pass_through[GUEST_PATH_MAX];

static u64 string_length(const char *text)
{
	return strlen(text);
}

static long platform_readlink(const char *path, char *out, u64 size)
{
	return readlink(path, out, size);
}

#include "pathmap.c"

int main(int argc, char **argv)
{
	char out[GUEST_PATH_MAX];

	if (argc < 4) {
		fprintf(stderr, "usage: %s <mounts> <pass> <path> [nofollow]\n",
			argv[0]);
		return 2;
	}
	read_mounts(argv[1]);
	copy_string(pass_through, argv[2], sizeof(pass_through));

	/* "host" asks whether a path has been through the mapping already */
	if (argc > 4 && !strcmp(argv[4], "host")) {
		printf("%s\n", already_host(argv[3]) ? "host" : "guest");
		return 0;
	}
	/* "back" asks the other direction, which is what getcwd needs */
	if (argc > 4 && !strcmp(argv[4], "back")) {
		if (!unmap_path(argv[3], out)) {
			printf("fail\n");
			return 1;
		}
		printf("%s\n", out);
		return 0;
	}
	if (passed_through(argv[3])) {
		printf("pass\n");
		return 0;
	}
	if (!map_path(argv[3], argc < 5, out)) {
		/* the reason, not just the refusal — the loader reports these
		 * back from the device and the names have to mean the same
		 * thing in both places */
		static const char *why[4] = {
			"none", "long", "loop", "noroot",
		};

		printf("fail %s\n", why[map_failure & 3]);
		return 1;
	}
	printf("%s\n", out);
	return 0;
}
