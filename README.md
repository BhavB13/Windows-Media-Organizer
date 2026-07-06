# Duplicate & Transfer Manager

Duplicate & Transfer Manager is a Windows desktop utility for finding duplicate
files and safely importing new media from local folders, removable drives, and
Android devices connected through ADB.

The current interface is the stabilized legacy frontend. Its operations now use
the framework-neutral service and Qt controller architecture described in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The complete PySide6 frontend
overhaul is tracked in [OVERHAUL_PLAN.md](OVERHAUL_PLAN.md).

## Current capabilities

- Recursively discover local or Android files.
- Confirm duplicates by content hash.
- Review and optionally isolate duplicate files.
- Compare a source against an existing library and copy only new files.
- Preserve source directory structure.
- Resume interrupted transfers with journals.
- Use reusable local and Android hash caches.
- Verify Android transfers and produce JSON reports.
- Run copy-only dry runs before making changes.

## Requirements

- 64-bit Windows 10 or Windows 11
- Python 3.12
- Android Platform Tools available on `PATH` when using Android features

Phase 7 will bundle Android Platform Tools in the distributed application. The
development version still uses the installed `adb` command.

## Developer installation

Open PowerShell in the repository:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Run the application:

```powershell
duplicate-transfer-manager
```

It can also be launched from the repository with:

```powershell
python main.py
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

Runtime caches, reports, journals, logs, quarantine data, and updates are stored
under:

```text
%LOCALAPPDATA%\DuplicateTransferManager
```

On the first launch after upgrading, compatible legacy runtime files beside the
source code are copied into the new data location. Original files are retained.

## Command-line cache builder

```powershell
dtm-build-cache --root "D:\Pictures" --media-only
```

Use `dtm-build-cache --help` for Android and custom-cache options.

## Project structure

- `src/duplicate_transfer_manager/core/` — framework-neutral contracts,
  cancellation, reporting, and structured errors
- `src/duplicate_transfer_manager/services/` — UI-independent operation and
  support services
- `src/duplicate_transfer_manager/controllers/` — Qt worker and signal
  controllers
- `tests/` — automated tests
- `assets/` — icons and visual assets
- `packaging/` — installer and release configuration
- `scripts/` — development and maintenance scripts
- Root Python modules — stabilized legacy engine and frontend, retained until
  the later package migration is complete

The backend modules do not import Tkinter, PySide6, or UI widgets. Frontends
communicate through `OperationEvent`, `OperationResult`, and controller signals.

## Safety

Smart transfers are copy-only: source files are not modified or deleted.
Duplicate isolation can move local files only after confirmation. Always review
a dry run before processing important libraries and keep an independent backup.

## License

Licensed under the [MIT License](LICENSE).
