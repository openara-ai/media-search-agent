// Media Search Agent — Windows system tray controller
//
// Mirrors the macOS Swift menu bar app (installer/macos/launcher_app/main.swift).
// Publishes as a single self-contained exe — no .NET redistributable required.
//
// Build (done by build-bundle.sh — no Visual Studio required):
//   dotnet publish MediaSearchAgentTray.csproj -r win-x64 --self-contained true
//     -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true
//     -c Release -o <out-dir>
//
// Installed to: %LOCALAPPDATA%\MediaSearchAgent\bin\MediaSearchAgentTray.exe
// Path sidecar: %LOCALAPPDATA%\MediaSearchAgent\bin\msa-paths.env
//   (written by install.ps1; overrides default path derivation for custom -AppDir / -DataDir installs)

using System.Diagnostics;
using System.Net.Http;
using System.Reflection;
using System.Windows.Forms;

internal static class Program
{
    [STAThread]
    static void Main()
    {
        using var mutex = new System.Threading.Mutex(true, "Global\\MediaSearchAgent-Tray", out bool acquired);
        if (!acquired) return;

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new TrayApp());
    }
}

// ── Path resolution ───────────────────────────────────────────────────────────
//
// The tray exe lives at:  AppDir\bin\MediaSearchAgentTray.exe
// AppDir is therefore:    Path.GetFullPath(exeDir + "\..")
//
// install.ps1 writes a sidecar  AppDir\bin\msa-paths.env  when the user chose
// custom -AppDir or -DataDir at install time.  The sidecar is parsed first;
// env vars (set when launched via msa.cmd tray) take highest priority.

internal sealed record MsaPaths(
    string AppDir,
    string DataDir,
    string LogDir,
    string ConfigPath,
    string Launcher,        // AppDir\bin\msa.cmd
    string UninstallScript  // AppDir\repo\uninstall.ps1
);

internal static class PathLoader
{
    internal static MsaPaths Load()
    {
        string exeDir = Path.GetDirectoryName(Environment.ProcessPath
            ?? AppContext.BaseDirectory)!;

        var sc = LoadSidecar(exeDir);

        string Get(string envKey, string fallback) =>
            Environment.GetEnvironmentVariable(envKey)
            ?? sc.GetValueOrDefault(envKey)
            ?? fallback;

        string appDir  = Path.GetFullPath(Path.Combine(exeDir, ".."));
        string dataDir = Get("MSA_DATA_DIR",
            Path.Combine(Environment.GetFolderPath(
                Environment.SpecialFolder.UserProfile), "MediaSearchAgent"));
        string logDir     = Get("MSA_LOG_DIR",     Path.Combine(appDir, "logs"));
        string configPath = Get("MSA_CONFIG_PATH", Path.Combine(dataDir, "config.yaml"));

        return new MsaPaths(
            AppDir:         appDir,
            DataDir:        dataDir,
            LogDir:         logDir,
            ConfigPath:     configPath,
            Launcher:       Path.Combine(appDir, "bin", "msa.cmd"),
            UninstallScript: Path.Combine(appDir, "repo", "uninstall.ps1")
        );
    }

    private static Dictionary<string, string> LoadSidecar(string exeDir)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        string envFile = Path.Combine(exeDir, "msa-paths.env");
        if (!File.Exists(envFile)) return result;

        foreach (var line in File.ReadLines(envFile))
        {
            var t = line.Trim();
            if (t.StartsWith('#') || !t.Contains('=')) continue;
            int eq = t.IndexOf('=');
            result[t[..eq].Trim()] = t[(eq + 1)..].Trim();
        }
        return result;
    }
}

// ── API port resolution ───────────────────────────────────────────────────────

internal static class ConfigParser
{
    internal static int ResolvePort(string configPath, int defaultPort = 8000)
    {
        if (!File.Exists(configPath)) return defaultPort;
        try
        {
            bool inApi = false;
            foreach (var line in File.ReadLines(configPath))
            {
                var t = line.TrimStart();
                if (t.StartsWith("api:")) { inApi = true; continue; }
                if (inApi && line.Length > 0 && line[0] != ' ' && line[0] != '\t') break;
                if (inApi && t.StartsWith("port:") &&
                    int.TryParse(t[5..].Trim(), out int port))
                    return port;
            }
        }
        catch { }
        return defaultPort;
    }
}

