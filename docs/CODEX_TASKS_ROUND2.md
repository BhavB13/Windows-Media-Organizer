# Codex task specification — round 2

Author: Claude. Written after a whole-program sweep plus the Disk Audit review.
Baseline commit: `59b01fa`. Four commits are unpushed; `origin/main` is at `23326ec`.

Everything here was verified against the working tree at `59b01fa`. Line numbers
are from that commit. **Re-check them before editing** — do not trust this
document over the source.

## Ground rules for every task

1. **Reproduce before fixing.** Write the regression test first, run it against
   the current code, and confirm it fails. A test that passes before your change
   proves nothing. Say so in the commit message when a test is a *guard* (it
   passes before and after, and exists to stop a future regression) rather than
   a regression test.
2. **Do not break the architecture invariants**: the engine stays
   framework-neutral (no Qt in `core/`, `services/`, `sorting/`, or the root
   engine modules); duplicates are quarantined, never deleted; all scanning,
   hashing, ADB and transfer work stays off the Qt main thread; existing JSON
   formats (caches, journals, manifests, profiles, audit snapshots) must keep
   loading.
3. **Full suite green before each commit**: `.\.venv\Scripts\python.exe -m unittest discover -s tests -t .`
   Baseline is **297 tests, 1 skipped**. Also run `scripts/smoke_ui.py`.
4. **One task per commit.** Explain what was wrong and why the fix is right, not
   what you typed.
5. If you find that a claim below is **wrong**, stop and say so rather than
   implementing around it. I would rather be corrected than have you build on a
   bad premise.

---

## Task 1 — Remove the legacy Tkinter frontend (highest priority)

### What is wrong

`README.md:115-118` documents `python legacy_main.py` as a supported way to
launch the product. That frontend moves the user's duplicate originals inside
the scan loop:

```python
# ui_components.py:633
shutil.move(dup.path, target)
```

`grep -c "QuarantineRecord\|manifest\|OperationRecordService" ui_components.py`
returns **0**. There is no manifest, no quarantine record, no activity record —
only a `MOVED:` line in a session log. `DuplicateQuarantineService.list_records`
reads only `paths.quarantine/*/manifest.json`, so these files are invisible to
the Quarantine page and cannot be restored from inside the app.

The only gate is a yes/no dialog shown *before* the scan starts
(`ui_components.py:495-502`), which cannot list files that have not been found
yet.

### Why it matters

`docs/USER_GUIDE.md:9-12` states the product's two invariants — "**Nothing is
deleted.** Duplicates go into quarantine and can be brought back" and "**Nothing
runs without review.**" `README.md:188-190` repeats the promise. The documented
legacy path honours neither. A user who follows the README can move thousands of
originals into a flat folder with no way back inside the product.

Every other file-losing path in this codebase has been closed. This is the last
one, and it is the one we tell people to use.

### What to do

**Decision already taken: delete it.** The alternative — routing the legacy
isolate step through `DuplicateQuarantineService` — means maintaining a second
frontend that nothing uses. Deletion is reversible through git if that turns out
to be wrong.

Remove:

- The README section at `README.md:115-118` (the sentence and the fenced block).
- `"legacy_main"` (`pyproject.toml:55`) and `"ui_components"`
  (`pyproject.toml:59`) from `[tool.setuptools] py-modules`.
- `"tkinterdnd2==0.4.3"` (`pyproject.toml:24`) from `dependencies` — **first
  confirm** nothing else imports it (`grep -rn "tkinterdnd2\|import tkinter"`).
- The files `legacy_main.py` and `ui_components.py`.

Then check for fallout: `grep -rn "legacy_main\|ui_components"` across the repo
including `packaging/`, `scripts/`, `docs/`, and `tests/`. `docs/PHASE_6_7_AUDIT.md:35`
names both in a `compileall` line — update it. Check
`packaging/duplicate_transfer_manager.spec` for hidden imports.

