# Desktop review and Android companion — September 2026

The request was a comprehensive verification of the desktop app plus an Android app that copies
phone files onto a Samsung 256 GB USB drive without duplicating what is already there.

The work was split across several agent sessions. This file records what was actually verified,
and — more importantly — what was **not**, so nobody later mistakes an intention for a result.

## What was verified, and how

All figures below were produced on this machine on 8 September 2026.

### Desktop (Python)

| Check | Result |
| --- | --- |
| `.venv/Scripts/python.exe -m unittest discover -s tests` | **343 passed, 1 skipped**, 132 s |
| `python -m compileall engine.py transfer_safety.py src/` | clean |

The one skip is a Windows directory-symlink permission case; it needs an elevated shell and is
unrelated to any change here. The baseline before this work was 311 tests, so the suite grew by
32 tests covering the fixes below.

### Android

| Check | Result |
| --- | --- |
| `testDebugUnitTest` | **49 passed** (5 Names, 28 TransferEngine, 16 DocumentIds) |
| `lintDebug` | **0 errors, 5 warnings** |
| `assembleDebug` | `app/build/outputs/apk/debug/app-debug.apk`, 56,717 bytes |
| `apksigner verify --verbose --print-certs` | verifies, v2 scheme, 1 signer, `CN=Android Debug` |

Toolchain: JDK 21 (Android Studio JBR), Android SDK build-tools 37.0.0, platform 35,
Gradle 8.11.1, AGP 8.9.2. `android/build-android.ps1` reproduces all of it and now runs
`apksigner` itself, so a package Gradle produced but Android would refuse to install fails the
build rather than passing quietly.

Lint's first two runs failed to resolve its own analysis tools from `dl.google.com`. That was a
network result, not a code result: the host resolved intermittently and transferred at roughly
165 KB/s, so Gradle's stock timeouts gave up part-way and reported a resolution failure that read
like a broken build. Raising the HTTP timeouts and retry count — now the default in
`build-android.ps1` — got a complete run, which then found a real crash defect (see below). It
was worth the wait. The five remaining warnings are benign: `accessibilityHeading` is ignored
below API 28 rather than failing, the activity label duplicates the application label, and
`DataExtractionRules` suggests a newer way to express the backup opt-out that
`allowBackup="false"` already achieves.

## Not verified — do not describe these as done

1. **Nothing has run on hardware.** No phone was attached (`adb devices` was empty) and no USB
   drive was tested. The 12-step manual checklist in `android/README.md` is unexecuted. Until
   someone works through it, the app's behaviour on a real Samsung drive is a design intention,
   not a measured fact.
2. **No emulator run.** The `Pixel_10_Pro` AVD exists but was never launched. There are no
   instrumented (`androidTest`) tests at all — every Android test is a JVM test against an
   in-memory `FakeDocumentStore`. `SafDocumentStore`'s real interaction with `DocumentsContract`
   is therefore untested; only its id reasoning (`DocumentIds`) is.
3. **The APK is debug-signed.** It installs for testing. It is not distributable, and no release
   keystore exists.

## What changed

### Android companion (`android/`, new)

A self-contained native app sharing no code with the Python desktop app. Identity is the SHA-256
of a file's whole content; names, sizes and timestamps never decide a duplicate. The source is
never written to.

The ordering matters and is the thing most likely to be broken by a careless edit: the source is
hashed **before** anything is created on the drive. A duplicate therefore costs no document, which
is what lets a re-run against a full drive still work. Bytes then go to a `.dtmpart` document,
are read back and re-hashed, and only a match renames it into place. An existing file is never
opened for writing, and only partials created by the current run are ever deleted.

Corrections applied to the first draft, all of which were real defects:

* the whole source was written to the drive before the duplicate decision;
* `isSameOrDescendant` compared tree-scoped URI strings, so the same folder under two grants
  looked like two folders, and the volume root `primary:` was not recognised as the ancestor of
  `primary:DCIM` (no slash follows the colon);
* rename relied on a no-clobber guarantee `DocumentsContract` does not make universally;
* cancellation during traversal returned a partial list, which could be reported as a successful
  empty run;
