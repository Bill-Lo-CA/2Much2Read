# 2fun4distract agent: what is showing on each monitor, every 15 seconds.
# It sends process names and how much of each monitor they cover. Window titles are never read.

$ErrorActionPreference = "Stop"

$Server = "__SERVER__"
$Directory = Join-Path $env:LOCALAPPDATA "2fun4distract"
$Pending = Join-Path $Directory "pending"
$LogFile = Join-Path $Directory "agent.log"
$IntervalSeconds = 15
$SamplesPerBatch = 4
# Two weeks of batches, after which the receiver would not count them anyway.
$MaxPendingFiles = 20160
$MaxLogBytes = 1048576

$source = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public class ScreenEntry
{
    public int Monitor;
    public uint ProcessId;
    public double Percent;
}

public class ScreenSample
{
    public uint ForegroundProcessId;
    public long IdleSeconds;
    public List<ScreenEntry> Entries = new List<ScreenEntry>();
}

public static class ScreenSampler
{
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    public struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }

    [StructLayout(LayoutKind.Sequential)]
    public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }

    delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr data);
    delegate bool MonitorEnumProc(IntPtr monitor, IntPtr hdc, IntPtr clip, IntPtr data);

    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc callback, IntPtr data);
    [DllImport("user32.dll")] static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc callback, IntPtr data);
    [DllImport("user32.dll", EntryPoint = "GetMonitorInfoW")] static extern bool GetMonitorInfo(IntPtr monitor, ref MONITORINFO info);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "GetClassNameW", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder name, int capacity);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO info);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out int value, int size);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out RECT value, int size);

    const int GWL_EXSTYLE = -20;
    const int WS_EX_TRANSPARENT = 0x20;
    const int WS_EX_TOOLWINDOW = 0x80;
    const int WS_EX_NOACTIVATE = 0x08000000;
    const int DWMWA_EXTENDED_FRAME_BOUNDS = 9;
    const int DWMWA_CLOAKED = 14;
    const int GridColumns = 96;
    const int GridRows = 54;

    static readonly string[] ShellClasses = { "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd" };

    struct Shown
    {
        public RECT Bounds;
        public uint ProcessId;
    }

    public static void MakeDpiAware()
    {
        // Without this, coordinates on a monitor scaled differently from the primary one are wrong.
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); }
        catch (EntryPointNotFoundException) { }
    }

    // Front to back, as EnumWindows gives them: only windows a person could actually see.
    static List<Shown> ShownWindows()
    {
        List<Shown> windows = new List<Shown>();
        EnumWindows(delegate(IntPtr hwnd, IntPtr data)
        {
            if (!IsWindowVisible(hwnd) || IsIconic(hwnd)) return true;
            int cloaked;
            if (DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, out cloaked, sizeof(int)) == 0 && cloaked != 0) return true;
            if ((GetWindowLong(hwnd, GWL_EXSTYLE) & (WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)) != 0) return true;
            StringBuilder name = new StringBuilder(64);
            GetClassName(hwnd, name, name.Capacity);
            if (Array.IndexOf(ShellClasses, name.ToString()) >= 0) return true;
            RECT bounds;
            if (DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, out bounds, Marshal.SizeOf(typeof(RECT))) != 0
                && !GetWindowRect(hwnd, out bounds)) return true;
            if (bounds.Right <= bounds.Left || bounds.Bottom <= bounds.Top) return true;
            Shown shown = new Shown();
            shown.Bounds = bounds;
            GetWindowThreadProcessId(hwnd, out shown.ProcessId);
            windows.Add(shown);
            return true;
        }, IntPtr.Zero);
        return windows;
    }

    public static ScreenSample Take()
    {
        ScreenSample sample = new ScreenSample();
        List<MONITORINFO> monitors = new List<MONITORINFO>();
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, delegate(IntPtr monitor, IntPtr hdc, IntPtr clip, IntPtr data)
        {
            MONITORINFO info = new MONITORINFO();
            info.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
            if (GetMonitorInfo(monitor, ref info)) monitors.Add(info);
            return true;
        }, IntPtr.Zero);

        List<Shown> windows = ShownWindows();
        GetWindowThreadProcessId(GetForegroundWindow(), out sample.ForegroundProcessId);

        for (int index = 0; index < monitors.Count; index++)
        {
            RECT area = monitors[index].rcMonitor;
            int width = area.Right - area.Left;
            int height = area.Bottom - area.Top;
            Dictionary<uint, int> cells = new Dictionary<uint, int>();
            for (int row = 0; row < GridRows; row++)
            {
                int y = area.Top + (2 * row + 1) * height / (2 * GridRows);
                for (int column = 0; column < GridColumns; column++)
                {
                    int x = area.Left + (2 * column + 1) * width / (2 * GridColumns);
                    foreach (Shown window in windows)
                    {
                        if (x >= window.Bounds.Left && x < window.Bounds.Right && y >= window.Bounds.Top && y < window.Bounds.Bottom)
                        {
                            int count;
                            cells.TryGetValue(window.ProcessId, out count);
                            cells[window.ProcessId] = count + 1;
                            break;
                        }
                    }
                }
            }
            foreach (KeyValuePair<uint, int> pair in cells)
            {
                ScreenEntry entry = new ScreenEntry();
                entry.Monitor = index + 1;
                entry.ProcessId = pair.Key;
                entry.Percent = Math.Round(100.0 * pair.Value / (GridColumns * GridRows), 1);
                sample.Entries.Add(entry);
            }
        }

        LASTINPUTINFO input = new LASTINPUTINFO();
        input.cbSize = (uint)Marshal.SizeOf(typeof(LASTINPUTINFO));
        if (GetLastInputInfo(ref input)) sample.IdleSeconds = unchecked((uint)Environment.TickCount - input.dwTime) / 1000;
        return sample;
    }
}
'@

