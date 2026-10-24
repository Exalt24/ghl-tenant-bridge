<#
.SYNOPSIS
  Runs the relay worker as a Windows scheduled task that starts at boot and restarts itself. No third party binary.

.DESCRIPTION
  Registers one task with Windows Task Scheduler:
    - trigger: at startup
    - principal: SYSTEM (a service account), so it runs whether or not anyone is logged on, highest run level
    - action: <python> -m relay.worker, working directory = the relay folder
    - on failure: restart every 1 minute, up to 999 times. Task Scheduler applies this only when it cannot LAUNCH the action
      (observed 2026-10-24: a launch failure was retried every minute, a process that started and exited non-zero was not)
    - a second trigger, once from now and repeated every 1 minute indefinitely: with one instance at a time it does nothing while
      the worker runs, and starts a new worker within a minute when it has died. This is what restarts a killed or crashed worker.
    - no execution time limit (the Task Scheduler default of 3 days would stop the worker), one instance at a time

  The worker reads its settings from environment variables (DATABASE_URL or RELAY_DB_PATH, RELAY_CONFIG, and every secret the
  config names). A SYSTEM task sees MACHINE level variables, so set them first from an elevated PowerShell with
  [Environment]::SetEnvironmentVariable('NAME', 'value', 'Machine'). This script never reads, prints or stores a value: it only
  says whether each name is set.

  -RunAs CurrentUser is the per-user mode, for a machine where no elevated shell is at hand (a developer PC, a check run):
    - trigger: at logon of the current user, plus the immediate start below
    - principal: the current user, logon type Interactive (no stored password), run level Limited, so registering it needs no
      elevation. It runs only while that user is logged on. S4U ("whether or not logged on") was tried on a non-elevated shell and
      Windows refused it with Access is denied, because it needs the batch logon right.
    - an Interactive task opens a window for a console program, so without -PythonExe the default is pythonw.exe from the same
      folder as python.exe (no window, no console)
    - the rest of the definition is the same: action, restart on failure every 1 minute up to 999 times, the second trigger that
      repeats every 1 minute, no time limit, one instance
    - the worker sees the current user's USER level environment variables (set them with
      [Environment]::SetEnvironmentVariable('NAME', 'value', 'User')), and nothing is read from the machine level
  It only runs while that user's tasks can run, so it is not a replacement for the SYSTEM task on a server.

  -WhatIf prints the exact task definition (built as real ScheduledTask objects, so a wrong parameter fails here) and registers
  nothing. Without -WhatIf the SYSTEM, LOCAL SERVICE and NETWORK SERVICE accounts must run elevated; CurrentUser does not.

.EXAMPLE
  .\install-service.ps1 -WhatIf
  .\install-service.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = 'RelayWorker',
    [string]$RelayDir = '',
    [string]$PythonExe = '',
    [ValidateSet('SYSTEM', 'LOCAL SERVICE', 'NETWORK SERVICE', 'CurrentUser')]
    [string]$RunAs = 'SYSTEM',
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

$perUser = ($RunAs -eq 'CurrentUser')
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
    if ($perUser) {
        # an Interactive task opens a console window for python.exe and takes the foreground, so the default is the windowless twin
        $windowless = Join-Path (Split-Path -Parent $PythonExe) 'pythonw.exe'
        if (Test-Path -LiteralPath $windowless) { $PythonExe = $windowless }
    }
}
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "PythonExe '$PythonExe' does not exist." }

$userId = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
$action  = New-ScheduledTaskAction -Execute $PythonExe -Argument '-m relay.worker' -WorkingDirectory $RelayDir
if ($perUser) {
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $userId
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
} else {
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $RunAs -LogonType ServiceAccount -RunLevel Highest
}
# Restart on failure does not fire for a worker that started and then died (see the header), so a second, time based trigger fires
# every minute from now on: MultipleInstances IgnoreNew makes it a no-op while the worker runs and a restart within a minute once it
# has died. (A repetition set on the logon or boot trigger itself only counts from the next logon or boot, not from registration.)
# With -NoStart the repeat begins 10 minutes from now, so registering does not start the worker behind the operator's back.
$repeatFrom = if ($NoStart) { (Get-Date).AddMinutes(10) } else { Get-Date }
$repeat = New-ScheduledTaskTrigger -Once -At $repeatFrom -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings  = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $action -Trigger @($trigger, $repeat) -Principal $principal -Settings $settings `
    -Description 'relay worker: drains deliveries, restarts itself after a failure'

$limit = if ($task.Settings.ExecutionTimeLimit -eq 'PT0S') { 'unlimited (PT0S)' } else { $task.Settings.ExecutionTimeLimit }
Write-Output 'TASK DEFINITION'
Write-Output ("TaskName: {0}" -f $TaskName)
if ($perUser) {
    Write-Output ("Trigger: at logon of {0} ({1})" -f $userId, $task.Triggers[0].CimClass.CimClassName)
} else {
    Write-Output ("Trigger: at startup ({0})" -f $task.Triggers[0].CimClass.CimClassName)
}
Write-Output ("RepeatTrigger: {0}" -f $task.Triggers[1].CimClass.CimClassName)
Write-Output ("RepeatInterval: {0}" -f $task.Triggers[1].Repetition.Interval)
Write-Output ("RepeatStart: {0}" -f ([datetime]$task.Triggers[1].StartBoundary).ToString('yyyy-MM-ddTHH:mm:ss'))
Write-Output ("RepeatDuration: {0}" -f $(if ([string]::IsNullOrEmpty($task.Triggers[1].Repetition.Duration)) { 'indefinitely' } else { $task.Triggers[1].Repetition.Duration }))
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

$scope = if ($perUser) { 'User' } else { 'Machine' }
foreach ($name in 'DATABASE_URL', 'RELAY_DB_PATH', 'RELAY_CONFIG') {
    $set = -not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name, $scope))
    Write-Output ("{0} environment {1}: {2}" -f $scope, $name, $(if ($set) { 'set' } else { 'NOT set' }))
}

if ($PSCmdlet.ShouldProcess($TaskName, 'register the relay worker scheduled task')) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin -and -not $perUser) { throw 'Registering a task that runs as SYSTEM needs an elevated PowerShell. Re-run as administrator.' }
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Output ("Registered task {0}." -f $TaskName)
    if (-not $NoStart) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Output ("Started task {0}." -f $TaskName)
    }
}
