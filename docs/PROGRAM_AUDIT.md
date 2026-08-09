# Whole-Program Audit

Six subsystem audits plus a synthesis pass, run 2026-08-08, covering the three
axes requested: **logic faults**, **execution time**, and **program purpose**.

72 findings were reported. They are recorded as written by the
auditors and, apart from the ones marked FIXED, **have not been individually
verified**. Treat them as leads. Several were found on inspection to overstate
severity, and the synthesis pass itself notes that six findings describe code
that no longer exists.

Entries marked **FIXED** were reproduced, fixed, and given a regression test
that was checked against the pre-fix commit and fails there.

## Fixed during this audit

| Finding | Commit |
| --- | --- |
| "Select recommended copies" also selected the copy being kept, so confirming quarantined every copy in the group | `fb8271e` |
| `prune_runs` deleted the only copies of files displaced by an Overwrite | `fcfb784` |
| Overwrite backup displaced the incumbent with an unverified, unjournaled cross-volume move | `cd94a07` |
| The sort journal was rewritten in full per item: measured 38.05 s / 346.9 MB for 600 files, against 6.72 s / 1.3 MB after | `dfdd0fe` |
| The bundled adb was never used; every call took whatever was on PATH | `3673358` |
| adb's pipes were not drained during a pull, so a large video could deadlock it | `40f3445` |

## Summary by severity

| Severity | Count |
| --- | --- |
| blocker | 10 |
| high | 21 |
| medium | 33 |
| low | 8 |

| Axis | Count |
| --- | --- |
| logic | 28 |
| execution-time | 26 |
| purpose | 18 |

## Synthesis and implementation plan

# Ranked Implementation Plan — Duplicate & Transfer Manager 0.8.0

**Verification note:** I read the current source, not the audit text. Six raw findings describe code that no longer exists — they were fixed in `dfdd0fe` and `cd94a07`. Those are listed in §4 and excluded from the plan. Everything below I confirmed against the working tree at the cited line.

---

## TIER 0 — Loses, corrupts, or silently skips a user's file

### 1. "Select recommended copies" checks the Keep row too — quarantine takes every copy
- **Defect:** `src/duplicate_transfer_manager/ui/pages.py:860-861` — `keep.setChecked(...)` runs *before* `keep.toggled.connect(...)`, so `_keep_item` never fires during render. `_keep_item` (`pages.py:956`) is the only code that ever calls `check.setEnabled(False)` on the keeper's quarantine checkbox. At initial render every checkbox is enabled; `_select_recommended_duplicates` (`pages.py:908-912`) checks *every enabled* box and `_selected_ids` (`pages.py:997-1002`) returns everything checked-and-enabled. The keeper goes to `quarantine()`.
- **Fix:** In `_render_next_batch`, set `check.setEnabled(item.id != group.keep_item_id)` at creation (`pages.py:866-867`). Additionally make `_selected_ids` filter against `group.keep_item_id` from `self.review` rather than trusting widget enabled-state.
- **Test:** `tests/test_phase2_ui.py`, offscreen. Build a `DuplicateReview` with one 3-item group, render it, call `_select_recommended_duplicates()`, assert `keep_item_id not in page._selected_ids()` and `len(_selected_ids()) == 2`.

### 2. Sampled "Fast" hash decides duplicate-skip on import with no full-content confirmation
- **Defect:** `engine.py:880` (`if h in dest_hashes:`) treats a sampled digest as proof. `compute_hash` in fast mode hashes only `size + first 1 MiB + last 1 MiB` above 2 MiB (`engine.py:90-92`). `build_transfer_hash_settings` (`engine.py:390-394`) upgrades to `full` **only** when `source_is_adb`. The Fast profile sets `hash_mode: "fast"` (`services/import_workflow.py:38`) and `duplicate_policy` is hardcoded `"skip"` (`import_workflow.py:131`). `group_duplicates` already has the correct pattern — a full-SHA-256 confirmation pass at `engine.py:464-515` with the comment "A sampled digest is only a candidate filter." The transfer path has no equivalent. `docs/USER_GUIDE.md:97-100` claims Fast "still reads every shortlisted file in full" — false for imports.
- **Fix:** Simplest correct change: drop the `source_is_adb` condition at `engine.py:392` so `hash_mode = "full"` for every transfer. If Fast must stay sampled for the compare-scan phase, keep a `digest -> [dest_path]` index (`drive_cache.hash_index` already exists) and re-hash source + matching destination with `sha256`/`full` before classifying as duplicate.
- **Test:** `tests/test_engine.py`. Two >2 MiB files with identical size, identical first and last 1 MiB, different middles. Run `execute_smart_transfer` with a Fast-profile settings object and `source_is_adb=False`; assert `transferred == 1` and `skipped == 0`.

### 3. `is_hidden_or_system` returns True on any attribute-read failure — subtrees vanish with no error
- **Defect:** `utils.py:118-122`. `GetFileAttributesW` has no `restype`, so `INVALID_FILE_ATTRIBUTES` arrives as `-1`; `-1 & (0x2|0x4)` is `6`, truthy. `_is_excluded_local` (`discovery.py:65-69`) then makes `scan_local_tree` `continue` at `discovery.py:236`/`242` **without appending to `errors`**, so `incomplete=bool(errors)` (`discovery.py:286`) stays False and the run reports a complete listing. For a directory this prunes the whole subtree. The bare `except:` at `utils.py:122` swallows ctypes errors into the same path.
- **Fix:** Set `GetFileAttributesW.restype = ctypes.c_uint32`, `argtypes = [ctypes.c_wchar_p]`, compare against `0xFFFFFFFF` explicitly and return `False` on the sentinel. Replace the bare `except:` with `except OSError`. Have `_is_excluded_local` return a tri-state (or raise) so `scan_local_tree` records the path in `errors`. Bonus: `discovery.py:229` already holds a `DirEntry`; `entry.stat().st_file_attributes` gives the same bits with no extra syscall.
- **Test:** `tests/test_engine.py`. Monkeypatch `utils.ctypes.windll.kernel32.GetFileAttributesW` to return `-1`; assert `is_hidden_or_system(p) is False`. Second test: force the same failure inside a `scan_local_tree` over a tmp tree and assert the file is present in `files` or in `errors`, never silently absent.

### 4. Drive-cache "count matches" fast path trusts cached hashes with no size/mtime check
- **Defect:** `engine.py:649-653` — when `cached_count == total_dest`, the whole compare phase is replaced by `drive_cache.hashes_under_root(...)`. `entries_under_root` (`drive_cache.py:150-163`) filters only on path prefix, `stale`, `algo`, `mode` — it never compares cached `size`/`mtime` against disk. The slow path *does* validate, per file, via `get_valid_hash(f.path, algo, mode, f.size, f.created)` (`engine.py:670-676`). A file count is defeated by any equal-sized add/delete pair made outside the app.
- **Fix:** Take the fast path only when every discovered path has a non-stale entry whose `size` and `mtime` match. The discovery scan already stat'd everything (`discovery.py:243-247`), so this costs nothing extra.
- **Test:** `tests/test_engine.py`. Build a `DriveHashCache` for 3 files, then replace one file on disk with different content of the same size and touch its mtime; assert the fast path is rejected and the changed file is re-hashed.

### 5. Files skipped by the filename-conflict policy are reported to the user as duplicates
- **Defect:** `engine.py:921` increments `skipped` when `resolve_conflict_path` returns `""` under `conflict_policy="skip"` — a *unique* source file that was never copied. `engine.py:906` increments the same counter for genuine content duplicates. `engine.py:1114` returns `"duplicates": isolated + skipped`, and the summary row is literally "Duplicates skipped" (`import_workflow.py:192`). Conflict skips are not appended to `failures`, so they never reach `write_transfer_report`.
- **Fix:** Add a `conflicts` counter, increment it at `engine.py:921` (and the two post-promote branches at `engine.py:956`, `engine.py:977`), return it in the result dict, and give it its own summary row ("Skipped — a different file already has this name"). Append them to `failures` so the report carries the source paths.
- **Test:** `tests/test_engine.py`. Local import of one file whose target name already exists with different content, `conflict_policy="skip"`; assert `result["duplicates"] == 0`, `result["conflicts"] == 1`, and the source path appears in the written report.

### 6. Single-read staged import leaks a full copy when the conflict policy skips
- **Defect:** `engine.py:920-923`. In the `uses_staged_import` path the file is pulled in full to `<output_root>/.duplicate_transfer_manager_staging` at `engine.py:803` *before* its hash is known. Every other exit removes it (`engine.py:868-872`, `engine.py:881-885`) — this one does not. `promote_transfer_file` handles its own skip cleanup (`engine.py:245`), so **only** the pre-copy `resolve_conflict_path` branch leaks. `os.rmdir(staging_dir)` at `engine.py:1106` then fails silently on the non-empty directory.
- **Fix:** `os.remove(staged_path)` in that branch. Better: wrap the per-file body in `try/finally` that unlinks any un-promoted `staged_path`, so a future branch cannot reintroduce it. Log what `os.rmdir` leaves behind instead of swallowing.
- **Test:** Same test as #5, with `source_is_adb=True` and a faked `pull_with_retries`; assert the staging directory is empty afterwards.

### 7. No `try/finally` in `execute_smart_transfer` — an OSError strands the phone and discards all hashing
- **Defect:** `engine.py:604` acquires `prevent_windows_sleep()`; `engine.py:757-759` calls `ADBBridge.enable_usb_stay_awake` (persistent device setting). Teardown lives at `engine.py:1074-1111` and is reached only by falling through. `build_target_path` calls `os.makedirs` at `engine.py:160/166/169` and is invoked at `engine.py:908` — **before** the per-file `try` opens at `engine.py:926`. `os.makedirs(settings.isolate_folder)` at `engine.py:890` is likewise unguarded. Any `OSError` escapes the function: no `drive_cache.save()`, no `adb_cache.save()`, no `restore_stay_awake`, no staging cleanup, no report.
- **Fix:** Wrap `engine.py:604` → the summary block in `try/finally`; move `restore_windows_sleep`, `restore_stay_awake`, `journal.save(force=True)`, both cache saves, staging cleanup, and `write_transfer_report` into the `finally`. Independently, move `build_target_path` and the isolate `makedirs` inside the existing per-file `except OSError` so one bad path costs one file.
- **Test:** `tests/test_engine.py`. Destination template `"date"` into a root that already contains a *file* named `2024`; assert `execute_smart_transfer` returns a result dict with `errors >= 1`, that `drive_cache.save` was called, and that `ADBBridge.restore_stay_awake` was called (mock).

### 8. Quarantine, restore, sort-undo and sort-retry all run on the Qt main thread
- **Defect:** Four call sites, one root cause — no worker boundary exists for these operations.
  - `pages.py:1043` → `duplicate_workflow.py:368` (`ADBBridge.pull`, a blocking Popen poll loop with a 180 s stall timeout) and `:373` (`shutil.move`).
  - `pages.py:2438`/`:2456` → `duplicate_workflow.py:423-448` (`copy2` + two full SHA-256 reads per file).
  - `sort_workspace.py:1513` → `SortExecutor.undo` (full SHA-256 per restored file at `executor.py:219` and again inside `_restore_for_undo` at `executor.py:545`, plus moves).
  - `sort_workspace.py:1528` → `retry_failed`/`resume_run`, both of which end in the full `execute()` pipeline (`executor.py:329`, `:341`).
  `SortController` (`controllers/operations.py:88-151`) exposes only preview/execute/pause/resume/skip/cancel — no undo or retry entry point exists at all. `DuplicateScanController` and `TransferController` are the only controllers instantiated anywhere in `ui/` (`pages.py:364`, `pages.py:1094`, `sort_workspace.py:397`). This violates the stated invariant directly.
- **Fix:** Add `QuarantineController(BaseOperationController)` wrapping `quarantine`/`restore_record`/`restore_operation`, and add `undo(run_id)`/`retry(run_id)` to `SortController`, all through `start_task`. Thread `CancellationToken` + `OperationReporter` into `DuplicateQuarantineService.quarantine` and `SortExecutor.undo` (which currently accepts neither) and check the token at the top of each per-item loop so a cancel ends with a flushed manifest/journal.
- **Test:** No GUI needed for the service half: call `quarantine(...)` with a token pre-cancelled after N items, assert it returns cleanly and the manifest records the completed N. For the UI half, offscreen test asserting `page.controller.busy` becomes True and `quarantine_service.quarantine` is never called from the main thread (assert it is called with a non-`None` cancellation token).

### 9. Closing the window neither cancels nor waits for in-flight workers
- **Defect:** `ui/shell.py:350-352` — `closeEvent` saves settings and calls `super()`. It never checks `controller.busy` or calls `wait_for_done` (`controllers/base.py:188-191`, which exists and has no caller). `launch_verified_update` at `shell.py:362` proves the codebase knows the check. The process lingers copying files after the window vanishes; a worker emitting into `WorkerSignals` during interpreter teardown can abort the process, leaving a stale journal checkpoint and partial destination files.
- **Fix:** In `closeEvent`, collect busy controllers from `self.pages`; if any, prompt; on confirm `cancel()` each and `wait_for_done(timeout)` before `accept()`, else `event.ignore()`.
- **Test:** Offscreen. Start a fake long task on `DuplicateScanController`, call `window.closeEvent(QCloseEvent())` with the confirm dialog patched to Yes, assert `cancel` and `wait_for_done` were both called and the event was accepted.

### 10. `resume_run` re-executes legacy runs the user already rolled back
- **Defect:** `executor.py:335` builds the terminal set from `{"completed", "ignored", "skipped", "previewed"}`. `SortingMigrationService._record` (`migration.py:88`) emits the literal `"undone"` for any legacy item with `restored_at` — not in that set. Legacy manifests are imported at startup (`ui/app.py:49`). `_retry_selected` (`sort_workspace.py:1528`) calls `resume_run` whenever the run has no `"failed"` records, which is always true for imported legacy runs, and passes `confirmed=True`, bypassing the destructive-plan gate at `executor.py:106`. The migrated items carry accurate size/mtime, so the staleness guard at `executor.py:386` passes.
- **Fix:** Invert the test — treat a record as terminal when it is not resumable: add `"undone"` and skip any record carrying `undone_at`. Additionally gate `resume_run`/`retry_failed` on `journal.get("status") != "legacy_imported"` and disable Undo/Retry for imported runs in `_refresh_history`.
- **Test:** `tests/test_sorting.py`. Write a journal with one record `status="undone"`, `undone_at` set; assert `resume_run` raises `ServiceError(VALIDATION, "no interrupted files")` rather than executing.

### 11. Monitored-folder snapshot is committed before the sort runs
- **Defect:** `sorting/monitor.py:47` calls `self._save(folder.id, current)` unconditionally, before returning the changed files. In `scheduled_sort.py:22-33` the poll is followed by `SortPlanner.build` and `SortExecutor.execute`, either of which can raise (`planner.py:139`, `executor.py:471`), and the body is caught by a blanket `except Exception` that writes one "failed" row. The change set is gone; those files are never sorted by any future run.
- **Fix:** Have `poll` return the new snapshot without persisting, and add `commit(monitor_id, snapshot)` called only after a terminal non-failed executor status. On partial success, commit only entries whose record reached a terminal status.
- **Test:** `tests/test_sorting.py`. Poll a tmp folder with 2 files, do not commit, poll again; assert both files are still reported as changed.

### 12. EXIF capture time parsed as local wall clock, formatted as UTC
- **Defect:** `sorting/metadata.py:41` — `datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").timestamp()` interprets a zoneless EXIF wall clock in the *sorting machine's* zone. `planner.py:128` converts it straight back with `datetime.fromtimestamp(captured, timezone.utc)` and feeds `stamp.year/month/day` into `{year}/{month}/{day}`. The round trip shifts the rendered date by the local UTC offset. Meanwhile the review table shows the naive local time (`duplicate_workflow.py:158`), so the folder disagrees with the date the user was shown.
- **Fix:** `strptime(...).replace(tzinfo=timezone.utc).timestamp()` so the UTC read-back returns the original digits. Use the same basis for the `metadata.modified` fallback and for the review-table labels.
- **Test:** `tests/test_sorting.py`. Monkeypatch `time.tzset`/`TZ` or freeze via a fixed offset: build metadata with `captured` from EXIF `2024:01:31 21:00`, render `{year}/{month}`, assert `2024/01` under any `TZ`.

### 13. Free-space preflight sums per destination directory, not per volume
- **Defect:** `executor.py:453-465` keys `required_by_root` on `destination.parent` walked up to its nearest *existing* ancestor, then checks each key independently at `executor.py:467-477`. Two already-existing category folders on the same drive become two keys, each compared against the whole volume's free space. It only appears correct on the very first run, when the folders don't exist yet and collapse to a shared ancestor.
- **Fix:** Key on the volume: `os.path.splitdrive(destination)[0]` / `Path(destination).anchor` on Windows, `os.stat(root).st_dev` elsewhere. One `disk_usage` call per volume, compared against the summed requirement.
- **Test:** `tests/test_sorting.py`. Two plan items of 60 units each into two existing subdirs of one tmp root; monkeypatch `shutil.disk_usage` to report 100 free; assert `_validate_space` raises.

### 14. Transfer preflight sums every source file, hard-blocking resumes
- **Defect:** `transfer_safety.py:207-211` computes `required = sum(info.size for info in source_files)` over the entire listing and appends a hard **error**. `execute_smart_transfer` returns immediately (`engine.py:591-602`). But both mechanisms that shrink the real requirement — the journal resume check (`engine.py:770-777`) and the duplicate check (`engine.py:880`) — live inside the per-file loop, which is never reached. The documented resume feature is unreachable exactly when the destination is nearly full.
- **Fix:** Pass the journal into `preflight_transfer` and exclude already-complete sources from `required`; or move the space check after resume/duplicate filtering. Keep a hard error only when the single largest source file will not fit.
- **Test:** `tests/test_engine.py`. Journal marking 90% of sources complete, destination free space below the raw sum but above the remainder; assert `preflight_failed` is absent and the transfer proceeds.

---

## TIER 1 — Program reports a false result

### 15. Duplicate scan computes an "incomplete scan" warning and throws it away
- **Defect:** `services/duplicate_service.py:89-99` builds `incomplete_warning` with a comment explaining it exists to stop users acting on a partial listing, then returns `warnings=tuple(discovery.errors)` at `:154` — the variable is dead after `:99`. The `reporter.log` outlet at `:99` also goes nowhere: `DuplicatesPage` never connects `technical_log` (the only `technical_log.connect` in the UI is `ImportPage`, `pages.py:1386`). And `DuplicateReview.warnings` is stored but rendered by no widget; the banner is an unconditional green "Scan complete" (`pages.py:788`).
- **Fix:** `warnings=(*incomplete_warning, *discovery.errors)`. Render `DuplicateReview.warnings` as an `InlineMessage` above the group table and switch the completion banner to "warning" style when non-empty.
- **Test:** `tests/test_phase3.py`. Force `discovery.incomplete=True`; assert the returned `OperationResult.warnings[0]` contains "incomplete".

