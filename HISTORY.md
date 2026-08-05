# Development history

The 112 commits that built extCLI 0.1.0, from the first skeleton to the last
commit before the working tree was lost. Times are UTC.

This file exists because the commits themselves do not. The object store went
with the working tree, and what survived was metadata: the reflog, and 111
commit objects rescued one at a time. A commit object carries its message, its
author, its dates and the *hash* of its tree — not the tree. Without the tree
and blob objects the file contents at each step are gone, and a hash cannot be
reversed into the thing it names, so the diffs are not recoverable. This list
is the record that remains.

The source is not lost. The state at the final commit was recovered in full and
is what this repository holds: every module verified against the shipped
bytecode, the native binaries and the dex rebuilt byte for byte from their
sources, 653 tests passing.

| | |
|---|---|
| Commits | 112 |
| First | 2026-07-29 22:24 |
| Last | 2026-08-01 14:44 |
| Span | 64 hours |
| Author | Claude <noreply@anthropic.com>, SSH-signed |

## 2026-07-29

*6 commits*

- **22:24** `f17976c` — Add plugin skeleton, capability probe and terminal renderer
- **22:24** `64c07ec` — Add the console: terminal screen, command layer and menu entries
- **22:32** `9767dfe` — Work on clients with and without the elyx module
- **22:39** `1caf97e` — Fix the console failing to open, and make failures name themselves
- **22:44** `da08cb3` — Stop deriving the install path from __file__
- **22:48** `1a3c081` — Read the linker probe correctly: running our own ELFs is allowed

## 2026-07-30

*32 commits*

- **09:21** `5ec340b` — Replace the command splitter with a real shell
- **09:25** `48af374` — Add `config`: the client's own settings from the console
- **09:54** `8f51a90` — Drop Termux, and make an empty console explain itself
- **09:56** `a204de6` — Attach the console to the fragment view as well as returning it
- **10:18** `47912d2` — Open the console as a bottom sheet, and let diagnostics test it
- **10:25** `120c8d3` — Ship a terminal that works: stock views by default, dex renderer opt-in
- **10:29** `e9abace` — Add `send`: messages, files and photos from the console
- **14:12** `932056c` — Pack colors as signed 32-bit ints, which is what Android takes
- **14:35** `90cd329` — Make the console a Termux screen: no header, no text field
- **14:50** `db36b4c` — Keep the session alive across screens, and stop losing focus
- **15:02** `8b5bcf9` — Give the console its own window instead of a fragment
- **15:10** `3d22ed6` — Make the console window actually fill the screen
- **15:37** `688eb3e` — Split send from search, select text by hand, sweep every command
- **15:43** `a11325f` — Fix the two things the device found
- **15:48** `403986f` — Get out of selection mode, and stop shrinking the window
- **15:54** `6f6a964` — Move selection to its own window, and make the device answer about the bar
- **16:04** `f39e37d` — Select in the terminal, and let the window past the navigation bar
- **16:10** `9e4be1d` — Size the console window in pixels, not MATCH_PARENT
- **19:29** `33dbbd0` — Start the rootfs: run our own binaries, and measure whether that is enough
- **19:39** `7ea28e2` — Fix send on SDK 1.4.5.3, stop copying the cursor, add a self-test
- **19:44** `f0a6352` — Work out how a guest binary can be started, rather than assuming
- **19:52** `08cfe01` — The rootfs verdict was the experiment's fault, not the device's
- **19:56** `ba86acb` — A rootfs on a device that only allows linker-wrapped exec
- **20:02** `3350b24` — Ship Alpine inside the plugin: rootfs install alpine
- **20:13** `29daafe` — Follow guest symlinks the guest's way, and try two more launch forms
- **20:17** `8b14138` — Name the crash, and say what four red lines actually mean
- **20:25** `51fc276` — Ask whether execve is allowed anywhere before building around it
- **20:31** `12a1606` — Nothing writable is executable; build our own binary and ask about that
- **20:43** `1c668b2` — An ELF loader of our own: any arm64 rootfs runs
- **20:49** `c17dd8c` — Let the loader find its own arguments instead of counting them
- **20:54** `396095e` — Make the loader name the syscall the sandbox refused
- **21:02** `8964455` — Ask the sandbox which syscalls it allows, from outside

## 2026-07-31

*54 commits*