// ── Tray application ──────────────────────────────────────────────────────────

internal sealed class TrayApp : ApplicationContext
{
    private const int PollIntervalMs       = 5_000;
    private const int ReadyTimeoutSec      = 90;
    // Require N consecutive failed polls before flipping the tray to Stopped.
    // A single 4 s health timeout is regularly tripped by transient stalls
    // (uvicorn busy on a heavy request, loopback connection reaped, brief GC
    // pause), which used to cause the status to flap between Running/Stopped
    // every 5 s. Hysteresis: trust a single success, but confirm failures.
    private const int FailuresToStopped    = 3;

    private readonly MsaPaths _paths;
    private readonly int      _apiPort;
    private readonly string   _healthUrl;
    private readonly string   _launchUrl;

    private readonly NotifyIcon          _tray;
    private readonly ToolStripMenuItem   _statusItem;
    private readonly ToolStripMenuItem   _stopItem;
    private readonly ToolStripMenuItem   _loginItem;
    private readonly System.Windows.Forms.Timer _pollTimer;
    private readonly HttpClient          _http = new() { Timeout = TimeSpan.FromSeconds(4) };

    private bool _running;
    private int  _consecutiveFailures;
    private bool _pollInFlight;

    internal TrayApp()
    {
        _paths     = PathLoader.Load();
        _apiPort   = ConfigParser.ResolvePort(_paths.ConfigPath);
        _healthUrl = $"http://localhost:{_apiPort}/health";
        _launchUrl = $"http://localhost:{_apiPort}/?launch=1";

        var menu = new ContextMenuStrip();

        menu.Items.Add("Open Media Search", null, (_, _) => OnOpenBrowser());
        menu.Items.Add(new ToolStripSeparator());

        _statusItem = new ToolStripMenuItem("○ Stopped") { Enabled = false };
        menu.Items.Add(_statusItem);
        menu.Items.Add(new ToolStripSeparator());

        _stopItem = new ToolStripMenuItem("Stop Services", null, (_, _) => OnStop())
            { Enabled = false };
        menu.Items.Add(_stopItem);

        menu.Items.Add("Open Command Prompt", null, (_, _) => OnOpenCmd());
        menu.Items.Add(new ToolStripSeparator());

        var more = new ToolStripMenuItem("More");
        more.DropDownItems.Add("View Logs", null, (_, _) => OnViewLogs());

        _loginItem = new ToolStripMenuItem("Start on Login", null, (_, _) => OnToggleLogin())
            { Checked = IsLoginEnabled() };
        more.DropDownItems.Add(_loginItem);
        more.DropDownItems.Add(new ToolStripSeparator());
        more.DropDownItems.Add("Uninstall\u2026", null, (_, _) => OnUninstall());
        more.DropDownItems.Add(new ToolStripSeparator());
        more.DropDownItems.Add(
            new ToolStripMenuItem($"Version {AppVersion()}") { Enabled = false });

        menu.Items.Add(more);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Quit", null, (_, _) => OnQuit());

        _tray = new NotifyIcon
        {
            Icon             = LoadIcon(),
            Text             = "Media Search Agent",
            Visible          = true,
            ContextMenuStrip = menu
        };

        _pollTimer = new System.Windows.Forms.Timer { Interval = PollIntervalMs };
        _pollTimer.Tick += async (_, _) => await PollAsync();
        _pollTimer.Start();

        // Auto-start API on first launch, then open browser — fire-and-forget.
        _ = LaunchSequenceAsync();
    }

    // ── Health polling ────────────────────────────────────────────────────────

    private async Task PollAsync()
    {
        // System.Windows.Forms.Timer ticks on the UI thread, but the await below
        // yields control back to the message loop — without this guard a slow
        // poll can overlap the next tick and double-increment _consecutiveFailures
        // or apply stale statuses out of order. Single-threaded UI means a plain
        // bool is enough; no Interlocked needed.
        if (_pollInFlight) return;
        _pollInFlight = true;
        try
        {
            bool healthy = await IsHealthyAsync();
            if (healthy)
            {
                _consecutiveFailures = 0;
                ApplyStatus(true);
            }
            else if (++_consecutiveFailures >= FailuresToStopped)
            {
                ApplyStatus(false);
            }
        }
        finally
        {
            _pollInFlight = false;
        }
    }