Note in `CHANGELOG.md` that the legacy interface is gone and why.

### Tests

- A packaging test asserting `legacy_main` and `ui_components` are absent from
  `pyproject.toml` py-modules and that `tkinterdnd2` is absent from
  dependencies. Run it before the change to confirm it fails.
- Confirm `scripts/smoke_ui.py` still passes and the full suite is green.

### Do not

- Do not remove `DuplicateQuarantineService` or anything the PySide6 app uses.
- Do not delete `main.py`.

---

## Task 2 — Stop the app and PRIVACY.md describing an update feature that does not exist

### What is wrong

Three separate things, one root cause:

1. `UpdateController` (`src/duplicate_transfer_manager/controllers/support.py:168`)
   is defined and tested but **never instantiated anywhere in `ui/`**.
   `MainWindow.launch_verified_update` (`ui/shell.py`) exists but has no caller.
2. `SettingsPage` offers "Check for updates automatically" and an update-channel
   dropdown. Both are saved to `settings.json`. **Nothing reads either back** —
   `grep -rn "check_updates_automatically\|update_channel" --include=*.py src/`
   returns only `core/contracts.py` (the field definitions) and `ui/pages.py`
   (the widgets writing them).
3. `PRIVACY.md:13-14` states: *"If 'Check for updates automatically' is enabled,
   the app fetches a signed update manifest no more than once per day, and
   downloads an installer…"* — **this does not happen.** `UpdateService` only
   *verifies* a manifest it is handed; no code fetches one, and there is no
   configured release endpoint.

A privacy document making a false factual claim about network behaviour is the
serious part. The dead checkbox is merely embarrassing.

### What to do

There is no release endpoint and no fetch implementation, so wiring a real
update check is not possible in this change. Make the product honest instead:

- Remove the "Check for updates automatically" checkbox and the update-channel
  dropdown from `SettingsPage`, along with the corresponding writes.
- Keep `check_updates_automatically` and `update_channel` in `AppSettings` so
  existing `settings.json` files still load — **do not** remove the fields.
  `AppSettings.from_dict` filters unknown keys, so removing them would silently
  discard user data on load.
- In their place, show the real state from `UpdateService.status()`: current
  version, whether an update source is configured, and last check if any. If
  `status()["configured"]` is false, say plainly that automatic updates are not
  available in this build.
- `UpdateService.status()` hardcodes `"channel": "stable"`. Either make it
  reflect the setting or drop the key; do not report a channel the user cannot
  choose.
- Rewrite `PRIVACY.md:13-14` to describe what the app actually does: it makes no
  network requests. Check the rest of `PRIVACY.md` and `SECURITY.md` for the
  same claim appearing elsewhere.
- Keep `UpdateService`, `UpdateController` and `launch_verified_update`. They
  are the verified-install path for when a release channel exists, and they are
  tested. Add a one-line comment in `UpdateController` saying it is intentionally
  not yet wired and why.

### Tests

- A test asserting an `AppSettings` round-trip preserves
  `check_updates_automatically` and `update_channel` after the widgets are gone.
- A test asserting `PRIVACY.md` does not claim the app fetches anything. A simple
  substring assertion is fine; the point is that the next person who adds a
  network call has to update the document.

---

## Task 3 — Wire `DeviceController`, delete the controllers nothing will use

### What is wrong

Thirteen controller classes are defined; **four** are instantiated in `ui/`
(`DiskAuditController`, `DuplicateScanController`, `SortController`,
`TransferController`). The rest are dead.

Meanwhile ADB enumeration runs as a blocking subprocess on the Qt main thread at
four call sites:

- `ui/pages.py:730` — `ADBBridge.get_directory_structure(...)`
- `ui/pages.py:779` — `ADBBridge.list_devices()`
- `ui/pages.py:1585` — `ADBBridge.get_directory_structure(...)`
- `ui/pages.py:1633` — `ADBBridge.list_devices()`