- **09:07** `3b9dc60` — Scan every syscall number instead of a list of guesses
- **09:13** `0893939` — Trace the guest, and stop claiming to know why it died
- **09:33** `0d54e8f` — Answer the syscalls the sandbox refuses instead of dying on them
- **09:46** `3a9cb59` — Replace a refused syscall instead of cancelling it
- **09:50** `2ed166e` — Read stdin as text before handing it to the rootfs
- **09:57** `2069a72` — Give the guest the rootfs as its own /
- **10:12** `d1e17f5` — Translate the guest's paths at the syscall
- **10:16** `e06a7c9` — Say why cd is not in the rootfs
- **14:05** `55bcd4b` — Give the guest four mounted paths, and a switch for each
- **14:09** `7985eb7` — Move the shell into the world it is running in
- **14:14** `d3ed311` — Type Alpine's programs directly, and drop `rootfs run`
- **14:19** `ae35ff8` — Translate the working directory, and what a glob comes back with
- **14:29** `9781454` — Keep the cursor under the output, and show a command working
- **14:36** `3b2dc4e` — Give the guest a resolver
- **14:40** `e595cea` — Make the loader say which call failed and why
- **14:45** `7a98203` — Refuse a bare program name instead of handing it to the loader
- **14:53** `9cf2598` — Ask the device how a file may be written here
- **14:57** `f9bca9e` — Tell the guest this filesystem has no unnamed files
- **15:00** `82ffb53` — Say what was measured, not what it sounded like
- **15:09** `14676cb` — Let the guest exec, by turning its exec into the one that is allowed
- **15:17** `e3a3e24` — Rewrite a relative exec too, and refresh the resolver on demand
- **15:26** `d0599ee` — Keep the scratch page where both sides look after an exec
- **15:31** `24c6901` — Map the page in a loader that was started by an exec
- **15:36** `f879cbf` — Map the page before anything is opened
- **15:43** `6cb6014` — Do not translate a path that has already been translated
- **15:54** `e1c5ba8` — Ask for a directory by the name the kernel uses
- **16:03** `7bcceb6` — Give a guest program a terminal instead of a pipe
- **16:07** `ff61012` — Understand the colours a program actually sends
- **16:16** `eea1e5a` — Turn a moved cursor into the gap it would have left
- **16:25** `ca55e6a` — Measure a line by what is drawn, and the screen by what it is
- **16:30** `0975aa9` — One answer to what is invisible, and use it for blankness too
- **16:40** `3e79e4c` — Let the reader scroll, let a progress bar redraw, and open a script by its path
- **19:05** `f88cf13` — Carry out the cursor moves instead of guessing at them
- **19:05** `6782257` — Put the transcript through the same function, as the last message said
- **19:11** `36300fb` — Make the trace usable on something that takes a minute
- **19:21** `2ec1170` — Give the guest a home inside itself, and say why a path failed
- **19:30** `ae2e61a` — Do not translate paths into a page that is not there yet
- **19:45** `a5b8761` — Give a piped program an end, and a running one an ear
- **19:53** `1ffa325` — Look where a user installs things
- **20:00** `14bbe5a` — Stop a program that is running
- **20:13** `3c9d5ba` — Make the rootfs ready without being asked
- **20:28** `6905ff2` — Show the first setup happening
- **20:35** `168c151` — Let the container be deleted
- **20:43** `74f3eb5` — Wait for the client to finish saying its piece
- **20:53** `dea7e99` — Build the card the client builds
- **20:59** `64595dc` — Use a bulletin the client can actually build
- **21:05** `e9efd6d` — Put the bar inside the card
- **22:31** `cc73fa0` — Material's own bar, along the bottom of the card
- **22:37** `589dd96` — Give the bar the corner instead of stopping short of it
- **22:41** `5a9d2f8` — Do not paint the thing that only clips
- **22:52** `34de4c8` — The grey half-circle was one colour drawn twice
- **23:01** `47e006e` — Take off the ornaments that draw where the fill ends
- **23:06** `b6c4697` — Keep the gap, which is the point of it
- **23:14** `2b442ff` — Settings worth opening

## 2026-08-01

*20 commits*

- **00:11** `58464d1` — A cursor that can go up, and a redraw that waits
- **00:26** `d7e6f45` — Redraw the tail, not the whole terminal
- **00:31** `191ed33` — Keep the fling
- **00:47** `332ec7a` — A mount for the client's own code, and quieter defaults
- **01:01** `b26706b` — Tools in the container, fetched rather than bundled
- **01:15** `3988c86` — Ask what to put in the container, once
- **01:21** `074a212` — The client's own tick, not two glyphs
- **01:30** `1123e83` — Groups that open on springs
- **01:36** `0783571` — A grouped list, and a tick that is measured rather than named
- **01:40** `272f303` — The line that was missing was not a colour
- **09:39** `92de120` — The button says what it will do
- **09:53** `682935f` — The button that stays walks into the middle
- **10:11** `642460c` — Two threads, one page, the wrong path
- **10:22** `d3ee857` — The prompt no longer blinks out on every command
- **10:47** `ad33a2d` — A screen a program can keep to itself
- **10:52** `73d39d9` — The run that was still two parts long
- **11:01** `7cb6619` — A cursor to swap, and keys that repeat while held
- **11:06** `da71828` — The cursor's cell, this time actually in the build
- **14:28** `1d26c94` — Keys that arrive, and a screen that is the size it looks
- **14:44** `a9deba7` — Toolsets that need each other, and an install you can watch

---

Timestamps come from each commit object where one survives, and from the reflog
otherwise; the two agree everywhere both exist. The hashes are the originals.
They resolve to nothing now — the objects they name were never pushed and exist
nowhere — but they are what these commits were called.
