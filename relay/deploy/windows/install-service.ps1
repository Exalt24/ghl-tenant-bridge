<#
.SYNOPSIS
  Runs the relay worker as a Windows scheduled task that starts at boot and restarts itself. No third party binary.

.DESCRIPTION
  Registers one task with Windows Task Scheduler:
    - trigger: at startup
    - principal: SYSTEM (a service account), so it runs whether or not anyone is logged on, highest run level
    - action: <python> -m relay.worker, working directory = the relay folder
    - on failure: restart every 1 minute, up to 999 times
    - no execution time limit (the Task Scheduler default of 3 days would stop the worker), one instance at a time

  The worker reads its settings from environment variables (DATABASE_URL or RELAY_DB_PATH, RELAY_CONFIG, and every secret the
  config names). A SYSTEM task sees MACHINE level variables, so set them first from an elevated PowerShell with
  [Environment]::SetEnvironmentVariable('NAME', 'value', 'Machine'). This script never reads, prints or stores a value: it only
  says whether each name is set.

  -WhatIf prints the exact task definition (built as real ScheduledTask objects, so a wrong parameter fails here) and registers
  nothing. Without -WhatIf the script must run elevated.

.EXAMPLE
  .\install-service.ps1 -WhatIf
  .\install-service.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = 'RelayWorker',
    [string]$RelayDir = '',
    [string]$PythonExe = '',
    [ValidateSet('SYSTEM', 'LOCAL SERVICE', 'NETWORK SERVICE')]
    [string]$RunAs = 'SYSTEM',
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

if (-not $RelayDir) {
    # $PSScriptRoot is not set inside a param() default on Windows PowerShell 5.1, so the default is worked out here
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $RelayDir = (Resolve-Path (Join-Path $here '..\..')).Path
}
if (-not (Test-Path -LiteralPath (Join-Path $RelayDir 'relay\worker.py'))) {
    throw "RelayDir '$RelayDir' does not hold relay\worker.py. Pass -RelayDir with the folder that does."
}
if (-not $PythonExe) {
    $venv = Join-Path $RelayDir '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) {
        $PythonExe = $venv
    } else {
        $found = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $found) { throw 'No python.exe found. Create relay\.venv or pass -PythonExe with the full path.' }
        $PythonExe = $found.Source
    }
}
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "PythonExe '$PythonExe' does not exist." }

$action    = New-ScheduledTaskAction -Execute $PythonExe -Argument '-m relay.worker' -WorkingDirectory $RelayDir
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $RunAs -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'relay worker: drains deliveries, restarts itself after a failure'

$limit = if ($task.Settings.ExecutionTimeLimit -eq 'PT0S') { 'unlimited (PT0S)' } else { $task.Settings.ExecutionTimeLimit }
Write-Output 'TASK DEFINITION'
Write-Output ("TaskName: {0}" -f $TaskName)
Write-Output ("Trigger: at startup ({0})" -f $task.Triggers[0].CimClass.CimClassName)
Write-Output ("RunAs: {0}" -f $task.Principal.UserId)
Write-Output ("LogonType: {0}" -f $task.Principal.LogonType)
Write-Output ("RunLevel: {0}" -f $task.Principal.RunLevel)
Write-Output ("Execute: {0}" -f $task.Actions[0].Execute)
Write-Output ("Arguments: {0}" -f $task.Actions[0].Arguments)
Write-Output ("WorkingDirectory: {0}" -f $task.Actions[0].WorkingDirectory)
Write-Output ("RestartCount: {0}" -f $task.Settings.RestartCount)
Write-Output ("RestartInterval: {0}" -f $task.Settings.RestartInterval)
Write-Output ("ExecutionTimeLimit: {0}" -f $limit)
Write-Output ("MultipleInstances: {0}" -f $task.Settings.MultipleInstances)
Write-Output ("StartWhenAvailable: {0}" -f $task.Settings.StartWhenAvailable)

foreach ($name in 'DATABASE_URL', 'RELAY_DB_PATH', 'RELAY_CONFIG') {
    $set = -not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name, 'Machine'))
    Write-Output ("Machine environment {0}: {1}" -f $name, $(if ($set) { 'set' } else { 'NOT set' }))
}

if ($PSCmdlet.ShouldProcess($TaskName, 'register the relay worker scheduled task')) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw 'Registering a task that runs as SYSTEM needs an elevated PowerShell. Re-run as administrator.' }
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Output ("Registered task {0}." -f $TaskName)
    if (-not $NoStart) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Output ("Started task {0}." -f $TaskName)
    }
}
