param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Title,
    [Parameter(Mandatory = $true, Position = 1)]
    [string]$Message
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Icon = New-Object System.Windows.Forms.NotifyIcon
$Icon.Icon = [System.Drawing.SystemIcons]::Information
$Icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
$Icon.BalloonTipTitle = $Title
$Icon.BalloonTipText = $Message
$Icon.Visible = $true
$Icon.ShowBalloonTip(10000)
try {
    $Until = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $Until) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 200
    }
} finally {
    $Icon.Visible = $false
    $Icon.Dispose()
}