### 16. A phone disconnecting mid-scan yields a duplicate report that looks complete
- **Defect:** `compute_hash` deliberately re-raises `ADBOperationError` when the device is gone (`engine.py:75-76`), but `group_duplicates` catches it in the bare `except Exception: continue` at `engine.py:460` and again at `engine.py:504`. Nothing sets a flag, nothing enters the result. `DuplicateScanService` returns `COMPLETED` with `warnings=discovery.errors` — which only ever carries *discovery* errors, never hashing errors.
- **Fix:** Catch `ADBOperationError` explicitly in both loops, break, and propagate a `device_lost` flag / failed-hash count. Refuse `COMPLETED` in `DuplicateScanService.run` when any candidate failed to hash; surface it in the results banner the way `discovery.incomplete` should be (#15).
- **Test:** `tests/test_engine.py`. Fake `compute_hash` raising `ADBOperationError(device_unavailable=True)` on the third candidate; assert the returned data carries `device_lost` and the service status is not `COMPLETED`.

### 17. Dry-run and failed quarantine records are indistinguishable from real ones
- **Defect:** `duplicate_workflow.py:366` writes `status="previewed"`, `:377` writes `status="failed"` into the manifest. `list_records()` (`:493-495`) returns every record with no filter, and `QuarantineRecord.from_dict` (`core/contracts.py:188-201`) does not carry `status` at all — the field is destroyed at the boundary. So the Quarantine page shows preview-only and failed rows as restorable and adds their sizes to "Recoverable space"; `DashboardService.summary` counts them too. Separately, a dry run still does `operation_root.mkdir(exist_ok=False)` at `:307` and writes a real manifest, contradicting the dialog text at `pages.py:1031-1033`.
- **Fix:** Add `status: str = "completed"` to `QuarantineRecord` and populate it in `from_dict`/`to_dict` (the default keeps existing manifests loading). Filter `list_records()` to `status == "completed"` by default in both `DuplicateQuarantineService` and `QuarantineService` (`support_services.py:330-352`). Write dry runs to `dry_run_manifest.json` so the `*/manifest.json` glob does not pick them up.
- **Test:** `tests/test_phase3.py`. Run `quarantine(..., dry_run=True)` over tmp files, then `list_records()`; assert it returns `[]` and the originals still exist.

### 18. A transfer with per-file errors reports COMPLETED and `successful == True`
- **Defect:** `services/transfer_service.py:96` sets `FAILED` only for `preflight_failed`/`adb_device_failed`; a run with `errors: 300` falls through to `COMPLETED` at `:99` while `:117-124` builds `OperationFailure` objects for those same errors. `OperationResult.successful` (`core/contracts.py:93-94`) is `status == COMPLETED`, so it returns True with a non-empty `failures` tuple. `resume_information["can_resume"]` is `status in {CANCELLED, FAILED}` (`:153`) — False for exactly the runs with unfinished files. `DuplicateScanService` has the same shape.
- **Fix:** Redefine `OperationResult.successful` as `self.status is OperationState.COMPLETED and not self.failures` — contained to `contracts.py`, fixes both services at once. Include the error case in the `can_resume` set.
- **Test:** `tests/test_phase1.py`. `OperationResult(status=COMPLETED, failures=(one,))` → `successful is False`.

### 19. ML prior-destination history reads the 100 *oldest* runs
- **Defect:** `sorting/workflow.py:77` — `for run in reversed(self.executor.list_runs()[-100:])`. `list_runs` sorts `reverse=True` (`executor.py:264`), i.e. newest-first, because run ids are `sort_YYYYMMDD_HHMMSS_ffffff`. `[-100:]` takes the tail of a newest-first list — the 100 oldest. `_prior_destination` then picks `matches[-1]` (`ml.py:122`), whose whole point is recency. Everything recent is discarded before the suggester sees it.
- **Fix:** `self.executor.list_runs()[:100]`, keeping `reversed()` so the newest land last for `matches[-1]`.
- **Test:** `tests/test_sorting.py`. Write 150 journals with distinct destinations; assert the destination from the newest run wins the suggestion.

### 20. "Replace existing filename" silently retains every overwritten file forever
- **Defect:** `promote_transfer_file` moves the incumbent to `<output_root>/.duplicate_transfer_manager_backups` at `engine.py:221-224`. That directory is in `DEFAULT_EXCLUDES` (`utils.py:26`) so no scan sees it; `cleanup_partial_files` (`transfer_safety.py:301-330`) only walks staging and journal `partial_path` entries; nothing in the UI lists it; and `preflight_transfer` (`transfer_safety.py:207-211`) assumes a replace consumes no net space. A 300 GB in-place refresh on a 350 GB drive passes preflight and fills the disk.
- **Fix:** Pick one and document it next to the conflict-policy control: either delete the backup after the successful `os.replace` at `engine.py:226` (what the label promises), or count the retained bytes into the result dict, add them to the preflight requirement when `conflict_policy == "replace"`, and add a "Clear replaced-file backups" action.
- **Test:** `tests/test_engine.py`. Replace-policy import over an existing target; assert either the backup root is empty (option A) or `result["replaced_backup_bytes"] > 0` and preflight's `required` includes it (option B).

### 21. Overview's "Connected devices" is hardwired to zero
- **Defect:** `pages.py:303` calls `summary(include_storage=include_storage)` and never passes `include_devices=True`; `DashboardService.summary` defaults it False (`support_services.py:585`) and populates `connected_devices` only when set (`:605-610`). The card and the storage row always read 0 / "None", including after Refresh.
- **Fix:** Either remove the card and the row, or populate it from a deferred off-thread `DeviceController` refresh (`list_devices` is a blocking subprocess and must not run in `refresh`).
- **Test:** `tests/test_phase2_ui.py`. With `list_devices` mocked to return one device, assert the metric card is non-zero after the deferred refresh — or, if removed, assert the card is absent.

### 22. Activity's "Duplicate scans" filter excludes scheduled scans
- **Defect:** `pages.py:1904` — exact match `record.get("type") != "duplicate_scan"`. Every record the scheduled scan writes is typed `"scheduled_duplicate_scan"` (`scheduled_scan.py:61`, and `:22`/`:46` for the failure paths). The "Sorting" branch three lines below (`pages.py:1908-1913`) handles its scheduled variant explicitly, so this is inconsistent within one function.
- **Fix:** Accept both via set membership.
- **Test:** `tests/test_phase2_ui.py`. Write one record of each type, select "Duplicate scans", assert both rows survive.

---

## TIER 2 — Execution time at realistic scale

### 23. Import compare-hashing is single-threaded; the "Worker count" control and Fast's 8 workers do nothing
- **Defect:** `engine.py:666-699` is a plain sequential `for` loop with one full-file read at a time. `settings.max_hash_workers` is referenced only at `engine.py:447` and `:482`, both inside `group_duplicates`, which `execute_smart_transfer` never calls. The Import page wires a "Worker count" spinbox straight to `max_hash_workers` (`pages.py:1253-1256`) and the Fast profile advertises `max_hash_workers: 8` with "Faster comparison for large local libraries" (`import_workflow.py:37-40`). Neither has any effect. **Scale:** a first import against a 150k-file / 600 GB NAS or USB library reads everything serially; the bottleneck is per-file latency and request depth, so 4-8 readers typically cut this several-fold.
- **Fix:** Hash the compare folder through `ThreadPoolExecutor(max_workers=settings.max_hash_workers)` the same way `group_duplicates` does, keeping the `stop_event` checks and the `HashCancelled` break, and collect into `dest_hashes` + the drive cache as futures complete. If parallelism is deliberately withheld, remove the spinbox and correct the profile description — right now both mislead.
- **Test:** `tests/test_engine.py`. Instrument `compute_hash` to record thread idents; assert more than one distinct ident with `max_hash_workers=4` over 20 files.

### 24. `ml_feedback.json` is re-read and re-parsed once per unmatched file
- **Defect:** `SortPlanner.build` calls `self.ml.predict(...)` inside its per-file loop (`planner.py:60`). With `ml_enabled` true and no injected classifier, `predict` reaches `_matching_feedback` (`ml.py:115-117`), which calls `self.list_feedback()` — a fresh `read_text()` + `json.loads()` + `MLFeedback` construction for every stored correction — on **every call**. Nothing is cached. Cost is O(files × corrections) in parsing plus one file-open syscall per file even when the file does not exist. `record_correction` (`ml.py:97-106`) reloads the whole list to append one entry. **Scale:** 100k files × ~500 corrections ≈ 100k opens and parses of a ~150 KB file — minutes of CPU during preview.
- **Fix:** Load once lazily, invalidate on mtime, and index as `dict[(extension, source_parent)] -> MLFeedback` so `_matching_feedback` is an O(1) lookup. `record_correction` appends to the index and rewrites.
- **Test:** `tests/test_sorting.py`. Count `Path.read_text` calls on the feedback path across a 200-file `build`; assert ≤ 1.

### 25. `list_runs()` parses every journal ever written — twice per history refresh, three times at startup, once per preview
- **Defect:** `executor.py:262-269` does `json.loads(path.read_text())` on every `runs/*/journal.json`; each journal embeds full `planned_items` plus `records`. `_refresh_history` calls it at `sort_workspace.py:1482` and then calls `_refresh_stats`, which calls it again at `:1567`. `SortWorkspace.__init__` reaches it three times (`:458` plus `_refresh_profiles → _profile_changed → _refresh_stats`), and `SortWorkspace` is constructed eagerly in `MainWindow.__init__` (`shell.py:275`) — so this is on the app startup path. `HybridSortService.prepare` calls it unconditionally via `_prior_destinations` (`workflow.py:50`) **even when `profile.ml_enabled` is False** — the Simple profile hardcodes `ml_enabled=False` (`presets.py:127`), so the whole result is parsed and discarded. Journal volume also grows unbounded: `scheduled_sort.py:28` writes a run directory on every poll even when zero files changed (~2,160 journals per 90-day window at hourly monitoring).
- **Fix:** (a) `_refresh_history` passes its already-loaded `self.runs` into `_refresh_stats`; (b) skip `_prior_destinations` entirely when `not profile.ml_enabled` and bound it to the newest N runs before opening any file; (c) add a summary mode to `list_runs` reading only header fields — cheap now that per-item history lives in `records.jsonl` (`executor.py:629-651`); (d) defer the first history load until the Sort route is navigated to; (e) `scheduled_sort` returns early when `changed` is empty.
- **Test:** `tests/test_sorting.py`. Write 20 journals, instrument `Path.read_text`; assert `_prior_destinations` opens zero files for an `ml_enabled=False` profile and that a summary `list_runs` never materializes `planned_items`.

### 26. Duplicate review decodes every image twice and writes a thumbnail, on the UI thread, with no cache
- **Defect:** `pages.py:782` calls `build_duplicate_review` synchronously from `_on_scan_completed`, *after* `self.progress_panel.hide()` at `pages.py:785` — so there is not even a spinner. For every item it calls `_dimensions` (`duplicate_workflow.py:171`, `Image.open`) and then `_thumbnail` (`:187-189`, a second `Image.open`, full decode, resize, `convert("RGB")`, JPEG write). `_thumbnail` never checks whether `target` already exists, so re-running the same scan regenerates everything. **Scale:** 2,500 duplicate 12 MP JPEGs = 5,000 decodes + 2,500 encodes + 2,500 writes on the event loop.
- **Fix:** Build the review inside the worker (`build_duplicate_review` is framework-neutral — return it in `OperationResult.data` from `DuplicateScanService.run`). Add `if target.exists(): return str(target), ""` at the top of `_thumbnail`, and take dimensions from the single `Image.open` that `_thumbnail` already performs.
- **Test:** `tests/test_phase3.py`. Call `build_duplicate_review` twice over the same tmp images with a counter on `Image.open`; assert the second call opens ≤ 1 file per item.

### 27. Quarantine manifest is fully rewritten and fsync'd once per file
- **Defect:** `duplicate_workflow.py:380` — `self._write_manifest(operation, manifest_records, failures, ...)` sits inside the per-item loop and serializes the complete record list every time (`:515-523`), through `_atomic_json_write` which does `json.dump(indent=2)` + `flush` + `os.fsync` + `os.replace` (`support_services.py:46-50`). O(n²) bytes and O(n) forced flushes, and per #8 it is all frozen-UI time. **Scale:** 5,000 items × ~2,500 records average ≈ 12.5M record serializations and 5,001 fsyncs.
- **Fix:** The pre-loop write at `:353` already establishes the recovery record. Checkpoint on a bounded cadence (every N items / few seconds) plus unconditionally on the final item and on any exception — the pattern `TransferJournal.save` already uses at `transfer_safety.py:58-59`.
- **Test:** `tests/test_phase3.py`. Count `_atomic_json_write` calls for a 200-item quarantine; assert ≤ 10 and that the final manifest lists all 200.

### 28. 30-second monitor timer walks monitored folders on the main thread and wipes the user's reviewed plan
- **Defect:** `sort_workspace.py:460-463` starts an unconditional 30 s `QTimer` wired to `_poll_change_monitors` (`:1457`), which calls `SortMonitorService.poll` directly — no worker. `poll` does `root.rglob("*")` with `stat()` + `resolve()` per entry (`monitor.py:33-46`), then `MetadataExtractor.extract` on every changed file, which opens images with Pillow and spawns a 15 s-timeout `ffprobe` per video (`metadata.py:57-73`). The first poll after a monitor is added has an empty snapshot (`monitor.py:37`), so *every* file in the tree is fully extracted in one main-thread pass. It then calls `_add_sources` (`:1470`) → `_invalidate_plan` (`:934`), which cancels an in-flight preview or sets `self.plan = None` and clears `review_table` (`:999-1006`). The timer runs on every route, not just Sort Files.
- **Fix:** Run the poll on the thread pool; only while the Sort route is visible; back the interval off or drive it from `QFileSystemWatcher`; have `poll` return path/size/mtime deltas without invoking `MetadataExtractor` (the planner already extracts off-thread). Never let a background poll mutate the active session — queue additions and surface a dismissible "3 new files found — add to this run?" banner.
- **Test:** `tests/test_sorting.py`. Assert `SortMonitorService.poll` returns deltas without calling `MetadataExtractor.extract`. Offscreen UI test: with a plan present, invoke `_poll_change_monitors` and assert `page.plan is not None`.

### 29. `ffprobe` is spawned for every video during scanning even when no rule uses video metadata
- **Defect:** `metadata.py:44-47` calls `_video_metadata` for anything whose guessed mime starts with `video/`, unconditionally. Width/height/duration are only consumed if the active profile has a condition on `WIDTH`/`HEIGHT`/`DURATION` (`rules.py:112-115`); the Simple profile builds extension-only conditions (`presets.py:113`, `:121`). `shutil.which("ffprobe")` is re-resolved on every call (`metadata.py:58`). **Scale:** a Windows process launch is roughly 30-80 ms, so 3,000 clips is minutes of wall clock before the plan appears, plus ffprobe's own container reads over USB.
- **Fix:** Inspect the profile's conditions once in `HybridSortService.prepare` and pass `need_media_dimensions` down to `SortScanner`/`MetadataExtractor`, defaulting off. Cache `shutil.which` at instance level.
- **Test:** `tests/test_sorting.py`. Patch `subprocess.run`; assert zero calls when the profile has no dimension/duration condition, and non-zero when it does.

### 30. Planner re-stats and re-resolves the source roots for every file
- **Defect:** `planner.py:96` — `any(Path(value).is_dir() and _within(destination, Path(value).resolve()) for value in sources)` reconstructs a Path, stats it, and calls `resolve()` for every source on every file. `sources` is loop-invariant. On Windows `Path.resolve()` goes through `_getfinalpathname`, which opens a handle. `planner.py:52` adds another per-file `resolve()` on `metadata.path`, which `MetadataExtractor.extract` already stored resolved (`metadata.py:50`) — a guaranteed no-op realpath call.
- **Fix:** Hoist `source_roots = tuple(p for p in (Path(v).expanduser().resolve() for v in sources) if p.is_dir())` before the loop. Drop the `.resolve()` at `:52` and document the already-resolved invariant.
- **Test:** `tests/test_sorting.py`. Counter on `Path.resolve`; assert calls scale with `len(sources)`, not `len(files) × len(sources)`.

### 31. Cross-volume moves hash the source twice and the destination twice
- **Defect:** `executor.py:388` computes `fingerprint = self._fingerprint(source, control)`. On the EXDEV/EACCES/EPERM fallback path `_move` does `self._verify_fingerprint(destination, self._fingerprint(source, control))` at `executor.py:508` — recomputing the identical digest of the same unchanged source that is sitting in a local variable one frame up. Control returns to `_execute_item`, which verifies the destination *again* at `:436`. For same-volume moves `os.replace` never touches the bytes, yet the file is hashed at `:388` and the destination re-read at `:436` — 2N of reads for a pure rename.
- **Fix:** Pass the computed `fingerprint` into `_move`. Skip the redundant `_verify_fingerprint` at `:436` on the branch that already verified inside `_move`. For same-volume `os.replace`, record the source fingerprint (undo needs it) but skip the post-rename re-read.
- **Test:** `tests/test_sorting.py`. Counter on `SortExecutor._fingerprint`; assert exactly 1 call for a same-volume MOVE and ≤ 2 for a forced-EXDEV move.

### 32. Bulk duplicate selection is O(n²) with a table repaint per row
- **Defect:** `pages.py:908-918` loops every checkbox calling `setChecked()`. Each emits `toggled` → `_duplicate_selection_changed` (`:902`) → `self.table.selectRow(row)` (full selection change + repaint) → `currentCellChanged` → `_update_duplicate_detail` (`:920`), which linear-scans `item_rows` (`:925`) and runs a nested generator over every group and item (`:926-934`) → plus `_refresh_recoverable` (`:1004`), which rebuilds a dict over all items and calls `_selected_ids` over all checkboxes. **Scale:** 5,000 rows ≈ 25M dict/attribute operations and 5,000 selection changes per button click.
- **Fix:** Guard with a `self._bulk_update` flag that short-circuits `_duplicate_selection_changed`, wrap in `setUpdatesEnabled(False)`, and call `_refresh_recoverable()` + `_update_duplicate_detail()` once at the end. Cache the id→item and row→id maps.
- **Test:** Offscreen. 2,000 rows; counter on `_refresh_recoverable`; assert 1 call per bulk click.

### 33. Dashboard reads every operation record, transfer report and quarantine manifest — twice at startup
- **Defect:** `DashboardService.summary()` calls `operations.list_records()` (`support_services.py:582`), which opens and parses every file in `paths.operations` (`:472-479`), then `_records_from_reports()` (`:543-547`) which parses every report including its `failures` array, then `quarantine.list_records()` (`:587`) which parses every manifest. Only `records[:5]` and `interrupted[:5]` are used (`:609-610`). `OverviewPage.__init__` calls `refresh(include_storage=False)` then `QTimer.singleShot(0, self.refresh)` (`pages.py:299-300`) — the whole read happens twice, on the main thread. Nothing prunes `paths.operations`; `operation_audit.jsonl` is never rotated. **Scale:** ~740 small-file opens × 2 at six months of use; with real-time AV at 1-3 ms each that is 1.5-4.5 s of frozen window, growing without bound.
- **Fix:** Add a `limit` to `list_records()`, sort `paths.operations.glob("*.json")` by name (IDs are timestamp-prefixed, so name order is time order), parse only the newest K. Skip `_records_from_reports()` for the dashboard or cap it identically. Drop the duplicate startup refresh. Add operations/audit retention to the existing `cache_retention_days` prune.
- **Test:** `tests/test_phase3.py`. 300 record files; counter on `Path.read_text`; assert `summary()` opens ≤ 20.

### 34. Activity and Quarantine search boxes re-read every record file on each keystroke
- **Defect:** `pages.py:1810` connects `search.textChanged` directly to `refresh`, which calls `operations.list_records()` (`:1901`) — a full glob + parse of every operation JSON *and* every report — then rebuilds the table with a fresh `QCheckBox` cell widget per row. `QuarantinePage` does the same at `pages.py:2206`/`:2294`.
- **Fix:** Load once into memory, filter in memory on keystroke, keep the existing Refresh button for re-reading disk. If disk reads must stay, debounce through a ~250 ms `QTimer`.
- **Test:** Offscreen. Type 6 characters via `QTest.keyClicks`; assert `list_records` is called once.

### 35. Sort review table builds a `QCheckBox` widget per planned file in one pass; filter re-joins every row per keystroke
- **Defect:** `sort_workspace.py:1048-1087` builds the entire table in one synchronous loop, creating a real `QCheckBox` cell widget (`:1058`) plus five `QTableWidgetItem`s per item, with no batching. `DuplicatesPage` already solved this with a 150-row `QTimer.singleShot` render queue (`pages.py:849-898`). `_filter_review` (`:1116-1130`), bound to `textChanged` (`:721`), builds a fresh `" ".join(...).casefold()` over eight fields for every item on every keystroke.
- **Fix:** Reuse the batched render-queue pattern; replace the per-row checkbox widget with a checkable `QTableWidgetItem`; precompute each item's lowercase haystack once at render.
- **Test:** Offscreen. 5,000-item plan; assert `_render_plan` returns before all rows are populated (queue non-empty) and that filtering does not recompute haystacks.

### 36. `_restore_for_undo` globs the backup directory to compute a counter
- **Defect:** `executor.py:543` — `backup_dir / f"{len(list(backup_dir.glob('*'))):06d}_{target.name}"` enumerates every backup written so far, once per restored file. O(k²) directory entries across an undo. (The equivalent in `_execute_item` was already fixed to a path-hash token at `executor.py:404`; this one was missed.)
- **Fix:** Same treatment — hash the target path, or `tempfile.mkstemp` in the backup directory. The index carries no semantics beyond uniqueness.
- **Test:** `tests/test_sorting.py`. Counter on `Path.glob`; assert an undo of 200 records with conflicting targets performs O(1) globs of `undo_backups`.

---

## TIER 3 — Product incoherence and dead code

### 37. Scheduled scan/sort/organizer tasks launch the GUI in every packaged install
- **Defect:** `support_services.py:123` builds the command as `shutil.which("dtm-scheduled-scan")` with a fallback of `[sys.executable, "-m", "duplicate_transfer_manager.scheduled_scan", ...]`. `packaging/installer.iss:38` ships only `dist\DuplicateTransferManager\*`, so no console script exists and `which` returns None; `sys.executable` is then the PyInstaller windowed exe whose entry point is `main.py`. I grepped for `frozen` across `src/` — **zero hits**, so the `-m ...` arguments are handed to Qt and discarded. Same fallback at `sorting/scheduler.py:22` and `support_services.py:176`. Invisible in development, where `pip install -e .` creates the scripts. Result: `schtasks` returns 0, the UI reports success, and every day at the scheduled time a stray app window opens and no scan runs. README.md:40-42 and USER_GUIDE.md:155-157 promise the feature works.
- **Fix:** Branch on `getattr(sys, "frozen", False)` in `ScheduledScanService.command`, `configure_organizer_preview`, and `SortScheduleService.command`; emit `[sys.executable, "--run-scheduled-scan", ...]` with an argv branch in `main.py`/`ui/app.py` that dispatches before any `QApplication` is created. Alternatively add console-script EXE targets to the PyInstaller spec and ship them in `installer.iss`.
- **Test:** `tests/test_phase3.py`. Monkeypatch `sys.frozen = True` and `sys.executable` to a fake path; assert the generated command's first token is that executable and the remaining tokens are a flag the frozen entry point actually parses.

### 38. The whole update feature is unreachable; PRIVACY.md describes requests the app never makes
- **Defect:** `UpdateController` is never constructed anywhere in `src/` (my grep for it, `DeviceController`, `ReportsController`, `QuarantineController` across `src/` outside `controllers/` returned nothing). Even if it were, `UpdateController.check()` calls only `service.status()`, which performs no network I/O and hardcodes `"channel": "stable"` (`support_services.py:698`) regardless of `AppSettings.update_channel`. `check_manifest_url`/`download_installer`/`launch_verified_installer` have no caller except `MainWindow.launch_verified_update` (`shell.py:354`), which itself has no caller. `PRIVACY.md:10-16` states the app "fetches a signed update manifest no more than once per day"; `SECURITY.md:31-45` describes a verification posture that never engages. The verification code is real and tested — just unreachable.
- **Fix:** Either wire it up (construct `UpdateController` in `MainWindow`, gate a check on `settings.check_updates_automatically`, pass `settings.update_channel` through, add a "Check for updates" action reaching `launch_verified_update`), or remove the "Update channel" and "Check for updates automatically" controls and correct `PRIVACY.md:10-21` to say the app makes no network requests.
- **Test:** `tests/test_phase1.py`. Assert `UpdateService.status()["channel"]` reflects the passed setting, and an integration assertion that `check_manifest_url` is reachable from a constructed `MainWindow` (or that the settings controls no longer exist).

### 39. Simple/Advanced mode only hides one card on the Settings page
- **Defect:** `AppSettings.experience_mode` has exactly two consumers in the entire application — the combo that displays it (`pages.py:2540`) and the assignment that saves it (`pages.py:2659`). The only attached behaviour is `_sync_advanced_visibility` (`pages.py:2731-2732`), which toggles the Settings page's own card. The duplicate scan's hash algorithm/mode/threads/min-size/exclusions (`pages.py:470-506`), the import page's hash mode/workers/retries/conflict policy/cache toggles/timeouts (`pages.py:1249-1319`), and the Sort Files profile/rule/ML panel (`sort_workspace.py:436-440`) are all built unconditionally. `docs/USER_GUIDE.md:18-20` and `docs/ARCHITECTURE.md:121-122` both promise "Advanced reveals them".
- **Fix:** Pass `AppSettings` into the visibility logic of `DuplicatesPage`, `ImportPage` and `SortWorkspace` (they already receive `settings`) and collapse their advanced `DisclosurePanel`s in simple mode, refreshing from `MainWindow._preferences_saved` (`shell.py:326-328`). If per-page gating is not wanted, delete the selector and fix both docs.
- **Test:** Offscreen. Construct `DuplicatesPage` with `experience_mode="simple"`; assert the advanced panel `isHidden()`; flip to `"advanced"` via `update_preferences` and assert it is visible.

### 40. The documented legacy Tk frontend moves duplicates mid-scan with no manifest and no restore
- **Defect:** `ui_components.py:633` does `shutil.move(dup.path, target)` **inside the scan loop**, one group at a time as they are discovered, into a user-chosen flat folder. The only gate is a yes/no dialog *before* the scan starts (`ui_components.py:495-502`), which cannot list files that have not been found yet. No `QuarantineRecord`, no manifest under `paths.quarantine`, no `OperationRecordService` entry. `DuplicateQuarantineService.list_records` globs only `paths.quarantine/*/manifest.json` (`duplicate_workflow.py:487`), so these files are invisible to the Quarantine page and unrecoverable from within the product. `README.md:117` documents `python legacy_main.py` as a supported launch, `pyproject.toml:55` ships `legacy_main` as a py-module, and `tkinterdnd2` is a hard runtime dependency solely for it. This directly contradicts `USER_GUIDE.md:9-12` ("Nothing is deleted… Nothing runs without review") and `README.md:188-190`.
- **Fix:** Remove `legacy_main.py`/`ui_components.py` from `py-modules`, drop `tkinterdnd2`, and delete the README section — or route `OrganizerFrame`'s isolate step through `DuplicateQuarantineService.quarantine` after a review step. Leaving a documented path that bypasses the recovery guarantee is the worst option for a pre-1.0 release.
- **Test:** Packaging test asserting `legacy_main` is not in `pyproject.toml` `py-modules` and `tkinterdnd2` is not in `dependencies` (option A), or a service test asserting the legacy isolate writes a manifest `list_records()` can read (option B).

### 41. `FileOrganizerService` is a second, unreachable sorting engine the changelog says was removed
- **Defect:** `services/organizer_service.py` (611 lines) has its own category taxonomy, EXIF reader, ML heuristic, and manifest format, all duplicating `sorting/`. `FileOrganizerController` (`controllers/operations.py:54-85`) is exported but never instantiated — my grep across `src/` outside `controllers/` returned nothing. `ScheduledScanService.configure_organizer_preview` (`support_services.py:163-186`) has no caller, so `scheduled_organizer.py` and `dtm-scheduled-organizer` can never run. The only surviving in-app call is `prune_manifests` at `ui/app.py:48` and `pages.py:2694`, pruning a directory that can no longer be populated. `AppSettings.organization_presets` / `organization_schedule_frequency` (`contracts.py:118-119`) are dead. `CHANGELOG.md:110-113` records the removal of only the *page*. The two engines already disagree — `_ml_suggestion` and `LocalMLService._tokens` (`sorting/ml.py:39-46`) are separate keyword lists. Two latent defects sit inside it that must not be resurrected: `rollback` with `conflict_policy="replace"` calls `resolved.unlink()` (`organizer_service.py:391`) with no backup, and `_update_catalog` (`:584`) rebuilds the whole catalog once per record.
- **Fix:** Delete `organizer_service.py`, `FileOrganizerController`, `scheduled_organizer.py`, the `dtm-scheduled-organizer` entry point, `configure_organizer_preview`, and the dead settings fields — keeping only what `SortingMigrationService` needs to read existing manifests, and keeping `AppSettings.from_dict`'s unknown-key tolerance so old settings files load. Drop the `prune_manifests` calls. Drop the `organization*` branch from the Activity filter (`pages.py:1911`). Correct `CHANGELOG.md:110-113`.
- **Test:** `tests/test_organizer.py` becomes a migration-only test; add an assertion that an old `settings.json` containing `organization_presets` still loads through `AppSettings.from_dict`.

### 42. Two different features are both called "quarantine", stored apart, only one restorable
- **Defect:** The rule editor offers every `SortAction` unfiltered (`sort_workspace.py:157`, `for value in SortAction:`), including `QUARANTINE`. The planner routes those files to `self.paths.sorting / "quarantine"` (`planner.py:121-124`) — a different root from `paths.quarantine`. `DuplicateQuarantineService.list_records` globs only `paths.quarantine/*/manifest.json` (`duplicate_workflow.py:487`), so sort-quarantined files never appear on the Quarantine page, have no `QuarantineRecord`, and cannot be restored by it. `USER_GUIDE.md:104-112` describes exactly one quarantine.
- **Fix:** Have the executor write a `QuarantineRecord` manifest under `paths.quarantine` for `SortAction.QUARANTINE`, reusing the operation-folder format so both producers feed one page. Or rename the sort action ("Hold" / "Set aside") and document where those files go.
- **Test:** `tests/test_sorting.py`. Execute a one-item plan with `SortAction.QUARANTINE`; assert `DuplicateQuarantineService(paths).list_records()` returns it.

### 43. Android settings are persisted and round-tripped but never applied
- **Defect:** Three of four Android fields have no reader outside the widgets that write them. `android_enabled` (`contracts.py:107`, written `pages.py:2677`) gates nothing — the Android source tile (`pages.py:397`), device picker, ADB browse and cache are all built unconditionally. `android_default_path` (written `pages.py:2678`) is never read; both pages hardcode `/sdcard/DCIM` (`pages.py:670`, `:1460`) and `/sdcard` as the browse fallback (`:639`, `:1427`). `keep_android_awake` (written `pages.py:2679`) is never applied — the import page hardcodes `self.keep_awake.setChecked(True)` (`pages.py:1283-1284`) and `update_preferences` (`:1408-1418`) refreshes only favourite locations. `android_platform_tools_version` has no reader at all. Meanwhile `android_platform_tools_path`, the one field that *is* honoured (`ui/app.py:45-46`), has no UI control. `README.md:46-48` lists "Android behavior" among what the user can configure.
- **Fix:** Seed the Android path selectors and browse roots from `android_default_path`; seed `keep_awake` from `keep_android_awake` in both the constructor and `update_preferences`; gate the Android controls on `android_enabled` or delete that checkbox; delete `android_platform_tools_version` and add a picker for `android_platform_tools_path`.
- **Test:** Offscreen. Construct `ImportPage` with `android_default_path="/sdcard/Pictures"` and `keep_android_awake=False`; assert the path field and the checkbox reflect them, and again after `update_preferences`.

### 44. Drive/ADB cache lock is honoured by one writer of two; `.tmp` is a fixed shared name; no stale recovery
- **Defect:** `build_drive_cache` acquires an exclusive lock at `drive_cache.py:278` and releases at `:302`. `engine.load_drive_cache`/`load_adb_cache` (`engine.py:399-420`) and the saves at `engine.py:1077`/`:1088` never do — and those saves rewrite the whole JSON from the in-memory snapshot taken at run start. A lock only one writer checks provides no mutual exclusion. `DriveHashCache.save` writes to a fixed `f"{self.path}.tmp"` (`drive_cache.py:68`), so two writers share one scratch file before `os.replace`. `load` treats `JSONDecodeError` as "start empty" (`:60-61`), so corruption is silent. `acquire_lock` writes the PID (`:80-83`) but nothing ever reads it, `release_lock` runs only in `build_drive_cache`'s `finally`, and it catches only `FileExistsError` — a hard kill leaves the lock forever and every subsequent weekly `schedule_adb_cache.ps1` run raises `RuntimeError: Cache is locked` with no error surface.
- **Fix:** Have `load_drive_cache`/`load_adb_cache` take the same lock for the run, falling back to read-only cache use when it cannot be taken (`update_drive_cache` already handles that path cleanly). Switch `save()` to `tempfile.mkstemp(dir=...)`. Give `acquire_lock` a PID + timestamp payload with staleness reclaim, and catch `OSError`. Give the scheduled script a nonzero-exit log.
- **Test:** `tests/test_engine.py`. Write a `.lock` containing a PID that is not alive and an old mtime; assert `acquire_lock` reclaims it. Second test: two `DriveHashCache.save()` calls concurrently do not collide on the same temp name.

### 45. `SortController.execution_control` is written from the worker thread and never cleared
- **Defect:** `controllers/operations.py:114` assigns `self.execution_control` inside the task body — on the pool thread — while `pause`/`resume`/`skip_current`/`cancel` read it from the main thread with no synchronisation (`:128-151`). It is never reset on completion, so after the first run the attribute permanently holds the previous run's control. `pause()` checks only `if not self.execution_control or not self.busy` — both satisfied by a stale control in the window between `start_task` and the worker entering the body — then unconditionally calls `self._set_state(OperationState.PAUSED)`.
- **Fix:** Create the `SortExecutionControl` in `execute()` on the calling thread before `start_task`, pass it into the closure, guard access with the controller's existing `_lock`, and clear it in `_on_finished`.
- **Test:** `tests/test_phase1.py`. Run a controller task to completion, assert `execution_control is None`; then assert `pause()` returns False before a second task's body has run.

### 46. "Skip current file" skips whichever review row is selected — usually none
- **Defect:** `sort_workspace.py:1208-1213` reads `self.review_table.currentRow()` and passes `self.plan.items[row].metadata.path`. That row is whatever the user last clicked while reviewing; `_render_plan` never calls `selectRow`, so after a fresh preview `currentRow()` is `-1` and the method returns without calling the controller at all. The processing header (`:768`) says "You can pause, skip the current file, or cancel safely" and the button reads "Skip current file" (`:779`).
- **Fix:** Track the processing source path from `OperationEvent.current_item` in `_on_progress` and pass that; disable the button when no current item is known; show a banner naming the skipped file.
- **Test:** Offscreen. Feed an `OperationEvent` with `current_item` set, click Skip, assert `controller.skip_current` was called with that path.

### 47. Duplicates page reimplements the service's keep-selection rule
- **Defect:** `pages.py:971-981` contains its own `quality_key` closure and oldest/newest ordering, a line-for-line re-derivation of `_quality_key` (`duplicate_workflow.py:195-204`) and the `prefer` branch in `build_duplicate_review`. Identical today, so there is no live divergence — but the decision of which file survives a quarantine now has two implementations, only one covered by service tests.
- **Fix:** Export `reselect_keepers(review, prefer)` from `duplicate_workflow` and have `_apply_preference` call it; delete the UI copy.
- **Test:** `tests/test_phase3.py`. Assert `reselect_keepers(review, "quality")` and the initial `build_duplicate_review(..., prefer="quality")` pick the same keeper.

### 48. Default profile and default categories only apply at page construction
- **Defect:** All pages are constructed once in `MainWindow.__init__` (`shell.py:262-280`). `_preferences_saved` (`shell.py:326-328`) forwards to only `duplicates` and `import`, and `ImportPage.update_preferences` (`pages.py:1408-1418`) refreshes nothing but the two favourite-location combos. `default_transfer_profile` is applied only in the constructor (`pages.py:1236-1237`), `default_file_categories` only in the constructor's checkbox loop (`:1221-1225`). Nothing forwards settings to `SortWorkspace` at all, despite it holding and mutating a live `settings` reference (`sort_workspace.py:904`).
- **Fix:** Extend both `update_preferences` methods to re-seed the profile combo and category checkboxes (guarding values the user has already overridden for the pending run), and forward to `SortWorkspace`.
- **Test:** Offscreen. Construct `ImportPage` with Balanced, call `update_preferences` with Reliable, assert the combo reads Reliable.

### 49. `HashCache` bare excepts hide a cache that is never being saved
- **Defect:** `utils.py:91` wraps `makedirs` + `open` + `json.dump` in `except: pass`; `utils.py:82` swallows everything into `self.data = {}`; `SessionLogger.log` does the same at `utils.py:69`. The write is non-atomic — it truncates `self.path` in place rather than using the tmp + `os.replace` pattern that `DriveHashCache.save` and `_atomic_json_write` both use. The lock at `utils.py:74` covers `get`/`set` but not `save`, so `json.dump` can iterate `self.data` while a worker mutates it; the resulting `RuntimeError` is swallowed by the same bare except.
- **Fix:** Catch `(OSError, TypeError, ValueError)` explicitly and log through the session logger; route the write through an atomic tmp + `os.replace`; snapshot `dict(self.data)` under the lock before dumping.
- **Test:** `tests/test_engine.py`. Patch `open` to raise `OSError` on the cache path; assert the failure is logged and `save()` does not silently succeed. Second: assert a killed write leaves the previous file intact.

---

## §4 — Findings I believe are WRONG or OVERSTATED

**Already fixed — these describe code that no longer exists. Do not schedule them.**

1. **"Overwrite backup destroys the incumbent with an unverified, unjournaled move" (blocker).** Fixed in `cd94a07`. `executor.py:414-429` now appends a `"replacing"` intent record naming source, destination and backup path *before* touching anything, then does `copy2` → `_verify_fingerprint(backup, incumbent_fingerprint)` → `unlink()`. Exactly the fix the finding proposes.

2. **"Run journal is fully rewritten after every item — O(n²), 473 GB / 120 GB" (blocker, reported twice: sorting-engine and cross-cutting).** Fixed in `dfdd0fe`. `_write_journal` is now called only at run start (`executor.py:122`), on cancel (`:140`, `:162`) and at the end (`:198`). Per-item durability is one appended line to `records.jsonl` (`_append_record`, `executor.py:632-651`), with `_merge_appended_records` folding it back in `load_run` (`:354`). The 473 GB and 120 GB figures are both obsolete.

3. **"`prune_runs` permanently deletes overwritten originals" (high, reported twice).** Fixed. `executor.py:288-289` skips any run where `_holds_replaced_originals` is true, and `_holds_replaced_originals` (`:313-320`) returns True on `OSError` — never deletes on a guess. The docstring at `:272-279` now states the real behaviour, and `runs_retained_for_replaced_files` (`:296`) exists to surface them.

4. **"Per-item `sum(...)` over the plan slice is O(n²) arithmetic."** Fixed. `planned_total` is hoisted to `executor.py:126` and `planned_remaining` is a running subtraction at `:186`, with a comment naming the old bug.

5. **"Backup index is derived by globbing `replaced/` on each conflict" — half fixed.** `executor.py:404` now uses a SHA-256 path token. Only `_restore_for_undo` at `executor.py:543` still globs; I kept that as finding #36 at low priority.

**Overstated but real:**

6. **"Restore reads every quarantined file twice with SHA-256" framed as pure waste.** The two reads at `duplicate_workflow.py:435` and `:437` are a legitimate before/after verification of the move — the second read is what proves the file did not change in flight. The `replace`-policy pair at `:423-424` is likewise a verified backup. The real defect is that all of it runs on the main thread with no progress or cancel (#8), plus the per-record manifest rewrite (`_mark_restored` at `:527`). Do not "optimize" the verification away.

7. **"Cross-volume moves read the destination 2× and the source 3×."** Source is read twice, not three times (`executor.py:388` and `:508`); destination twice (`:508` and `:436`). The waste is real, the multiplier is smaller than claimed.

8. **"Conflict skips leak staged files at `engine.py:955` and `:977`."** Only `engine.py:920-923` leaks. The other two branches run after `promote_transfer_file`, which removes the staged file on its own skip path (`engine.py:245`). Narrowed in #6.

9. **"Sort Files quarantine files are unrecoverable once retention prunes the run."** No longer true after the `prune_runs` fix — but `sorting/quarantine` still does not appear on the Quarantine page, which is the real defect (#42).

---

## §5 — Cannot be settled without a real device, a packaged build, or a GUI session

- **#37, scheduled tasks in frozen builds.** The `sys.frozen` gap is provable statically and the fix is unit-testable with a mocked `sys.frozen`, but *confirming* that the installed task opens a window rather than running a scan requires building the installer from `packaging/installer.iss` and registering the task on a real machine.
- **#8/#28 perceived responsiveness.** That the work is on the main thread is proven statically. Whether Windows actually paints "Not Responding" at a given file count, and how long the freeze lasts, needs an interactive session.
- **#23 hashing speedup.** The absence of a thread pool is certain; the actual speedup from 4-8 readers depends on the specific NAS/USB stack and must be measured on real hardware.
- **ADB timing claims throughout** (`list_devices` 6-12 s, `adb pull` throughput, the 180 s stall timeout on a locked phone) require a physical Android device. The blocking structure itself is confirmed in `adb_bridge.py`.
- **#44, cache clobbering between the weekly `schedule_adb_cache.ps1` task and a concurrent import.** The one-sided lock and shared `.tmp` are provable; observing an actual interleaved corruption needs two real processes racing.
- **#3, `GetFileAttributesW` failures in the wild** (long paths, denied ACLs, transient sharing violations). The `-1 & 6 == 6` logic is arithmetic and testable by monkeypatch; which real paths trigger it on a user's drive is not.
- **#39 and #43 visual outcomes.** The dead settings fields are proven by grep; whether the resulting UI reads as coherent is a design judgement for a GUI session.

---

## §6 — Verdict

The engine is in better shape than the raw findings suggest — the sorting executor's overwrite-safety, journal-write amplification, and retention-prune data loss were all genuinely fixed in the last two commits, and the duplicate-scan path already has the full-content confirmation pass that the transfer path is missing. What remains is not architectural rot but a consistent, narrow pattern of the same three mistakes repeated across subsystems: a correct safety mechanism built and then not applied everywhere (full-hash confirmation on scan but not import; a lock in `build_drive_cache` but not in the engine; verified backups in `undo` but count-only validation in the cache fast path), a worker boundary built and then bypassed (`BaseOperationController` exists and is used by exactly two of the eight long operations), and a setting persisted and then never read (`experience_mode`, three of four Android fields, `update_channel`, `default_transfer_profile` after construction). **It is not releasable.** Findings #1 through #14 can each cost a user files or silently misreport what happened to them, and #1 in particular — one line ordering error that turns "Select recommended copies" into "select everything" — would empty a photo library on first use. #37 and #38 ship documented features that provably cannot function in a packaged install, and #40 ships a documented launch path that contradicts the product's headline safety promise. Realistically: Tier 0 plus #15-#18 and #37-#38 and #40 are the release gate; Tier 2 is a fast-follow (nothing there loses data, and #23/#25/#29 are the ones users will actually feel); Tier 3 is where the product decides what it is, and half of it is deletion rather than construction, which is the cheapest work on this list.

## All findings

### 1. [blocker/execution-time] The run journal is fully rewritten after every item — O(n^2) bytes written (measured 473 GB for a 20k-file run) **FIXED in `dfdd0fe`**

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:144, :178, :557-569`

**Claim:** `execute()` calls `self._write_journal(journal_path, journal)` once per item, and `_write_journal` serializes the *entire* payload — including `planned_items`, which holds a full serialized FileMetadata for every selected file and never shrinks — to a temp file and os.replaces it. So write #1 already costs O(n), and the total is O(n^2). I measured the serialized sizes with the real `_serialize_item`/`_record` on a representative photo item: 867 bytes per planned_item, 630 bytes per record. That gives ~7.5 MB of journal at 5,000 items and ~29.9 MB at 20,000, and a total of ~29.6 GB / ~473 GB written over the run respectively. The dry-run branch (:144) does the same per item, so a preview of 50k files pays the identical cost while touching no user file.

**Failure scenario:** User points Sort Files at a 20,000-photo library and clicks through Review to Processing. The actual file moves are same-volume renames that should take seconds; instead the run writes roughly 473 GB of JSON to the app data drive and spends hours at 100% on json.dump. On an SSD this burns a measurable fraction of the drive's write endurance for a single sort. The progress bar advances, so the user sees no error — just an operation that never finishes.

**Suggested fix:** Stop rewriting the whole document. Write the immutable header + `planned_items` once at run start, then append each record as one line to a sibling `records.jsonl`, and only materialize the consolidated `journal.json` at run end (and on cancel). `load_run`/`list_runs`/`undo`/`resume_run` read the appended log if the consolidated file is absent. If a single-file format must be kept, at minimum flush on an interval/byte budget rather than per item.

### 2. [blocker/execution-time] Confirming quarantine runs shutil.move and `adb pull` synchronously on the Qt main thread

**Location:** `src/duplicate_transfer_manager/ui/pages.py:1043 (calls services/duplicate_workflow.py:368, 373)`

**Claim:** `DuplicatesPage._confirm_quarantine` is a plain `clicked` slot and calls `self.quarantine_service.quarantine(...)` inline. That service loops over every selected item doing `ADBBridge.pull(item.path, stored_path, serial=...)` for Android items (duplicate_workflow.py:368) and `shutil.move(item.path, stored_path)` for local ones (line 373). No controller, no QRunnable, no thread pool — this violates the stated invariant that ADB/transfer work never runs on the Qt main thread.

**Failure scenario:** User scans /sdcard/DCIM on a phone, selects 400 duplicate photos and videos (~6 GB) and clicks Confirm quarantine. The app performs 400 sequential `adb pull` operations on the GUI thread. The window stops repainting immediately, Windows marks it "Not Responding", there is no progress bar and no Cancel button, and the only recourse the user has is to kill the process mid-pull. Even the local-only path stalls for minutes when quarantining a large set across a slow drive.

**Suggested fix:** Route quarantine through a `BaseOperationController` task (the same pattern `DuplicateScanController` already uses) so it runs on the pool thread, reports progress into the existing `ProgressPanel`, and honours the cancellation token.

### 3. [blocker/execution-time] Sort Undo and Retry/Resume execute the whole file-moving pipeline on the Qt main thread, bypassing SortController

**Location:** `src/duplicate_transfer_manager/ui/sort_workspace.py:1513, 1528 (calls sorting/executor.py:195, 279, 288)`

**Claim:** `_undo_selected` calls `self.executor.undo(run_id)` directly, which reverses every completed record with fingerprint verification and `shutil.move` (executor.py:216-241). `_retry_selected` calls `self.executor.retry_failed(...)` / `self.executor.resume_run(...)`, both of which end in `self.execute(plan, ...)` (executor.py:286, 298) — the identical move/copy/verify/journal pipeline that `SortController.execute` deliberately pushes onto a worker thread. Here it runs inline on the GUI thread, so `SortExecutionControl` (pause/skip/cancel) is never created and the pause/resume/skip buttons and progress panel are all bypassed.

**Failure scenario:** User sorts 40,000 photos into D:\Sorted, decides it was wrong, and clicks "Undo selected run". The app hashes and moves 40,000 files back on the GUI thread. The window is frozen for many minutes with no progress and no way to stop; if the user force-kills it, the journal is only rewritten at the end (executor.py:247-250) so every already-restored file is left with `undone_at` unset and a second undo attempt will try to restore files that are no longer at the destination.

**Suggested fix:** Add `undo`/`retry`/`resume` entry points to `SortController` that wrap these calls in `start_task`, wire them to the existing `progress`/`completed`/`failed` signals, and reuse the process page's progress panel and cancel button.

### 4. [blocker/execution-time] Quarantine and restore run entirely on the Qt main thread

**Location:** `src/duplicate_transfer_manager/ui/pages.py:1043 (also 2432-2446, 2456)`

**Claim:** `_confirm_quarantine` calls `DuplicateQuarantineService.quarantine(...)` synchronously from a `clicked` handler (wired at pages.py:583), and `restore_selected_operation` / the restore-all path call `restore_operation(...)` the same way. Every other long operation in the app goes through `BaseOperationController.start_task` → `OperationWorker` on a QThreadPool (controllers/base.py:108-132), but there is no quarantine or restore controller in controllers/operations.py at all. The work these calls do on the GUI thread is: `shutil.move` per file, `ADBBridge.pull` per Android file (a subprocess spawn plus a full USB transfer, duplicate_workflow.py:368), a full manifest rewrite per item (duplicate_workflow.py:380), and on restore two full SHA-256 reads per file plus a `copy2` backup (duplicate_workflow.py:423-437). This violates the stated invariant that ADB and transfer work never runs on the Qt main thread.

**Failure scenario:** A user scans a phone, gets 3,000 duplicate photos, and clicks "Confirm quarantine". Each Android duplicate is pulled over USB one at a time on the GUI thread. The window stops repainting immediately, Windows marks it "Not Responding" and grays it out, there is no progress and no cancel, and the operation runs for tens of minutes. A user who concludes the app has hung and kills it from Task Manager stops the run mid-pull, leaving manifest records stuck at status "pending" and a partial file in the quarantine folder. The same freeze happens on "Restore selected operation" for a large operation, where every file is hashed twice before it is put back.

**Suggested fix:** Add a `QuarantineController(BaseOperationController)` in controllers/operations.py that wraps `DuplicateQuarantineService.quarantine` and `.restore_operation` in `start_task`, threading `CancellationToken` and `OperationReporter` through the per-item loops in duplicate_workflow.py so the existing progress/cancel UI applies. Have pages.py call the controller and react to `completed`/`failed` signals instead of calling the service inline.

### 5. [blocker/logic] Overwrite backup destroys the incumbent file with an unverified, unjournaled move **FIXED in `cd94a07`**

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:352-358 (and the record/journal write at :170-178)`

**Claim:** When an item has conflict=="overwrite", `_execute_item` displaces the existing destination file with a bare `shutil.move(str(destination), str(backup_path))` into `run_root/replaced/`. `run_root` is under %LOCALAPPDATA%\DuplicateTransferManager\sorting\runs, so for the normal case (sorting onto D:\ or an external drive) this is a cross-volume move, i.e. copy2 + unlink of the original, with (a) no fingerprint verification of the copy and (b) no journal entry written before the original is deleted. The code proves this is the wrong standard: `_restore_for_undo` (executor.py:472-473) does exactly the same displacement and *does* verify (`shutil.copy2` then `_verify_fingerprint(backup, self._fingerprint(target))`). The journal record naming the backup is only produced by `_record(..., replaced_backup=backup)` at executor.py:377, appended at :170 and flushed at :178 — all strictly after the original has already been deleted from the destination volume.

**Failure scenario:** User sorts a folder onto an external USB drive with a rule whose conflict policy is Overwrite (or Keep Newest where the source wins). For file X, the incumbent D:\Sorted\Pictures\IMG_001.jpg is copied to C:\Users\...\sorting\runs\sort_.../replaced/000000_IMG_001.jpg and deleted from D:. Either (1) the USB read glitched and the C: copy is silently corrupt — nothing checks it, and the verified-looking journal now points at a bad backup; or (2) the app is killed / the machine loses power in the ~second between the delete and the journal flush — D:\Sorted\Pictures\IMG_001.jpg no longer exists, the journal contains no record of the backup, nothing in Undo or History references the orphan in `replaced/`, and `prune_runs` deletes the whole directory after the retention window. The user's original file is gone.

**Suggested fix:** Write an intent record (source, destination, backup_path, expected fingerprint) into the journal and flush it *before* displacing the incumbent, so a crash leaves a recoverable pointer. Replace the bare `shutil.move` with the `_restore_for_undo` pattern: `shutil.copy2` to the backup, `_verify_fingerprint(backup, self._fingerprint(destination))`, and only then unlink the destination. Add a startup reconciliation that surfaces orphaned `replaced/` entries for runs whose journal has no matching record.

### 6. [blocker/logic] Undo and Retry/Resume run file hashing and moves synchronously on the Qt main thread

**Location:** `src/duplicate_transfer_manager/ui/sort_workspace.py:821-822, :1513, :1528 (executor work at sorting/executor.py:213-241, :457-494)`

**Claim:** `undo.clicked.connect(self._undo_selected)` and `retry.clicked.connect(self._retry_selected)` are direct Qt signal connections, and the handlers call `self.executor.undo(...)` and `self.executor.retry_failed(...)/resume_run(...)` inline. `SortController` (src/duplicate_transfer_manager/controllers/operations.py:88-151) exposes only `preview`, `execute`, `pause`, `resume`, `skip_current`, `cancel` — there is no worker entry point for undo or retry at all, so this work cannot currently reach the thread pool. `undo()` computes a full SHA-256 over every restored file at executor.py:219, again at :485, plus `shutil.copy2`/`shutil.move` for every incumbent; `retry_failed`/`resume_run` invoke the entire `execute()` path — hashing, copying, moving, and the O(n^2) journal writes above. This directly violates the stated invariant that scanning/hashing/transfer work never runs on the Qt main thread.

**Failure scenario:** User completes a live sort of 8,000 photos (~200 GB) onto an external drive, notices a wrong rule, opens History and clicks Undo. The window immediately stops repainting and Windows marks it "Not Responding". There is no progress, no cancel, and no way to tell the app from a hang — every restored file is read end-to-end twice for SHA-256 over USB. The user force-kills it partway, leaving the run half-undone with files split between the destination and the original locations. Retry has the same shape and additionally performs live moves.

**Suggested fix:** Add `undo(run_id, ...)` and `retry(run_id, ...)` methods to `SortController` that wrap the executor calls in `start_task(lambda cancellation, reporter: ...)`, thread an `OperationReporter` and `CancellationToken` through `SortExecutor.undo`/`retry_failed`/`resume_run` (undo currently accepts neither), and have the workspace buttons drive the controller and react to its finished signal instead of calling the executor inline. Also drop the redundant second `_verify_fingerprint` at :485 when the `shutil.move` to the temporary stayed on the same volume.

### 7. [blocker/logic] Sampled "Fast" hashes decide duplicate-skip during import with no full-content confirmation

**Location:** `engine.py:880 (decision), engine.py:90-92 (sampled digest), engine.py:390-394 (build_transfer_hash_settings), src/duplicate_transfer_manager/services/import_workflow.py:36-42,131`

**Claim:** `execute_smart_transfer` treats equality of a sampled digest as proof of duplication. `compute_hash` in `fast` mode hashes only `size + first 1 MiB + last 1 MiB` for files over 2 MiB (engine.py:90-92). The transfer loop then does a bare `if h in dest_hashes:` at engine.py:880 and skips the file. `build_transfer_hash_settings` (engine.py:390-394) upgrades to `full` **only** when `source_is_adb` is true, so a local-source import keeps `fast`. The "Fast" transfer profile sets `hash_mode: "fast"` (import_workflow.py:38) and `duplicate_policy` is hardcoded to `"skip"` (import_workflow.py:131). The scan/quarantine path already fixed exactly this: `group_duplicates` re-hashes every candidate with full SHA-256 at engine.py:464-515, with the comment "A sampled digest is only a candidate filter." The transfer path has no equivalent pass. docs/USER_GUIDE.md:97-100 tells the user Fast "still reads every shortlisted file in full" — that promise is false for imports.

**Failure scenario:** User picks the "Fast" profile and imports from an SD card or an old external drive (`source_kind` != "phone", so no ADB full-hash upgrade). A 400 MB video on the card was trimmed in the middle by an editor that rewrote in place: same byte size, same first MiB (container header), same last MiB (index/footer) as a different clip already in the library. The sampled digests collide, `h in dest_hashes` is true, the file is counted as a duplicate and never copied. The summary reads "Duplicates skipped: 1". The user formats the card. The clip is gone and no report, log line, or journal entry records that it existed.

**Suggested fix:** Mirror the group_duplicates fix in the transfer path: when `hash_settings.hash_mode == "fast"` and `h in dest_hashes`, re-hash the source file and the matching destination file(s) with `sha256`/`full` before classifying as a duplicate — or simply extend `build_transfer_hash_settings` (engine.py:390-394) to force `hash_mode = "full"` for every transfer, not just ADB ones, since the duplicate decision there is unconditionally destructive-by-omission. A cheap middle ground: keep the drive-cache hash index keyed by digest so the candidate's destination path is recoverable, then confirm the pair in full.

### 8. [blocker/logic] Quarantine confirm runs ADB pulls and file moves on the Qt main thread

**Location:** `src/duplicate_transfer_manager/services/duplicate_workflow.py:360-380 (called from src/duplicate_transfer_manager/ui/pages.py:583, 1036)`

**Claim:** `DuplicateQuarantineService.quarantine()` is invoked directly from the `quarantine_button.clicked` slot `_confirm_quarantine` (pages.py:583 -> pages.py:1036). There is no controller and no worker between the button and the service — every other operation in the app goes through `BaseOperationController.start_task`. Inside the loop the service calls `ADBBridge.pull(...)` (duplicate_workflow.py:368) and `shutil.move(...)` (duplicate_workflow.py:373) synchronously. `ADBBridge.pull` is a fully blocking `subprocess.Popen` + `while process.poll() is None: ... time.sleep(...)` poll loop with a 180-second default stall timeout (adb_bridge.py:528-590). This violates the stated invariant that ADB/transfer work never runs on the Qt main thread.

**Failure scenario:** A user scans an Android device, finds 400 duplicate photos totalling 2 GB, and clicks "Quarantine". The app immediately freezes: each duplicate is pulled over USB one at a time on the event-loop thread. At ~30 MB/s that is roughly 70 seconds of a completely unresponsive window with a stale progress panel; Windows paints the title bar "Not Responding" and offers to close the app. If the phone screen locks mid-run, `pull` blocks for the full 180-second stall timeout on a single file before raising, with no cancel button reachable because the event loop is not running. Killing the app at that point leaves the manifest describing files whose moves never completed.

**Suggested fix:** Add a `QuarantineController(BaseOperationController)` alongside the existing `DuplicateScanController`/`TransferController`, and have `_confirm_quarantine` call `controller.start(...)` so the work runs on the thread pool with the existing cancellation token and progress reporter. Thread the `CancellationToken` into `quarantine()` and check it at the top of the per-item loop so a partially completed operation ends cleanly with its manifest flushed.

### 9. [blocker/logic] "Select recommended copies" also checks the Keep row, so confirming quarantine moves every copy including the keeper **FIXED in `fb8271e`**

**Location:** `src/duplicate_transfer_manager/ui/pages.py:859-871, 908-912, 997-1002`

**Claim:** In `_render_next_batch` the Keep radio's `setChecked()` (line 860) runs BEFORE `toggled.connect(...)` (line 861), so `_keep_item()` never fires during rendering. `_keep_item` is the only code that ever calls `check.setEnabled(False)` on the keeper's quarantine checkbox (pages.py:947-956). At initial render every quarantine checkbox is therefore left enabled, including the keeper's. `_select_recommended_duplicates` (908-912) checks *every enabled* checkbox, and `_selected_ids` (997-1002) returns everything checked-and-enabled, so the keeper is passed to `quarantine_service.quarantine()` along with the duplicates.

**Failure scenario:** User scans D:\Photos, gets 300 groups of 3 identical JPEGs, clicks "Select recommended copies" (tooltip at pages.py:554: "Select every copy except the file chosen to keep in each duplicate group"), then "Confirm quarantine". All 900 files are moved into quarantine and D:\Photos is left with zero copies of each photo, instead of the 300 keepers the button promised. The files are recoverable via the Quarantine page, but the user's library is emptied and the recoverable-space figure shown before confirming was also inflated by the keepers' sizes.

**Suggested fix:** Move the `keep.toggled.connect(...)` line above `keep.setChecked(...)` in `_render_next_batch`, or explicitly set `check.setEnabled(item.id != group.keep_item_id)` when the checkbox is created (pages.py:866-867). Also make `_selected_ids` defensively exclude any id equal to its group's `keep_item_id` rather than relying on widget enabled-state as the source of truth.

### 10. [blocker/purpose] Scheduled scan/sort/organizer tasks launch the GUI instead of running, in every packaged install

**Location:** `src/duplicate_transfer_manager/services/support_services.py:122-128, 176-178; src/duplicate_transfer_manager/sorting/scheduler.py:22-24; packaging/installer.iss:37-38`

**Claim:** README.md:40-42 and docs/USER_GUIDE.md:155-157 promise "a daily or weekly **read-only** duplicate scan" that "records findings only". The scheduled command is built as `shutil.which("dtm-scheduled-scan")` with a fallback of `[sys.executable, "-m", "duplicate_transfer_manager.scheduled_scan", ...]` (support_services.py:123-124). In a packaged install neither branch works: packaging/installer.iss:38 ships only `dist\DuplicateTransferManager\*`, so no `dtm-scheduled-scan.exe` console script exists and `which` returns None; and `sys.executable` is then `DuplicateTransferManager.exe`, a PyInstaller windowed build whose entry point is main.py. There is no `sys.frozen` handling anywhere in src/ (grep for `frozen`/`sys.argv` finds only `QApplication(list(argv or sys.argv))` at ui/app.py:30), so `-m duplicate_transfer_manager.scheduled_scan --source ... --data-root ...` is simply passed to Qt and discarded. The same fallback is used for the monitored-folder sort task (sorting/scheduler.py:23) and the organizer preview (support_services.py:177). This is invisible in development, where `pip install -e .` does create the console scripts from pyproject.toml:34-36.

**Failure scenario:** A user installs the signed release, opens Settings → Advanced, sets "Daily read-only duplicate scan" on D:\Photos, and saves. `ScheduledScanService.configure` succeeds (schtasks returns 0), so the UI reports success and persists the setting. Every day at the scheduled time Windows launches DuplicateTransferManager.exe, which opens a full application window on the user's desktop and runs no scan at all. No duplicate_scan record is ever written, so the Activity page stays empty and the user concludes the feature is broken — while a stray app window appears daily. The same happens to every monitored-folder sorting schedule configured through the Advanced panel.

**Suggested fix:** Detect `getattr(sys, "frozen", False)` in `ScheduledScanService.command`, `configure_organizer_preview`, and `SortScheduleService.command`, and in frozen builds emit `[sys.executable, "--run-scheduled-scan", ...]` with a corresponding argv branch in main.py/ui/app.py that dispatches to `scheduled_scan.main()` before any QApplication is created. Alternatively add the console-script shims to the PyInstaller spec as extra EXE targets and ship them in installer.iss. Add a test that asserts the generated command is executable under a simulated frozen `sys.executable`.

### 11. [high/execution-time] Import hashing is entirely single-threaded; the "Worker count" control and the Fast profile's 8 workers do nothing

**Location:** `engine.py:666-699, engine.py:447/482 (the only pools), src/duplicate_transfer_manager/ui/pages.py:1253-1256,1300-1302, src/duplicate_transfer_manager/services/import_workflow.py:40`

**Claim:** The compare-library hashing loop at engine.py:666-699 is a plain sequential `for` loop: one full-file read at a time, no `ThreadPoolExecutor`. `settings.max_hash_workers` is referenced only at engine.py:447 and engine.py:482, both inside `group_duplicates`, which `execute_smart_transfer` never calls. So on any import where the drive cache misses (first ever import, or a count mismatch), the dominant cost — reading every byte of the existing library — runs on one thread while the configured worker count is ignored. This is also a purpose defect: the Import page exposes a "Worker count" spinbox wired straight to `max_hash_workers` (pages.py:1253-1256, added to the form at pages.py:1300-1302), and the Fast profile advertises `max_hash_workers: 8` with the description "Faster comparison for large local libraries" (import_workflow.py:37-40). Neither has any effect on a transfer.

**Failure scenario:** User points a first import at a 150,000-file, 600 GB photo library on an external USB drive or NAS. There is no cache yet, so the engine reads all 600 GB serially. On a NAS at ~80 MB/s effective single-stream throughput that is over two hours before a single file is copied. The user raises "Worker count" to 16 and picks the Fast profile expecting the comparison to speed up, per the profile's own description; wall-clock time is identical because no pool is ever created. On the same hardware, 4-8 concurrent readers would typically cut this several-fold since the bottleneck is per-file latency and request depth, not raw link bandwidth.

**Suggested fix:** Hash the compare folder through a `ThreadPoolExecutor(max_workers=settings.max_hash_workers)` the same way `group_duplicates` does, submitting `compute_hash` per file and collecting into `dest_hashes` and the drive cache as futures complete (keep `stop_event` checks and the `HashCancelled` break). If parallelism is deliberately withheld for a reason, remove the "Worker count" control from the Import page and correct the Fast profile description — right now both mislead.

### 12. [high/execution-time] Restore reads every quarantined file twice with SHA-256 on the Qt main thread

**Location:** `src/duplicate_transfer_manager/services/duplicate_workflow.py:423-424, 435-438 (called from src/duplicate_transfer_manager/ui/pages.py:2424-2431, 2442-2446)`

**Claim:** `restore_selected` and `restore_selected_operation` are plain Qt slots that call `restore_record`/`restore_operation` inline. Per restored file `restore_record` computes `_sha256_path(source)` (line 435) and `_sha256_path(temporary)` (line 437) — two complete reads of the file. With `conflict_policy == "replace"` it additionally does `shutil.copy2` plus `_sha256_path(backup)` and `_sha256_path(resolved)` (lines 423-424), i.e. a full copy and two more full reads of the displaced file. `_mark_restored` (line 527) then re-reads and rewrites the entire operation manifest once per record, so `restore_operation` over N records performs N full manifest read+write+fsync cycles as well. All of it on the event-loop thread.

**Failure scenario:** A user quarantines 800 photos averaging 6 MB (4.8 GB) and later clicks "Restore selected operation". The UI blocks while the app reads ~9.6 GB through SHA-256 (two passes over every file) plus 800 read-modify-write-fsync cycles of a manifest that is ~250 KB by then. On a mechanical external drive this is several minutes of a frozen window with no progress and no cancel, and the "Restore complete: N restored…" banner only appears once everything has finished.

**Suggested fix:** Move restore behind a controller/worker like the other operations, and report per-file progress through the existing `OperationReporter`. Separately, hoist the manifest update out of the per-record loop: have `restore_operation` collect the restored paths and call a single `_mark_restored_bulk` at the end (and on early exit), instead of `restore_record` rewriting the manifest each time.

### 13. [high/execution-time] Duplicate review decodes and re-encodes a thumbnail for every duplicate on the UI thread, with no cache

**Location:** `src/duplicate_transfer_manager/services/duplicate_workflow.py:163-192, 227-235 (called from src/duplicate_transfer_manager/ui/pages.py:775-786)`

**Claim:** `_on_scan_completed` is a Qt slot on the main thread and calls `build_duplicate_review` synchronously (pages.py:780). For every item in every group that function calls `_dimensions` (duplicate_workflow.py:229), which opens the image with PIL, and then `_thumbnail` (line 230), which opens it a second time, fully decodes it, resizes to 96x96, converts to RGB and writes a JPEG (lines 186-189). That is two image opens and one full decode + encode + disk write per duplicate item. `_thumbnail` never checks whether `target` already exists, so re-running the same scan regenerates every thumbnail from scratch. The scan itself was correctly run on a worker (`DuplicateScanController`), but this post-processing is not, and `self.progress_panel.hide()` runs at pages.py:777 before the work starts, so there is not even a spinner.

**Failure scenario:** A user scans a 40,000-photo library and the scan finds 2,500 duplicate 12-megapixel JPEGs. When the worker finishes, the window goes unresponsive for minutes while 2,500 full JPEG decodes and 2,500 JPEG encodes run on the event-loop thread — after the progress panel has already been hidden, so the app looks hung with no indication anything is happening. Re-running the scan, or toggling the keep-preference and re-scanning, pays the whole cost again because nothing is cached.

**Suggested fix:** Do the review build in the worker: return the raw groups from the controller and construct `DuplicateReview` inside the worker task (it is already framework-neutral), or generate thumbnails lazily as table rows scroll into view. At minimum add `if target.exists(): return str(target), ""` at the top of `_thumbnail`, and get the dimensions from the single `Image.open` already performed in `_thumbnail` instead of opening the file twice.

### 14. [high/execution-time] Every duplicate image is decoded twice and a thumbnail JPEG written to disk, all inside the scan-completed slot on the main thread

**Location:** `src/duplicate_transfer_manager/ui/pages.py:787 (calls services/duplicate_workflow.py:229-234, 163-192)`

**Claim:** `_on_scan_completed` is a queued slot on the main thread (WorkerSignals lives on the main thread, so `controller.completed` is delivered there). It calls `build_duplicate_review(...)`, which for every item calls `_dimensions()` — `Image.open(path)` (duplicate_workflow.py:171) — and `_thumbnail()` — a second `Image.open`, `image.thumbnail((96,96))`, `convert("RGB")` and `save(target, "JPEG")` (duplicate_workflow.py:187-189). That is two full image decodes plus one file write per duplicate item, on the UI thread. The scan itself was correctly offloaded; the expensive post-processing was not.

**Failure scenario:** User scans a 200 GB photo library and the scan finishes with 8,000 duplicate JPEG/HEIC files. At the exact moment the progress panel would hide, the app freezes for several minutes doing 16,000 Pillow decodes and 8,000 thumbnail writes, with the progress bar stuck at 100% and no indication anything is still happening. Users conclude the app crashed at the finish line.

**Suggested fix:** Build the review inside the worker task (return the `DuplicateReview` as part of `OperationResult.data` from `DuplicateScanService.run`), or generate thumbnails lazily per visible row in `_render_next_batch` instead of eagerly for the whole result set.

### 15. [high/execution-time] Quarantine restore copies and moves files on the Qt main thread, once per selected record

**Location:** `src/duplicate_transfer_manager/ui/pages.py:2438, 2456 (calls services/duplicate_workflow.py:390-451)`

**Claim:** `QuarantinePage.restore_selected` loops over checked records calling `self.service.restore_record(...)` inline (pages.py:2436-2445); `restore_selected_operation` calls `self.service.restore_operation(...)` inline. Both perform `shutil.copy2` backups and `shutil.move` per file (duplicate_workflow.py:423, 436, 448). No controller is used even though `QuarantineController` exists in controllers/support.py:107 for exactly this. Each call is followed by `self.refresh()`, which re-globs and re-parses every quarantine manifest.

**Failure scenario:** User realises the previous cleanup was wrong, checks 900 quarantined videos (~30 GB) and clicks "Restore quarantine". The UI freezes for the whole restore with no progress, no cancel, and no partial feedback; if the user force-closes the app mid-restore some files sit in the app's temporary move staging with no journal entry telling them where they went.

**Suggested fix:** Run restores through `QuarantineController` (or a new `start_task` wrapper) on the thread pool, emit per-file progress, and refresh the table once at completion instead of after every record.

### 16. [high/execution-time] ADB device enumeration and remote folder listing run as blocking subprocesses on the Qt main thread

**Location:** `src/duplicate_transfer_manager/ui/pages.py:685, 641, 1474, 1429`

**Claim:** `DuplicatesPage._refresh_devices` (pages.py:681-695) and `ImportPage._refresh_devices` (pages.py:1470-1485) call `ADBBridge.list_devices()` synchronously. That does `start-server` (subprocess, 3 s timeout), `adb devices -l` (3 s timeout), plus one `adb shell getprop ro.product.model` per unnamed device (3 s each) — adb_bridge.py:104-218. `_browse_adb_path` (pages.py:641, 1429) similarly calls `ADBBridge.get_directory_structure` with an 8 s `adb shell ls` timeout. `_refresh_devices` is invoked straight from `_source_changed`, i.e. the instant the user clicks the "Android phone" source card. `DeviceController` (controllers/support.py:58-79) exists to do exactly this off-thread and is never instantiated anywhere in the UI.

**Failure scenario:** With a phone plugged in but the adb server not yet running (or the phone locked and unresponsive), the user clicks the "Android phone" card. The window locks for 6-12 s with no cursor change or busy indicator; clicking Browse then locks it for another 8 s while `adb shell ls` times out. Users assume the click did not register and click repeatedly, queueing more blocking ADB calls.

**Suggested fix:** Instantiate `DeviceController` on both pages and populate `device_choice` from its `completed` signal; do the same for folder browsing via a small `run_loader` task, showing a spinner while it runs.

### 17. [high/execution-time] The sorting executor rewrites the entire run journal once per file

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:144 and 178 (writer at 557-569)`

**Claim:** `_write_journal` is called inside the per-item loop at executor.py:144 and 178. It serializes the whole journal — `planned_items` for every selected file plus every record accumulated so far — with `json.dump(..., indent=2)` into a fresh `mkstemp` file and `os.replace`s it, on every single item. That is O(n²) bytes written and O(n²) JSON serialization. Two more per-item O(n) costs sit alongside it: `remaining = sum(value.metadata.size for value in selected[index:])` at executor.py:181 copies a list slice and sums it every iteration, and `total_bytes=sum(value.metadata.size for value in selected)` at executor.py:185 re-sums the full plan every iteration. The overwrite path adds a directory listing per conflict (`len(list((run_root / 'replaced').glob('*')))`, executor.py:355), as does `_restore_for_undo` at executor.py:471.

**Failure scenario:** A user sorts a 20,000-file photo library. Each record is roughly 600 bytes of indented JSON, so the journal reaches about 12 MB by the end and the average rewrite is about 6 MB — roughly 120 GB written to the SSD and 200 million dict-to-JSON conversions across the run, for a job whose actual file moves are a few hundred GB of reads at best and near-zero for same-volume renames. On a same-drive sort where `os.replace` is instant, journal bookkeeping dominates total wall clock and the operation takes hours instead of minutes. The per-item sums add another 200 million iterations plus 20,000 list-slice allocations.

**Suggested fix:** Append records to a JSON Lines sidecar (`records.jsonl`) as they complete and rewrite `journal.json` only at start, on cancellation, and at the end — `list_runs`/`load_run` can fold the sidecar in, keeping the existing schema readable. Hoist `total_bytes` out of the loop and track `remaining` as a running subtraction instead of re-summing a slice. Replace the `glob('*')` counters at 355 and 471 with an incrementing counter held for the run.

### 18. [high/logic] resume_run re-executes imported legacy operations the user already rolled back

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:290-294; src/duplicate_transfer_manager/sorting/migration.py:88; src/duplicate_transfer_manager/ui/sort_workspace.py:1528`

**Claim:** `resume_run` builds its terminal set from `record.get("status") in {"completed", "ignored", "skipped", "previewed"}` and resumes everything else. `SortingMigrationService._record` (migration.py:88) emits `status = "previewed" if dry_run else "undone" if value.get("restored_at") else "completed"` — the literal `"undone"` is not in that terminal set. Legacy manifests are imported into Sort history automatically at startup (ui/app.py:49). The migrated planned_items carry accurate `size` and `modified` copied from `OrganizationPlanItem` (core/contracts.py:150-151), and `OrganizerService.rollback` restores with `shutil.move` back to `source_path`, preserving mtime — so `_execute_item`'s staleness guard at executor.py:339 passes. The History Retry button calls `resume_run` whenever the selected run has no `"failed"` records (sort_workspace.py:1528), which is always true for imported legacy runs, and passes `confirmed=True`.

**Failure scenario:** User previously used the old File Organizer, then rolled the operation back because the folder layout was wrong. On upgrade, that manifest is imported as run `sort_legacy_<id>` and appears in Sort Files → History. The user selects it (or is just exploring) and clicks Retry. `retry_failed` is skipped because there are no failed records, so `resume_run` runs: it rebuilds a MOVE plan from the legacy destinations, `confirmed=True` bypasses the destructive-plan gate, and every file the user deliberately restored is silently moved back into the organizer layout they rejected — with no Review step and no plan preview.

**Suggested fix:** Treat a record as terminal when it is not resumable rather than enumerating done-states: add `"undone"` and `"legacy_imported"` to the set, and also skip any record carrying `undone_at`. Better, gate `resume_run`/`retry_failed` on `journal.get("status") != "legacy_imported"` and disable the Retry/Undo buttons for imported legacy runs in `sort_workspace._refresh_history`, since those runs were never executed by this engine.

### 19. [high/logic] Files skipped by the filename-conflict policy are reported to the user as duplicates

**Location:** `engine.py:920-923, engine.py:955-958, engine.py:976-978, engine.py:1114, src/duplicate_transfer_manager/services/import_workflow.py:192`

**Claim:** The `skipped` counter is incremented for two semantically opposite outcomes. At engine.py:906 it means "this file's content already exists in the library" (a genuine duplicate). At engine.py:921, 956, and 977 it means "a *different* file with the same name already occupies the target path and `conflict_policy` is `skip`" — the source file is unique and was never copied. Both feed the same counter, and engine.py:1114 reports `"duplicates": isolated + skipped`. The UI summary row is literally labelled "Duplicates skipped" (import_workflow.py:192), and the engine log says `Duplicates Skipped: {skipped}` (engine.py:1061). Conflict skips are not added to `failures`, so they never appear in the transfer report's failure list either — the only trace is a `SKIPPED existing target due to conflict policy` line buried in the activity log.

**Failure scenario:** User sets "Skip existing filename" and imports two phones into one library with `destination_template="date"`. Both phones produced `IMG_0042.jpg` in the same month, with completely different photos. Phone B's `IMG_0042.jpg` hits the existing name, `resolve_conflict_path` returns `""`, and the file is never copied. The summary reports "New files copied: 900 / Duplicates skipped: 41". The user reads that as "41 photos were already in my library", confirms the import succeeded, and wipes phone B. All 41 unique photos are lost.

**Suggested fix:** Add a separate `conflicts` counter, increment it at engine.py:921/956/977 instead of `skipped`, return it in the result dict, and surface it as its own summary row ("Skipped — filename already in use"). Also append these to `failures` so `write_transfer_report` records the source paths. Keep `duplicates = isolated + skipped` meaning content duplicates only.

### 20. [high/logic] Drive-cache "count matches" fast path trusts cached hashes without per-file size/mtime validation

**Location:** `engine.py:643-660, drive_cache.py:150-179`

**Claim:** When `cached_count == total_dest` (engine.py:649), the whole compare phase is replaced by `drive_cache.hashes_under_root(...)` (engine.py:653). `hashes_under_root` → `entries_under_root` (drive_cache.py:150-179) filters only on path prefix, `stale`, `algo`, and `mode`. It never compares the cached `size`/`mtime` against what is actually on disk — unlike the slow path, which calls `get_valid_hash(f.path, algo, mode, f.size, f.created)` per file (engine.py:670-676) and does validate. So the fast path is strictly less safe than the slow one, and a file count is a one-dimensional check that any equal-sized add/delete pair defeats. The discovered `FileInfo` objects already carry `size` and `created` (discovery.py:247), so the validating comparison costs nothing extra.

**Failure scenario:** User's library drive is cached (either by a prior import or by `build_drive_cache.py`). Between runs, outside the app, they move 300 old photos to an archive drive and drop 300 new photos in. File count is unchanged, so `cache_count_matches` is true and the entire cache is used verbatim. `dest_hashes` now contains 300 hashes of photos that are no longer in the library and is missing the 300 that are. On the next phone import, any source photo identical to one of the 300 archived files is reported as a duplicate and never copied — even though the library no longer contains it. The user sees "Duplicates skipped" and deletes them from the phone.

**Suggested fix:** Replace the count comparison with a set comparison: build `{normalize_path(f.path): (f.size, f.created)}` from `dest_files` and take the fast path only if every discovered path has a non-stale cache entry with a matching size and mtime. Given the discovery scan already stat'd every file, this is the same cost as the count check and closes the hole. Failing that, drop the fast path entirely and rely on the per-file `get_valid_hash` loop, which already avoids re-reading unchanged files.

### 21. [high/logic] Preflight free-space check sums every source file, so repeat imports and resumes are hard-blocked

**Location:** `transfer_safety.py:207-213, engine.py:582-602, engine.py:770-777`

**Claim:** `preflight_transfer` computes `required = sum(info.size for info in source_files)` over the *entire* source listing and appends a hard `error` (not a warning) if `free < required` (transfer_safety.py:207-211). `execute_smart_transfer` returns immediately on any preflight error with `preflight_failed: True` (engine.py:591-602). But the two mechanisms that make the real requirement far smaller — the journal resume check (engine.py:770-777) and the duplicate check (engine.py:880) — both live *inside* the per-file loop, which is never reached. The engine therefore refuses to start transfers it would complete comfortably, and the failure message ("Insufficient free space: need up to N bytes") points the user at a problem that does not exist.

**Failure scenario:** User imports a 256 GB phone into a library drive with 90 GB free. The first import copies 180 GB across two sessions and is cancelled. On the third session, or on any later top-up import of the same phone, preflight sums all 250 GB of phone content, sees 90 GB free, and blocks with "Transfer did not start". The journal already records 180 GB as complete and only ~5 GB of new photos actually need copying. The documented resume feature is unreachable exactly when it matters most — on a nearly-full destination.

**Suggested fix:** Move the free-space check after the journal-resume and duplicate filtering, or pass the journal into `preflight_transfer` and exclude already-complete sources from `required`. At minimum, demote the shortfall to a warning when a journal exists for this output root, since `required` is a strict upper bound that the engine knows is loose. Keep the hard error only for the case where even the largest single source file will not fit.

### 22. [high/logic] Dry-run and failed quarantine records are indistinguishable from real ones in every listing

**Location:** `src/duplicate_transfer_manager/services/duplicate_workflow.py:366, 377, 484-503; src/duplicate_transfer_manager/core/contracts.py:187-201`

**Claim:** `quarantine()` records a per-item `status` of `"previewed"` on a dry run (line 366) or `"failed"` when the move/pull raises (line 377), and writes those records into the operation manifest. But `list_records()` (lines 493-495) returns every record in `payload["records"]` with no status filter, and `QuarantineRecord.from_dict` (contracts.py:188-201) does not carry `status` at all, so the information is destroyed at the boundary. `QuarantineService.list_records` in support_services.py:330-352 has the same gap. Consequently `QuarantinePage.refresh` shows preview-only and failed records as "Ready to restore" (pages.py:2318-2322) and adds their sizes to the "Recoverable space" card, and `DashboardService.summary` counts them into `quarantine_count` and `recoverable_bytes` (support_services.py:587-594, 611-612). Separately, a dry run still calls `operation_root.mkdir(parents=True, exist_ok=False)` (line 307) and writes a real manifest, so a "preview" permanently creates a quarantine operation — contradicting the confirmation dialog text at pages.py:1022 ("without moving local files or pulling Android copies").

**Failure scenario:** A cautious user ticks "Dry run quarantine" on 600 duplicates to see what would happen. The dialog says nothing will be moved. They then open the Quarantine page and it reports "600 files in quarantine, 3.2 GB recoverable space, 1 operation", and the Dashboard shows the same 3.2 GB as recoverable. Their originals are all still in place. They tick everything and click "Restore quarantine" to undo what they think happened, and get "Restore complete: 0 restored, 0 skipped, 600 failed" — six hundred lines of "quarantined file is missing". Nothing is damaged, but the app has reported a fictitious quarantine state and a fictitious 3.2 GB of recoverable space.

**Suggested fix:** Add a `status` field to `QuarantineRecord` (defaulting to `"completed"` so existing manifests keep loading) and populate it in `from_dict`/`to_dict`. Filter `list_records()` in both `DuplicateQuarantineService` and `QuarantineService` to `status == "completed"` by default, with an explicit opt-in for showing previewed/failed rows. For dry runs, write the preview manifest under a distinct name (e.g. `dry_run_manifest.json`) or a `"dry_run": true` operation directory that the listing glob does not pick up.

### 23. [high/logic] Closing the main window neither cancels nor waits for in-flight scan/import/sort workers

**Location:** `src/duplicate_transfer_manager/ui/shell.py:350-352`

**Claim:** `MainWindow.closeEvent` only saves settings and calls `super().closeEvent(event)`. It does not check `self.pages[...].controller.busy`, does not call `controller.cancel()`, and does not `wait_for_done()`. `BaseOperationController.wait_for_done` exists (controllers/base.py:188-191) but is never called from the UI. `launch_verified_update` (shell.py:354-380) proves the code knows how to check `controller.busy` — the close path does not.

**Failure scenario:** A user is 20 minutes into a 50 GB Android import and clicks the window's X. Settings are saved, the window disappears, `application.exec()` returns and `run()` returns — but the pool thread is still copying. The global QThreadPool destructor blocks interpreter shutdown, so the process lingers invisibly in Task Manager still writing files while the user believes the app is closed; if it is killed at that point the transfer journal's last checkpoint is stale and partially written files remain in the destination. A worker emitting into a `WorkerSignals` QObject during interpreter teardown can also abort the process.

**Suggested fix:** In `closeEvent`, if any controller is busy, prompt the user; on confirm call `cancel()` on each busy controller and `wait_for_done(timeout)` before accepting the event, otherwise `event.ignore()`.

### 24. [high/logic] A 30-second timer walks monitored folders on the main thread and silently discards the user's reviewed sort plan

**Location:** `src/duplicate_transfer_manager/ui/sort_workspace.py:460-463, 1457-1472, 926-935, 991-1006`

**Claim:** `self.change_timer` fires `_poll_change_monitors` every 30 s on the GUI thread. For each enabled `filesystem_change` monitor it calls `SortMonitorService.poll` (sorting/monitor.py:28-48), which does `root.rglob("*")` with `path.stat()` and `path.resolve()` per file, runs `MetadataExtractor.extract` on every changed file, then writes a JSON snapshot containing an entry for every file found. It then calls `_add_sources(added)` (line 1470), which calls `_invalidate_plan(...)` (sort_workspace.py:934). `_invalidate_plan` cancels an in-flight preview (line 992-996) or, if a plan exists, sets `self.plan = None` and clears `review_table` (999-1006).

**Failure scenario:** User builds a sort review over 30,000 files, spends five minutes ticking the rows they want, and pauses to check something. The 30-second monitor timer fires, finds one new file in a monitored Downloads folder, adds it as a source and wipes the plan: the review table empties, every approval the user made is gone, and the banner says only "The selected sources changed." Separately, on a monitored folder of 100k files, each tick freezes the UI for the duration of a full recursive walk plus a 100k-entry JSON write, every 30 seconds.

**Suggested fix:** Run monitor polling on the thread pool (a `DataController.run_loader` task fits), and never let a background poll mutate the active session: queue detected files and surface them as a dismissible "3 new files found — add to this run?" banner instead of calling `_add_sources` directly.

### 25. [high/logic] An I/O error during a transfer permanently leaves the PC awake and the phone's stay-awake setting changed

**Location:** `engine.py:604 and engine.py:1095-1111 (no try/finally between them)`

**Claim:** `execute_smart_transfer` acquires two process-external side effects — `prevent_windows_sleep()` at engine.py:604 and `ADBBridge.enable_usb_stay_awake(...)` at engine.py:757-759, which runs `svc power stayon usb` and persists on the device (adb_bridge.py:258-277) — and releases them only by falling through to engine.py:1095-1111. There is no try/finally: the only function-level `try:` after line 604 is the `os.rmdir` at line 1105. Several statements in the main loop are outside any `except`: `os.makedirs(os.path.dirname(target_path), exist_ok=True)` inside `build_target_path` (engine.py:160, 166, called at engine.py:908 before the `try` at 926), `os.makedirs(settings.isolate_folder, ...)` at engine.py:890, and `journal.complete(...)` / `journal.fail(...)` at engine.py:1010 and 848, which call `TransferJournal.save` → `os.makedirs` + `open` (transfer_safety.py:61-68). Any OSError from these escapes the function.

**Failure scenario:** A user imports 2,000 photos from a phone to an external USB drive. Halfway through, the drive is bumped and disconnects. The next `build_target_path` call raises `OSError: [WinError 3]` from `os.makedirs`, which propagates out of `execute_smart_transfer` uncaught. Result: the transfer report at engine.py:1130 is never written, the drive and ADB caches are never saved so all the hashing work is thrown away, the staging directory is left behind, `SetThreadExecutionState` is never cleared so Windows will not sleep again until the app is closed, and the phone's `stay_on_while_plugged_in` global setting stays modified — its screen now stays on every time it is plugged in, until the user finds and changes that setting by hand.

**Suggested fix:** Wrap everything from engine.py:604 to the summary block in `try: ... finally:` and move the teardown (restore_windows_sleep, restore_stay_awake, journal.save(force=True), cache saves, staging rmdir, report write) into the `finally`. Independently, put `journal.complete`/`journal.fail` and the `os.makedirs` calls behind the same `except OSError` handling the copy path already uses, so a single unwritable path degrades to a per-file failure rather than aborting the run.

### 26. [high/logic] The drive/ADB cache lock is one-sided, so the scheduled refresh and the app can clobber or corrupt the same cache file

**Location:** `drive_cache.py:63-91 and drive_cache.py:278 vs engine.py:399-420, 1074-1091`

**Claim:** `build_drive_cache` — the entry point the shipped scheduled task uses (schedule_adb_cache.ps1 generates a weekly task running `build_drive_cache.py --adb --serial ... --root /sdcard`) — takes an exclusive lock at drive_cache.py:278 and releases it at 302. Nothing else in the codebase ever calls `acquire_lock`; `engine.load_drive_cache`/`load_adb_cache` (engine.py:399-420) and the `drive_cache.save()`/`adb_cache.save()` calls at engine.py:1077 and 1089 do not. Both processes resolve to the identical path via `default_adb_cache_path(serial)` / `default_cache_path(root)`, so the lock protects nothing. Worse, `DriveHashCache.save` writes to a fixed `f"{self.path}.tmp"` (drive_cache.py:68) rather than a unique temporary, so two writers share one scratch file before `os.replace`. `load` treats any `json.JSONDecodeError` as "start empty" (drive_cache.py:60-61), so corruption is silent. `acquire_lock` also has no stale-lock recovery and only catches `FileExistsError`.

**Failure scenario:** The weekly cache refresh fires at 03:00 and takes 40 minutes on a 60,000-photo phone. The user starts an import at 03:10. Both processes hold the same cache in memory, both write `<cache>.json.tmp`, and the interleaved writes are `os.replace`d into place. The next run calls `load()`, hits a JSONDecodeError, silently resets to empty data, and logs "ADB cache loaded ... 0 files, 0 hashes" — so the app re-hashes every file on the phone, an operation the cache exists specifically to avoid. Separately, if the machine sleeps or loses power while `build_drive_cache` holds the lock, the `.lock` file is never removed and every subsequent weekly refresh raises `RuntimeError("Cache is locked")` from a scheduled task whose output nobody sees.

**Suggested fix:** Have `load_drive_cache`/`load_adb_cache` acquire the same lock for the duration of the run and skip cache updates (read-only mode) when it cannot be taken, or move both writers onto a single lock-plus-merge helper. Switch `save()` to `tempfile.mkstemp(dir=...)` like `_atomic_json_write` in services/support_services.py:38 so concurrent writers cannot share a scratch file. Give `acquire_lock` a PID/timestamp payload and a staleness timeout, and catch `OSError` rather than only `FileExistsError`.

### 27. [high/purpose] prune_runs permanently deletes the only copies of overwritten user files, contradicting its own docstring and the user guide **FIXED in `fcfb784`**

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:265-277 (docstring at :266); called at ui/app.py:50 and ui/pages.py:2695`

**Claim:** `prune_runs` does `shutil.rmtree(journal.parent)` on any run directory older than `sorting_history_retention_days` (default 90, core/contracts.py:121). That directory contains `replaced/` — the incumbent files displaced by Overwrite/Keep-Newest/Keep-Largest conflicts (executor.py:355-358) — and `undo_backups/`. Those are the user's own files, and after an overwrite they are the *only* remaining copy. The rmtree is a hard delete, not a Recycle Bin send (contrast `SortAction.RECYCLE` at executor.py:342-346, which does use send2trash). The docstring on line 266 claims "Remove only expired app-owned journals/backups, never sorted files," which is false for `replaced/`. It also runs unattended at every application start (ui/app.py:50) with no prompt. docs/USER_GUIDE.md states "Every live run writes a journal and can be undone," and its Settings section describes only cache retention as safe to lose ("costs you speed, not any data") while explicitly promising quarantined files are "not cleaned up on a timer" — the retention spinbox label "days sorting history and undo retention" reads as losing the ability to reverse a move, not as destroying a file.

**Failure scenario:** In January the user sorts a scanned-document folder with the conflict policy set to Overwrite; 40 older versions of files are displaced into `runs/sort_2026.../replaced/`. In May they discover one of those older versions was the one they actually wanted. The run has aged past 90 days, so the app silently rmtree'd it during a routine launch. Undo reports the run as unavailable, and the file is not in the Recycle Bin or in Quarantine — it is unrecoverable.

**Suggested fix:** Never let retention pruning touch `replaced/` or `undo_backups/`. Either exclude those subdirectories from the rmtree and keep them until the user explicitly clears them (matching the quarantine promise the user guide already makes), or route them through `send2trash` so they land in the Recycle Bin. Fix the docstring, and state the behaviour next to the retention spinbox and in the user guide's Settings section.

### 28. [high/purpose] Startup pruning permanently deletes the only surviving copy of overwritten user files

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:271-283 and 362-393; src/duplicate_transfer_manager/ui/app.py:50; src/duplicate_transfer_manager/ui/pages.py:2709`

**Claim:** docs/USER_GUIDE.md:9 states the first of the app's two headline promises: "**Nothing is deleted.**" But when a Sort Files run resolves a conflict with Overwrite, executor.py:389-392 copies the incumbent destination file into `runs/<run_id>/replaced/`, verifies the copy, then calls `destination.unlink()` — that backup is now the only copy of the user's original file. `SortExecutor.prune_runs` (executor.py:271-283) then does `shutil.rmtree(journal.parent)` on any run directory whose journal.json mtime is older than the retention window, which removes `replaced/` along with the journal. It is called unconditionally at every launch (ui/app.py:50) and again on every Settings save (pages.py:2709), with no confirmation and no notice. docs/SORT_FILES_ARCHITECTURE.md:145-146 acknowledges it deletes "preserved overwrite backups" but characterises this as "never delet[ing] a sorted user file" — the backup is a user file, and it is the only one left.

**Failure scenario:** A user sorts a photo library with an Overwrite conflict policy in March. Twelve edited JPEGs are overwritten by same-named files; the originals are preserved in `sorting/runs/sort_.../replaced/` and the history row shows "Undo: Available". The user never opens Sort Files again. In June they launch the app for an unrelated import; `prune_runs(90)` fires during startup and rmtree's the run directory. The twelve original photos are gone permanently — not in quarantine, not in the Recycle Bin, with no record and no prompt, from an app whose user guide opens by saying nothing is deleted.

**Suggested fix:** Make `prune_runs` delete only journals and metadata, never `replaced/`; move surviving overwrite backups into the app quarantine (with a QuarantineRecord so they appear on the Quarantine page and can be restored) before removing the run, or refuse to prune any run that still has un-undone `replaced_backup` entries. If retention on backups is genuinely wanted, prompt for it and say so in README/USER_GUIDE rather than relying on a line in the architecture doc.

### 29. [high/purpose] The entire update feature is unreachable; PRIVACY.md describes network requests the app never makes

**Location:** `src/duplicate_transfer_manager/controllers/support.py:168-183; src/duplicate_transfer_manager/services/support_services.py:694-704, 732-760, 862, 941; src/duplicate_transfer_manager/ui/shell.py:354-380; src/duplicate_transfer_manager/core/contracts.py:106,109`

**Claim:** PRIVACY.md:10-16 states "The application makes network requests in exactly two situations" and that "If 'Check for updates automatically' is enabled, the app fetches a signed update manifest no more than once per day". README.md:53-54 and CHANGELOG.md:56-57 make the same assumption. No such code path exists in the running app: `UpdateController` is never constructed anywhere in src/ (grep finds it only in controllers/__init__.py and tests/test_phase1.py:398), and even if it were, `UpdateController.check()` (support.py:178-183) calls only `service.status()`, which performs no network I/O and hardcodes `"channel": "stable"` (support_services.py:698) regardless of `AppSettings.update_channel`. `check_manifest_url`, `download_installer`, and `launch_verified_installer` have no caller in src/ except `MainWindow.launch_verified_update` (shell.py:354), which itself has no caller. `AppSettings.check_updates_automatically` (contracts.py:109) is only written and re-read by the two checkboxes that set it (pages.py:2568-2569/2674 and 2806-2807/2817). The verification code in support_services.py is real and well-tested — it is simply never reached.

**Failure scenario:** A user reads PRIVACY.md, decides they do not want the app contacting GitHub, and unchecks "Check for updates automatically" — which changes nothing, because nothing ever checked. A different user leaves it on, picks the "Beta" update channel in Advanced Settings (pages.py:2613-2626), and waits. No check ever runs, no update is ever offered, and the channel selection is inert; when 0.9.0 ships they stay on 0.8.0 indefinitely with no indication anything is wrong. The security posture documented in SECURITY.md:31-45 also never engages, because no manifest is ever fetched.

**Suggested fix:** Either wire it up — construct `UpdateController` in MainWindow, gate a periodic/manual check on `settings.check_updates_automatically`, pass `settings.update_channel` into `check_manifest_url` and into `UpdateService.status()`, and add a "Check for updates" action that reaches `launch_verified_update` — or, if the update flow is deliberately deferred to a later release, remove the dead "Update channel" and "Check for updates automatically" controls and correct PRIVACY.md:10-21 to say the app makes no network requests at all.

### 30. [high/purpose] Simple/Advanced mode only hides one card on the Settings page; every other advanced control is always visible

**Location:** `src/duplicate_transfer_manager/ui/pages.py:2731-2732, 2540, 2659, 470-506, 1249-1319; src/duplicate_transfer_manager/ui/sort_workspace.py:436-440`

**Claim:** docs/USER_GUIDE.md:18-20 says "Choose **Simple** or **Advanced** mode in Settings. Simple is the default and hides technical controls. Advanced reveals them"; docs/ARCHITECTURE.md:121-122 repeats "Advanced mode reveals technical controls". `AppSettings.experience_mode` has exactly two consumers in the whole application: the combo box that displays it (pages.py:2540) and the assignment that saves it (pages.py:2659). The only behaviour attached to it is `_sync_advanced_visibility` (pages.py:2731-2732), which toggles `self.advanced_settings` — the Settings page's own card. The duplicate scan's hash algorithm, hash mode, thread count, minimum size, and exclusion controls (pages.py:470-506), the import page's hash mode, worker count, retries, conflict policy, cache toggles, dry-run, reconnect and stall timeouts (pages.py:1249-1319), and the Sort Files profile/rule/ML/automation panel (sort_workspace.py:436-440) are all built unconditionally and shown in both modes; no page reads `experience_mode` at all, and none is passed the setting for that purpose.

**Failure scenario:** A non-technical user completes onboarding in the default Simple mode and opens Find Duplicates expecting the promised simplified surface. They see an "Advanced" disclosure containing "Hash algorithm", "Hash mode", "Threads", and "Excluded folders", expand it out of curiosity, and set Hash mode to "Fast" — a mode the same app documents as usually slower for photos and videos (USER_GUIDE.md:96-101). Simple mode gave them none of the protection it advertised. Conversely, switching to Advanced changes nothing outside Settings, so a user who deliberately opts into Advanced sees no new capability and reasonably concludes the toggle is broken.

**Suggested fix:** Pass `AppSettings` through to `DuplicatesPage`, `ImportPage`, and `SortWorkspace` visibility logic (they already receive `settings`) and hide or collapse the advanced `DisclosurePanel`s when `experience_mode == "simple"`, refreshing them from `MainWindow._preferences_saved` (shell.py:326-328). Add a UI test asserting that the duplicate and import advanced panels are hidden in Simple mode and visible in Advanced. If per-page gating is not wanted, delete the mode selector and correct USER_GUIDE.md:18-20 and ARCHITECTURE.md:121-122.

### 31. [high/purpose] The documented legacy Tkinter frontend moves duplicates mid-scan with no review, no quarantine, and no restore record

**Location:** `ui_components.py:494-502, 615-637; src/duplicate_transfer_manager/services/duplicate_workflow.py:487; README.md:114-118`

**Claim:** docs/USER_GUIDE.md:9-12 states the app's two invariants — "**Nothing is deleted.** Duplicates go into quarantine and can be brought back" and "**Nothing runs without review.** Every operation shows you what it will do before it does it" — and README.md:188-190 says "Duplicate quarantine can move local files only after scan, review, and explicit confirmation." README.md:114-118 nevertheless documents `python legacy_main.py` as a supported way to launch the product. That frontend does neither: `OrganizerFrame._run_scan` moves duplicates inside the scan loop with `shutil.move(dup.path, target)` (ui_components.py:633) to a plain user-chosen "isolate folder", one group at a time as they are discovered. There is no review step (the only gate is a yes/no dialog *before* the scan starts, ui_components.py:495-502, which cannot list files that have not been found yet), no manifest is written, and nothing is recorded through `OperationRecordService`. Because `DuplicateQuarantineService.list_records` reads only `paths.quarantine/*/manifest.json` (duplicate_workflow.py:487), these files are invisible to the Quarantine page and cannot be restored by the app.

**Failure scenario:** A user follows README.md:114-118, launches the Tk frontend, points it at D:\Photos, sets an isolate folder, clicks through the one confirmation, and starts a scan. Twenty minutes later 4,000 files have already been moved out of their original folders into one flat directory, renamed by `ensure_unique_path` wherever names collided — before the user has seen a single duplicate group. They then open the PySide6 app's Quarantine page to undo it and it is empty, because no quarantine manifest exists. The original folder structure is unrecoverable from within the product.

**Suggested fix:** Either remove `legacy_main.py`/`ui_components.py` and the README.md:114-118 launch instructions (and drop the now-unused `tkinterdnd2` hard dependency at pyproject.toml:24), or route `OrganizerFrame` through `DuplicateQuarantineService.quarantine` after a review step so the same manifest, restore path, and activity record apply. Do not leave a documented launch path whose duplicate handling contradicts the product's stated safety model.

### 32. [medium/execution-time] Every plan preview and every History refresh parses every sorting journal ever written

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:256-263; workflow.py:75-87; ui/sort_workspace.py:1482 and :1567`

**Claim:** `list_runs()` does `json.loads(path.read_text())` on every `runs/*/journal.json`, and each journal embeds full `planned_items` plus `records` — measured at ~7.5 MB for a 5,000-file run and ~30 MB for a 20,000-file run. `HybridSortService.prepare` calls it unconditionally via `_prior_destinations` on every preview build, including when `profile.ml_enabled` is False (the quick/Simple profile hardcodes `ml_enabled=False`, presets.py:127) — so the entire result is parsed and thrown away. Worse, `_refresh_history` calls `list_runs()` at sort_workspace.py:1482 and then immediately calls `_refresh_stats`, which calls `list_runs()` again at :1567 — the full history is parsed twice per refresh, on the Qt main thread. Journal volume also grows fast: scheduled_sort.py:28 writes a run directory on every scheduled poll even when zero files changed, so hourly monitoring produces ~2,160 journals inside the 90-day retention window.

**Failure scenario:** A user with hourly folder monitoring and a handful of large manual sorts opens Sort Files → History. The app reads and JSON-parses several hundred megabytes across ~2,000 journal files twice in a row on the UI thread; the window locks for many seconds. The same cost is then paid again on the worker thread every time they build a preview, adding an unexplained delay after "Scanning" completes for suggestion data that a Simple-mode profile discards outright.

**Suggested fix:** Give `list_runs` a summary mode that reads only the header fields (status, run_id, created_at, counts) — cheap once records live in a sibling `records.jsonl` — and have `_refresh_history` pass its already-loaded list into `_refresh_stats` instead of re-fetching. Skip `_prior_destinations` entirely when `not profile.ml_enabled`, and bound it to the newest N runs before opening any file. Have `scheduled_sort` return early without creating a run when `changed` is empty.

### 33. [medium/execution-time] ml_feedback.json is re-read and re-parsed once per unmatched file during planning

**Location:** `src/duplicate_transfer_manager/sorting/ml.py:115-117 (via :108-113), driven from planner.py:57-63`

**Claim:** `SortPlanner.build` calls `self.ml.predict(...)` inside its per-file loop for every file no association matched. With `ml_enabled` true (the default, models.py:169) and no injected classifier, `predict` reaches `_matching_feedback`, which calls `self.list_feedback()` — a fresh `read_text()` plus `json.loads()` plus construction of an `MLFeedback` object for every stored correction — on every single call. Nothing is cached across the loop. The cost is O(files x corrections) in parsing plus one file-open syscall per file even when the feedback file does not exist. `record_correction` (ml.py:97-106) has the same shape on the write side: it reloads the whole list to append one entry.

**Failure scenario:** User runs an advanced profile with two narrow rules over a 100,000-file Downloads/Pictures tree. Nearly every file falls through to ML, so the app opens and fully parses ml_feedback.json 100,000 times. After a few months of Review corrections the file holds ~500 entries (~150 KB), so this is ~15 GB of redundant reads and 100,000 JSON parses — minutes of pure CPU added to the preview with the progress text still reading "Matching associations and local suggestions…".

**Suggested fix:** Load the feedback list once in `LocalMLService.__init__` or lazily on first use, keyed by mtime for invalidation, and index it as `dict[(extension, source_parent)] -> MLFeedback` so `_matching_feedback` is an O(1) dict lookup. Have `record_correction` append to the in-memory index and rewrite the file.

### 34. [medium/execution-time] Cross-volume moves hash the source three times and the destination twice

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:341 vs :428-437 and :364`

**Claim:** `_execute_item` computes `fingerprint = self._fingerprint(source, control)` at :341. On the EXDEV/EACCES/EPERM fallback path `_move` then does `self._verify_fingerprint(destination, self._fingerprint(source, control))` at :436 — recomputing the identical digest of the same unchanged source file, which is already sitting in the local variable one frame up but is never passed in. Control returns to `_execute_item`, which verifies the destination *again* at :364 against the same expected value. Net per cross-volume move: source read 3x, destination read 2x, destination written 1x — 6N bytes of I/O where 3N is sufficient. For same-volume moves, `os.replace` never touches the bytes yet the file is still hashed once before (:341) and once after (:364), so a pure rename costs 2N of reads.

**Failure scenario:** User moves a 400 GB video library from an internal drive to an external USB 3.0 disk. The transfer reads 1.2 TB from the source and 800 GB back from the USB disk instead of 400 GB each way, roughly doubling a multi-hour operation. On a same-drive reorganization of 500 GB of photos, the app reads 1 TB purely to hash files whose bytes were never copied.

**Suggested fix:** Pass the already-computed `fingerprint` into `_move` and use it for the post-copy verification instead of re-hashing the source. Drop the duplicate `_verify_fingerprint` at :364 for the branch that already verified inside `_move`. For same-volume `os.replace`, verifying the destination is redundant with verifying the source — record the source fingerprint (undo needs it) but skip the post-rename re-read.

### 35. [medium/execution-time] Planner re-stats and re-resolves the source roots for every single file

**Location:** `src/duplicate_transfer_manager/sorting/planner.py:96 (also :52)`

**Claim:** Inside the per-file loop, `any(Path(value).is_dir() and _within(destination, Path(value).resolve()) for value in sources)` reconstructs a Path, stats it, and calls `resolve()` for every entry in `sources` on every file. `sources` is loop-invariant. On Windows `Path.resolve()` is not free — it goes through `_getfinalpathname`, which opens a handle on the path — so this is roughly two syscalls per (file x source). Line 52 adds another: `Path(metadata.path).resolve()` per file, even though `MetadataExtractor.extract` already stored a resolved path (`path=str(candidate.resolve())`, metadata.py:50), making it a guaranteed no-op realpath call.

**Failure scenario:** User previews a sort over three selected folders containing 100,000 files. The recursion check alone issues ~600,000 avoidable stat/realpath syscalls, plus 100,000 more from the redundant source resolve — seconds to tens of seconds of pure syscall overhead on a spinning or network drive, added to a preview that is supposed to be a pure in-memory planning pass over already-collected metadata.

**Suggested fix:** Hoist the roots out of the loop before iterating files: `source_roots = tuple(p for p in (Path(v).expanduser().resolve() for v in sources) if p.is_dir())`, then test `any(_within(destination, root) for root in source_roots)`. Drop the `.resolve()` at :52 and rely on the already-resolved `metadata.path` (or resolve once in the scanner and document the invariant).

### 36. [medium/execution-time] ffprobe is spawned for every video during scanning even when no rule uses video metadata

**Location:** `src/duplicate_transfer_manager/sorting/metadata.py:44-47 and :56-73`

**Claim:** `MetadataExtractor.extract` unconditionally calls `_video_metadata` for anything whose guessed mime type starts with `video/`, which runs `subprocess.run([ffprobe, ...], timeout=15)` — one process spawn plus a container parse per file. The resulting width/height/duration are only ever consumed if the active profile has a condition on `ConditionField.WIDTH`, `HEIGHT`, or `DURATION` (rules.py:112-115). The quick/Simple profile builds extension-only conditions (presets.py:113, :121) and the typical advanced profile uses extension or filename, so for the overwhelming majority of runs this work is computed and discarded. `shutil.which("ffprobe")` is also re-resolved on every call.

**Failure scenario:** User points Simple sorting at a 3,000-clip camera-footage folder to move everything into a Videos folder by extension. Preview spawns 3,000 ffprobe processes — on Windows a process launch alone is roughly 30-80 ms, so several minutes of wall clock before the plan appears, plus ffprobe's own container reads over USB, entirely for width/height/duration values that no rule in the profile references.

**Suggested fix:** Make video (and EXIF) probing conditional: have `SortPlanner`/`HybridSortService` inspect the profile's conditions once and pass a flag such as `need_media_dimensions` down to `SortScanner`/`MetadataExtractor`, defaulting to off. Cache `shutil.which("ffprobe")` at module or instance level. If probing must stay eager, at least populate it lazily on first access of the field.

### 37. [medium/execution-time] Quarantine rewrites and fsyncs the entire manifest once per file

**Location:** `src/duplicate_transfer_manager/services/duplicate_workflow.py:380, 505-525; src/duplicate_transfer_manager/services/support_services.py:38-56`

**Claim:** `self._write_manifest(operation, manifest_records, failures, ...)` sits inside the per-item loop at duplicate_workflow.py:380 and serializes the complete `manifest_records` list every time. `_write_manifest` builds the whole payload dict (line 515-523) and hands it to `_atomic_json_write`, which does `json.dump(..., indent=2)`, `stream.flush()`, `os.fsync()`, and `os.replace()` (support_services.py:46-50). So quarantining N items performs N full serializations of an N-record document and N fsyncs — O(N^2) bytes written and O(N) forced disk flushes. Each `QuarantineRecord` serializes to roughly 300 bytes at `indent=2`.

**Failure scenario:** Quarantining 5,000 duplicates produces a ~1.5 MB manifest, written 5,001 times: about 3.7 GB of disk writes and 5,001 fsyncs for an operation whose real work is 5,000 renames. On Windows an fsync on a consumer SSD is commonly 1-10 ms, so the manifest bookkeeping alone adds tens of seconds to minutes — and because this runs on the main thread (see the blocker above), it is all frozen-UI time. On a spinning external drive it dominates the operation entirely.

**Suggested fix:** Write the manifest once up front (already done at line 353) and then persist incrementally: append per-item status transitions to a small `manifest.progress.jsonl` sidecar, and rewrite the full `manifest.json` only at the end of the loop and on the cancellation/exception path. Alternatively, flush the full manifest on a bounded cadence (every N items or every few seconds) rather than every item — crash recovery only needs bounded staleness, not per-item durability.

### 38. [medium/execution-time] Dashboard reads every operation record, transfer report and quarantine manifest on the main thread, twice at startup

**Location:** `src/duplicate_transfer_manager/services/support_services.py:470-481, 541-571, 581-594; src/duplicate_transfer_manager/ui/pages.py:299-303`

**Claim:** `DashboardService.summary()` calls `self.operations.list_records()` (line 582), which opens and `json.loads` every file in `paths.operations` (lines 472-479) and then calls `_records_from_reports()`, which opens and fully parses every file in `paths.reports` (lines 543-547) including their embedded `failures` arrays. It also calls `self.quarantine.list_records()` (line 587), which parses every quarantine manifest. Only `records[:5]` and `interrupted[:5]` are actually used (lines 609-610). `DashboardPage.__init__` invokes `refresh(include_storage=False)` and then `QTimer.singleShot(0, self.refresh)` (pages.py:299-300), so the whole read happens twice at startup, on the Qt main thread — `DashboardPage.refresh` is a plain method, not routed through a controller. Nothing prunes `paths.operations`: `record()` writes a new JSON file per operation (line 466) and `remove_record` is only ever called from the Activity page's manual delete (pages.py:2153). `prune_cache`/`clear_cache` only touch `paths.cache`. The append-only `operation_audit.jsonl` (line 490) is likewise never rotated, and `list_audit_events` reads it whole (lines 496-506). The mechanism is one file open + JSON parse per historical record, not a single large read — which is the expensive shape on Windows.

**Failure scenario:** After six months of daily use a user has ~500 operation records, ~200 transfer reports (a report from a failed import carries a per-file failure array and can be tens of KB), and ~40 quarantine manifests. Opening the app — Overview is the landing route — costs ~740 file opens and JSON parses, twice, on the UI thread. With real-time antivirus inspecting each small file at 1-3 ms, that is 1.5-4.5 seconds of a frozen window before the dashboard paints, and it gets worse every month with no upper bound and no user-visible way to trim it other than deleting activity rows one at a time.

**Suggested fix:** Sort `paths.operations.glob("*.json")` by filename (the IDs are timestamp-prefixed, so name order is time order) and parse only the newest K files needed for the dashboard — add a `limit` parameter to `list_records()` and pass a small value from `DashboardService.summary`. Do not call `_records_from_reports()` at all for the dashboard, or cap it the same way. Route `DashboardPage.refresh` through a worker like the other pages, and drop the duplicate startup refresh. Add operation-record retention to the existing `cache_retention_days` prune so `operations/` and `operation_audit.jsonl` do not grow forever.

### 39. [medium/execution-time] Bulk duplicate selection is O(n^2): each checkbox toggle re-selects a table row and rescans every group

**Location:** `src/duplicate_transfer_manager/ui/pages.py:908-918, 902-906, 920-945, 1004-1018`

**Claim:** `_select_recommended_duplicates` / `_clear_duplicate_selection` loop over all n checkboxes calling `setChecked()`. Each `setChecked` emits `toggled` → `_duplicate_selection_changed` (902) → `self.table.selectRow(row)` (a full table selection change and repaint) → `currentCellChanged` → `_update_duplicate_detail` (920), which does a linear scan of `self.item_rows` (line 925) plus a nested generator over every group and every item (926-934) → plus `_refresh_recoverable` (1004), which rebuilds a dict over all items (1009-1013) and calls `_selected_ids` over all checkboxes. So one button click costs roughly n * O(n) work plus n row-selection repaints.

**Failure scenario:** On a result set of 5,000 duplicate rows, clicking "Select recommended copies" performs ~25 million dict/attribute operations and 5,000 table selection changes; the button appears to hang for many seconds and the table visibly flickers as the selection walks down every row.

**Suggested fix:** Guard the bulk operations with a `self._bulk_update = True` flag that makes `_duplicate_selection_changed` a no-op, do the whole loop with `table.setUpdatesEnabled(False)`, then call `_refresh_recoverable()` and `_update_duplicate_detail()` once at the end. Cache the item-id → item map and the row → item-id map instead of rebuilding them per call.

### 40. [medium/execution-time] Activity and Quarantine search boxes re-read and re-parse every record file from disk on each keystroke

**Location:** `src/duplicate_transfer_manager/ui/pages.py:1810, 1894-1950, 2206, 2293-2341`

**Claim:** `ActivityPage.search.textChanged` is connected directly to `refresh` (pages.py:1810). `refresh` calls `self.operations.list_records()` (line 1901), which globs `paths.operations/*.json` and `json.loads` every one of them plus `_records_from_reports()` (support_services.py:474-485), then rebuilds the whole table row by row with a fresh `QCheckBox` cell widget per row. `QuarantinePage` does the same (pages.py:2206, 2294) via `DuplicateQuarantineService.list_records()`, which globs and parses every `*.json` and `*/manifest.json` under the quarantine root.

**Failure scenario:** A user with a year of history (several thousand operation JSON files and dozens of quarantine manifests) types "import" into the Activity search box. Six keystrokes trigger six full directory globs, several thousand file reads and JSON parses each, and six complete table rebuilds — the search box drops characters and the page stutters badly on every letter.

**Suggested fix:** Load records once into memory and filter the in-memory list on keystroke; add an explicit Refresh (already present) for re-reading disk. If disk reads must stay, debounce `textChanged` through a ~250 ms QTimer and move the read into the existing `ReportsController`/`QuarantineController` loaders.

### 41. [medium/execution-time] Sorting history parses every run journal three times during page construction, on the app startup path

**Location:** `src/duplicate_transfer_manager/ui/sort_workspace.py:456-458, 1481-1492, 1566-1573`

**Claim:** `SortExecutor.list_runs()` (sorting/executor.py:256-263) reads and `json.loads` every `sorting/runs/*/journal.json` — files that contain a full `records` entry and a `planned_items` entry for every file in that run. `_refresh_history` calls it (line 1482) and then calls `_refresh_stats` (line 1492), which calls `list_runs()` a second time (line 1567). During `SortWorkspace.__init__`, `_refresh_profiles` → `_profile_changed` → `_refresh_stats` is one call and `_refresh_history` adds two more, so three full parses happen before the window is ever shown — `SortWorkspace` is constructed eagerly in `MainWindow.__init__` (shell.py:275).

**Failure scenario:** A user who has run twenty sorts of 30,000 files each has journals totalling hundreds of megabytes. Launching the app blocks for several seconds before the window appears, and every profile change or history refresh re-parses the whole set again, twice.

**Suggested fix:** Have `_refresh_history` pass its already-loaded `self.runs` into `_refresh_stats` instead of re-calling `list_runs()`, defer the first history load until the Sort route is first navigated to, and add a lightweight journal-summary read that skips `planned_items`.

### 42. [medium/execution-time] The sort review table materialises a QCheckBox widget per planned file in one pass and re-joins every item's text on each filter keystroke

**Location:** `src/duplicate_transfer_manager/ui/sort_workspace.py:1048-1087, 1116-1130`

**Claim:** `_render_plan` builds the entire table in a single synchronous loop, creating a real `QCheckBox` cell widget (line 1058) and five `QTableWidgetItem`s per plan item, with no batching. `DuplicatesPage` already solved exactly this problem with a 150-row `QTimer.singleShot` render queue (pages.py:849-898); the sort workspace does not use that pattern. `_filter_review` (1116-1130), bound to `review_filter.textChanged` (line 721), builds a fresh `" ".join(...).casefold()` string over eight fields for every item on every keystroke and calls `setRowHidden` per row.

**Failure scenario:** User drops a folder of 60,000 files and clicks "Review sort setup". When the preview returns, the UI freezes for tens of seconds creating 60,000 checkbox widgets and 300,000 table items, and memory spikes sharply. Once rendered, typing a four-character filter performs 240,000 string joins and 240,000 setRowHidden calls, so each character takes a visible pause.

**Suggested fix:** Reuse the batched render-queue pattern from `DuplicatesPage._render_next_batch`; replace the per-row checkbox widget with a checkable `QTableWidgetItem`; precompute each item's lowercase search haystack once when the plan is rendered and reuse it in `_filter_review`.

### 43. [medium/execution-time] Monitored-folder polling runs a recursive scan with Pillow and ffprobe on the Qt main thread every 30 seconds

**Location:** `src/duplicate_transfer_manager/ui/sort_workspace.py:460-463, 1457-1472, 926-935, 991-996; src/duplicate_transfer_manager/sorting/monitor.py:29-47; src/duplicate_transfer_manager/sorting/metadata.py:33-48`

**Claim:** `SortWorkspace.__init__` starts an unconditional 30-second `QTimer` (sort_workspace.py:460-463) wired to `_poll_change_monitors`, which calls `SortMonitorService.poll` directly — no worker, no `SortController` — for every filesystem_change monitor on the active profile. `poll` walks the tree with `root.rglob("*")` and issues `path.stat()` plus `path.resolve()` per entry (monitor.py:33-46), then calls `MetadataExtractor.extract` on each changed file, which opens every image with Pillow and reads EXIF (metadata.py:33-43) and spawns an `ffprobe` subprocess with a 15-second timeout per video (metadata.py:57-73). All of it executes on the Qt main thread, violating docs/ARCHITECTURE.md:176-181 ("Scanning, hashing, ADB commands, transfers, and file-backed controller tasks execute through `QThreadPool`"). The first poll after a monitor is added has an empty snapshot (monitor.py:37, `_load` returns `{}` on missing file), so *every* file in the tree is treated as changed and fully extracted in one main-thread pass. `_poll_change_monitors` also feeds results into `_add_sources` (926-935), which calls `_invalidate_plan` (991-996) — and when a review is being built, that cancels it.

**Failure scenario:** A user adds their 40,000-file D:\Camera folder as a filesystem_change monitor. The next 30-second tick performs 40,000 stat+resolve calls and 40,000 Pillow opens on the GUI thread; the window stops repainting and Windows marks it "Not Responding" for minutes, on a timer the user cannot see or stop and that keeps firing on every route, not just Sort Files. Separately, a user on the Sort Files page clicks "Review sort setup" for a large job; twenty seconds in, the timer fires, one new file has landed in the monitored folder, and their in-progress review is cancelled with "The selected sources changed. The current review build is being cancelled safely." — repeatedly, for as long as the folder stays active.

**Suggested fix:** Move the poll onto `SortController`/`QThreadPool` like every other file-backed operation, only run it while the Sort Files route is visible, back off the interval (or drive it from `QFileSystemWatcher`), and have `poll` return path/size/mtime deltas without invoking `MetadataExtractor` — extract metadata later, in the planner, where it already runs off-thread. Do not let a background poll invalidate or cancel a review the user is actively building; queue the additions and offer them instead.

### 44. [medium/execution-time] The quarantine manifest is rewritten in full after every single file

**Location:** `src/duplicate_transfer_manager/services/duplicate_workflow.py:380`

**Claim:** Inside the `for item, stored_path_text, manifest_record in planned:` loop (duplicate_workflow.py:360), `self._write_manifest(operation, manifest_records, failures, dry_run=dry_run)` runs once per item at line 380. `_write_manifest` builds the full payload including every record (line 520) and hands it to `_atomic_json_write`, which does `json.dump(..., indent=2)`, `flush()`, `os.fsync()`, and `os.replace` (support_services.py:38-56). Manifest cost is therefore O(n²) in records and O(n) fsyncs, and because this loop runs on the GUI thread (see the main-thread quarantine finding) it compounds that freeze directly.

**Failure scenario:** A user quarantines 5,000 local duplicates. The manifest is serialized 5,000 times at an average of 2,500 records each — about 12.5 million record serializations and 5,000 fsync'd writes averaging over 1 MB, several gigabytes of disk traffic for a job that only needs to move 5,000 files. On a spinning disk the fsync alone adds minutes, and every second of it is a second the window is frozen.

**Suggested fix:** Write the manifest once before the loop (the pre-write at line 353 already establishes the recovery record) and then checkpoint on a bounded cadence — every N items or every few seconds, plus unconditionally on the final item and on any failure — the same pattern `TransferJournal.save` already uses at transfer_safety.py:58-59.

### 45. [medium/execution-time] `adb devices` runs on the Qt main thread from combo-box handlers

**Location:** `src/duplicate_transfer_manager/ui/pages.py:685 and 1474`

**Claim:** `_refresh_devices` calls `ADBBridge.list_devices()` directly. It is reached from `_source_changed`, which is connected to `source_picker.selection_changed` (pages.py:408, 1122) and so runs on the GUI thread. `list_devices` calls `ADBBridge.start_server()` (a `subprocess.run` of `adb start-server`, up to `ADB_QUICK_TIMEOUT` = 3s), then `adb devices -l` (another 3s), then one `adb shell getprop ro.product.model` per device whose model was not in the listing (3s each) — adb_bridge.py:188-223. Worst case is roughly 6-9 seconds of blocked event loop for a single device, more for several. This is the same class of violation as the invariant that ADB work stays off the Qt main thread.

**Failure scenario:** A user opens Import Files and clicks the "Phone" source card. The adb server is not yet running (first use after a reboot), so `start-server` has to fork the daemon and scan USB. The window freezes for several seconds with no cursor change and no message; on a machine with a flaky USB stack, where `adb devices` blocks until the 3s timeout, it freezes for the full timeout on every single click of the card.

**Suggested fix:** Move device enumeration into the existing worker infrastructure — a small `DeviceController(BaseOperationController)` wrapping `DeviceService.list_devices`, or a `QRunnable` posting results back via a signal. Populate the combo with a "Looking for devices…" placeholder immediately and fill it in when the worker returns.

### 46. [medium/logic] Free-space preflight sums per destination directory instead of per volume

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:380-405`

**Claim:** `_validate_space` keys `required_by_root` on `destination.parent` walked up to its nearest *existing* ancestor, then checks each key independently against `shutil.disk_usage(root).free`. Distinct category folders that already exist on the same drive become distinct keys with the same underlying free-space number, so each requirement is compared against the full volume free space rather than their sum. The bug only hides on the very first run (when the category folders don't exist yet and everything collapses to a shared ancestor) — on every subsequent run, which is the normal case, the folders exist and the check splits.

**Failure scenario:** User sorts 60 GB of video and 60 GB of photos onto D:\ with 100 GB free, into the already-existing D:\Sorted\Videos and D:\Sorted\Pictures. Both checks pass (60 < 100 twice), the run starts, and it fills the disk partway through. The user gets a wall of per-file ENOSPC failures, a half-sorted library split across two locations, and a full system drive — exactly the situation the preflight exists to prevent.

**Suggested fix:** Key the accumulator on the volume, not the directory: on Windows use `os.path.splitdrive(destination)[0]` or the resolved mount point (`Path(anchor)`), and fall back to `os.stat(root).st_dev` elsewhere. Keep one `disk_usage` call per volume and compare the summed requirement for that volume.

### 47. [medium/logic] ML prior-destination history reads the 100 oldest runs instead of the 100 newest

**Location:** `src/duplicate_transfer_manager/sorting/workflow.py:77`

**Claim:** `for run in reversed(self.executor.list_runs()[-100:])`. `list_runs` (executor.py:258) returns `sorted(glob, reverse=True)`, i.e. newest-first, because run ids are `sort_YYYYMMDD_HHMMSS_ffffff` and sort lexicographically by time. Slicing `[-100:]` off a newest-first list takes the *last* hundred elements — the 100 oldest runs. Everything recent is discarded before `_prior_destination` (ml.py:120-122) picks `matches[-1]`, whose whole point is recency. The correct slice is `[:100]`.

**Failure scenario:** A user with monitored-folder automation accumulates well over 100 runs. In month one they sorted screenshots into D:\Temp; in month four they reorganized to D:\Archive\Screenshots and have sorted there dozens of times since. The Review screen keeps proposing D:\Temp, because only their earliest 100 runs are ever consulted and their recent, consistent choices are invisible to the suggester. The suggestion silently gets staler as they use the app more.

**Suggested fix:** Change to `self.executor.list_runs()[:100]` and drop the `reversed()` (or keep `reversed()` so the newest end up last, matching `matches[-1]`). Add a test that records 150 runs and asserts the newest destination wins.

### 48. [medium/logic] is_hidden_or_system returns True when the Windows attribute call fails, silently dropping files from scans

**Location:** `utils.py:118-124, discovery.py:65-69`

**Claim:** `GetFileAttributesW` returns `INVALID_FILE_ATTRIBUTES` (0xFFFFFFFF) on failure — long path, access denied, transient sharing violation. `restype` is left at ctypes' default `c_int`, so that arrives as `-1`, and `-1 & (0x2 | 0x4)` evaluates to `6`, which is truthy. The function therefore reports "hidden and system" for every path it could not read (utils.py:121-123). `skip_hidden_system` is hardcoded `True` for imports (import_workflow.py:126), and `_is_excluded_local` (discovery.py:65-69) silently drops the entry — it is not added to `discovery.errors`, not added to `discovery.unreadable`, and does not set `incomplete`. The `source_listing_incomplete` reporting added at engine.py:570-580 and engine.py:1124-1126 cannot see it. The bare `except:` at utils.py:123 also swallows any ctypes error into the same `False`/skip behaviour.

**Failure scenario:** User's library lives under a deep folder tree on a drive where Windows long-path support is off, and one subtree's full paths exceed 260 characters. `GetFileAttributesW` fails with ERROR_PATH_NOT_FOUND on that directory, `is_hidden_or_system` returns True, and `_is_excluded_local` prunes the entire subtree from the compare scan with zero log output. Those files are absent from `dest_hashes`, so the phone's copies of them are all re-imported as "new" and land beside the originals. In the reverse direction — the same failure on the *source* side of a folder-to-folder import — the subtree is simply never offered for import, the run reports success with `source_listing_incomplete: false`, and the user has no way to discover the omission.

**Suggested fix:** Check the sentinel explicitly: set `GetFileAttributesW.restype = ctypes.c_uint32` and return `False` (plus record the path as unreadable) when the result is `0xFFFFFFFF`, rather than letting the sentinel's bits satisfy the hidden/system mask. Failing to read attributes should mean "include and report", never "silently exclude". While there, replace the bare `except:` with `except OSError`. Bonus: `discovery.py` already has a `DirEntry` in hand, so `entry.stat().st_file_attributes` gives the same bits from the cached directory enumeration and removes one `GetFileAttributesW` syscall per file across the whole scan.

### 49. [medium/logic] makedirs outside the per-file try aborts the whole transfer, discarding the drive cache and leaving the phone's stay-awake setting on

**Location:** `engine.py:160,166,169 (inside build_target_path), engine.py:890, engine.py:908-917 vs the try at engine.py:926, engine.py:1074-1097`

**Claim:** `build_target_path` is called at engine.py:908, but the per-file `try` does not open until engine.py:926. `build_target_path` calls `os.makedirs` at engine.py:160, 166, and 169, and the isolate branch calls it at engine.py:890 — all outside any handler. An `OSError` from any of them propagates out of the loop and out of `execute_smart_transfer`. `OperationWorker.run` catches it (controllers/base.py:55) so the app survives, but every finalizer at the bottom of the function is skipped: `drive_cache.save()` (engine.py:1074-1080), `adb_cache.save()` (engine.py:1084-1091), `ADBBridge.restore_stay_awake` (engine.py:1095-1097), the staging-directory cleanup (engine.py:1104-1108), and `write_transfer_report` (engine.py:1128-1133). `restore_stay_awake` writes back `settings put global stay_on_while_plugged_in` (adb_bridge.py:280-296), so skipping it leaves a persistent change on the user's phone.

**Failure scenario:** User imports with `destination_template="date"` into a library that already contains a *file* named `2024` at the root (an extensionless export). `os.makedirs(dest_root/2024/05)` raises `NotADirectoryError` at engine.py:160 on the first 2024 photo. The transfer dies with a generic error banner after having already hashed a 400 GB library — and because `drive_cache.save()` never runs, all of that hashing is thrown away and the next attempt repeats it from scratch. The phone is left with "stay awake while charging" permanently enabled, which the app turned on and never told the user about.

**Suggested fix:** Wrap the target-path construction and the isolate-folder `makedirs` in the same `try` that already handles `OSError` per file (engine.py:993-1008) so a bad path costs one file, not the run. Independently, wrap the body of `execute_smart_transfer` from the sleep-inhibitor onward in `try/finally` so cache saves, `restore_stay_awake`, staging cleanup, and the report always run — these are exactly the steps that must not be skipped when something goes wrong.

### 50. [medium/logic] Single-read staged import leaks a full copy of the source file when the conflict policy skips

**Location:** `engine.py:799-823 (staging), engine.py:918-923 (skip path), contrast engine.py:868-872 and engine.py:881-885`

**Claim:** In the single-read path (`uses_staged_import`, engine.py:733-736), the full file is pulled from the phone to `staged_path` under `<output_root>/.duplicate_transfer_manager_staging` before its hash is known (engine.py:799-817). Every later exit removes it — the no-hash path at engine.py:868-872 and the duplicate path at engine.py:881-885 — except one: when `resolve_conflict_path` returns `""` under `conflict_policy="skip"`, engine.py:920-923 increments `skipped` and `continue`s without touching `staged_path`. The file stays in staging for the rest of the run and beyond; `os.rmdir(staging_dir)` at engine.py:1106 then fails silently because the directory is non-empty, so nothing cleans it at the end either. Only a manual "Clean partial files" run (pages.py:1660) removes it.

**Failure scenario:** User imports a phone with "Skip existing filename" selected into a library that already has many matching filenames from a previous flat import. Every such photo is pulled in full to staging, then abandoned there. On a 200 GB phone where 60 GB of files hit name conflicts, 60 GB of hidden staged copies accumulate under the output root during the run — on a destination that preflight already flagged as tight. If the destination fills, subsequent genuine copies start failing with disk-full errors that have no obvious cause, and the space stays consumed after the app closes.

**Suggested fix:** Remove `staged_path` in the conflict-skip branch before `continue`, matching engine.py:881-885. Better, hoist the cleanup into a small helper (or a `try/finally` around the per-file body) that unlinks `staged_path` on every exit that does not promote it, so a future branch cannot reintroduce the leak. Also make the final `os.rmdir` failure at engine.py:1104-1108 log what is left behind rather than swallowing it.

### 51. [medium/logic] Drive-cache lock is honoured by only one of two writers and never expires, so scheduled refreshes and imports clobber each other

**Location:** `drive_cache.py:73-91 (lock), drive_cache.py:269-302 (build_drive_cache takes it), engine.py:399-420 and engine.py:1074-1091 (transfer never takes it), schedule_adb_cache.ps1`

**Claim:** `build_drive_cache` acquires an exclusive lock file before touching a cache (drive_cache.py:278-279). `execute_smart_transfer` does not: `load_drive_cache`/`load_adb_cache` just construct and `load()` (engine.py:399-420), and the finalizers call `drive_cache.save()`/`adb_cache.save()` (engine.py:1077, 1088), which rewrite the entire JSON from the in-memory copy captured at the start of the run. A lock that only one of two writers checks provides no mutual exclusion. Separately, `acquire_lock` writes the PID (drive_cache.py:80-83) but nothing ever reads it, and `release_lock` only runs in `build_drive_cache`'s `finally` — a hard kill leaves the lock file permanently, and `schedule_adb_cache.ps1` registers a weekly unattended task with no error surface.

**Failure scenario:** The user installs the weekly scheduled ADB cache refresh (`schedule_adb_cache.ps1`). Sunday 03:00 it starts hashing the whole phone. The user wakes up, plugs in, and runs an import, which loads the ADB cache as it was before the scheduled run started. The import finishes at 04:30 and `adb_cache.save()` writes its stale snapshot over everything the scheduled job just wrote — hours of device hashing silently discarded, so the next import re-hashes the entire phone. Separately, if the machine sleeps or is shut down mid-refresh, the `.lock` file survives; every subsequent Sunday run raises `RuntimeError: Cache is locked`, exits, and the user's cache silently stops updating forever with no message anywhere.

**Suggested fix:** Have `execute_smart_transfer` take the same lock around its cache load/save window (or fall back to read-only cache use when the lock is held, which it already handles cleanly via `update_drive_cache`). Make `acquire_lock` treat a lock whose recorded PID is no longer alive — or whose mtime exceeds a generous timeout — as stale and reclaim it, since the PID is already written for exactly this purpose. Give the scheduled script a nonzero-exit log so a permanently locked cache is visible.

### 52. [medium/logic] Duplicate scan computes an "incomplete scan" warning and then discards it

**Location:** `src/duplicate_transfer_manager/services/duplicate_service.py:89-99, 144-156; src/duplicate_transfer_manager/services/duplicate_workflow.py:72`

**Claim:** `DuplicateScanService.run` builds `incomplete_warning` at lines 95-98 with a comment explaining that this exact message exists to stop users acting on a partial listing, but the returned `OperationResult` sets `warnings=tuple(discovery.errors)` (line 154) — `incomplete_warning` is never added and the local variable is dead after line 99. The only other outlet is `reporter.log(...)` at line 99, and `DuplicatesPage` never connects the controller's `technical_log` signal (the sole `technical_log.connect` in the UI is `ImportPage`, pages.py:1386), so that message is discarded too. It also goes out as a WARNING event through `emit` (reporting.py:146), but `_on_progress` only writes it into the progress panel, which `_on_scan_completed` hides at pages.py:777 before rendering results. Compounding this, `DuplicateReview.warnings` (duplicate_workflow.py:72) is stored at pages.py:785 and copied at pages.py:987 but never rendered by any widget, so the individual discovery errors do not appear on the review screen either — the banner is an unconditional green "Scan complete. Review checked duplicates before confirming quarantine." (pages.py:788).

**Failure scenario:** A user scans an external drive where a dozen folders raise PermissionError (a OneDrive-locked tree, a Windows-protected folder). Discovery reports `incomplete`, but the review screen shows a green "Scan complete" with, say, 40 duplicate groups and no warning of any kind. The user quarantines them believing the drive was fully compared. The uncompared folders held additional copies of the very files just quarantined — and, in the other direction, copies of the file that was kept. To learn the scan was partial they must navigate to Activity and click into the record's detail text.

**Suggested fix:** Add `incomplete_warning` to the returned warnings: `warnings=(*incomplete_warning, *discovery.errors)`. Then render `DuplicateReview.warnings` on the review screen — an `InlineMessage`/warning banner above the group table when the tuple is non-empty — and make the completion banner use the "warning" style rather than "success" when it is.

### 53. [medium/logic] SortController.execution_control is written from the worker thread and never cleared, so Pause/Resume can act on a stale run and misreport state

**Location:** `src/duplicate_transfer_manager/controllers/operations.py:99, 112-133, 148-151`

**Claim:** `SortController.execute` assigns `self.execution_control = SortExecutionControl(cancellation)` inside the task body (line 114), i.e. on the pool thread, while `pause`/`resume`/`skip_current`/`cancel` read it from the main thread with no synchronisation. It is never reset to `None` on completion, so after the first run the attribute permanently holds the previous run's control. `pause()` only checks `if not self.execution_control or not self.busy` (line 129) — both are satisfied by a stale control during the window between `start_task` and the worker actually running — and then unconditionally calls `self._set_state(OperationState.PAUSED)` (line 132).

**Failure scenario:** User runs a sort, it completes, then starts a second sort and immediately clicks Pause before the pool thread has entered the task body. `pause()` pauses the *previous, finished* control, returns True, and sets the controller state to PAUSED. The UI now shows a paused run that is in fact copying files at full speed; clicking Resume sets state TRANSFERRING on a run that was never paused. `cancel()` (line 149-150) likewise calls `.cancel()` on the stale control, though `super().cancel()` saves it.

**Suggested fix:** Create the `SortExecutionControl` in `execute()` on the calling thread before `start_task`, pass it into the task closure, protect access with the controller's existing `_lock`, and clear it in `_on_finished`.

### 54. [medium/logic] A phone disconnecting mid-scan yields a duplicate report that looks complete but was computed from a fraction of the files

**Location:** `engine.py:460-461 (and the identical handler at engine.py:504-505)`

**Claim:** `compute_hash` deliberately re-raises `ADBOperationError` when the device is gone (engine.py:75-76) so callers can stop. In `group_duplicates` that exception lands in `except Exception: continue` at engine.py:460, which discards it and moves to the next future; the same swallow repeats in the full-SHA-256 confirmation pass at engine.py:504. Nothing sets a flag, nothing is added to the returned data, and `DuplicateScanService` goes on to return `OperationState.COMPLETED` with `warnings=discovery.errors` (duplicate_service.py:144-157) — which only ever carries *discovery* errors, never hashing errors. The scan result is indistinguishable from a healthy one.

**Failure scenario:** A user scans /sdcard/DCIM for duplicates. Two minutes in, the USB cable is nudged and the phone drops to `unauthorized`. Every remaining hash raises, every exception is swallowed, and the scan finishes reporting, say, "12 duplicate groups, 24 duplicate files" instead of the several hundred that exist. The user reviews and quarantines that partial set, believes the folder is clean, and the real duplicates are still there. The only trace is a stream of `WARNING:` lines in a log the user has no reason to open.

**Suggested fix:** Catch `ADBOperationError` explicitly in both loops, break out, and propagate a `device_lost` flag (or the count of files that failed to hash) into the returned value. Have `DuplicateScanService.run` refuse to report `COMPLETED` when any candidate file failed to hash — return a warning-bearing result and surface it in the results banner, the way `discovery.incomplete` is already surfaced at duplicate_service.py:89-99.

### 55. [medium/logic] Any GetFileAttributesW failure is read as "hidden or system", silently dropping the file with no error recorded

**Location:** `utils.py:118-124, consumed at discovery.py:65-69`

**Claim:** `is_hidden_or_system` calls `ctypes.windll.kernel32.GetFileAttributesW(path)` with no `restype`/`argtypes` and no error check. On failure the API returns `INVALID_FILE_ATTRIBUTES` (0xFFFFFFFF), which arrives as `-1` through the default `c_int` restype; `-1 & (0x2 | 0x4)` is `6`, so the function returns `True`. I confirmed this on this machine: `GetFileAttributesW` on a missing path returns `-1` and `is_hidden_or_system` returns `True`. `_is_excluded_local` (discovery.py:66) then returns True and `scan_local_tree` does a bare `continue` at discovery.py:236 or 242 — it does not append to `errors`, so `incomplete=bool(errors)` at discovery.py:286 stays False and the scan is reported as covering everything. For a directory this prunes the entire subtree.

**Failure scenario:** A user scans a network share or an external drive that hiccups for a moment, or another process renames a folder between `os.scandir` listing it and the attribute check. `GetFileAttributesW` returns -1 for that entry, the scan treats it as a system folder and skips it and everything under it, and the completeness machinery that the rest of discovery.py was carefully built around (`incomplete`, `unreadable`, the warnings at engine.py:570-580) never fires. The import finishes claiming success while a whole subtree of photos was never offered.

**Suggested fix:** Set `GetFileAttributesW.restype = ctypes.c_uint32` and `argtypes = [ctypes.c_wchar_p]`, compare against `0xFFFFFFFF` explicitly, and on failure raise or return a distinguishable value so `_is_excluded_local` can record it as a scan error rather than as an exclusion. `scan_local_tree` should append such entries to `errors` so `incomplete` reflects them.

### 56. [medium/logic] The monitored-folder snapshot is saved before the sort runs, so a failed run permanently loses the change set

**Location:** `src/duplicate_transfer_manager/sorting/monitor.py:47, consumed at src/duplicate_transfer_manager/scheduled_sort.py:22-33`

**Claim:** `SortMonitorService.poll` builds `current` from the live folder and calls `self._save(folder.id, current)` at monitor.py:47 unconditionally, immediately before returning the changed files — i.e. before the caller has done anything with them. In `run_scheduled_sort` the sequence is `changed = SortMonitorService(paths).poll(monitor)`, then `SortPlanner(...).build(...)`, then `SortExecutor(...).execute(...)`. Every step after the poll can raise: `SortPlanner._render_destination` raises `ServiceError` on a bad template (planner.py:137), `SortExecutor._validate_space` raises when the destination is short on space or unreadable (executor.py:398-405), and the whole body is wrapped in `except Exception` at scheduled_sort.py:43 which just records a "failed" row. The snapshot has already been advanced, so those files are no longer "changed" on the next poll.

**Failure scenario:** A user has an hourly monitored folder sorting new camera imports onto an external drive. Overnight the drive is unplugged. The scheduled run polls, records 400 new photos in the snapshot, then `_validate_space` raises because the destination root does not exist. One `scheduled_sort / failed` record is written. The next hourly run sees no changes — the 400 photos are already in the snapshot — so they are never sorted, not by that run nor any future one. The user has to notice the single failed row in the activity list and manually reset the monitor (`SortMonitorService.reset`) to recover, and nothing in the failure message tells them that.

**Suggested fix:** Return the new snapshot from `poll` without persisting it, and add a `commit(monitor_id, snapshot)` the caller invokes only after the executor reports a terminal, non-failed status. On partial success, commit only the entries whose files reached a terminal record status so the failed ones stay in the change set.

### 57. [medium/logic] EXIF capture time is parsed as local wall clock and then formatted as UTC, misfiling files by up to a day

**Location:** `src/duplicate_transfer_manager/sorting/metadata.py:41, consumed at src/duplicate_transfer_manager/sorting/planner.py:128-134`

**Claim:** metadata.py:41 does `datetime.strptime(str(raw_date), "%Y:%m:%d %H:%M:%S").timestamp()`. EXIF DateTimeOriginal is a bare wall clock with no zone; `strptime` yields a naive datetime and `.timestamp()` interprets it in the *sorting machine's* local zone, producing an epoch that is offset from the photo's actual wall clock. planner.py:128 then converts it straight back with `datetime.fromtimestamp(captured, timezone.utc)` and feeds `stamp.year/month/day` into the `{year}`, `{month}`, `{day}` destination tokens. The round trip therefore shifts the rendered date by the local UTC offset. The fallback path (`metadata.modified`, a genuine epoch) is also rendered in UTC while every date the user actually sees is rendered locally — `_date_label` in duplicate_workflow.py:158 uses naive `datetime.fromtimestamp`.

**Failure scenario:** A user in New York (UTC-5) sorts with a destination template of `D:/Photos/{year}/{month}`. A photo whose EXIF reads `2024:01:31 21:00` becomes epoch 2024-02-01T02:00Z and is filed into `D:/Photos/2024/02`. The review table beside it shows "2024-01-31 21:00". Every evening photo in the last days of a month lands in the wrong month's folder, and the same library sorted on a laptop in a different timezone produces a different layout — so re-running the profile after travelling scatters files across two folders.

**Suggested fix:** Keep the EXIF wall clock as a wall clock: either store `captured` as an ISO string and format it directly, or attach `timezone.utc` at parse time (`strptime(...).replace(tzinfo=timezone.utc).timestamp()`) so the UTC read-back at planner.py:128 returns the original digits. Whichever is chosen, use the same basis for the `metadata.modified` fallback and for the labels shown in the review table so the folder a file lands in always matches the date the user was shown.

### 58. [medium/purpose] "Replace existing filename" silently retains every overwritten file forever in an undocumented hidden folder

**Location:** `engine.py:218-231, utils.py:26 (DEFAULT_EXCLUDES), transfer_safety.py:301-342 (cleanup does not cover it), transfer_safety.py:207-213 (preflight does not account for it)`

**Claim:** With `conflict_policy="replace"`, `promote_transfer_file` moves the existing target into `<output_root>/.duplicate_transfer_manager_backups` before writing (engine.py:220-224). That directory is in `DEFAULT_EXCLUDES` (utils.py:26), so no scan ever sees it; `cleanup_partial_files` only walks the *staging* directory and journal `partial_path` entries (transfer_safety.py:301-330), so it never removes backups; nothing in the UI lists them; and `preflight_transfer`'s space calculation (transfer_safety.py:207-211) assumes a replace consumes no net extra space. The user chose an option labelled "Replace existing filename" (pages.py:1264) and gets silent duplication of every replaced file instead. The Quarantine section of docs/USER_GUIDE.md:106-108 documents replace-safety for *restores* only; the import conflict policy's backup behaviour is documented nowhere.

**Failure scenario:** User re-imports a reorganised 300 GB photo library with "Replace existing filename" to refresh files in place, on a drive with 350 GB free. Preflight computes `required = 300 GB` against 350 GB free and passes. Every replaced file is moved to `.duplicate_transfer_manager_backups` rather than freed, so actual consumption approaches 600 GB. The drive fills partway through, copies start failing with disk-full errors, and neither the summary, the transfer report, the cleanup tool, nor the docs mention where the missing 300 GB went.

**Suggested fix:** Either (a) surface the backups: count them in the result dict, report the total bytes retained, and extend `cleanup_partial_files` (or add a sibling "Clear replaced-file backups" action) to purge them, plus add the backup size to the preflight `required` total when `conflict_policy == "replace"`; or (b) delete the backup after the `os.replace` at engine.py:226 succeeds, which is the behaviour the label promises. Whichever is chosen, document it next to the conflict-policy control.

### 59. [medium/purpose] FileOrganizerService is a second, entirely unreachable file-organization engine

**Location:** `src/duplicate_transfer_manager/services/organizer_service.py:1-611; src/duplicate_transfer_manager/controllers/operations.py:54-84; src/duplicate_transfer_manager/services/support_services.py:163-186; src/duplicate_transfer_manager/ui/app.py:48`

**Claim:** Nothing in the running application reaches `FileOrganizerService.build_plan`, `.organize`, `.rollback`, `.list_operations`, `.search_catalog`, `.relabel_plan_item`, `.set_ml_extension_rule`, or `.exclude_ml_folder`. `FileOrganizerController` (operations.py:54) is defined and exported but never instantiated by any page — `grep -rn FileOrganizerController src/` returns only its definition and its re-export. `ScheduledScanService.configure_organizer_preview` (support_services.py:163) is likewise never called, so `scheduled_organizer.py` and the `dtm-scheduled-organizer` console script never run. The feature is fully superseded by `src/duplicate_transfer_manager/sorting/` (`HybridSortService` + `SortExecutor`, which has its own `undo` at sorting/executor.py:195) driven by `SortController` from `ui/sort_workspace.py:397`. The only surviving call is `FileOrganizerService(paths).prune_manifests(...)` on every startup (ui/app.py:48 and pages.py:2694), which walks and prunes a directory that can no longer be populated. `AppSettings.organization_presets` and `AppSettings.organization_schedule_frequency` (contracts.py:118-119) are dead for the same reason. The two engines disagree substantively: organizer_service categorizes by hardcoded extension sets and filename keywords (`_category` line 55, `_ml_suggestion` line 74), uses a `manifest.json` format with an embedded `plan` array, and resolves collisions with `" (n)"` suffixes — none of which matches the sorting profile the user actually configures.

**Failure scenario:** A maintainer is asked to fix a mis-categorization complaint ("my .heic files land in Other"). They find `_category` in organizer_service.py, fix it, ship it, and nothing changes for any user, because the live path is `sorting/rules.py`. Worse is the reverse: someone wires `FileOrganizerController` back into a page to "restore the organizer tab" and users get a second sorting engine with an incompatible manifest format whose undo (`rollback`, line 362) is not reachable from the Sort Files history UI, so runs made through it cannot be undone through the control the docs point at.

**Suggested fix:** Delete `organizer_service.py`, `FileOrganizerController`, `scheduled_organizer.py`, `ScheduledScanService.configure_organizer_preview`, the `dtm-scheduled-organizer` entry point, and the `organization_presets`/`organization_schedule_frequency` settings fields (keeping `AppSettings.from_dict`'s unknown-key tolerance so old settings files still load), and drop the `prune_manifests` calls from startup. If any of it is being kept deliberately for a future re-wire, say so in a module docstring and stop calling `prune_manifests` on a dead directory. Note that the module also contains two latent defects that should not be resurrected as-is: `rollback` with `conflict_policy="replace"` calls `resolved.unlink()` (line 391), permanently deleting a user file with no backup — unlike `duplicate_workflow.restore_record`, which makes a verified backup first (lines 420-425) — and `_update_catalog` (line 584) rebuilds the entire catalog list once per record, which is O(records x entries) against an unbounded, never-pruned `organization_catalog.json`.

### 60. [medium/purpose] "Skip current file" skips whichever review row is selected — usually none — not the file being processed

**Location:** `src/duplicate_transfer_manager/ui/sort_workspace.py:1208-1213, 768, 779`

**Claim:** `_skip_current` reads `self.review_table.currentRow()` and passes `self.plan.items[row].metadata.path` to `controller.skip_current`. The review table's current row is whatever the user last clicked while reviewing — it has nothing to do with the file the executor is on, and `_render_plan` never calls `selectRow`, so after a fresh preview `currentRow()` is -1 and the method returns without calling the controller at all. Meanwhile the processing page header (line 768) states "You can pause, skip the current file, or cancel safely" and the button is labelled "Skip current file" (line 779).

**Failure scenario:** A sort run stalls on one large file on a flaky network drive. The user clicks "Skip current file" repeatedly; because they never clicked a row in the review table, `currentRow()` is -1 and nothing happens — no feedback, no banner, no state change. If they had clicked row 0 earlier, the button instead adds an already-processed file to the skip set and the stalled file keeps stalling.

**Suggested fix:** Track the currently processing source path from `OperationEvent.current_item` in `_on_progress` and pass that to `controller.skip_current`; disable the button when no current item is known, and show a banner confirming which file was skipped.

### 61. [medium/purpose] FileOrganizerService is a second, unreachable sorting implementation that the changelog says was removed

**Location:** `src/duplicate_transfer_manager/services/organizer_service.py:155-611; src/duplicate_transfer_manager/controllers/operations.py:54-84; src/duplicate_transfer_manager/services/support_services.py:163-186; src/duplicate_transfer_manager/core/contracts.py:119; CHANGELOG.md:110-113`

**Claim:** CHANGELOG.md:110-113 records under Removed: "The unreachable legacy `OrganizerPage` and its tests, superseded by the Sort Files workspace." The page went; the subsystem did not. `FileOrganizerService` (611 lines, its own category taxonomy at organizer_service.py:30-36, its own EXIF reader at 103-152, its own ML heuristic at 74-100, its own manifest format) is still built and shipped, and duplicates the Sort Files pipeline's rules.py/ml.py/metadata.py/planner.py with different rules — e.g. `_ml_suggestion` (organizer_service.py:83-99) and `LocalMLService._tokens` (sorting/ml.py:39-46) are separate keyword lists that already disagree ("trip", "meal", "recipe", "drawing" exist only in one). Its live path is unreachable from the UI: `FileOrganizerController` (operations.py:54) is never constructed (grep across src/ finds only the export in controllers/__init__.py), and the only in-app calls are `prune_manifests` (ui/app.py:48, pages.py:2708). Its one runnable entry point, `dtm-scheduled-organizer`, forces `dry_run=True` (scheduled_organizer.py:16-18) and can never be scheduled anyway because `ScheduledScanService.configure_organizer_preview` (support_services.py:163) has no caller, and `AppSettings.organization_schedule_frequency` (contracts.py:119) has no reader and no writer anywhere outside the dataclass and tests.

**Failure scenario:** A maintainer reads the changelog, believes the legacy organizer is gone, and fixes a category-mapping bug only in `sorting/rules.py`. The bug persists in `organizer_service.py:55-71`, which still writes `organization/*/manifest.json` files that `SortingMigrationService.migrate_legacy_runs` (sorting/migration.py:27) imports into Sort history as authoritative planned items. Meanwhile a user editing settings.json by hand sets `organization_schedule_frequency: "daily"`, which the app accepts, persists, round-trips through the Settings page, and completely ignores.

**Suggested fix:** Delete `organizer_service.py`, `FileOrganizerController`, `scheduled_organizer.py`, the `dtm-scheduled-organizer` entry point (pyproject.toml:35), `configure_organizer_preview`, and `AppSettings.organization_schedule_frequency`/`organization_presets`/`organization_retention_days` — keeping only whatever `SortingMigrationService` needs to read existing manifests. If it must stay, give it a reachable route and reconcile its category and ML rules with `sorting/`, and correct CHANGELOG.md:110-113.

### 62. [medium/purpose] Two different features both called "quarantine", stored apart, only one of them restorable from the Quarantine page

**Location:** `src/duplicate_transfer_manager/sorting/planner.py:121-124; src/duplicate_transfer_manager/sorting/models.py:29; src/duplicate_transfer_manager/services/duplicate_workflow.py:487; src/duplicate_transfer_manager/ui/sort_workspace.py:156-158`

**Claim:** docs/USER_GUIDE.md:104-112 describes exactly one quarantine: "Open **Quarantine**. You can search, filter by status, and group by operation. Restore individual files or a whole operation." But a Sort Files association can be given `SortAction.QUARANTINE` — the rule editor offers every member of the enum without filtering (`for value in SortAction:` at sort_workspace.py:157) — and the planner routes those files to `self.paths.sorting / "quarantine"` (planner.py:121-124), a different root from `paths.quarantine`. `DuplicateQuarantineService.list_records`, which backs the Quarantine page, globs only `self.paths.quarantine/*/manifest.json` (duplicate_workflow.py:487), so sort-quarantined files never appear there, have no `QuarantineRecord`, and cannot be restored by the Quarantine page's rename/skip/replace flow. Their only recovery route is Undo on that specific run in Sort Files history — which the retention prune eventually removes.

**Failure scenario:** A user builds a rule "junk screenshots → Quarantine" and runs it live over 900 files. Weeks later they want three of those screenshots back. They open Quarantine as the user guide instructs, search, and find nothing — the page is empty or shows only duplicate operations. The files are sitting in `%LOCALAPPDATA%\DuplicateTransferManager\sorting\quarantine` under names the user cannot map back, and the only in-app undo is an all-or-nothing rollback of the entire 900-file run.

**Suggested fix:** Have the sort executor write a `QuarantineRecord` manifest under `paths.quarantine` for `SortAction.QUARANTINE` (reusing `DuplicateQuarantineService`'s operation-folder and manifest format) so both producers feed one Quarantine page, or rename the sort action to something distinct ("Hold"/"Set aside") and document where those files go in USER_GUIDE.md alongside the Recycle Bin caveat already noted in PRIVACY.md:84-86.

### 63. [medium/purpose] Android settings are persisted and round-tripped but never applied

**Location:** `src/duplicate_transfer_manager/core/contracts.py:107-113; src/duplicate_transfer_manager/ui/pages.py:2601-2606, 2677-2679, 1283-1284, 1408-1418, 670, 1460, 639, 1427; src/duplicate_transfer_manager/services/support_services.py:365`

**Claim:** README.md:46-48 lists "Android behavior" among what the user can configure. Three of the four Android fields have no reader outside the widgets that write them. `android_enabled` (contracts.py:107, written at pages.py:2677) gates nothing — the Android source tile (pages.py:397), device picker, ADB browse, and Android hash cache are built unconditionally, so unchecking "Enable Android features" has no effect. `android_default_path` (contracts.py:111, written at pages.py:2678) is never read; both pages hardcode `/sdcard/DCIM` as a placeholder (pages.py:670, 1460) and `/sdcard` as the browse fallback (pages.py:639, 1427). `keep_android_awake` (contracts.py:108, written at pages.py:2679) is never applied — the import page hardcodes `self.keep_awake.setChecked(True)` (pages.py:1283-1284) and `ImportPage.update_preferences` (pages.py:1408-1418) refreshes only favourite locations. `android_platform_tools_version` (contracts.py:113) has no reader at all; the version README.md:73-76 says "diagnostics display" comes from `packaging/android_platform_tools_manifest.json` (support_services.py:365). Meanwhile `android_platform_tools_path`, the one field that *is* honoured (ui/app.py:45-46), has no UI control at all.

**Failure scenario:** A user whose camera folder is `/sdcard/Pictures` sets "Default Android path" in Advanced Settings and saves; the banner confirms "Settings saved on this PC." Every subsequent import still opens on `/sdcard/DCIM` and every ADB browse still starts at `/sdcard`. A second user turns off "Enable Android features" to keep a shared machine off USB devices; the Android source tile, device list, and ADB browse are all still there and fully functional. A third user who has their own platform-tools build has no way to point the app at it short of editing settings.json by hand.

**Suggested fix:** Read `android_default_path` when seeding the Android path selectors and browse roots, seed `self.keep_awake` from `settings.keep_android_awake` in both the constructor and `update_preferences`, and either gate the Android source/device/ADB controls on `android_enabled` or delete that checkbox. Delete `android_platform_tools_version` and add a path picker for `android_platform_tools_path`, which is already wired at ui/app.py:45-46.

### 64. [medium/purpose] The documented legacy Tkinter frontend moves duplicates with no manifest, contradicting the app's quarantine safety model

**Location:** `ui_components.py:621-638, documented at README.md:114-118`

**Claim:** README.md:188-189 states the product's safety model: "Duplicate quarantine can move local files only after scan, review, and explicit confirmation" into app-managed quarantine with restore. The PySide6 path honours that through `DuplicateQuarantineService`, which writes a manifest per operation and supports `restore_record`/`restore_operation`. The legacy Tkinter `OrganizerFrame._run_scan` instead does `shutil.move(dup.path, target)` at ui_components.py:633 into a user-chosen "Move Duplicates To" folder, with `ensure_unique_path` for naming and nothing else — no `QuarantineRecord`, no manifest under `paths.quarantine`, no restore path. README.md:114-118 documents `python legacy_main.py` as a supported way to run the app, and `legacy_main` and `ui_components` are shipped as py-modules in pyproject.toml, with `tkinterdnd2` a hard runtime dependency solely for them. Two shipped duplicate-handling implementations disagree about the product's central safety promise.

**Failure scenario:** A user follows the README's compatibility instructions, runs `python legacy_main.py`, picks an isolate folder and clicks "Find & Sort Duplicates". Several thousand originals are moved out of their folders. Later they open the PySide6 app's Quarantine page to undo it — and it is empty, because `QuarantineService.list_records` only reads manifests under `paths.quarantine` (support_services.py:330-352) and none were written. The only record of what moved where is a `MOVED:` line in a session log, and reversing it means matching flattened, possibly renamed files back to folders by hand.

**Suggested fix:** Decide which one ships. Either point the legacy Organizer's isolate step at `DuplicateQuarantineService` so it writes the same manifests the restore UI reads, or remove `legacy_main.py`/`ui_components.py` from `py-modules`, drop the `tkinterdnd2` dependency, and delete the README section that recommends running it. Leaving a documented path that silently bypasses the recovery guarantee is the worst of the three options for a pre-1.0 release.

### 65. [low/execution-time] Backup index is derived by globbing the whole backup directory on each conflict

**Location:** `src/duplicate_transfer_manager/sorting/executor.py:355 and :471`

**Claim:** The replaced-file backup name embeds `len(list((run_root / 'replaced').glob('*')))`, so each overwrite enumerates every backup written so far — O(k^2) directory entries across k overwrites. `_restore_for_undo` has the identical pattern with `len(list(backup_dir.glob('*')))` at :471, making undo O(k^2) as well. The glob result is used only as a monotonic counter that the loop could keep in a local variable.

**Failure scenario:** A user runs a large re-sort with Overwrite as the conflict policy and 5,000 destinations collide. The app enumerates roughly 12.5 million directory entries purely to compute filename prefixes, on top of the actual copying — noticeable stall growth as the run progresses, and the same again if they later undo it.

**Suggested fix:** Track the next index in a counter on the executor run (initialized once from the directory listing, incremented in memory), or use `tempfile.mkstemp`/uuid in the backup directory since the index carries no semantics beyond uniqueness.

### 66. [low/logic] A transfer with per-file errors is reported as COMPLETED and OperationResult.successful is True

**Location:** `src/duplicate_transfer_manager/services/transfer_service.py:94-125, 137-138; src/duplicate_transfer_manager/core/contracts.py:92-94`

**Claim:** `TransferService.run` sets `status = OperationState.FAILED` only when `raw["preflight_failed"]` or `raw["adb_device_failed"]` is set (line 96); a run where the engine reports `errors: N` with N > 0 falls through to `status = OperationState.COMPLETED` (line 99). The service does build an `OperationFailure` for those errors (lines 117-124), so the returned `OperationResult` carries `status == COMPLETED` together with a non-empty `failures` tuple, and `OperationResult.successful` (contracts.py:93-94) returns True. `resume_information["can_resume"]` is also computed as `status in {CANCELLED, FAILED}` (line 153), so it is False for exactly the runs that have unfinished files. `DuplicateScanService` has the same shape at duplicate_service.py:144-155 (status COMPLETED with a populated `failures` tuple built from `discovery.errors`). The UI happens to compensate by recording `"warning"` when `counts["errors"]` is non-zero (pages.py:1702), but the contract itself asserts success.

**Failure scenario:** An import of 5,000 files hits 300 read errors on a flaky USB connection. `execute_smart_transfer` returns `errors: 300`, the service returns `status=COMPLETED`, `successful=True`, and `can_resume=False`. Any consumer that trusts `OperationResult.successful` — the natural thing for a future CLI wrapper, the scheduled-scan entry points, or a headless test — concludes the transfer finished cleanly, and the resume affordance is switched off for precisely the run that needs it. The 300 files are silently absent from the destination.

**Suggested fix:** Treat a non-empty `failures` tuple as disqualifying: set `status = OperationState.FAILED` (or introduce a `COMPLETED_WITH_ERRORS` state, since `OperationState.terminal` already handles the three-way split) when `error_count > 0`, and include that state in the `can_resume` set. Alternatively redefine `OperationResult.successful` as `self.status == COMPLETED and not self.failures` so no caller can read success past a recorded failure — that change is contained to contracts.py and fixes the duplicate-scan path at the same time.

### 67. [low/logic] Bare `except:` in HashCache hides a cache that is never being saved

**Location:** `utils.py:82, utils.py:91, utils.py:69`

**Claim:** `HashCache.save` wraps `os.makedirs` + `open` + `json.dump` in `except: pass` (utils.py:91) and `HashCache.load` swallows everything into `self.data = {}` (utils.py:82). The write is also non-atomic — it truncates `self.path` in place rather than using the tmp+`os.replace` pattern that `DriveHashCache.save` (drive_cache.py:63-71) and `_atomic_json_write` (support_services.py:38-56) use. `SessionLogger.log` likewise swallows log-write failures at utils.py:69. Note the lock at utils.py:74 covers `get`/`set` but not `save`, so `json.dump` can iterate `self.data` while a worker mutates it — the resulting `RuntimeError` is then swallowed by the same bare except.

**Failure scenario:** The user's profile disk fills up, or an antivirus product locks `hash_cache.json`. Every `save()` fails silently. The user notices only that every duplicate scan takes as long as the first one did, forever, with nothing in the log and nothing in Settings to explain it. If the app is killed while `save()` is mid-write, the truncated file is read back as an empty cache on next launch — again silently.

**Suggested fix:** Catch `(OSError, TypeError, ValueError)` explicitly, log the failure through the session logger, and route the write through `_atomic_json_write` so a crash cannot truncate the file. Take `self.lock` around the `json.dump` (or snapshot `dict(self.data)` under the lock first) so a concurrent `set` cannot corrupt the serialization.

### 68. [low/purpose] Overview's "Connected devices" metric is hardwired to zero — the dashboard never queries for devices

**Location:** `src/duplicate_transfer_manager/ui/pages.py:302-347 (with services/support_services.py:585, 605-610)`

**Claim:** `OverviewPage.refresh` calls `self.dashboard_service.summary(include_storage=include_storage)` and never passes `include_devices=True`. `DashboardService.summary` defaults `include_devices=False` (support_services.py:585) and only populates `connected_devices` when that flag is set (605-610). So `devices` is always `[]`, meaning the metric card "Connected devices — Android devices available for duplicate scans or imports" always reads 0 and the storage row "Connected devices" always reads "None", including the Refresh button path.

**Failure scenario:** User plugs in an authorised phone, opens Overview and sees "0 Connected devices" and "Connected devices: None". They click Refresh; it still says 0. They conclude the app cannot see their phone and give up — even though the Import page's device dropdown lists it correctly.

**Suggested fix:** Either pass `include_devices=True` from a deferred, off-thread refresh (via `DeviceController`, since `list_devices()` is a blocking subprocess and must not run in `refresh`), or remove the metric card and storage row so the dashboard stops making a claim it never evaluates.

### 69. [low/purpose] An entire second sorting engine and six Qt controllers are defined, exported, and unreachable from the UI

**Location:** `src/duplicate_transfer_manager/controllers/support.py:58-183, controllers/operations.py:54-85, services/support_services.py:163-186`

**Claim:** `DeviceController`, `ReportsController`, `QuarantineController`, `SettingsController`, `DiagnosticsController`, `UpdateController` and `FileOrganizerController` are all defined and re-exported from `controllers/__init__.py`, but a repository-wide search finds no instantiation outside `build/` and tests — the UI does all of that work synchronously instead (see the ADB, quarantine, restore and activity findings above). Likewise `FileOrganizerService` (611 lines, services/organizer_service.py) has `build_plan`/`organize` reachable only from `scheduled_organizer.py` and tests; `SortWorkspace` uses the unrelated `HybridSortService`. `ScheduledScanService.configure_organizer_preview` (support_services.py:163-186) has no caller in the UI at all, and `ActivityPage.refresh`'s "Sorting" filter matches `organization*` record types (pages.py:1911) that the interactive app can never produce.

**Failure scenario:** A maintainer fixing the main-thread ADB freeze reads `DeviceController` and reasonably assumes device listing is already off-thread; it is not. A user who reads the Activity page's "Sorting" filter expecting organizer runs never sees any, because the only path that writes `organization` records is a scheduled task the UI provides no way to configure.

**Suggested fix:** Either adopt the support controllers where the pages currently block (which also fixes several findings above), or delete the unused controllers and the organizer service/scheduling path and drop the `organization*` branch from the Activity filter, so there is one sorting engine and one threading pattern.

### 70. [low/purpose] The duplicates page reimplements the service's keep-selection rule instead of reusing it

**Location:** `src/duplicate_transfer_manager/ui/pages.py:964-995 (duplicates services/duplicate_workflow.py:195-257)`

**Claim:** `DuplicatesPage._apply_preference` contains its own `quality_key` closure (pages.py:971-980) and its own oldest/newest ordering, which are line-for-line re-derivations of `_quality_key` (duplicate_workflow.py:195-204) and the `prefer` branch inside `build_duplicate_review` (duplicate_workflow.py:251-257). The two are identical today, so there is no live divergence — but the keep decision, which determines which file survives a quarantine, now has two independent implementations, only one of which is covered by service tests.

**Failure scenario:** A future change to the tie-break rule (say, preferring the shorter path on equal pixels) is applied in `duplicate_workflow._quality_key` only. From then on the initial review shows one keeper, and the moment the user clicks the same 'Highest resolution' radio the UI silently picks a different keeper — so which copy survives depends on whether the user touched the radio button.

**Suggested fix:** Export the preference logic from `duplicate_workflow` (e.g. a `reselect_keepers(review, prefer)` helper) and have `_apply_preference` call it, deleting the UI-side copy.

### 71. [low/purpose] Default-profile and default-category settings only apply at page construction, so saving them appears to do nothing until restart

**Location:** `src/duplicate_transfer_manager/ui/pages.py:1221-1225, 1236-1237, 1408-1418, 599, 621-630; src/duplicate_transfer_manager/ui/shell.py:262-280, 326-328`

**Claim:** All eight pages are constructed once in `MainWindow.__init__` (shell.py:262-280) and reused for the process lifetime. `_preferences_saved` (shell.py:326-328) forwards saved settings to `update_preferences` on only the duplicates and import pages, and `ImportPage.update_preferences` (pages.py:1408-1418) refreshes nothing but the two favourite-location combo boxes. `default_transfer_profile` is applied only in the constructor (pages.py:1236-1237) and `default_file_categories` only in the constructor's checkbox loop (pages.py:1221-1225), so neither takes effect on a page the user has already visited. Nothing forwards saved settings to `SortWorkspace` at all, despite it holding a live `settings` reference it mutates (sort_workspace.py:904).

**Failure scenario:** A user runs an import, decides Reliable should be their default, goes to Settings, changes "Default transfer profile" to Reliable, saves, and returns to Import Files. The profile combo still reads "Balanced — recommended". They change it again, save again, same result, and conclude the setting is broken. It in fact took effect — it will be visible the next time they launch the app.

**Suggested fix:** Extend `ImportPage.update_preferences` and `DuplicatesPage.update_preferences` to re-seed the profile combo and category checkboxes from the new `AppSettings` (guarding against clobbering values the user has already overridden for the pending run), and forward saved settings to `SortWorkspace` from `MainWindow._preferences_saved`.

### 72. [low/purpose] Scheduled scan results are excluded from the Activity page's "Duplicate scans" filter

**Location:** `src/duplicate_transfer_manager/ui/pages.py:1904; src/duplicate_transfer_manager/scheduled_scan.py:60-68, 21-27, 45-51`

**Claim:** README.md:41-42 says scheduled runs "only record duplicate findings locally", and USER_GUIDE.md:140-142 points users at Activity to see "what the app has done: scans, imports, quarantine actions, and failures." `ActivityPage.refresh` filters the "Duplicate scans" category with an exact match, `record.get("type") != "duplicate_scan"` (pages.py:1904), but every record the scheduled scan writes uses the type `"scheduled_duplicate_scan"` (scheduled_scan.py:61, and likewise for its failure paths at 22 and 46). The "Sorting" filter immediately below (pages.py:1908-1913) does handle its scheduled variant explicitly, so the omission is inconsistent within the same function.

**Failure scenario:** A user enables the weekly read-only scan, waits two weeks, then opens Activity and selects the "Duplicate scans" filter to check whether it ran. The list is empty, so they conclude the schedule never fired and go looking for a problem — when the records exist and are visible only under the unfiltered view.

**Suggested fix:** Match the pattern already used for sorting: accept both `duplicate_scan` and `scheduled_duplicate_scan` in the "Duplicate scans" branch at pages.py:1904 (a `startswith`/set membership check), and add a test covering a scheduled record surviving each filter.