    private async Task<bool> IsHealthyAsync()
    {
        try
        {
            // ResponseHeadersRead does not buffer the body, so the response must
            // be disposed promptly to release the underlying socket — otherwise
            // long-running tray sessions can exhaust connections and start
            // returning false health checks.
            using var resp = await _http.GetAsync(
                _healthUrl, HttpCompletionOption.ResponseHeadersRead);
            return resp.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    private void ApplyStatus(bool running)
    {
        if (_running == running) return;
        _running = running;
        _statusItem.Text  = running
            ? $"● Running \u2014 http://localhost:{_apiPort}"
            : "○ Stopped";
        _stopItem.Enabled = running;
        _tray.Text        = running
            ? "Media Search Agent \u2014 Running"
            : "Media Search Agent \u2014 Stopped";
    }

    // ── Launch sequence (on tray startup) ────────────────────────────────────

    private async Task LaunchSequenceAsync()
    {
        bool alreadyRunning = await IsHealthyAsync();
        ApplyStatus(alreadyRunning);

        if (alreadyRunning) return;

        _tray.BalloonTipTitle = "Media Search Agent";
        _tray.BalloonTipText  = "Starting up\u2026 your browser will open shortly.";
        _tray.BalloonTipIcon  = ToolTipIcon.Info;
        _tray.ShowBalloonTip(4_000);

        RunLauncher("api start");
        await WaitForReadyAsync();
        OpenUrl(_launchUrl);
    }

    private async Task WaitForReadyAsync()
    {
        for (int i = 0; i < ReadyTimeoutSec; i++)
        {
            await Task.Delay(1_000);
            if (await IsHealthyAsync()) { ApplyStatus(true); return; }
        }
    }

    // ── Menu actions ──────────────────────────────────────────────────────────

    private async void OnOpenBrowser()
    {
        if (_running) { OpenUrl(_launchUrl); return; }
        RunLauncher("api start");
        await WaitForReadyAsync();
        OpenUrl(_launchUrl);
    }

    private void OnStop() => RunLauncher("api stop");

    private void OnOpenCmd()
    {
        string binDir = Path.Combine(_paths.AppDir, "bin");
        string path   = Environment.GetEnvironmentVariable("PATH") ?? "";
        var psi = new ProcessStartInfo("cmd.exe")
        {
            UseShellExecute = false,
        };
        psi.EnvironmentVariables["PATH"]             = $"{binDir};{path}";
        psi.EnvironmentVariables["MSA_DATA_DIR"]     = _paths.DataDir;
        psi.EnvironmentVariables["MSA_CONFIG_PATH"]  = _paths.ConfigPath;
        psi.EnvironmentVariables["MSA_LOG_DIR"]      = _paths.LogDir;
        Process.Start(psi);
    }

    private void OnViewLogs()
    {
        if (Directory.Exists(_paths.LogDir))
            OpenUrl(_paths.LogDir);
    }

    private void OnToggleLogin()
    {
        bool enabled = IsLoginEnabled();
        if (IsTaskSchedulerTaskPresent())
        {
            Schtasks(enabled ? "/Change /TN MediaSearchAgent /DISABLE"
                             : "/Change /TN MediaSearchAgent /ENABLE");
        }
        else
        {
            ToggleRunKey(!enabled);
        }
        _loginItem.Checked = !enabled;
    }

    private void OnUninstall()
    {
        var answer = MessageBox.Show(
            "This will stop all services, remove the app, and delete the Python " +
            "environment (~500 MB).\n\n" +
            "Your index, thumbnails, and config will not be removed unless you " +
            "choose to delete them.",
            "Uninstall Media Search Agent?",
            MessageBoxButtons.OKCancel,
            MessageBoxIcon.Warning,
            MessageBoxDefaultButton.Button2);

        if (answer != DialogResult.OK) return;

        if (!File.Exists(_paths.UninstallScript))
        {
            MessageBox.Show(
                "Uninstall script not found. Please run uninstall.ps1 manually.",
                "Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        Process.Start(new ProcessStartInfo
        {
            FileName  = "powershell.exe",
            Arguments = $"-ExecutionPolicy Bypass -File \"{_paths.UninstallScript}\"" +
                        $" -AppDir \"{_paths.AppDir}\" -DataDir \"{_paths.DataDir}\"",
            UseShellExecute = true
        });

        ExitThread();
    }

    private void OnQuit()
    {
        RunLauncher("api stop");
        ExitThread();
    }

    // ── Task Scheduler helpers ────────────────────────────────────────────────

    private const string RunKeyPath =
        @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string AutoStartName = "MediaSearchAgent";

    private static bool IsLoginEnabled()
    {
        // Task Scheduler: task present and not disabled.
        if (IsTaskSchedulerTaskPresent())
        {
            try
            {
                var psi = new ProcessStartInfo("schtasks.exe",
                    $"/Query /TN {AutoStartName} /FO CSV /NH")
                {
                    UseShellExecute        = false,
                    RedirectStandardOutput = true,
                    CreateNoWindow         = true
                };
                using var p = Process.Start(psi)!;
                string output = p.StandardOutput.ReadToEnd();
                p.WaitForExit();
                return p.ExitCode == 0
                    && (output.Contains("Ready") || output.Contains("Running"));
            }
            catch { return false; }
        }

        // Run-key fallback: entry present means enabled.
        try
        {
            using var key = Microsoft.Win32.Registry.CurrentUser
                .OpenSubKey(RunKeyPath);
            return key?.GetValue(AutoStartName) != null;
        }
        catch { return false; }
    }

    private static bool IsTaskSchedulerTaskPresent()
    {
        try
        {
            var psi = new ProcessStartInfo("schtasks.exe",
                $"/Query /TN {AutoStartName} /FO CSV /NH")
            {
                UseShellExecute        = false,
                RedirectStandardOutput = true,
                CreateNoWindow         = true
            };
            using var p = Process.Start(psi)!;
            p.StandardOutput.ReadToEnd();
            p.WaitForExit();
            return p.ExitCode == 0;
        }
        catch { return false; }
    }

    private static void ToggleRunKey(bool enable)
    {
        try
        {
            if (enable)
            {
                string exePath = Environment.ProcessPath
                    ?? AppContext.BaseDirectory;
                using var key = Microsoft.Win32.Registry.CurrentUser
                    .OpenSubKey(RunKeyPath, writable: true)
                    ?? Microsoft.Win32.Registry.CurrentUser
                    .CreateSubKey(RunKeyPath);
                key.SetValue(AutoStartName, $"\"{exePath}\"");
            }
            else
            {
                using var key = Microsoft.Win32.Registry.CurrentUser
                    .OpenSubKey(RunKeyPath, writable: true);
                key?.DeleteValue(AutoStartName, throwOnMissingValue: false);
            }
        }
        catch { }
    }

    private static void Schtasks(string args)
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo("schtasks.exe", args)
                { UseShellExecute = false, CreateNoWindow = true })!;
            p.WaitForExit();
        }
        catch { }
    }

    // ── Shell helpers ─────────────────────────────────────────────────────────

    private void RunLauncher(string args)
    {
        if (!File.Exists(_paths.Launcher)) return;
        Process.Start(new ProcessStartInfo("cmd.exe",
            $"/c \"{_paths.Launcher}\" {args}")
            { UseShellExecute = false, CreateNoWindow = true });
    }

    private static void OpenUrl(string target) =>
        Process.Start(new ProcessStartInfo(target) { UseShellExecute = true });

    // ── Icon + version ────────────────────────────────────────────────────────

    private static System.Drawing.Icon LoadIcon()
    {
        using var stream = Assembly.GetExecutingAssembly()
            .GetManifestResourceStream("icon.ico");
        if (stream is not null) return new System.Drawing.Icon(stream);
        return System.Drawing.SystemIcons.Application;
    }

    private static string AppVersion() =>
        Assembly.GetExecutingAssembly()
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()
            ?.InformationalVersion.Split('+')[0] ?? "unknown";

    // ── Cleanup ───────────────────────────────────────────────────────────────

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _pollTimer.Dispose();
            _tray.Dispose();
            _http.Dispose();
        }
        base.Dispose(disposing);
    }
}
