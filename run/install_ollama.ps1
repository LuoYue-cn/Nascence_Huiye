$ErrorActionPreference='Stop'
$TaskRoot=Split-Path $PSScriptRoot -Parent
$Arch=[System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLower()
if ($Arch -eq 'x64') { $Arch='amd64' }
if ($Arch -notin @('amd64','arm64')) { throw 'Unsupported Windows CPU architecture.' }
$Temp=Join-Path $TaskRoot ('.ollama-install-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $Temp | Out-Null
try {
    $Zip=Join-Path $Temp 'ollama.zip'
    Invoke-WebRequest "https://ollama.com/download/ollama-windows-$Arch.zip" -OutFile $Zip
    $Runtime=Join-Path $Temp 'runtime'
    Expand-Archive $Zip $Runtime
    if (!(Test-Path (Join-Path $Runtime 'ollama.exe'))) { throw 'The Ollama release is incomplete.' }
    $Target=Join-Path $TaskRoot 'ollama\bin'
    New-Item -ItemType Directory -Force (Split-Path $Target -Parent) | Out-Null
    if (Test-Path $Target) { Move-Item $Target (Join-Path $Temp 'previous-runtime') }
    # Keep DLLs and all runtime files together with the executable.
    Move-Item $Runtime $Target
} finally { Remove-Item $Temp -Recurse -Force -ErrorAction SilentlyContinue }
