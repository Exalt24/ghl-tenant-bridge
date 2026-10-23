<#
.SYNOPSIS
  Stops and removes the scheduled task that install-service.ps1 registered.

.DESCRIPTION
  -WhatIf says what would be removed and changes nothing. Removing a task that does not exist is not an error.
  A worker process the task started is stopped with the task.

.EXAMPLE
  .\uninstall-service.ps1 -WhatIf
  .\uninstall-service.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = 'RelayWorker'
)

$ErrorActionPreference = 'Stop'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Write-Output ("TaskName: {0}" -f $TaskName)
Write-Output ("Exists: {0}" -f $(if ($existing) { 'yes' } else { 'no' }))
if (-not $existing) {
    Write-Output 'Nothing to remove.'
    return
}
if ($PSCmdlet.ShouldProcess($TaskName, 'stop and unregister the relay worker scheduled task')) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output ("Removed task {0}." -f $TaskName)
}
