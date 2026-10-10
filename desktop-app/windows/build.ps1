param([switch]$Test,[string]$EngineRepository,[string]$StoreRepository)
$ErrorActionPreference='Stop'
$compiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $compiler)) { $compiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path $compiler)) { throw 'The Windows .NET Framework compiler is required.' }
$build=Join-Path $PSScriptRoot 'build'
New-Item -ItemType Directory -Path $build -Force | Out-Null
$executable=Join-Path $build 'AppEngine.exe'
# Generate the same four-tile app icon used by the macOS launcher.
Add-Type -AssemblyName System.Drawing
$bitmap=New-Object Drawing.Bitmap 64,64
$graphics=[Drawing.Graphics]::FromImage($bitmap)
$graphics.Clear([Drawing.Color]::FromArgb(31,48,92))
$colors=@([Drawing.Color]::Turquoise,[Drawing.Color]::DodgerBlue,[Drawing.Color]::Orange,[Drawing.Color]::MediumOrchid)
for ($index=0;$index -lt 4;$index++) {
    $brush=New-Object Drawing.SolidBrush $colors[$index]
    try {$graphics.FillRectangle($brush,12+($index%2)*22,12+[int][Math]::Floor($index/2)*22,18,18)} finally {$brush.Dispose()}
}
$icon=[Drawing.Icon]::FromHandle($bitmap.GetHicon())
$iconPath=Join-Path $build 'AppEngine.ico'
$stream=[IO.File]::Create($iconPath)
try {$icon.Save($stream)} finally {$stream.Dispose();$icon.Dispose();$graphics.Dispose();$bitmap.Dispose()}
$files=@('Settings.cs','WindowsProcess.cs','Services.cs','Launcher.cs','Program.cs') | ForEach-Object {Join-Path $PSScriptRoot $_}
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ "/win32icon:$iconPath" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll /reference:System.Management.dll "/out:$executable" $files
if ($LASTEXITCODE) {throw 'Windows launcher compilation failed.'}
if ($Test) {
    $testExe=Join-Path $build 'LauncherTests.exe'
    & $compiler /nologo /target:exe /main:AppEngineDesktop.Tests /reference:System.Web.Extensions.dll /reference:System.Management.dll "/out:$testExe" (Join-Path $PSScriptRoot 'Settings.cs') (Join-Path $PSScriptRoot 'WindowsProcess.cs') (Join-Path $PSScriptRoot 'Services.cs') (Join-Path $PSScriptRoot 'Tests.cs')
    if ($LASTEXITCODE) {throw 'Windows tests compilation failed.'}
    if ($EngineRepository -and $StoreRepository) { & $testExe $executable $EngineRepository $StoreRepository } else { & $testExe $executable }
    if ($LASTEXITCODE) {throw 'Windows launcher tests failed.'}
}
Write-Output "Built: $executable"
