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
- Include HEIC and common camera RAW formats in the Pictures category; files without a native preview retain metadata-based review.
- Confirm duplicates by content hash.
- Review duplicate groups, choose which copy to keep, estimate recoverable
  space, choose oldest/newest/highest-resolution keep preferences, and move selected duplicates into app-managed quarantine.
- Restore quarantined local files individually or by operation with
  rename/skip/replace conflict choices.
- Start from an overview dashboard with primary duplicate/import actions,
  connected Android devices, recent operations, interrupted work, and local
  storage summaries.
- Review local activity records and existing transfer reports; reports can be
  opened, exported, or removed from the app data folder. A separate local,
  append-only audit history remains available after activity-record cleanup.
- Search and filter quarantine records with operation grouping, recoverable-size
  summaries, and restore controls.
- Sort pictures, videos, audio, documents, archives, or selected extensions with
  a simple Import-style review flow. Advanced profiles, priority associations,
  additional actions, local ML suggestions, monitoring, verification, history,
  and undo remain available when needed.
  every live move writes a reversible 90-day manifest and can be rolled back.
- Compare a source against an existing library and copy only new files.
- Use a guided import workflow with phone, folder, or drive sources; plain
  Reliable/Balanced/Fast profiles; saved local locations; optional UTC date-folder
  organization; review-before-run; live stages; and summary
  cards.
- Schedule a daily or weekly **read-only** duplicate scan from Advanced Settings.
  Scheduled runs only record duplicate findings locally; they never quarantine,
  move, or delete files.
- Monitor folders through local change polling or profile-scoped Windows
  schedules. Automation defaults to dry run; live automation requires explicit
  approval and never processes unresolved review items.
- Configure theme, Simple/Advanced mode, default categories, default transfer
  profile, cache management, Android behavior, diagnostics consent, and update
  channel.
- Complete first-run onboarding for local scans, Android authorization, privacy,
  diagnostics consent, and update behavior.
- Keep diagnostic and crash reports sanitized with local correlation IDs instead
  of filenames, paths, hashes, or device serials.
- Verify signed update manifests, installer size, SHA-256 checksums, downgrade
  protection, and Windows Authenticode signatures before update launch.
- Preserve source directory structure.
- Resume interrupted transfers with journals. Resume trusts a file whose size
  and recorded timestamp still match and re-reads anything that changed; an
  advanced option re-verifies every resumed file by content.
- Use reusable local and Android hash caches.
- Verify transfers, produce JSON reports, and clean up leftover partial files.
  Cleanup removes only files the app can prove it owns — its own staging
  directory or paths recorded in a transfer journal — so unrelated files that
  happen to end in `.partial` are never deleted.
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

If `python` points to another bundled runtime or reports `No module named pip`,
install/register Python 3.12 first and use the Windows launcher explicitly:

```powershell
winget install --id Python.Python.3.12 -e --source winget
py -0p
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe main.py
```

The previous Tkinter interface remains temporarily available for compatibility:

```powershell
python legacy_main.py
```

## Windows release build

Release packaging requires Windows, Python 3.12, Inno Setup 6, Windows SDK
`signtool`, a code-signing certificate, and a protected update-manifest private
key.

```powershell
.\scripts\build_release.ps1 -Version 0.8.0 -Channel stable
```

The GitHub Actions workflow in `.github/workflows/release.yml` builds the
PyInstaller app, signs the executable, builds and signs the Inno installer,
generates a signed update manifest, and uploads a draft GitHub Release.

See [docs/PROGRAM_AUDIT.md](docs/PROGRAM_AUDIT.md) for the whole-program audit
findings and their implementation plan.
See [docs/PHASE_6_7_AUDIT.md](docs/PHASE_6_7_AUDIT.md) for the Phase 6-7
completion audit and the remaining external release gates.
See [docs/TARGETED_FIX_MANUAL_CHECKLIST.md](docs/TARGETED_FIX_MANUAL_CHECKLIST.md)
for Windows/device manual checks covering local scans, ADB paths, dry runs,
quarantine restore, reports, themes, and the iOS placeholder.

## Tests

```powershell
python -m unittest discover -s tests -v
python scripts\smoke_ui.py
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
- `src/duplicate_transfer_manager/sorting/` — profile persistence, metadata,
  rule/ML decisions, planning, monitoring, journaled execution, and migration
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

## Documentation

- [User guide](docs/USER_GUIDE.md) — importing from a phone, reviewing
  duplicates, restoring from quarantine, sorting, and troubleshooting
- [Changelog](CHANGELOG.md) — what changed and why
- [Privacy](PRIVACY.md) — what stays on your PC, and what sanitization does
- [Security policy](SECURITY.md) — reporting a vulnerability, how updates are
  verified, and current release gates
- [Contributing](CONTRIBUTING.md) — setup, tests, and the architecture rules
- [Third-party licenses](THIRD_PARTY_LICENSES.md) — what is redistributed
- [Architecture](docs/ARCHITECTURE.md) — engine/UI separation and threading

## License

Licensed under the [MIT License](LICENSE). Redistributed components are listed
in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
