# Handoff — Codex Review of Commit `b3430f6`

Author: Claude (Opus 5) · Date: 2026-08-06 · Repo: `Windows-Media-Organizer`
Branch state: `main` == `V1` == `origin/main` == `b3430f6`

## 1. What this handoff is for

Commit `b3430f6` landed a large body of previously uncommitted work. This
document records exactly what shipped, what was verified, and a findings list
produced from reading the code. **Your job is to proofread it**: confirm,
correct, or reject each finding, and add anything material that was missed.

Do not change code in this pass. Review only.

## 2. Branch reconciliation (completed)

| Branch | Before | After |
| --- | --- | --- |
| `V1` (local, held all work) | `e151455` + 79 uncommitted files | `b3430f6`, pushed |
| `main` (local) | `380d29b`, stale by two commits | fast-forwarded to `b3430f6` |
| `origin/main` | `e151455` | `b3430f6` |
| `origin/V1` | did not exist | created at `b3430f6` |
| `codex/media-organizer-publish` | `c9668f1`, remote already deleted | left untouched, nothing merged |

`codex/media-organizer-publish` was checked and deliberately not merged.
`git diff -w 380d29b..c9668f1` over every code file returns **empty** — the
branch's only content is CRLF/LF churn, a stale README for the old
"Media Organizer Pro" name, and runtime data (`transfer_reports/`,
`transfer_state/`) that Phase 0 intentionally untracked and `.gitignore` now
excludes. Merging it would regress the product rename and re-add ignored
runtime data. Please confirm you agree with that call.

## 3. What commit `b3430f6` contains

79 files, +11,178 / −207.

**Reliability and privacy (Phase 6)**

- `src/duplicate_transfer_manager/core/security.py` (new) — recursive payload
  sanitization, local correlation IDs, canonical manifest JSON, RSA-SHA256
  PKCS#1 v1.5 signature verification.
- Sanitized diagnostics and local crash reports; Sentry remains opt-in.
- `CrashReportService` plus a local exception hook installed in `ui/app.py`.

**Packaging, signing, updates (Phase 7)**

- `packaging/duplicate_transfer_manager.spec`, `packaging/installer.iss`,
  `packaging/release_manifest.example.json`, `packaging/update_public_key.json`,
  `assets/app.ico`.
- `.github/workflows/release.yml` — PyInstaller build, Authenticode signing,
  Inno Setup installer, signed update manifest, draft GitHub Release.
- `scripts/build_release.ps1`, `scripts/sign_update_manifest.py`,
  `scripts/create_app_icon.py`, `scripts/smoke_ui.py`.
- `UpdateService` verification: required fields, channel match, downgrade
  protection, minimum supported version, manifest signature, installer size,
  SHA-256 checksum, Windows Authenticode status.

**Sort Files (new subsystem)**

New package `src/duplicate_transfer_manager/sorting/` — `models`,
`persistence`, `metadata`, `rules`, `ml`, `planner`, `executor`, `monitor`,
`scheduler`, `session`, `presets`, `migration`, `workflow`. Plus
`SortController`, `ui/sort_workspace.py` (1,578 lines), and
`scheduled_sort.py` / `scheduled_organizer.py` / `scheduled_scan.py` entry
points. The `sort` route now builds `SortWorkspace`; the older
`FileOrganizerService` is retained for non-destructive manifest migration.

**Docs and tests**

- `AGENTS.md`, `docs/SORT_FILES_ARCHITECTURE.md`, `docs/PHASE_6_7_AUDIT.md`,
  `docs/TARGETED_FIX_MANUAL_CHECKLIST.md`, `docs/FILE_ORGANIZER_PLAN.md`.
- `tests/test_phase6.py`, `test_phase7.py`, `test_sorting.py` (801 lines),
  `test_organizer.py`, plus expanded Phase 1–5 coverage.

