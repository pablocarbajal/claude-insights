#!/usr/bin/env python3
"""
Claude Insight v2 — one-command, zero-install AI-fluency analyzer.

    python3 insight.py

Reads your local Claude Code transcripts (~/.claude/projects/**/*.jsonl),
estimates how skillfully you drive an AI coding agent, and writes a single
self-contained HTML report (./ai_fluency_report.html) that opens in your browser.

Design principles (see README "Methodology"):
  * It measures SKILL, not activity. Every score input is a per-prompt or
    per-opportunity RATE pushed through a saturating curve, so using the agent
    MORE can never raise your score — only using it BETTER can.
  * It only looks at YOUR real typed prompts and Claude's real tool actions.
    Tool-results, subagent turns, slash-command stubs, injected system text and
    pasted walls of text are filtered out before anything is scored.
  * Every number is auditable: baselines are recomputed from your corpus at
    runtime, formulas are documented, and thin signals are flagged "low data"
    and pulled toward a neutral 50 instead of faking confidence.

Pure Python standard library — no pip, no Ollama, no API key. One command runs the
whole pass: de-contaminate and scrub your transcripts, score them, and (as
`/ai-fluency` in Claude Code) write a Sonnet+Opus skill map grounded in the AI
Fluency framework on top. The only thing it writes is
the HTML report and a local copy of your transcripts in an archive
(~/.claude/insight-archive) so history survives Claude Code's 30-day cleanup —
pass --no-archive to skip that and read your transcripts without copying them.
"""

import argparse
import glob
import hashlib
import html
import json
import math
import os
import re
import shutil
import statistics
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime

# --------------------------------------------------------------------------- #
# Constants & tunables (documented; shown in the report's methodology appendix)
# --------------------------------------------------------------------------- #

DEFAULT_DIRS = ["~/.claude/projects", "~/.claude/sessions"]

# Claude Code deletes transcripts older than its `cleanupPeriodDays` setting (default 30),
# so by default only ~30 days of history is ever on disk. We mirror each run's transcripts
# into this persistent archive so history accumulates indefinitely and survives the cleanup.
# Keep this on a PRIVATE, per-person path. A single archive folder shared between different
# people or computers (e.g. a synced team Dropbox) merges everyone's transcripts into one
# analysis — so each person must point --archive at their own location, not a shared one.
DEFAULT_ARCHIVE_DIR = "~/.claude/insight-archive"

GAP_CAP_SECONDS = 300          # idle gaps longer than this are NOT counted as active time
MAX_HUMAN_PROMPT_CHARS = 6000  # anything longer is treated as a paste/injection, not a typed prompt
PROVISIONAL_MIN_PROMPTS = 30   # below this the headline score is shown as a hedged range

EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
READ_TOOLS = {"read", "grep", "glob"}

# Text that marks a "user"-role record as injected/system rather than typed by the human.
INJECTION_MARKERS = (
    "<task-notification>", "<command-name>", "<command-message>", "<command-args>",
    "<local-command-caveat>", "<local-command-stdout>", "<system-reminder>",
    "<bash-input>", "<bash-stdout>", "caveat: the messages below",
    "[request interrupted", "base directory for this skill", "<user-prompt-submit-hook>",
    "<user-memory-input>", "this session is being continued", "plugin sent a message:",
)

_PASTED_RE = re.compile(r"<pasted_content\b[^>]*>.*?(</pasted_content>|\Z)", re.S | re.I)

# Subagent system prompts get stored as plain user-role text with no other marker.
# They almost always open with "You are <role>…". This catches the back-door inflation.
_INJECTED_HEAD = re.compile(
    r"^\s*(you are\b|<[a-z][\w-]*>|base directory for this skill)", re.I
)

# Broad, project-extensible verification matcher (matched against real Bash commands).
VERIFY_RE = re.compile(
    r"\b("
    r"pytest|unittest|jest|vitest|mocha|go test|cargo (test|build|check)|"
    r"npm (run )?(test|build|lint)|yarn (test|build|lint)|pnpm (test|build|lint)|"
    r"ruff|eslint|flake8|mypy|tsc\b|make (test|lint|build|check)|playwright|"
    r"python\d? -m \w|\.venv/bin/python|lsof -ti|curl .*(localhost|127\.0\.0\.1)|"
    r"docker compose|docker-compose|pre-commit|dotnet (build|test|run)|msbuild|cmake --build|"
    r"flutter (test|build|analyze)|gradlew? (build|test)|(npm|pnpm|yarn) (-C \S+ |--dir \S+ )?(run )?(test|build|lint)"
    r")",
    re.I,
)
# Clean-teardown of a live system (small bonus, folded into Verification).
TEARDOWN_RE = re.compile(r"(lsof -ti.*kill|pkill|kill -9|docker compose down|docker-compose down)", re.I)

# Direction (prompt-quality) cues. Every cue list is bilingual (English + Spanish) and
# accent-tolerant, so Spanish speakers aren't scored as if they never state a why or a rule.
# Spanish verbs may carry an attached pronoun ("corrígelo", "créame"), hence the _CL suffix.
_CL = r"(?:lo|la|los|las|le|les|me|nos|melo|mela|selo|sela)?"
ARTIFACT_RE = re.compile(
    r"([\w./\-]+\.(py|js|ts|tsx|jsx|html|css|md|json|sh|ya?ml|toml|rs|go|java|cpp|c|rb|sql))"
    r"|((?:/[\w.\-]+){2,})"        # multi-segment paths (not bare /word or </tag>)
    r"|(`[^`]+`)"                  # inline code / quoted token
    r"|(\b\w+\(\))",               # function() reference
    re.I,
)
CONSTRAINT_CUE = re.compile(
    r"\b(only|must|should|shouldn't|don't|do not|never|always|keep|ensure|instead of|"
    r"at most|at least|exactly|without|except|make sure|no more than|leave .* as is|"
    # es
    r"s[oó]lo|solamente|debe|deben|tiene que|tienen que|nunca|siempre|mant[eé]n|mantener|"
    r"aseg[uú]rate|en vez de|en lugar de|como m[aá]ximo|al menos|exactamente|sin|excepto|salvo|"
    r"no m[aá]s de|no (toques|cambies|borres|uses|modifiques|agregues|quites)|"
    r"d[eé]ja(lo|la|los|las)? como est[aá])\b", re.I
)
INTENT_CUE = re.compile(
    r"\b(so that|because|the goal is|in order to|for the demo|for my|for the|so i can|so we can|"
    r"so it|i need|i want .* so|"
    # es
    r"para que|porque|ya que|el objetivo es|la idea es|con el fin de|a fin de|de modo que|"
    r"as[ií] (puedo|podemos|queda)|necesito|lo necesito|para (mi|mis|el|la|los|las|poder))\b", re.I
)
ACTION_VERB = re.compile(
    r"\b(add|create|build|make|implement|write|fix|change|update|refactor|remove|delete|run|"
    r"generate|set up|setup|install|deploy|edit|rename|move|clean|stitch|speed up|merge|split|"
    # es (imperative, often with an attached pronoun)
    rf"(?:agrega|a[ñn]ade|crea|construye|haz|hace|implementa|escribe|arregla|corrige|corr[ií]ge|"
    rf"corr[ií]je|cambia|actualiza|refactoriza|quita|elimina|borra|ejecuta|corre|genera|configura|"
    rf"instala|despliega|edita|renombra|mueve|limpia|une|divide|pon|convierte|reemplaza|sube|"
    rf"arma|monta|traduce|transcribe|cierra|abre){_CL})\b", re.I
)

# A bare go-ahead / continuation ("ok", "sí, hazlo", "sigue, página 26"). These answer a
# proposal the agent just made, so they aren't briefs and must not drag Briefing down.
ACK_RE = re.compile(
    r"^\s*(ok(ay)?|dale|me sirve( eso)?|s[ií]+|ya|listo|sigue|sigamos|contin[uú]a|continuemos|adelante|hazlo|"
    r"de acuerdo|vale|perfecto|bien|est[aá] bien|exacto|correcto|yes|yep|yeah|sure|go ahead|"
    r"continue|proceed|do it|sounds good|lgtm)\b", re.I
)
ACK_MAX_WORDS = 8

# Iteration cues. A bare Spanish "no" only counts at the start of the prompt: mid-sentence it's
# ordinary negation ("no sé", "¿no?"), not a rejection.
CORRECTION_CUE = re.compile(
    r"\b(nope|wrong|not quite|that's not|thats not|actually|instead|revert|undo|redo|try again|"
    r"too (aggressive|agressive|much|many|slow|fast|big|small)|still (broken|failing|wrong|not)|"
    r"doesn't work|does not work|not working|unteligible|unteliggeble|"
    # es
    r"est[aá] mal|incorrecto|en realidad|mejor no|deshaz|revierte|otra vez|de nuevo|"
    r"sigue (sin|fallando|igual|mal)|no funciona|no sirve|no se ve|"
    r"no sale|no sali[oó]|no compila|no anda|no carga|no aparece|no era eso|no es eso)\b"
    r"|^\s*(no|nop|nel)\b", re.I
)
PRAISE_CUE = re.compile(
    r"\b(great|perfect|love it|nice|awesome|excellent|beautiful|exactly|"
    r"genial|perfecto|excelente|buen[ií]simo|me encanta|bien hecho|exacto|incre[ií]ble)\b", re.I
)
# After the agent changes something, did YOU look at the result before moving on? A reply that
# reports what you saw ("funciona", "no se ve el icono", "the button works now", an error, a
# number) counts as checking; a bare "ok, sigue" right after an edit is a rubber stamp.
CHECKED_CUE = re.compile(
    r"\b(works|working now|it runs|ran it|i tested|tested it|i tried|compiles|builds|shows|"
    r"appears|i see|looks (good|right|fine|wrong|off)|error|exception|log|screenshot|"
    r"funciona|funcion[oó]|ya (sale|anda|carga|compila|aparece|se ve)|se ve|veo|sale|aparece|"
    r"prob[eé]|lo prob[eé]|ya lo prob[eé]|corr[ií]|compila|compil[oó]|captura|pantallazo)\b", re.I
)
CORRECTION_RATE_CEILING = 0.35   # a "high" correction rate; lower is better

# Delegation / planning tool signals.
DELEGATION_TOOLS = {"agent", "task", "workflow", "exitplanmode", "enterplanmode"}

# Dimension weights (sum to 1.0).
WEIGHTS = {
    "Direction": 0.24,
    "Verification": 0.22,
    "Context": 0.22,
    "Iteration": 0.18,
    "Toolcraft": 0.14,
}
# Opportunity-count targets for per-dimension confidence shrinkage.
TARGET_N = {"Direction": 60, "Verification": 15, "Context": 25, "Iteration": 12, "Toolcraft": 40}

# User-facing labels. "Direction" is shown as "Briefing" so it never collides with the
# "Director" archetype (the dimension measures how well you brief; the archetype, that
# you delegate — different things).
DISPLAY_NAMES = {"Direction": "Briefing", "Verification": "Verification",
                 "Context": "Context-setting", "Iteration": "Iteration", "Toolcraft": "Toolcraft"}

def disp(name):
    return DISPLAY_NAMES.get(name, name)

# Teacher content for each skill (kind, plain-English, with before/after examples and a
# weekly practice). Used to make the report explain what to improve and exactly how.
SKILL_TEACH = {
    "Direction": {
        "what_it_is": "Telling the agent what you want and giving it something to aim at: a goal plus a file, a constraint, or a way to know it worked.",
        "why_it_matters": "When your goal and your limits are clear up front, the agent gets it right the first time instead of guessing and pulling you into rounds of fixes.",
        "how_to_improve": "Before you hit enter, add one anchor to your goal: the file to touch, a rule it must not break, or a 'done when…' line. One line is plenty.",
        "examples": [
            {"before": "fix the login bug", "after": "Users stay logged out after a correct password on Safari. The check lives in src/auth/session.ts. Fix it so a valid login sets the session cookie, and keep the current tests green."},
            {"before": "add caching to the API", "after": "Cache GET /products responses in api/products.py for 60s to ease DB load on repeat reads. Don't cache authed requests, and add a test that a second call within 60s skips the DB."},
        ],
        "practice": "Before sending a prompt, add one anchor to your goal: a file path, a constraint, or a 'done when…' line.",
        "good_looks_like": "Every request says what you want plus where to work or how success is judged, so the agent acts instead of guessing.",
    },
    "Verification": {
        "what_it_is": "Having the agent prove its own work — run the tests, build, lint, or launch the app — before it tells you it's done.",
        "why_it_matters": "Code that looks right but was never run is where most AI bugs hide; checking it turns “probably works” into “I watched it work.”",
        "how_to_improve": "In the same prompt that asks for the change, name the exact command that proves it (a test, build, lint, or curl) and tell the agent to run it and show you the output before stopping.",
        "examples": [
            {"before": "Fix the off-by-one in the pagination helper.", "after": "Fix the off-by-one in the pagination helper, then run `pytest tests/test_pagination.py -x` and paste the output. Don't call it fixed until that test passes."},
            {"before": "Add a /health endpoint to the FastAPI server.", "after": "Add a /health endpoint to the FastAPI server. Start it on port 8000, curl `localhost:8000/health`, and show me the response. Run `ruff check` too and confirm it's clean before you finish."},
        ],
        "practice": "Before you accept any change, ask: “How did you verify this? Run it and show me the output.”",
        "good_looks_like": "Every change ends with proof — a passing test, a green build, a real response — pasted back to you, not just a claim.",
    },
    "Context": {
        "what_it_is": "Pointing the agent at the real code — a file, a function, a line area — and having it read that before it changes anything.",
        "why_it_matters": "When the agent sees the actual current code first, its edits fit what's really there instead of a guess, so they apply cleanly the first time.",
        "how_to_improve": "Before any edit, name the exact file (and the function or area if you can) and tell the agent to read it first. Let it look before it leaps.",
        "examples": [
            {"before": "Add retry logic to the API client.", "after": "Read src/api/client.ts first, then add retry-with-backoff to the request() method. Show me the change before you apply it."},
            {"before": "Fix the timezone bug in the date formatter.", "after": "Open src/utils/date.ts and find formatDate(). Read how it handles timezones now, then fix the off-by-one so UTC inputs render in the user's local zone."},
        ],
        "practice": "Start your next edit request with “Read <file> first, then…” so the agent grounds itself before touching anything.",
        "good_looks_like": "Every edit lands on code the agent just read, so diffs apply cleanly with nothing broken around them.",
    },
    "Iteration": {
        "what_it_is": "When the agent goes the wrong way, steering it back with a precise correction — naming what broke and the rule to follow — instead of just “no” or “try again.”",
        "why_it_matters": "A precise correction lands the fix in one round; a vague “no” makes the agent guess again, and you burn turns while the code drifts further off.",
        "how_to_improve": "When a result is wrong, say three things in one message: the symptom you saw, the rule it broke, and what to do instead. Then let it run.",
        "examples": [
            {"before": "no that's not right, try again", "after": "The retry loop catches the exception but never re-raises after the last attempt, so failures look like successes. Re-raise the original error once retries run out, and keep the existing backoff."},
            {"before": "this is wrong, fix the test", "after": "The test passes because you mocked the function under test instead of the network call. Don't mock get_user — mock requests.get inside it, and assert it was called with the real URL."},
        ],
        "practice": "Before sending a correction, check it names both the symptom and the rule. If it only says “no,” add the missing half.",
        "good_looks_like": "One sharp correction — symptom, rule, and the fix — and the agent lands it on the next try.",
    },
    "Toolcraft": {
        "what_it_is": "Letting the agent use the right tool for each step — searching the code, running commands, starting the app, working in the background — instead of forcing everything through chat.",
        "why_it_matters": "The agent works faster and more reliably when it searches and runs things for real, rather than reasoning about the code from memory.",
        "how_to_improve": "Tell the agent which action to take first — search the codebase, run the suite, start the server — so it gathers facts and checks its work with the tool built for each step.",
        "examples": [
            {"before": "How does login work in this app?", "after": "Search the codebase for the login flow (grep for auth, session, login), read the files you find, then explain how a request goes from form submit to a logged-in session."},
            {"before": "Add a retry to the API client, and make sure the tests still pass.", "after": "Add retry-with-backoff to the API client. Then run the suite in the background; if anything fails, read the failure, fix it, and report back when it's green."},
        ],
        "practice": "Add one line to your next task telling the agent which action to take first: “search for…”, “run the tests”, or “start the server and check.”",
        "good_looks_like": "You hand off a whole job and the agent searches, edits, runs, and verifies on its own — each step using the tool made for it.",
    },
}

BANDS = [
    ("Operator", 0, 39, "You use the agent as fast hands. Prompts are short and underspecified, "
     "edits often happen without reading the file first, and changes are rarely verified. The "
     "fastest gains live right here: state a goal plus one constraint, and let the agent read "
     "before it edits."),
    ("Developing", 40, 54, "Real back-and-forth is emerging and one or two habits are solid. Some "
     "prompts carry a file path or a constraint; verification happens occasionally. The gap to the "
     "next level is consistency — doing the right thing by default, not just sometimes."),
    ("Proficient", 55, 69, "You drive the agent deliberately. Most prompts are specific, edits "
     "usually follow a read of the same file, and you verify more often than not. Solid, reliable "
     "AI-assisted engineering. Remaining gains are about altitude (saying why) and orchestration."),
    ("Advanced", 70, 84, "You orchestrate rather than operate. Prompts encode goals, constraints "
     "and acceptance criteria; reading precedes editing as a habit; verification is near-automatic; "
     "you use planning and delegation fluently. You brief the agent like a senior teammate."),
    ("Expert", 85, 100, "You treat the agent as a managed engineering system: consistently "
     "high-context prompts with explicit success criteria, disciplined read→edit→verify loops, "
     "deliberate delegation, and almost no wasted correction cycles."),
]

# Archetype axes and prototypes.
# The archetype describes YOUR DRIVING STYLE, so it is built only from signals you
# control and DISCOUNTS the habits Claude does on its own. Verification and Context
# (read-before-edit, running tests) are largely the agent's defaults, so they carry
# low "agency" weight; how you brief (Direction), correct (Iteration), reach for tools
# (Toolcraft) and hand off work (Delegation) carry full weight.
ARCHETYPE_AXES = ["Direction", "Verification", "Context", "Iteration", "Toolcraft", "Delegation"]
AGENCY = {"Direction": 1.0, "Verification": 0.35, "Context": 0.15,
          "Iteration": 1.0, "Toolcraft": 0.8, "Delegation": 1.0}

# Prototype vectors over ARCHETYPE_AXES (0-100). Delegation is the axis that separates
# a hands-off delegator from a hands-on builder. These are the five explicit, recognizable
# builder archetypes; the classifier picks the nearest one from your AGENCY-WEIGHTED vector.
PROTOTYPES = {
    "Autonomous Agent": {"emoji": "🤖", "vec": [58, 65, 62, 62, 85, 96],
        "blurb": "You delegate whole, end-to-end jobs and trust the agent to run them — you set the outcome and let Claude pick the steps."},
    "Architect":        {"emoji": "🏗️", "vec": [80, 66, 88, 65, 60, 48],
        "blurb": "You plan and explore before you build — you read and design first, so changes land on a clear structure."},
    "Debugger":         {"emoji": "🐛", "vec": [62, 88, 82, 85, 60, 28],
        "blurb": "You hunt problems methodically — read to diagnose, change, verify, and repeat until it's truly fixed."},
    "Collaborator":     {"emoji": "🤝", "vec": [66, 62, 66, 80, 55, 38],
        "blurb": "You work with the agent like a teammate — ask for options, give feedback, and steer toward alignment."},
    "Sprinter":         {"emoji": "⚡", "vec": [45, 38, 52, 46, 62, 30],
        "blurb": "You move fast and direct — terse prompts, quick turns, low ceremony. Great velocity; briefing and verification are the growth edges."},
}
ARCHETYPE_MARGIN = 0.06   # cosine-similarity margin below which we emit a blended label


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _text_of(content):
    """Concatenate the text blocks of a message content (str or list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _is_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/?[^<]+?)\s*</command-name>")


def _command_name(text):
    """Pull the slash-command name out of a <command-name>…</command-name> stub, else None."""
    m = _COMMAND_NAME_RE.search(text)
    return m.group(1) if m else None


def _looks_injected(text):
    head = text[:200].lstrip()
    if len(text) > MAX_HUMAN_PROMPT_CHARS:
        return True
    if _INJECTED_HEAD.match(head):
        return True
    low = text.lower()
    return any(m in low for m in INJECTION_MARKERS)


def _denamespace_tool(name):
    """mcp__<hash>__slack_read_thread -> slack_read_thread; keep core names as-is."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        return parts[-1] if parts else name
    return name


