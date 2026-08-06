# Sort Files Architecture and User Workflow

## Scope

Sort Files replaces the earlier single-purpose organizer workspace with a
profile-driven hybrid sorting system. DropIt documentation was not present in
the project workspace during implementation, so the supplied functional
requirements were treated as authoritative. The interface uses Duplicate &
Transfer Manager's existing PySide6 design system and does not reproduce
DropIt's interface.

## Module boundaries

The implementation lives under `duplicate_transfer_manager.sorting`:

- `models.py` — versioned profiles, associations, conditions, monitored
  folders, metadata, ML suggestions, plans, and results.
- `persistence.py` — atomic profile CRUD, duplicate, enable/disable,
  import/export, validation, and legacy preset migration.
- `metadata.py` — recursive/manual discovery, Pillow image metadata, and
  optional local `ffprobe` video metadata.
- `rules.py` — deterministic priority evaluation, ALL/ANY include conditions,
  exclusions, regex, numeric, date, path, name, type, and metadata matches.
- `ml.py` — optional local classifier provider, filename/history fallback,
  prior-destination signals, and correction feedback. Feedback is never used
  to silently retrain a model.
- `planner.py` — user-rule precedence, ML fallback, destination templates,
  confidence bands, six conflict policies, duplicate-operation detection,
  recursion prevention, and immutable review plans.
- `executor.py` — confirmation, free-space preflight, partial-file-safe copies,
  atomic same-volume moves, pause/resume/cancel/skip, retry, SHA-256
  verification, overwrite backups, journals, post-session resume, and undo.
- `monitor.py` — local snapshots for scheduled and filesystem-change polling.
- `scheduler.py` and `scheduled_sort.py` — profile-scoped Windows Task
  Scheduler configuration and safe background runs.
- `migration.py` — non-destructive import of legacy organizer manifests into
  Sort history. Original manifests remain available.
- `workflow.py` — the framework-neutral Scan → Metadata → Rules → ML → Plan →
  Execute orchestration boundary.
- `session.py` — the framework-neutral guided-workflow state machine. It owns
  setup → review → processing → results validity, binds a plan to its setup,
  clears stale approvals, and prevents processing before review.
- `presets.py` — beginner-friendly Pictures, Videos, Audio, Documents, and
  optional Archives definitions; custom-extension normalization; and quick
  deterministic Move/Copy profile generation.

`SortController` is the only Qt-dependent operation layer. It runs discovery,
metadata extraction, planning, copying, hashing, verification, and journaling
in the existing worker pool. `SortWorkspace` owns widgets and UI state only.

## Associations

Each association stores a name, enabled state, numeric priority, ALL/ANY match
mode, include and exclusion conditions, action, destination template, rename
template, and conflict policy. Higher priority wins. Equal-priority matches are
ambiguous and enter Review; no association is chosen silently.

Supported actions are Move, Copy, Rename, Ignore, Quarantine, and Recycle Bin.
Supported conflict policies are Skip, Rename, Overwrite, Keep Newest, Keep
Largest, and Review. Destination and rename templates may use `name`, `stem`,
`suffix`, `extension`, `media_type`, `year`, `month`, and `day`.

## ML behavior

Deterministic user associations always override ML. Unmatched or ambiguous
files may be sent to an optional local classifier provider. The provider
receives filename, extension, source path, extracted metadata, profile settings,
and prior local destinations, and returns category, destination, confidence,
and explanation.

When no trained provider is installed, the system remains fully functional:
local filename/history/media-type suggestions are marked as fallback results
and require review according to profile thresholds. Editing an ML destination
stores local feedback containing the prior suggestion and correction. It does
not retrain a model and does not move a file.

## Safety pipeline

```text
Scan
  → metadata extraction
  → deterministic association evaluation
  → optional local ML suggestion
  → conflict-aware immutable operation plan
  → dry-run preview and explicit row approval
  → live confirmation
  → journaled execution
  → SHA-256 verification
  → local Activity/history record
  → undo or retry/resume where supported
```

Live execution processes only rows checked in Review. Copy+Overwrite is treated
as destructive. Existing overwrite targets are moved into the app-owned run
directory before processing and restored on undo. Cross-volume moves use a
verified partial-copy workflow before the source is removed. Recursive targets,
self-targets, and duplicate planned destinations are non-executable. Recycle Bin
operations use the OS recycle facility and cannot be undone by this app.

## Profiles and monitoring

Profiles support create, edit, duplicate, import/export, enable/disable, and
delete. Existing organizer presets are migrated non-destructively.

Monitored folders support recursive/non-recursive scans, filesystem-change
polling, or hourly/daily/weekly Windows scheduled tasks. Dry run is the default.
Live scheduled processing requires an explicit approval stored with that
monitored folder. Files that remain ambiguous, conflicting, or below the high
confidence threshold are not automatically processed.

## Workspace

The dedicated Sort Files route follows the same single-page progressive pattern
as Import Files. The everyday path is:

1. Add files/folders or use drag-and-drop.
2. Choose one destination root.
3. Select Pictures, Videos, Audio, Documents, and optionally Archives. Add
   specific extensions such as `.psd` or `.pages` when needed.
4. Choose Move or Copy and simple existing-filename handling.
5. Click **Review sort setup**, check the proposed files and destinations, then
   click **Run sort**. Progress and results appear below the setup in place.

Quick sorting uses deterministic generated associations and excludes unselected
formats from the plan. It does not require ML, profile knowledge, rule
priorities, or templates. Profiles, the full association builder, ML, and
monitored folders remain in the collapsed **Advanced profiles, rules, ML, and
automation** section.

Changing a source, category, extension, action, profile, rule, destination, or dry-run setting
invalidates the existing plan and approvals. If settings change while a review
is being built, that worker is cancelled safely. A new review is mandatory.

## Persistence

All state stays under `%LOCALAPPDATA%\DuplicateTransferManager\sorting`:

- `profiles.json`
- `ml_feedback.json`
- `monitor_snapshots/`
- `runs/<run-id>/journal.json`
- `runs/<run-id>/replaced/`
- `quarantine/`

Expired history cleanup removes only app-owned journals and preserved overwrite
backups. It never deletes a sorted user file.
