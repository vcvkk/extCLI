# SPDX-License-Identifier: Apache-2.0

"""Everything that touches exteraGram classes lives here.

The rest of the plugin must not import android/java modules directly: keeping
that boundary is what lets shell/, render/ and tui/composer be unit-tested off
device, and what will make an AyuGram port a change in this package only.
"""
