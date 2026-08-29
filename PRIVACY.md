# Privacy

Duplicate & Transfer Manager is local-first. It processes your files on your PC
and does not upload them anywhere.

## What leaves your PC

Under default settings, nothing.

This build makes **no network requests at all**.

1. **Update checks do not happen.** No release source is configured, and no code
   path in the application reaches the update service, so the app never contacts
   a server to look for a new version. Manifest verification is implemented and
   tested, ready for a future release channel, but it is dormant: nothing calls
   it. Earlier versions of this document described a daily update check. That
   was never true in a shipped build, and the claim has been removed rather than
   left to imply traffic that does not occur.
2. **Crash diagnostics are disabled by default and have no transport.** Reports
   are written locally and sanitized. Nothing is transmitted.

If either of these ever starts making a request, this document must change in
the same commit.

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

Sanitization covers rooted paths (`C:\Users\...`, UNC paths, and paths beginning
with a separator) and also bare file names appearing in free-form error text, so
`Could not read IMG_1234.jpg` becomes
`Could not read <redacted:filename:f-caf88bc25fd8>`.

Redaction is deliberately conservative in one direction and careful in the
other: an ordinary uppercase word such as `PERMISSION` is left readable, because
over-redacting error text makes reports useless without making them safer, while
anything shaped like a device serial, hash, path, or file name is replaced.

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
