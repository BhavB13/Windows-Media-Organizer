# Codex task specification — round 2

Author: Claude. Written after a whole-program sweep plus the Disk Audit review,
then **corrected against Codex's read-only audit** — see "Corrections" below.
Baseline commit: `8aff9ce`. Five commits are unpushed; `origin/main` is at `23326ec`.

Line numbers are from `8aff9ce`. **Re-check them before editing** — do not trust
this document over the source.

## Corrections applied after review

Codex checked this spec against the source and found four material errors in the
first draft. All are fixed below; recording them so the reasoning is auditable:

- **Task 2 said "no fetch implementation at all". That was wrong.**
  `UpdateService.check_manifest_url()` calls `urlopen` at
  `support_services.py:873` and `download_installer()` at `:1012`. The feature is
  built; it is simply unreachable and unconfigured. This changes the fix.
- **Task 2 missed the onboarding dialog**, which repeats the same false claim at
  `ui/pages.py:3064`.
- **Task 3 said DeviceController "exists to run exactly this".** It covers
  `list_devices` only. `DeviceService` has no folder enumeration, so two of the
  four blocking call sites need new API, not just rewiring.
- **Task 1 said files move "inside the scan loop".** They move after
  `DuplicateScanService.run()` returns, while iterating its results
  (`ui_components.py:598`). The defect stands; the wording was wrong.

Also missed: `requirements.txt:6` pins `tkinterdnd2`, and "thirteen controllers"
counted two base classes as if they were dead.

## Ground rules for every task

1. **Reproduce before fixing.** Write the regression test first, run it against
   current code, confirm it fails. A test that passes before your change proves
   nothing. If a test is a *guard* (passes before and after, exists to stop a
   future regression), say so in the commit message.
2. **Do not break the architecture invariants**: the engine stays
   framework-neutral (no Qt in `core/`, `services/`, `sorting/`, or the root
   engine modules); duplicates are quarantined, never deleted; all scanning,
   hashing, ADB and transfer work stays off the Qt main thread; existing JSON
   formats (caches, journals, manifests, profiles, audit snapshots) must keep
   loading.
3. **Full suite green before each commit**:
   `.\.venv\Scripts\python.exe -m unittest discover -s tests -t .`
   Baseline is **297 tests, 1 skipped**. Also run `scripts/smoke_ui.py`.
4. **Never mix tasks in one commit.** Within task 4, one commit per independent
   defect is right — do not bundle unrelated fixes to satisfy the rule.
5. If a claim here is wrong, stop and say so rather than implementing around it.
   You already did this once and it improved the spec.

---

## Task 1 — Remove the legacy Tkinter frontend (highest priority)

### What is wrong

`README.md:115-118` documents `python legacy_main.py` as a supported launch.
That frontend moves the user's duplicate originals at `ui_components.py:633`:

```python
shutil.move(dup.path, target)
```

The move happens after `DuplicateScanService.run()` returns
(`ui_components.py:598`), while iterating the completed result groups.
`grep -c "QuarantineRecord\|manifest\|OperationRecordService" ui_components.py`
returns **0** — no manifest, no quarantine record, no activity record, only a
`MOVED:` line in a session log. `DuplicateQuarantineService.list_records` reads
only `paths.quarantine/*/manifest.json`, so these files are invisible to the
Quarantine page and unrecoverable from inside the app.

The only gate is a yes/no dialog shown *before* the scan starts
(`ui_components.py:495-502`), which cannot list files not yet found.

### Why it matters

`docs/USER_GUIDE.md:9-12` states the product's invariants — "**Nothing is
deleted.** Duplicates go into quarantine and can be brought back" and "**Nothing
runs without review.**" `README.md:188-190` repeats it. The documented legacy
path honours neither. Every other file-losing path in this codebase is closed;
this is the last one, and it is the one we tell people to use.

### What to do

**Decision taken: delete it.** Maintaining a second frontend nothing uses costs
more than it is worth, and deletion is reversible through git.

Remove:

- `legacy_main.py` and `ui_components.py`.
- The README section at `README.md:115-118`, and the project-structure wording
  that still describes a retained frontend among the root modules.
- `"legacy_main"` (`pyproject.toml:55`) and `"ui_components"` (`:59`) from
  `[tool.setuptools] py-modules`.
- `"tkinterdnd2==0.4.3"` from `pyproject.toml:24` **and** `requirements.txt:6`.
- `THIRD_PARTY_LICENSES.md:17-20`, which lists and explains tkinterdnd2.
- The Tk adapter description in `docs/ARCHITECTURE.md:24` and its threading
  section.
