# Phone to Drive (Android)

A small native Android app that copies folders from a phone onto a USB drive (e.g. a Samsung
256 GB stick over OTG) **without copying the same content twice**.

This directory is self-contained. It shares no code with the Python desktop app in the repository
root; only the safety ideas are shared (content-based duplicate detection, never delete the source,
verify before counting a success).

## Scope

* Pick a **source** folder on the phone and a **destination** folder on the drive.
* **Review** first: see how many files are new, how many are duplicates, and which files will be
  copied. Nothing is written during a review.
* **Copy new files**: copies everything whose exact content is not already somewhere under the
  destination, preserving the relative subfolder layout.
* A file whose name collides with a *different* file on the drive is saved as `name (1).ext`, so
  nothing is ever replaced.
* The source is opened read-only in spirit and in practice: the app never deletes, renames, moves,
  or writes anything on the phone side.
* No internet permission is declared. Nothing leaves the device.

### Not in scope

Two-way sync, deleting duplicates, moving files, scheduling, cloud anything, media-library
awareness (MediaStore), and any change to the desktop app.

## How duplicates are decided

Identity is the **SHA-256 of the entire file**. Names, sizes, and timestamps are never used to
decide that two files are the same.

Before copying, the app hashes every file already under the destination folder, recursively. During
the copy it hashes each source file as the bytes stream out, and skips the file when that digest is
already present — either on the drive, or earlier in the same batch (so two identical photos in
different phone folders are copied once).

There is **no hash cache**. Every run re-reads and re-hashes both sides. That makes a run slow on a
full drive, and it makes a re-run correct after either side has changed.

## How a file is written

1. A brand-new document is created with a `.dtmpart` name. Existing documents are never opened for
   writing, so an interrupted run cannot damage a file that was already there.
2. The bytes are streamed in and hashed on the way.
3. The written document is **read back** and hashed again. A file only counts as copied when the
   read-back digest matches the source digest.
4. The partial is renamed to its final name, stepping to `name (1)`, `name (2)`, ... if the name is
   taken. Renaming never replaces an existing document.
5. Anything that fails, or a cancel, deletes the partial. The app only ever deletes partials it
   created during the current run — never any pre-existing file, and never anything on the phone.

Leftover `.dtmpart` files from a killed process are ignored by later runs (they are not treated as
real content) and are not deleted, since another process may own them.

## Refusing to guess

The run aborts, copying nothing, when:

* the destination cannot be **fully** enumerated (unplugged drive, revoked grant, provider error) —
  an unreadable folder must never be mistaken for an empty one;
* a file already on the drive cannot be read, so duplicate detection would be unreliable;
* the source cannot be fully enumerated, so files would be silently missed;
* the source and destination are the same folder, or one contains the other.

Per-file problems (an unreadable photo, a file that changes on the phone mid-run, a write refused by
the drive) are reported as failures for that file; the rest of the batch continues.

Only one operation runs at a time. Review and Copy are refused while something is already running.

## Android folder restrictions

Modern Android does not let an app reach storage through file paths. This app therefore uses the
**Storage Access Framework**: `ACTION_OPEN_DOCUMENT_TREE` for both pickers and `DocumentsContract`
for all I/O, with persistable read/write grants so the choices survive a restart. Consequences:

* Some folders cannot be granted at all by system policy — notably `Android/data` and
  `Android/obb`. Files under those cannot be copied by this app on Android 11+.
* The destination must be a folder the system exposes as writable. On most phones this is any
  folder on a mounted USB or SD volume that appears in the picker.
* Whether a document id looks like a path is a provider detail; the app never parses it.

## USB OTG / mounted volume requirement

The drive must be **mounted by Android** and visible in the system file picker before it can be
selected. That normally means:

* a phone that supports USB OTG host mode (most modern Samsung phones do),
* a USB-C drive or an OTG adapter,
* a filesystem Android can mount — **exFAT or FAT32 is the safe choice**; NTFS is usually not
  mountable on stock Android,
* enough battery, since the phone powers the drive.