def _mcp_server(raw):
    """mcp__<server>__<tool> -> <server>; strip a leading 'plugin_<plugin>_' wrapper.

    mcp__grafana-qa__query_prometheus        -> grafana-qa
    mcp__plugin_sre_jaeger-qa__find-traces   -> jaeger-qa
    mcp__plugin_engram_engram__mem_save      -> engram

    Returns None for non-MCP tool names. Ponytail ceiling: the only assumption is that
    the plugin name is a single, '_'-free token, so dropping the first '_'-delimited
    token of the wrapper yields the server-id.
    """
    if not raw.startswith("mcp__"):
        return None
    parts = raw.split("__")
    if len(parts) < 2:
        return None
    server = parts[1]
    if server.startswith("plugin_"):
        rest = server[len("plugin_"):]            # 3.8-safe: slice, not str.removeprefix
        server = rest.split("_", 1)[1] if "_" in rest else rest
    return server


def _mcp_plugin(raw):
    """mcp__plugin_<plugin>_<server>__<tool> -> <plugin> (the marketplace plugin id token).

    mcp__plugin_sre_jaeger-qa__find-traces   -> sre
    mcp__plugin_engram_engram__mem_save      -> engram

    Returns None for non-plugin MCP names (e.g. mcp__context7__…) and non-MCP names — only
    plugin-provided servers carry a plugin token. Same ceiling as _mcp_server: the plugin
    name is assumed to be a single, '_'-free token."""
    if not raw.startswith("mcp__"):
        return None
    parts = raw.split("__")
    if len(parts) < 2 or not parts[1].startswith("plugin_"):
        return None
    rest = parts[1][len("plugin_"):]              # 3.8-safe slice
    return rest.split("_", 1)[0] if rest else None


# --------------------------------------------------------------------------- #
# Crabi-tool adoption — offline provenance join + tiny signature map
# --------------------------------------------------------------------------- #
#
# Adoption is read LIVE and OFFLINE from ~/.claude config (no network, no bundled
# plugin catalog). The two-file join — installed_plugins.json + known_marketplaces.json
# — deterministically proves which plugins came from crabi/ai-marketplace; the signature
# map below is needed ONLY for non-plugin / display signals (Finding 4). All of this is
# additive post-parse: parse(), the scored corpus, and every score stay untouched.

def _read_json(path):
    """Load a JSON file; return {} on any missing/malformed/IO error — never raise."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_installed_plugins(claude_dir=None):
    """{plugin_id -> first install record dict} from plugins/installed_plugins.json.

    Top-level shape is {"version", "plugins"}; plugins[id] is a LIST of install records,
    so we index [0] (the install record). Returns {} on anything malformed."""
    root = os.path.expanduser(claude_dir or "~/.claude")
    raw = _read_json(os.path.join(root, "plugins", "installed_plugins.json"))
    plugins = raw.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    out = {}
    for pid, records in plugins.items():
        if isinstance(records, list) and records and isinstance(records[0], dict):
            out[pid] = records[0]
    return out


def _load_known_marketplaces(claude_dir=None):
    """{marketplace_name -> entry dict} from plugins/known_marketplaces.json (keyed by NAME)."""
    root = os.path.expanduser(claude_dir or "~/.claude")
    raw = _read_json(os.path.join(root, "plugins", "known_marketplaces.json"))
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _load_enabled_plugins(claude_dir=None):
    """{plugin_id -> bool} from settings.json -> enabledPlugins. {} if absent/malformed."""
    root = os.path.expanduser(claude_dir or "~/.claude")
    raw = _read_json(os.path.join(root, "settings.json"))
    enabled = raw.get("enabledPlugins")
    return enabled if isinstance(enabled, dict) else {}


def _load_mcp_server_names(claude_json=None):
    """set[str] of configured MCP server NAMES from ~/.claude.json (param overridable).

    Union of: top-level `mcpServers` keys, each `projects[<path>].mcpServers` keys, and a
    best-effort `<path>/.mcp.json` -> `mcpServers` keys per project path. Never raises;
    returns set() on any miss.

    SECRET SAFETY: reads KEY NAMES ONLY, never entry values — mcpServers entries carry env
    vars / headers that can hold tokens; none of that may reach the corpus/report/evidence.
    """
    path = os.path.expanduser(claude_json or "~/.claude.json")
    raw = _read_json(path)
    names = set()

    def _keys(section):
        return set(section) if isinstance(section, dict) else set()

    names |= _keys(raw.get("mcpServers"))
    projects = raw.get("projects")
    if isinstance(projects, dict):
        for proj_path, proj in projects.items():
            if isinstance(proj, dict):
                names |= _keys(proj.get("mcpServers"))
            # best-effort per-project .mcp.json (keys only)
            names |= _keys(_read_json(os.path.join(proj_path, ".mcp.json")).get("mcpServers"))
    return names


# Tiny signature map — NON-PLUGIN / DISPLAY ONLY (Finding 4). Plugin provenance is read
# live from config, so the crabi/ai-marketplace target needs NO entry here. Each named
# target's USED rules are the only thing that can rot, so keep this small.
#   * plugin_id       — OPTIONAL. Present for plugin-backed targets (installed/enabled/version
#                       come from the plugin config join). Absent for MCP-server targets.
#   * mcp_tool_regex  — engram's tools land in tool_usage as bare mem_* (Finding 5).
#   * namespace       — skills/commands surface as "<ns>:<name>" and "/<ns>:<name>"; a single
#                       namespace token covers ponytail / obsidian / desplega usage.
#   * mcp_server_key  — an MCP-server target is INSTALLED iff this name is in the configured
#                       MCP servers (~/.claude.json). No plugin_id, no version/enabled signal.
#   * mcp_server_regex — an MCP-server target's USED count sums corpus.mcp_servers ids matching
#                       this regex (direct server + claude.ai connector share one target).
# No hook-banner parsing (Decision: speculative without Superpowers installed — Appendix).
SIGNATURES = {
    "engram": {
        "plugin_id": "engram@engram",
        # mem_* tools already land in tool_usage; counting both tool_usage AND
        # mcp_servers["engram"] would double-count, so engram USED rides on the regex alone.
        "mcp_tool_regex": re.compile(r"^mem_[a-z_]+$"),
    },
    "ponytail": {
        "plugin_id": "ponytail@ponytail",
        "namespace": "ponytail",
    },
    "obsidian": {
        "plugin_id": "obsidian@obsidian-skills",
        "namespace": "obsidian",
        # The crabi-obsidian-notes skill drives Obsidian through the CLI (`obsidian vault=…`),
        # not `/obsidian:` slash-commands — so count the Bash CLI form too, else real usage
        # reads as used=0 (Finding: false "installed but never used" nudge). ponytail: matches
        # the skill's mandatory `obsidian vault=` form; a bare `obsidian <subcmd>` won't count.
        "cmd_regex": re.compile(r"\bobsidian vault="),
    },
    "desplega": {
        "plugin_id": "desplega@desplega-ai-toolbox",
        "namespace": "desplega",
    },
    "superpowers": {
        # ponytail: install key is a best-effort guess — superpowers isn't installed on this
        # machine, so this id is unverified until it shows up in installed_plugins.json.
        "plugin_id": "superpowers@superpowers",
        "namespace": "superpowers",
    },
    "context7": {
        # MCP-server target (no plugin_id): installed = configured in ~/.claude.json; used =
        # direct server id `context7` + claude.ai connector id `claude_ai_Context7`.
        "mcp_server_key": "context7",
        "mcp_server_regex": re.compile(r"(?i)^(claude_ai_)?context7$"),
    },
}

# The provenance key: a plugin is "Crabi-authored" iff its marketplace resolves to this repo.
CRABI_REPO = "crabi/ai-marketplace"

# Display names + category for the usage panel's MCP-server grouping (display only; rot-safe
# because an unmapped server just falls back to its raw id).
SERVER_DISPLAY = {
    "engram": ("Engram", "Memory"),
    "ponytail": ("Ponytail", "Code-quality"),
    "grafana-prod": ("Grafana (Prod)", "Observability"),
    "grafana-qa": ("Grafana (QA)", "Observability"),
    "jaeger-prod": ("Jaeger (Prod)", "Tracing"),
    "jaeger-qa": ("Jaeger (QA)", "Tracing"),
    "context7": ("Context7", "Docs"),
    "claude_ai_Context7": ("Context7 (claude.ai)", "Docs"),
    "github": ("GitHub", "VCS"),
}


def _namespace_used(corpus, namespace):
    """How many times a "<ns>:" skill/command was invoked, via corpus.commands.

    Commands surface as "/<ns>:<name>" (e.g. "/desplega:research"); we also accept a bare
    "<ns>:" prefix for robustness. Skills don't reach the timeline, so commands is the
    practical USED source for namespace tools (Finding 4)."""
    n = 0
    pref_cmd = "/" + namespace + ":"
    pref_bare = namespace + ":"
    for cmd, count in corpus.commands.items():
        if cmd.startswith(pref_cmd) or cmd.startswith(pref_bare):
            n += count
    return n


def _cmd_used(corpus, regex):
    """How many Bash commands in the timeline matched `regex`.

    The USED signal for tools driven through a CLI rather than slash-commands or MCP
    calls — e.g. the crabi-obsidian-notes skill shells out `obsidian vault=… create …`,
    which lands in the timeline as a bash event's `cmd`, invisible to namespace matching."""
    n = 0
    for _sid, _project, timeline in _iter_sessions(corpus):
        for ev in timeline:
            if ev.get("name") == "bash" and regex.search(ev.get("cmd") or ""):
                n += 1
    return n


def _plugin_used(corpus, plugin):
    """Total invocations of a marketplace plugin across ALL channels: slash-commands
    (`/<plugin>:`), MCP calls (`mcp__plugin_<plugin>_…`), and Skill/Agent "<plugin>:…" ids.

    A namespace-only count lies for plugins driven by MCP (sre → grafana/jaeger) or
    subagents (breaking-balls-reviewer) — they'd read as never-used. ponytail: coarse union;
    a slash-command that also fires a Skill event may double-count, fine for a used/never
    signal."""
    used = _namespace_used(corpus, plugin)
    used += corpus.mcp_plugins.get(plugin, 0)
    pref = plugin + ":"
    for sk, n in corpus.skill_invocations.items():
        if sk.startswith(pref):
            used += n
    return used


def _marketplace_repo(marketplaces, name):
    """known_marketplaces[name].source.repo, or None if any link is missing/malformed."""
    entry = marketplaces.get(name)
    if not isinstance(entry, dict):
        return None
    source = entry.get("source")
    if not isinstance(source, dict):
        return None
    return source.get("repo")


def _version_tuple(v):
    """Parse a dotted-int version into a tuple, or None if missing/"unknown"/unparseable."""
    if not isinstance(v, str) or v.strip().lower() in ("", "unknown"):
        return None
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except ValueError:
        return None


def _version_outdated(installed, latest):
    """True/False/None — is `installed` behind `latest`?

    Returns None (unknown) when either side is missing, "unknown", or unparseable, so the
    report never makes a false up-to-date / outdated claim. ponytail: naive dotted-int
    compare, no prerelease/semver-build handling — fine for plugin versions."""
    iv, lv = _version_tuple(installed), _version_tuple(latest)
    if iv is None or lv is None:
        return None
    return iv < lv


def _load_marketplace_catalog(marketplace=None, repo_slug=None, claude_dir=None):
    """{plugin_name -> latest_version|None} from a marketplace's local clone manifest.

    Resolves the marketplace entry EITHER by name (direct key in known_marketplaces) OR by
    repo slug (the entry whose source.repo == repo_slug — used for the Crabi target). Reads
    its installLocation (fallback <root>/plugins/marketplaces/<name>) + .claude-plugin/
    marketplace.json. Best-effort: returns {} on any miss, never raises."""
    root = os.path.expanduser(claude_dir or "~/.claude")
    marketplaces = _load_known_marketplaces(claude_dir)

    name = marketplace
    entry = None
    if name is not None:
        entry = marketplaces.get(name)
    elif repo_slug is not None:
        for n, e in marketplaces.items():
            if isinstance(e, dict):
                src = e.get("source")
                if isinstance(src, dict) and src.get("repo") == repo_slug:
                    name, entry = n, e
                    break
    if name is None:
        return {}

    install_loc = None
    if isinstance(entry, dict):
        loc = entry.get("installLocation")
        if isinstance(loc, str) and loc:
            install_loc = os.path.expanduser(loc)
    if install_loc is None:
        install_loc = os.path.join(root, "plugins", "marketplaces", name)

    manifest = _read_json(os.path.join(install_loc, ".claude-plugin", "marketplace.json"))
    plugins = manifest.get("plugins")
    if not isinstance(plugins, list):
        return {}
    out = {}
    for p in plugins:
        if isinstance(p, dict) and isinstance(p.get("name"), str):
            out[p["name"]] = p.get("version")  # may be None / "unknown"
    return out


def build_adoption(corpus, claude_dir=None, claude_json=None):
    """Per-target {installed, enabled, used} from the offline provenance join + signatures.

    Targets:
      * "crabi/ai-marketplace" — installed iff ANY installed plugin's marketplace resolves
        (via known_marketplaces) to CRABI_REPO; enabled iff any such plugin is enabled; used
        = invocations of those plugins' commands (namespace = plugin name). Pure provenance —
        no signature needed (Finding 4).
      * named tools (engram / ponytail / obsidian / desplega) — installed iff their plugin_id
        is in installed_plugins; used per their signature rules.
      * MCP-server targets (context7) — no plugin_id; installed iff the server name is in
        ~/.claude.json (claude_json param overridable for tests); enabled + version fields
        stay None; used sums corpus.mcp_servers ids matching the target's mcp_server_regex.

    Pure post-parse + config read; raises on nothing (config readers return {} on failure).
    """
    installed = _load_installed_plugins(claude_dir)
    marketplaces = _load_known_marketplaces(claude_dir)
    enabled = _load_enabled_plugins(claude_dir)
    mcp_server_names = _load_mcp_server_names(claude_json)   # loaded once, not per target

    out = {}

    # --- crabi/ai-marketplace via provenance join -------------------------------------
    crabi_ids = []
    for pid in installed:
        # plugin_id is "<plugin>@<marketplace>"; rsplit is 3.8-safe.
        if "@" not in pid:
            continue
        _plugin, marketplace = pid.rsplit("@", 1)
        if _marketplace_repo(marketplaces, marketplace) == CRABI_REPO:
            crabi_ids.append(pid)
    # installed crabi plugin name -> its plugin_id (to read install record / enabled flag).
    crabi_pid_by_name = {pid.rsplit("@", 1)[0]: pid for pid in crabi_ids}
    crabi_used = sum(_plugin_used(corpus, name) for name in crabi_pid_by_name)

    # Latest versions from the local marketplace clone (best-effort; {} if missing). Build the
    # breakdown over the UNION of catalog names + installed crabi plugins so not-installed
    # catalog plugins still show up. Falls back to installed-only when the catalog read misses.
    catalog = _load_marketplace_catalog(repo_slug=CRABI_REPO, claude_dir=claude_dir)
    names = sorted(set(catalog) | set(crabi_pid_by_name))
    breakdown = []
    for name in names:
        pid = crabi_pid_by_name.get(name)
        is_inst = pid is not None
        inst_ver = installed[pid].get("version") if is_inst else None
        latest_ver = catalog.get(name)
        breakdown.append({
            "name": name,
            "used": _plugin_used(corpus, name),
            "enabled": (bool(enabled.get(pid)) if is_inst else None),
            "installed": is_inst,
            "installed_version": inst_ver,
            "latest_version": latest_ver,
            "outdated": _version_outdated(inst_ver, latest_ver),
        })
    out[CRABI_REPO] = {
        "installed": bool(crabi_ids),
        "enabled": (any(enabled.get(pid) for pid in crabi_ids) if crabi_ids else None),
        "used": crabi_used,
        "plugins": sorted(crabi_pid_by_name),
        "plugin_breakdown": breakdown,   # per-plugin used / never-used (multi-channel)
    }

    # --- named tools via signature map ------------------------------------------------
    for target, sig in SIGNATURES.items():
        pid = sig.get("plugin_id")   # optional — absent for MCP-server targets
        used = 0

        rx = sig.get("mcp_tool_regex")
        if rx is not None:
            for tool, count in corpus.tool_usage.items():
                if rx.match(tool):
                    used += count
        ns = sig.get("namespace")
        if ns:
            used += _namespace_used(corpus, ns)
        cmd_rx = sig.get("cmd_regex")
        if cmd_rx is not None:
            used += _cmd_used(corpus, cmd_rx)
        # MCP-server USED: sum corpus.mcp_servers ids matching the regex (direct + connector).
        srv_rx = sig.get("mcp_server_regex")
        if srv_rx is not None:
            for sid, count in corpus.mcp_servers.items():
                if srv_rx.match(sid):
                    used += count

        if pid is not None:
            # Plugin-backed: installed/enabled/version from the config join.
            is_installed = pid in installed
            # Per-tool version + outdated marker. installed_version comes off the install
            # record; latest comes from the tool's marketplace clone, resolved by NAME (a
            # suggested tool only knows its marketplace name — the plugin_id suffix).
            installed_version = installed[pid].get("version") if is_installed else None
            mkt_name = pid.rsplit("@", 1)[1] if "@" in pid else None
            latest_version = _load_marketplace_catalog(
                marketplace=mkt_name, claude_dir=claude_dir).get(target) if mkt_name else None
            out[target] = {
                "installed": is_installed,
                "enabled": (bool(enabled.get(pid)) if is_installed else None),
                "used": used,
                "installed_version": installed_version,
                "latest_version": latest_version,
                "outdated": _version_outdated(installed_version, latest_version),
            }
        else:
            # MCP-server target: installed from ~/.claude.json; no enabled/version signal.
            srv_key = sig.get("mcp_server_key")
            out[target] = {
                "installed": srv_key in mcp_server_names,
                "enabled": None,
                "used": used,
                "installed_version": None,
                "latest_version": None,
                "outdated": None,
            }

    return out


# Redact machine-identifying home paths from free text before it is shown in the report or
# written to the evidence bundle. Applied only at PRESENTATION, never to the scored corpus,
# so scores stay byte-identical.
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/)[^/\s]+")
_WIN_HOME_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+")


def _scrub_paths(text):
    """/Users/<name>/x -> ~/x ; bare /Users/<name> -> ~ ; same for /home/<name> and Windows."""
    if not isinstance(text, str):
        return text
    text = _HOME_PATH_RE.sub("~", text)
    text = _WIN_HOME_RE.sub("~", text)
    return text


class Corpus:
    """Everything we measured from the transcripts, cleanly separated from scoring."""

    def __init__(self):
        self.files = 0
        self.projects = set()
        self.total_bytes = 0
        self.user_records = 0
        self.filtered = Counter()       # why user records were not counted as prompts
        self.real_prompts = []          # list of dicts: text, project, session, idx
        self.tool_usage = Counter()     # de-namespaced tool name -> count
        self.mcp_servers = Counter()    # mcp server-id (plugin infix stripped) -> call count
        self.mcp_plugins = Counter()    # mcp plugin token (mcp__plugin_<plugin>_…) -> call count
        self.commands = Counter()       # slash-command name -> count
        self.skill_invocations = Counter()  # Skill/Agent "<ns>:<name>" -> count
        self.total_tool_calls = 0
        self.delegation_events = 0
        self.first_ts = None
        self.last_ts = None
        self.cc_version = None          # Claude Code version off the latest event that carries one
        self._cc_version_ts = None      # ts of that event, so a later versioned event can win
        self.active_seconds = 0.0
        # Per-session ordered timelines of {"kind": "prompt"|"tool", ...}
        self.sessions = {}              # session_id -> {"project","timeline":[...]}


# Agent-to-agent transcripts (Claude Code subagents, Workflow runs) live under a
# ".../subagents/..." path. They are NOT the user's own prompts — counting them would
# contaminate the assessment and inflate counts every time a workflow is run — so they
# are excluded from discovery (an explicitly named single file is still honored).
_SUBAGENT_RE = re.compile(r"[/\\]subagents[/\\]")


def _filter_transcripts(paths):
    return [p for p in paths if not _SUBAGENT_RE.search(p)]


