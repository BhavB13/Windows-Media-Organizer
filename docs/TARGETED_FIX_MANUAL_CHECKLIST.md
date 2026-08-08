# Targeted Fix Manual Test Checklist

Use this checklist on Windows after installing the project dependencies.

## Local scan

- Start the app and open **Find Duplicates**.
- Choose a local folder with nested subfolders and filenames containing spaces,
  parentheses, apostrophes, Unicode, and long names.
- Click **Review scan setup** first; confirm **Run scan** is enabled only after
  review is valid.
- Run the scan and confirm duplicate groups are grouped by matching file
  content, not just filename.

## ADB scan

- Connect an authorized Android device.
- Select **Android device** and test nested paths including `/sd/DCIM`,
  `/storage/self/primary/DCIM`, `/storage/emulated/0/DCIM/Camera`, and
  `/sdcard/DCIM/Camera`.
- Confirm inaccessible paths show a safe validation message and do not freeze
  startup.
- Use the Android browse action from duplicate scan and import source fields;
  confirm choosing a child folder updates the path to the selected nested
  Android folder.

## Dry run

- In **Import Files**, enable **Dry run** and run an import. Confirm no files,
  reports, journals, or caches are written as live transfer output.
- In duplicate review, enable **Dry run quarantine** and confirm selected files
  remain in place while the dry-run manifest records what would happen.
- In **Quarantine**, enable **Dry run restore** and confirm restore destinations
  are reported without moving quarantined files.
- In **Import Files**, enable **Dry run partial cleanup** and confirm the listed
  files remain on disk. Cleanup only ever targets files the app can prove it
  owns — its own staging directory or paths recorded in a transfer journal — so
  place an unrelated file named `download.partial` under the save location
  first and confirm it is neither listed nor removed by a live cleanup.
- In **Activity**, enable **Dry run report actions** and confirm export/remove
  report actions preview the target without writing or deleting report files.

## Duplicates

- Scan files with identical content but different names.
- Scan same-size files with different content.
- Confirm only true content matches appear in duplicate groups.

## Transfer

- Import nested local folders to a separate destination.
- Confirm copied paths preserve relative source structure.
- Repeat with existing destination filenames and test rename, skip, and replace
  conflict policies.
- Cancel between files and rerun to verify completed-file journal checkpoints
  are respected.

## Quarantine restore

- Quarantine a local duplicate, then restore it with rename, skip, and replace
  policies.
- Select a missing quarantined file and confirm the UI reports it safely.
- Use **Open quarantined copy** and **Open original folder** from the quarantine
  page.
- Confirm supported images show a preview and unsupported files show metadata.

## Reports

- Complete an import and open **Activity**.
- Use **Open report**, **Open reports folder**, **Export report**, and
  **Remove report**.
- Confirm removing a report requires confirmation and does not remove imported
  files.

## Theme switching

- Switch between light, dark, and system themes.
- Confirm cards, inputs, dropdowns, banners, and selected navigation states
  update consistently without stale colors.

## iOS placeholder

- Open **Import Files** and confirm the text
  “iOS transfer support coming soon.” is visible.
- Confirm no iOS transfer action is offered yet.
## Sort Files

### Profiles and associations

- [ ] Create, edit, duplicate, export, import, disable, and delete a profile.
- [ ] Build associations using ALL and ANY conditions, multiple inclusions,
      exclusions, regex, size/date comparisons, and image/video metadata.
- [ ] Confirm a higher-priority deterministic rule wins and equal-priority
      matches enter Review rather than choosing a rule silently.
- [ ] Exercise Move, Copy, Rename, Ignore, Quarantine, and Recycle Bin actions.
- [ ] Exercise Skip, Rename, Overwrite, Keep Newest, Keep Largest, and Review
      conflict policies against real destination files.

### Preview and approval

- [ ] Confirm Sort Files opens as one Import-style page: source, destination,
      categories, Review setup, Run, progress, and results appear in order
      without mandatory navigation tabs.
- [ ] With defaults selected, confirm pictures go to Pictures, videos to Videos,
      audio to Audio, and office/text/PDF files to Documents.
- [ ] Disable a category and confirm those formats do not appear in Review and
      remain in place. Enable Archives and confirm compressed files go there.
- [ ] Enter custom extensions with and without dots or wildcards, choose their
      category, and confirm normalization, de-duplication, and validation.
- [ ] Change a category, extension, Move/Copy action, profile, source,
      destination, dry-run setting, or a
      rule after a review. Confirm the plan and approvals are cleared and a new
      review is required.
- [ ] Add individual files, a recursive folder, and drag-and-dropped sources;
      confirm each physical file appears only once in the preview.
- [ ] Confirm the preview shows source, rule or ML decision, confidence, action,
      destination, conflict, warning, and explanation.
- [ ] Leave Dry run enabled and execute selected rows. Confirm no source or
      destination file changes and the local preview journal is still created.
- [ ] Confirm a live run processes only explicitly checked Review rows, even
      when a deterministic rule or high-confidence suggestion preselects them.
- [ ] Change a source after preview and confirm execution refuses it until a new
      preview is built.
- [ ] Edit an ML destination and save feedback. Confirm feedback stays local,
      does not retrain silently, and does not move the file.
- [ ] Disable ML and confirm unmatched files remain available for manual Review.
- [ ] Expand Advanced options and confirm profiles, full association rules, ML,
      and monitored folders remain available without cluttering the default flow.

### Processing safety and recovery

- [ ] During a large copy, test pause/resume, per-file skip, cancellation, and
      retry. Confirm the app's own partial files are removed, that unrelated
      files ending in `.partial` are untouched, and completed files remain valid.
- [ ] Test a cross-volume Move and confirm the destination hash is verified
      before the source is removed.
- [ ] Replace an existing destination, then undo the run. Confirm both the
      incoming source and displaced destination are restored.
- [ ] Modify a sorted destination after the run and confirm Undo refuses to
      overwrite the changed file.
- [ ] Confirm self-targets, destinations inside selected source trees, duplicate
      planned targets, invalid paths, and insufficient disk space cannot run.
- [ ] Send a disposable test file to Recycle Bin and restore it using Windows;
      confirm the app clearly marks that action as not app-undoable.

### Monitoring, history, and presentation

- [ ] Add recursive and nonrecursive monitored folders. Confirm filesystem
      polling does not repeat unchanged files.
- [ ] Configure hourly, daily, and weekly Windows scheduled tasks. Confirm dry
      run is the default and live scheduling requires explicit approval.
- [ ] Confirm medium/low-confidence, ambiguous, and conflicting monitored items
      are not processed automatically.
- [ ] From History, open source/destination/report locations, export JSON and
      CSV, retry failures, resume a cancelled run, and undo an eligible run.
- [ ] Switch light/dark/system themes and resize through 960x720, 1366x768, and
      1920x1080. Confirm all Sort sections remain readable without horizontal
      page scrolling or clipped controls.