## 4. What was actually verified

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests` in the project `.venv` | 189 tests, OK, 314 s |
| Same suite in a throwaway venv built from `requirements-dev.txt` only | 189 tests, OK, 277 s |
| `python scripts/smoke_ui.py` | passes |
| `compileall` over `src/`, `tests/`, `scripts/`, root modules | clean |
| Engine/UI boundary — grep for `PySide6`/`tkinter` in `core/`, `services/`, `sorting/`, root engine modules | **zero hits**; Qt confined to `ui/` and `controllers/qt_compat.py` |
| `shell=True` / `os.system` anywhere | zero hits; ADB uses argument lists |
| Secret scan over committed files | clean; only the public verification key is committed |

Not run: any Windows GUI interaction, a real ADB device, a PyInstaller build,
an Inno Setup build, or the GitHub Actions release workflow.

## 5. Findings for you to check

Each item lists the claim and how it was reached. Please mark each
**CONFIRMED**, **WRONG** (with reasoning), or **NEEDS TEST**.

### F1 — `requirements.txt` omits `Send2Trash`; shipped builds lose Recycle Bin

`pyproject.toml` declares `Send2Trash==1.8.3`, but `requirements.txt` lists only
Pillow, PySide6, tkinterdnd2. `.github/workflows/release.yml` installs
`requirements-dev.txt` (which only chains `requirements.txt`) and never runs
`pip install -e .`, so PyInstaller's `hiddenimports=["send2trash"]` cannot
resolve. `sorting/executor.py:22-25` degrades to `_send_to_recycle = None` and
`executor.py:332-333` then raises "Recycle Bin support is unavailable in this
installation" — permanently, in every released build.

Verified: a clean venv built from `requirements-dev.txt` reports
`No module named 'send2trash'`. Tests cannot catch this because
`tests/test_sorting.py:587` patches `_send_to_recycle`.

Severity: **high** — advertised feature silently absent from every release.

### F2 — Update trust root falls back to the current working directory

`services/support_services.py:922-932`, `_resource_path()`:

```python
source_candidate = source_root / relative_path
if source_candidate.exists():
    return source_candidate
return Path.cwd() / relative_path
```

`UpdateService.__init__` uses this to locate `packaging/update_public_key.json`.
If the bundled and source copies are both missing, the app loads its update
signing key from whatever directory it happens to be started in. Anyone able to
influence the working directory can substitute their own key and then present a
manifest that passes every signature check.

Suggested fix: raise instead of falling back. A missing trust root must be a
hard error, never a search.

Severity: **high** (security).

### F3 — Update signing key is RSA-1024

`packaging/update_public_key.json` — `n` is 256 hex characters, i.e. a 1024-bit
modulus, `key_id` `dtm-dev-release-key-2026`. NIST has disallowed RSA-1024 since
2010. This is the trust root for code updates on user machines.

Suggested fix: rotate to RSA-3072/4096 (or Ed25519) before public v1, and treat
the current key as development-only.

Severity: **high** (security).

### F4 — Installer download is unbounded, unstaged, and verified late

`services/support_services.py:787-798`:

```python
target = self.paths.updates / f"DuplicateTransferManagerSetup-{...}.exe"
with urlopen(str(manifest["installer_url"]), timeout=60) as response:
    target.write_bytes(response.read())
...
self.verify_manifest(manifest, installer_path=target)
```

`response.read()` pulls the whole body into memory with no cap even though the
manifest declares `size`, so a hostile or compromised host can exhaust memory.
The bytes also land directly on the final path and are only verified afterwards,
leaving an unverified `.exe` at a predictable location if verification fails.

Suggested fix: stream in chunks, abort past `manifest["size"]`, write to
`.partial`, verify, then `os.replace`; delete the partial on any failure.

Severity: **high**.

### F5 — A skipped update check still resets the 24-hour timer

`services/support_services.py:661-667`:

```python
if not force and not self.check_due():
    result = {"checked": False, "reason": "last check was less than 24 hours ago"}
    self.record_check(result)
    return result
```

`record_check()` writes `checked_at = now` unconditionally. An app opened at
least once per day therefore keeps pushing the deadline forward and may never
perform a real check.

Suggested fix: don't call `record_check` on the skip path.

Severity: **medium**.

### F6 — PKCS#1 v1.5 padding bytes are not validated

`core/security.py:118-123` checks the `00 01` prefix, requires a separator at
index ≥ 10, and compares the DigestInfo tail exactly — but never checks that the
padding run is all `0xFF`. Non-conformant encoded messages therefore verify.
The exact-tail comparison rules out Bleichenbacher trailing-garbage forgery, and
`e = 65537` makes the classic low-exponent attack impractical, so this is
hardening rather than a live break — but a verifier should reject anything that
is not strict PKCS#1 v1.5.

Severity: **medium** (security hardening).

### F7 — No scheme or host validation on update URLs

`urlopen()` is called on a caller-supplied `manifest_url`
(`support_services.py:669`) and on `manifest["installer_url"]`
(`:789`). `urlopen` honours `file://` and plain `http://`. The installer URL is
covered by the manifest signature, but the manifest URL itself is not validated
at all.

Suggested fix: require `https://` and pin the expected release host before
fetching.

Severity: **medium**.

### F8 — Publisher identity is optional

`authenticode_thumbprint` is absent from the required-field list in
`verify_manifest()` (`support_services.py:691-704`), and
`verify_authenticode()` returns `True` for *any* validly signed binary when
`expected_thumbprint` is empty (`:878-882`). A manifest that simply omits the
field gets publisher pinning silently disabled.

