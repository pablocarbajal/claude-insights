"""Tests for the Claude Code /ai-fluency skill and its installer."""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "ai-fluency"
WORKFLOW = REPO_ROOT / ".claude" / "workflows" / "ai-fluency.js"
FRAMEWORK = REPO_ROOT / "reference" / "ai-fluency-framework.md"
INSTALL = REPO_ROOT / "install.sh"


class SkillFilesTests(unittest.TestCase):
    def test_skill_pieces_exist(self):
        self.assertTrue((SKILL_DIR / "SKILL.md").exists())
        self.assertTrue(WORKFLOW.exists(), "the workflow the skill invokes must exist")
        self.assertTrue(FRAMEWORK.exists(), "the framework the workflow reads must exist")
        self.assertTrue((REPO_ROOT / "insight.py").exists(), "the engine the skill runs must exist")

    def test_frontmatter_has_required_fields(self):
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---", text, re.S)
        self.assertIsNotNone(match, "SKILL.md must start with a YAML frontmatter block")
        fm = match.group(1)
        self.assertRegex(fm, r"(?m)^name:\s*ai-fluency\s*$")
        self.assertRegex(fm, r"(?m)^description:\s*\S")
        # The skill must invoke the v2 engine and the bundled two-model workflow.
        self.assertIn("insight.py", text)
        self.assertIn("Workflow", text)


class InstallerTests(unittest.TestCase):
    PIECES = ("insight.py", "ai-fluency-framework.md", "SKILL.md", "ai-fluency.js", "ai-fluency-deep.js")

    def test_installer_places_every_skill_piece(self):
        text = INSTALL.read_text(encoding="utf-8")
        for needed in self.PIECES + (".claude/skills/ai-fluency", ".claude/workflows"):
            self.assertIn(needed, text, f"installer should reference {needed}")

    def test_windows_installer_places_every_skill_piece(self):
        text = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
        for needed in self.PIECES + (r".claude\skills\ai-fluency", r".claude\workflows"):
            self.assertIn(needed, text, f"Windows installer should reference {needed}")

    def test_installers_find_a_python_that_runs(self):
        # On Windows `python3` can be the Store stub, so the installers must run each
        # candidate instead of merely finding it on PATH, and offer to install Python.
        for name in ("install.sh", "install.ps1"):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("sys.version_info >= (3, 8)", text, name)
            self.assertIn("winget install --id Python.Python", text, name)
            self.assertIn("CLAUDE_INSIGHT_INSTALL_PYTHON", text, name)
            self.assertIn("CLAUDE_INSIGHT_REPO", text, name)

    def test_skill_does_not_depend_on_python3_alias(self):
        # Every command block picks an interpreter that runs (python3 → python fallback).
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        for line in text.splitlines():
            if "insight.py" in line and line.startswith(("python3 ", "python ")):
                self.fail(f"SKILL.md runs the engine with a bare interpreter name: {line[:60]}")
        self.assertIn("$PY", text)


if __name__ == "__main__":
    unittest.main()
