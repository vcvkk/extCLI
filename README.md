# extCLI

A plugin for exteraGram that turns your client into a TUI/CLI environment: a
real shell, control over plugins and client settings, tooling for plugin
authors, and a mode that renders the whole client as text.

Status: early development.

## What works

- A console, opened from the side menu, a chat's overflow menu, or by typing
  `.cli` in any chat.
- A POSIX-style shell: pipes, redirection including here-documents, `&&`/`||`,
  variables and `${...}` forms, `$(...)`, arithmetic, globbing, `if`, `for`,
  `while`/`until`, `case`, functions, aliases and `source`.
- Real programs through `/system/bin/sh` and the toybox applets, with a
  Python-implemented fallback set (`ls`, `cat`, `grep`, `head`, …) for devices
  where the system shell is unavailable.
- Client commands: `plugin` (list/info/install/enable/disable/reload/path/config),
  `host` (status/paths/check/backends/version/class), `tg` (send/read/get/chats/id),
  `patch` (open/list/diff/code/build/revert/drop), `config`, `log`.
- `patch`: unpack an installed plugin into a workspace under `/patch`, edit it
  with anything in the container, and build the changes into a *new* plugin —
  `extCLI patch-62Yg28`, carrying a summary of what moved. The original stays
  installed, so turning the patch off puts the phone back. For plugins that
  shipped compiled, `patch code` disassembles a `.pyc` and can swap a constant
  exactly, leaving every instruction and line number where it was.
- Colors taken from your Telegram theme, plus an Amoled theme.
- The extra key rows under the terminal can be rearranged in the settings, on
  a live preview drawn by the same code that draws the real ones.

## Requirements

| | |
|---|---|
| Client | exteraGram 12.9.0 or newer |
| Plugin SDK | 1.4.5.1 or newer |
| Android | 7.0+ (API 24) |

## Building

Install [ElyxBuilder](https://pypi.org/project/ElyxBuilder/):

```bash
pip install ElyxBuilder
```

From the repository root:

```bash
elyb build -v -nf          # uncompiled
elyb build -c 2 -v -nf     # compiled to bytecode (Python 3.11)
```

The archive is written to `builds/`.

### Kotlin renderer

The character-grid renderer is Kotlin, shipped prebuilt as
`extcli/dex/terminal.dex`, so an ordinary plugin build needs no Java toolchain.
Rebuild it only when `kotlin/` changes:

```bash
./scripts/kotlin-build.sh
```

The script looks for its toolchain in `$EXTCLI_TOOLCHAIN`
(default `/opt/extcli-toolchain`), or in a local Android SDK:

| Tool | Where to get it |
|---|---|
| `kotlinc` | [Kotlin releases](https://github.com/JetBrains/kotlin/releases) → unpack as `kotlinc/` |
| `r8.jar` (provides d8) | [Google Maven](https://maven.google.com/com/android/tools/r8/) |
| `android-all.jar` | [Maven Central](https://repo1.maven.org/maven2/org/robolectric/android-all/) |

## Tests

The shell, the output formatting layer and the TUI composer never import
Android APIs, so they run off device:

```bash
python3 -m pytest tests/
```

## Notes on the runtime

exteraGram targets SDK 36, so SELinux forbids executing files from the app's
data directory. extCLI therefore does not assume a bundled `proot` can run:
it probes the device at startup and picks a backend from what is actually
allowed — `/system/bin/sh` in a pty, in-process Python builtins, the dynamic
linker, or a Termux bridge. Run **Diagnostics** on the plugin's settings page
to see the result for your device.

## License

[Apache-2.0](LICENSE)
