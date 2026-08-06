# Duplicate & Transfer Manager — Agent Notes

Project: **Duplicate & Transfer Manager** (`duplicate-transfer-manager`, pyproject v0.8.0).
A local-first Windows 10/11 desktop app for finding duplicate files and safely
transferring them (including Android via bundled ADB). Actively being rebuilt from a
Tkinter UI onto **PySide6** while preserving the tested Python transfer engine.

## First Reads

Read these before making changes:

1. `OVERHAUL_PLAN.md` — the phased Public v1 overhaul roadmap and current phase status
2. `docs/ARCHITECTURE.md` — engine/UI separation, controllers, threading model
3. `docs/PHASE_6_7_AUDIT.md` — most recent audit of Phase 6/7 work
4. `docs/TARGETED_FIX_MANUAL_CHECKLIST.md` — manual verification steps
5. `docs/FILE_ORGANIZER_PLAN.md` — the file-organizer feature plan (new, untracked area)

## Working Rules

- **The worktree has substantial uncommitted user work on branch `V1`.** Do NOT
  revert, reset, stash, or reorganize existing changes unless explicitly asked.
- Keep the **transfer engine framework-neutral** — discovery, hashing, duplicate
  grouping, ADB access, transfer safety, and copy logic must stay independent of
  PySide6. UI concerns live under `src/duplicate_transfer_manager/ui/`.
- Run all scanning/hashing/ADB/transfer work in Qt worker threads; never touch
  widgets or UI state off the main thread.
- Duplicates are **quarantined, not deleted**. Preserve that safety contract.
- Prefer small, verifiable slices. Preserve compatibility with existing JSON
  caches and transfer journals during v1.

## Repo Layout

- `src/duplicate_transfer_manager/` — packaged app (services, ui, version)
  - `services/` — engine orchestration: import_workflow, transfer_service,
    support_services, ios_transfer, organizer_service (new)
  - `ui/` — PySide6 shell, pages, app, theme, widgets
- Top-level legacy modules (`engine.py`, `models.py`, `utils.py`,
  `transfer_safety.py`, `adb_bridge.py`, `drive_cache.py`) — engine internals
- `tests/` — pytest suite, phase-organized (`test_phase1.py` … `test_phase7.py`,
  `test_engine.py`, `test_organizer.py`)
- `packaging/` — Inno Setup installer, PyInstaller spec, release manifests
- `scripts/`, `assets/`, `docs/`

## Validation

- Tests: `python -m pytest -q` (run the smallest relevant subset for changed files,
  e.g. `python -m pytest tests/test_engine.py -q`)
- Dev deps: `pip install -r requirements-dev.txt`
- Build/package artifacts live under `packaging/` (Inno Setup `installer.iss`,
  PyInstaller `duplicate_transfer_manager.spec`)

Run the smallest relevant validation for the files you changed, and note anything
you did not run in the handoff.
