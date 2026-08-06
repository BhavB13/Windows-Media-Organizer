# Implementation Tasks — Agreed Findings for `b3430f6`

Status: reconciled between Claude (Opus 5) and Codex (GPT-5.6-Sol) on 2026-08-06.
Inputs: `docs/HANDOFF_CODEX_REVIEW.md`, `docs/CODEX_REVIEW_RESPONSE.md`.

Every item below was independently verified at the cited `file:line` by both
reviewers. Items where the two reviews disagreed are recorded in section 0 with
the resolution.

## 0. Reconciliation record

| Item | Claude | Codex | Agreed outcome |
| --- | --- | --- | --- |
| F1–F10 | raised | CONFIRMED, some severities adjusted | agreed as stated below |
| F11 (settings don't reach Sort Files) | raised | WRONG | **withdrawn.** `MainWindow` hands the *same* `AppSettings` instance to `SettingsPage` (`shell.py:277`) and `SortWorkspace` (`:274`), and `SettingsPage._save()` mutates it in place (`pages.py:3176-3203`), so values do propagate. `SortWorkspace` also never reads them. Not a defect. |
| F12 cross-volume read count | "three reads" | five I/O passes | **Codex correct**; one copy read plus four SHA-256 reads (`executor.py:330`, `:396-410`, `:425`, `:353`). Perf only. |
| M1–M10 | missed | raised | verified and accepted, with two severity adjustments noted in T1 and T11 |
| `codex/media-organizer-publish` not merged | proposed | agreed | branch stays unmerged; delete only as a separate deliberate action |

Two nuances added during verification that neither review stated initially:

- **T1**: the duplicate page's hash-mode default is `Full content — safest`
  (`pages.py:472`, first combo item). Fast sampling is opt-in, which lowers the
  blast radius but does not remove the hazard.
- **T11**: no caller anywhere passes `operation_id` into
  `DuplicateQuarantineService.quarantine()`, so the path-traversal half of that
  finding is API-surface hardening, not a live exploit.

---

## Tier 1 — Data-safety blockers (must land before any public build)

### T1. Confirm duplicates with full content before any destructive action

`engine.py:38-42` — in `fast` mode, files over 2 MiB are hashed as
`size + first 1 MiB + last 1 MiB`. `group_duplicates` (`engine.py:269-298`)
treats that digest as definitive, and `duplicate_workflow.py:270-288` then
physically moves group members into quarantine. Two same-size files with
matching ends and different middles are presented as duplicates, so a user can
quarantine a file that has no surviving twin.

Fix: treat sampled digests as a *candidate* filter only. Before a group reaches
review (or at minimum before quarantine executes), re-hash every member of every
candidate group with a full-content SHA-256 and re-split the groups on the
result. Surface the revalidation as a scan stage so the user sees why it takes
longer.

Tests: two >2 MiB files sharing size, first MiB, and last MiB but differing in
the middle must produce **zero** duplicate groups in fast mode; an identical pair
must still group.

### T2. Make replace-restore transactional (both code paths)

- `duplicate_workflow.py:339-343` — `resolved.unlink()` then `shutil.move()`.
- `sorting/executor.py:446-454` + `:214-224` — `_undo_target()` unlinks the
  original path for `OVERWRITE`, then `undo()` moves.

In both, a failure after the unlink leaves the incumbent file permanently gone.

Fix: move the incumbent into an app-owned backup directory, restore into a
temporary name, verify, promote atomically, and only then delete the backup. On
any failure, restore the incumbent and keep both the backup and the journal
entry.

Tests: force the move to fail (patch `shutil.move` to raise) and assert the
incumbent file still exists with its original content.

### T3. Stop `cleanup_partial_files` from deleting unrelated user files

`transfer_safety.py:203-216` walks the entire tree under `root` and `os.remove`s
every file ending in `.partial`, with no ownership check. `pages.py:1600-1617`
points that at the user's own library or save location, and the confirmation text
only promises not to touch sources or completed imports. The transfer engine
writes plain `f"{target_path}.partial"` (`engine.py:655`), so app partials are
indistinguishable from a user's `download.partial`.

Fix: delete only paths the app can prove it owns — journal-recorded partial paths
or an app-owned staging directory. Rename the engine's suffix to a namespaced
one (the sort executor already uses `.dtm-partial`, `executor.py:397`). Prefer
moving candidates to quarantine over unlinking. Update the confirmation copy to
state exactly what will be removed.

Tests: a user file named `download.partial` under the selected root must survive
a live cleanup; a journal-recorded app partial must be removed.

### T4. Journal quarantine moves before mutating, and write manifests atomically

`duplicate_workflow.py:270-303` moves every selected item and only then writes
the sole manifest at `:304`. `_write_manifest` (`:391-411`) and `_mark_restored`
(`:413-425`) both use plain `write_text()`. A crash mid-loop, or one torn write,
leaves files displaced with no recovery record — `list_records()` silently skips
unreadable manifests (`:379-389`), so the whole operation disappears from the UI.

Fix: write and fsync an initial manifest before the first move, append a
checkpoint per item, and make every manifest write temp-file + `os.replace`.
Reuse `_atomic_json_write` from `services/support_services.py:35`.

Tests: simulate a crash after the first move and assert the operation is still
listed and restorable; assert a truncated manifest does not silently vanish.

### T5. Preserve the conflict policy at commit time in the transfer engine

`engine.py:611-625` picks a target via `resolve_conflict_path()`, then
`:626-657` promotes with `os.replace()`, which overwrites whatever occupies that
path at commit time — even under the default `rename` policy. `sorting/executor.py:341-343`
already detects this class of post-preview change; the transfer engine does not.

Fix: use a no-clobber promotion for `rename`/`skip` (open with `O_EXCL` or
re-reserve a unique name and retry on collision). Only an explicitly approved
`replace` may overwrite, and it must keep a rollback backup.

Tests: create the resolved target between resolution and promotion and assert the
pre-existing file is not overwritten under `rename`.

### T6. Never cache a cancelled hash

`engine.py:44-55` — the full-hash loop exits on `stop_event.is_set()`, then
still computes `h.hexdigest()` and writes it to `hash_cache` keyed by the
original `(path, algo, mode, size, mtime)`. The cached value covers only a prefix
— or no content at all if cancellation was already set — and later uncancelled
runs consume it. Poisoned prefix digests can make distinct files match, which
feeds straight into T1.

Fix: on cancellation, return no digest and write nothing to the cache. Give
callers a way to distinguish cancellation from hash failure (both currently
return `""`).

Tests: cancel mid-hash, assert no cache entry is written and a later full run
produces the true digest.

---

## Tier 2 — Updater trust chain (must close before automatic updates ship)

### T7. Fail closed on a missing trust root

`services/support_services.py:922-932` — `_resource_path()` falls back to
`Path.cwd() / relative_path`, which `UpdateService` uses for
`packaging/update_public_key.json` (`:612-615`, `:899-911`). Raise instead. A
missing trust root must be a hard error, never a search path.

### T8. Bound and stage the installer download

`services/support_services.py:786-798` — `response.read()` is unbounded and
writes straight to the final `.exe`, verified only afterwards. Stream in chunks,
abort past the manifest-declared `size`, reject non-positive or implausible
declared sizes, write to `.partial`, verify, then `os.replace`. Delete the
partial on any failure.

### T9. Validate update URLs

`services/support_services.py:669` and `:789` pass URLs straight to `urlopen()`,
which honours `file://` and `http://`. Require `https://` and pin the expected
release host, including redirect targets.

### T10. Tighten manifest verification

- Reject non-strict PKCS#1 v1.5: `core/security.py:112-123` never checks that the
  padding run is all `0xFF`. Verify `decoded[2:separator] == b"\xff" * n`.
  (Not known-exploitable at `e=65537` — this is strictness, not a live break.)
- Make `authenticode_thumbprint` a required, non-empty signed field
  (`:691-704`), and stop returning `True` from `verify_authenticode()` for any
  valid signer when the expected thumbprint is empty (`:876-882`).
- Don't reset the check timer on the skip path (`:661-667` calls `record_check`,
  which always stamps `checked_at` at `:884-889`).
