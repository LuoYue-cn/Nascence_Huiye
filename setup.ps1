param([switch]$WithOllama,[switch]$PullModel)
$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot
python -m venv venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create Python virtual environment.' }
$Python=Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
npm ci --prefix frontend
if ($LASTEXITCODE -ne 0) { throw 'npm install failed.' }
npm run build --prefix frontend
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
if (!$env:OLLAMA_MODELS) { $env:OLLAMA_MODELS=Join-Path $PSScriptRoot 'ollama\models' }
New-Item -ItemType Directory -Force $env:OLLAMA_MODELS | Out-Null
if ($WithOllama) { & "$PSScriptRoot\run\install_ollama.ps1" }
$OwnedOllama=$null
try {
    if ($PullModel) {
        $Ollama=Join-Path $PSScriptRoot 'ollama\bin\ollama.exe'
        if (!(Test-Path $Ollama)) { $Ollama=(Get-Command ollama -ErrorAction Stop).Source }
        try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null } catch {
            $OwnedOllama=Start-Process $Ollama -ArgumentList 'serve' -PassThru -NoNewWindow
            $Ready=$false
            for ($i=0;$i -lt 60;$i++) {
                try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null; $Ready=$true; break } catch { Start-Sleep -Seconds 1 }
                if ($OwnedOllama.HasExited) { throw 'Ollama exited during startup.' }
            }
            if (!$Ready) { throw 'Ollama readiness timed out.' }
        }
        $Model=(& $Python -c 'from config.api_config import config; print(config["ollama_embed_model"])').Trim()
        & $Ollama pull $Model
        if ($LASTEXITCODE -ne 0) { throw 'Model pull failed.' }
    }
} finally {
    if ($OwnedOllama -and !$OwnedOllama.HasExited) { Stop-Process -Id $OwnedOllama.Id; $OwnedOllama.WaitForExit() }
}
Write-Host 'Setup complete. Run start.bat.'
