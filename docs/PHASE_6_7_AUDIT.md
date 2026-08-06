# Phase 6-7 Audit

Date: July 8, 2026

## Phase 6 - Reliability, Accessibility, And Security Hardening

Status: source implementation complete; signed Windows release publication remains release-gate work.

- Unit coverage: `tests/test_phase6.py`, existing controller/service tests, and migration/runtime tests cover settings, migration, quarantine, error mapping, operation records, diagnostics privacy, local crash reports, and recoverable item failures.
- UI coverage: PySide6 UI tests cover navigation, form validation, themes, keyboard shortcuts, accessible names, and shell creation using the offscreen Qt runtime.
- Integration coverage: tests use temporary directories and mocked ADB/device responses for dashboard, diagnostics, quarantine, and transfer-report behavior.
- Edge-case coverage: existing engine tests cover inaccessible ADB paths, transfer validation, resume/journal behavior, cache behavior, and copy verification. Phase 6 adds explicit sanitized diagnostics/crash and recoverable quarantine failure coverage.
- File-changing safety: duplicate quarantine and report operations remain app-folder constrained; imports and scans keep review/progress/operation-record paths.
- Privacy: `sanitize_payload` and `sanitize_text` redact paths, hashes, device serials, and sensitive report fields. Diagnostics report Sentry as disabled unless explicit consent exists.
- Crash handling: the PySide6 bootstrap installs a local crash dialog that shows and can copy the sanitized report before any opt-in submission transport exists.
- Accessibility: automated checks remain focused on accessible names, shortcuts, theme switching, responsive navigation, and focus policy. Full screen-reader and Windows contrast review still needs a manual Windows pass.

## Phase 7 - Packaging, Signing, Diagnostics, And Updates

Status: release pipeline and app-side verification implemented; actual signed installer publication requires Windows runner secrets.

- PyInstaller: `packaging/duplicate_transfer_manager.spec` builds a windowed `DuplicateTransferManager` bundle and includes license, README, generated app icon, Android Platform Tools metadata, and update public key data.
- Inno Setup: `packaging/installer.iss` creates a per-user installer with Start menu entry, optional desktop shortcut, signed uninstaller, upgrade-in-place behavior, and user-data preservation.
- Signing workflow: `.github/workflows/release.yml` restores a certificate from GitHub secrets, signs the PyInstaller executable before installer creation, downloads and version-checks Android Platform Tools, signs the installer and generated uninstaller, and verifies Authenticode.
- Update manifest: `packaging/release_manifest.example.json` defines version, channel, installer URL, size, SHA-256 checksum, release notes URL, minimum supported version, Authenticode thumbprint, and signature.
- Manifest signing: `scripts/sign_update_manifest.py` signs canonical manifest JSON using RSA-SHA256 through OpenSSL. The repository includes only the public verification key.
- Updater verification: `UpdateService.verify_manifest` rejects missing fields, channel mismatch, invalid signature, non-newer versions, unsupported minimum versions, bad installer size, and bad SHA-256 checksums. On Windows it also verifies Authenticode status and certificate thumbprint.
- Update flow: `UpdateService.check_manifest_url` enforces the once-per-day check cadence unless forced, `download_installer` refuses to download without user approval and verifies the downloaded installer, and `launch_verified_installer` preserves resumable local state before launching a verified installer.
- Sentry/diagnostics: diagnostics remain opt-in, local-first, and sanitized; no analytics transport is configured by default.

## Verification Commands

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests scripts main.py legacy_main.py ui_components.py engine.py runtime_paths.py
python scripts\smoke_ui.py
```

Linux shell result on July 8, 2026 with temporary PySide6 dependencies extracted under `/tmp`:

```text
Ran 99 tests in 55.965s
OK
```

## Remaining External Release Gates

- Run `scripts/build_release.ps1` on Windows with Inno Setup and Windows SDK signing tools installed.
- Configure GitHub secrets: `WINDOWS_SIGNING_CERT_BASE64`, `WINDOWS_SIGNING_CERT_PASSWORD`, `WINDOWS_SIGNING_CERT_THUMBPRINT`, and `UPDATE_MANIFEST_PRIVATE_KEY_PEM`.
- Run the `signed-windows-release` workflow and verify the draft release artifacts on Windows 10 and Windows 11.