def discover_files(explicit):
    if explicit:
        p = os.path.expanduser(explicit)
        if os.path.isfile(p) and p.endswith(".jsonl"):
            return [p]
        if os.path.isdir(p):
            return _filter_transcripts(sorted(glob.glob(os.path.join(p, "**", "*.jsonl"), recursive=True)))
        return []
    env = os.environ.get("CLAUDE_PROJECTS_DIR")
    roots = [env] if env else DEFAULT_DIRS
    files = []
    for r in roots:
        rp = os.path.expanduser(r)
        if os.path.isdir(rp):
            files.extend(glob.glob(os.path.join(rp, "**", "*.jsonl"), recursive=True))
    return _filter_transcripts(sorted(set(files)))


def _dedupe_sessions(files):
    """When the same session shows up in more than one root (the live ~/.claude/projects dir
    AND the persistent archive — possibly under a since-renamed project folder, a different-case
    path, or a synced copy from another machine), keep a single copy of it: the largest one,
    since transcripts only ever grow, so the biggest file is the most complete. Claude Code
    session filenames are globally-unique IDs, so the filename alone identifies the session —
    keying on it (not the parent folder) is what makes the dedupe robust to all of the above."""
    best = {}
    for path in files:
        key = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        cur = best.get(key)
        if cur is None or size > cur[0]:
            best[key] = (size, path)
    return sorted(p for _, p in best.values())


def archive_transcripts(live_files, archive_dir):
    """Copy live transcripts into a persistent archive so they survive Claude Code's
    `cleanupPeriodDays` deletion. Each file is mirrored to
    <archive>/<project folder>/<session>.jsonl. We copy only when the archived copy is
    missing or strictly smaller than the live one (transcripts only grow, so a >= archive copy
    is the more complete one and must never be overwritten with a smaller/equal one). We write
    via a temp file + atomic replace, re-checking the archive size just before the swap so a
    concurrent run can't clobber a larger copy, and always clean up the temp file.
    Returns (n_new, n_updated); a stderr note is printed if any file could not be archived."""
    arch_root = os.path.expanduser(archive_dir)
    new = updated = failed = 0
    for path in live_files:
        project = os.path.basename(os.path.dirname(path)) or "default"
        dest_dir = os.path.join(arch_root, project)
        dest = os.path.join(dest_dir, os.path.basename(path))
        try:
            live_size = os.path.getsize(path)
        except OSError:
            continue
        arch_size = os.path.getsize(dest) if os.path.exists(dest) else -1
        if arch_size >= live_size:
            continue  # already archived an equal-or-more-complete copy
        tmp = dest + ".tmp"
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copyfile(path, tmp)
            # Another run may have grown the archive while we were copying — don't shrink it.
            current = os.path.getsize(dest) if os.path.exists(dest) else -1
            if current >= live_size:
                continue
            os.replace(tmp, dest)  # atomic; never leaves a half-written archive copy
        except OSError:
            failed += 1
            continue
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if arch_size < 0:
            new += 1
        else:
            updated += 1
    if failed:
        print(f"  Note: {failed} transcript(s) could not be archived to {archive_dir} "
              f"(check permissions / disk space). They were still analyzed from disk.",
              file=sys.stderr)
    return new, updated


def parse(files):
    c = Corpus()
    c.files = len(files)
    for path in files:
        project = os.path.basename(os.path.dirname(path)) or "default"
        c.projects.add(project)
        try:
            c.total_bytes += os.path.getsize(path)
        except OSError:
            pass
        session_id = os.path.splitext(os.path.basename(path))[0]
        timeline = []
        ts_in_file = []
        prompt_idx = 0
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(e.get("timestamp"))
                if ts:
                    ts_in_file.append(ts)
                    c.first_ts = ts if c.first_ts is None or ts < c.first_ts else c.first_ts
                    c.last_ts = ts if c.last_ts is None or ts > c.last_ts else c.last_ts
                # Keep the CC version off the LATEST event that actually carries one — ~28% of
                # events have no `version`, so reading the max-ts event would often miss it.
                v = e.get("version")
                if v and (c._cc_version_ts is None or (ts is not None and ts >= c._cc_version_ts)):
                    c.cc_version = v
                    c._cc_version_ts = ts if ts is not None else c._cc_version_ts
                msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                role = e.get("role") or msg.get("role") or e.get("type")
                content = msg.get("content", e.get("content"))

                if role == "assistant":
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                raw = b.get("name", "unknown")
                                srv = _mcp_server(raw)
                                if srv:
                                    c.mcp_servers[srv] += 1
                                plg = _mcp_plugin(raw)
                                if plg:
                                    c.mcp_plugins[plg] += 1
                                name = _denamespace_tool(raw)
                                c.tool_usage[name] += 1
                                c.total_tool_calls += 1
                                inp = b.get("input", {}) if isinstance(b.get("input"), dict) else {}
                                # Skill tool / Agent subagent_type carry a "<ns>:<name>" id — the
                                # USED signal for plugins driven via skills/subagents, not slash-commands.
                                sk = inp.get("skill") or inp.get("subagent_type")
                                if isinstance(sk, str) and sk:
                                    c.skill_invocations[sk] += 1
                                if name.lower() in DELEGATION_TOOLS:
                                    c.delegation_events += 1
                                if name.lower() == "bash" and inp.get("run_in_background"):
                                    c.delegation_events += 1
                                fpath = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                                cmd = inp.get("command") if name.lower() == "bash" else None
                                timeline.append({
                                    "kind": "tool", "name": name.lower(),
                                    "file": fpath, "cmd": cmd, "server": srv,
                                })
                    continue

                if role != "user":
                    continue
                c.user_records += 1
                if _is_tool_result(content):
                    c.filtered["tool results"] += 1
                    continue
                if e.get("isSidechain") is True:
                    c.filtered["subagent turns"] += 1
                    continue
                if e.get("isMeta") is True:
                    c.filtered["meta-injected"] += 1
                    continue
                raw_text = _text_of(content).strip()
                # Material the user pasted in (docs, logs, other chats) isn't something they
                # wrote; score only the words around it.
                text = _PASTED_RE.sub(" ", raw_text).strip()
                if not text:
                    c.filtered["injected / pasted" if raw_text else "empty"] += 1
                    continue
                cmd = _command_name(text)
                if cmd:
                    c.commands[cmd] += 1
                if _looks_injected(text):
                    c.filtered["injected / pasted"] += 1
                    continue
                # A genuine, human-typed prompt.
                prompt_idx += 1
                rec = {"text": text, "project": project, "session": session_id, "idx": prompt_idx}
                c.real_prompts.append(rec)
                timeline.append({"kind": "prompt", "text": text, "rec": rec})

        if len(ts_in_file) >= 2:
            ts_in_file.sort()
            c.active_seconds += sum(
                min((ts_in_file[i + 1] - ts_in_file[i]).total_seconds(), GAP_CAP_SECONDS)
                for i in range(len(ts_in_file) - 1)
            )
        if timeline:
            c.sessions[session_id] = {"project": project, "timeline": timeline}
    return c


# --------------------------------------------------------------------------- #
# Scoring helpers
# --------------------------------------------------------------------------- #

def squash(x, target):
    """Saturating curve: hitting `target` maxes the signal; exceeding adds nothing."""
    if target <= 0:
        return 0.0
    return max(0.0, min(1.0, x / target))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _run_fingerprint(corpus):
    """A stable hash of THIS run's de-contaminated prompt set. It binds an AI analysis
    (the Opus-stage skill map) to the exact data it was written from, so a stale or
    foreign ``analysis.json`` — e.g. left over from a previous run or another person on a
    machine that reuses the fixed ``~/.claude/insight/`` paths — carries a different
    fingerprint and is refused at merge time. This is what stops one person's written
    verdict from ever rendering inside someone else's report."""
    h = hashlib.sha256()
    for p in sorted(corpus.real_prompts, key=lambda r: (r["session"], r["idx"])):
        h.update(f"{p['session']}\x1f{p['idx']}\x1f{p['text']}\x1e".encode("utf-8"))
    h.update(f"|n={len(corpus.real_prompts)}".encode("utf-8"))
    return h.hexdigest()[:16]


def _is_action_prompt(text):
    return bool(ACTION_VERB.search(text))


def _is_ack(text):
    """A short go-ahead / continuation that answers the agent's own proposal."""
    return bool(ACK_RE.search(text)) and len(text.split()) <= ACK_MAX_WORDS


# --------------------------------------------------------------------------- #
# The five dimensions — each returns (score_0_100, detail_dict, evidence_list)
# --------------------------------------------------------------------------- #

def score_direction(corpus):
    prompts = corpus.real_prompts
    if not prompts:
        return 0.0, {"n": 0}, []
    # Briefing is about how you OPEN work, so go-aheads ("sí, hazlo", "sigue, página 26") are
    # left out of the denominator — being brief there is correct, not a weak brief.
    briefs = [p for p in prompts if not _is_ack(p["text"])] or prompts
    n = len(briefs)
    constraint = artifact = intent = 0
    weak_examples = []
    for p in briefs:
        t = p["text"]
        has_artifact = bool(ARTIFACT_RE.search(t))
        has_constraint = bool(CONSTRAINT_CUE.search(t) and ACTION_VERB.search(t))
        has_intent = bool(INTENT_CUE.search(t))
        artifact += 1 if has_artifact else 0
        constraint += 1 if has_constraint else 0
        intent += 1 if has_intent else 0
        if _is_action_prompt(t) and not (has_artifact or has_constraint or has_intent) and len(t) < 120:
            weak_examples.append(p)
    constraint_rate = constraint / n
    artifact_rate = artifact / n
    intent_rate = intent / n
    # front-loading: penalize rules first revealed via a high-info correction
    corr = _find_corrections(corpus)
    new_rule_corrections = sum(1 for x in corr if x["high_info"])
    action_prompts = max(1, sum(1 for p in briefs if _is_action_prompt(p["text"])))
    front_loading = 1 - clamp(new_rule_corrections / action_prompts, 0, 1)
    score = 100 * (
        0.30 * squash(constraint_rate, 0.45)
        + 0.20 * squash(artifact_rate, 0.45)
        + 0.25 * squash(intent_rate, 0.30)
        + 0.25 * front_loading
    )
    detail = {
        "n": n, "constraint_rate": constraint_rate, "artifact_rate": artifact_rate,
        "intent_rate": intent_rate, "front_loading": front_loading,
        "followups": len(prompts) - n,
    }
    return score, detail, weak_examples[:6]


def _iter_sessions(corpus):
    for sid, s in corpus.sessions.items():
        yield sid, s["project"], s["timeline"]


# Planning skills/commands surface in a timeline event's `name` as one of these tokens.
_PLAN_NAME_RE = re.compile(r"plan|research|brainstorm", re.I)
# Tools that spawn other agents — the "Delegate" work-type (a subset of DELEGATION_TOOLS:
# the plan-mode toggles aren't delegation work).
_DELEGATE_WORK = {"agent", "task", "workflow"}

WORK_TYPE_BUCKETS = ["Build", "Debug", "Plan", "Investigate", "Shell/Ops", "Delegate", "Other"]


def classify_work_types(corpus):
    """Work-type mix over every timeline TOOL event (prompts excluded — this measures
    actions, not chat).

    Pure post-parse: reads only `corpus.sessions[*]["timeline"]`, touches nothing scored.
    Each tool event lands in exactly one bucket, by already-present signals:
      * Build       — `name` is an edit tool (EDIT_TOOLS).
      * Debug       — `cmd` matches VERIFY_RE (test/build/run invocations).
      * Plan        — `name` reads as a planning skill/command (plan/research/brainstorm),
                      plus plan-ish slash-command invocations from `corpus.commands`.
      * Investigate — an MCP call (event has a `server`) or a read tool (READ_TOOLS):
                      observability queries + code reading.
      * Shell/Ops   — a Bash command that wasn't a Debug/verify run (git, file ops, …).
      * Delegate    — spawns a sub-agent/workflow (_DELEGATE_WORK).
      * Other        — everything else (web fetch/search, task mgmt, misc tooling).

    Returns {"counts": {bucket: int}, "pct": {bucket: float}}; pct is over all classified
    events and sums to ~100 (± rounding). Ponytail ceiling: coarse per-event heuristic;
    when an event matches more than one signal, precedence is the bucket order above.
    """
    counts = {b: 0 for b in WORK_TYPE_BUCKETS}
    for _sid, _project, timeline in _iter_sessions(corpus):
        for ev in timeline:
            if ev.get("kind") != "tool":          # work-type is about actions, not prompts
                continue
            name = (ev.get("name") or "")
            cmd = ev.get("cmd") or ""
            if name in EDIT_TOOLS:
                counts["Build"] += 1
            elif cmd and VERIFY_RE.search(cmd):
                counts["Debug"] += 1
            elif _PLAN_NAME_RE.search(name):
                counts["Plan"] += 1
            elif ev.get("server") or name in READ_TOOLS:
                counts["Investigate"] += 1
            elif name in _DELEGATE_WORK:
                counts["Delegate"] += 1
            elif name == "bash":
                counts["Shell/Ops"] += 1
            else:
                counts["Other"] += 1
    # Planning runs through slash commands (counted in corpus.commands), which never reach the
    # timeline — without this fold the Plan bucket reads ~0% on real data. ponytail: commands
    # aren't timeline events; counting plan-ish invocations is the cheap, parse()-free fix.
    for cmd, n in corpus.commands.items():
        if _PLAN_NAME_RE.search(cmd):
            counts["Plan"] += n
    total = sum(counts.values())
    pct = {b: (100.0 * counts[b] / total if total else 0.0) for b in WORK_TYPE_BUCKETS}
    return {"counts": counts, "pct": pct}


def _find_corrections(corpus):
    """Correction turns: short rejections that follow an assistant action, praise-guarded."""
    out = []
    for sid, project, timeline in _iter_sessions(corpus):
        saw_tool = False
        for ev in timeline:
            if ev["kind"] == "tool":
                saw_tool = True
                continue
            t = ev["text"]
            head = t[:160]
            if CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head) and saw_tool:
                high_info = bool(
                    re.search(r"\d", t) or ARTIFACT_RE.search(t) or len(t.split()) >= 8
                    or INTENT_CUE.search(t)
                )
                out.append({"session": sid, "project": project, "text": t, "high_info": high_info})
            saw_tool = False  # reset: correction must directly follow an action turn
    return out


def score_iteration(corpus):
    prompts = corpus.real_prompts
    n = len(prompts)
    corr = _find_corrections(corpus)
    k = len(corr)
    if n == 0:
        return 50.0, {"n": 0, "corrections": 0}, []
    rate = k / n
    specificity = (sum(1 for x in corr if x["high_info"]) / k) if k else 1.0
    score = 100 * (0.6 * (1 - clamp(rate / CORRECTION_RATE_CEILING, 0, 1)) + 0.4 * specificity)
    low_info = [x for x in corr if not x["high_info"]]
    # Confidence is keyed on prompt count n (the opportunity count), NOT correction count k:
    # a user with many clean prompts and zero corrections has STRONG evidence of good iteration,
    # so it must not be shrunk toward 50 as if it were "no data".
    detail = {"n": n, "corrections": k, "correction_rate": rate, "specificity": specificity}
    return score, detail, low_info[:4]


def score_context(corpus):
    total_edits = 0
    grounded = 0
    blind_examples = []
    for sid, project, timeline in _iter_sessions(corpus):
        read_paths = set()
        edited_paths = set()
        written_paths = set()   # files the agent authored this session (grounded to edit)
        for ev in timeline:
            if ev["kind"] != "tool":
                continue
            name, fpath = ev["name"], ev.get("file")
            if name in READ_TOOLS and fpath:
                read_paths.add(fpath)
            elif name in EDIT_TOOLS:
                total_edits += 1
                if not fpath:
                    grounded += 1  # can't attribute; don't penalize
                    continue
                is_new_write = (name == "write" and fpath not in read_paths and fpath not in edited_paths)
                # grounded if it was read, OR authored earlier this session, OR is being created now
                if fpath in read_paths or fpath in written_paths or is_new_write:
                    grounded += 1
                else:
                    blind_examples.append({"session": sid, "project": project, "file": fpath})
                if name == "write":
                    written_paths.add(fpath)
                edited_paths.add(fpath)
    if total_edits == 0:
        return 50.0, {"n": 0, "grounded": 0, "total_edits": 0, "rate": None}, []
    rate = grounded / total_edits
    score = 100 * squash(rate, 0.85)
    return score, {"n": total_edits, "grounded": grounded, "total_edits": total_edits, "rate": rate}, blind_examples[:4]


REVIEW_KINDS = ("checked", "specific_fix", "vague_reject", "rubber_stamp", "moved_on")


def review_replies(corpus):
    """Your first reply after the agent edited something, classified:

      * checked      — you report what you saw ("funciona", "the modal opens now", an error)
      * specific_fix — a correction that names what broke (you clearly looked)
      * vague_reject — "no", "hazlo de nuevo": you looked, but didn't say what failed
      * rubber_stamp — a bare "ok, sigue" with no sign you checked the result
      * moved_on     — a new instruction; neutral, not counted either way

    The agent's own builds and tests are scored elsewhere; this is the human half of the loop."""
    counts = Counter({k: 0 for k in REVIEW_KINDS})
    examples = {k: [] for k in REVIEW_KINDS}
    for sid, project, timeline in _iter_sessions(corpus):
        edited = False
        for ev in timeline:
            if ev["kind"] == "tool":
                edited = edited or ev["name"] in EDIT_TOOLS
                continue
            if not edited:
                continue
            edited = False
            t = ev["text"]
            head = t[:160]
            if CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head):
                specific = bool(re.search(r"\d", t) or ARTIFACT_RE.search(t) or len(t.split()) >= 8
                                or CHECKED_CUE.search(t))
                kind = "specific_fix" if specific else "vague_reject"
            elif CHECKED_CUE.search(head):
                kind = "checked"
            elif _is_ack(t):
                kind = "rubber_stamp"
            else:
                kind = "moved_on"
            counts[kind] += 1
            if len(examples[kind]) < 4:
                examples[kind].append({"session": sid, "project": project, "text": t})
    judged = counts["checked"] + counts["specific_fix"] + counts["vague_reject"] + counts["rubber_stamp"]
    looked = counts["checked"] + counts["specific_fix"]
    return {"counts": counts, "judged": judged,
            "rate": (looked / judged) if judged else None, "examples": examples}


REVIEW_MIN_N = 5          # below this many judged replies, Verification stays agent-only
REVIEW_WEIGHT = 0.30      # share of Verification that is YOUR review of the result


def score_verification(corpus):
    episodes = 0
    verified = 0
    teardown_bonus = 0
    unverified_examples = []
    for sid, project, timeline in _iter_sessions(corpus):
        open_ep = False
        ep_files = []
        for ev in timeline:
            if ev["kind"] == "prompt":
                # a "run it / does it work / confirm" prompt verifies an open episode
                if open_ep and re.search(r"\b(run it|does it work|confirm|check (it|that)|verify|did it work|"
                                         r"c[oó]rre(lo)?|pru[eé]ba(lo)?|verif[ií]ca(lo)?|comprueba|"
                                         r"chequea|revisa si|ya funciona|funciona\?)", ev["text"], re.I):
                    verified += 1
                    open_ep = False
                continue
            name = ev["name"]
            cmd = ev.get("cmd") or ""
            if name in EDIT_TOOLS:
                if not open_ep:
                    open_ep = True
                    episodes += 1
                    ep_files = []
                if ev.get("file"):
                    ep_files.append(os.path.basename(ev["file"]))
            elif name == "bash":
                if TEARDOWN_RE.search(cmd):
                    teardown_bonus = 5
                if open_ep and VERIFY_RE.search(cmd):
                    verified += 1
                    open_ep = False
            elif name in READ_TOOLS and open_ep and ev.get("file") and os.path.basename(ev["file"]) in ep_files:
                # re-reading the just-edited file is a (weak) check
                verified += 1
                open_ep = False
        if open_ep:
            unverified_examples.append({"session": sid, "project": project,
                                        "files": ", ".join(sorted(set(ep_files))[:3]) or "files"})
    if episodes == 0:
        return 50.0, {"n": 0, "episodes": 0, "verified": 0, "rate": None}, []
    rate = verified / episodes
    agent_score = min(100, 100 * squash(rate, 0.60) + teardown_bonus)
    # The agent runs builds and tests on its own, so a perfect agent_score says little about
    # YOU. Blend in whether you looked at the result before saying "ok, sigue".
    review = review_replies(corpus)
    score = agent_score
    if review["judged"] >= REVIEW_MIN_N:
        score = (1 - REVIEW_WEIGHT) * agent_score + REVIEW_WEIGHT * 100 * review["rate"]
    return score, {"n": episodes, "episodes": episodes, "verified": verified, "rate": rate,
                   "teardown_bonus": teardown_bonus, "agent_score": agent_score,
                   "review": {k: review[k] for k in ("counts", "judged", "rate")}}, unverified_examples[:4]


