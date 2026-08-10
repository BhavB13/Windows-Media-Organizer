#define MyAppName "Duplicate & Transfer Manager"
#define MyAppVersion GetEnv("DTM_VERSION")
#if MyAppVersion == ""
#define MyAppVersion "0.8.0"
#endif
#define MyAppPublisher "BhavB13"
#define MyAppExeName "DuplicateTransferManager.exe"
#define MyAppUrl "https://github.com/BhavB13/Windows-Media-Organizer"
; Pass /DSIGNED to ISCC to sign the installer and uninstaller. Without a
; code-signing certificate the build still produces a working installer, which
; is what makes an unsigned preview download possible at all; Windows
; SmartScreen will warn about it until a signed release is published.

[Setup]
AppId={{7E431F55-FDDE-4DD1-98BA-2F2E6171243D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
AppSupportURL={#MyAppUrl}/issues
AppUpdatesURL={#MyAppUrl}/releases
AppCopyright=Copyright (c) BhavB13. MIT Licensed.
VersionInfoVersion={#MyAppVersion}
LicenseFile=..\LICENSE
DefaultDirName={autopf}\DuplicateTransferManager
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Relative to this file's directory, so it must climb out of packaging\ to
; reach the repository dist\installer that the release script and the CI
; workflow both look in. Without the prefix the installer was written to
; packaging\dist\installer and the build failed its own existence check.
OutputDir=..\dist\installer
OutputBaseFilename=DuplicateTransferManagerSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\assets\app.ico
SetupLogging=yes
#ifdef SIGNED
SignedUninstaller=yes
SignTool=signtool
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\dist\DuplicateTransferManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
