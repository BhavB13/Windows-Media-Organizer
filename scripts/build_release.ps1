param(
    [string]$Version = "",
    [string]$Channel = "stable",
    [string]$ManifestPrivateKey = ""
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt

if (-not $Version) {
    $Version = python -c "from duplicate_transfer_manager.version import __version__; print(__version__)"
}

$env:DTM_VERSION = $Version

python -m unittest discover -s tests -v
python scripts\create_app_icon.py
pyinstaller --noconfirm packaging\duplicate_transfer_manager.spec

$signtool = "${env:ProgramFiles(x86)}\Windows Kits\10\bin\x64\signtool.exe"
if (-not (Test-Path $signtool)) {
    $signtool = "signtool.exe"
}
if (-not $env:WINDOWS_SIGNING_CERT_THUMBPRINT) {
    throw "WINDOWS_SIGNING_CERT_THUMBPRINT is required for Phase 7 release builds."
}
& $signtool sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /sha1 $env:WINDOWS_SIGNING_CERT_THUMBPRINT "dist\DuplicateTransferManager\DuplicateTransferManager.exe"
& $signtool verify /pa /v "dist\DuplicateTransferManager\DuplicateTransferManager.exe"

$platformToolsManifest = Get-Content -Raw packaging\android_platform_tools_manifest.json | ConvertFrom-Json
$platformToolsZip = Join-Path $env:TEMP "platform-tools.zip"
$platformToolsTemp = Join-Path $env:TEMP "dtm-platform-tools"
Remove-Item -Recurse -Force $platformToolsTemp -ErrorAction SilentlyContinue
Invoke-WebRequest -Uri $platformToolsManifest.download_url -OutFile $platformToolsZip
Expand-Archive -LiteralPath $platformToolsZip -DestinationPath $platformToolsTemp -Force
$adb = Join-Path $platformToolsTemp "platform-tools\adb.exe"
if (-not (Test-Path $adb)) {
    throw "Downloaded Android Platform Tools did not contain adb.exe"
}
$adbVersion = & $adb version
if ($adbVersion -notmatch [regex]::Escape($platformToolsManifest.version)) {
    throw "Downloaded Android Platform Tools version did not match pinned version $($platformToolsManifest.version): $adbVersion"
}
Copy-Item -Recurse -Force (Join-Path $platformToolsTemp "platform-tools") "dist\DuplicateTransferManager\platform-tools"

$inno = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $inno)) {
    throw "Inno Setup 6 compiler was not found at $inno"
}
$signCommand = "`"$signtool`" sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /sha1 $env:WINDOWS_SIGNING_CERT_THUMBPRINT `$f"
& $inno "/Ssigntool=$signCommand" packaging\installer.iss

$installer = Join-Path $root "dist\installer\DuplicateTransferManagerSetup-$Version.exe"
if (-not (Test-Path $installer)) {
    throw "Expected installer was not created: $installer"
}
& $signtool verify /pa /v $installer

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
$size = (Get-Item -LiteralPath $installer).Length
$manifest = [ordered]@{
    version = $Version
    channel = $Channel
    installer_url = "https://github.com/BhavB13/Windows-Media-Organizer/releases/download/v$Version/DuplicateTransferManagerSetup-$Version.exe"
    size = $size
    sha256 = $hash
    release_notes_url = "https://github.com/BhavB13/Windows-Media-Organizer/releases/tag/v$Version"
    minimum_supported_version = "0.6.0"
    signature_algorithm = "RSASSA-PKCS1-v1_5-SHA256"
    signature = ""
    authenticode_thumbprint = $env:WINDOWS_SIGNING_CERT_THUMBPRINT
    publisher = "BhavB13"
}

$manifestPath = Join-Path $root "dist\installer\update-manifest-$Version.json"
$manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $manifestPath
if ($ManifestPrivateKey) {
    python scripts\sign_update_manifest.py $manifestPath $ManifestPrivateKey
}
Write-Host "Built $installer"
Write-Host "Update manifest: $manifestPath"
