# fake-trial-poke project config for gs-app-pack
# Build: C:\Users\User\gs-app-pack\pack.ps1 -Clean
# Release: C:\Users\User\gs-app-pack\pack.ps1 -Tag v0.1.0 -Clean

$AppName      = "Fake Trial Poke"
$AppVersion   = "0.1.0"
$AppId        = "64B1E22E-7CEC-4892-B65D-79C6D750DD16"
$AppExe       = "fake-trial-poke"
$AppPublisher = "gsinvest"
$AppUrl       = "https://github.com/gsinvest017-ai/fake-trial-poke"

$ServerMode   = "function"
$ServerModule = "service"
$ServerFunc   = "main"
$ServerApp    = ""
$ServerCmd    = ""
$ServerHost   = "127.0.0.1"
$ServerPort   = 8900

$ConfigModule = ""
$ConfigFunc   = ""

$WinTitle     = "假試撮盤前監控"
$WinWidth     = 1440
$WinHeight    = 900
$WinBgColor   = "#07060a"

$DwmCaption   = 0x000A0607
$DwmBorder    = 0x0037AFD4
$DwmText      = 0x0095D1E8

$IconBg       = "#07060a"
$IconRing     = "#d4af37"
$IconSize     = 32

$PyiAddData = @(
    "系統檔案\ui;ui",
    "系統檔案\data\holidays.txt;data",
    "系統檔案\.env.example;."
)

$PyiExtraArgs = @(
    "--paths=系統檔案",
    "--collect-all=shioaji",
    "--collect-all=pysolace",
    "--collect-all=tzdata",

    # tkinter is stdlib, but PyInstaller only bundles it when something imports
    # it at module scope -- and keyguard's licence gate imports it lazily, so it
    # would silently fall back to a bare MessageBox. Collecting it explicitly is
    # what makes the refusal a proper window the customer can copy from.
    # Verify with `keyguard.appgate.AppGate.refusal_ui()` == "window".
    "--collect-all=tkinter"
)

$PythonExe = "C:\Users\User\fake-trial-poke\系統檔案\.venv\Scripts\python.exe"
$env:PATH = "$(Split-Path -Parent $PythonExe);$env:PATH"

# Missing Keyguard must fail the build; the runtime fallback is intentionally
# fail-open so a packaging mistake cannot lock paying customers out.
$RequireNonEditable = @("keyguard")

$PostBuildCheck = @'
{python} -m keyguard.packagecheck '{dist}' --email-env FAKE_TRIAL_POKE_LICENCE_EMAIL --require-console-output --require-window
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
{python} 'C:\Users\User\gs-app-pack\scripts\smoke_launch.py' '{dist}' --timeout 90
'@

$InstallerRequiresGh = $false