def score_toolcraft(corpus):
    total = corpus.total_tool_calls
    if total == 0:
        return 0.0, {"n": 0, "distinct": 0, "evenness": 0.0, "delegation_events": 0}, []
    # Collapse case-variant duplicates (e.g. "Bash" vs "bash") for an honest distinct count.
    merged = Counter()
    for name, cnt in corpus.tool_usage.items():
        merged[name.lower()] += cnt
    distinct = len(merged)
    breadth = squash(distinct / 20, 1.0)
    # Shannon evenness of the usage distribution.
    counts = list(merged.values())
    H = -sum((x / total) * math.log(x / total) for x in counts if x > 0)
    evenness = (H / math.log(distinct)) if distinct > 1 else 0.0
    active_hours = max(corpus.active_seconds / 3600, 0.5)
    delegation = squash(corpus.delegation_events / active_hours, 2.0)
    score = 100 * (0.45 * breadth + 0.30 * evenness + 0.25 * delegation)
    detail = {"n": total, "distinct": distinct, "evenness": evenness,
              "delegation_events": corpus.delegation_events}
    return score, detail, []


# --------------------------------------------------------------------------- #
# Aggregate: confidence shrinkage, overall score, band, archetype
# --------------------------------------------------------------------------- #

def shrink(score, n, target_n):
    c = min(1.0, n / target_n) if target_n else 1.0
    return 50 + (score - 50) * c, c


def band_for(score):
    for name, lo, hi, meaning in BANDS:
        if lo <= score <= hi:
            return name, meaning
    return BANDS[-1][0], BANDS[-1][3]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def classify_archetype(dim_scores, delegation_score):
    """Nearest-prototype over your DRIVING-STYLE vector, with a margin guard.

    The vector adds a Delegation axis and is AGENCY-WEIGHTED: axes you control
    (Direction, Iteration, Toolcraft, Delegation) count fully, while axes the agent
    mostly drives on its own (Verification, Context) are heavily discounted — so the
    archetype reflects how *you* drive, not Claude's built-in habits.
    """
    scores = dict(dim_scores)
    scores["Delegation"] = delegation_score
    V = [scores[ax] for ax in ARCHETYPE_AXES]
    names = list(PROTOTYPES.keys())
    mat = [PROTOTYPES[n]["vec"] for n in names]
    # z-score each axis across prototypes + the user vector, then apply agency weights
    cols = list(zip(*(mat + [V])))
    means = [statistics.mean(col) for col in cols]
    stds = [statistics.pstdev(col) or 1.0 for col in cols]
    w = [AGENCY[ax] for ax in ARCHETYPE_AXES]

    def zw(vec):
        return [w[i] * (v - means[i]) / stds[i] for i, v in enumerate(vec)]

    vz = zw(V)
    sims = sorted(((round(_cosine(vz, zw(PROTOTYPES[n]["vec"])), 3), n) for n in names), reverse=True)
    top_sim, top = sims[0]
    second_sim, second = sims[1]
    blended = (top_sim - second_sim) < ARCHETYPE_MARGIN
    second_short = second.replace("The ", "")
    article = "an" if second_short[:1] in "AEIOU" else "a"
    return {
        "primary": top, "primary_sim": top_sim, "secondary": second, "secondary_sim": second_sim,
        "blended": blended, "all": sims, "delegation_score": round(delegation_score),
        "label": f"{PROTOTYPES[top]['emoji']} {top}" + (f", with {article} {second_short} streak" if blended else ""),
        "blurb": PROTOTYPES[top]["blurb"],
    }


# --------------------------------------------------------------------------- #
# Analysis orchestration
# --------------------------------------------------------------------------- #

def analyze(corpus):
    raw, detail, evidence = {}, {}, {}
    for name, fn in (("Direction", score_direction), ("Verification", score_verification),
                     ("Context", score_context), ("Iteration", score_iteration),
                     ("Toolcraft", score_toolcraft)):
        s, d, ev = fn(corpus)
        raw[name], detail[name], evidence[name] = s, d, ev

    shrunk, conf = {}, {}
    for name in raw:
        shrunk[name], conf[name] = shrink(raw[name], detail[name].get("n", 0), TARGET_N[name])

    overall_raw = round(sum(WEIGHTS[n] * raw[n] for n in WEIGHTS))
    overall = round(sum(WEIGHTS[n] * shrunk[n] for n in WEIGHTS))
    band, band_meaning = band_for(overall)
    # Delegation is a user-driven archetype axis (handoffs per active hour).
    active_hours = max(corpus.active_seconds / 3600, 0.5)
    delegation_score = 100 * squash(corpus.delegation_events / active_hours, 2.0)
    archetype = classify_archetype(shrunk, delegation_score)

    # length distribution of real prompts (context only)
    lens = [len(p["text"]) for p in corpus.real_prompts]
    words = [len(p["text"].split()) for p in corpus.real_prompts]
    dist = {}
    if lens:
        dist = {
            "median_chars": int(statistics.median(lens)),
            "mean_chars": int(statistics.mean(lens)),
            "median_words": int(statistics.median(words)),
            "under_80_pct": round(100 * sum(1 for L in lens if L < 80) / len(lens)),
        }

    return {
        "raw": raw, "shrunk": shrunk, "conf": conf, "detail": detail, "evidence": evidence,
        "overall_raw": overall_raw, "overall": overall, "band": band, "band_meaning": band_meaning,
        "archetype": archetype, "dist": dist, "fingerprint": _run_fingerprint(corpus),
    }


def build_action_plan(corpus, result):
    """Growth cards ranked by impact = (target - score) * weight. The teaching copy
    comes from SKILL_TEACH; user-specific evidence comes from result['evidence']."""
    TARGET = 85
    cards = []
    for name in WEIGHTS:
        score = result["shrunk"][name]
        impact = (TARGET - score) * WEIGHTS[name]
        cards.append({"dim": name, "score": round(score), "impact": impact,
                      "weak": result["evidence"].get(name, []),
                      "detail": result["detail"][name]})
    cards.sort(key=lambda c: c["impact"], reverse=True)
    # strength callout = highest shrunk score
    strength = max(WEIGHTS, key=lambda n: result["shrunk"][n])
    return cards, strength


def _shortest_action_prompt(corpus):
    cands = [p["text"] for p in corpus.real_prompts if _is_action_prompt(p["text"]) and len(p["text"]) < 40]
    return min(cands, key=len) if cands else None