If the drive does not appear in the picker, the problem is the mount, not the app.

Note that exFAT/FAT32 have their own limits: a FAT32 volume cannot hold a file larger than 4 GB, and
some characters are not allowed in names. Those failures surface as per-file errors from the drive.

## Long-running work

Scans and copies run in a foreground service with an ongoing notification, a progress count, and a
**Cancel** action, so the work survives leaving the app or rotating the screen. The Activity only
observes shared state while it is started, so rotation attaches a new Activity to the same run
rather than restarting or leaking the old one.

## Build and test

Use the helper script; it locates the JDK and SDK, regenerates `local.properties`, keeps the
build to one worker and a small heap, and prints the APK's size and SHA-256 at the end.

```
# from android/
.\build-android.ps1                            # unit tests, lint, debug APK
.\build-android.ps1 -Tasks testDebugUnitTest   # JVM tests only
.\build-android.ps1 -Offline                   # rebuild from a warm Gradle cache
.\build-android.ps1 -Tasks connectedDebugAndroidTest   # instrumented tests; needs a device or emulator
```

`lintDebug` downloads its own analysis tools from `dl.google.com` on first run — tens of
megabytes, and slow enough on some connections that Gradle's stock timeouts give up part-way and
report a resolution failure that reads like a broken build. The script raises those timeouts and
retries. `testDebugUnitTest` and `assembleDebug` need no network once the cache is warm; add
`-Offline` to keep the whole build off the network.

The Gradle wrapper in `gradle/wrapper/` is pinned to 8.11.1. Toolchain expected by
`app/build.gradle`: AGP 8.9.2, Gradle 8.11.1, JDK 17, `compileSdk`/`targetSdk` 35, `minSdk` 26.

### Unit tests

`app/src/test/java/` runs entirely on the JVM — no emulator, no Android classes. The engine tests
drive an in-memory `FakeDocumentStore`; `saf/DocumentIdsTest` exercises the document-id reasoning
directly. Covered:

* identical content under a different name is skipped, including deep in the destination tree;
* identical content within one source batch is copied once;
* same name with different content produces `name (1)`, `name (2)`, and never overwrites;
* relative subfolders are preserved and existing destination folders are reused;
* the source tree is unchanged after a run;
* incomplete destination or source enumeration aborts the run and writes nothing;
* an unreadable existing destination file aborts the run;
* a corrupted write fails verification, is not counted as copied, and leaves nothing behind;
* a source file that changes mid-run is reported, not copied;
* cancel stops early, leaves no partials, and a re-run finishes the remaining files;
* source/destination overlap is rejected in all three forms;
* re-runs copy only what is genuinely new, including after a source file is edited;
* preview reports counts and unreadable files without writing;
* an unreadable source file fails before anything is created on the drive, so it costs no
  document and leaves no partial behind;
* document-id reasoning: the same folder reached through two grants compares equal, the volume
  root contains everything on its volume, a shared name prefix (`DCIM` vs `DCIM2`) is not
  containment, a separate volume is provably unrelated, and an id from a provider whose format
  is not documented returns `UNKNOWN` so the caller fails closed rather than guessing;
* partials are created untyped, and a partial whose name the provider altered is refused before
  any bytes are written.

### Instrumented tests

`app/src/androidTest/java/` runs `SafDocumentStore` against the real system external-storage
provider on a device or emulator. Access comes from adopting the shell's `MANAGE_DOCUMENTS`
permission rather than a tree grant from the picker, so the grant itself is the one thing not
exercised. Each test works in its own `DtmTest-*` folder on primary storage and deletes it.
Covered: listing, child ids, directory reuse, truncating writes, deleted documents, collision
behaviour of create and rename (neither may replace an existing file), partial naming, overlap
through two grants, volume root, shared name prefixes, the trusted-destination check, and a full
engine run and re-run.

These found one defect the JVM tests could not: the provider appends the extension a typed MIME
implies, so a partial created as `image/jpeg` under `IMG.jpg.dtmpart` was stored as
`IMG.jpg.dtmpart.jpg` — no longer recognisable as a partial, and indexed by the gallery. Partials
are now created as `application/octet-stream`, and the final rename restores the real type.