Note: the PowerShell path itself is fine — `Get-AuthenticodeSignature |
ConvertTo-Json -Compress` at default depth does emit
`SignerCertificate.Thumbprint`, confirmed on this machine, and `Status`
serializes to `0` for Valid, which the code handles.

Severity: **medium**.

### F9 — Test imports work only by alphabetical accident

No `conftest.py` exists and no test imports `runtime_paths`. `src/` reaches
`sys.path` only as a side effect of `tests/test_engine.py` (first alphabetically)
importing the root `engine` module. Verified in a clean venv:

- `python -m unittest discover -s tests` → 189 tests OK
- `python -m unittest tests.test_sorting` → `ModuleNotFoundError: No module named 'duplicate_transfer_manager'`
- `python -m unittest tests.test_phase7` → same failure

`AGENTS.md` tells agents to run the smallest relevant subset
(`python -m pytest tests/test_engine.py -q`), which does not work as written —
and `pytest`, though pinned in `requirements-dev.txt`, is not installed in the
project `.venv`.

Suggested fix: add `tests/conftest.py` (or a `sys.path` bootstrap in
`tests/__init__.py`) and correct the AGENTS.md command.

Severity: **medium** (developer experience, silent CI fragility).

### F10 — `OrganizerPage` is unreachable dead code

`ui/pages.py:1702-2281` (~580 lines). The `sort` route builds `SortWorkspace`
(`ui/shell.py:274`); `OrganizerPage` is referenced nowhere outside its own
definition and three tests in `tests/test_phase2_ui.py:278/300/327`. Those tests
report passing coverage for a screen no user can open.

Severity: **medium** (maintenance and false coverage signal).

### F11 — Saved preferences never reach the Sort Files page

`ui/shell.py:325-327` refreshes only the duplicates and import pages:

```python
def _preferences_saved(self, settings: AppSettings) -> None:
    self.pages["duplicates"].update_preferences(settings)
    self.pages["import"].update_preferences(settings)
```

`SortWorkspace` defines no `update_preferences`, so Simple/Advanced mode and
other saved settings do not propagate to it until the app restarts.

Severity: **medium**.

### F12 — Smaller items

- `unused.json` — tracked, empty (`{}`), repo root. Delete.
- All eight pages are constructed eagerly in `MainWindow.__init__`
  (`shell.py:261-279`), including the 1,578-line `SortWorkspace`. Startup pays
  for screens that may never open; consider lazy construction.
- Sanitizer accuracy (`core/security.py:31-35`): `PATH_PATTERN` requires a drive
  letter, UNC prefix, or leading slash, so a bare `IMG_1234.jpg` inside a
  free-text message is **not** redacted — against the stated "no filenames"
  guarantee. Meanwhile `ANDROID_SERIAL_PATTERN`'s `\b[A-Z0-9]{8,}\b` branch
  redacts ordinary uppercase words such as `PERMISSION`.
- `_compare_versions` (`support_services.py:935-945`) drops non-numeric parts, so
  `0.9.0rc1` compares equal to `0.9`.
- `launch_verified_installer` (`:839`) starts the installer but never closes the
  app; `OVERHAUL_PLAN.md` Phase 7 says to close safely after launch.
- `SortExecutor._validate_space` (`executor.py:372`) skips `SortAction.RENAME`,
  so a cross-volume rename gets no free-space preflight.
- `executor.py:425` re-hashes the source inside `_move`'s cross-volume path
  after `_execute_item:330` already hashed it — three full reads per
  cross-volume move.
- `ui/pages.py` is 3,341 lines holding eight page classes; `sort_workspace.py`
  is 1,578. Both are past comfortable review size.
- `ui/sort_workspace.py:469` calls `self.advanced_panel._toggle(True)`, reaching
  into another widget's private method.

## 6. Design questions, not defects

- `scheduled_sort.py:24-31` auto-approves every non-review row for live
  unattended runs, gated on the profile-level `monitor.live_approved` flag. That
  matches the documented automation contract, but it means one approval
  authorizes every future scheduled run on new files. Flag if you think v1
  should require re-approval or cap batch size.
- `retry_failed` / `resume_run` (`executor.py:268-287`) re-approve their own
  items rather than requiring a fresh review pass.

## 7. What I want back

1. A verdict per finding: CONFIRMED / WRONG / NEEDS TEST, with reasoning where
   you disagree.
2. Anything material missed — especially in `sorting/planner.py`,
   `sorting/rules.py`, `sorting/persistence.py`, `services/duplicate_workflow.py`,
   and the root `engine.py`/`discovery.py`/`transfer_safety.py` modules, which I
   read less closely.
3. Your read on the `codex/media-organizer-publish` decision in section 2.
4. A severity ordering you would actually ship against.

No code changes in this pass.
