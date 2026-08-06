# Scripts

Development, maintenance, migration, and release helper scripts belong here.
The legacy scheduled ADB cache script remains at the repository root until its
Phase 1 command paths are packaged.

- `smoke_ui.py` creates the PySide6 shell in an isolated data directory and
  closes it without first-run onboarding. Use it to validate GUI startup in CI,
  release checks, and local Windows setup.