### Manual test steps (on a device)

1. Install the debug APK. Grant the notification permission when asked.
2. Plug in the USB drive and confirm it appears in the Android file picker.
3. Tap **Choose phone folder**, pick e.g. `DCIM/Camera`.
4. Tap **Choose drive folder**, pick a folder on the USB volume.
5. Tap **Review**. Confirm the counts, and confirm nothing was written to the drive.
6. Tap **Copy new files**. Watch the notification progress; rotate the screen and confirm progress
   continues and the UI reattaches.
7. Tap **Review**, then **Copy new files** again. Expect 0 copied and everything skipped.
8. Rename one copied file on the drive, then re-run. Expect it still to be skipped (content match).
9. Edit one file on the phone, re-run. Expect exactly one new file, saved as `name (1).ext`.
10. Start a large copy and tap **Cancel** in the notification. Confirm no `.dtmpart` files remain
    and that a re-run completes the rest.
11. Unplug the drive mid-copy. Expect an error message, not a crash, and no claim of success.
12. Confirm the phone folder is byte-for-byte unchanged throughout.

### What has actually been verified

Verified on 8 September 2026, on Windows with JDK 21 (Android Studio JBR) and SDK build-tools
37.0.0:

* `testDebugUnitTest` — **49 JVM tests pass** (5 `NamesTest`, 28 `TransferEngineTest`,
  16 `DocumentIdsTest`).
* `lintDebug` — **0 errors, 5 warnings**. The warnings are benign: `accessibilityHeading` is
  ignored below API 28 rather than failing, the activity label duplicates the application label,
  and `DataExtractionRules` suggests a newer way to express the backup opt-out that
  `allowBackup="false"` already achieves.
* `assembleDebug` — builds `app/build/outputs/apk/debug/app-debug.apk` (56,717 bytes).
* `apksigner verify` — verifies under APK Signature Scheme v2, one signer, the standard
  `CN=Android Debug` certificate. This is a debug-signed build: it installs for testing and is
  not suitable for distribution. A release build needs its own keystore, which does not exist yet.

Re-verified on 15 September 2026 after the partial-naming fix:

* `testDebugUnitTest` — **51 JVM tests pass** (5 `NamesTest`, 30 `TransferEngineTest`,
  16 `DocumentIdsTest`).
* `connectedDebugAndroidTest` — **17 instrumented tests pass** on the `Pixel_10_Pro` emulator
  (Android API 37, x86_64).
* `lintDebug` — 0 errors, the same 5 warnings. `assembleDebug` and `apksigner verify` pass.

Nothing has run below API 37, so the `isChildDocument` version guard for Android 8.0-9 is still
covered only by reasoning.

**Steps 1-12 above have not been run on hardware.** No phone was attached and no USB drive was
tested. The device behaviour described in this README is the intended
behaviour, not a measured one. Nothing here should be described to anyone as verified against a
real Samsung drive until someone runs that list.

## Known limitations

* Every run re-hashes the whole destination. On a large drive this is slow and battery-hungry.
* Duplicate detection is content-only. Two files that are visually identical but differ by one byte
  (re-encoded, different EXIF) are treated as different files and both are kept.
* A destination subfolder is created before the app knows whether the file in it will turn out to be
  a duplicate, so a subtree that is entirely duplicates can leave empty folders behind.
* `.dtmpart` files left by a force-killed process are ignored but not cleaned up.
* Timestamps and EXIF are not preserved beyond what the storage provider does on its own; the drive
  decides the modification time of the new file.
* Very deep trees (more than 64 levels) are refused rather than walked.
* The collision-rename search gives up after 500 candidates for a single name.
* Empty source folders are not recreated at the destination; only folders containing copied files
  are made.
* The app icon is a stock system drawable; there is no designed launcher icon yet.
* No app-language or right-to-left testing has been done beyond using standard layouts.
