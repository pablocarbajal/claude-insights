#!/usr/bin/env bash
set -euo pipefail

# Claude Insight — installs the /ai-fluency skill into Claude Code (macOS, Linux, Git Bash on Windows).
# Usage: curl -fsSL https://raw.githubusercontent.com/arodriguezcb/claude-insights/main/install.sh | bash
#
# Options, as environment variables:
#   CLAUDE_INSIGHT_REPO=owner/name    install from a fork (default: arodriguezcb/claude-insights)
#   CLAUDE_INSIGHT_REF=branch-or-tag  install that branch or tag instead of the latest release
#   CLAUDE_INSIGHT_INSTALL_PYTHON=1   if Python is missing, install it without asking
#
# After this, open Claude Code in any folder and run:  /ai-fluency

REPO="${CLAUDE_INSIGHT_REPO:-arodriguezcb/claude-insights}"
SKILL_DIR="${HOME}/.claude/skills/ai-fluency"
WORKFLOW_DIR="${HOME}/.claude/workflows"
PY_CHECK='import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'

echo "🔍 Installing the Claude Insight /ai-fluency skill"
echo "=================================================="

# A Python 3.8+ that actually runs. On Windows, `python3` is often the Microsoft Store
# stub: it exists on PATH but only prints "Python was not found" — so each candidate is
# run, not just looked up.
find_python() {
  local cand
  for cand in python3 python "py -3"; do
    if $cand -c "$PY_CHECK" >/dev/null 2>&1; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

# The one command that installs Python on this machine, or nothing if no package manager
# is recognised.
python_install_cmd() {
  if command -v winget >/dev/null 2>&1; then
    echo "winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements"
  elif command -v brew >/dev/null 2>&1; then
    echo "brew install python"
  elif command -v apt-get >/dev/null 2>&1; then
    echo "sudo apt-get update && sudo apt-get install -y python3"
  elif command -v dnf >/dev/null 2>&1; then
    echo "sudo dnf install -y python3"
  elif command -v pacman >/dev/null 2>&1; then
    echo "sudo pacman -S --noconfirm python"
  fi
}

install_python() {
  local cmd answer
  cmd="$(python_install_cmd)"
  if [ -z "$cmd" ]; then
    echo "❌ Python 3.8+ is required and no package manager was found."
    echo "   Install it from https://www.python.org/downloads/ and re-run this script."
    exit 1
  fi
  echo "⚠️  Python 3.8+ was not found. It can be installed with:"
  echo "      $cmd"
  if [ "${CLAUDE_INSIGHT_INSTALL_PYTHON:-}" != "1" ]; then
    # Under `curl | bash` stdin is the script itself, so the question goes to the terminal.
    if [ -r /dev/tty ]; then
      printf "   Install it now? [y/N] "
      read -r answer < /dev/tty || answer=""
    else
      answer=""
    fi
    case "$answer" in
      y|Y|yes|YES|s|S|si|sí) ;;
      *) echo "   Not installed. Run the command above (or re-run with CLAUDE_INSIGHT_INSTALL_PYTHON=1)."; exit 1 ;;
    esac
  fi
  echo "📦 Installing Python…"
  bash -c "$cmd"
  if ! PY="$(find_python)"; then
    echo "✅ Python installed, but this terminal can't see it yet. Open a NEW terminal and re-run this script."
    exit 1
  fi
}

if ! PY="$(find_python)"; then
  install_python
fi
echo "✅ Python found: $PY ($($PY -c 'import sys; print(sys.version.split()[0])'))"

# Resolve what to download: an explicit branch/tag, else the latest *tagged release* so a
# work-in-progress commit on main can never break a fresh install, else main.
# `|| true` keeps a no-release repo (grep matches nothing) from aborting the script
# under `set -euo pipefail` before the main fallback can run.
if [ -n "${CLAUDE_INSIGHT_REF:-}" ]; then
  REF="$CLAUDE_INSIGHT_REF"
  echo "📦 Installing ${REPO} @ ${REF}"
  if curl -fsSLI "https://github.com/${REPO}/archive/refs/tags/${REF}.tar.gz" >/dev/null 2>&1; then
    URL="https://github.com/${REPO}/archive/refs/tags/${REF}.tar.gz"
  else
    URL="https://github.com/${REPO}/archive/refs/heads/${REF}.tar.gz"
  fi
else
  REF="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
    | grep '"tag_name"' | head -1 | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/' || true)"
  if [ -n "$REF" ]; then
    echo "📦 Latest release of ${REPO}: ${REF}"
    URL="https://github.com/${REPO}/archive/refs/tags/${REF}.tar.gz"
  else
    echo "ℹ️  No tagged release found in ${REPO} — falling back to main."
    URL="https://github.com/${REPO}/archive/refs/heads/main.tar.gz"
  fi
fi

# Download the repo into a temp dir (tarball — no git or pip needed).
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "📥 Downloading…"
if ! curl -fsSL "$URL" | tar -xz -C "$TMP"; then
  echo "❌ Download failed (${URL}). Check the name of the repo/branch and your connection."
  exit 1
fi
# The tarball holds one folder, named by GitHub after the ref (slashes become dashes).
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
if [ ! -f "$SRC/insight.py" ]; then
  echo "❌ The download has no insight.py — is ${REPO} a Claude Insight repo?"
  exit 1
fi

# Install the skill self-contained: the engine and the framework live next to SKILL.md,
# and the workflows go where Claude Code looks for workflows.
mkdir -p "$SKILL_DIR/reference" "$WORKFLOW_DIR"
cp "$SRC/insight.py"                            "$SKILL_DIR/insight.py"
cp "$SRC/reference/ai-fluency-framework.md"     "$SKILL_DIR/reference/ai-fluency-framework.md"
cp "$SRC/.claude/skills/ai-fluency/SKILL.md"    "$SKILL_DIR/SKILL.md"
cp "$SRC/.claude/workflows/ai-fluency.js"       "$WORKFLOW_DIR/ai-fluency.js"
# The deep read is newer than some releases: install it when the download has it.
if [ -f "$SRC/.claude/workflows/ai-fluency-deep.js" ]; then
  cp "$SRC/.claude/workflows/ai-fluency-deep.js" "$WORKFLOW_DIR/ai-fluency-deep.js"
fi

echo "✅ Installed:"
echo "   • skill     → $SKILL_DIR"
echo "   • workflows → $WORKFLOW_DIR/ai-fluency.js, ai-fluency-deep.js"
echo ""
echo "🎉 Done. Open Claude Code in any folder and run:"
echo ""
echo "      /ai-fluency"
echo ""
echo "   Or, without Claude Code:  $PY \"$SKILL_DIR/insight.py\""
echo "   The report lands in ~/.claude/insight/ai_fluency_report.html"
