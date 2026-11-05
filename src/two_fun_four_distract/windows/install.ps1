# Sets up the 2fun4distract agent for this user, starting it now and at each sign-in.
# It needs no administrator rights. Run it with -Uninstall to take it all away again.
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"

$Server = "__SERVER__"
$Directory = Join-Path $env:LOCALAPPDATA "2fun4distract"
$Extension = Join-Path $Directory "extension"
$Agent = Join-Path $Directory "agent.ps1"
$PidFile = Join-Path $Directory "agent.pid"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValue = "2fun4distract agent"

function Stop-Agent {
    if (-not (Test-Path $PidFile)) { return }
    $running = Get-Process -Id ([int](Get-Content $PidFile)) -ErrorAction SilentlyContinue
    # Only if it is still ours: Windows reuses process ids.
    if ($null -ne $running -and $running.ProcessName -eq "powershell") {
        Stop-Process -Id $running.Id -Force
        $running.WaitForExit(5000) | Out-Null
    }
    Remove-Item $PidFile
}

if ($Uninstall) {
    Stop-Agent
    Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue
    if (Test-Path $Directory) { Remove-Item $Directory -Recurse -Force }
    "Removed. Remove the 2fun4distract extension from your browser's extensions page as well."
    return
}

Stop-Agent
New-Item -ItemType Directory -Force -Path $Extension | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri "$Server/setup/agent.ps1" -OutFile $Agent
Invoke-WebRequest -UseBasicParsing -Uri "$Server/setup/manifest.json" -OutFile (Join-Path $Extension "manifest.json")
Invoke-WebRequest -UseBasicParsing -Uri "$Server/setup/background.js" -OutFile (Join-Path $Extension "background.js")

$arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Agent`""
Set-ItemProperty -Path $RunKey -Name $RunValue -Value "powershell.exe $arguments"
Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WindowStyle Hidden

"The agent is running, and will start again at each sign-in."
""
"To split browser time by site, add the extension to Chrome or Edge:"
"  1. Open chrome://extensions (edge://extensions in Edge)."
"  2. Turn on Developer mode."
"  3. Click Load unpacked and choose this folder:"
"     $Extension"
