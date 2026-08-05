# SPDX-License-Identifier: Apache-2.0

"""Turning command results into terminal output.

Commands never write escape codes themselves: they return Block objects
(blocks.py), and a style module renders those into ANSI text. That indirection
is what lets the same command look right in the classic prompt style now and in
the other planned styles later, and it keeps this whole layer testable without
a device.
"""