`DeviceController` (`controllers/support.py:58`) exists to run exactly this off
the main thread, and is never used. On a slow or unauthorised device these calls
block the window for seconds.

### What to do

Two halves.

**a) Wire `DeviceController`** into `DuplicatesPage` and `ImportPage` so device
refresh and remote folder browsing run on the worker. Follow the pattern
`DiskAuditPage` uses — it is the cleanest example in the tree. Keep the existing
error messages ("Select an authorized Android device before browsing phone
folders.") so behaviour is unchanged when no device is attached.

Note: this cannot be fully verified without a real Android device. Test what is
testable — that the call goes through the controller and not inline — and say
plainly in the commit message that live-device behaviour is unverified.

**b) Delete the controllers that nothing will use**, after confirming each has
no caller: `FileOrganizerController`, `ReportsController`, `QuarantineController`,
`SettingsController`, `DiagnosticsController`. Keep `DataController` if the
retained controllers still inherit from it, and keep `UpdateController` (task 2).

The reason for deleting rather than wiring: quarantine, restore, sort undo/retry
and the monitor poll were already moved off the main thread through
`BasePage.run_in_background()`. Those controllers would be a second way to do
something that already works, and two patterns for one job is how this layer got
confusing. Remove their tests along with them.

### Do not

- Do not change `BasePage.run_in_background()` or the pages already using it.
- Do not delete `BaseOperationController`.

---

## Task 4 — Audit the local import/transfer path

### What is wrong

Every other subsystem has had a defect sweep with adversarial verification. The
import/transfer path has not — the agent assigned to it died twice, once on a
connection loss and once on a session limit. It is the subsystem where the
drive-cache and staging defects lived, so it is the likeliest place for
something still hiding.

### What to do

This is an **audit, not a build**. Read the source and report; fix only what you
can prove.

Read at minimum: `engine.py` (`execute_smart_transfer` and every helper),
`transfer_safety.py`, `discovery.py`, `drive_cache.py`,
`services/transfer_service.py`, `services/import_workflow.py`, and `ImportPage`
in `ui/pages.py`.

Trace these journeys concretely and say where each goes wrong:

1. Import from a local folder into an existing library. Some files are new, some
   are byte-identical to files already in the library, one has a name that
   already exists at the destination with different content, one is unreadable.
   The run finishes. Is every number the user sees true?
2. Cancel a large import halfway, then resume it later.
3. Run the same import a second time with nothing changed.

Report only: crashes/hangs; wrong results the user would act on; controls that
provably do nothing. Not style, not naming, not pure performance.

**Already fixed — do not report these again:** conflict skips counted as
duplicates; the drive-cache count-based fast path; missing `try/finally` in
`execute_smart_transfer`; staged copies leaking on conflict skip; the preflight
blocking resumes; the import banner claiming success when files failed.

For each finding give file and line, what the code actually does (quote it), a
concrete failure scenario, and a suggested fix. Then fix the ones you are
confident in, each with a regression test checked against pre-fix code.

---

## Two product decisions — do not act without the owner

Raise these; do not implement either unless Bhavesh says so.

1. **"Quick" audit takes 187 seconds** on a 510 GB drive (measured). It is on a
   worker with progress and cancel, so it is not a freeze, but the name promises
   seconds. Options: rename the modes, show an estimate before starting, or cap
   the measurement.
2. **Disk Audit took `Alt+2`**, shifting every later shortcut down one (Find
   Duplicates is now `Alt+3`, Help `Alt+9`). Nothing is released, so no muscle
   memory exists to break, but preserving the original numbering and giving Disk
   Audit the next unused slot is the safer default.

---

## Verification I will perform on your work

- Re-run the full suite and `scripts/smoke_ui.py`.
- Re-run my offscreen probe, which navigates every route and clicks every
  enabled control, and confirm zero exceptions.
- Check each regression test against the pre-fix commit to confirm it actually
  fails there.
- Spot-check that the claims in your commit messages match the source.
