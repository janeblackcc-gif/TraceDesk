param(
    [string]$RuntimeRoot,
    [int]$Port = 0,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$pythonExe = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Missing .venv. Follow the installation steps in README.md.' }
$env:PYTHONUTF8 = '1'
if ($RuntimeRoot) {
    $runtimeExe = Join-Path $RuntimeRoot 'ollama-0.33.3/ollama.exe'
    $modelStore = Join-Path $RuntimeRoot 'models'
    foreach ($required in @($runtimeExe, $modelStore)) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Required portable-runtime path missing: $required" }
    }
    $env:TRACEDESK_OLLAMA_URL = 'http://127.0.0.1:11435'
    if (-not $Check) {
        $runtimeListener = @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -EQ 11435)
        if (-not $runtimeListener.Count) {
            $logRoot = Join-Path $PSScriptRoot 'data/local_runtime'
            [System.IO.Directory]::CreateDirectory($logRoot) | Out-Null
            $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
            $env:OLLAMA_HOST = '127.0.0.1:11435'
            $env:OLLAMA_MODELS = $modelStore
            $env:OLLAMA_NUM_PARALLEL = '1'
            $env:OLLAMA_NO_CLOUD = '1'
            $runtime = Start-Process -FilePath $runtimeExe -ArgumentList 'serve' -WorkingDirectory (Split-Path $runtimeExe) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot "$stamp-ollama.out.log") -RedirectStandardError (Join-Path $logRoot "$stamp-ollama.err.log")
            if ($runtime.WaitForExit(2000)) { throw "Ollama exited with code $($runtime.ExitCode); see $logRoot" }
        }
    }
}
$launchArgs = @((Join-Path $PSScriptRoot 'scripts/start.py'), '--require-models')
if ($Port) { $launchArgs += @('--port', "$Port") }
if ($Check) { $launchArgs += '--check' }
& $pythonExe @launchArgs
exit $LASTEXITCODE
