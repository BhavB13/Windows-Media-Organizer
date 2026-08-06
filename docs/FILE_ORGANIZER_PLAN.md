# File Organizer and Local ML Expansion Plan

> Superseded by the profile-driven Sort Files rebuild documented in
> [SORT_FILES_ARCHITECTURE.md](SORT_FILES_ARCHITECTURE.md). This file remains as
> historical context for migration and recovery compatibility.

## Summary

Add a new **File Organizer** section to Duplicate & Transfer Manager. Its
initial default workflow will flatten selected subfolders into one main
destination folder, using safe rename-on-conflict behavior and a 90-day
reversible operation record.

The organizer will expand in phases: deterministic flattening first,
rule-based categories next, then local-only ML auto-organization for
high-confidence files. No file content, filename, path, or media metadata
leaves the PC.

## Implementation Status — July 17, 2026

- Complete locally: navigation, staged review, selected-folder flattening,
  type/date rules, JPEG embedded capture-date preference, collision review,
  dry-run/live operations, manifest-backed rollback, 90-day retention,
  presets, Activity records, catalog search, and read-only scheduled previews.
- Complete locally: immutable reviewed-plan execution, source-change checks,
  SHA-256 rollback fingerprints, recoverable Advanced replace backups, and
  local correction rules for labels, extensions, and excluded folders.
- Pending external release decision: a specific license-reviewed, signed ONNX
  CPU model pack and its distribution/update policy. Until then, “Local ML
  organization” is explicitly a deterministic on-device suggestion layer, not
  a trained-model feature.

## Core Organizer Workflow

1. Add an **Organize Files** route to main navigation, overview actions,
   Activity, Help, onboarding, and keyboard shortcuts.
2. Build a staged workflow:
   - Choose a local source folder and main destination folder.
   - Select discovered subfolders to flatten.
   - Choose Flatten, Type Rules, Date Rules, or ML Organization.
   - Review planned moves, collisions, skipped files, and estimated size.
   - Run as dry run or live operation, then show completion and rollback.
3. Default behavior:
   - Flatten files from selected subfolders into the chosen destination.
   - Leave unselected folders unchanged.
   - Rename collisions by default; never overwrite without an Advanced option.
   - Exclude app runtime folders, hidden/system folders, generated output, and
     paths outside the selected source.
4. Require review before every live run. Dry run shows exact source-to-
   destination mappings without moving files.

## Reversible Recovery and Safety

- Add `OrganizerSettings`, `OrganizationPlanItem`, and `OrganizationResult`
  contracts plus a framework-neutral `FileOrganizerService` and Qt controller.
- Persist a manifest for each operation: original/final paths, move reason,
  timestamp, conflict result, and file fingerprint.
- Add an Organization Recovery view with individual/whole-operation rollback,
  rename/skip/replace conflict handling, and changed/missing-file warnings.
- Retain rollback manifests for 90 days by default. Expiry removes only
  app-owned manifests/logs, never organized user files.
- Record previews, runs, rollbacks, cancellations, and failures in Activity and
  the append-only local audit history.

## Organization Modes

1. **Flatten selected folders — initial default**
   - Move selected folders' descendant files to the destination root.
   - Show empty source folders for optional cleanup; cleanup is off by default.
2. **Rule-based type sorting**
   - Optional category folders: Pictures, Videos, Audio, Documents, Archives,
     Projects, Installers, and Other.
   - Use deterministic extension rules; unknown types remain in Other or review.
3. **Date organization**
   - Optional `Pictures/YYYY/MM`, `Videos/YYYY/MM`, and equivalent layouts.
   - Prefer embedded capture time; otherwise use UTC filesystem timestamps.
4. **Duplicate-aware destination handling**
   - Check filename collisions first and optionally hash same-size candidates.
   - Never automatically delete duplicates; skip or route decisions to duplicate
     review.

## Local-Only ML Auto-Organization

- Deliver ML after deterministic organization is stable and fully tested.
- Use a bundled, license-reviewed ONNX Runtime CPU model pack. Processing stays
  local and models are versioned, signed, optional, and independently updated.
- Initial taxonomy: Screenshots, Receipts, People, Pets, Travel, Food, Artwork,
  and Miscellaneous for images. Use deterministic rules for video, audio,
  archives, installers, and unsupported formats.
- Auto-organize only when explicitly enabled and confidence is at least 92% by
  default; expose an Advanced 80–99% threshold.
- Low-confidence, unsupported, conflicting, and duplicate candidates stay in a
  review queue.
- Show thumbnail, predicted label, confidence, proposed destination, and reason.
  Support bulk accept/reject, relabel, always-use-rule, and never-auto-organize-
  this-folder actions.
- Convert user corrections into local deterministic overrides before altering
  future behavior.

## Additional Product Improvements

- Saved organization presets: Downloads Cleanup, Camera Card Import, Desktop
  Cleanup, and Project Archive.
- Watched-folder automation built on the scheduled-task system; dry run by
  default, with explicit live-preset approval.
- Optional local searchable catalog for filename, extension, size, date,
  category, operation, and user tags.
- Preview-only tools for large files, stale downloads, empty folders,
  screenshots, archives, installers, and extensionless files.
- CSV/JSON exports, Help topics, sanitized support bundles, and accessibility
  coverage for folder trees, plan tables, rollback, and scaling.

## Testing and Acceptance Criteria

- Service tests for selected-folder flattening, dry run, conflict policies,
  cancellation, rollback, missing files, permissions, low disk space, Unicode,
  long paths, removable drives, and network shares.
- UI tests for review gating, folder selection, plan filtering, bulk actions,
  rollback, audit entries, confidence controls, and local-only disclosure.
- Integration tests for full temporary-directory organization/rollback,
  scheduled previews/live presets, and deterministic offline ML fixtures.
- Release checks for packaged model assets, model licenses, signature rejection,
  and manual Windows 10/11 large-library and recovery tests.

## Assumptions and Defaults

- Initial default: flatten selected subfolders into one destination folder.
- Live operations move files with 90-day manifest-backed rollback.
- Default conflict policy: rename; overwrite is Advanced-only.
- Automation is off by default.
- ML is local-only, opt-in, and automatically moves only files at 92%+
  confidence.
- iOS, macOS, and cloud classification are outside this feature plan.
