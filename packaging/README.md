# Packaging

PyInstaller and Inno Setup configuration will live here. Signed Windows release
packaging is scheduled for Phase 7.

Phase 5 pins the Android Platform Tools release metadata in
`android_platform_tools_manifest.json` so diagnostics and release packaging use
one explicit, license-compatible version. The application reads this manifest
without modifying system-wide ADB installations or environment variables.
