# Claude Insight — installs the /ai-fluency skill into Claude Code (Windows PowerShell)
# and generates your first report right away.
# Usage:  irm https://raw.githubusercontent.com/pablocarbajal/claude-insights/main/install.ps1 | iex
#
# Options, as environment variables:
#   $env:CLAUDE_INSIGHT_REPO = "owner/name"     install from a fork (default: pablocarbajal/claude-insights)
#   $env:CLAUDE_INSIGHT_REF = "branch-or-tag"   install that branch or tag instead of the latest release
#   $env:CLAUDE_INSIGHT_INSTALL_PYTHON = "1"    if Python is missing, install it (winget) without asking
#   $env:CLAUDE_INSIGHT_NO_RUN = "1"            only install; don't generate the report at the end
#
# After this, open Claude Code in any folder and run:  /ai-fluency

# Everything runs inside one script block and stops with `throw`, never `exit`: under
# `irm … | iex` an `exit` would close the person's PowerShell window before they can read why.
& {
$ErrorActionPreference = 'Stop'
$Repo = if ($env:CLAUDE_INSIGHT_REPO) { $env:CLAUDE_INSIGHT_REPO } else { 'pablocarbajal/claude-insights' }
$SkillDir = Join-Path $HOME '.claude\skills\ai-fluency'
$WorkflowDir = Join-Path $HOME '.claude\workflows'
$PyCheck = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'

# A Python 3.8+ that actually runs. `python` / `python3` may be the Microsoft Store stub,
# which is on PATH but only prints "Python was not found" — so each candidate is run.
function Find-Python {
    foreach ($cand in @(@('python'), @('py', '-3'), @('python3'))) {
        try {
            $rest = @($cand | Select-Object -Skip 1)
            & $cand[0] @rest -c $PyCheck 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return ,$cand }
        } catch { }
    }
    return $null
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python 3.8+ is required and winget is not available. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again."
    }
    $cmd = 'winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements'
    Write-Host "Python 3.8+ was not found. It can be installed with:"
    Write-Host "      $cmd"
    if ($env:CLAUDE_INSIGHT_INSTALL_PYTHON -ne '1') {
        $answer = Read-Host "   Install it now? [y/N]"
        if ($answer -notmatch '^(y|yes|s|si|sí)$') {
            throw "Python not installed. Run the command above (or set CLAUDE_INSIGHT_INSTALL_PYTHON=1) and run this again."
        }
    }
    Write-Host "Installing Python..."
    Invoke-Expression $cmd
    # winget updates the machine PATH, not this window's: pick it up before looking again.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $found = Find-Python
    if (-not $found) {
        throw "Python is installed, but this window can't see it yet. Open a NEW PowerShell and run this again."
    }
    return ,$found
}

function Invoke-Py([string[]]$Py, [string[]]$Arguments) {
    $rest = @($Py | Select-Object -Skip 1)
    & $Py[0] @rest @Arguments
}

try {
    Write-Host "Installing the Claude Insight /ai-fluency skill"
    Write-Host "================================================"

    $Py = Find-Python
    if (-not $Py) { $Py = Install-Python }
    $PyVersion = Invoke-Py $Py @('-c', 'import sys; print(sys.version.split()[0])')
    Write-Host "Python found: $($Py -join ' ') ($PyVersion)"

    # Resolve what to download: an explicit branch/tag, else the latest tagged release, else main.
    $Ref = $env:CLAUDE_INSIGHT_REF
    if ($Ref) {
        Write-Host "Installing $Repo @ $Ref"
        $Url = "https://github.com/$Repo/archive/refs/tags/$Ref.zip"
        try { Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing | Out-Null }
        catch { $Url = "https://github.com/$Repo/archive/refs/heads/$Ref.zip" }
    } else {
        try {
            $Ref = (Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -UseBasicParsing).tag_name
        } catch { $Ref = $null }
        if ($Ref) {
            Write-Host "Latest release of ${Repo}: $Ref"
            $Url = "https://github.com/$Repo/archive/refs/tags/$Ref.zip"
        } else {
            Write-Host "No tagged release found in $Repo - using main."
            $Url = "https://github.com/$Repo/archive/refs/heads/main.zip"
        }
    }

    # Download the repo into a temp dir (zip — no git or pip needed).
    $Tmp = Join-Path ([IO.Path]::GetTempPath()) ("claude-insight-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Tmp | Out-Null
    try {
        Write-Host "Downloading..."
        $Zip = Join-Path $Tmp 'src.zip'
        try { Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing }
        catch { throw "Download failed ($Url). Check the name of the repo/branch and your connection." }
        Expand-Archive -Path $Zip -DestinationPath $Tmp
        # The zip holds one folder, named by GitHub after the ref (slashes become dashes).
        $Src = (Get-ChildItem -Path $Tmp -Directory | Select-Object -First 1).FullName
        if (-not (Test-Path (Join-Path $Src 'insight.py'))) {
            throw "The download has no insight.py - is $Repo a Claude Insight repo?"
        }

        # Install the skill self-contained: the engine and the framework live next to SKILL.md,
        # and the workflows go where Claude Code looks for workflows.
        New-Item -ItemType Directory -Force -Path (Join-Path $SkillDir 'reference'), $WorkflowDir | Out-Null
        Copy-Item (Join-Path $Src 'insight.py')                           (Join-Path $SkillDir 'insight.py')
        Copy-Item (Join-Path $Src 'reference\ai-fluency-framework.md')    (Join-Path $SkillDir 'reference\ai-fluency-framework.md')
        Copy-Item (Join-Path $Src '.claude\skills\ai-fluency\SKILL.md')   (Join-Path $SkillDir 'SKILL.md')
        Copy-Item (Join-Path $Src '.claude\workflows\ai-fluency.js')      (Join-Path $WorkflowDir 'ai-fluency.js')
        # The deep read is newer than some releases: install it when the download has it.
        $Deep = Join-Path $Src '.claude\workflows\ai-fluency-deep.js'
        if (Test-Path $Deep) { Copy-Item $Deep (Join-Path $WorkflowDir 'ai-fluency-deep.js') }
    } finally {
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    }

    $Engine = Join-Path $SkillDir 'insight.py'
    Write-Host "Installed:"
    Write-Host "   - skill     -> $SkillDir"
    Write-Host "   - workflows -> $WorkflowDir\ai-fluency.js, ai-fluency-deep.js"
    Write-Host ""

    # Generate the report now, so one command takes you from nothing to your report. The
    # engine finds the chats itself (~\.claude, CLAUDE_CONFIG_DIR, WSL) and, if there are
    # none, says where it looked and what to do.
    if ($env:CLAUDE_INSIGHT_NO_RUN -ne '1') {
        Write-Host "Generating your report..."
        Write-Host ""
        Invoke-Py $Py @($Engine)
        Write-Host ""
    }
    Write-Host "Run it again any time with:  $($Py -join ' ') `"$Engine`""
    Write-Host "Or, for the AI-written read, open Claude Code and run:  /ai-fluency"
} catch {
    Write-Host ""
    Write-Host "Claude Insight was not installed: $($_.Exception.Message)" -ForegroundColor Red
}
}
