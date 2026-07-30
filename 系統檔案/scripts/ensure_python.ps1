[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputFile
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PythonVersion = "3.12.10"
$PythonInstallerUrl = (
    "https://www.python.org/ftp/python/$PythonVersion/" +
    "python-$PythonVersion-amd64.exe"
)
$PythonInstallerPath = Join-Path -Path ([IO.Path]::GetTempPath()) `
    -ChildPath "preopen-python-$PythonVersion-amd64-$PID.exe"
$PythonProbe = (
    "import sys,struct;" +
    "ok=sys.version_info[:2]==(3,12) and struct.calcsize('P')*8==64;" +
    "print(sys.executable if ok else '');" +
    "raise SystemExit(0 if ok else 3)"
)

function Test-TruthyEnvironmentValue {
    param([string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name)
    return $value -in @("1", "true", "TRUE", "yes", "YES")
}

function Invoke-PythonProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [string[]]$PrefixArguments = @()
    )

    if ([string]::IsNullOrWhiteSpace($Executable)) {
        return $null
    }

    $process = $null
    try {
        $argumentParts = @($PrefixArguments) + @(
            "-c",
            ('"{0}"' -f $PythonProbe.Replace('"', '\"'))
        )
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $Executable
        $startInfo.Arguments = ($argumentParts -join " ")
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            return $null
        }
        if (-not $process.WaitForExit(15000)) {
            try {
                $process.Kill()
            }
            catch {
                # Best-effort cleanup of a hung Store alias or probe.
            }
            return $null
        }

        $stdout = $process.StandardOutput.ReadToEnd().Trim()
        if ($process.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($stdout)) {
            return $null
        }

        $resolved = [IO.Path]::GetFullPath($stdout)
        if (-not [IO.Path]::IsPathRooted($resolved)) {
            return $null
        }
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            return $null
        }
        return $resolved
    }
    catch {
        return $null
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
    }
}

function Find-Python312 {
    $pyLauncher = Get-Command "py.exe" -CommandType Application `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $pyLauncher) {
        $found = Invoke-PythonProbe -Executable $pyLauncher.Source `
            -PrefixArguments @("-3.12")
        if ($null -ne $found) {
            return $found
        }
    }

    $pathPython = Get-Command "python.exe" -CommandType Application `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $pathPython) {
        $found = Invoke-PythonProbe -Executable $pathPython.Source
        if ($null -ne $found) {
            return $found
        }
    }

    $knownCandidates = @(
        (Join-Path -Path $env:LOCALAPPDATA `
            -ChildPath "Programs\Python\Python312\python.exe"),
        (Join-Path -Path $env:ProgramFiles `
            -ChildPath "Python312\python.exe")
    )
    if (${env:ProgramFiles(x86)}) {
        $knownCandidates += Join-Path -Path ${env:ProgramFiles(x86)} `
            -ChildPath "Python312\python.exe"
    }

    foreach ($candidate in ($knownCandidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $found = Invoke-PythonProbe -Executable $candidate
        if ($null -ne $found) {
            return $found
        }
    }

    return $null
}

function Invoke-HiddenProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string]$Arguments,

        [int]$TimeoutMilliseconds = 900000
    )

    $process = $null
    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $Executable
        $startInfo.Arguments = $Arguments
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            return 1
        }
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            try {
                $process.Kill()
            }
            catch {
                # The caller will report a timeout failure.
            }
            return 1460
        }
        return $process.ExitCode
    }
    catch {
        return 1
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
    }
}

function Install-WithWinget {
    $winget = Get-Command "winget.exe" -CommandType Application `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $winget) {
        Write-Host "[Python] winget is unavailable; trying python.org."
        return $false
    }

    Write-Host "[Python] Installing Python 3.12 with winget (silent) ..."
    $arguments = (
        "install -e --id Python.Python.3.12 --silent " +
        "--accept-package-agreements --accept-source-agreements " +
        "--disable-interactivity --scope user"
    )
    $exitCode = Invoke-HiddenProcess -Executable $winget.Source `
        -Arguments $arguments
    if ($exitCode -ne 0) {
        Write-Host (
            "[Python] winget failed with exit code $exitCode; " +
            "trying python.org."
        )
        return $false
    }
    return $true
}

function Install-WithOfficialInstaller {
    Write-Output (
        "[Python] Downloading the official Python $PythonVersion " +
        "64-bit installer ..."
    )
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $PythonInstallerUrl `
        -OutFile $PythonInstallerPath -TimeoutSec 180

    $signature = Get-AuthenticodeSignature -FilePath $PythonInstallerPath
    if (
        $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid `
        -or $null -eq $signature.SignerCertificate `
        -or $signature.SignerCertificate.Subject -notmatch "Python Software Foundation"
    ) {
        throw "The downloaded Python installer signature is not trusted."
    }

    Write-Output "[Python] Running the official per-user installer (silent) ..."
    $arguments = "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0"
    $exitCode = Invoke-HiddenProcess -Executable $PythonInstallerPath `
        -Arguments $arguments
    if ($exitCode -ne 0) {
        throw "The official Python installer failed with exit code $exitCode."
    }
}

try {
    $outputParent = Split-Path -Parent ([IO.Path]::GetFullPath($OutputFile))
    if (-not (Test-Path -LiteralPath $outputParent -PathType Container)) {
        throw "The Python result folder does not exist: $outputParent"
    }
    Remove-Item -LiteralPath $OutputFile -Force -ErrorAction SilentlyContinue

    $pythonPath = Find-Python312
    if ($null -ne $pythonPath) {
        Write-Output "[Python] Found 64-bit Python 3.12: $pythonPath"
        [IO.File]::WriteAllText(
            $OutputFile,
            $pythonPath,
            (New-Object Text.UTF8Encoding($false))
        )
        exit 0
    }

    if (
        (Test-TruthyEnvironmentValue -Name "INSTALL_DRY_RUN") -or
        (Test-TruthyEnvironmentValue -Name "INSTALL_SKIP_PYTHON_INSTALL")
    ) {
        throw (
            "Python 3.12 was not found and automatic installation is " +
            "disabled for this test run."
        )
    }

    [void](Install-WithWinget)
    $pythonPath = Find-Python312

    if ($null -eq $pythonPath) {
        try {
            Install-WithOfficialInstaller
        }
        catch {
            Write-Output "[Python] Official installer failed: $($_.Exception.Message)"
        }
        $pythonPath = Find-Python312
    }

    if ($null -eq $pythonPath) {
        throw "Please install Python 3.12 first, then run the launcher again."
    }

    Write-Output "[Python] Python 3.12 is ready: $pythonPath"
    [IO.File]::WriteAllText(
        $OutputFile,
        $pythonPath,
        (New-Object Text.UTF8Encoding($false))
    )
    exit 0
}
catch {
    Write-Error (
        "[FAILED] Python 3.12 setup failed. " +
        "Please install Python 3.12 first. $($_.Exception.Message)"
    )
    exit 1
}
finally {
    if (Test-Path -LiteralPath $PythonInstallerPath -PathType Leaf) {
        Remove-Item -LiteralPath $PythonInstallerPath -Force `
            -ErrorAction SilentlyContinue
    }
}
