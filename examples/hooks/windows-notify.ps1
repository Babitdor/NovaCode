# Nova notification hook (Windows).
# Reads a notification JSON payload from stdin and shows a desktop toast.
# For APPROVAL notifications it also routes you to the terminal where Nova is
# waiting for your y/n — by bringing that terminal window to the foreground.
# Wired via ~/.nova/hooks.json on the "notification" event.
$ErrorActionPreference = 'Stop'
try {
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $n = $raw | ConvertFrom-Json
    # The TUI handles the icon and routes clicks to the originating tab.
    if ($n.desktop_managed -eq $true) { exit 0 }

    # --- Card content (define BEFORE building the toast) ---
    $title   = if ($n.title)   { [string]$n.title }   else { 'Nova' }
    $message = if ($n.message) { [string]$n.message } else { '' }
    $source  = if ($n.source)  { [string]$n.source }  else { '' }
    if ($source) { $title = "Nova - $source : $title" }

    # An approval/interrupt needs a response in the terminal.
    $isApproval = ($n.level -eq 'approval') -or [bool]$n.action_id

    # Use the current installation logo passed by Nova, for every severity.
    $icon = if ($n.icon_path) { [string]$n.icon_path } else {
        Join-Path $env:USERPROFILE '.nova\icons\nova.png'
    }
    $sound = switch ($n.level) {
        'error'    { 'Alarm2' }
        'warning'  { 'Reminder' }
        'approval' { 'Reminder' }
        default    { 'Default' }
    }

    # --- Route to the terminal: bring Nova's console window to the foreground so
    # the y/n prompt is right there. Walk up the process tree from this hook to
    # the hosting terminal (first ancestor that owns a visible window). ---
    function Focus-NovaTerminal {
        try {
            Add-Type -Namespace NovaNative -Name Win -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int n);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool IsIconic(System.IntPtr h);
'@ -ErrorAction SilentlyContinue
        } catch {}
        $cur = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction SilentlyContinue).ParentProcessId
        for ($i = 0; $i -lt 12 -and $cur; $i++) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
            if (-not $p) { break }
            $proc = Get-Process -Id $cur -ErrorAction SilentlyContinue
            if ($proc -and $proc.MainWindowHandle -ne 0) {
                $h = $proc.MainWindowHandle
                try {
                    if ([NovaNative.Win]::IsIconic($h)) { [NovaNative.Win]::ShowWindow($h, 9) | Out-Null } # SW_RESTORE
                    [NovaNative.Win]::SetForegroundWindow($h) | Out-Null
                } catch {}
                return
            }
            $cur = $p.ParentProcessId
            if ($cur -eq 0) { break }
        }
    }
    if ($isApproval) { Focus-NovaTerminal }

    # --- Preferred: BurntToast (rich card). Install once with:
    #   Install-Module -Name BurntToast -Scope CurrentUser -Force
    if (Get-Module -ListAvailable -Name BurntToast) {
        Import-Module BurntToast -ErrorAction Stop
        $params = @{
            Text  = @($title, $message)
            Sound = $sound
        }
        # Approvals are answered in the terminal (already brought to the front
        # above), so no button. Non-approval info offers a jump to the project.
        if (-not $isApproval) {
            $proj = (Get-Location).Path -replace '\\', '/'
            $openUri = [System.Uri]::EscapeUriString("vscode://file/$proj")
            $params['Button'] = (New-BTButton -Content 'Open in VS Code' -Arguments $openUri)
        }
        if (Test-Path -LiteralPath $icon) { $params['AppLogo'] = $icon }
        New-BurntToastNotification @params | Out-Null
        exit 0
    }

    # --- Fallback: native WinRT toast (no module required, Win10/11). ---
    $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]
    $hasIcon = Test-Path -LiteralPath $icon
    $tType = if ($hasIcon) {
        [Windows.UI.Notifications.ToastTemplateType]::ToastImageAndText02
    } else {
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02
    }
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($tType)
    if ($hasIcon) {
        $image = $xml.GetElementsByTagName('image').Item(0)
        $image.SetAttribute('src', ([System.Uri]::new($icon)).AbsoluteUri)
        $image.SetAttribute('alt', 'Nova')
    }
    $texts = $xml.GetElementsByTagName('text')
    $texts.Item(0).AppendChild($xml.CreateTextNode($title)) | Out-Null
    $texts.Item(1).AppendChild($xml.CreateTextNode($message)) | Out-Null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
} catch {
    # A notifier failure must never surface to Nova.
    exit 0
}
