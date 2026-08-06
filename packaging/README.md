# Packaging

Phase 7 release packaging is represented by:

- `duplicate_transfer_manager.spec` for PyInstaller windowed builds.
- `installer.iss` for a per-user Inno Setup installer with Start menu and
  optional desktop shortcuts.
- `android_platform_tools_manifest.json` for the pinned Android Platform Tools
  release metadata and build-time download URL.
- `release_manifest.example.json` for the signed update manifest contract.
- `update_public_key.json` for application-side RSA-SHA256 manifest
  verification.

The Windows release workflow signs the app executable before it is placed into
the installer, downloads and version-checks Android Platform Tools, signs the
installer and generated uninstaller through Inno Setup, verifies Authenticode,
recomputes the installer size and SHA-256 checksum, signs the update manifest,
and uploads a draft GitHub Release. Signing secrets stay in GitHub Actions
secrets and are not stored in this repository.