- Use a standards-compliant version parser: `_compare_versions` (`:935-945`)
  reduces `0.9.0rc1` to `(0, 9)`, equal to `0.9`.

### T11. Harden quarantine operation identity

`duplicate_workflow.py:101-102` gives operation IDs one-second resolution, and
`:259-266` joins a caller-supplied ID into the quarantine path unvalidated with
`exist_ok=True`, so two operations in the same second merge and the second
manifest overwrites the first (`:399-410`). No caller currently passes an ID, so
the traversal risk is API-surface only — fix both anyway: unpredictable unique
IDs, reject an existing operation directory, validate any supplied ID as a single
safe path component, and assert the resolved directory stays under
`paths.quarantine`.

---

## Tier 3 — Correctness and hygiene

### T12. Verify resume against the recorded digest, not just size

`transfer_safety.py:68-74` stores a digest that `is_complete()` (`:82-90`) never
consults — it compares sizes only, and `engine.py:487-491` skips on that basis.
A target corrupted to different content of the same length is treated as
complete. Validate the digest (or a fingerprint bound to size and mtime with a
documented risk policy) before skipping.

### T13. Fix migrated organizer presets losing their category folders

`sorting/persistence.py:117-129` builds a `MOVE` association carrying
`rename_template="{media_type}/{stem}{suffix}"`, but `sorting/planner.py:135-140`
applies `rename_template` only when the action is `RENAME`. Migrated type-mode
presets therefore flatten into the destination root. Encode the category in the
destination template instead, and add a migration test asserting the resulting
layout.

