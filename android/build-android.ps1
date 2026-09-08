<#
.SYNOPSIS
    Reproducible Windows build/test entry point for the "Phone to Drive" Android app.

.DESCRIPTION
    Locates the JDK and the Android SDK, then drives the pinned Gradle wrapper.
    Everything the build needs is discovered or reported; the script never guesses
    silently and never installs anything.

    The Gradle wrapper batch file does not survive being launched from a shell that
    mangles the space in "Media Organizer", so this script is the supported way to
    build on this machine.

.PARAMETER Tasks
    Gradle tasks to run. Defaults to the full acceptance set:
    testDebugUnitTest (JVM engine tests), lintDebug, assembleDebug.

.PARAMETER JdkHome
    Overrides JDK auto-detection.

.PARAMETER SdkHome
    Overrides Android SDK auto-detection.

.PARAMETER Offline
    Adds --offline, for rebuilding with an already-populated Gradle cache.

.EXAMPLE
    .\build-android.ps1
    .\build-android.ps1 -Tasks testDebugUnitTest
    .\build-android.ps1 -Tasks assembleDebug -Offline
#>
[CmdletBinding()]
param(
    [string[]] $Tasks = @('testDebugUnitTest', 'lintDebug', 'assembleDebug'),
    [string] $JdkHome,
    [string] $SdkHome,
    [switch] $Offline
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectDir = $PSScriptRoot

function Resolve-Jdk {
    param([string] $Explicit)

    $candidates = @()
    if ($Explicit)    { $candidates += $Explicit }
    if ($env:JAVA_HOME) { $candidates += $env:JAVA_HOME }
    $candidates += @(
        'C:\Program Files\Android\Android Studio\jbr',
        'C:\Program Files\Eclipse Adoptium\jdk-17',
        'C:\Program Files\Microsoft\jdk-17'
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path (Join-Path $candidate 'bin\java.exe'))) {
            return (Resolve-Path $candidate).Path
        }
    }
    throw "No JDK found. Pass -JdkHome, or install Android Studio's bundled JBR."
}

function Resolve-Sdk {
    param([string] $Explicit)

    $candidates = @()
    if ($Explicit)         { $candidates += $Explicit }
    if ($env:ANDROID_HOME) { $candidates += $env:ANDROID_HOME }
    if ($env:ANDROID_SDK_ROOT) { $candidates += $env:ANDROID_SDK_ROOT }
    $candidates += (Join-Path $env:LOCALAPPDATA 'Android\Sdk')

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path (Join-Path $candidate 'platforms'))) {
            return (Resolve-Path $candidate).Path
        }
    }
    throw "No Android SDK found. Pass -SdkHome, or install one via Android Studio."
}

$jdk = Resolve-Jdk -Explicit $JdkHome
$sdk = Resolve-Sdk -Explicit $SdkHome

# app/build.gradle compiles against this platform; failing here beats a confusing Gradle error.
$requiredPlatform = 'android-35'
if (-not (Test-Path (Join-Path $sdk "platforms\$requiredPlatform"))) {
    throw "SDK at '$sdk' has no $requiredPlatform. Install it via Android Studio's SDK Manager."
}

$env:JAVA_HOME = $jdk
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk

# local.properties is what the Android Gradle plugin reads; regenerate it so a fresh
# clone builds without any manual step.
$localProperties = Join-Path $projectDir 'local.properties'
"sdk.dir=$($sdk -replace '\\', '\\')" | Set-Content -Path $localProperties -Encoding ASCII

Write-Host "JDK        : $jdk"
Write-Host "Android SDK: $sdk"
Write-Host "Tasks      : $($Tasks -join ', ')"

# This machine has 16 GB of RAM; a single worker and a small heap keep a build from
# competing with the emulator or an IDE.
$gradleArgs = @(
    '--no-daemon',
    '--max-workers=1',
    '-Dorg.gradle.jvmargs=-Xmx768m -Dfile.encoding=UTF-8',
    '--stacktrace'
)

# lint fetches its own analysis tools from dl.google.com on first run, and they are large.
# On a slow or intermittent link Gradle's stock timeouts give up part-way and report a
# resolution failure that reads like a broken build. Wait longer and retry instead.
$gradleArgs += @(
    '-Dorg.gradle.internal.http.connectionTimeout=120000',
    '-Dorg.gradle.internal.http.socketTimeout=300000',
    '-Dorg.gradle.internal.repository.max.retries=10'
)
if ($Offline) { $gradleArgs += '--offline' }
$gradleArgs += $Tasks

$wrapper = Join-Path $projectDir 'gradlew.bat'
Push-Location $projectDir
try {
    & $wrapper @gradleArgs
    $exit = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($exit -ne 0) {
    Write-Host "Gradle failed with exit code $exit." -ForegroundColor Red
    exit $exit
}

$apk = Join-Path $projectDir 'app\build\outputs\apk\debug\app-debug.apk'
if (Test-Path $apk) {
    $hash = (Get-FileHash -Path $apk -Algorithm SHA256).Hash
    $size = [math]::Round((Get-Item $apk).Length / 1KB, 1)
    Write-Host ""
    Write-Host "APK    : $apk"
    Write-Host "Size   : $size KB"
    Write-Host "SHA-256: $hash"

    # A package Gradle produced but that Android would refuse to install is not a successful
    # build. Ask the SDK's own verifier rather than assuming.
    $buildTools = Get-ChildItem -Path (Join-Path $sdk 'build-tools') -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name |
        Select-Object -Last 1
    $apkSigner = if ($buildTools) { Join-Path $buildTools.FullName 'apksigner.bat' } else { $null }
    if ($apkSigner -and (Test-Path $apkSigner)) {
        Write-Host ""
        Write-Host "Verifying the signature with $($buildTools.Name) apksigner..."
        & $apkSigner verify --verbose $apk
        if ($LASTEXITCODE -ne 0) {
            Write-Host "The APK failed signature verification." -ForegroundColor Red
            exit $LASTEXITCODE
        }
        Write-Host "Signature verified. DEBUG-signed: installable for testing, not for" -ForegroundColor Yellow
        Write-Host "distribution. A release build needs its own keystore." -ForegroundColor Yellow
    }
    else {
        Write-Host "No apksigner under the SDK build-tools; signature NOT verified." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Build succeeded." -ForegroundColor Green
