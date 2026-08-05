# SPDX-License-Identifier: Apache-2.0

"""Starting a process and waiting for it.

Two shapes, used by everything that measures this device: one that runs a
command, and one that runs it with an environment. Neither imports anything of
the client's, so the measurements can be exercised on a desktop.

They live here rather than in the `rootfs` builtin because the same steps run
without a console at all — the first setup happens when the plugin is loaded,
where there is nobody to type a command.
"""

import os


def plain(timeout=15):
    """Runs a command and returns (status, stdout, stderr)."""
    import subprocess

    def run(command):
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       universal_newlines=True)
            out, err = process.communicate(timeout=timeout)
            return process.returncode, out, err
        except Exception as e:
            return 126, "", "%s: %s" % (type(e).__name__, e)

    return run


def watching(timeout=15):
    """The same, but each line of output is shown as it arrives.

    The syscall scan is a child process per number and takes long enough to
    look stuck. It prints the refusals in order, so a caller watching the
    lines can tell how far through the table it is — which is the only
    progress it can report without being asked to count.

    Marked with `streams` so a caller can tell it from `plain`, which cannot.
    """
    import subprocess

    def run(command, on_line=None):
        lines = []
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       universal_newlines=True, bufsize=1)
        except Exception as e:
            return 126, "", "%s: %s" % (type(e).__name__, e)
        try:
            for line in process.stdout:
                lines.append(line)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:
                        pass
            process.wait(timeout=timeout)
            err = process.stderr.read()
        except Exception as e:
            try:
                process.kill()
            except Exception:
                pass
            return 126, "".join(lines), "%s: %s" % (type(e).__name__, e)
        return process.returncode or 0, "".join(lines), err or ""

    run.streams = True
    return run


def with_env(timeout=20):
    """The same, for a command that has to be told where things are."""
    import subprocess

    def run(command, env):
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(os.environ, **(env or {})), universal_newlines=True)
            out, err = process.communicate(timeout=timeout)
            return process.returncode, out, err
        except Exception as e:
            return 126, "", "%s: %s" % (type(e).__name__, e)

    return run