### T14. Ship Recycle Bin support or remove the action

Add `Send2Trash==1.8.3` to `requirements.txt` (it is declared at
`pyproject.toml:19-23` but missing from `requirements.txt:1-4`). The release job
installs only `requirements-dev.txt` (`.github/workflows/release.yml:39-48`), so
the `send2trash` hidden import (`packaging/duplicate_transfer_manager.spec:19-24`)
cannot resolve and every recycle action fails at
`sorting/executor.py:331-334`. Tests miss it because `tests/test_sorting.py:587`
patches `_send_to_recycle`. Add a packaging test that imports the real module.

### T15. Space-check cross-volume renames

`sorting/executor.py:368-377` excludes `SortAction.RENAME` from
`_validate_space()`, but `_move()` falls back to copy-and-delete across volumes
(`:417-426`). Include RENAME whenever the source and destination volumes differ.

### T16. Close the diagnostics filename gap

`core/security.py:31-33` — `PATH_PATTERN` only matches rooted paths, so a bare
`IMG_1234.jpg` in free text survives `sanitize_text()`, contradicting the
documented "no filenames" guarantee. Separately, `ANDROID_SERIAL_PATTERN`'s
`\b[A-Z0-9]{8,}\b` branch (`:35`) redacts ordinary words like `PERMISSION`.
Tighten both, and keep production diagnostics disabled until this is tested.

### T17. Make test invocation deterministic

There is no `tests/conftest.py`, `tests/__init__.py` is empty, and no test
imports `runtime_paths`. `src/` reaches `sys.path` only because
`tests/test_engine.py` sorts first and imports the root `engine` module, which
pulls in `transfer_safety` → `runtime_paths` (`transfer_safety.py:9-10`,
`runtime_paths.py:3-11`). Verified in a clean venv built from
`requirements-dev.txt`: the full discovery run passes 189 tests, but
`python -m unittest tests.test_sorting` and `tests.test_phase7` both fail with
`ModuleNotFoundError: No module named 'duplicate_transfer_manager'`.

Add `tests/conftest.py` (or a bootstrap in `tests/__init__.py`), and fix the
`AGENTS.md` validation section — the `python -m pytest tests/test_engine.py -q`
example does not work in an environment without an editable install, and `pytest`
is not installed in the project `.venv` despite being pinned.

### T18. Remove dead weight

- Delete `OrganizerPage` (`ui/pages.py:1702-2281`, ~580 lines) and its three
  tests (`tests/test_phase2_ui.py:33,278,300,327`). The `sort` route builds
  `SortWorkspace` (`ui/shell.py:274`); the page is unreachable and its passing
  tests are a false coverage signal. Keep `FileOrganizerService` — migration
  still uses it.
- Delete `unused.json` (tracked, contains `{}`).
- Replace `sort_workspace.py:465-470`'s `self.advanced_panel._toggle(True)` with
  a public API on `DisclosurePanel`.

### T19. Close the app after launching the installer

`services/support_services.py:831-844` starts the installer and returns; nothing
quiesces operations, persists state, or shuts down. `OVERHAUL_PLAN.md` Phase 7
requires an orderly close. Implement at the application layer, not inside the
service, and handle launch failure.

---

## Owner actions (not code — cannot be delegated)

### O1. Rotate the update signing key — hard release gate

`packaging/update_public_key.json:4` is a **1024-bit** modulus (256 hex digits),
labelled `dtm-dev-release-key-2026`, and the file itself says to replace it
before release. RSA-1024 has been disallowed by NIST since 2010 and this key is
the trust root for code updates on user machines.

Generate an RSA-3072/4096 (or Ed25519) keypair, store the private key as the
`UPDATE_MANIFEST_PRIVATE_KEY_PEM` GitHub secret, commit only the new public key,
and add a test asserting the shipped binary carries the production key.

### O2. Constrain scheduled live sorting

`scheduled_sort.py:19-31` lets a single `monitor.live_approved` boolean authorize
every future unattended run. Both reviewers agree perpetual unlimited batches are
too broad for v1: bind approval to an immutable tuple of profile revision,
monitored root, allowed actions, destinations, and conflict policy; revoke it on
any change; add per-run file-count and byte caps; default to prohibiting recycle
and overwrite; stop on any review or conflict row.

### O3. Decide the retry/resume approval rule

`sorting/executor.py:268-287` — `retry_failed` and `resume_run` manufacture their
own approval. Agreed position: resume may reuse the original approval only after
revalidating source identity, destination occupancy, and profile revision;
failed-item retry should normally return to Review, since failure usually means
the environment changed.

---

## Manual verification still outstanding

No Windows GUI interaction, real ADB device, PyInstaller build, Inno Setup build,
or GitHub Actions release run has been executed against this commit.
`docs/TARGETED_FIX_MANUAL_CHECKLIST.md` covers the device and GUI paths.
