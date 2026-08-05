# SPDX-License-Identifier: Apache-2.0

"""Execution backends for external commands.

`exec` would be a confusing package name in Python, so the layer is called
`backends`. Ordered by preference at runtime:

  system  -- subprocess/pty on /system/bin/sh (toybox); the default
  inproc  -- pure-Python builtins, always available
  linker  -- /system/bin/linker64 <binary> to run our own ELFs (probe-gated)

extCLI deliberately does not talk to Termux or any other app: everything it can
do, it does inside the client's own sandbox.

Which of these actually work on a given device is decided by probe.py, never
assumed.
"""
