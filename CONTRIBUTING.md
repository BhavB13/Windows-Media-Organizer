# Contributing

Thanks for looking at this project. It moves people's photo libraries around, so
the bar for changes that touch file operations is deliberately high.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m pip install -r requirements-dev.txt
```

Install the package itself (`pip install -e .`), not just the requirements.
Without it, running a single test module fails to import
`duplicate_transfer_manager`.

Run the app:

```powershell
python main.py
```

## Tests

```powershell
python -m unittest discover -s tests
python scripts\smoke_ui.py
```

The full suite must pass before a change is merged. Run the smallest relevant
subset while iterating, then the full suite before you push. Set
`QT_QPA_PLATFORM=offscreen` if you hit display errors in a headless environment.

## Architecture rules

These are enforced by review and are not negotiable:

- **The engine stays framework-neutral.** No PySide6, Qt, or Tkinter imports in
  `core/`, `services/`, `sorting/`, or the root engine modules (`engine.py`,
  `discovery.py`, `transfer_safety.py`, `adb_bridge.py`, `drive_cache.py`,
  `models.py`, `utils.py`). Qt belongs in `ui/` and `controllers/`.
- **All scanning, hashing, ADB, and transfer work runs off the main thread.**
  Workers emit data objects; they never read or mutate widgets. Capture widget
  values before starting work.
- **Duplicates are quarantined, never deleted.**
- **Existing JSON formats stay readable.** Hash caches, transfer journals,
  quarantine manifests, and reports must keep loading. If a format has to
  change, version it and keep the reader tolerant of the old shape.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full picture and
[AGENTS.md](AGENTS.md) for a shorter orientation.

## Changes that touch file operations

Anything that copies, moves, overwrites, deletes, or restores a file needs more
than a passing test run:

- Describe the failure mode you are protecting against, and add a test that
  fails without your change.
- Preserve the transactional shape: never remove or overwrite an existing file
  before its replacement is verified and safely promotable.
- Prefer no-clobber primitives (`open(..., "xb")`, `os.link`) over an
  unconditional `os.replace` when a conflict policy says not to overwrite.
- If you touch resume, quarantine, or undo, say in the PR what happens when the
  process is killed halfway through.

## Performance claims

If a change is justified by speed, measure it and put the numbers in the commit
message — before and after, on a stated workload. Several optimisations in this
codebase were reverted or redirected because measurement contradicted the
intuition behind them; one "obvious" hot path turned out to cost 4% while an
unsuspected directory walk cost 3.3 seconds of startup.

## Commit messages

Explain why, not just what. If a change fixes a defect, describe the failure it
prevents. Note what you verified and what you did not — especially anything that
needs a real Android device, a packaged build, or a Windows GUI session, since
none of those run in CI.

## Manual verification

CI cannot cover device or GUI behaviour. If your change affects either, walk the
relevant sections of
[docs/TARGETED_FIX_MANUAL_CHECKLIST.md](docs/TARGETED_FIX_MANUAL_CHECKLIST.md)
and say what you ran.

## Reporting bugs

Include your Windows version, the app version, whether the source was a local
folder, a removable drive, or an Android device, and what you expected instead.
Sanitized diagnostics from the crash dialog are useful; raw paths are not
necessary and you should not paste them.

For security issues, follow [SECURITY.md](SECURITY.md) instead of opening an
issue.
