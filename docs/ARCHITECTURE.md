# Application Architecture

Phase 1 separates long-running operations from frontend widgets while retaining
the tested legacy engine and its JSON cache and journal formats. Phase 2 adds
the primary PySide6 shell and reusable design system on top of this boundary.
Phase 3 wires the duplicate review workflow to that boundary and adds
manifest-based quarantine and restore services. Phase 4 replaces the legacy
sync screen with a guided copy-only import workflow. Phase 5 adds the overview,
activity, settings, diagnostics, and recovery surfaces that make those
operations manageable after they run. Phases 6 and 7 harden diagnostics,
update verification, release packaging, signing, and crash handling.

## Dependency direction

```text
Frontend
  -> Qt controllers
    -> framework-neutral services
      -> existing discovery, engine, ADB, cache, and safety modules
```

Core contracts and services never import a frontend framework. Controllers are
the only application layer below the UI that imports Qt. The PySide6 shell is
the primary frontend. The temporary Tkinter compatibility frontend invokes the
same services from Qt-owned workers and schedules all widget updates on Tk’s
main thread.

## Design system

`duplicate_transfer_manager.ui` owns semantic color tokens, spacing, radius,
typography, icons, reusable controls, pages, and the responsive shell. Pages do
not define independent themes. The shell supports Windows system, light, and
dark appearance preferences, collapses navigation at narrow logical widths, and
uses scrollable content with a maximum readable width on large displays.

## Core contracts

- `OperationEvent` carries phase, state, progress, byte and item counts, current
  item, rate, ETA, severity, message, timestamp, and structured details.
- `OperationResult` carries terminal status, counts, duration, warnings,
  failures, report path, resume information, and operation-specific data.
- `CancellationToken` provides cooperative cancellation and remains compatible
  with engine functions expecting `threading.Event.is_set()`.
- `StructuredError` separates safe user-facing guidance from technical details
  retained by the activity log.
- `AppSettings` contains appearance, simple/advanced mode, transfer defaults,
  diagnostic consent, update channel, and Android preferences.
- `QuarantineRecord` describes the original and stored paths, content hash,
  size, reason, operation ID, and timestamp.

## Operation lifecycle

Controllers permit one operation at a time and expose Qt signals for progress,
state changes, recoverable errors, completion, cancellation, failure, technical
logs, and busy-state changes.

Supported states are:

```text
idle -> validating -> scanning/comparing/transferring
                    -> reconnecting/paused
                    -> cancelling
                    -> completed/cancelled/failed
```

`DuplicateScanController` and `TransferController` execute the primary
operations. Device, report, quarantine, settings, diagnostics, and update
controllers use the same worker and error boundary.

## Duplicate quarantine workflow

The duplicate page runs scanning through `DuplicateScanController`; scanning
and initial review are read-only. Engine `FileInfo` groups are converted into
UI-ready `DuplicateReview` data with file metadata, optional local thumbnails,
default keep selection, and recoverable-size estimates. Confirmed duplicates
are handled by `DuplicateQuarantineService`:

- local duplicates are moved into an operation folder under app quarantine;
- Android duplicates are pulled into quarantine while phone originals remain
  untouched;
- every operation writes `manifest.json` with records, failures, and safety
  metadata;
- local records can be restored individually or by operation using
  rename/skip/replace conflict policies.

## Guided import workflow

The Import Files page builds `TransferSettings` through reusable import
workflow helpers, then runs the existing transfer engine through
`TransferController`. The UI captures all widget values before starting work,
keeps the default transfer copy-only and structure-preserving, and places hash
mode, workers, retries, conflict policy, caches, reconnect timing, and Android
keep-awake behavior inside Advanced options.

Reliable, Balanced, and Fast profiles map to concrete hash, retry, and worker
defaults while remaining overridable. Progress events are classified into
discovery, comparison, copying, verification, reconnecting, and report
generation stages. Completion is presented as summary cards backed by
`OperationResult` counts and engine result data; detailed logs remain available
in an expandable activity panel.

