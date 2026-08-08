# User Guide

Duplicate & Transfer Manager finds duplicate files, imports photos and videos
from a phone or drive, and sorts files into folders. Everything happens on your
PC — nothing is uploaded.

Two ideas run through the whole app:

- **Nothing is deleted.** Duplicates go into quarantine and can be brought back.
- **Nothing runs without review.** Every operation shows you what it will do
  before it does it.

## First run

On first launch you'll be asked about local scanning, Android authorization,
privacy, diagnostics, and updates. Diagnostics are off unless you turn them on.

Choose **Simple** or **Advanced** mode in Settings. Simple is the default and
hides technical controls. Advanced reveals them without changing what the app
does by default.

## Importing from an Android phone

This is the main workflow for most people.

### Preparing the phone

1. Enable **Developer options**: Settings → About phone → tap Build number seven
   times.
2. In Developer options, turn on **USB debugging**.
3. Connect the phone and set the USB mode to **File transfer**, not charging.
4. Unlock the phone and approve the **Allow USB debugging** prompt.

If the phone doesn't appear, the app will tell you what to check. The three
things that catch people out:

- **The cable.** Many charging cables have no data wires. Try a different one.
- **USB mode.** Some phones default to charging only on every reconnect.
- **A locked screen.** Unlock the phone, then reconnect.

### Running the import

1. Open **Import Files** and choose **Android phone**, then pick your device.
2. Set the source folder — `/sdcard/DCIM/Camera` is the usual place for photos.
3. Choose an **existing library** to compare against. Files already there are
   skipped, so you can import repeatedly without creating duplicates.
4. Choose where new files should be saved.
5. Pick the file types you want.
6. Choose a profile:
   - **Reliable** — hashes on the phone before copying. Slowest, most thorough.
   - **Balanced** — the default, and the right choice for most imports.
   - **Fast** — more parallelism; best on a good cable and a fast PC.
7. Click **Review import**, check the summary, then **Run import**.

Imports are copy-only. Nothing on your phone is modified or deleted.

### If an import is interrupted

Unplugging the phone, a crash, or closing the app mid-import is recoverable. The
app writes a journal as it goes. Return to **Activity**, find the interrupted
operation, and resume it. Files already copied are skipped.

Resume checks each completed file's size and timestamp rather than re-reading
it, so resuming a large library starts quickly. If a file has changed since it
was copied, it's verified by content and re-copied if it no longer matches.

If you specifically want every already-copied file re-read and verified — after
a suspected drive fault, say — enable **Re-read every resumed file to verify
it** under Advanced options. On a large library this takes a long time, which is
why it's off by default.

## Finding duplicates

1. Open **Find Duplicates** and choose a folder, drive, or Android device.
2. Click **Review scan setup**, then **Run scan**.
3. Work through the groups. Each shows the copies found and pre-selects one to
   keep — you can change it, or use the oldest / newest / highest-resolution
   preferences.
4. The estimated space you'd recover is shown as you select.
5. Confirm to move the unselected copies into quarantine.

Duplicates are matched by content, not by name. Two files with different names
and identical contents are duplicates; two files with the same name and
different contents are not.

Scanning and reviewing never change anything. Only the final confirmation moves
files, and it moves them to quarantine rather than deleting them.

Android duplicates are **copied** into quarantine — the originals stay on the
phone, so review them on the phone before removing anything there.

### Hash modes

Leave this alone unless you have a reason. **Full content** reads every file
completely and is recommended.

**Fast** samples the start and end of large files to shortlist candidates, then
still reads every shortlisted file in full before anything is quarantined. For
photos and videos that means it usually reads the same files twice, because
media files that share an exact size are nearly always genuine copies. It helps
only in libraries with many same-size files that differ in content.

## Getting files back from quarantine

Open **Quarantine**. You can search, filter by status, and group by operation.

Restore individual files or a whole operation. If something already exists at
the original location, choose to rename, skip, or replace. Replacing keeps the
existing file safe until the restore has succeeded.

Quarantined files stay until you remove them. They are not cleaned up on a
timer.

## Sorting files

**Sort Files** moves files into folders by rules you choose.

For simple jobs, pick a category — pictures, videos, audio, documents, archives,
or specific extensions — choose a destination, and review the plan.

Advanced profiles support named rules with priorities, include and exclude
conditions, destination templates using values like `{year}`, `{month}`, and
`{media_type}`, and per-rule conflict policies.

Whatever the complexity, the same safety rules apply:

- Only rows you tick in Review are processed.
- A dry run shows exact source-to-destination mappings without touching a file.
- Every live run writes a journal and can be undone, except Recycle Bin actions,
  which the Windows Recycle Bin owns.
- Ambiguous matches and conflicts go to Review rather than being resolved
  silently.

### Automation

You can monitor folders on a schedule. Automation defaults to a dry run. Live
automation requires explicit approval and never processes anything sitting in
Review.

## Activity and reports

**Activity** lists what the app has done: scans, imports, quarantine actions,
and failures. Import reports can be opened, exported, or removed.

Removing an activity record does not erase history — a separate append-only
audit log remains.

## Settings worth knowing

- **Theme** — system, light, or dark.
- **Simple / Advanced mode** — how many controls are shown.
- **Cache retention** — how long hash caches are kept. Caches make repeat scans
  much faster; clearing them costs you that speed, not any data.
- **Diagnostics** — off by default. See [PRIVACY.md](../PRIVACY.md).
- **Update channel** — which releases you're offered.
- **Scheduled duplicate scan** — a daily or weekly **read-only** scan. It records
  findings only. It never quarantines, moves, or deletes anything.

## Where your files live

```text
%LOCALAPPDATA%\DuplicateTransferManager
```

Caches, reports, journals, logs, quarantined files, sorting history, and
downloaded updates. Uninstalling does not remove this folder, so quarantined
files survive an upgrade.

## Troubleshooting

**The phone isn't detected.** Cable, USB mode, USB debugging, locked screen — in
that order. The app shows specific guidance for whichever state it sees.

**An import stopped partway.** Go to Activity and resume it. Nothing is lost.

**A scan is slow the first time.** Hashing reads every candidate file. The
results are cached, so the second scan of the same library is much faster.

**Duplicates aren't being found.** Files must be byte-identical. A photo edited,
re-saved, or re-compressed is a different file even if it looks the same.

**I restored a file to the wrong place.** Restores are recorded in Activity, and
the quarantine manifest keeps the original path.

## Getting help

Open an issue on the repository. For anything security related, follow
[SECURITY.md](../SECURITY.md) instead.