def build_evidence(corpus, result, cards, archive_info=None):
    """Serialize a de-contaminated EVIDENCE bundle for the two-model analysis pipeline
    (Sonnet 4.6 explores it; Opus 4.8 analyzes it against the bundled AI-fluency
    framework). It contains your real prompts/behavior with home paths scrubbed, and is
    git-ignored. Deterministic (no randomness) so runs are reproducible."""
    prompts = corpus.real_prompts
    sample, seen = [], set()

    def add(p):
        k = (p["session"], p["idx"])
        if k in seen:
            return
        seen.add(k)
        sample.append({"text": _scrub_paths(p["text"][:600]), "project": _project_label(p["project"]),
                       "chars": len(p["text"])})

    by_len = sorted(prompts, key=lambda p: len(p["text"]))
    for p in by_len[:6]:                 # the terse nudges
        add(p)
    for p in by_len[-14:]:               # the rich, intent-carrying prompts
        add(p)
    stride = max(1, len(prompts) // 20)  # an even spread through the timeline
    for p in prompts[::stride]:
        if len(sample) >= 50:
            break
        add(p)

    def clean_ex(items):
        out = []
        for e in items or []:
            if not isinstance(e, dict):
                continue
            c = {}
            if e.get("text"):
                c["text"] = _scrub_paths(str(e["text"])[:300])
            if e.get("file"):
                c["file"] = os.path.basename(str(e["file"]))
            if e.get("files"):
                c["files"] = str(e["files"])
            if e.get("project"):
                c["project"] = _project_label(e["project"])
            if c:
                out.append(c)
        return out

    span_days = (corpus.last_ts - corpus.first_ts).days if corpus.first_ts and corpus.last_ts else 0
    a = result["archetype"]
    return {
        "schema": "claude-insight-evidence/1",
        "meta": {
            "sessions": corpus.files, "projects": len(corpus.projects),
            "real_prompts": len(prompts), "user_records": corpus.user_records,
            "filtered_noise": dict(corpus.filtered),
            "span_days": span_days,
            "active_hours": round(corpus.active_seconds / 3600, 1),
            "archive": archive_info,
            "prompt_distribution": result["dist"],
            # Binds any analysis produced from this bundle back to this exact run; the
            # analysis stage must echo it so a stale/foreign analysis can be refused.
            "run_fingerprint": result.get("fingerprint"),
        },
        "scores": {
            "overall": result["overall"], "overall_raw": result["overall_raw"],
            "band": result["band"], "weights": WEIGHTS,
            "dimensions_raw": {k: round(v) for k, v in result["raw"].items()},
            "dimensions_adjusted": {k: round(v) for k, v in result["shrunk"].items()},
            "confidence": {k: round(v, 2) for k, v in result["conf"].items()},
            "dimension_names": DISPLAY_NAMES,
        },
        "dimension_detail": result["detail"],
        "archetype": {"primary": a["primary"], "secondary": a["secondary"],
                      "blended": a.get("blended")},
        "behavior": {
            "sample_prompts": sample,
            "weak_examples": {c["dim"]: clean_ex(c["weak"]) for c in cards},
            "tool_usage": dict(corpus.tool_usage),
            "delegation_events": corpus.delegation_events,
            # Additive, descriptive usage/adoption views — outside run_fingerprint (which
            # hashes only real_prompts), so they never affect the merge-gate. The coaching
            # nudge in .claude/workflows/ai-fluency.js reads behavior.adoption from here.
            "usage": {
                "mcp_servers": dict(corpus.mcp_servers),
                "commands": dict(corpus.commands),
                "work_types": classify_work_types(corpus),
            },
            "adoption": build_adoption(corpus),
        },
    }


def _analysis_section_html(analysis):
    """Render the AI-authored skill map (produced by the Opus analysis stage,
    grounded in reference/ai-fluency-framework.md). Falls back to nothing if absent."""
    if not analysis or not isinstance(analysis, dict):
        return ""
    parts = ['<section><h3>Skill map — analyzed against the AI Fluency framework</h3>']
    read = analysis.get("overall_read") or analysis.get("summary")
    if read:
        parts.append(f'<p class="assess">{_esc(read)}</p>')
    for s in analysis.get("skill_map") or []:
        if not isinstance(s, dict):
            continue
        comp = _esc(s.get("competency", "?"))
        lvl = s.get("level", "?")
        label = _esc(s.get("level_label", ""))
        summ = _esc(s.get("summary", ""))
        nxt = _esc(s.get("next_move", ""))
        ev = "".join(f"<li>“{_esc(str(x)[:200])}”</li>" for x in (s.get("evidence") or [])[:3])
        parts.append(
            f'<div class="dim"><div class="dim-h"><b>{comp}</b>'
            f'<span class="pill">Level {_esc(lvl)}/5 · {label}</span></div>'
            f'<p>{summ}</p>'
            + (f'<ul class="ev">{ev}</ul>' if ev else "")
            + (f'<p class="next"><b>Your next move:</b> {nxt}</p>' if nxt else "")
            + '</div>')
    strengths = analysis.get("strengths") or []
    if strengths:
        items = "".join(f"<li>{_esc(s)}</li>" for s in strengths[:5])
        parts.append(f'<p style="margin-top:14px"><b>What you already do well:</b></p><ul class="facts">{items}</ul>')
    parts.append('<p style="color:var(--mut);font-size:13px;margin-top:10px">'
                 'This section is written by Claude Opus 4.8 from your de-contaminated evidence '
                 '(explored by Claude Sonnet 4.6), grounded in the bundled AI Fluency framework. '
                 'The numbers above are computed deterministically and independently.</p>')
    parts.append('</section>')
    return "".join(parts)


def _server_display(server):
    """(display-name, category) for an mcp server-id; falls back to the raw id / '—'."""
    name, cat = SERVER_DISPLAY.get(server, (server, "—"))
    return name, cat


# Friendly labels for the adoption targets (the crabi marketplace key is a repo slug).
_ADOPTION_LABEL = {
    CRABI_REPO: "Crabi marketplace",
    "engram": "Engram (memory)",
    "ponytail": "Ponytail (code-quality)",
    "obsidian": "Obsidian (notes)",
    "desplega": "Desplega (toolbox)",
    "context7": "Context7",
}


def _outdated_badge(state):
    """An 'update available · vX → vY' badge when `state["outdated"]` is True; '' otherwise.

    Renders no version claim when outdated is None (unknown) or False (current) — the report
    never asserts up-to-date / outdated unless the versions actually compared as behind."""
    if not isinstance(state, dict) or state.get("outdated") is not True:
        return ""
    iv = state.get("installed_version") or "?"
    lv = state.get("latest_version") or "?"
    return f'<span class="tag w">update available · v{_esc(iv)} → v{_esc(lv)}</span>'


def _usage_section_html(usage, adoption, span_days):
    """One combined 'Usage & Adoption' section, mirroring _analysis_section_html.

    Renders four blocks over the REAL measured window (span_days, an honest count):
      * MCP calls grouped by server (display names from SERVER_DISPLAY),
      * top slash commands by count,
      * the Build/Debug/Plan/Other work-type mix as bars,
      * per-target installed/enabled/used adoption badges, visually emphasizing the
        installed-but-never-used case (the strongest coaching signal).
    All dynamic text is escaped via _esc. Returns '' only on a fully empty payload."""
    if not usage and not adoption:
        return ""
    usage = usage or {}
    span = f"over the last {span_days} days" if span_days else "over your history"

    parts = ['<section><h3>Usage &amp; Crabi-tool adoption</h3>']

    # --- MCP calls by server --------------------------------------------------------
    servers = (usage.get("mcp_servers") or Counter())
    if servers:
        rows = ""
        top = servers.most_common(1)[0][1] or 1
        for server, n in servers.most_common(12):
            name, cat = _server_display(server)
            rows += (f'<div class="bar-item"><div class="bl">{_esc(name)} '
                     f'<span class="loc">{_esc(cat)}</span></div>'
                     f'<div class="bt"><i style="width:{100 * n / top:.0f}%"></i></div>'
                     f'<div class="bv">{n:,}</div></div>')
        parts.append(f'<p class="def">MCP-server calls {_esc(span)}:</p>{rows}')
    else:
        parts.append(f'<p class="rate">No MCP-server calls {_esc(span)}.</p>')

    # --- Slash commands -------------------------------------------------------------
    commands = (usage.get("commands") or Counter())
    if commands:
        items = "".join(
            f"<li><b>{n:,}</b> {_esc(cmd)}</li>" for cmd, n in commands.most_common(10)
        )
        parts.append('<p class="def" style="margin-top:14px">Top slash commands '
                     f'{_esc(span)}:</p><ul class="facts">{items}</ul>')

    # --- Work-type mix --------------------------------------------------------------
    wt = usage.get("work_types") or {}
    pct = wt.get("pct") or {}
    if pct:
        bars = ""
        for bucket in WORK_TYPE_BUCKETS:
            p = pct.get(bucket, 0.0)
            n = (wt.get("counts") or {}).get(bucket, 0)
            # A real-but-tiny share must not read "0%" (e.g. 60 subagent calls among 35k).
            label = "<1%" if 0 < p < 0.5 else f"{p:.0f}%"
            bars += (f'<div class="bar-item"><div class="bl">{_esc(bucket)}</div>'
                     f'<div class="bt"><i style="width:{p:.0f}%"></i></div>'
                     f'<div class="bv" title="{n:,} calls">{_esc(label)}</div></div>')
        parts.append(f'<p class="def" style="margin-top:14px">Work-type mix {_esc(span)}:</p>{bars}')

    # --- Adoption: two sub-sections --------------------------------------------------
    if adoption:
        # (1) Crabi suggested plugins — the named-tool targets as aggregate badge rows.
        parts.append('<p class="def" style="margin-top:18px">'
                     '<b>Crabi suggested plugins</b> — the tools we recommend leaning on:</p>')
        for target in SIGNATURES:
            state = adoption.get(target)
            if not isinstance(state, dict):
                continue
            label = _ADOPTION_LABEL.get(target, target)
            installed = state.get("installed")
            enabled = state.get("enabled")
            used = state.get("used", 0)
            badges = []
            badges.append('<span class="tag s">installed</span>' if installed
                          else '<span class="tag ld">not installed</span>')
            if installed:
                if enabled is True:
                    badges.append('<span class="tag s">enabled</span>')
                elif enabled is False:
                    badges.append('<span class="tag w">disabled</span>')
            # The coaching signal: installed but never invoked.
            if installed and not used:
                badges.append('<span class="tag w">never used</span>')
            elif used:
                badges.append(f'<span class="tag ld">used {used:,}×</span>')
            badges.append(_outdated_badge(state))
            unused = installed and not used
            row_cls = ' style="border-left:4px solid var(--warn)"' if unused else ""
            parts.append(
                f'<div class="dim"{row_cls}><div class="top">'
                f'<span class="name">{_esc(label)} {"".join(b for b in badges if b)}</span></div>'
                + ('<p class="rate">Installed but never used — a tool you could lean on.</p>'
                   if unused else
                   ('<p class="rate">Not installed — install it to unlock this workflow.</p>'
                    if not installed else ""))
                + '</div>')

        # (2) Crabi AI marketplace — every catalog plugin, status + outdated marker.
        crabi = adoption.get(CRABI_REPO)
        bd = (crabi.get("plugin_breakdown") if isinstance(crabi, dict) else None) or []
        if bd:
            parts.append('<p class="def" style="margin-top:18px">'
                         '<b>Crabi AI marketplace</b> — every plugin in the catalog:</p>')
            for p in sorted(bd, key=lambda x: (not x.get("installed"), -x.get("used", 0), x["name"])):
                p_installed = p.get("installed")
                p_used = p.get("used", 0)
                if not p_installed:
                    status = '<span class="tag ld">not installed</span>'
                    note = "not installed"
                elif p_used:
                    status = f'<span class="tag s">installed · used {p_used:,}×</span>'
                    note = ""
                else:
                    status = '<span class="tag w">installed · idle</span>'
                    note = "installed but idle"
                out_badge = _outdated_badge(p)
                row_cls = ' style="border-left:4px solid var(--warn)"' if (p_installed and not p_used) else ""
                parts.append(
                    f'<div class="dim"{row_cls}><div class="top">'
                    f'<span class="name">{_esc(p["name"])} {status}{out_badge}</span></div>'
                    + (f'<p class="rate">{_esc(note)}</p>' if note else "")
                    + '</div>')

    parts.append('<p style="color:var(--mut);font-size:13px;margin-top:10px">'
                 'Raw counts over your real measured window — not normalized. Adoption is read '
                 'live from your local plugin config; nothing left this machine.</p>')
    parts.append('</section>')
    return "".join(parts)


def _growth_cards_html(analysis):
    """The 'how to grow' cards, written FOR THIS PERSON by the Opus analysis stage:
    each item names the habit, why it matters, how to grow it, and a before/after where
    the 'before' is one of THEIR real prompts and the 'after' is Opus's tailored rewrite.
    Returns '' when there is no analysis (the caller then falls back to the generic
    teaching examples), so the report only ever shows canned examples when no AI ran."""
    if not analysis or not isinstance(analysis, dict):
        return ""
    items = [g for g in (analysis.get("top_growth") or []) if isinstance(g, dict)]
    if not items:
        return ""
    out = []
    for i, g in enumerate(items[:3]):
        title = _esc(g.get("title", "Your next growth move"))
        why = _esc(g.get("why", ""))
        how = _esc(g.get("how", ""))
        before = g.get("example_before")
        after = g.get("example_after")
        ba = ""
        if before and after:
            ba = (f'<div class="ba"><div class="before"><span>A prompt you wrote</span>'
                  f'“{_esc(str(before)[:400])}”</div>'
                  f'<div class="after"><span>Tailored rewrite for you</span>'
                  f'“{_esc(str(after)[:600])}”</div></div>')
        out.append(
            f'<div class="card prio"><div class="ph">Priority {i + 1} · written for you</div>'
            f'<h4>{title}</h4>'
            + (f'<p class="why"><b>Why it matters.</b> {why}</p>' if why else "")
            + (f'<div class="wwh"><span class="lab">How to grow it</span>'
               + (f'<p class="how">{how}</p>' if how else "") + f'{ba}</div>'
               if (how or ba) else "")
            + '</div>')
    return "".join(out)


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def _project_label(name):
    """Claude encodes an absolute path with '-' for '/', so we can't perfectly
    recover hyphenated names. Drop the home/boilerplate prefix and show the rest.
    '-Users-me-Dropbox-AI-platzi-executive-assistant' -> 'AI platzi executive assistant'."""
    s = re.sub(r"^[A-Za-z]--", "-", name)                    # Windows drive: 'C--Users-me-…' / 'C--Dev-…'
    s = re.sub(r"^-?(?:Users|home)-[^-]+(?:-|$)", "", s)     # strip -Users-/-home-<user>- (mac, linux, win)
    s = re.sub(r"^-", "", s)
    s = re.sub(r"^Dropbox-", "", s)                          # strip a common cloud-folder prefix
    s = s.replace("-", " ").strip()
    # Nothing left -> the session ran in $HOME itself; never echo the raw name (it holds the username).
    if not s:
        return "home" if re.match(r"^-?(?:Users|home)-", name) else name
    return s


def terminal_summary(corpus, result):
    a = result["archetype"]
    lines = [
        "",
        f"  AI Fluency Score: {result['overall']}/100  ({result['band']})",
        f"  Archetype: {a['label']}",
        f"  Based on {len(corpus.real_prompts)} real prompts across {len(corpus.projects)} projects, "
        f"{corpus.files} sessions ({corpus.total_bytes/1e6:.1f} MB).",
        "",
    ]
    return "\n".join(lines)


def _esc(s):
    return html.escape(str(s))


# Each archetype's encouraging "next gain" — frames the top growth lever as a natural
# progression for that style rather than a deficit.
ARCH_PATHS = {
    "Autonomous Agent": "You already hand off whole jobs well — add one sharp sentence of intent per hand-off and far more will land right the first time, with less back-and-forth.",
    "Architect": "Your planning is a real strength — pair it with a quick check after each change so your designs ship proven, not just drawn.",
    "Debugger": "Your diagnostic discipline is excellent — capture each fix as a small reusable rule so the same bug never costs you twice.",
    "Collaborator": "Your back-and-forth keeps things aligned — front-loading a constraint or two will get you there in fewer rounds.",
    "Sprinter": "Your speed is real — a one-line brief plus a quick test keeps that speed from turning into rework.",
}

_SIG_DESC = {
    "Delegation": "how much you hand off — you give Claude whole jobs and trust it to run them end-to-end",
    "Toolcraft": "the range of tools you bring to bear — you reach past the shell for the right instrument",
    "Iteration": "how cleanly you change course — your corrections tend to name the fix, not just reject",
    "Briefing": "how concretely you frame requests when it matters",
}

# The specific, evidence-grounded line that explains each dimension as a growth edge.
_GROWTH_LINE = {
    "Direction": "{s}s win on how sharply they frame the work they hand off — and right now yours are often one-liners like “{ex}”, so Claude fills gaps you could have decided.",
    "Verification": "Right now changes often move on without a test, build or run to confirm them — the cheapest reliability you can buy back.",
    "Context": "Right now some edits land before the file has been read that session — an easy blind-edit risk to remove.",
    "Iteration": "Right now corrections lean toward brief rejections; naming the symptom and the exact rule resolves loops in fewer turns.",
    "Toolcraft": "Right now most work funnels through one tool — reaching for search, planning and delegation widens what you can take on.",
}


def build_assessment(corpus, result, cards):
    """A coherent, professional written read — synthesizes the numbers into one story
    and explicitly resolves the archetype-vs-weakest-dimension tension."""
    a = result["archetype"]
    arch = a["primary"]
    short = arch.replace("The ", "")
    art = "an" if short[:1] in "AEIOU" else "a"
    deleg = a["delegation_score"]
    n_deleg = corpus.delegation_events
    median = result["dist"].get("median_chars", "?")

    # signature strength = your strongest USER-driven signal (not Claude's defaults)
    user_signals = {
        "Briefing": result["shrunk"]["Direction"], "Iteration": result["shrunk"]["Iteration"],
        "Toolcraft": result["shrunk"]["Toolcraft"], "Delegation": float(deleg),
    }
    sig = max(user_signals, key=user_signals.get)

    growth = cards[0]["dim"]
    growth_disp = disp(growth)
    example = _shortest_action_prompt(corpus) or "run it"
    path_why = ARCH_PATHS.get(arch, "Keep building the habits below and your next run will show the gain.")

    p1 = (f"You drive Claude like <b>{_esc(a['label'])}</b>. {_esc(a['blurb'])} "
          f"The clearest signal is your delegation rate — <b>{deleg}/100</b>, from {n_deleg} hand-offs to "
          f"subagents, background jobs and planning — paired with fast, terse prompts (median "
          f"{median} characters).")

    # Only credit the read→edit→verify loop when the data actually shows it; otherwise this
    # clause was claiming a discipline some users' own report contradicts (0% verified / grounded).
    loop_ok = result["shrunk"]["Context"] >= 55 and result["shrunk"]["Verification"] >= 55
    p2_mid = ("That, plus the disciplined read→edit→verify loop your sessions show, is"
              if loop_ok else "That is")
    p2 = (f"Your strongest <i>self-driven</i> habit is {_esc(_SIG_DESC.get(sig, sig.lower()))}. "
          f"{p2_mid} why your overall score lands at <b>{result['overall']}/100 ({_esc(result['band'])})</b>.")

    gline = _GROWTH_LINE.get(growth, "").format(s=_esc(short), ex=_esc(example))
    p3 = (f"And the apparent tension, resolved: your lowest dimension is <b>{_esc(growth_disp)}</b> — but for "
          f"{art} {_esc(short)} that isn't a contradiction, it's the <i>defining</i> growth edge. {gline} "
          f"{_esc(path_why)}")

    return (f'<p class="assess">{p1}</p><p class="assess">{p2}</p><p class="assess">{p3}</p>')


def build_html(corpus, result, cards, strength, archive_info=None, analysis=None, analysis_note=None,
               usage=None, adoption=None, generated_at=None):
    # Testability seam: tests pass a fixed datetime; the real run defaults to now.
    # (Phase 2 renders it; Phase 1 only threads it.)
    generated_at = generated_at or datetime.now()
    a = result["archetype"]
    d = result["dist"]
    analysis_section = _analysis_section_html(analysis)
    # When an AI analysis was expected but couldn't be used (no-op'd, empty, or from a
    # different run), say so plainly instead of letting the template-only report pass as
    # the full thing. Silent on a plain deterministic run (no --analysis was attempted).
    analysis_status_html = ""
    if not analysis_section and analysis_note:
        analysis_status_html = (
            '<section><div class="prov">ℹ️ <b>Deterministic report only.</b> '
            f'{_esc(analysis_note)} — the Sonnet&nbsp;+&nbsp;Opus skill map was not added on top. '
            'The scores, archetype and dimensions below are still fully computed from your data; '
            'to add the AI-written skill map, re-run <code>/ai-fluency</code> inside Claude Code.'
            '</div></section>')
    days = (corpus.last_ts - corpus.first_ts).days if corpus.first_ts and corpus.last_ts else 0
    # Usage & adoption block: honest span label uses the same measured `days` as the data tile.
    usage_section = _usage_section_html(usage, adoption, days)
    active_h = corpus.active_seconds / 3600
    provisional = len(corpus.real_prompts) < PROVISIONAL_MIN_PROMPTS

    DIM_BLURB = {
        "Direction": "How clearly you tell the agent what you want before it acts.",
        "Verification": "Whether changes get checked (tests / build / app) — and whether you look at the result before saying “ok, next”.",
        "Context": "Reading a file before editing it — grounded, not blind, changes.",
        "Iteration": "Correcting precisely instead of thrashing with vague rejections.",
        "Toolcraft": "Using a healthy range of tools — not forcing everything through one.",
    }

    def dim_rate_line(name):
        det = result["detail"][name]
        if name == "Verification" and det.get("rate") is not None:
            line = f"agent verified {det['verified']} of {det['episodes']} edit-bursts ({det['rate']*100:.0f}%)"
            rv = det.get("review") or {}
            if rv.get("judged"):
                rs = rv["counts"]["rubber_stamp"]
                line += (f" · you checked the result in {rv['rate']*100:.0f}% of {rv['judged']} replies "
                         f"({rs} bare “ok, sigue”)")
            return line
        if name == "Context" and det.get("rate") is not None:
            return f"{det['grounded']} of {det['total_edits']} edits were grounded in a prior read ({det['rate']*100:.0f}%)"
        if name == "Direction":
            line = (f"{det['constraint_rate']*100:.0f}% carry a constraint · "
                    f"{det['artifact_rate']*100:.0f}% name a file/error · {det['intent_rate']*100:.0f}% state a why")
            if det.get("followups"):
                line += f" · {det['n']} task prompts ({det['followups']} go-aheads like “sí, hazlo” left out)"
            return line
        if name == "Iteration":
            return f"{det['corrections']} correction turns ({det['correction_rate']*100:.0f}% of prompts); {det['specificity']*100:.0f}% were specific"
        if name == "Toolcraft":
            return f"{det.get('distinct', 0)} distinct tools, evenness {det.get('evenness', 0.0):.2f}, {det.get('delegation_events', 0)} delegations"
        return ""

    # dimension bars
    dim_html = ""
    order = sorted(WEIGHTS, key=lambda n: result["shrunk"][n], reverse=True)
    for name in order:
        sc = round(result["shrunk"][name])
        raw_sc = round(result["raw"][name])
        c = result["conf"][name]
        lowdata = c < 0.75
        tag = ""
        if name == strength:
            tag = '<span class="tag s">Strength</span>'
        elif name == cards[0]["dim"]:
            tag = '<span class="tag w">Top growth lever</span>'
        ld = '<span class="tag ld">low data</span>' if lowdata else ""
        # Quality tier drives the fill color (blue high / violet mid / orange low) — a
        # colorblind-safe ramp that rides alongside the bar length, the /100 number and
        # the text tag, so quality is never read from color alone.
        fill_cls = "" if sc >= 60 else ("mid" if sc >= 40 else "lo")
        dim_html += f"""
      <div class="dim">
        <div class="top"><span class="name">{_esc(disp(name))} {tag}{ld}</span><span class="sval">{sc}<span class="hint">/100</span></span></div>
        <div class="bar"><i class="{fill_cls}" style="width:{sc}%"></i></div>
        <p class="def">{_esc(DIM_BLURB[name])}</p>
        <p class="rate">{_esc(dim_rate_line(name))}<span class="wt"> · weight {int(WEIGHTS[name]*100)}%</span></p>
      </div>"""

    # archetype affinity
    aff = ""
    for sim, nm in a["all"]:
        pct = max(0, round((sim + 1) / 2 * 100))
        aff += f"""<div class="bar-item"><div class="bl">{PROTOTYPES[nm]['emoji']} {_esc(nm)}</div>
          <div class="bt"><i style="width:{pct}%"></i></div><div class="bv">{sim:+.2f}</div></div>"""

    # Archive stat tile (we only analyze what Claude Code keeps on disk — ~30 days).
    archive_tile = ""
    if archive_info:
        archive_tile = (f'<div class="ing"><div class="n">{archive_info["archived_sessions"]:,}</div>'
                        f'<div class="l">sessions in your archive</div></div>')

    # action cards (what/where/how)
    def evidence_html(card):
        name = card["dim"]
        ev = card["weak"]
        if not ev:
            return '<p class="ev-none">No clear examples in your transcripts — this is already a habit. ✓</p>'
        items = ""
        # small-sample guard per project
        proj_counts = Counter(p["project"] for p in corpus.real_prompts)
        for e in ev[:3]:
            if name == "Direction" or name == "Iteration":
                proj = e["project"]; txt = _scrub_paths(e["text"])
                small = " <em>(illustrative, small sample)</em>" if proj_counts.get(proj, 0) < 10 else ""
                items += f'<li>“{_esc(txt[:140])}” <span class="loc">— {_esc(_project_label(proj))}{small}</span></li>'
            elif name == "Context":
                small = " <em>(illustrative)</em>" if proj_counts.get(e["project"], 0) < 10 else ""
                items += f'<li>Edited <code>{_esc(os.path.basename(e["file"]))}</code> without reading it first <span class="loc">— {_esc(_project_label(e["project"]))}{small}</span></li>'
            elif name == "Verification":
                small = " <em>(illustrative)</em>" if proj_counts.get(e["project"], 0) < 10 else ""
                items += f'<li>A burst of edits to <code>{_esc(e["files"])}</code> with nothing run afterwards <span class="loc">— {_esc(_project_label(e["project"]))}{small}</span></li>'
        return f"<ul class='ev'>{items}</ul>"

    cards_html = ""
    for i, card in enumerate(cards[:2]):
        name = card["dim"]
        t = SKILL_TEACH[name]
        # These before/after pairs are a fixed teaching library, identical for every user
        # with this weak dimension — they are NOT drawn from this person's transcripts.
        # Label them as such so they can never be mistaken for a personalized rewrite (the
        # personalized signal is the "Where this shows up in your sessions" block above).
        ex_html = "".join(
            f'<div class="ba"><div class="before"><span>Instead of</span>“{_esc(e["before"])}”</div>'
            f'<div class="after"><span>Stronger</span>“{_esc(e["after"])}”</div></div>'
            for e in t["examples"]
        )
        cards_html += f"""
      <div class="card prio">
        <div class="ph">Priority {i+1} · {_esc(disp(name))} <span class="pscore">now {card['score']}/100</span></div>
        <h4>{_esc(t['what_it_is'])}</h4>
        <p class="why"><b>Why it matters.</b> {_esc(t['why_it_matters'])}</p>
        <div class="wwh"><span class="lab">Where this shows up in your sessions</span>{evidence_html(card)}</div>
        <div class="wwh"><span class="lab">How to grow it</span><p class="how">{_esc(t['how_to_improve'])}</p>
          <p class="exgen">Generic illustrations of the habit — <b>not</b> from your sessions:</p>
          {ex_html}
        </div>
        <p class="tgt">🎯 Try this next session: {_esc(t['practice'])}</p>
      </div>"""

    # strength callout — lead with the user's signature (self-driven) strength.
    # Floor the praise: if even the best dimension is weak, frame it as "relatively
    # strongest" rather than asserting a mastered habit the rate-line would contradict.
    s_det = dim_rate_line(strength)
    strong_score = round(result["shrunk"][strength])
    if strong_score >= 55:
        strength_head = "Keep doing this"
        strength_body = (f"{_esc(SKILL_TEACH[strength]['good_looks_like'])} The evidence in your "
                         f"sessions: {_esc(s_det)}. This is your foundation — build on it.")
    else:
        strength_head = "Your relatively strongest area"
        strength_body = (f"Even your strongest dimension has room to grow ({strong_score}/100), but this "
                         f"is the most natural place to build from. The evidence in your sessions: "
                         f"{_esc(s_det)}.")
    strength_html = f"""
      <div class="card keep">
        <div class="ph">{strength_head} · {_esc(disp(strength))} <span class="pscore">{strong_score}/100</span></div>
        <p>{strength_body}</p>
      </div>"""

    # skill map (levels)
    skill_levels = _skill_levels(result)
    skill_html = ""
    for sk in skill_levels:
        dots = "".join(
            f'<span class="dot {"on" if i < sk["level"] else ""}"></span>' for i in range(5)
        )
        skill_html += f"""<div class="skill">
          <div class="sk-top"><span class="sk-name">{_esc(sk['name'])} <span class="lvl">Level {sk['level']}/5</span></span><span class="sk-dots">{dots}</span></div>
          <p class="sk-what">{_esc(sk['what'])}</p>
          <p class="sk-now"><b>You're here:</b> {_esc(sk['now'])}</p>
          <p class="sk-next"><b>Next move:</b> {_esc(sk['next'])}</p></div>"""

    prov_banner = ""
    if provisional:
        prov_banner = (f'<div class="prov">⚠️ Provisional: only {len(corpus.real_prompts)} real prompts found — '
                       f'treat the score as a rough range (±10). It sharpens as you use Claude Code more.</div>')
    # On thin data the archetype is the least stable signal (a near-neutral vector lands on
    # whichever prototype is closest by a hair), so hedge it explicitly rather than asserting it.
    arch_hedge = ""
    if provisional:
        arch_hedge = ('<p style="margin-top:8px;font-size:12.5px;color:var(--warn)">Provisional — based on only '
                      f'{len(corpus.real_prompts)} prompt(s); the archetype can shift as more history accumulates.</p>')

    # Header provenance meta line: evaluated date + CC version always render; the activity
    # window is guarded so a timestamp-less corpus never formats a None date (Desired-End-State #4).
    meta_bits = [f"Evaluated {generated_at:%Y-%m-%d}",
                 f"Claude Code v{_esc(corpus.cc_version) if corpus.cc_version else '—'}"]
    if corpus.first_ts and corpus.last_ts:
        meta_bits.append(
            f"activity {corpus.first_ts:%Y-%m-%d} → {corpus.last_ts:%Y-%m-%d} ({days} days)")
    meta_line = '<p class="meta">' + " · ".join(meta_bits) + "</p>"

    # fun facts
    facts = [
        f"{len(corpus.real_prompts)} prompts you actually typed (out of {corpus.user_records:,} user records — the rest were tool output, subagent turns or system text)",
        f"median prompt is {d.get('median_chars','?')} characters ({d.get('median_words','?')} words); {d.get('under_80_pct','?')}% are under 80 chars",
        f"{active_h:.0f} hours of hands-on time (idle gaps over 5 min excluded)",
        f"{result['detail']['Toolcraft']['distinct']} distinct tools used; {corpus.total_tool_calls:,} tool calls in total",
        f"most-used tool: {corpus.tool_usage.most_common(1)[0][0] if corpus.tool_usage else 'n/a'}",
        f"{corpus.delegation_events} delegations (subagents / background jobs / planning)",
    ]
    facts_html = "".join(f"<li>{_esc(f)}</li>" for f in facts)
    assessment_html = build_assessment(corpus, result, cards)

    # "What to improve": prefer the Opus analysis's tailored growth cards (grounded in this
    # person's real prompts). Only fall back to the generic teaching examples when no AI
    # analysis ran — so a finished report is personalized, not a library of stock examples.
    growth_cards = _growth_cards_html(analysis)
    if growth_cards:
        improve_cards = growth_cards
        improve_intro = ('<p class="exgen" style="margin-bottom:14px">Written for you by Claude '
                         'Opus&nbsp;4.8 from your real prompts — your highest-leverage moves, each '
                         'with one of your prompts rewritten.</p>')
    else:
        improve_cards = cards_html
        improve_intro = ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your AI Fluency Report</title>
<style>
:root{{--bg:#121214;--p:#1b1b1f;--p2:#232329;--ink:#ededf2;--mut:#aab0c2;--line:#34343d;
--ac:#7c5cff;--ac2:#3ad6c9;--ac-soft:#a99bff;--ac2-soft:#5fd6c9;
--q-hi:#7fb2ff;--q-lo:#f0a35e;--warn:#ffb454;
/* 8-pt spacing scale */
--s1:8px;--s2:16px;--s3:24px;--s4:32px;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding-bottom:80px}}
.wrap{{max-width:880px;margin:0 auto;padding:0 var(--s3)}}
header{{text-align:center;padding:64px 0 var(--s2)}}
.kick{{letter-spacing:.22em;text-transform:uppercase;font-size:12px;color:var(--mut)}}
h1{{font-size:34px;line-height:1.15;margin:var(--s1) 0 4px;font-weight:800}}
.sub{{color:var(--mut);max-width:620px;margin:var(--s1) auto 0;font-size:15px}}
.meta{{color:var(--mut);font-size:13px;margin:var(--s2) auto 0;letter-spacing:.02em}}
.hero{{margin:var(--s4) auto 0;display:flex;gap:var(--s2);align-items:stretch;flex-wrap:wrap;justify-content:center}}
.score-card{{background:var(--p2);border:1px solid var(--line);border-radius:18px;
padding:var(--s3) var(--s4);text-align:center;min-width:240px}}
.ring{{position:relative;width:170px;height:170px;margin:0 auto}}
.ring .n{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}}
.ring .n b{{font-size:54px;line-height:1;font-weight:800}}
.ring .n s{{text-decoration:none;color:var(--mut);font-size:13px}}
.band{{margin-top:var(--s2);font-size:19px;font-weight:700;color:var(--ac2-soft)}}
.rawnote{{color:var(--mut);font-size:12px;margin-top:4px}}
.arch{{flex:1;min-width:260px;background:var(--p);border:1px solid var(--line);border-radius:18px;padding:var(--s3);text-align:left}}
.arch .emoji{{font-size:40px}}
.arch h2{{font-size:23px;margin:6px 0;font-weight:700}}
.arch p{{color:var(--mut);font-size:15px}}
.prov{{background:rgba(255,180,84,.08);border:1px solid rgba(255,180,84,.32);color:#ffe6c2;border-radius:10px;padding:12px var(--s2);margin:var(--s3) 0 0;font-size:14px}}
section{{margin:var(--s4) 0}}
h3{{font-size:13px;letter-spacing:.16em;text-transform:uppercase;color:var(--mut);border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:var(--s3);font-weight:700}}
.band-meaning{{background:var(--p);border:1px solid var(--line);border-left:3px solid var(--ac-soft);border-radius:10px;padding:var(--s2) var(--s3);color:var(--ink)}}
.assess{{background:var(--p);border:1px solid var(--line);border-radius:12px;padding:var(--s2) var(--s3);margin-bottom:var(--s1);font-size:15.5px;line-height:1.6;color:var(--ink)}}
.assess b{{color:#fff}}
.ingest{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:var(--s1)}}
.ing{{background:var(--p);border:1px solid var(--line);border-radius:12px;padding:var(--s2)}}
.ing .n{{font-size:24px;font-weight:700;color:var(--ac2-soft)}}
.ing .l{{color:var(--mut);font-size:13px;margin-top:2px}}
.honesty{{margin-top:var(--s2);background:var(--p);border:1px solid var(--line);border-radius:12px;padding:var(--s2) var(--s3)}}
.honesty b{{color:var(--ink)}}
.honesty ul{{list-style:none;display:flex;flex-wrap:wrap;gap:var(--s1) var(--s3);margin-top:var(--s1)}}
.honesty li{{color:var(--mut);font-size:14px}}
.dim{{background:var(--p);border:1px solid var(--line);border-radius:12px;padding:var(--s2) var(--s3);margin-bottom:var(--s1)}}
.dim .top{{display:flex;justify-content:space-between;align-items:baseline}}
.dim .name{{font-weight:700;font-size:17px}}
.dim .sval{{font-size:22px;font-weight:800}} .dim .hint{{color:var(--mut);font-size:12px;font-weight:400}}
.dim-h{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:6px}}
.dim-h b{{font-size:17px}}
.pill{{font-size:12px;font-weight:700;color:var(--ink);background:var(--p2);border:1px solid var(--line);border-radius:99px;padding:3px 11px;white-space:nowrap}}
.ev{{margin:var(--s1) 0 0 0;padding-left:18px}} .ev li{{color:var(--mut);font-size:14px;margin:3px 0}}
.next{{margin-top:var(--s1);font-size:14.5px}} .next b{{color:#fff}}
/* Bullet-graph dimension bar: faint quality band behind a flat sequential fill + a target tick.
   Quality is encoded by FILL LENGTH + a per-dimension text label (.tag), never color alone. */
.bar{{position:relative;height:10px;background:var(--p2);border-radius:3px;overflow:hidden;margin:11px 0 9px}}
.bar::before{{content:"";position:absolute;left:0;top:0;bottom:0;width:55%;background:rgba(127,178,255,.08)}}
.bar::after{{content:"";position:absolute;left:60%;top:-2px;bottom:-2px;width:2px;background:var(--mut);opacity:.55}}
.bar>i{{display:block;height:100%;border-radius:3px;background:var(--q-hi);position:relative;z-index:1}}
.bar>i.lo{{background:var(--q-lo)}} .bar>i.mid{{background:var(--ac-soft)}}
.def{{color:var(--ink);font-size:14.5px}} .rate{{color:var(--mut);font-size:13px;margin-top:3px}} .wt{{opacity:.7}}
.tag{{font-size:10.5px;padding:2px 8px;border-radius:6px;font-weight:700;margin-left:6px;vertical-align:middle;border:1px solid transparent}}
.tag.s{{background:rgba(127,178,255,.14);color:var(--q-hi);border-color:rgba(127,178,255,.3)}}
.tag.w{{background:rgba(240,163,94,.14);color:var(--q-lo);border-color:rgba(240,163,94,.3)}}
.tag.ld{{background:rgba(170,176,194,.12);color:var(--mut);border-color:rgba(170,176,194,.26)}}
.bar-item{{display:flex;align-items:center;gap:var(--s2);margin:7px 0}}
.bl{{min-width:160px;font-size:14px}} .bt{{flex:1;height:8px;background:var(--p2);border-radius:3px;overflow:hidden}}
.bt>i{{display:block;height:100%;background:var(--ac2-soft)}} .bv{{min-width:46px;text-align:right;color:var(--mut);font-size:13px}}
.card{{background:var(--p);border:1px solid var(--line);border-radius:12px;padding:var(--s3);margin-bottom:var(--s2)}}
.prio{{border-left:3px solid var(--q-lo)}} .keep{{border-left:3px solid var(--q-hi)}}
.ph{{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:var(--mut)}}
.pscore{{float:right;color:var(--ac2-soft);letter-spacing:0}}
.card h4{{font-size:18px;margin:var(--s1) 0 12px;font-weight:700}}
.wwh{{margin:var(--s2) 0}} .wwh .lab{{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin-bottom:6px}}
ul.ev{{list-style:none}} ul.ev li{{background:var(--p2);border-radius:8px;padding:9px 12px;margin-bottom:7px;font-size:14px}}
.loc{{color:var(--mut);font-size:12.5px}} .ev-none{{color:var(--q-hi);font-size:14px}}
.ba{{display:grid;grid-template-columns:1fr 1fr;gap:var(--s1);margin-top:var(--s1)}}
.why{{color:var(--mut);font-size:14px;margin:2px 0 4px}} .why b{{color:var(--ink)}}
.how{{font-size:14.5px;margin:0 0 4px}}
.exgen{{font-size:12px;color:var(--mut);margin:var(--s1) 0 2px;font-style:italic}}
.sk-what{{color:var(--ink);font-size:13.5px;margin-top:5px}}
.lvl{{font-size:11px;color:var(--ac2-soft);font-weight:600;margin-left:6px}}
/* before/after teaching pairs: quality read from the label ("Instead of" / "Stronger"),
   not red/green fills — a neutral well + a positive blue accent, colorblind-safe. */
.before,.after{{border-radius:8px;padding:10px 13px;font-size:14px}}
.before{{background:var(--p2);color:var(--ink)}}
.after{{background:rgba(127,178,255,.08);color:var(--ink);border-left:2px solid var(--q-hi)}}
.before span,.after span{{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin-bottom:3px}}
.after span{{color:var(--q-hi)}}
.tgt{{margin-top:var(--s1);color:var(--ac2-soft);font-size:14px}}
.skill{{background:var(--p);border:1px solid var(--line);border-radius:12px;padding:var(--s2) var(--s3);margin-bottom:var(--s1)}}
.sk-top{{display:flex;justify-content:space-between;align-items:center}} .sk-name{{font-weight:700}}
.dot{{display:inline-block;width:11px;height:11px;border-radius:50%;background:var(--p2);border:1px solid var(--line);margin-left:4px}}
.dot.on{{background:var(--ac-soft);border-color:var(--ac-soft)}}
.sk-now{{color:var(--mut);font-size:13.5px;margin-top:6px}} .sk-next{{font-size:13.5px;margin-top:3px}}
.facts{{list-style:none}} .facts li{{background:var(--p);border:1px solid var(--line);border-radius:8px;padding:11px 15px;margin-bottom:var(--s1);font-size:14.5px}}
.facts li::before{{content:"›";color:var(--ac2-soft);font-weight:800;margin-right:9px}}
details{{background:var(--p);border:1px solid var(--line);border-radius:12px;padding:var(--s2) var(--s3);margin-top:var(--s2)}}
summary{{cursor:pointer;color:var(--mut);font-size:14px}} details p,details li{{color:var(--mut);font-size:13px;margin-top:var(--s1)}}
footer{{text-align:center;color:var(--mut);font-size:13px;margin-top:var(--s4)}}
code{{background:var(--p2);padding:1px 6px;border-radius:5px;font-size:13px}}
@media(max-width:640px){{.ba{{grid-template-columns:1fr}}.bl{{min-width:120px}}}}
</style></head><body><div class="wrap">

<header>
  <div class="kick">Claude Insight · AI Fluency Report</div>
  <h1>How skillfully you build with AI</h1>
  <p class="sub">A read of how you actually drive Claude Code — measured from your real prompts and Claude's real actions, analyzed entirely on your machine.</p>
</header>

{prov_banner}

<div class="hero">
  <div class="score-card">
    <div class="ring">
      <svg width="170" height="170" style="transform:rotate(-90deg)">
        <circle cx="85" cy="85" r="74" fill="none" stroke="#23264a" stroke-width="12"/>
        <circle cx="85" cy="85" r="74" fill="none" stroke="url(#g)" stroke-width="12" stroke-linecap="round"
          stroke-dasharray="{2*math.pi*74*result['overall']/100:.0f} 999"/>
        <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#7c5cff"/><stop offset="1" stop-color="#3ad6c9"/></linearGradient></defs>
      </svg>
      <div class="n"><b>{result['overall']}</b><s>/ 100</s></div>
    </div>
    <div class="band">{_esc(result['band'])}</div>
    <div class="rawnote">raw {result['overall_raw']} · confidence-adjusted {result['overall']}</div>
  </div>
  <div class="arch">
    <div class="emoji">{PROTOTYPES[a['primary']]['emoji']}</div>
    <h2>{_esc(a['label'])}</h2>
    <p>{_esc(a['blurb'])}</p>
    <p style="margin-top:10px;font-size:13px">Closest match {a['primary_sim']:+.2f}, next is {_esc(a['secondary'].replace('The ',''))} {a['secondary_sim']:+.2f}{' — close, so this is a blend' if a['blended'] else ''}. Built from how <b>you</b> drive — your briefs, corrections, tool choices and how much you hand off ({a['delegation_score']}/100 delegation) — and it deliberately discounts the read-before-edit and run-the-tests habits Claude does on its own, so it reflects you, not the agent.</p>
    <p style="margin-top:8px;font-size:12.5px;color:var(--mut)">Your <b>score</b> measures the quality of the collaboration (you + Claude); your <b>archetype</b> measures your driving style alone — so they can differ on purpose.</p>
    {arch_hedge}
  </div>
</div>

<!-- ===== Group 1 · The verdict — "How did I do?" ===== -->
<div class="band-meaning" style="margin-top:22px"><b>{_esc(result['band'])} ({result['overall']}/100).</b> {_esc(result['band_meaning'])}</div>
{meta_line}

<!-- ===== Group 2 · The breakdown — "Why?" ===== -->
<section>
  <h3>Professional assessment</h3>
  {assessment_html}
</section>

{analysis_section}
{analysis_status_html}

<section>
  <h3>The five dimensions</h3>
  {dim_html}
</section>

<!-- ===== Group 3 · Strengths & standing — "Where do I stand?" ===== -->
<section>
  <h3>Where you stand</h3>
  {strength_html}
  {skill_html}
  <details>
    <summary>Archetype affinity — how close you are to each builder style</summary>
    {aff}
  </details>
</section>

<!-- ===== Group 4 · What to do next — "What now?" ===== -->
<section>
  <h3>What to improve — and exactly how</h3>
  {improve_intro}
  {improve_cards}
</section>

<!-- ===== Group 5 · Trust & method — "Can I believe it?" ===== -->
<details>
  <summary>Trust &amp; method — the data, your tool adoption, and the honest numbers</summary>
  <section>
    <h3>How much data this is based on</h3>
    <div class="ingest">
      <div class="ing"><div class="n">{corpus.files}</div><div class="l">sessions scanned</div></div>
      <div class="ing"><div class="n">{len(corpus.projects)}</div><div class="l">projects</div></div>
      <div class="ing"><div class="n">{corpus.total_bytes/1e6:.1f} MB</div><div class="l">transcript data parsed</div></div>
      <div class="ing"><div class="n">{days} days</div><div class="l">span of activity</div></div>
      <div class="ing"><div class="n">{len(corpus.real_prompts)}</div><div class="l">real prompts you typed</div></div>
      <div class="ing"><div class="n">{active_h:.0f} h</div><div class="l">hands-on active time</div></div>
      {archive_tile}
    </div>
  </section>

  {usage_section}

  <section>
    <h3>Honest numbers at a glance</h3>
    <ul class="facts">{facts_html}</ul>
  </section>
</details>

<footer>Generated locally by Claude Insight v2 · evaluated {generated_at:%Y-%m-%d} · Claude Code v{_esc(corpus.cc_version) if corpus.cc_version else '—'} · your transcripts never left this machine.</footer>
</div></body></html>"""


def _skill_levels(result):
    """Map dimension scores to L1-L5 skill levels with now/next text."""
    def lvl(score):
        return max(1, min(5, int(score // 20) + 1))
    s = result["shrunk"]
    defs = [
        ("Briefing & specificity", "Direction",
         "name a goal + one anchor (path, constraint, or acceptance test) in most action prompts",
         {1: "Mostly short nudges with little context.", 2: "Occasional context; one constraint sometimes.",
          3: "Most prompts carry a goal + one anchor.", 4: "Goal + constraint + criterion are common.",
          5: "Consistently high-context with front-loaded rules."}),
        ("Verification discipline", "Verification",
         "end edit-bursts by running the tests / the app before moving on",
         {1: "Edits accepted blind, almost no checks.", 2: "Verifies occasionally.",
          3: "Verifies most bursts of edits.", 4: "Verifies nearly every change.",
          5: "Verification is a reflex — stated up front and layered."}),
        ("Context grounding (read→edit)", "Context",
         "have the agent read the target file before changing it",
         {1: "Often edits files it never read.", 2: "Reads before editing about half the time.",
          3: "Usually points the agent at the right place first.", 4: "Routinely reads target + deps before changing.",
          5: "Deliberate exploration before non-trivial changes."}),
        ("Iteration & recovery", "Iteration",
         "make corrections name a symptom + the exact rule, in one line",
         {1: "Low-info rejections, long loops.", 2: "Corrects but vaguely.",
          3: "Mixes precise and bare corrections.", 4: "Low correction rate, mostly specific.",
          5: "Surgical feedback; turns misses into reusable rules."}),
        ("Toolcraft & orchestration", "Toolcraft",
         "reach past the shell — search, planning, delegation for the right jobs",
         {1: "Effectively one tool.", 2: "The core trio (Bash/Read/Edit).",
          3: "Adds search/web and some planning.", 4: "Comfortable with MCP + balanced spread.",
          5: "20+ tools used appropriately, low concentration."}),
    ]
    out = []
    for name, dim, nxt, rub in defs:
        L = lvl(s[dim])
        if dim == "Toolcraft" and L == 4:
            # breadth is already there at L4; "reach past the shell" would contradict the data
            nxt = "hand multi-step jobs to plan mode or subagents instead of driving every step yourself"
        out.append({"name": name, "dim": dim, "level": L, "now": rub[L],
                    "what": SKILL_TEACH[dim]["what_it_is"],
                    "next": nxt if L < 5 else "maintain this — it's a real strength."})
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Spanish version + language toggle
# --------------------------------------------------------------------------- #
# The report is rendered in English, then each visible text segment (the text between two
# tags) is translated by EXACT match or by an anchored pattern. Anything unknown — your own
# prompts, project names, the AI-written skill map — is left untouched, so a quote of yours
# can never be "translated" by accident. Both versions ship in one file behind a toggle.

_ARCH_ES = {"Autonomous Agent": "Agente autónomo", "Architect": "Arquitecto", "Debugger": "Depurador",
            "Collaborator": "Colaborador", "Sprinter": "Velocista"}
_BAND_ES = {"Operator": "Operador", "Developing": "En desarrollo", "Proficient": "Competente",
            "Advanced": "Avanzado", "Expert": "Experto"}
_DIM_ES = {"Briefing": "Cómo pides", "Verification": "Verificación", "Context-setting": "Contexto",
           "Iteration": "Iteración", "Toolcraft": "Herramientas"}

ES = {
    **_ARCH_ES, **_BAND_ES, **_DIM_ES,
    # header & hero
    "Your AI Fluency Report": "Tu reporte de fluidez con IA",
    "Claude Insight · AI Fluency Report": "Claude Insight · Reporte de fluidez con IA",
    "How skillfully you build with AI": "Qué tan bien construyes con IA",
    "A read of how you actually drive Claude Code — measured from your real prompts and Claude's real actions, analyzed entirely on your machine.":
        "Una lectura de cómo manejas de verdad Claude Code — medida con tus prompts reales y las acciones reales de Claude, analizada por completo en tu computadora.",
    "you": "tú", "Your": "Tu", "score": "puntaje", "archetype": "arquetipo",
    "measures the quality of the collaboration (you + Claude); your": "mide la calidad de la colaboración (tú + Claude); tu",
    "measures your driving style alone — so they can differ on purpose.": "mide solo tu estilo — por eso pueden no coincidir, y es a propósito.",
    # archetype blurbs
    PROTOTYPES["Autonomous Agent"]["blurb"]: "Delegas trabajos completos, de principio a fin, y confías en que el agente los resuelva — tú pones el resultado y Claude elige los pasos.",
    PROTOTYPES["Architect"]["blurb"]: "Planificas y exploras antes de construir — lees y diseñas primero, así los cambios caen sobre una estructura clara.",
    PROTOTYPES["Debugger"]["blurb"]: "Cazas problemas con método — lees para diagnosticar, cambias, verificas y repites hasta que de verdad quede arreglado.",
    PROTOTYPES["Collaborator"]["blurb"]: "Trabajas con el agente como con un compañero — pides opciones, das feedback y lo guías hasta estar de acuerdo.",
    PROTOTYPES["Sprinter"]["blurb"]: "Vas rápido y directo — prompts cortos, vueltas rápidas, poca ceremonia. Mucha velocidad; tus puntos a crecer son cómo pides y cómo verificas.",
    # band meanings
    BANDS[0][3]: "Usas al agente como manos rápidas. Los prompts son cortos y poco específicos, se edita sin leer el archivo antes y casi nunca se verifica. Aquí están las mejoras más rápidas: di un objetivo más una restricción, y deja que el agente lea antes de editar.",
    BANDS[1][3]: "Empieza a haber un ida y vuelta real y uno o dos hábitos ya son sólidos. Algunos prompts traen una ruta o una restricción; a veces se verifica. Lo que falta para el siguiente nivel es constancia — hacer lo correcto siempre, no solo a veces.",
    BANDS[2][3]: "Manejas al agente con intención. La mayoría de los prompts son específicos, las ediciones suelen venir después de leer el archivo y verificas más veces de las que no. Ingeniería con IA sólida y confiable. Lo que queda por ganar es decir el porqué y orquestar mejor.",
    BANDS[3][3]: "Orquestas en vez de operar. Tus prompts traen objetivos, restricciones y criterios de aceptación; leer antes de editar es un hábito; verificar es casi automático; usas planificación y delegación con soltura. Le das instrucciones al agente como a un compañero senior.",
    BANDS[4][3]: "Tratas al agente como un sistema de ingeniería gestionado: prompts con mucho contexto y criterios de éxito explícitos, ciclos disciplinados de leer→editar→verificar, delegación pensada y casi ninguna corrección desperdiciada.",
    # section titles
    "Professional assessment": "Evaluación profesional",
    "The five dimensions": "Las cinco dimensiones",
    "Where you stand": "Dónde estás",
    "Archetype affinity — how close you are to each builder style": "Afinidad de arquetipo — qué tan cerca estás de cada estilo",
    "What to improve — and exactly how": "Qué mejorar — y cómo exactamente",
    "Trust & method — the data, your tool adoption, and the honest numbers": "Confianza y método — los datos, tus herramientas y los números honestos",
    "How much data this is based on": "Con cuántos datos se calculó",
    "Honest numbers at a glance": "Números honestos de un vistazo",
    "Usage & Crabi-tool adoption": "Uso y adopción de herramientas Crabi",
    "Skill map — analyzed against the AI Fluency framework": "Mapa de habilidades — analizado con el marco AI Fluency",
    # assessment fragments
    "You drive Claude like": "Manejas Claude como",
    "Your strongest": "Tu hábito",
    "self-driven": "propio",
    "And the apparent tension, resolved: your lowest dimension is": "Y la aparente contradicción, resuelta: tu dimensión más baja es",
    "defining": "principal",
    # dimensions
    "How clearly you tell the agent what you want before it acts.": "Qué tan claro le dices al agente lo que quieres antes de que actúe.",
    "Whether changes get checked (tests / build / app) — and whether you look at the result before saying “ok, next”.":
        "Si los cambios se comprueban (tests / build / app) — y si tú miras el resultado antes de decir “ok, sigue”.",
    "Reading a file before editing it — grounded, not blind, changes.": "Leer un archivo antes de editarlo — cambios con base, no a ciegas.",
    "Correcting precisely instead of thrashing with vague rejections.": "Corregir con precisión en vez de dar vueltas con rechazos vagos.",
    "Using a healthy range of tools — not forcing everything through one.": "Usar variedad de herramientas — no forzar todo por una sola.",
    "Strength": "Fortaleza", "Top growth lever": "Mayor palanca de mejora", "low data": "pocos datos",
    # where you stand
    "Keep doing this": "Sigue así", "Your relatively strongest area": "Tu área relativamente más fuerte",
    "Briefing & specificity": "Cómo pides y qué tan específico",
    "Verification discipline": "Disciplina de verificación",
    "Context grounding (read→edit)": "Contexto (leer→editar)",
    "Iteration & recovery": "Iteración y recuperación",
    "Toolcraft & orchestration": "Herramientas y orquestación",
    "You're here:": "Estás aquí:", "Next move:": "Siguiente paso:",
    "Mostly short nudges with little context.": "Sobre todo empujoncitos cortos con poco contexto.",
    "Occasional context; one constraint sometimes.": "Contexto de vez en cuando; a veces una restricción.",
    "Most prompts carry a goal + one anchor.": "La mayoría de los prompts traen un objetivo + un ancla.",
    "Goal + constraint + criterion are common.": "Objetivo + restricción + criterio son lo normal.",
    "Consistently high-context with front-loaded rules.": "Siempre con mucho contexto y las reglas dichas de entrada.",
    "Edits accepted blind, almost no checks.": "Cambios aceptados a ciegas, casi sin comprobar.",
    "Verifies occasionally.": "Verifica de vez en cuando.",
    "Verifies most bursts of edits.": "Verifica la mayoría de las tandas de cambios.",
    "Verifies nearly every change.": "Verifica casi todos los cambios.",
    "Verification is a reflex — stated up front and layered.": "Verificar es un reflejo — dicho de entrada y en capas.",
    "Often edits files it never read.": "Seguido edita archivos que nunca leyó.",
    "Reads before editing about half the time.": "Lee antes de editar más o menos la mitad de las veces.",
    "Usually points the agent at the right place first.": "Suele apuntar al agente al lugar correcto primero.",
    "Routinely reads target + deps before changing.": "Lee el archivo y sus dependencias antes de cambiar, por rutina.",
    "Deliberate exploration before non-trivial changes.": "Explora a propósito antes de cambios no triviales.",
    "Low-info rejections, long loops.": "Rechazos sin información, vueltas largas.",
    "Corrects but vaguely.": "Corrige, pero de forma vaga.",
    "Mixes precise and bare corrections.": "Mezcla correcciones precisas y vacías.",
    "Low correction rate, mostly specific.": "Pocas correcciones y casi todas específicas.",
    "Surgical feedback; turns misses into reusable rules.": "Feedback quirúrgico; convierte errores en reglas reutilizables.",
    "Effectively one tool.": "En la práctica, una sola herramienta.",
    "The core trio (Bash/Read/Edit).": "El trío básico (Bash/Read/Edit).",
    "Adds search/web and some planning.": "Suma búsqueda/web y algo de planificación.",
    "Comfortable with MCP + balanced spread.": "Cómodo con MCP y un uso equilibrado.",
    "20+ tools used appropriately, low concentration.": "Más de 20 herramientas bien usadas, poca concentración.",
    "name a goal + one anchor (path, constraint, or acceptance test) in most action prompts":
        "di un objetivo + un ancla (ruta, restricción o prueba de aceptación) en la mayoría de los pedidos",
    "end edit-bursts by running the tests / the app before moving on": "cierra cada tanda de cambios corriendo los tests / la app antes de seguir",
    "have the agent read the target file before changing it": "haz que el agente lea el archivo antes de cambiarlo",
    "make corrections name a symptom + the exact rule, in one line": "que tus correcciones digan el síntoma + la regla exacta, en una línea",
    "reach past the shell — search, planning, delegation for the right jobs": "ve más allá de la consola — búsqueda, planificación y delegación donde toque",
    "hand multi-step jobs to plan mode or subagents instead of driving every step yourself":
        "pasa los trabajos de varios pasos al modo plan o a subagentes en vez de manejar cada paso tú",
    "maintain this — it's a real strength.": "mantenlo — es una fortaleza real.",
    # improvement cards
    "Why it matters.": "Por qué importa.",
    "Where this shows up in your sessions": "Dónde aparece en tus sesiones",
    "How to grow it": "Cómo mejorarlo",
    "Generic illustrations of the habit —": "Ejemplos genéricos del hábito —",
    "not": "no", "from your sessions:": "salen de tus sesiones:",
    "Instead of": "En vez de", "Stronger": "Mejor",
    "No clear examples in your transcripts — this is already a habit. ✓": "No hay ejemplos claros en tus sesiones — ya es un hábito. ✓",
    "(illustrative, small sample)": "(ilustrativo, muestra chica)", "(illustrative)": "(ilustrativo)",
    "Edited": "Editó", "without reading it first": "sin leerlo antes",
    "A burst of edits to": "Una tanda de cambios en", "with nothing run afterwards": "sin correr nada después",
    "A prompt you wrote": "Un prompt tuyo", "Tailored rewrite for you": "Reescrito para ti",
    "Your next move:": "Tu siguiente paso:", "What you already do well:": "Lo que ya haces bien:",
    "This section is written by Claude Opus 4.8 from your de-contaminated evidence (explored by Claude Sonnet 4.6), grounded in the bundled AI Fluency framework. The numbers above are computed deterministically and independently.":
        "Esta sección la escribe Claude Opus 4.8 con tu evidencia depurada (explorada por Claude Sonnet 4.6), basada en el marco AI Fluency incluido. Los números de arriba se calculan aparte, de forma determinista.",
    "Written for you by Claude Opus 4.8 from your real prompts — your highest-leverage moves, each with one of your prompts rewritten.":
        "Escrito para ti por Claude Opus 4.8 con tus prompts reales — tus movimientos de mayor impacto, cada uno con uno de tus prompts reescrito.",
    "Deterministic report only.": "Solo reporte determinista.",
    "inside Claude Code.": "dentro de Claude Code.",
    # data tiles
    "sessions scanned": "sesiones analizadas", "projects": "proyectos", "transcript data parsed": "de transcripciones leídas",
    "span of activity": "de actividad", "real prompts you typed": "prompts reales que escribiste",
    "hands-on active time": "de tiempo activo", "sessions in your archive": "sesiones en tu archivo",
    # usage & adoption
    "Build": "Construir", "Debug": "Depurar", "Plan": "Planificar", "Investigate": "Investigar",
    "Shell/Ops": "Consola/Ops", "Delegate": "Delegar", "Other": "Otro",
    "Crabi suggested plugins": "Plugins sugeridos por Crabi",
    "— the tools we recommend leaning on:": "— las herramientas que recomendamos usar:",
    "Crabi AI marketplace": "Marketplace de IA de Crabi", "— every plugin in the catalog:": "— todos los plugins del catálogo:",
    "installed": "instalado", "not installed": "no instalado", "enabled": "activado", "disabled": "desactivado",
    "never used": "nunca usado", "installed · idle": "instalado · sin uso", "installed but idle": "instalado pero sin uso",
    "Installed but never used — a tool you could lean on.": "Instalado pero nunca usado — una herramienta que podrías aprovechar.",
    "Not installed — install it to unlock this workflow.": "No instalado — instálalo para usar este flujo.",
    "Engram (memory)": "Engram (memoria)", "Ponytail (code-quality)": "Ponytail (calidad de código)",
    "Obsidian (notes)": "Obsidian (notas)", "Desplega (toolbox)": "Desplega (caja de herramientas)",
    "Raw counts over your real measured window — not normalized. Adoption is read live from your local plugin config; nothing left this machine.":
        "Conteos en bruto sobre tu periodo real medido — sin normalizar. La adopción se lee de tu configuración local de plugins; nada salió de esta computadora.",
}

# Teaching copy: the SKILL_TEACH texts and their before/after examples.
_TEACH_ES = {
    "Direction": ("Decirle al agente lo que quieres y darle algo a qué apuntar: un objetivo más un archivo, una restricción o una forma de saber que funcionó.",
                  "Cuando tu objetivo y tus límites están claros desde el principio, el agente acierta a la primera en vez de adivinar y meterte en rondas de arreglos.",
                  "Antes de dar enter, súmale un ancla a tu objetivo: el archivo a tocar, una regla que no debe romper o una línea de 'listo cuando…'. Con una línea basta.",
                  ["arregla el bug del login", "Los usuarios quedan deslogueados en Safari aunque pongan bien la contraseña. La comprobación está en src/auth/session.ts. Arréglalo para que un login válido guarde la cookie de sesión, y que los tests actuales sigan en verde.",
                   "agrega caché a la API", "Cachea por 60 s las respuestas de GET /products en api/products.py para bajar la carga de la base en lecturas repetidas. No caches pedidos autenticados, y agrega un test que compruebe que una segunda llamada dentro de 60 s no va a la base."],
                  "Antes de mandar un prompt, súmale un ancla a tu objetivo: una ruta, una restricción o una línea de 'listo cuando…'.",
                  "Cada pedido dice lo que quieres y dónde trabajar o cómo se juzga el éxito, así el agente actúa en vez de adivinar."),
    "Verification": ("Hacer que el agente pruebe su propio trabajo — correr los tests, el build, el lint o abrir la app — antes de decirte que terminó.",
                     "El código que parece bien pero nunca se corrió es donde se esconden la mayoría de los bugs de IA; comprobarlo convierte “seguro funciona” en “lo vi funcionar”.",
                     "En el mismo prompt donde pides el cambio, nombra el comando exacto que lo prueba (un test, build, lint o curl) y pídele al agente que lo corra y te muestre la salida antes de terminar.",
                     ["Arregla el error por uno en el helper de paginación.", "Arregla el error por uno en el helper de paginación, luego corre `pytest tests/test_pagination.py -x` y pega la salida. No lo des por arreglado hasta que ese test pase.",
                      "Agrega un endpoint /health al servidor FastAPI.", "Agrega un endpoint /health al servidor FastAPI. Levántalo en el puerto 8000, haz curl a `localhost:8000/health` y muéstrame la respuesta. Corre también `ruff check` y confirma que está limpio antes de terminar."],
                     "Antes de aceptar cualquier cambio, pregunta: “¿Cómo lo verificaste? Córrelo y muéstrame la salida.”",
                     "Cada cambio termina con una prueba — un test que pasa, un build en verde, una respuesta real — pegada de vuelta, no solo una afirmación."),
    "Context": ("Apuntar al agente al código real — un archivo, una función, una zona — y hacer que lo lea antes de cambiar nada.",
                "Cuando el agente ve primero el código actual, sus cambios encajan con lo que de verdad hay en vez de con una suposición, y se aplican bien a la primera.",
                "Antes de cualquier cambio, nombra el archivo exacto (y la función o zona si puedes) y pídele al agente que lo lea primero. Que mire antes de saltar.",
                ["Agrega lógica de reintentos al cliente de la API.", "Lee primero src/api/client.ts y luego agrega reintentos con espera creciente al método request(). Muéstrame el cambio antes de aplicarlo.",
                 "Arregla el bug de zona horaria en el formateador de fechas.", "Abre src/utils/date.ts y busca formatDate(). Lee cómo maneja hoy las zonas horarias y luego arregla el desfase para que las fechas en UTC salgan en la zona local del usuario."],
                "Empieza tu próximo pedido de cambio con “Lee <archivo> primero, y luego…” para que el agente se ubique antes de tocar nada.",
                "Cada cambio cae sobre código que el agente acaba de leer, así los diffs se aplican limpios sin romper nada alrededor."),
    "Iteration": ("Cuando el agente se va por mal camino, traerlo de vuelta con una corrección precisa — diciendo qué se rompió y qué regla seguir — en vez de solo “no” o “intenta de nuevo”.",
                  "Una corrección precisa logra el arreglo en una vuelta; un “no” vago hace que el agente vuelva a adivinar, y gastas turnos mientras el código se aleja más.",
                  "Cuando un resultado está mal, di tres cosas en un mensaje: el síntoma que viste, la regla que rompió y qué hacer en su lugar. Después déjalo correr.",
                  ["no, así no es, intenta de nuevo", "El bucle de reintentos atrapa la excepción pero nunca la vuelve a lanzar después del último intento, así que los fallos parecen éxitos. Relanza el error original cuando se acaben los reintentos y mantén la espera actual.",
                   "esto está mal, arregla el test", "El test pasa porque simulaste la función que estás probando en vez de la llamada de red. No simules get_user — simula requests.get dentro de ella y comprueba que se llamó con la URL real."],
                  "Antes de mandar una corrección, revisa que diga el síntoma y la regla. Si solo dice “no”, agrega la mitad que falta.",
                  "Una corrección precisa — síntoma, regla y arreglo — y el agente lo logra en el siguiente intento."),
    "Toolcraft": ("Dejar que el agente use la herramienta correcta en cada paso — buscar en el código, correr comandos, levantar la app, trabajar en segundo plano — en vez de forzar todo por el chat.",
                  "El agente trabaja más rápido y con más confianza cuando busca y corre las cosas de verdad, en vez de razonar sobre el código de memoria.",
                  "Dile al agente qué hacer primero — buscar en el código, correr la suite, levantar el servidor — para que junte datos y compruebe su trabajo con la herramienta hecha para cada paso.",
                  ["¿Cómo funciona el login en esta app?", "Busca en el código el flujo de login (grep de auth, session, login), lee los archivos que encuentres y luego explica cómo un pedido va del envío del formulario a una sesión iniciada.",
                   "Agrega un reintento al cliente de la API, y asegúrate de que los tests sigan pasando.", "Agrega reintentos con espera creciente al cliente de la API. Luego corre la suite en segundo plano; si algo falla, lee el fallo, arréglalo y avísame cuando esté en verde."],
                  "Agrega una línea a tu próxima tarea diciéndole al agente qué hacer primero: “busca…”, “corre los tests” o “levanta el servidor y comprueba”.",
                  "Pasas un trabajo completo y el agente busca, edita, corre y verifica solo — cada paso con la herramienta hecha para eso."),
}
for _dim, (_what, _why, _how, _ex, _practice, _good) in _TEACH_ES.items():
    _t = SKILL_TEACH[_dim]
    ES[_t["what_it_is"]] = _what
    ES[_t["why_it_matters"]] = _why
    ES[_t["how_to_improve"]] = _how
    ES[_t["practice"]] = _practice
    ES[_t["good_looks_like"]] = _good
    for _pair, (_b, _a) in zip(_t["examples"], zip(_ex[::2], _ex[1::2])):
        ES[_pair["before"]] = _b
        ES[_pair["after"]] = _a

_SIG_DESC_ES = {
    _SIG_DESC["Delegation"]: "cuánto delegas — le das a Claude trabajos completos y confías en que los resuelva de punta a punta",
    _SIG_DESC["Toolcraft"]: "la variedad de herramientas que usas — vas más allá de la consola por el instrumento correcto",
    _SIG_DESC["Iteration"]: "lo limpio que cambias de rumbo — tus correcciones suelen decir el arreglo, no solo rechazar",
    _SIG_DESC["Briefing"]: "lo concreto que planteas los pedidos cuando importa",
}
_ARCH_PATHS_ES = {
    ARCH_PATHS["Autonomous Agent"]: "Ya delegas bien trabajos completos — suma una frase clara de intención por cada encargo y mucho más saldrá bien a la primera, con menos ida y vuelta.",
    ARCH_PATHS["Architect"]: "Planificar es una fortaleza real — combínalo con una comprobación rápida después de cada cambio para que tus diseños salgan probados, no solo dibujados.",
    ARCH_PATHS["Debugger"]: "Tu disciplina de diagnóstico es excelente — guarda cada arreglo como una pequeña regla reutilizable para que el mismo bug nunca te cueste dos veces.",
    ARCH_PATHS["Collaborator"]: "Tu ida y vuelta mantiene todo alineado — decir una o dos restricciones de entrada te hará llegar en menos rondas.",
    ARCH_PATHS["Sprinter"]: "Tu velocidad es real — un pedido de una línea más una prueba rápida evita que esa velocidad se convierta en retrabajo.",
    "Keep building the habits below and your next run will show the gain.": "Sigue construyendo los hábitos de abajo y tu próxima medición mostrará la mejora.",
}
_GROWTH_LINE_ES = {
    _GROWTH_LINE["Verification"]: "Ahora mismo los cambios suelen seguir sin un test, build o ejecución que los confirme — la confiabilidad más barata que puedes recuperar.",
    _GROWTH_LINE["Context"]: "Ahora mismo algunos cambios caen antes de leer el archivo en esa sesión — un riesgo de editar a ciegas fácil de quitar.",
    _GROWTH_LINE["Iteration"]: "Ahora mismo las correcciones tienden a ser rechazos breves; decir el síntoma y la regla exacta cierra las vueltas en menos turnos.",
    _GROWTH_LINE["Toolcraft"]: "Ahora mismo casi todo el trabajo pasa por una sola herramienta — usar búsqueda, planificación y delegación amplía lo que puedes encarar.",
}
ES.update(_SIG_DESC_ES)
ES.update(_ARCH_PATHS_ES)

_ARCH_ALT = "|".join(map(re.escape, _ARCH_ES))
_BAND_ALT = "|".join(map(re.escape, _BAND_ES))


def _tr_es(s):
    """Spanish for one visible text segment, or None when it isn't report copy."""
    if s in ES:
        return ES[s]
    for rx, fn in _ES_PATTERNS:
        m = rx.fullmatch(s)
        if m:
            return fn(m)
    return None


def _tr_or_keep(s):
    out = _tr_es(s)
    return s if out is None else out


def _arch_label_es(m):
    out = f"{m['emoji']} {_ARCH_ES[m['arch']]}"
    if m["second"]:
        out += f", con un toque de {_ARCH_ES[m['second']]}"
    return out


def _growth_tail_es(m):
    rest = m[1]
    d = re.fullmatch(rf"({_ARCH_ALT})s win on how sharply they frame the work they hand off — and right now "
                     r"yours are often one-liners like “(.*?)”, so Claude fills gaps you could have decided\. (.*)", rest)
    if d:
        line = (f"Los perfiles {_ARCH_ES[d[1]]} ganan según lo claro que plantean lo que delegan — y ahora mismo "
                f"los tuyos suelen ser frases de una línea como “{d[2]}”, así que Claude llena huecos que podrías "
                f"haber decidido tú.")
        return f"punto de crecimiento. {line} {_tr_or_keep(d[3])}"
    for en, es in _GROWTH_LINE_ES.items():
        if rest.startswith(en):
            return f"punto de crecimiento. {es} {_tr_or_keep(rest[len(en):].strip())}"
    return f"punto de crecimiento. {_tr_or_keep(rest)}"


def _rate_line_es(s):
    parts = [p.strip() for p in s.split(" · ")]
    return " · ".join(_tr_or_keep(p) for p in parts)


_ES_PATTERNS = [(re.compile(rx), fn) for rx, fn in [
    (rf"(?P<emoji>\S+) (?P<arch>{_ARCH_ALT})(?:, with an? (?P<second>{_ARCH_ALT}) streak)?", _arch_label_es),
    (r"raw (\d+) · confidence-adjusted (\d+)", lambda m: f"bruto {m[1]} · ajustado por confianza {m[2]}"),
    (rf"Closest match (\S+), next is ({_ARCH_ALT}) (\S+)( — close, so this is a blend)?\. Built from how",
     lambda m: f"Coincidencia más cercana {m[1]}; le sigue {_ARCH_ES[m[2]]} {m[3]}"
               + (" — muy cerca, así que es una mezcla" if m[4] else "") + ". Se calcula a partir de cómo"),
    (r"drive — your briefs, corrections, tool choices and how much you hand off \((\d+)/100 delegation\) — and it "
     r"deliberately discounts the read-before-edit and run-the-tests habits Claude does on its own, so it reflects you, not the agent\.",
     lambda m: f"manejas — tus pedidos, correcciones, herramientas y cuánto delegas ({m[1]}/100 de delegación) — y "
               "descuenta a propósito los hábitos de leer antes de editar y correr los tests, que Claude hace por su cuenta, "
               "para que te refleje a ti y no al agente."),
    (r"Provisional — based on only (\d+) prompt\(s\); the archetype can shift as more history accumulates\.",
     lambda m: f"Provisional — basado en solo {m[1]} prompt(s); el arquetipo puede cambiar a medida que haya más historial."),
    (r"⚠️ Provisional: only (\d+) real prompts found — treat the score as a rough range \(±10\)\. It sharpens as you use Claude Code more\.",
     lambda m: f"⚠️ Provisional: solo se encontraron {m[1]} prompts reales — toma el puntaje como un rango aproximado (±10). "
               "Se afina a medida que uses más Claude Code."),
    (rf"({_BAND_ALT}) \((\d+)/100\)\.", lambda m: f"{_BAND_ES[m[1]]} ({m[2]}/100)."),
    (rf"(\d+)/100 \(({_BAND_ALT})\)", lambda m: f"{m[1]}/100 ({_BAND_ES[m[2]]})"),
    (r"Evaluated (\S+) · Claude Code v(\S+)(?: · activity (\S+) → (\S+) \((\d+) days\))?",
     lambda m: f"Evaluado {m[1]} · Claude Code v{m[2]}"
               + (f" · actividad {m[3]} → {m[4]} ({m[5]} días)" if m[3] else "")),
    (r"\. (.+?) The clearest signal is your delegation rate —",
     lambda m: f". {_tr_or_keep(m[1])} La señal más clara es tu nivel de delegación —"),
    (r", from (\d+) hand-offs to subagents, background jobs and planning — paired with fast, terse prompts \(median (\S+) characters\)\.",
     lambda m: f", con {m[1]} encargos a subagentes, tareas en segundo plano y planificación — junto con prompts rápidos y "
               f"cortos (mediana de {m[2]} caracteres)."),
    (r"habit is (.+?)\. (That, plus the disciplined read→edit→verify loop your sessions show, is|That is) why your overall score lands at",
     lambda m: f"más fuerte es {_tr_or_keep(m[1])}. "
               + ("Eso, más el ciclo disciplinado de leer→editar→verificar que muestran tus sesiones, es"
                  if m[2].startswith("That, plus") else "Eso es")
               + " lo que deja tu puntaje general en"),
    (rf"— but for an? ({_ARCH_ALT}) that isn't a contradiction, it's the",
     lambda m: f"— pero para un perfil {_ARCH_ES[m[1]]} eso no es una contradicción, es tu"),
    (r"growth edge\. (.*)", _growth_tail_es),
    (r"agent verified (\d+) of (\d+) edit-bursts \((\d+)%\)(?: · you checked the result in (\d+)% of (\d+) replies \((\d+) bare “ok, sigue”\))?",
     lambda m: f"el agente verificó {m[1]} de {m[2]} tandas de cambios ({m[3]}%)"
               + (f" · tú revisaste el resultado en el {m[4]}% de {m[5]} respuestas ({m[6]} “ok, sigue” a secas)" if m[4] else "")),
    (r"(\d+) of (\d+) edits were grounded in a prior read \((\d+)%\)",
     lambda m: f"{m[1]} de {m[2]} cambios se hicieron después de leer el archivo ({m[3]}%)"),
    (r"(\d+)% carry a constraint · (\d+)% name a file/error · (\d+)% state a why(?: · (\d+) task prompts \((\d+) go-aheads like “sí, hazlo” left out\))?",
     lambda m: f"{m[1]}% traen una restricción · {m[2]}% nombran un archivo/error · {m[3]}% dicen el porqué"
               + (f" · {m[4]} pedidos de tarea ({m[5]} vistos buenos tipo “sí, hazlo” no cuentan)" if m[4] else "")),
    (r"(\d+) correction turns \((\d+)% of prompts\); (\d+)% were specific",
     lambda m: f"{m[1]} correcciones ({m[2]}% de los prompts); {m[3]}% fueron específicas"),
    (r"(\d+) distinct tools, evenness ([\d.]+), (\d+) delegations",
     lambda m: f"{m[1]} herramientas distintas, equilibrio {m[2]}, {m[3]} delegaciones"),
    (r"· weight (\d+)%", lambda m: f"· peso {m[1]}%"),
    (r"(.+?) The evidence in your sessions: (.+)\. This is your foundation — build on it\.",
     lambda m: f"{_tr_or_keep(m[1])} La evidencia en tus sesiones: {_rate_line_es(m[2])}. Esta es tu base — construye sobre ella."),
    (r"Even your strongest dimension has room to grow \((\d+)/100\), but this is the most natural place to build from\. "
     r"The evidence in your sessions: (.+)\.",
     lambda m: f"Incluso tu dimensión más fuerte puede crecer ({m[1]}/100), pero es el lugar más natural desde donde construir. "
               f"La evidencia en tus sesiones: {_rate_line_es(m[2])}."),
    (r"(Keep doing this|Your relatively strongest area) · (.+)", lambda m: f"{ES[m[1]]} · {_tr_or_keep(m[2])}"),
    (r"Level (\d)/5", lambda m: f"Nivel {m[1]}/5"),
    (r"Level (\S+)/5 · (.*)", lambda m: f"Nivel {m[1]}/5 · {m[2]}"),
    (r"Priority (\d+) · written for you", lambda m: f"Prioridad {m[1]} · escrito para ti"),
    (r"Priority (\d+) · (.+)", lambda m: f"Prioridad {m[1]} · {_tr_or_keep(m[2])}"),
    (r"now (\d+)/100", lambda m: f"ahora {m[1]}/100"),
    (r"“(.*)”", lambda m: f"“{ES[m[1]]}”" if m[1] in ES else None),
    (r"🎯 Try this next session: (.+)", lambda m: f"🎯 Prueba esto en tu próxima sesión: {_tr_or_keep(m[1])}"),
    (r"(\d+) days", lambda m: f"{m[1]} días"),
    (r"(MCP-server calls|Top slash commands|Work-type mix|No MCP-server calls) (?:over the last (\d+) days|over your history)([:.])",
     lambda m: {"MCP-server calls": "Llamadas a servidores MCP", "Top slash commands": "Comandos con / más usados",
                "Work-type mix": "Tipo de trabajo", "No MCP-server calls": "Sin llamadas a servidores MCP"}[m[1]]
               + (f" en los últimos {m[2]} días" if m[2] else " en tu historial") + m[3]),
    (r"used ([\d,]+)×", lambda m: f"usado {m[1]}×"),
    (r"installed · used ([\d,]+)×", lambda m: f"instalado · usado {m[1]}×"),
    (r"update available · (.+)", lambda m: f"actualización disponible · {m[1]}"),
    (r"(.*) — the Sonnet \+ Opus skill map was not added on top\. The scores, archetype and dimensions below are still "
     r"fully computed from your data; to add the AI-written skill map, re-run",
     lambda m: f"{m[1]} — no se agregó encima el mapa de habilidades de Sonnet + Opus. Los puntajes, el arquetipo y las "
               "dimensiones de abajo igual se calculan completos con tus datos; para sumar el mapa escrito por IA, vuelve a correr"),
    (r"(\d+) prompts you actually typed \(out of ([\d,]+) user records — the rest were tool output, subagent turns or system text\)",
     lambda m: f"{m[1]} prompts que escribiste de verdad (de {m[2]} registros de usuario — el resto eran salidas de "
               "herramientas, turnos de subagentes o texto del sistema)"),
    (r"median prompt is (\S+) characters \((\S+) words\); (\S+)% are under 80 chars",
     lambda m: f"la mediana de un prompt es {m[1]} caracteres ({m[2]} palabras); el {m[3]}% tiene menos de 80"),
    (r"(\d+) hours of hands-on time \(idle gaps over 5 min excluded\)",
     lambda m: f"{m[1]} horas de trabajo activo (sin contar pausas de más de 5 min)"),
    (r"(\d+) distinct tools used; ([\d,]+) tool calls in total",
     lambda m: f"{m[1]} herramientas distintas; {m[2]} llamadas a herramientas en total"),
    (r"most-used tool: (.+)", lambda m: f"herramienta más usada: {m[1]}"),
    (r"(\d+) delegations \(subagents / background jobs / planning\)",
     lambda m: f"{m[1]} delegaciones (subagentes / tareas en segundo plano / planificación)"),
    (r"Generated locally by Claude Insight v2 · evaluated (\S+) · Claude Code v(\S+) · your transcripts never left this machine\.",
     lambda m: f"Generado localmente por Claude Insight v2 · evaluado {m[1]} · Claude Code v{m[2]} · tus transcripciones "
               "nunca salieron de esta computadora."),
]]

_SEGMENT_RE = re.compile(r">([^<]+)<")


def translate_html_es(html_en):
    """The Spanish twin of a rendered report: only the <body> text is touched."""
    def seg(m):
        raw = m.group(1)
        core = " ".join(html.unescape(raw).split())
        if not core:
            return m.group(0)
        es = _tr_es(core)
        if es is None:
            return m.group(0)
        lead = raw[: len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        return f">{lead}{html.escape(es, quote=False)}{trail}<"
    head, sep, body = html_en.partition("<body>")
    if not sep:
        return _SEGMENT_RE.sub(seg, html_en)
    return head + sep + _SEGMENT_RE.sub(seg, body)


_LANG_CSS = """
.lang-switch{position:fixed;top:12px;right:12px;z-index:10;display:flex;background:var(--p2);
border:1px solid var(--line);border-radius:99px;padding:3px;gap:2px;font-size:12px}
.lang-switch button{all:unset;cursor:pointer;padding:5px 11px;border-radius:99px;color:var(--mut);font-weight:700;letter-spacing:.06em}
.lang-switch button[aria-pressed="true"]{background:var(--ac-soft);color:#121214}
.lang-switch button:focus-visible{outline:2px solid var(--ac2-soft);outline-offset:2px}
"""

_LANG_JS = """
(function(){
  var titles={en:%s,es:%s};
  function set(lang){
    document.querySelectorAll('[data-lang]').forEach(function(el){el.hidden=el.getAttribute('data-lang')!==lang;});
    document.querySelectorAll('.lang-switch button').forEach(function(b){b.setAttribute('aria-pressed',String(b.value===lang));});
    document.documentElement.lang=lang; document.title=titles[lang];
    try{localStorage.setItem('ai-fluency-lang',lang);}catch(e){}
  }
  var saved=null; try{saved=localStorage.getItem('ai-fluency-lang');}catch(e){}
  var nav=(navigator.language||'en').toLowerCase();
  set(saved==='en'||saved==='es'?saved:(nav.indexOf('es')===0?'es':'en'));
  document.querySelectorAll('.lang-switch button').forEach(function(b){b.addEventListener('click',function(){set(b.value);});});
})();
"""


def bilingual_html(html_en):
    """One file with the English report and its Spanish twin, plus an EN/ES switch.
    Opens in Spanish when the browser is set to Spanish; the choice is remembered."""
    html_es = translate_html_es(html_en)
    body_en = re.search(r"<body[^>]*>(.*)</body>", html_en, re.S).group(1)
    body_es = re.search(r"<body[^>]*>(.*)</body>", html_es, re.S).group(1)
    switch = ('<div class="lang-switch" role="group" aria-label="Language / Idioma">'
              '<button type="button" value="en" aria-pressed="true">EN</button>'
              '<button type="button" value="es" aria-pressed="false">ES</button></div>')
    js = _LANG_JS % (json.dumps("Your AI Fluency Report"), json.dumps(ES["Your AI Fluency Report"]))
    out = html_en.replace("</style>", _LANG_CSS + "</style>", 1)
    return re.sub(r"(<body[^>]*>).*(</body>)",
                  lambda m: (f'{m[1]}{switch}<div data-lang="en">{body_en}</div>'
                             f'<div data-lang="es" lang="es" hidden>{body_es}</div><script>{js}</script>{m[2]}'),
                  out, count=1, flags=re.S)


def _artifact_html(html_doc):
    """Content-only variant of the report for the Artifact tool, which wraps the file in
    its own <!doctype>/<head>/<body> at publish time. Keep <title> + <style> + the <body>
    inner so the hosted page matches the local report."""
    style = "\n".join(re.findall(r"<style[^>]*>.*?</style>", html_doc, re.S | re.I))
    tm = re.search(r"<title[^>]*>.*?</title>", html_doc, re.S | re.I)
    title = tm.group(0) if tm else "<title>Your AI Fluency Report</title>"
    bm = re.search(r"<body[^>]*>(.*?)</body>", html_doc, re.S | re.I)
    return f"{title}\n{style}\n{bm.group(1) if bm else html_doc}"


def main(argv=None):
    # Windows consoles default to cp1252 and crash on the archetype emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Claude Insight v2 — AI fluency analyzer (one command, zero install).")
    ap.add_argument("path", nargs="?", help="transcript dir or .jsonl file (default: ~/.claude/projects)")
    ap.add_argument("-o", "--out", default="ai_fluency_report.html", help="HTML output path")
    ap.add_argument("--json", action="store_true", help="print raw metrics as JSON and exit")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the report in a browser")
    ap.add_argument("--archive", default=os.environ.get("CLAUDE_INSIGHT_ARCHIVE", DEFAULT_ARCHIVE_DIR),
                    metavar="DIR",
                    help="persistent archive that preserves transcripts beyond Claude Code's "
                         "30-day cleanup so history accumulates (default ~/.claude/insight-archive; "
                         "keep it private to you — a folder shared between people mixes their data)")
    ap.add_argument("--no-archive", action="store_true",
                    help="don't copy this run's transcripts into the archive (still reads an existing one)")
    ap.add_argument("--evidence", metavar="PATH",
                    help="write the de-contaminated evidence bundle (JSON) for the two-model "
                         "analysis pipeline to PATH ('-' for stdout), then continue")
    ap.add_argument("--analysis", metavar="PATH",
                    help="merge an AI analysis (JSON from the Opus stage) into the report's skill map")
    ap.add_argument("--analysis-evidence", metavar="PATH", dest="analysis_evidence",
                    help="the evidence bundle the --analysis was produced from; its run_fingerprint "
                         "is checked against this run so a stale/foreign analysis can't be merged")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the terminal summary (the skill's internal measure pass uses this "
                         "so the score isn't surfaced before the full AI report is ready)")
    args = ap.parse_args(argv)

    files = discover_files(args.path)

    # Default mode: maintain + read the persistent archive so we can analyze more than the
    # ~30 days Claude Code keeps on disk. Skipped when an explicit path is given.
    archive_info = None
    if not args.path:
        archive_dir = os.path.expanduser(args.archive)
        new = updated = 0
        if not args.no_archive:
            new, updated = archive_transcripts(files, archive_dir)
        arch_files = _filter_transcripts(glob.glob(os.path.join(archive_dir, "**", "*.jsonl"), recursive=True))
        merged = _dedupe_sessions(files + arch_files)
        archive_info = {
            "dir": args.archive, "enabled": not args.no_archive,
            "live_sessions": len(files), "archived_sessions": len(arch_files),
            "merged_sessions": len(merged), "new": new, "updated": updated,
        }
        files = merged
        # If most of what we're analyzing comes only from the archive (not this machine's
        # live transcripts), a shared/synced archive could be feeding in someone else's data.
        archive_only = archive_info["merged_sessions"] - archive_info["live_sessions"]
        if archive_only > max(25, 2 * archive_info["live_sessions"]):
            print(f"  Note: {archive_only} of {archive_info['merged_sessions']} analyzed sessions exist "
                  f"only in the archive ({args.archive}), not in your live transcripts. If that archive "
                  f"is shared or synced across people/machines, this report may mix in data that isn't "
                  f"yours — point --archive at a private, per-person path.", file=sys.stderr)

    if not files:
        where = args.path or "~/.claude/projects"
        print(f"No Claude Code transcripts found in {where}.\n"
              f"Point at your transcripts with:  python3 insight.py /path/to/dir", file=sys.stderr)
        return 1

    corpus = parse(files)
    if not corpus.real_prompts:
        print("Found transcripts but no real human-typed prompts to analyze.", file=sys.stderr)
        return 1

    result = analyze(corpus)
    cards, strength = build_action_plan(corpus, result)

    if args.evidence:
        bundle = build_evidence(corpus, result, cards, archive_info)
        text = json.dumps(bundle, indent=2)
        if args.evidence == "-":
            print(text)
        else:
            ep = os.path.abspath(args.evidence)
            os.makedirs(os.path.dirname(ep) or ".", exist_ok=True)
            with open(ep, "w", encoding="utf-8") as f:
                f.write(text)
            if not args.quiet:
                print(f"  Evidence: {ep}", file=sys.stderr)

    analysis = None
    analysis_note = None
    if args.analysis:
        try:
            with open(os.path.expanduser(args.analysis), encoding="utf-8") as f:
                analysis = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Could not read --analysis {args.analysis}: {e}", file=sys.stderr)
            return 1
        # Don't blindly trust the analysis file: it lives at a fixed, reused path, so it
        # may be empty (the AI stage no-op'd) or left over from a different run/person.
        # Validate shape + provenance; on any failure render the deterministic report
        # only, and say so, rather than pasting someone else's verdict into this report.
        current_fp = result.get("fingerprint")
        if not isinstance(analysis, dict) or not analysis.get("skill_map"):
            print("  Note: --analysis had no usable skill map (the AI stage may not have run); "
                  "rendering the deterministic report only.", file=sys.stderr)
            analysis_note = "the AI skill-map stage returned no usable output"
            analysis = None
        elif args.analysis_evidence:
            # Deterministic provenance gate: the analysis is valid for this run only if the
            # evidence it was built from fingerprints to THIS run's data. insight.py wrote
            # that fingerprint, so this check never depends on the model copying anything.
            evidence_fp = None
            try:
                with open(os.path.expanduser(args.analysis_evidence), encoding="utf-8") as f:
                    evidence_fp = (json.load(f).get("meta") or {}).get("run_fingerprint")
            except (OSError, json.JSONDecodeError):
                evidence_fp = None
            if evidence_fp != current_fp:
                print(f"  Note: the --analysis does not match this run (its evidence fingerprint "
                      f"{evidence_fp} != {current_fp}). Ignoring it so it can't leak into this "
                      f"report; rendering the deterministic report only.", file=sys.stderr)
                analysis_note = ("the saved AI analysis was produced from a different run / "
                                 "dataset, so it was not used")
                analysis = None
        else:
            # Manual --analysis with no evidence binding: if the file itself happens to carry a
            # run_fingerprint, honor it; otherwise merge (back-compat with hand-written analyses).
            supplied_fp = analysis.get("run_fingerprint")
            if supplied_fp and current_fp and supplied_fp != current_fp:
                print(f"  Note: the supplied --analysis was produced from a DIFFERENT run "
                      f"(fingerprint {supplied_fp} != {current_fp}). Ignoring it so it can't "
                      f"leak into this report; rendering the deterministic report only.",
                      file=sys.stderr)
                analysis_note = ("the saved AI analysis was produced from a different run / "
                                 "dataset, so it was not used")
                analysis = None

    if args.json:
        payload = {
            "overall": result["overall"], "overall_raw": result["overall_raw"],
            "band": result["band"], "archetype": result["archetype"]["label"],
            "dimensions_raw": result["raw"], "dimensions_adjusted": result["shrunk"],
            "confidence": result["conf"], "detail": result["detail"],
            "cc_version": corpus.cc_version,
            "data_ingested": {
                "files": corpus.files, "projects": len(corpus.projects),
                "bytes": corpus.total_bytes, "user_records": corpus.user_records,
                "real_prompts": len(corpus.real_prompts), "filtered": dict(corpus.filtered),
                "active_hours": round(corpus.active_seconds / 3600, 1),
                "prompt_distribution": result["dist"],
                "archive": archive_info,
            },
            "adoption": build_adoption(corpus),
        }
        print(json.dumps(payload, indent=2))
        return 0

    # Usage & adoption are additive, post-parse views (never touch the scored corpus).
    usage = {
        "mcp_servers": corpus.mcp_servers,
        "commands": corpus.commands,
        "work_types": classify_work_types(corpus),
    }
    adoption = build_adoption(corpus)

    # Render fully before touching the file, so a render error can't leave a 0-byte report.
    html_doc = build_html(corpus, result, cards, strength, archive_info, analysis, analysis_note,
                          usage=usage, adoption=adoption)
    html_doc = bilingual_html(html_doc)
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    # Content-only sibling the /ai-fluency skill publishes via the Artifact tool.
    art_path = os.path.splitext(out_path)[0] + ".artifact.html"
    with open(art_path, "w", encoding="utf-8") as f:
        f.write(_artifact_html(html_doc))

    if not args.quiet:
        print(terminal_summary(corpus, result))
        if archive_info and archive_info["enabled"]:
            print(f"  Archive: {archive_info['merged_sessions']} sessions preserved at "
                  f"{archive_info['dir']} ({archive_info['new']} new, {archive_info['updated']} updated this run).")
        print(f"  Report: {out_path}\n")
    if not args.no_open:
        try:
            webbrowser.open(f"file://{out_path}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
