param(
    [Parameter(Mandatory=$true)][string]$Serial,
    [string]$Root = "/sdcard",
    [string]$TaskName = "Media Organizer ADB Cache",
    [string]$Day = "Sunday",
    [string]$At = "03:00"
)

$project = $PSScriptRoot
$runner = Join-Path $project "refresh_adb_cache.ps1"
$safeProject = $project.Replace("'", "''")
$safeSerial = $Serial.Replace("'", "''")
$safeRoot = $Root.Replace("'", "''")

@"
`$state = (& adb -s '$safeSerial' get-state 2>`$null)
if (`$state -ne 'device') { exit 0 }
Set-Location '$safeProject'
& py -3 build_drive_cache.py --adb --serial '$safeSerial' --root '$safeRoot'
"@ | Set-Content -Path $runner -Encoding UTF8

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $Day -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Refresh the Media Organizer ADB cache when the phone is connected." -Force
Write-Host "Scheduled '$TaskName' weekly on $Day at $At. Disconnected phones are skipped safely."
