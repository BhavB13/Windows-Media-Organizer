# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

**No public release has been made yet.** Everything below is development history
against version 0.8.0. Version numbers become meaningful from the first tagged
release onward.

## [Unreleased]

### Fixed — data safety

These came out of a two-reviewer audit of the pre-release code. Each is a case
where a user could have lost a file.

- Fast hashing sampled the first and last megabyte of large files and treated
  the result as definitive, so two same-size files with matching ends and
  different middles were reported as duplicates. A file with no surviving copy
  could be quarantined. Sampled digests are now only a candidate filter; every
  candidate group is re-confirmed with a full-content SHA-256 before review.
- Replace-restore deleted the file already at the destination before moving the
  replacement into place, so a failed move left the original gone. Both
  duplicate restore and Sort Files undo now keep the incumbent until a verified
  promotion succeeds.
- "Clean partial files" recursively deleted every file ending in `.partial`
  under the chosen library, including files the app never created. Cleanup now
  removes only files the app can prove it owns.
- Quarantine wrote its manifest only after every file had been moved, without
  atomicity, so an interrupted operation could leave files displaced with no
  recovery record. The manifest is now written before the first move,
  checkpointed per item, and written atomically.
- Transfer promotion used an unconditional `os.replace`, overwriting whatever
  occupied the destination at commit time even under the default rename policy.
  Promotion now honours the reviewed conflict policy with a no-clobber write.
- Cancelling a hash mid-file still computed and cached a digest covering only
  the bytes read so far, poisoning later runs. Cancellation now yields no digest
  and writes nothing to the cache.
- Resume trusted file size alone, so a target corrupted to different content of
  the same length was skipped as complete.
- Migrated organizer presets lost their category folders, flattening everything
  into the destination root.

### Fixed — update chain

- The update public key was loaded from the current working directory when the
  bundled and source copies were both missing, letting a chosen working
  directory substitute the trust root. A missing trust root is now a hard error.
- Installer downloads were unbounded, written straight to the final path, and
  verified only afterwards. They are now streamed with a manifest-bounded size
  cap, staged, verified, then promoted.
- PKCS#1 v1.5 padding bytes were not validated during signature verification.
- The publisher thumbprint was optional, so a manifest omitting it silently
  disabled publisher pinning.
- A skipped update check still reset the 24-hour timer, so an app opened daily
  could never actually check.
- Version comparison dropped non-numeric components, making `0.9.0rc1` compare
  equal to `0.9`.

### Added

- An off-by-default advanced import option, "Re-read every resumed file to
  verify it", for certainty against silent corruption that timestamps cannot
  detect.
- Android connection guidance naming the causes adb's own status does not: a
  power-only cable, a USB mode set to charging, and a locked phone reporting
  offline.

### Changed — performance

Measured before and after, on stated workloads.

- Resume no longer re-reads every completed file. It trusts a target whose size
  and recorded timestamp both still match, and falls through to full content
  verification when either differs. Resuming a 200 GB import that had completed
  180 GB previously re-read all 180 GB before transferring anything.
- ADB pulls no longer wait half a second per file. The completion check runs
  every 50 ms while progress reporting stays on its original cadence: 0.508 s to
  0.208 s per file measured against the real code path.
- Device hashing is batched into grouped `sha256sum` commands instead of one
  `adb exec-out` per file. Each spawn costs about 35 ms before the phone reads
  anything, so 10,000 files previously spent roughly 350 seconds on process
  startup alone.
- Startup no longer blocks on a walk of the app cache. `_directory_size` uses
  `os.scandir` instead of `Path.rglob`, which made three stat calls per entry,
  and the dashboard paints before measuring storage. Overview page construction
  went from 3,349 ms to 44 ms; the test suite dropped from 304 s to 106 s.

### Changed — other

- Fast hash mode is relabelled and documented. Now that it confirms candidates
  with a full read, it is slower than Full content for photos and videos, where
  same-size files are usually genuine copies.
- The resume journal is stored compactly rather than indented.
- `minimum_supported_version` is a release workflow input rather than hardcoded.
- The iOS "coming soon" notice is sourced from `IOSTransferService` rather than
  a hardcoded string.

### Removed

- The unreachable legacy `OrganizerPage` and its tests, superseded by the Sort
  Files workspace.
- A stray empty `unused.json` from the repository root.

## [0.8.0] — development milestone, July 8 2026

Phases 6 and 7 of the overhaul plan.

- Sanitized diagnostics and local crash reports with correlation identifiers;
  Sentry remains opt-in.
- Signed update manifests with channel, downgrade, checksum, and Authenticode
  verification.
- PyInstaller build, Inno Setup installer, app icon, and a signed-release GitHub
  Actions workflow.
- The profile-driven Sort Files workspace: versioned profiles, deterministic
  rules, optional local suggestions, journaled execution with verification and
  undo, monitoring, and scheduling.

## [0.5.0] — development milestone, July 6 2026

Phases 0 through 5.

- Repository restructured to `src/`, `tests/`, `assets/`, `packaging/`,
  `scripts/`, with runtime data moved to `%LOCALAPPDATA%`.
- The transfer engine separated from the UI behind framework-neutral contracts
  and Qt controllers.
- The PySide6 design system and application shell, with system, light, and dark
  themes.
- Duplicate review with manifest-backed quarantine and restore.
- The guided copy-only import workflow.
- Overview, Activity, Settings, Quarantine, and recovery surfaces.

## Earlier

The project began as a Tkinter application named "Media Organizer Pro". That
frontend is retained temporarily as `legacy_main.py` for compatibility and is
not the supported interface.
