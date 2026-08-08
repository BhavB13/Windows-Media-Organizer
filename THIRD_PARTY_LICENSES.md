# Third-Party Licenses

Duplicate & Transfer Manager is distributed under the [MIT License](LICENSE) and
redistributes the components below. License identifiers were read from the
installed package metadata for the pinned versions in `pyproject.toml`, not from
memory, so they describe what actually ships.

## Runtime dependencies

| Component | Version | License |
| --- | --- | --- |
| [PySide6](https://wiki.qt.io/Qt_for_Python) | 6.9.1 | `LGPL-3.0-only OR GPL-2.0-only` (a commercial Qt licence is also available) |
| [shiboken6](https://wiki.qt.io/Qt_for_Python) | 6.9.1 | `LGPL-3.0-only OR GPL-2.0-only` |
| [Pillow](https://python-pillow.org/) | 11.3.0 | `MIT-CMU` |
| [Send2Trash](https://github.com/arsenetar/send2trash) | 1.8.3 | BSD 3-Clause |
| [packaging](https://github.com/pypa/packaging) | 26.2 | `Apache-2.0 OR BSD-2-Clause` |
| [tkinterdnd2](https://github.com/pmgagne/tkinterdnd2) | 0.4.3 | MIT |

`tkinterdnd2` is used only by the legacy Tkinter frontend retained for
compatibility (`legacy_main.py`). It is not used by the PySide6 application.

## Bundled tools

| Component | Version | License |
| --- | --- | --- |
| [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) | 37.0.0 | Android Software Development Kit License Agreement |

Only `platform-tools/adb.exe` is redistributed, from the pinned release recorded
in `packaging/android_platform_tools_manifest.json`. The application never
modifies system-wide ADB installations or environment variables, and does not
alter a developer's existing `adb` on `PATH`.

## LGPL notice for Qt

PySide6 and shiboken6 are used under the LGPL v3. The LGPL requires that users be
able to replace the Qt libraries with their own compatible versions. The Windows
installer therefore ships the Qt libraries as separate, unmodified shared
libraries inside the application directory rather than statically linking them,
so they can be substituted.

Qt is not modified by this project. Source for the Qt libraries is available from
the Qt Project. If you need the exact sources corresponding to a shipped build,
open an issue referencing the release version.

## Verifying what a build contains

Every released installer is Authenticode signed and accompanied by a signed
update manifest listing its SHA-256 checksum. See [SECURITY.md](SECURITY.md) for
how those signatures are verified.

## Reporting a licensing problem

If you believe a component is misattributed or a required notice is missing,
open an issue. Licensing corrections are treated as release blockers.
