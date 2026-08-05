# SPDX-License-Identifier: Apache-2.0

"""A Linux root filesystem inside the client.

Nothing here imports Android: paths arrive as arguments, so the extraction
rules, the layout and the reading of the exec experiments are all tested on a
desktop. What must happen on a device is the measuring, and that is why
exec_probe exists — the question of whether a rootfs is possible at all is a
question about this device's SELinux policy, not about the code.
"""
