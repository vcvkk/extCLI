# Changelog

## 0.1.1-rc

### The bug that mattered

- A guest path can no longer reach a backend that cannot translate it. With a
  rootfs mounted, `/` means the container's root, and a backend that does not
  know that would have been handed the phone's own `/` instead — which is how
  a command aimed at a container reaches the device. Backends now declare
  whether they translate, and the ones that do not are left out entirely while
  a rootfs is active.

### Commands renamed

Old names are gone rather than aliased.

| was | is |
|---|---|
| `copy` | `clip` |
| `send` | `tg send` |
| `search` | `tg chats`, `tg id` |
| `host doctor` | `host check` |
| `host window`, `host selftest` | `host check --window`, `host check --self` |
| `backends` | `host backends` |
| `rootfs writes`, `dirs`, `native`, `syscalls`, `check`, `launch` | `rootfs probe <what>` |
| `rootfs tools` | `rootfs pkg` |
| `rootfs sources` | `rootfs images` |

Options follow the forms a shell user already knows: `--help`/`-h` on every
command, `--name=value`, short flags run together (`-la`, `-n5`), and `--` to
end the options.

### New

- **`patch`**, which patches two different things under one word.

  *A plugin.* `patch open <id>` unpacks it under `/patch`; edit it with
  anything in the container and `patch build` makes a new plugin called
  `extCLI patch-62Yg28`, with a summary of what moved in its description and
  the full report inside the archive. `patch code` handles the plugins that
  shipped compiled: it disassembles a `.pyc`, lists every string and name in
  it, and can swap a constant exactly, leaving every instruction and line
  number where it was. It does not pretend to decompile — Python 3.11 has no
  working decompiler.

  *The client.* `patch open client` indexes exteraGram's fifty thousand
  classes out of its own APK — read where it sits, one dex at a time, not
  copied in. `patch find` searches classes, methods and string constants,
  `patch dis` writes the smali for a class into the workspace, and
  `patch hook <class> <method>` starts a hook with the parameter types read
  out of the dex. `patch build` turns the hooks into a plugin that applies
  them as it loads.

  The APK is never rewritten and the original plugin is never overwritten. A
  patch is always a separate plugin, so turning it off puts the phone back —
  which is the whole reason for doing it this way. Repacking the APK would
  mean a client that has to be reinstalled, loses its data directory and stops
  updating.

  The dex parser and disassembler are checked against the real client: 54,584
  classes from seven dex files, 301,536 methods, 5,448,485 instructions, 217
  distinct opcodes, no desynchronisation and no unknown opcode.
- **`plugin install <file.eaf>`** — the last step of writing a plugin on the
  phone. The archive is read and checked here before the client is handed
  anything, and an install over a plugin already there is refused unless
  `--force` says otherwise.
- **`tg read`** — a conversation as a stream of text, so a chat can be piped
  into `grep`.
- **`tg get`** — a chat's attachments as files in the container.
- **`.cli <command>` answers in the chat** it was typed into. One message,
  edited as the output arrives, rendered through the same terminal the console
  uses — so a progress bar is the three lines it ends up being, not the twelve
  hundred it wrote. How often it may be rewritten is a setting; the shortest
  offered is a floor nothing may go under, and a rate limit from the server is
  honoured exactly as asked and remembered.
- **Export the container** — a button in the settings packs Alpine and
  everything installed into it into one file and opens the share sheet, so a
  backup can go to a chat with yourself.
- **The key rows are yours** — the extra keys under the terminal can be
  rearranged in the settings, on a live preview drawn by the same code that
  draws the real rows. Every action the console understands is on offer; a
  row holds up to eight keys and there can be three rows.
- **An rc file** — `.extclirc` in the home directory is run when a session
  starts, for aliases and variables that should always be there.
- **How many lines the console keeps** is a setting. 1500 threw away the
  start of a long `apk` run, which is where the error usually is.

### Fixed

- `rootfs mounts` never showed `/patch`, although the console mounted it: two
  lists of the mounts had drifted apart.
- The plugin-install method is taken from the client rather than guessed.

905 tests pass.
