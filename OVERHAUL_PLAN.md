# Duplicate & Transfer Manager — Phased Public v1 Overhaul

## Summary

Rebuild the frontend in PySide6 while preserving the tested Python transfer engine. The product will target both everyday and advanced users on 64-bit Windows 10 and 11, use a neutral Fluent-inspired light/dark design, bundle ADB, quarantine duplicates instead of deleting them, and ship as a signed Inno Setup installer.

The application remains local-first. Sentry crash diagnostics are disabled by default and require explicit consent. Updates are delivered through cryptographically verified, Authenticode-signed GitHub Releases.

## Phase 0 — Stabilize the Existing Project

**Status: Complete — July 6, 2026.** Verified with 35 automated tests,
including the original 29 engine tests and six Phase 0 path, migration, and
metadata tests.

- Rename all remaining user-facing references to “Duplicate & Transfer Manager.”
- Preserve the current dirty worktree before restructuring; do not discard existing engine changes.
- Add dependency management, `.gitignore`, licensing, version metadata, and developer setup documentation.
- Remove generated caches, reports, journals, bytecode, and local editor files from Git tracking while leaving users’ local copies intact.
- Establish `src/`, `tests/`, `assets/`, `packaging/`, and `scripts/` structure.
- Move runtime data to `%LOCALAPPDATA%\DuplicateTransferManager\`:
  - `cache`
  - `reports`
  - `journals`
  - `quarantine`
  - `logs`
  - `updates`
- Add a one-time, non-destructive migration that can import compatible legacy caches and journals from the old working directory.
- Pin supported Python and library versions.
- Gate completion on all 29 current tests continuing to pass.

## Phase 1 — Separate the Engine from the UI

**Status: Complete — July 6, 2026.** Verified with 54 automated tests using
the pinned PySide6 6.9.1 QtCore runtime, including worker-thread execution,
main-thread signal delivery, cancellation, structured errors, service
orchestration, settings, reports, quarantine records, and the original engine
compatibility suite.

- Keep discovery, hashing, duplicate grouping, ADB access, transfer safety, and copy logic independent of PySide6.
- Replace Tkinter-bound logging and callbacks with framework-neutral event interfaces.
- Introduce controllers for duplicate scans, transfers, device discovery, reports, quarantine, settings, diagnostics, and updates.
- Run all scanning, hashing, ADB, and transfer work in Qt worker threads; prohibit widgets and UI state from being accessed outside the main thread.
- Add cancellation tokens and explicit operation states:
  - Idle
  - Validating
  - Scanning
  - Comparing
  - Transferring
  - Paused or reconnecting
  - Cancelling
  - Completed
  - Failed
- Convert backend failures into structured, user-readable errors while retaining technical details in logs.
- Preserve compatibility with the existing JSON caches and transfer journals during v1.

### Core interfaces

- `OperationEvent`: phase, progress, bytes processed, total bytes, item counts, current item, rate, ETA, severity, and user-facing message.
- `OperationResult`: status, counts, duration, warnings, failures, report path, and resume information.
- `AppSettings`: appearance, simple/advanced mode, transfer defaults, diagnostic consent, update channel, and Android preferences.
- `QuarantineRecord`: original path, stored path, hash, size, reason, operation ID, and timestamp.
- Controllers expose Qt signals for progress, state changes, recoverable errors, completion, and cancellation.

## Phase 2 — Design System and Application Shell

**Status: Complete — July 6, 2026.** Verified with 64 automated tests, a
warning-free version 0.3.0 wheel, an installed-wheel shell smoke test, and
visual review at 960×720, 1360×860, and 1920×1080 in light and dark themes,
including a true 200% Qt scale-factor render.

- Create a reusable PySide6 design system rather than styling individual screens independently.
- Use Segoe UI, restrained blue accent colors, clear spacing, rounded cards, consistent iconography, and accessible semantic colors.
- Support system, light, and dark themes.
- Build responsive layouts for Windows display scaling from 100% through 200%.
- Create the main shell:
  - Overview
  - Find Duplicates
  - Import Files
  - Activity
  - Quarantine
  - Settings
  - Help
- Replace the permanent ADB-heavy header with a compact contextual device indicator.
- Add reusable controls for path selection, source cards, empty states, inline validation, progress, notifications, confirmation, and completion summaries.
- Add keyboard navigation, visible focus states, screen-reader labels, and reduced-motion behavior.
- Produce connected static screens before wiring destructive or long-running actions.
- Gate completion on visual review at common laptop and desktop resolutions in both themes.

## Phase 3 — Duplicate Review Workflow

**Status: Complete — July 6, 2026.** Implemented as a staged PySide6 workflow
backed by the existing duplicate scan controller plus a manifest-based
quarantine and restore service. Verified with Phase 3 service tests covering
default keep selection, recoverable-size estimates, local quarantine,
manifest writing, individual restore, operation restore conflict policies, and
Android quarantine copy behavior.

- Replace the current single dense duplicate screen with a staged workflow:
  1. Select a local or Android location.
  2. Select file categories and exclusions.
  3. Review a plain-language scan summary.
  4. Run the scan.
  5. Review duplicate groups.
  6. Choose copies to keep.
  7. Confirm quarantine.
- Keep hash algorithm, hash mode, thread count, and exclusions inside Advanced settings.
- Remove the unimplemented AI controls from public v1.
- Show duplicate groups using thumbnails where available, plus filename, path, dimensions when available, size, date, and device.
- Default selection to keeping the oldest file, with a visible option to prefer newest.
- Never move files during scanning or initial review.
- Show estimated recoverable space before confirmation.
- Move selected duplicates into app-managed quarantine with a manifest; never permanently delete from within v1.
- Support restoring individual files or an entire operation, resolving path conflicts with rename/skip/replace choices.
- Treat unavailable Android previews and metadata as supported empty states rather than errors.

### Acceptance scenarios

- Scan local folders, full drives, and authorized Android paths.
- Cancel safely during discovery and hashing.
- Review thousands of results without freezing the UI.
- Quarantine selected local duplicates and restore them.
- Pull selected Android duplicates into local quarantine without deleting the phone originals.
- Recover cleanly from inaccessible files, lost authorization, and partial metadata.

## Phase 4 — Guided Import and Transfer Workflow

**Status: Complete — July 6, 2026.** Implemented as a guided PySide6 import
flow backed by `TransferController`, `TransferService`, and reusable import
workflow helpers. Verified with Phase 4 tests covering copy-only
structure-preserving settings, Reliable/Balanced/Fast presets, category
mapping, same-library save explanation, distinct transfer stages, summary-card
result data, and partial-file cleanup.

- Replace “Smart Sync Transfer” with a guided import flow:
  1. Choose phone, folder, or drive as the source.
  2. Choose the existing library to compare against.
  3. Choose where new files should be saved.
  4. Choose file categories.
  5. Review the operation.
  6. Run and monitor.
  7. Review the result.
- Rename technical fields:
  - “Compare folder” → “Existing library”
  - “Output folder” → “Save new files to”
- Explain when the existing library and save location are the same.
- Keep the default transfer copy-only and structure-preserving.
- Put hash mode, worker count, retries, conflict policy, cache controls, and keep-awake options in Advanced mode.
- Provide Reliable, Balanced, and Fast presets with plain-language descriptions.
- Show distinct stages for discovery, comparison, copying, verification, reconnecting, and report generation.
- Support cancellation, ADB reconnection, journal-based resume, partial-file cleanup, and verified completion.
- Present completion as summary cards rather than raw logs:
  - New files copied
  - Duplicates skipped
  - Files resumed
  - Errors
  - Data transferred
  - Report location
- Keep detailed activity logs available through an expandable panel.

## Phase 5 — Overview, Activity, Settings, and Recovery

**Status: Complete — July 6, 2026.** Implemented as a local-first recovery and
operations layer on top of the Phase 2 shell and Phase 3/4 workflows. Verified
with Phase 5 service tests covering operation records, transfer-report activity,
report export/removal, dashboard summaries with connected devices, quarantine
storage, settings persistence, diagnostics Platform Tools metadata, and
quarantine grouping/filtering.

- Build an overview dashboard with primary actions, connected devices, recent operations, interrupted transfers, and storage summaries.
- Add an Activity page backed by local operation records and existing transfer reports.
- Allow reports to be opened, exported, or removed locally.
- Add a Quarantine page with restore, search, filtering, size summaries, and operation grouping.
- Add settings for:
  - Theme
  - Simple or Advanced mode
  - Default file categories
  - Default transfer profile
  - Cache management
  - Android behavior
  - Diagnostic consent
  - Update channel
- Default to Simple mode; Advanced mode reveals technical controls without changing engine behavior.
- Add first-run onboarding for local scans, Android authorization, privacy, diagnostics consent, and update behavior.
- Bundle a pinned, license-compatible Android Platform Tools release and display its version in diagnostics.
- Never alter system-wide ADB installations or environment variables.

## Phase 6 — Reliability, Accessibility, and Security Hardening

- Add unit tests for controllers, settings, migration, quarantine, updater verification, and error mapping.
- Add PySide6 UI tests for navigation, form validation, state transitions, simple/advanced mode, themes, and cancellation.
- Add integration tests using temporary directories and mocked ADB devices.
- Test large libraries, Unicode paths, long Windows paths, removable drives, low disk space, permission failures, hash failures, device disconnects, and interrupted transfers.
- Ensure every file-changing action has preflight validation, a clear review step, an operation record, and recoverable failure behavior.
- Sanitize logs and diagnostic events:
  - Do not send filenames, paths, device serials, hashes, or media metadata to Sentry.
  - Replace sensitive values with local-only correlation identifiers.
- Keep Sentry disabled until the user explicitly opts in.
- Add a local crash dialog that can show or copy the sanitized report before submission.
- Run accessibility checks for keyboard-only use, contrast, scaling, focus order, and screen-reader labels.
- Gate release on clean tests, successful upgrade testing, and manual Windows 10/11 smoke tests.

## Phase 7 — Packaging, Signing, Diagnostics, and Updates

- Build the application with PyInstaller in windowed mode.
- Include Qt assets, application icons, bundled ADB tools, licenses, version metadata, and required runtime libraries.
- Create a per-user Inno Setup installer with:
  - Start menu entry
  - Optional desktop shortcut
  - Uninstaller
  - Upgrade-in-place support
  - Preservation of user data
- Sign executables, bundled helpers, installer, and uninstaller with an Authenticode code-signing certificate.
- Publish releases through GitHub Actions using protected signing secrets.
- Generate a signed update manifest containing version, channel, installer URL, size, SHA-256 checksum, release notes URL, minimum supported version, and signature.
- The updater will:
  - Check GitHub Releases on startup no more than once per day and on manual request.
  - Download only after user approval.
  - Verify manifest signature, checksum, publisher identity, and Authenticode signature.
  - Refuse invalid or downgraded packages.
  - Launch the verified Inno installer, close the app safely, and preserve resumable state.
- Configure Sentry with separate development and production environments, release tagging, strict redaction, and opt-in initialization.
- Do not collect usage analytics beyond explicitly approved crash diagnostics.

## Phase 8 — Public v1 Release

- Replace the repository README with product screenshots, features, safety guarantees, installation, Android setup, privacy, troubleshooting, and development instructions.
- Add a user guide covering duplicate review, quarantine restoration, imports, interrupted transfers, reports, and advanced settings.
- Add privacy policy, diagnostic disclosure, third-party licenses, changelog, contribution guide, and security reporting instructions.
- Publish signed installer, checksums, signed update manifest, release notes, and source tag.
- Validate installation, update, rollback recovery, uninstall, and user-data preservation on clean Windows 10 and Windows 11 machines.
- Mark v1 complete only when a non-technical user can install the program, authorize a phone, safely review duplicates, import new media, recover quarantined files, and understand the final results without referring to developer logs.

## Assumptions and Defaults

- Product name: **Duplicate & Transfer Manager**.
- Frontend: PySide6 with a custom Fluent-inspired design system; no GPL-only UI dependency.
- Audience: Simple mode for everyday users and Advanced mode for power users.
- Platform: 64-bit Windows 10 and 11.
- Privacy: Local processing, with Sentry crash diagnostics strictly opt-in.
- Duplicate handling: Recoverable quarantine only; no permanent in-app deletion.
- Android: Bundled ADB tools with no system-wide changes.
- Distribution: Authenticode-signed Inno Setup installer.
- Updates: Signed automatic updater backed by GitHub Releases.
- Existing transfer engine and cache formats remain compatible through v1 unless tests demonstrate that a migration is required.