* `hashDestination` treated any `.dtmpart` suffix as its own, including a user's file;
* the service lacked Android 15 `onTimeout` handling, cancellation on destroy, and a wake lock;
* `MainActivity` recorded a review as approved before knowing it had succeeded, so a failed or
  cancelled review could authorise a copy;
* `DocumentsContract.isChildDocument` was called unguarded although it arrived in API 29 while
  `minSdk` is 26. On Android 8.0 through 9 the method does not exist and the call throws
  `NoSuchMethodError` — an `Error`, so the surrounding `catch (Exception)` would not have caught
  it either. The app would have crashed inside the one check that stops a folder being copied
  into itself. It is now version-guarded, and below API 29 the provider is treated as having no
  opinion: a provably unrelated pair still passes, anything unprovable fails closed with an
  explanation the user can act on. This is the defect lint found.

`DocumentIds` is deliberately a pure class with no Android imports so the id reasoning — the last
thing standing between the user and a run that copies a tree into itself — can be tested on a
plain JVM. `saf/DocumentIdsTest` covers the volume root, trailing-slash grants, shared name
prefixes (`DCIM` vs `DCIM2`), separate volumes, and the fail-closed `UNKNOWN` answer for providers
whose id format is not documented.

### Desktop fixes

* **Replace-policy import.** A failed verification used to leave the user's library file missing
  from its own path with no record of where it went, because only the `HashCancelled` branch
  rolled back. `restore_replaced_original` now runs on every failure path. It never uses
  `os.replace` — checking that a name is free and then replacing it are two steps, and anything
  landing in between is destroyed by the second — so it links or exclusively creates, and where
  the name has been taken again it leaves the original in the backups folder and says loudly
  where it is.
* **Local copies are verified by content**, not by size. Size remains a cheap pre-check.
* **Dry runs** no longer create destination folders, no longer ignore the resume journal, and no
  longer double-count identical sources.
* **A separate save folder** is now scanned for duplicates, so a resume journal being unavailable
  no longer causes repeat copies.
* **Sort Files** stages through uniquely named temporary files it owns, publishes without ever
  clobbering, and gives each overwrite attempt its own backup name so a retry cannot copy over an
  original already retained.
* **Cancelling a retrying pull** returns at once instead of burning the remaining attempts and a
  reconnect wait of up to 300 s.
* **Chained controller operations** keep their own cancellation token, so a queued `finished`
  signal from the previous worker cannot clear the current one's.
* **Partial-file cleanup** runs on the transfer worker instead of the UI thread, and is guarded
  against overlapping an import.
* One stale test (`reportsUnreadableSourceFileAndStillCopiesTheRest`) asserted the pre-fix failure
  message and was updated; it now also documents that an unreadable source costs no document.

## 15 September — instrumented tests

Priority 2 below is done for the SAF layer. `app/src/androidTest/.../SafDocumentStoreInstrumentedTest`
(17 tests) runs `SafDocumentStore` and a full engine run against the real external-storage
provider on the `Pixel_10_Pro` emulator (API 37); see `android/README.md` for scope and the one
gap (the picker grant is replaced by the shell's `MANAGE_DOCUMENTS`).

They found a real defect: the provider appends the extension a typed MIME implies, so partials
were stored as `IMG.jpg.dtmpart.jpg`. A crash between verification and rename would then have left
a complete copy under that name, counted as the user's own content on the next run — suppressing
the copy — and visible in the gallery. `TransferEngine` now creates partials as
`application/octet-stream` and refuses to write into a partial whose name lost its suffix.
`FakeDocumentStore` now models the provider's extension behaviour, with two JVM tests.

Verified that day: 51 JVM tests, 17 instrumented tests, lint 0 errors / 5 warnings, APK builds and
verifies; desktop suite 343 passed, 1 skipped. Hardware (priority 1) is still unexecuted, and
nothing has run below API 37.

## If you pick this up next

In priority order:

1. Run the 12-step hardware checklist in `android/README.md` with a real phone and the Samsung
   drive. Everything else is secondary to this.
2. Add instrumented tests for `SafDocumentStore` against a real `DocumentsContract`, or at least
   an emulator smoke run — the SAF layer is the least-tested part of the Android app, and the
   `isChildDocument` defect above is exactly the class of bug a JVM test cannot see.
3. Decide whether a release build is wanted. If so it needs its own keystore and a launcher icon;
   the app currently uses a stock system drawable.
