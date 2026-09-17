[CmdletBinding()]
param(
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')]
    [string]$RunId = ("t075-formal-{0}" -f (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')),
    [string]$Alias = 'tracedesk-compshare-p1',
    [string]$BundleDirectory,
    [string]$CredentialsFile,
    [string]$ProjectRoot,
    [string]$SourceEnv,
    [string]$PublicOrigin,
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')]
    [string]$ResumeFromRunId,
    [switch]$KeepRunning
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$learnSsh = Join-Path $codexRoot 'skills/learn-ssh/scripts/ssh-node-ops.mjs'
if (-not $BundleDirectory) {
    $BundleDirectory = Join-Path $repositoryRoot 'artifacts/t075-launch-bundle-20260917-10'
}
if (-not $CredentialsFile) {
    $CredentialsFile = Join-Path $repositoryRoot '.local-secrets/target-browser-credentials-20260915-02.json'
}

function Invoke-LearnSshJson {
    param(
        [Parameter(Mandatory)] [string[]]$Arguments,
        [AllowNull()] [string]$StandardInput,
        [switch]$AllowFailure
    )

    $separatorIndex = [Array]::IndexOf($Arguments, '--')
    if ($separatorIndex -ge 0) {
        $beforeSeparator = if ($separatorIndex -gt 0) { @($Arguments[0..($separatorIndex - 1)]) } else { @() }
        $cliArguments = $beforeSeparator + @('--json') + @($Arguments[$separatorIndex..($Arguments.Count - 1)])
    }
    else {
        $cliArguments = @($Arguments) + @('--json')
    }
    if ($PSBoundParameters.ContainsKey('StandardInput')) {
        $raw = $StandardInput | & node $learnSsh @cliArguments 2>&1
    }
    else {
        $raw = & node $learnSsh @cliArguments 2>&1
    }
    $nativeExitCode = $LASTEXITCODE
    $text = ($raw | ForEach-Object { $_.ToString() }) -join "`n"
    try {
        $value = $text | ConvertFrom-Json
    }
    catch {
        throw "learn-ssh returned non-JSON output (exit $nativeExitCode): $text"
    }
    if ((-not $AllowFailure) -and (($nativeExitCode -ne 0) -or (-not $value.success))) {
        $errorProperty = $value.PSObject.Properties['error']
        $stderrProperty = $value.PSObject.Properties['stderr']
        $detail = if ($null -ne $errorProperty) { $errorProperty.Value } elseif ($null -ne $stderrProperty) { $stderrProperty.Value } else { $text }
        throw "learn-ssh failed (exit $nativeExitCode): $detail"
    }
    return $value
}

function Get-JsonValue {
    param(
        [Parameter(Mandatory)] [object]$Object,
        [Parameter(Mandatory)] [string]$Name,
        [AllowNull()] [object]$Default
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

if (-not (Test-Path -LiteralPath $learnSsh -PathType Leaf)) {
    throw "LearnSSH CLI not found: $learnSsh"
}
$bundleManifestPath = Join-Path $BundleDirectory 'manifest.json'
$bundleArchive = Join-Path $BundleDirectory 't075-launch-bundle.tar.gz'
if (-not (Test-Path -LiteralPath $bundleManifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $bundleArchive -PathType Leaf)) {
    throw "T-075 launch bundle is incomplete: $BundleDirectory"
}
if (-not (Test-Path -LiteralPath $CredentialsFile -PathType Leaf)) {
    throw "Private admin credentials file is missing: $CredentialsFile"
}

$bundleManifest = Get-Content -LiteralPath $bundleManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
$actualBundleHash = (Get-FileHash -LiteralPath $bundleArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($bundleManifest.scope -ne 't075-launch-bundle' -or $bundleManifest.formal_claim -ne 'none' -or
        $bundleManifest.contains_credentials -ne $false -or
        $bundleManifest.archive.sha256 -ne $actualBundleHash) {
    throw 'Launch bundle manifest or SHA-256 validation failed.'
}
$credentialData = Get-Content -LiteralPath $CredentialsFile -Raw -Encoding utf8 | ConvertFrom-Json
$legacyCredentials = ($null -ne $credentialData.PSObject.Properties['email']) -and
    ($null -ne $credentialData.PSObject.Properties['password'])
$capacityCredentials = ($null -ne $credentialData.PSObject.Properties['admin_email']) -and
    ($null -ne $credentialData.PSObject.Properties['admin_password'])
if (-not ($legacyCredentials -or $capacityCredentials)) {
    throw 'Credentials file does not contain a supported admin credential shape.'
}

$explicitValues = @(@($ProjectRoot, $SourceEnv, $PublicOrigin) | Where-Object { $_ })
if ($explicitValues.Count -ne 0 -and $explicitValues.Count -ne 3) {
    throw 'ProjectRoot, SourceEnv and PublicOrigin must be supplied together or all omitted for discovery.'
}

$localOutput = Join-Path $repositoryRoot "artifacts/t075-target-runs/$RunId"
if (Test-Path -LiteralPath $localOutput) {
    throw "Local run output already exists: $localOutput"
}
New-Item -ItemType Directory -Path $localOutput | Out-Null

$remoteReachable = $false
$poweroffRequested = $false
$runResult = $null
$downloaded = $false
$remoteUploadRoot = "/srv/tracedesk/t075-upload/$RunId"
$remoteWorkRoot = "/srv/tracedesk/t075-work/$RunId"
$remoteSourceEnv = "$remoteUploadRoot/source.env"
$remoteArchive = "/srv/tracedesk/t075-export/$RunId.tar.gz"
$remoteHash = "$remoteArchive.sha256"

try {
    if ($explicitValues.Count -eq 0) {
        $discoveryCommand = @'
set -eu
compose_candidates=$(find /srv/tracedesk -maxdepth 6 -type f -path '*/deploy/compose.yaml' ! -path '/srv/tracedesk/t075-*/*' -print 2>/dev/null || true)
compose_count=$(printf '%s\n' "$compose_candidates" | sed '/^$/d' | wc -l)
if [ "$compose_count" -ne 1 ]; then
  printf 'expected one compose file, found %s\n%s\n' "$compose_count" "$compose_candidates" >&2
  exit 21
fi
compose_file=$compose_candidates
project_root=$(dirname "$(dirname "$compose_file")")
env_candidates=''
while IFS= read -r candidate; do
  if sudo -n grep -q '^TRACEDESK_SECRET_DIR=' "$candidate" && sudo -n grep -q '^TRACEDESK_PUBLIC_ORIGIN=' "$candidate"; then
    env_candidates=$(printf '%s\n%s' "$env_candidates" "$candidate")
  fi
done <<EOF
$(sudo -n find /srv/tracedesk -maxdepth 6 -type f \( -name '.env*' -o -name '*.env' \) ! -name '*.example' ! -path '/srv/tracedesk/t075-*/*' -print 2>/dev/null || true)
EOF
env_candidates=$(printf '%s\n' "$env_candidates" | sed '/^$/d')
env_count=$(printf '%s\n' "$env_candidates" | sed '/^$/d' | wc -l)
if [ "$env_count" -ne 1 ]; then
  printf 'expected one production env, found %s\n%s\n' "$env_count" "$env_candidates" >&2
  exit 22
fi
source_env=$env_candidates
public_origin=$(sudo -n sed -n 's/^TRACEDESK_PUBLIC_ORIGIN=//p' "$source_env" | tail -n 1 | tr -d '\r' | sed 's/^"//;s/"$//;s/^'"'"'//;s/'"'"'$//')
printf 'PROJECT_ROOT=%s\nSOURCE_ENV=%s\nPUBLIC_ORIGIN=%s\n' "$project_root" "$source_env" "$public_origin"
'@
        $discovery = Invoke-LearnSshJson -Arguments @('exec', $Alias, '--stdin', '--timeout', '120') -StandardInput $discoveryCommand -AllowFailure
        $remoteReachable = $null -ne $discovery.PSObject.Properties['alias']
        if (-not $discovery.success) {
            $discoveryError = Get-JsonValue -Object $discovery -Name 'stderr' -Default (Get-JsonValue -Object $discovery -Name 'error' -Default 'unknown error')
            throw "Remote discovery failed: $discoveryError"
        }
        foreach ($line in ($discovery.stdout -split "`n")) {
            if ($line -match '^PROJECT_ROOT=(.+)$') { $ProjectRoot = $Matches[1].Trim() }
            elseif ($line -match '^SOURCE_ENV=(.+)$') { $SourceEnv = $Matches[1].Trim() }
            elseif ($line -match '^PUBLIC_ORIGIN=(.+)$') { $PublicOrigin = $Matches[1].Trim() }
        }
    }
    else {
        $probe = Invoke-LearnSshJson -Arguments @('exec', $Alias, '--timeout', '120', '--', 'printf ready') -AllowFailure
        $remoteReachable = $null -ne $probe.PSObject.Properties['alias']
        if (-not $probe.success) {
            $probeError = Get-JsonValue -Object $probe -Name 'stderr' -Default (Get-JsonValue -Object $probe -Name 'error' -Default 'unknown error')
            throw "Remote readiness probe failed: $probeError"
        }
        if ($probe.stdout.Trim() -ne 'ready') { throw 'Unexpected remote readiness response.' }
    }

    if ($ProjectRoot -notmatch '^/srv/tracedesk/[A-Za-z0-9._/-]+$' -or
            $SourceEnv -notmatch '^/srv/tracedesk/[A-Za-z0-9._/-]+$' -or
            $PublicOrigin -notmatch '^https://[A-Za-z0-9.:-]+$') {
        throw 'Discovered target paths or HTTPS origin failed the strict safety pattern.'
    }

    $prepareCommand = @"
set -eu
test -f '$ProjectRoot/deploy/compose.yaml'
sudo -n test -f '$SourceEnv'
configured_origin=`$(sudo -n sed -n 's/^TRACEDESK_PUBLIC_ORIGIN=//p' '$SourceEnv' | tail -n 1 | tr -d '\r' | sed 's/^"//;s/"`$//;s/^'"'"'//;s/'"'"'`$//')
test "`$configured_origin" = '$PublicOrigin'
test ! -e '$remoteUploadRoot'
test ! -e '$remoteWorkRoot'
sudo -n install -d -m 0700 '$remoteUploadRoot' '$remoteWorkRoot'
sudo -n chown "`$(id -u):`$(id -g)" '$remoteUploadRoot' '$remoteWorkRoot'
sudo -n install -m 0600 -o "`$(id -u)" -g "`$(id -g)" '$SourceEnv' '$remoteSourceEnv'
"@
    Invoke-LearnSshJson -Arguments @('exec', $Alias, '--stdin', '--timeout', '120') -StandardInput $prepareCommand | Out-Null
    Invoke-LearnSshJson -Arguments @('upload', $Alias, $bundleArchive, "$remoteUploadRoot/bundle.tar.gz") | Out-Null
    Invoke-LearnSshJson -Arguments @('upload', $Alias, $CredentialsFile, "$remoteUploadRoot/admin-credentials.json") | Out-Null

    $extractCommand = @"
set -eu
actual=`$(sha256sum '$remoteUploadRoot/bundle.tar.gz' | awk '{print `$1}')
test "`$actual" = '$actualBundleHash'
tar --extract --gzip --file '$remoteUploadRoot/bundle.tar.gz' --directory '$remoteWorkRoot'
test -f '$remoteWorkRoot/scripts/t075_target_execute.sh'
chmod 0700 '$remoteWorkRoot/scripts/t075_target_execute.sh'
chmod 0600 '$remoteUploadRoot/admin-credentials.json'
"@
    Invoke-LearnSshJson -Arguments @('exec', $Alias, '--stdin', '--timeout', '300') -StandardInput $extractCommand | Out-Null

    if ($ResumeFromRunId) {
        $resumeCommand = @"
set -euo pipefail
old_run='$ResumeFromRunId'
run_id='$RunId'
project_root='$ProjectRoot'
bundle_root='$remoteWorkRoot'
output='/srv/tracedesk/t075-evidence/$RunId'
export_root='/srv/tracedesk/t075-export'
capacity_env="/srv/tracedesk/t075-private/`$old_run.env"
provision_status="/srv/tracedesk/t075-evidence/`$old_run/provision/status.json"
data_dir="/srv/tracedesk/t075-data/`$old_run"
credentials='/srv/tracedesk/t075-private/.t075-capacity-credentials.json'
python_bin=/srv/tracedesk/vllm-venv/bin/python
test -x "`$python_bin"
test -f "`$capacity_env"
test -f "`$provision_status"
test -f "`$credentials"
test -d "`$data_dir"
test ! -e "`$output"
mkdir -m 0700 "`$output"
archive_evidence() {
  tar --create --gzip --file "`$export_root/`$run_id.tar.gz" --directory /srv/tracedesk/t075-evidence "`$run_id"
  sha256sum "`$export_root/`$run_id.tar.gz" > "`$export_root/`$run_id.tar.gz.sha256"
}
archive_on_exit() {
  exit_code=`$?
  trap - EXIT
  printf 'exit_code=%s\ncompleted_at=%s\n' "`$exit_code" "`$(date --utc +%Y-%m-%dT%H:%M:%SZ)" > "`$output/target-exit-status.txt"
  archive_evidence
  exit "`$exit_code"
}
trap archive_on_exit EXIT
cd -- "`$project_root"
PYTHONPATH="`$bundle_root:`$project_root" "`$python_bin" "`$bundle_root/scripts/capacity_driver.py" run \
  --output "`$output/run" \
  --base-url '$PublicOrigin' \
  --credentials-file "`$credentials" \
  --provision-status "`$provision_status" \
  --project-dir "`$project_root" \
  --compose-file "`$project_root/deploy/compose.yaml" \
  --env-file "`$capacity_env" \
  --project-name tracedesk-t075 \
  --data-dir "`$data_dir" \
  --duration-seconds 1800 \
  --max-error-rate 0.05 \
  --max-rss-growth-bytes 536870912 \
  --max-vram-growth-bytes 1073741824 \
  --code-ref 'bundle-sha256:$actualBundleHash' \
  --insecure
trap - EXIT
printf 'exit_code=0\ncompleted_at=%s\nresume_from=%s\n' "`$(date --utc +%Y-%m-%dT%H:%M:%SZ)" "`$old_run" > "`$output/target-exit-status.txt"
archive_evidence
echo "`$export_root/`$run_id.tar.gz"
"@
        $executeCommand = $resumeCommand
    }
    else {
        $executeCommand = "bash '$remoteWorkRoot/scripts/t075_target_execute.sh' '$ProjectRoot' '$remoteSourceEnv' '$remoteUploadRoot/admin-credentials.json' '$PublicOrigin' '$RunId' '$actualBundleHash'"
    }
    $runResult = Invoke-LearnSshJson -Arguments @('exec', $Alias, '--stdin', '--timeout', '18000', '--daemon-idle-timeout', '19000') -StandardInput $executeCommand -AllowFailure
    $remoteExitCode = Get-JsonValue -Object $runResult -Name 'exitCode' -Default -1
    $remoteStdout = Get-JsonValue -Object $runResult -Name 'stdout' -Default ''
    $remoteStderr = Get-JsonValue -Object $runResult -Name 'stderr' -Default (Get-JsonValue -Object $runResult -Name 'error' -Default '')
    Set-Content -LiteralPath (Join-Path $localOutput 'remote-stdout.log') -Value $remoteStdout -Encoding utf8
    Set-Content -LiteralPath (Join-Path $localOutput 'remote-stderr.log') -Value $remoteStderr -Encoding utf8

    $archiveProbe = Invoke-LearnSshJson -Arguments @('exec', $Alias, '--timeout', '120', '--', "test -f '$remoteArchive' -a -f '$remoteHash'") -AllowFailure
    if ($archiveProbe.success) {
        $localArchive = Join-Path $localOutput "$RunId.tar.gz"
        $localHash = "$localArchive.sha256"
        Invoke-LearnSshJson -Arguments @('download', $Alias, $remoteArchive, $localArchive) | Out-Null
        Invoke-LearnSshJson -Arguments @('download', $Alias, $remoteHash, $localHash) | Out-Null
        $expectedDownloadedHash = ((Get-Content -LiteralPath $localHash -Raw -Encoding utf8) -split '\s+')[0].ToLowerInvariant()
        $actualDownloadedHash = (Get-FileHash -LiteralPath $localArchive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expectedDownloadedHash -ne $actualDownloadedHash) {
            throw 'Downloaded target evidence archive failed SHA-256 verification.'
        }
        $downloaded = $true
    }

    $poweroffRequested = (($runResult.success -and $downloaded) -or ($remoteStderr -match 'timed out'))

    $status = [ordered]@{
        schema_version = 1
        scope = 't075-target-launch'
        formal_claim = 'none'
        run_id = $RunId
        bundle_sha256 = $actualBundleHash
        remote_exit_code = $remoteExitCode
        evidence_downloaded = $downloaded
        automatic_poweroff_requested = $poweroffRequested -and (-not $KeepRunning)
    }
    $status | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $localOutput 'launcher-status.json') -Encoding utf8
    if (-not $runResult.success) {
        throw "T-075 target execution failed with remote exit code $remoteExitCode; available evidence was downloaded."
    }
    if (-not $downloaded) {
        throw 'T-075 target execution returned success but no evidence archive was available.'
    }
}
finally {
    if ($remoteReachable -and $poweroffRequested -and (-not $KeepRunning)) {
        try {
            $poweroff = Invoke-LearnSshJson -Arguments @(
                'exec', $Alias, '--timeout', '120', '--',
                "sudo -n systemd-run --unit=tracedesk-t075-poweroff-$RunId --on-active=5s /usr/bin/systemctl poweroff"
            ) -AllowFailure
        }
        catch {
            $poweroff = $null
        }
        if (($null -eq $poweroff) -or (-not $poweroff.success)) {
            Write-Warning 'Automatic remote poweroff scheduling failed; power off the instance in the provider console immediately.'
        }
    }
}

Write-Host "T-075 evidence downloaded and verified: $localOutput"
