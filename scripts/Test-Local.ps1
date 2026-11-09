param([ValidateRange(10, 1800)][int]$TimeoutSeconds = 180)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$baseline = Get-Content -LiteralPath (Join-Path $root 'baseline.json') -Raw | ConvertFrom-Json

function Invoke-Hidden([string[]]$Arguments, [int]$Timeout = 30) {
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = 'docker.exe'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    # Never inherit a remote Docker context/host.
    foreach ($key in @('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')) {
        $info.Environment.Remove($key) | Out-Null
    }
    $info.ArgumentList.Add('--host')
    $info.ArgumentList.Add('npipe:////./pipe/dockerDesktopLinuxEngine')
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $process = [System.Diagnostics.Process]::Start($info)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Timeout * 1000)) {
            $process.Kill($true)
            $process.WaitForExit()
            throw "Docker command exceeded $Timeout seconds."
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Output = $stdout.GetAwaiter().GetResult()
            Error = $stderr.GetAwaiter().GetResult()
        }
    } finally { $process.Dispose() }
}

$inspect = Invoke-Hidden -Arguments @('image', 'inspect', $baseline.image, '--format', '{{json .}}')
if ($inspect.ExitCode -ne 0) { throw "Pinned Hermes image is missing. Download it explicitly: $($baseline.image)" }
$image = $inspect.Output | ConvertFrom-Json
# Classic Docker exposes a config ID; containerd may expose an index ID instead.
# Registry digest + platform are portable across those storage backends.
if ($baseline.image -notin $image.RepoDigests -or "$($image.Os)/$($image.Architecture)" -ne $baseline.platform) {
    throw 'Local registry digest/platform does not match the deployment baseline.'
}

$name = 'hermes-session-title-test-' + [guid]::NewGuid().ToString('N')
$program = "import os,signal,sys,unittest; signal.signal(signal.SIGALRM, lambda *_: os._exit(124)); signal.alarm($TimeoutSeconds); suite=unittest.defaultTestLoader.discover('/project/tests'); result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())"
$arguments = @(
    'run', '--rm', '--pull=never', '--name', $name,
    '--platform', $baseline.platform,
    '--network', 'none', '--read-only', '--user', '10000:10000',
    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
    '--memory', '512m', '--memory-swap', '512m', '--cpus', '0.5', '--pids-limit', '128',
    '--mount', "type=bind,source=$root,target=/project,readonly",
    '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777',
    '--tmpfs', '/opt/data:rw,noexec,nosuid,nodev,size=32m,uid=10000,gid=10000,mode=0700',
    '--env', 'HOME=/opt/data', '--env', 'HERMES_HOME=/opt/data',
    '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONPATH=/project:/opt/hermes',
    '--workdir', '/project', '--entrypoint', 'python', $baseline.image,
    '-B', '-c', $program
)
try {
    $result = Invoke-Hidden -Arguments $arguments -Timeout ($TimeoutSeconds + 20)
    Write-Output $result.Output
    Write-Output $result.Error
    if ($result.ExitCode -ne 0) { throw "Local tests failed (exit $($result.ExitCode))." }
} finally {
    # Only this invocation's uniquely named container; never other Docker workloads.
    $clean = $false
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        try {
            $removed = Invoke-Hidden -Arguments @('container', 'rm', '--force', $name)
            $clean = $removed.ExitCode -eq 0 -or $removed.Error -match 'No such container'
        } catch { $clean = $false }
        # Recheck even confirmed absence: a cancelled create request can complete late.
        if ($attempt -lt 2) { Start-Sleep -Seconds 1 }
    }
    if (-not $clean) { Write-Warning "Cleanup unresolved for $name. Check the local Docker Desktop daemon." }
}