- `docs/PHASE_6_7_AUDIT.md:35` names both modules in a `compileall` line.
- `docs/HANDOFF_CODEX_REVIEW.md:98` names tkinterdnd2 as a runtime dependency.

Confirmed by Codex: only those two files import `tkinter`/`tkinterdnd2`, and
there are **no references in `packaging/`, `scripts/`, or `tests/`**. The
PyInstaller spec has no hidden import for either.

**Leave historical records alone.** `docs/PROGRAM_AUDIT.md` and this document
are findings-as-written; do not rewrite history to match the new state. Update
active guidance only. `duplicate_transfer_manager.egg-info/` is generated —
regenerate, do not hand-edit.

Note the removal and its reason in `CHANGELOG.md`.

### Tests

A packaging test asserting `legacy_main` and `ui_components` are absent from
py-modules and `tkinterdnd2` absent from both dependency files. Run it before
the change to confirm it fails.

### Do not

Do not remove `DuplicateQuarantineService`, `main.py`, or anything the PySide6
app uses.

---

## Task 2 — Stop claiming an update feature that is built but unreachable

### What is actually wrong

The feature exists and does real network I/O:

- `UpdateService.check_manifest_url()` — `urlopen` at `support_services.py:873`,
  and it enforces the daily cadence.
- `UpdateService.download_installer()` — `urlopen` at `:1012`.

The defect is that **nothing reaches them**:

1. `UpdateController` (`controllers/support.py:168`) is never instantiated in
   `ui/`. `MainWindow.launch_verified_update` has no caller.
2. `check_updates_automatically` and `update_channel` are written by the UI and
   read only to repopulate the same widgets (`ui/pages.py:2805`). Nothing
   consumes them behaviourally, and no manifest endpoint is configured anywhere.
3. `status()["configured"]` only reports whether the bundled public-key file
   exists (`support_services.py:818`) — in a packaged build it will say `True`
   despite there being no endpoint. `status()` also hardcodes
   `"channel": "stable"`, ignoring the user's selection.
4. Two places promise the feature works: `SettingsPage`, and
   `FirstRunOnboardingDialog` at `ui/pages.py:3064` — *"Automatic update checks
   follow your selected channel and never change system ADB."*
5. `PRIVACY.md:13-16` states the app "fetches a signed update manifest no more
   than once per day, and downloads an installer only after you approve it."
   That would be true if the feature were wired. Today it never happens, so the
   document describes network behaviour that does not occur.

### What to do

There is no release endpoint, so wiring a live check is out of scope. Make the
product honest and leave the machinery ready:

- Split the misleading `configured` flag into two: whether the **public key** is
  present, and whether a **release endpoint** is configured. Do not present key
  presence as "updates are configured".
- Make `status()` report the selected channel or drop the key entirely. Do not
  report a channel the user cannot influence.
- In `SettingsPage`, replace the checkbox and channel dropdown with the real
  state: current version, and a plain statement that automatic updates are not
  available in this build because no release source is configured.
- Fix the onboarding copy at `ui/pages.py:3064` to match. Task 2 is incomplete
  while first launch still promises automatic checks.
- **Keep `check_updates_automatically` and `update_channel` in `AppSettings`.**
  `from_dict` filters unknown keys, so removing the fields would silently discard
  them from existing `settings.json`.
- Rewrite `PRIVACY.md:10-18` to describe what actually happens: the app makes no
  network requests. Keep the crash-diagnostics clause, which is accurate. Check
  `SECURITY.md` for the same claim — retain its description of the verification
  flow, but label it as not yet exposed or configured.
- Keep `UpdateService`, `UpdateController` and `launch_verified_update`. Add a
  comment on `UpdateController` recording that it is deliberately unwired
  pending a release endpoint.

### Tests

- An `AppSettings` round-trip preserving both fields after the widgets are gone.
- An assertion that `PRIVACY.md` does not claim the app fetches anything, so the
  next person adding a network call has to update the document.

---

## Task 3 — Wire device enumeration off the main thread; delete what nothing uses

### What is wrong

Eleven concrete controllers plus two base classes (`BaseOperationController`,
`DataController` — both active, not dead). **Four concrete controllers are
instantiated** in `ui/`: `DiskAuditController`, `DuplicateScanController`,
`SortController`, `TransferController`.

ADB enumeration runs as a blocking subprocess on the Qt main thread at four
sites, all confirmed:

- `ui/pages.py:779` and `:1633` — `ADBBridge.list_devices()`
- `ui/pages.py:730` and `:1585` — `ADBBridge.get_directory_structure(...)`

