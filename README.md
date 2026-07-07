# Duplicate & Transfer Manager

Duplicate & Transfer Manager is a Windows desktop utility for finding duplicate
files and safely importing new media from local folders, removable drives, and
Android devices connected through ADB.

The primary interface is a responsive PySide6 application shell with system,
light, and dark themes. Its operations use the framework-neutral service and Qt
controller architecture described in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The remaining workflow overhaul
is tracked in [OVERHAUL_PLAN.md](OVERHAUL_PLAN.md).

## Current capabilities

- Recursively discover local or Android files.
- Confirm duplicates by content hash.
- Review duplicate groups, choose which copy to keep, estimate recoverable
  space, and move selected duplicates into app-managed quarantine.
- Restore quarantined local files individually or by operation with
  rename/skip/replace conflict choices.
- Start from an overview dashboard with primary duplicate/import actions,
  connected Android devices, recent operations, interrupted work, and local
  storage summaries.
- Review local activity records and existing transfer reports; reports can be
  opened, exported, or removed from the app data folder.
- Search and filter quarantine records with operation grouping, recoverable-size
  summaries, and restore controls.
- Compare a source against an existing library and copy only new files.
- Use a guided import workflow with phone, folder, or drive sources; plain
  Reliable/Balanced/Fast profiles; review-before-run; live stages; and summary
  cards.
- Configure theme, Simple/Advanced mode, default categories, default transfer
  profile, cache management, Android behavior, diagnostics consent, and update
  channel.
- Complete first-run onboarding for local scans, Android authorization, privacy,
  diagnostics consent, and update behavior.
- Preserve source directory structure.
- Resume interrupted transfers with journals.
- Use reusable local and Android hash caches.
- Verify transfers, clean partial files, and produce JSON reports.
- Run copy-only dry runs before making changes.

## Requirements

- 64-bit Windows 10 or Windows 11
- Python 3.12
- Android Platform Tools available on `PATH` when using Android features during
  development

The repository pins the license-compatible Android Platform Tools release that
will be bundled by Windows packaging, and diagnostics display that pinned
version. Development runs may still use the installed `adb` command, but the
application does not alter system-wide ADB installations or environment
variables.

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

The previous Tkinter interface remains temporarily available for compatibility:

```powershell
python legacy_main.py
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
Duplicate quarantine can move local files only after scan, review, and explicit
confirmation. Android duplicates are copied into quarantine so phone originals
remain untouched. Always review results before processing important libraries
and keep an independent backup.

## License

Licensed under the [MIT License](LICENSE).
