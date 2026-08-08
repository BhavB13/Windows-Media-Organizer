# Privacy

Duplicate & Transfer Manager is local-first. It processes your files on your PC
and does not upload them anywhere.

## What leaves your PC

Under default settings, nothing.

The application makes network requests in exactly two situations, both of which
you control:

1. **Update checks.** If "Check for updates automatically" is enabled, the app
   fetches a signed update manifest no more than once per day, and downloads an
   installer only after you approve it. The request carries no identifier for
   you or your library.
2. **Crash diagnostics.** Disabled by default. Nothing is transmitted unless you
   explicitly turn on "Share sanitized crash diagnostics" in Settings.

There is no usage analytics, telemetry, account system, or advertising
identifier. The app does not phone home on launch.

## What never leaves your PC

Your files are never uploaded. Neither is the information that describes them:

- File names, folder names, and full paths
- File contents and thumbnails
- Content hashes
- Media metadata such as capture dates, camera model, and dimensions
- Android device serial numbers

## How crash reports are sanitized

If you opt in to crash diagnostics, reports are sanitized **before** they can be
shown, copied, or sent. Sanitization is implemented in
`duplicate_transfer_manager.core.security` and replaces sensitive values with
local-only correlation identifiers — short hashes that let two mentions of the
same path be recognised as the same thing without revealing what it was.

Redacted values include paths, file names, content hashes, and device serials.
The result looks like `<redacted:path:p-8f2c1a4b90de>` rather than a filename.

Correlation identifiers are derived from the value itself, so the same path
always produces the same identifier on your machine. They are not reversible
into the original path.

You can review a crash report in full before deciding whether to send it. The
local crash dialog shows the sanitized report and offers a copy action.

### Known limitation

Sanitization currently recognises rooted paths — `C:\Users\...`, UNC paths, and
paths beginning with a separator. A bare file name appearing inside free-form
error text may not be redacted. This is tracked as a defect and production
diagnostics should stay disabled until it is fixed. If you have opted in and are
concerned, turn diagnostics off in Settings.

## Where your data is stored

All runtime data stays in your Windows user profile:

```text
%LOCALAPPDATA%\DuplicateTransferManager
```

That directory holds hash caches, transfer reports, resume journals, quarantined
files, logs, activity records, sorting history, and downloaded updates. Nothing
is written outside it except the files you explicitly ask the app to copy, move,
or restore.

Uninstalling the application does not delete this directory, so quarantined
files and history survive an upgrade. Delete it manually if you want that data
gone.

## Quarantine, not deletion

Duplicate handling never permanently deletes your files. Confirmed duplicates
are moved into app-managed quarantine and can be restored. Android duplicates
are copied into quarantine — originals on the phone are left untouched.

The one exception is the Sort Files "Recycle Bin" action, which you must select
deliberately. It uses the Windows Recycle Bin and cannot be undone from within
this app.

## Android access

The app talks to your phone through ADB. It reads the folders you point it at
and copies files from them. It does not install anything on the phone, does not
modify or delete files on the phone during an import, and does not change your
system-wide ADB installation or environment variables.

While a transfer runs, the app may set the Android "stay awake while charging"
developer setting so the device does not sleep mid-transfer. The previous value
is restored when the transfer finishes.

## Questions

Open an issue on the repository. For security-sensitive reports, follow
[SECURITY.md](SECURITY.md) instead.