On a slow or unauthorised device these block the window for seconds.

### What to do

**a) Move all four off the main thread.** `DeviceController` already covers
`list_devices`. It has **no** remote-folder operation, and neither does
`DeviceService` (`support_services.py:212-214`), so the two
`get_directory_structure` sites need a new service method and a matching
controller method — this is new API, not just rewiring. Follow the pattern
`DiskAuditPage` uses; it is the cleanest example in the tree.

Give each page its own `DeviceController` rather than sharing one, to keep
result routing unambiguous. Preserve the existing messages ("Select an
authorized Android device before browsing phone folders.") so behaviour is
unchanged with no device attached. Decide and state how repeated refresh/browse
clicks, a source change mid-flight, and stale async results are handled.

This cannot be fully verified without a real Android device. Test what is
testable — that the call goes through the controller rather than inline — and
say plainly in the commit message that live-device behaviour is unverified.

**b) Delete the controllers with no production caller**:
`FileOrganizerController`, `ReportsController`, `QuarantineController`,
`SettingsController`, `DiagnosticsController`.

Quarantine, restore, sort undo/retry and the monitor poll already run off the
main thread via `BasePage.run_in_background()`. These controllers would be a
second way to do a solved job, and two patterns for one job is how this layer
got confusing.

Codex's coverage audit: deleting `SettingsController` uniquely removes a real
async-load test; the others lose only shallow interface coverage. If that test
covers `DataController` behaviour worth keeping, port it to a retained
controller rather than dropping it. Keep `DeviceController` (task 3a) and
`UpdateController` (task 2), both currently callerless but deliberately retained.

### Do not

Do not change `BasePage.run_in_background()` or the pages already using it. Do
not delete `BaseOperationController` or `DataController`.

---

## Task 4 — Journey-level audit of the local import/transfer path

### Framing

This subsystem *has* been audited before — `docs/PROGRAM_AUDIT.md` carries
import/transfer findings and several were fixed in `4909d27`, `8fed87d`,
`d8ad3f5` and `a4e367a`. What it has never had is a **journey-level sweep**: the
agent assigned to that died twice, once on a connection loss and once on a
session limit. Every other subsystem got one.

### What to do

**Audit, not build.** Read and report; fix only what you can prove.

Read at minimum: `engine.py` (`execute_smart_transfer` and every helper),
`transfer_safety.py`, `discovery.py`, `drive_cache.py`,
`services/transfer_service.py`, `services/import_workflow.py`, and `ImportPage`
in `ui/pages.py`.

Trace these journeys and say where each goes wrong:

1. Import from a local folder into an existing library. Some files are new, some
   byte-identical to library files, one has a name already taken at the
   destination with different content, one is unreadable. The run finishes.
   **Is every number the user sees true?** Specifically the summary cards from
   `summarize_transfer_result` (transferred / duplicates / resumed / errors /
   bytes), the Activity record status, and the counts in the written report.
2. Cancel a large import halfway, then resume it later.
3. Run the same import a second time with nothing changed.

Report only: crashes/hangs; wrong results the user would act on; controls that
provably do nothing. Not style, not naming, not pure performance.

**Already fixed — do not report again:** conflict skips counted as duplicates;
the drive-cache count-based fast path; missing `try/finally` in
`execute_smart_transfer`; staged copies leaking on conflict skip; the preflight
blocking resumes; the import banner claiming success when files failed.

For each finding: file and line, what the code actually does (quoted), a concrete
failure scenario, a suggested fix. Then fix the ones you are confident in, one
commit each, every fix with a regression test checked against pre-fix code.

If a finding is real but you are not confident in the fix, report it and leave
it. Say so rather than guessing.

---

## Two product decisions — do not act without the owner

Raise these; implement neither unless Bhavesh says so.

1. **"Quick" audit takes 187 seconds** on a 510 GB drive (measured). It runs on
   a worker with progress and cancel, so it is not a freeze, but the name
   promises seconds. Options: rename the modes, show an estimate first, or cap
   the measurement.
2. **Disk Audit took `Alt+2`**, shifting every later shortcut down one (Find
   Duplicates `Alt+3`, Help `Alt+9`). Nothing is released, so no muscle memory
   exists to break, but preserving the original numbering and giving Disk Audit
   the next unused slot is the safer default.

---

## Verification I will perform on your work

- Re-run the full suite and `scripts/smoke_ui.py`.
- Re-run my offscreen probe, which navigates every route and clicks every
  enabled control, and confirm zero exceptions.
- Check each regression test against the pre-fix commit to confirm it fails there.
- Spot-check that the claims in your commit messages match the source.
