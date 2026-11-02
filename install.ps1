# Claude Insight — installs the /ai-fluency skill into Claude Code (Windows PowerShell).
# Usage:  irm https://raw.githubusercontent.com/arodriguezcb/claude-insights/main/install.ps1 | iex
#
# Options, as environment variables:
#   $env:CLAUDE_INSIGHT_REPO = "owner/name"     install from a fork (default: arodriguezcb/claude-insights)
#   $env:CLAUDE_INSIGHT_REF = "branch-or-tag"   install that branch or tag instead of the latest release
#   $env:CLAUDE_INSIGHT_INSTALL_PYTHON = "1"    if Python is missing, install it (winget) without asking
#
# After this, open Claude Code in any folder and run:  /ai-fluency

$ErrorActionPreference = 'Stop'
$Repo = if ($env:CLAUDE_INSIGHT_REPO) { $env:CLAUDE_INSIGHT_REPO } else { 'arodriguezcb/claude-insights' }
$SkillDir = Join-Path $HOME '.claude\skills\ai-fluency'
$WorkflowDir = Join-Path $HOME '.claude\workflows'
$PyCheck = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'

Write-Host "Installing the Claude Insight /ai-fluency skill"
Write-Host "================================================"

# A Python 3.8+ that actually runs. `python` / `python3` may be the Microsoft Store stub,
# which is on PATH but only prints "Python was not found" — so each candidate is run.
function Find-Python {
    foreach ($cand in @(@('python'), @('py', '-3'), @('python3'))) {
        try {
            & $cand[0] $cand[1..($cand.Length)] -c $PyCheck 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return ($cand -join ' ') }
        } catch { }
    }
    return $null
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "Python 3.8+ is required and winget is not available."
        Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and re-run."
        exit 1
    }
    $cmd = 'winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements'
    Write-Host "Python 3.8+ was not found. It can be installed with:"
    Write-Host "      $cmd"
    if ($env:CLAUDE_INSIGHT_INSTALL_PYTHON -ne '1') {
        $answer = Read-Host "   Install it now? [y/N]"
        if ($answer -notmatch '^(y|yes|s|si|sí)$') {
            Write-Host "   Not installed. Run the command above (or set CLAUDE_INSIGHT_INSTALL_PYTHON=1) and re-run."
            exit 1
        }
    }
    Write-Host "Installing Python..."
    Invoke-Expression $cmd
    # winget updates the machine PATH, not this window's: pick it up before looking again.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $script:Py = Find-Python
    if (-not $script:Py) {
        Write-Host "Python installed, but this window can't see it yet. Open a NEW PowerShell and re-run this script."
        exit 1
    }
}

$Py = Find-Python
if (-not $Py) { Install-Python }
$PyVersion = & cmd /c "$Py -c `"import sys; print(sys.version.split()[0])`""
Write-Host "Python found: $Py ($PyVersion)"

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
        Write-Host "No tagged release found in $Repo - falling back to main."
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
    catch { Write-Host "Download failed ($Url). Check the name of the repo/branch and your connection."; exit 1 }
    Expand-Archive -Path $Zip -DestinationPath $Tmp
    # The zip holds one folder, named by GitHub after the ref (slashes become dashes).
    $Src = (Get-ChildItem -Path $Tmp -Directory | Select-Object -First 1).FullName
    if (-not (Test-Path (Join-Path $Src 'insight.py'))) {
        Write-Host "The download has no insight.py - is $Repo a Claude Insight repo?"; exit 1
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

Write-Host "Installed:"
Write-Host "   - skill     -> $SkillDir"
Write-Host "   - workflows -> $WorkflowDir\ai-fluency.js, ai-fluency-deep.js"
Write-Host ""
Write-Host "Done. Open Claude Code in any folder and run:"
Write-Host ""
Write-Host "      /ai-fluency"
Write-Host ""
Write-Host "   Or, without Claude Code:  $Py `"$SkillDir\insight.py`""
Write-Host "   The report lands in $HOME\.claude\insight\ai_fluency_report.html"
