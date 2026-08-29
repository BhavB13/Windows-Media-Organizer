# Disk Audit V1 acceptance record

This record maps the V1 specification to implementation and verification evidence.
It is intentionally separate from snapshots: audit snapshots are user data, while
this file documents product acceptance.

## Implemented surface

- `DiskAuditPage` is the second native route, with `Alt+1` through `Alt+9`
  navigation, fixed/removable drive selection, Quick/Deep modes, cancellable
  progress, capacity and comparison summaries, a protect-first warning, four
  searchable/collapsible tiers, responsive finding cards, safe path/clipboard
  actions, and method disclosure.
- Overview, Activity, and Help expose audit health, native saved-result reopening,
  terminal audit records, tier guidance, and the read-only contract.
- `DiskAuditService`, `AuditSnapshotStore`, and `DiskAuditController` preserve the
  framework boundary and Qt worker-thread model.
- Successful snapshots contain surfaced findings only. They are versioned,
  immutable, retained ten per stable volume identity, and separate from review,
  hidden, and user-declared backup metadata.

## Automated evidence

`tests/test_disk_audit.py` covers:

- Quick and Deep discovery; all four tiers; catalog thresholds and Deep defaults;
  separate top-50 file/folder caps; overlap and tier precedence.
- Stable IDs across mount-root changes; matching-mode comparisons; authoritative
  free-space deltas; missing and unavailable findings; fixture add/remove deltas.
- Snapshot schema checks, immutability, pruning, review-state persistence, and
  backward-compatible settings defaults.
- Permission failures, disappearing entries, inaccessible drives, Unicode/long
  paths, cancellation without a baseline, registry records without locations, and
  reparse-point behavior when the OS permits creation of the fixture.
- No source-file mutation and app-owned metadata paths.
- Off-main-thread service execution, main-thread signal delivery, concurrent-run
  rejection, and responsive cancellation.
- Drive/mode persistence, progress terminal state, four-tier rendering, search,
  hidden/review/backup controls, responsive four-column-to-stack reflow,
  clipboard commands/paths, native Activity reopening, dashboard health, mocked
  Explorer behavior, and completed/cancelled/failed Activity records.

The existing UI suite covers the native route, exact `Alt+1` through `Alt+9`
shortcuts, keyboard focus/accessibility, light/dark/system themes, and common
window-size reflow. Runtime-path tests cover creation of the app-owned `audits`
directory.

Current full-suite result: **292 tests passed, 1 skipped**. The skipped test is
the reparse-point fixture when this Windows account cannot create a symlink.

## Live Windows 11 evidence

- A read-only Quick audit of the system drive completed with findings in all four
  tiers and continued through inaccessible locations.
- A read-only Deep audit inspected 3,570,000 filesystem entries and completed with
  76 surfaced findings and 184 inaccessible-location warnings. It did not require
  elevation or mutate user/system files; its temporary snapshot was discarded.
- A second Deep audit ran end-to-end through the visible native page after the
  final catalog changes. The UI remained observable and responsive during the
  component-store measurement, kept Cancel actionable, transitioned into results,
  and completed with 83 findings and 184 inaccessible locations. Its temporary
  settings, Activity record, review state, and snapshot were removed when the
  acceptance window closed.
- The PySide6 shell smoke test and an offscreen rendered Disk Audit result page
  completed successfully.
- The actual product path helper was checked against live File Explorer. Folder
  findings opened directly. The check exposed and corrected an Explorer switch
  argument issue; the corrected helper opened `disk-audit.html` selected, with
  Explorer reporting one selected item and enabling its selection actions.

## External manual matrix still required

The current machine is Windows 11 and exposes no removable local drive. Before
release sign-off, perform the checklist in `TARGETED_FIX_MANUAL_CHECKLIST.md` on:

1. Windows 10 with a system drive and removable drive.
2. Windows 11 with a removable drive.

For each environment, confirm live responsiveness, Explorer folder/file behavior,
a disposable-fixture rescan delta, warning-only inaccessible paths, and absence of
cleanup, deletion, uninstall, elevation, or command execution. The local automated
reparse test is skipped when Windows denies symlink creation; the live Windows 11
Deep scan supplies additional real-filesystem coverage but does not replace that
fixture on every release environment.
