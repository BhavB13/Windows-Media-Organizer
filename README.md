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
- **Find similar photos** (sidebar: *Similar Photos*). Groups photos that look
  alike even when they share no bytes — a resized, recompressed or re-exported
  copy, including a rotated one — using perceptual hashes computed in pure
  Python. Strict, balanced and loose sensitivity. Each group shows the photos
  side by side with the recommended keep marked and the reasons written out:
  highest pixel count first, then larger file at equal pixels, then capture
  metadata, then the earliest timestamp. Byte-identical copies are labelled as
  such. Nothing is selected by default; chosen photos go to the same reversible
  quarantine, and a selection that would remove every copy of a photo is
  refused.
- **Browse and search a library catalog** (sidebar: *Library*, Alt+0). An
  incremental SQLite index keyed on the volume and the path relative to its
  root, re-reading a file only when its size or nanosecond mtime changed;
  search by kind, name, size and capture date; storage analytics by kind,
  volume, capture year and extension. An unplugged drive's entries stay and are
  shown as offline. The catalog is additive: the existing JSON caches remain the
  transfer engine's source of truth.
- **Warnings before downloading cloud-only files.** Before a duplicate scan,
  similar-photo comparison or import reads a folder, the app checks file
  attributes for OneDrive, Dropbox, Google Drive and iCloud placeholders, whose
  bytes are fetched the moment a file is read. If any would download, it says
  how many, how large and from where, and asks; Cancel is the default. The check
  never opens a file.
- **Sort by camera or phone.** Destination templates accept `{camera}`,
  `{camera_make}` and `{camera_model}` from EXIF alongside the existing date and
  type tokens, made safe as Windows folder names.
- **Capture dates for HEIC and RAW.** iPhone HEIC/HEIF, AVIF, TIFF-based RAW
  (DNG, CR2, NEF, ARW, ORF, RW2, PEF and others) and Fujifilm RAF are dated and
  attributed from their own EXIF, read without extra libraries, instead of by
  the day they were copied. Canon CR3 is not yet read.
- **Faster repeat comparisons.** Similar Photos remembers each photo's
  perceptual hash until the file changes, so scanning the same library again
  decodes only new or edited photos.
- **Clearer phone connection messages.** Each `adb` device state (unauthorized,
  offline, recovery, bootloader, no permissions and others) gets its own
  instruction instead of one generic "authorize USB debugging".
- **Upright, cached previews.** Duplicate and similar-photo previews come from a
  persistent thumbnail cache that honours EXIF orientation, decodes JPEGs at
  reduced size, keys entries on path, size and mtime so edited photos refresh,
  and is held to a byte budget.
- **Live sort destination preview.** When editing a rule's destination template
  in Sort Files, the dialog displays a live example path rendered from sample
  photo metadata as you type. Malformed braces or unknown tokens show an inline
  error and disable saving until corrected.
- **Quarantine reason column.** The Quarantine table and details panel show a
  Reason column indicating whether each item was quarantined as a "Duplicate" or
  a "Similar photo" (records created before this field existed display as
  "Duplicate").

### External drives

- Hash caches are keyed to the volume's GUID or serial and the path relative to
  its mount point, not to the drive letter, so a drive that comes back as `F:`
  instead of `E:` keeps its cache, and a cache is never matched against a
  different drive that was given the same letter. An offline volume's cache is
  never marked stale.
- Copying to a FAT32 drive skips files of 4 GB or more up front, with a reason
  in the report, instead of failing after writing most of a long video; they are
  also left out of the free-space estimate.
- If the destination drive is unplugged mid-transfer, the run stops and reports
  the disconnect instead of failing every remaining file. Running again after
  reconnecting resumes from the journal.
- A source folder inside the compare or output folder is refused, as are
  overlaps hidden by a junction, symlink or `subst` drive. Previously a source
  inside the library made every new photo match itself and report as already
  imported.

## Android apps

The native Android project in [`android/`](android/README.md) has two modules,
both independent of the Windows app.

- `:app` — the USB companion. Copies files from a selected phone folder to a
  USB drive mounted by Android, skipping identical content and retaining phone
  originals.
- `:organizer` — a Kotlin and Jetpack Compose organizer. Browses photos and
  videos through MediaStore, filters by kind, name, size and date, finds exact
  duplicates (size first, then SHA-256), recommends which copy to keep, and
  summarises storage. It is read-only with respect to your media: it never
  deletes, moves or writes a file. It handles Android 14 partial photo access
  as its own state.

Build both debug APKs and run the organizer's JVM unit tests:

```powershell
cd android
.\gradlew.bat :organizer:testDebugUnitTest :organizer:assembleDebug :app:assembleDebug
```

See the Android README for supported storage providers and the physical-device
verification checklist. The organizer has not yet been run on a device.

## Requirements

- 64-bit Windows 10 or Windows 11
- Python 3.12
- Android Platform Tools available on `PATH` when using Android features during
  development

Windows packaging bundles the license-compatible Android Platform Tools. Google
publishes only the latest release at a stable URL, so the build requires a
minimum version rather than an exact one and records what it actually shipped;
diagnostics report that recorded version. Development runs may still use the installed `adb` command, but the
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

## Windows release build

Release packaging requires Windows, Python 3.12, and Inno Setup 6
(`winget install --id JRSoftware.InnoSetup -e`). Signing additionally requires
Windows SDK `signtool`, a code-signing certificate, and a protected
update-manifest private key.

Without a certificate you can still produce a real, installable download:

```powershell
.\scripts\build_release.ps1 -Version 0.8.0 -Channel stable -SkipSigning
```

That writes a per-user installer, a portable ZIP, and an update manifest into
`dist\installer`. Unsigned builds raise a Windows SmartScreen warning on first
run, so publish them as previews rather than stable releases. See
[docs/RELEASING.md](docs/RELEASING.md) for the full process, the prerequisites,
and the checklist before calling a build stable.

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

The backend modules do not import PySide6 or UI widgets. Frontends
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