## Overview, activity, settings, and recovery

Phase 5 persists local operation records beside existing transfer reports.
`OperationRecordService` writes compact JSON summaries for duplicate scans,
quarantine actions, imports, cancellations, and failures, then merges those
records with legacy transfer report files for the Activity page.

`DashboardService` builds the Overview page model from operation records,
connected Android devices, interrupted or resumable work, quarantine records,
and app-owned storage sizes. It also owns local cache cleanup for Settings.

`ReportService` keeps report access constrained to the application reports
folder and supports local open, export, and removal actions. The Quarantine page
continues to use manifest-backed records, adding search, status filtering,
operation grouping, size summaries, and restore controls.

`SettingsService` persists the Phase 5 settings contract: theme, Simple or
Advanced mode, default categories, default transfer profile, cache retention,
Android defaults, diagnostics consent, update channel, and first-run onboarding
completion. Simple mode is the default; Advanced mode reveals technical controls
without changing backend behavior by itself.

Diagnostics include the pinned Android Platform Tools release metadata and
explicitly report that Duplicate & Transfer Manager does not mutate system-wide
ADB installations or environment variables.

## Hybrid Sort Files workflow

The Sort Files route is implemented as separate framework-neutral modules for
profile persistence, discovery/metadata, association evaluation, optional local
ML, planning, journaled execution, monitoring, and migration. Deterministic
user associations always win over ML. Same-priority ambiguity, conflicts, and
medium/low-confidence suggestions enter Review.

`SortController` owns the Qt worker boundary. The live executor processes only
explicitly approved rows, verifies every output, saves overwrite backups and a
transaction journal, and supports pause/resume/cancel/retry plus undo where the
action permits. Legacy organizer presets and manifests are imported
non-destructively. See
[SORT_FILES_ARCHITECTURE.md](SORT_FILES_ARCHITECTURE.md) for the complete
pipeline, contracts, automation behavior, and safety invariants.

## Reliability, Privacy, And Updates

`duplicate_transfer_manager.core.security` owns recursive payload sanitization,
local correlation identifiers, canonical manifest serialization, and RSA-SHA256
signature verification. Diagnostics and local crash reports use this sanitizer
so filenames, local paths, hashes, and device serials are replaced before any
report can be shown, copied, or prepared for opt-in submission.

`CrashReportService` writes sanitized crash reports under the app log directory.
The PySide6 bootstrap installs a local exception hook that displays the
sanitized report and offers a copy action; Sentry remains disabled unless the
user explicitly opts in and production transport is configured.

`UpdateService` verifies signed update manifests before accepting an update. It
checks required fields, channel, downgrade protection, minimum supported
version, RSA-SHA256 manifest signature, downloaded installer size, SHA-256
checksum, and Windows Authenticode status when running on Windows. Invalid
signatures, bad checksums, and non-newer versions are rejected.

## Packaging And Signing

Release packaging uses a PyInstaller windowed build collected by
`packaging/duplicate_transfer_manager.spec`, then a per-user Inno Setup
installer from `packaging/installer.iss`. The GitHub Actions release workflow
signs the PyInstaller executable before installer creation, signs the installer,
verifies Authenticode, generates a signed update manifest, and publishes draft
release artifacts. Protected secrets provide the Authenticode certificate and
update-manifest private key; only the public verification key is committed.

## Threading rules

- Widget values must be captured before work starts.
- Scanning, hashing, ADB commands, transfers, and file-backed controller tasks
  execute through `QThreadPool`.
- Workers emit data objects only; they never read or mutate widgets.
- Qt automatically queues controller signal delivery to the controller’s main
  thread.
- The legacy Tk adapter uses `after()` for every worker-originated widget
  update.

## Compatibility

Phase 1 intentionally preserves:

- Runtime hash-cache JSON
- Drive and ADB cache JSON version 1
- Transfer journal JSON version 1
- Existing transfer report JSON
- Root engine APIs and their legacy callback signatures

The services adapt those callbacks into structured events so later phases can
replace the frontend without rewriting the transfer engine.
