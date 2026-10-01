param([string]$BindAddress='127.0.0.1',[int]$Port=8000)
$ErrorActionPreference='Stop'
$TaskRoot=$PSScriptRoot
Set-Location $TaskRoot
$Python=Join-Path $TaskRoot 'venv\Scripts\python.exe'
if (!(Test-Path $Python)) { throw 'Run setup.ps1 first.' }
if (!(Test-Path 'frontend\dist\index.html')) { throw 'Build the management panel first.' }
if (!$env:OLLAMA_MODELS) { $env:OLLAMA_MODELS=Join-Path $TaskRoot 'ollama\models' }
$OwnedOllama=$null
try {
    $Url=(& $Python -c 'from config.api_config import config; print(config["ollama_base_url"].rstrip("/"))').Trim()
    if ($Url -in @('http://localhost:11434','http://127.0.0.1:11434')) {
        try { Invoke-RestMethod "$Url/api/tags" -TimeoutSec 2 | Out-Null } catch {
            $Ollama=Join-Path $TaskRoot 'ollama\bin\ollama.exe'
            if (Test-Path $Ollama) {
                $OwnedOllama=Start-Process $Ollama -ArgumentList 'serve' -PassThru -NoNewWindow
                for ($i=0;$i -lt 30;$i++) {
                    try { Invoke-RestMethod "$Url/api/tags" -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Seconds 1 }
                    if ($OwnedOllama.HasExited) { break }
                }
            }
        }
    }
    & $Python main.py --host $BindAddress --port $Port
    if ($LASTEXITCODE -ne 0) { throw "Service exited with code $LASTEXITCODE" }
} finally {
    if ($OwnedOllama -and !$OwnedOllama.HasExited) { Stop-Process -Id $OwnedOllama.Id; $OwnedOllama.WaitForExit() }
}
