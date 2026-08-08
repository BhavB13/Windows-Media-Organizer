# Security Policy

## Reporting a vulnerability

Report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/BhavB13/Windows-Media-Organizer/security/advisories/new)
on this repository. Please do not open a public issue for a vulnerability.

Include what you need to make the problem reproducible: affected version, the
steps, and what an attacker gains. If a proof of concept touches real files,
describe it rather than attaching anything containing personal data.

Expect an acknowledgement within a week. Because this is a small project, please
allow reasonable time for a fix before public disclosure.

## What counts as a vulnerability here

This is a local desktop application that moves a user's files. The issues that
matter most are:

- **Data loss or corruption.** Anything that can delete, overwrite, or corrupt a
  file the user did not agree to change. Duplicate handling is quarantine-based
  and must never delete permanently.
- **Update chain compromise.** Anything that lets an unsigned, tampered, or
  downgraded installer be accepted or launched.
- **Privacy leaks.** Anything that lets paths, file names, hashes, or device
  serials escape the machine, particularly through crash diagnostics.
- **Local privilege issues.** Anything that lets a file or directory outside the
  chosen scope be read or written.

## How updates are verified

An update is rejected unless all of the following hold:

- The manifest carries every required field and its channel matches the install.
- The version is strictly newer, and the installed version is at or above the
  manifest's minimum supported version.
- The manifest's RSA-SHA256 signature verifies against the public key shipped in
  the application.
- The downloaded installer's size and SHA-256 checksum match the manifest.
- On Windows, the installer's Authenticode signature is valid and its
  certificate thumbprint matches the one named in the signed manifest.

Downloads are bounded by the manifest-declared size, staged to a temporary file,
and promoted only after verification passes.

Only the public verification key is committed to this repository. The private
signing key and the Authenticode certificate live in protected CI secrets.

## Current security status

This project has not yet had a public release. One item is a release gate and is
documented here rather than hidden:

1. **The committed update key is a 1024-bit development key**
   (`packaging/update_public_key.json`, `key_id: dtm-dev-release-key-2026`).
   RSA-1024 is below current standards for a software-update trust root. It must
   be replaced with a production key before any public release.

Separately, no released build has yet been verified end to end: the packaged
installer, its signature, and the update flow have not been exercised on a clean
Windows machine.

## Supported versions

Until the first public release, only the current `main` branch is supported.
Once releases begin, the most recent stable release will receive security fixes.

## Scope

In scope: this repository's application code, packaging, and release workflow.

Out of scope: vulnerabilities in Qt, Pillow, Android Platform Tools, or Windows
itself — report those upstream. Also out of scope: issues that require an
attacker to already have administrator access to the user's machine, since at
that point the file system is theirs regardless.