function Write-Log([string]$Message) {
    try {
        if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt $MaxLogBytes) { Remove-Item $LogFile }
        Add-Content -Path $LogFile -Value "$([DateTime]::UtcNow.ToString('s'))Z $Message"
    } catch { }
}

function Get-ProcessNameOrNull([uint32]$ProcessId) {
    try { return (Get-Process -Id $ProcessId).ProcessName } catch { return $null }
}

function New-Sample {
    $taken = [ScreenSampler]::Take()
    $foregroundName = $null
    if ($taken.ForegroundProcessId -ne 0) { $foregroundName = Get-ProcessNameOrNull $taken.ForegroundProcessId }
    $entries = @(foreach ($entry in $taken.Entries) {
        $name = Get-ProcessNameOrNull $entry.ProcessId
        if ($null -ne $name) {
            @{
                monitor    = $entry.Monitor
                process    = $name
                percent    = $entry.Percent
                foreground = ($entry.ProcessId -eq $taken.ForegroundProcessId)
            }
        }
    })
    return @{
        at           = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
        seconds      = $IntervalSeconds
        idle_seconds = [long]$taken.IdleSeconds
        locked       = (($null -eq $foregroundName) -or ($foregroundName -in @("LockApp", "LogonUI")))
        entries      = $entries
    }
}

function Save-Batch($Samples) {
    $batchId = [Guid]::NewGuid().ToString()
    $body = @{ batch_id = $batchId; samples = @($Samples) } | ConvertTo-Json -Depth 6 -Compress
    # Named by time, so that resending in name order keeps them in the order they were taken.
    $name = "$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))-$batchId.json"
    [IO.File]::WriteAllText((Join-Path $Pending $name), $body, (New-Object Text.UTF8Encoding($false)))
}

function Send-Pending {
    $files = @(Get-ChildItem -Path $Pending -Filter "*.json" | Sort-Object Name)
    if ($files.Count -gt $MaxPendingFiles) {
        $files | Select-Object -First ($files.Count - $MaxPendingFiles) | Remove-Item
        $files = @($files | Select-Object -Last $MaxPendingFiles)
    }
    foreach ($file in $files) {
        try {
            Invoke-RestMethod -Method Post -Uri "$Server/computer/samples" -ContentType "application/json" `
                -Body ([IO.File]::ReadAllBytes($file.FullName)) -TimeoutSec 15 | Out-Null
            Remove-Item $file.FullName
        } catch [System.Net.WebException] {
            $response = $_.Exception.Response
            if ($null -ne $response -and [int]$response.StatusCode -ge 400 -and [int]$response.StatusCode -lt 500) {
                # Refused for what it is, so sending it again would only block every batch behind it.
                Write-Log "dropped $($file.Name): HTTP $([int]$response.StatusCode)"
                Remove-Item $file.FullName
            } else {
                # The receiver or the tailnet is away. Everything stays here until it is back.
                return
            }
        }
    }
}

# One at a time: a second copy would count every second twice.
$instance = New-Object System.Threading.Mutex($false, "Local\2fun4distract-agent")
if (-not $instance.WaitOne(0)) { exit 0 }

New-Item -ItemType Directory -Force -Path $Pending | Out-Null
Set-Content -Path (Join-Path $Directory "agent.pid") -Value $PID
Add-Type -TypeDefinition $source
[ScreenSampler]::MakeDpiAware()

$samples = New-Object System.Collections.ArrayList
while ($true) {
    try {
        [void]$samples.Add((New-Sample))
        if ($samples.Count -ge $SamplesPerBatch) {
            Save-Batch $samples.ToArray()
            $samples.Clear()
            Send-Pending
        }
    } catch {
        # Nothing is to stop the agent for good: it runs unattended, and the next sample may be fine.
        Write-Log "$($_.Exception.Message)"
    }
    Start-Sleep -Seconds $IntervalSeconds
}
