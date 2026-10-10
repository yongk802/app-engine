param(
    [string]$EngineRepository,
    [string]$StoreRepository,
    [string]$AppsDirectory,
    [string]$InstallDirectory,
    [int]$EnginePort=0,
    [int]$StorePort=0
)
$ErrorActionPreference='Stop'
$repoRoot=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (-not $EngineRepository) {
    if (Test-Path (Join-Path $repoRoot 'engine.py')) { $EngineRepository=$repoRoot }
    else { $EngineRepository=Join-Path (Split-Path $repoRoot -Parent) 'app-engine' }
}
$EngineRepository=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EngineRepository)
$parent=Split-Path $EngineRepository -Parent
if (-not $StoreRepository) { $StoreRepository=Join-Path $parent 'app-store' }
if (-not $AppsDirectory) { $AppsDirectory=Join-Path $parent 'personal-apps' }
$StoreRepository=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StoreRepository)
$AppsDirectory=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($AppsDirectory)
if (-not $InstallDirectory) {
    if (Test-Path 'D:\') { $InstallDirectory='D:\Apps\App Engine' }
    else { $InstallDirectory=Join-Path $env:LOCALAPPDATA 'App Engine' }
}
$InstallDirectory=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($InstallDirectory)
$destination=Join-Path $InstallDirectory 'AppEngine.exe'
$running=@(Get-CimInstance Win32_Process -Filter "Name='AppEngine.exe'" | Where-Object {$_.ExecutablePath -eq $destination})
if ($running.Count) { throw 'Close the App Engine window and stop its managed services before updating the launcher.' }
& (Join-Path $PSScriptRoot 'build.ps1')
New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
$settingsPath=Join-Path $InstallDirectory 'settings.json'
if (Test-Path $settingsPath) { $settings=Get-Content $settingsPath -Raw | ConvertFrom-Json }
else {
    $settings=[pscustomobject]@{ EngineRepository=$EngineRepository; StoreRepository=$StoreRepository; AppsDirectory=$AppsDirectory;
        StateDirectory=(Join-Path $InstallDirectory 'app-state'); StoreDataDirectory=(Join-Path $StoreRepository 'store-data'); EnginePort=8770; StorePort=8780 }
}
if ($PSBoundParameters.ContainsKey('EngineRepository')) { $settings.EngineRepository=$EngineRepository }
if ($PSBoundParameters.ContainsKey('StoreRepository')) { $settings.StoreRepository=$StoreRepository }
if ($PSBoundParameters.ContainsKey('AppsDirectory')) { $settings.AppsDirectory=$AppsDirectory }
if ($EnginePort) { $settings.EnginePort=$EnginePort }; if ($StorePort) { $settings.StorePort=$StorePort }
# Use the same validator as the executable before changing the installation.
Add-Type -Path (Join-Path $PSScriptRoot 'Settings.cs') -ReferencedAssemblies System.Web.Extensions
$validated=New-Object AppEngineDesktop.Settings
foreach ($property in $settings.PSObject.Properties) { $validated.($property.Name)=$property.Value }
$validated.Validate()
$shortcutPath=Join-Path ([Environment]::GetFolderPath('Desktop')) 'App Engine.lnk'
$shell=New-Object -ComObject WScript.Shell
if (Test-Path $shortcutPath) {
    $existing=$shell.CreateShortcut($shortcutPath)
    if ($existing.TargetPath -ne $destination) {throw "Refusing to replace unrelated shortcut: $shortcutPath"}
}
Copy-Item (Join-Path $PSScriptRoot 'build\AppEngine.exe') $destination -Force
[AppEngineDesktop.Settings]::Save($settingsPath,$validated)
$link=$shell.CreateShortcut($shortcutPath)
$link.TargetPath=$destination
$link.WorkingDirectory=$InstallDirectory
$link.Description='App Engine and App Store control panel'
$link.IconLocation="$destination,0"
$link.Save()
Write-Output "Installed: $destination"
Write-Output "Desktop shortcut: $shortcutPath"
