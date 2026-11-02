#!/usr/bin/env python3
"""
Claude Insight v2 — one-command, zero-install AI-fluency analyzer.

    python3 insight.py

Reads your local Claude Code transcripts (~/.claude/projects/**/*.jsonl),
estimates how skillfully you drive an AI coding agent, and writes a single
self-contained HTML report (~/.claude/insight/ai_fluency_report.html) that opens in your browser.

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
import functools
import glob
import hashlib
import html
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import time
import webbrowser
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timedelta

if sys.version_info < (3, 8):
    sys.exit("Claude Insight needs Python 3.8 or newer (this is %s). "
             "Install it from https://www.python.org/downloads/ or run install.sh / install.ps1."
             % sys.version.split()[0])

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
DEFAULT_REPORT = "~/.claude/insight/ai_fluency_report.html"
DEFAULT_DEEP_ANALYSIS = "~/.claude/insight/deep/analysis.json"
DEFAULT_AUDIT = "~/.claude/insight/audit/holdout.json"   # labeled with --audit; shown in the method tab

GAP_CAP_SECONDS = 300          # idle gaps longer than this are NOT counted as active time
MAX_HUMAN_PROMPT_CHARS = 6000  # anything longer is treated as a paste/injection, not a typed prompt
PROVISIONAL_MIN_PROMPTS = 30   # below this the headline score is shown as a hedged range
STORY_SAY_CHARS = 400          # how much of each assistant message the deep read keeps
STORY_ERR_CHARS = 240          # how much of each failed tool result it keeps

EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
READ_TOOLS = {"read", "grep", "glob"}

# Text that marks a "user"-role record as injected/system rather than typed by the human.
INJECTION_MARKERS = (
    "<task-notification>", "<command-name>", "<command-message>", "<command-args>",
    "<local-command-caveat>", "<local-command-stdout>", "<system-reminder>",
    "<bash-input>", "<bash-stdout>", "caveat: the messages below",
    "[request interrupted", "base directory for this skill", "<user-prompt-submit-hook>",
    "<user-memory-input>", "this session is being continued", "plugin sent a message:",
    # other sessions and the desktop app talking into this one (found by the detector audit)
    "another claude session sent a message", "<agent-message", "<artifact-view-context",
    "background (from the session that started this one)",
)

_PASTED_RE = re.compile(r"<pasted_content\b[^>]*>.*?(</pasted_content>|\Z)", re.S | re.I)

# Subagent system prompts get stored as plain user-role text with no other marker.
# They almost always open with "You are <role>…". This catches the back-door inflation.
_INJECTED_HEAD = re.compile(
    r"^\s*(you are\b|<[a-z][\w-]*[\s>]|base directory for this skill)", re.I
)

# Broad, project-extensible verification matcher (matched against real Bash commands).
VERIFY_RE = re.compile(
    r"\b("
    r"pytest|unittest|jest|vitest|mocha|go test|cargo (test|build|check)|"
    r"npm (run )?(test|build|lint)|yarn (test|build|lint)|pnpm (test|build|lint)|"
    r"ruff|eslint|flake8|mypy|tsc\b|make (test|lint|build|check)|playwright|"
    r"python\d? -m \w|python\d? \S+\.py\b|node \S+\.m?js\b|\.venv/bin/python|lsof -ti|curl .*(localhost|127\.0\.0\.1)|"
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
    r"([\w./\-]+\.(py|js|ts|tsx|jsx|html|css|md|json|jsonc|sh|ya?ml|toml|rs|go|java|cpp|hpp|c|h|cs|rb|sql|"
    r"pdf|txt|csv|xlsx|docx|bat|ps1|astro|vue|svg|xml|ini)\b)"
    r"|([A-Za-z]:\\[^\s\"'<>|]+)"   # Windows paths
    r"|(@\"[^\"]+\")"                 # @"file" references
    r"|((?:/[\w.\-]+){2,})"        # multi-segment paths (not bare /word or </tag>)
    r"|(```)"                      # a code block
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
    rf"arma|monta|traduce|transcribe|cierra|abre|usa|utiliza|trabaja|documenta|organiza|copia|"
    rf"mejora|busca|analiza|revisa|optimiza|ordena|descarga|lee|resume|investiga|compara|guarda|"
    rf"verifica|comprueba|prueba|ajusta|alinea|centra|completa|sigue con){_CL}"
    r"|improve|find|search|analy[sz]e|review|check|read|summari[sz]e|compare|save|test)\b", re.I
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
    r"no sale|no sali[oó]|no compila|no anda|no carga|no aparece|no era eso|no es eso|"
    r"no (borra|muestra|cambia|abre|guarda|respeta)|no pasa eso|es[eo] no era)\b"
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
    # "compilo" is also "how do I compile?"; only a finished compile counts as a report.
    r"prob[eé]|lo prob[eé]|ya lo prob[eé]|corr[ií]|compila bien|ya compil[oó]|compil[oó] bien|captura|pantallazo)\b", re.I
)
CORRECTION_RATE_CEILING = 0.35   # a "high" correction rate; lower is better

# Delegation / planning tool signals.
DELEGATION_TOOLS = {"agent", "task", "workflow", "exitplanmode", "enterplanmode"}

# Dimension weights (sum to 1.0).
# Context and the agent's half of Verification are mostly Claude's own habits, so they weigh
# less; what only YOU can do — brief, review, plan, close — weighs more.
# Context (reading a file before editing it) is something Claude does on its own, so it is shown
# but not scored: its old 10 points were shared out in proportion to the other six.
WEIGHTS = {
    "Direction": 0.22,
    "Verification": 0.22,
    "Iteration": 0.17,
    "Planning": 0.17,
    "Closure": 0.11,
    "Toolcraft": 0.11,
    "Context": 0.0,
}
UNSCORED = {d for d, w in WEIGHTS.items() if not w}
# Opportunity-count targets for per-dimension confidence shrinkage.
TARGET_N = {"Direction": 60, "Verification": 15, "Context": 25, "Iteration": 12, "Toolcraft": 40,
            "Planning": 10, "Closure": 10}

# User-facing labels. "Direction" is shown as "Briefing" so it never collides with the
# "Director" archetype (the dimension measures how well you brief; the archetype, that
# you delegate — different things).
DISPLAY_NAMES = {"Direction": "Briefing", "Verification": "Verification",
                 "Context": "Context-setting", "Iteration": "Iteration", "Toolcraft": "Toolcraft",
                 "Planning": "Planning", "Closure": "Closure"}

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

SKILL_TEACH["Planning"] = {
    "what_it_is": "Talking a piece of work through before the first change: the vision (what and why), then the structure (a plan or options), then the precise requirements.",
    "why_it_matters": "A plan agreed in two messages is cheaper than a build you have to steer back for ten; the agent proposes options and asks what it needs instead of guessing.",
    "how_to_improve": "For anything bigger than a quick fix, open with the idea and ask for the plan or the options before any code. Decide, then ask for the build with the details.",
    "examples": [
        {"before": "make me a bot that sends junior Python job offers to Discord", "after": "I want a portfolio bot that sends me junior Python job offers from my area to Discord. I don't know whether to scrape or use an API. How would you approach it? Give me two options with pros and cons — no code yet."},
        {"before": "add login to the app", "after": "Before touching code: propose how you'd add login to this app (session vs JWT, which files change, what to test) in a short table. I'll pick one and then you implement it."},
    ],
    "practice": "Before your next build, send one message that asks “how would you approach this?” and only then ask for code.",
    "good_looks_like": "Real work starts with a short conversation and an agreed plan; the build order comes last, on top of shared context.",
}
SKILL_TEACH["Closure"] = {
    "what_it_is": "Finishing the work: checking it, leaving it in a commit you decided on, and not letting a session die on an unresolved problem.",
    "why_it_matters": "Work without a commit is easy to lose and hard to undo; a session that ends on “sigue sin funcionar” leaves the problem for later you.",
    "how_to_improve": "When a piece of work is done and checked, ask for the commit yourself with a clear message. If something is still broken when you stop, ask for a short note of where it stands.",
    "examples": [
        {"before": "ok", "after": "Works: the form saves and the list refreshes. Commit it as “Guardar el formulario y refrescar la lista” and tell me what's left for tomorrow."},
        {"before": "sigue sin funcionar, lo dejo", "after": "It still fails with the same error on save. Write a three-line note in NOTAS.md with what we tried and what to try next, and don't commit."},
    ],
    "practice": "End your next working session with “commit this as …” or a note of where it stands.",
    "good_looks_like": "Every piece of work ends checked and committed when you say so, or with a clear note of what's pending.",
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


# A long prompt is usually a short request followed by something pasted (notes, an article,
# another chat's answer). Cues are read from the request only, so pasted text can't pass for
# your own constraints or goals.
PASTE_CHARS = 1000
LEAD_CHARS = 300


_IMAGE_SOURCE_RE = re.compile(r"\[Image: source: [^\]]*\]")


def _cue_text(text):
    text = _IMAGE_SOURCE_RE.sub(" ", text)          # the app's own path to a pasted image
    if len(text) <= PASTE_CHARS:
        return text
    return text.lstrip().split("\n", 1)[0][:LEAD_CHARS]   # the paste usually starts on a new line


def _has_artifact(text):
    """A file, path, function or error named anywhere — a pasted log or code block is real context,
    unlike pasted prose, so this one reads the whole prompt (minus the app's image paths)."""
    return bool(ARTIFACT_RE.search(_IMAGE_SOURCE_RE.sub(" ", text)))


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
        self.mcp_server_tools = defaultdict(Counter)   # server-id -> its tool names, to name UUID connectors
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
        self.sessions = {}              # session_id -> {"project","timeline":[...],"story":[...]}
        self.titles = {}                # session_id -> the chat's name (yours if you renamed it, else Claude's)


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


class Progress:
    """A one-line progress bar on stderr while the report is built, so a 10-second run never
    looks frozen. Silent when stderr isn't a terminal (pipes, the skill's --quiet pass)."""

    WIDTH = 28

    def __init__(self, total, enabled=True, stream=None, spanish=False):
        self.stream = stream or sys.stderr
        self.enabled = enabled and hasattr(self.stream, "isatty") and self.stream.isatty()
        self.total, self.done, self.label = max(1, total), 0, ""
        self.spanish = spanish
        self.start = time.monotonic()
        self.last = 0                       # length of the line on screen, to wipe it fully

    def stage(self, en, es):
        self.label = es if self.spanish else en
        self._draw()

    def tick(self, n=1):
        self.done = min(self.total, self.done + n)
        if self.done == self.total or self.done % 5 == 0:   # redraw a few times a second, not per file
            self._draw()

    def _draw(self):
        if not self.enabled:
            return
        frac = self.done / self.total
        full = int(frac * self.WIDTH)
        bar = "█" * full + "░" * (self.WIDTH - full)
        secs = time.monotonic() - self.start
        left = ""
        if 0.05 < frac < 1:
            eta = secs / frac - secs
            left = f" · ~{eta:.0f} s " + ("restantes" if self.spanish else "left")
        line = f"  {bar} {frac * 100:3.0f}%  {self.label:<34}{left}"
        self.stream.write("\r" + line.ljust(self.last))
        self.last = len(line)
        self.stream.flush()

    def note(self, msg):
        """Print a message on its own line without breaking the bar, then redraw the bar."""
        if not self.enabled:
            print(msg, file=self.stream)
            return
        self.stream.write("\r" + " " * self.last + "\r" + msg + "\n")
        self.last = 0
        self._draw()

    def finish(self):
        if not self.enabled:
            return
        secs = time.monotonic() - self.start
        msg = f"Listo en {secs:.1f} s" if self.spanish else f"Done in {secs:.1f} s"
        self.done = self.total
        self.label = msg
        self._draw()
        self.stream.write("\n")
        self.stream.flush()


def _spanish_locale():
    try:
        import locale
        return (locale.getlocale()[0] or os.environ.get("LANG", "")).lower().startswith(("es", "spanish"))
    except Exception:
        return False


def parse(files, on_file=None):
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
        # The fuller narrative behind `timeline` for the deep read: also what Claude SAID and
        # which tool calls failed. Kept apart so the scoring timeline stays exactly as it was.
        story = []
        titles = {}
        ts_in_file = []
        prompt_idx = 0
        s_tools, s_deleg = Counter(), 0
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
                if e.get("type") == "custom-title" and e.get("customTitle"):
                    titles["custom"] = e["customTitle"]
                elif e.get("type") == "ai-title" and e.get("aiTitle"):
                    titles["ai"] = e["aiTitle"]
                msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                role = e.get("role") or msg.get("role") or e.get("type")
                content = msg.get("content", e.get("content"))

                if role == "assistant":
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                                story.append({"kind": "say", "text": b["text"].strip()[:STORY_SAY_CHARS], "ts": ts})
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                raw = b.get("name", "unknown")
                                srv = _mcp_server(raw)
                                if srv:
                                    c.mcp_servers[srv] += 1
                                    c.mcp_server_tools[srv][raw.rsplit("__", 1)[-1].lower()] += 1
                                plg = _mcp_plugin(raw)
                                if plg:
                                    c.mcp_plugins[plg] += 1
                                name = _denamespace_tool(raw)
                                c.tool_usage[name] += 1
                                s_tools[name] += 1
                                c.total_tool_calls += 1
                                inp = b.get("input", {}) if isinstance(b.get("input"), dict) else {}
                                # Skill tool / Agent subagent_type carry a "<ns>:<name>" id — the
                                # USED signal for plugins driven via skills/subagents, not slash-commands.
                                sk = inp.get("skill") or inp.get("subagent_type")
                                if isinstance(sk, str) and sk:
                                    c.skill_invocations[sk] += 1
                                if name.lower() in DELEGATION_TOOLS:
                                    c.delegation_events += 1
                                    s_deleg += 1
                                if name.lower() == "bash" and inp.get("run_in_background"):
                                    c.delegation_events += 1
                                    s_deleg += 1
                                fpath = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                                cmd = inp.get("command") if name.lower() in SHELL_TOOLS else None
                                tool_ev = {
                                    "kind": "tool", "name": name.lower(),
                                    "file": fpath, "cmd": cmd, "server": srv,
                                }
                                timeline.append(tool_ev)
                                story.append({**tool_ev, "ts": ts})
                    continue

                if role != "user":
                    continue
                c.user_records += 1
                if _is_tool_result(content):
                    c.filtered["tool results"] += 1
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                            story.append({"kind": "error", "text": _text_of(b.get("content"))[:STORY_ERR_CHARS], "ts": ts})
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
                rec = {"text": text, "project": project, "session": session_id, "idx": prompt_idx, "ts": ts}
                c.real_prompts.append(rec)
                timeline.append({"kind": "prompt", "text": text, "rec": rec})
                # The same prompt is often logged twice (plain, then with its pasted image):
                # keep one, the fuller copy.
                if story and story[-1]["kind"] == "prompt" and text.startswith(story[-1]["text"]):
                    story[-1] = {"kind": "prompt", "text": text, "ts": ts}
                else:
                    story.append({"kind": "prompt", "text": text, "ts": ts})

        active = 0.0
        if len(ts_in_file) >= 2:
            ts_in_file.sort()
            active = sum(
                min((ts_in_file[i + 1] - ts_in_file[i]).total_seconds(), GAP_CAP_SECONDS)
                for i in range(len(ts_in_file) - 1)
            )
            c.active_seconds += active
        if timeline:
            c.sessions[session_id] = {"project": project, "timeline": timeline, "story": story,
                                      "stats": {"tools": s_tools, "deleg": s_deleg, "active": active,
                                                "start": min(ts_in_file) if ts_in_file else None}}
        if titles:
            c.titles[session_id] = titles.get("custom") or titles["ai"]
        if on_file:
            on_file()
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


def _has_intent(text):
    """A stated goal or reason. “porque/para que” inside a question (“¿porque no sale nada?”) is
    asking, not explaining, so a cue only counts when its sentence doesn't end in “?” and the
    prompt isn't a bare question."""
    t = _cue_text(text)
    question = "?" in t and not ACTION_VERB.search(t)
    if question:
        return False
    for m in INTENT_CUE.finditer(t):
        end = re.search(r"[.!?\n]", t[m.end():])
        if not (end and end.group() == "?"):
            return True
    return False


def _is_ack(text):
    """A short go-ahead / continuation that answers the agent's own proposal."""
    return (bool(ACK_RE.search(text)) and len(text.split()) <= ACK_MAX_WORDS
            and "?" not in text and not CORRECTION_CUE.search(text))


# --------------------------------------------------------------------------- #
# Detector audit: how often does each keyword detector get it right?
# --------------------------------------------------------------------------- #

# Each detector decides one thing about one prompt. `--audit` samples hits and misses so a
# person (or a reader with this rubric) can label them; `--audit-score` turns the labels into
# precision (of the hits, how many were right) and miss rate (of the misses, how many it should
# have caught), re-running the CURRENT detectors so a fix shows up in the numbers.
AUDIT_DETECTORS = {
    "constraint": (lambda t: bool(CONSTRAINT_CUE.search(_cue_text(t)) and ACTION_VERB.search(_cue_text(t))),
                   "Does the prompt set a rule or limit on HOW to do the work (only X, without Y, don't touch Z, keep W)?"),
    "artifact": (lambda t: _has_artifact(t),
                 "Does it name a specific file, path, function or error?"),
    "intent": (lambda t: _has_intent(t),
               "Does it say what the work is for, or why it is wanted?"),
    "go_ahead": (lambda t: _is_ack(t),
                 "Is it only an approval or a 'continue', with no new request in it?"),
    "correction": (lambda t: bool(CORRECTION_CUE.search(t[:160]) and not PRAISE_CUE.search(t[:160])),
                   "Does it reject or correct what Claude just did?"),
    "checked": (lambda t: _classify_reply(t) == "checked",
                "Does it report what you saw or tried in the result (works, an error, what shows)?"),
    "plan": (lambda t: bool(PLAN_CUE.search(_cue_text(t))),
             "Does it ask for a plan, options or a structure before building?"),
}
# Which prompts each detector is judged on: briefs (not go-aheads), every prompt, or the
# replies that come right after Claude changed something.
_AUDIT_POOL = {"constraint": "briefs", "artifact": "briefs", "intent": "briefs", "plan": "briefs",
               "go_ahead": "all", "correction": "after_change", "checked": "after_change"}


def audit_sample(corpus, per=10, seed=7):
    """Up to `per` hits and `per` misses for every detector, drawn at random but reproducibly."""
    after = []
    for _sid, _proj, timeline in _iter_sessions(corpus):
        saw_tool = False
        for ev in timeline:
            if ev["kind"] == "tool":
                saw_tool = True
                continue
            if saw_tool:
                after.append(ev["text"])
            saw_tool = False
    texts = [p["text"] for p in corpus.real_prompts]
    pools = {"all": texts, "briefs": [t for t in texts if not _is_ack(t)], "after_change": after}
    rng = random.Random(seed)
    items = []
    for name, (fn, question) in AUDIT_DETECTORS.items():
        pool = list(dict.fromkeys(pools[_AUDIT_POOL[name]]))     # unique, order kept
        hits = [t for t in pool if fn(t)]
        misses = [t for t in pool if not fn(t)]
        for decision, group in ((True, hits), (False, misses)):
            for t in rng.sample(group, min(per, len(group))):
                items.append({"id": f"{name}-{len(items):03d}", "detector": name, "question": question,
                              "decision": decision, "text": t[:6000], "label": None})
    return items


def audit_score(items):
    """Per detector: precision of its hits and how many of its misses it should have caught."""
    rows = {}
    for it in items:
        if it.get("label") is None or it["detector"] not in AUDIT_DETECTORS:
            continue
        if _looks_injected(it["text"]):           # no longer counted as one of your prompts
            rows.setdefault(it["detector"], {"hits": 0, "right_hits": 0, "misses": 0, "missed": 0})
            rows[it["detector"]]["filtered"] = rows[it["detector"]].get("filtered", 0) + 1
            continue
        r = rows.setdefault(it["detector"], {"hits": 0, "right_hits": 0, "misses": 0, "missed": 0})
        now = AUDIT_DETECTORS[it["detector"]][0](it["text"])
        if now:
            r["hits"] += 1
            r["right_hits"] += bool(it["label"])
        else:
            r["misses"] += 1
            r["missed"] += bool(it["label"])
    for r in rows.values():
        r["precision"] = r["right_hits"] / r["hits"] if r["hits"] else None
        r["miss_rate"] = r["missed"] / r["misses"] if r["misses"] else None
    return rows


# --------------------------------------------------------------------------- #
# The five dimensions — each returns (score_0_100, detail_dict, evidence_list)
# --------------------------------------------------------------------------- #

def score_direction(corpus):
    prompts = corpus.real_prompts
    if not prompts:
        return 0.0, {"n": 0}, []
    # Briefing is about how you OPEN work, so go-aheads ("sí, hazlo", "sigue, página 26") are
    # left out of the denominator — being brief there is correct, not a weak brief.
    # ...and so are questions and status notes ("¿está bien el comando?", "sale eso"): a question
    # needs no constraint or file. Briefing is scored on requests for work only.
    briefs = [p for p in prompts if not _is_ack(p["text"]) and _is_action_prompt(_cue_text(p["text"]))] \
        or [p for p in prompts if not _is_ack(p["text"])] or prompts
    n = len(briefs)
    constraint = artifact = intent = 0
    weak_examples = []
    for p in briefs:
        t = p["text"]
        lead = _cue_text(t)
        has_artifact = _has_artifact(t)
        has_constraint = bool(CONSTRAINT_CUE.search(lead) and ACTION_VERB.search(lead))
        has_intent = _has_intent(t)
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
        "followups": sum(1 for p in prompts if _is_ack(p["text"])),
        "questions": len(prompts) - n - sum(1 for p in prompts if _is_ack(p["text"])),
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


def _classify_reply(t):
    """How your message right after a change reads: checked, specific_fix, vague_reject,
    rubber_stamp or moved_on (see review_replies)."""
    head = t[:160]
    if CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head):
        specific = bool(re.search(r"\d", t) or ARTIFACT_RE.search(t) or len(t.split()) >= 8)
        return "specific_fix" if specific else "vague_reject"
    if CHECKED_CUE.search(head):
        return "checked"
    if _is_ack(t):
        return "rubber_stamp"
    return "moved_on"


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
            kind = _classify_reply(ev["text"])
            counts[kind] += 1
            if len(examples[kind]) < 4:
                examples[kind].append({"session": sid, "project": project, "text": ev["text"]})
    judged = counts["checked"] + counts["specific_fix"] + counts["vague_reject"] + counts["rubber_stamp"]
    looked = counts["checked"] + counts["specific_fix"]
    return {"counts": counts, "judged": judged,
            "rate": (looked / judged) if judged else None, "examples": examples}


REVIEW_MIN_N = 5          # below this many judged replies, Verification stays agent-only
DOC_EXTS = {".md", ".mdx", ".txt", ".rst", ".csv", ".log"}   # notes and docs don't need a test run
SHELL_TOOLS = {"bash", "powershell"}
VERIFY_SERVERS = {"Claude_Browser", "claude-in-chrome", "computer-use"}
# A prompt that asks for a check ("pruébalo", "run it") means YOU drove the verification.
ASK_VERIFY_RE = re.compile(r"(run it|does it work|confirm|check (it|that)|verify|did it work|"
                           r"c[oó]rre(lo)?|pru[eé]ba(lo)?|verif[ií]ca(lo)?|comprueba|"
                           r"chequea|revisa si|ya funciona|funciona\?)", re.I)


def score_verification(corpus):
    """Every burst of edits (the changes between two of your messages) gets one verdict:
    checked by Claude (a test/build/lint/run), by you (your next message reports what you saw,
    names what broke, or asks for a check), by both, or by nobody. Re-reading the file no longer
    counts. Half the score is Claude's checks, half is yours, and a change nobody checked that
    you waved through with "ok, sigue" counts against."""
    who = Counter({"both": 0, "claude": 0, "you": 0, "none": 0})
    bursts = blind_ok = you_judged = you_looked = 0
    teardown_bonus = 0
    unverified_examples = []
    for sid, project, timeline in _iter_sessions(corpus):
        open_b, claude_ok, files = False, False, []

        def close(reply):
            nonlocal bursts, blind_ok, you_judged, you_looked
            bursts += 1
            kind = _classify_reply(reply) if reply is not None else None
            asked = bool(reply and ASK_VERIFY_RE.search(reply))
            you = asked or kind in ("checked", "specific_fix", "vague_reject")
            if asked or (kind and kind != "moved_on"):
                you_judged += 1
                you_looked += you
            if not claude_ok and kind == "rubber_stamp":
                blind_ok += 1
            who["both" if claude_ok and you else "claude" if claude_ok else "you" if you else "none"] += 1
            if not claude_ok and not you and len(unverified_examples) < 6:
                unverified_examples.append({"session": sid, "project": project,
                                            "files": ", ".join(sorted(set(files))[:3]) or "files"})

        code = False
        for ev in timeline:
            if ev["kind"] == "prompt":
                if open_b and code:
                    close(ev["text"])
                open_b, claude_ok, files, code = False, False, [], False
                continue
            name, cmd = ev["name"], ev.get("cmd") or ""
            if name in EDIT_TOOLS:
                open_b = True
                claude_ok = False          # a check only counts if it came after the last edit
                f = ev.get("file")
                if f:
                    files.append(os.path.basename(f))
                code = code or not (f and os.path.splitext(str(f))[1].lower() in DOC_EXTS)
            elif name in SHELL_TOOLS:
                if TEARDOWN_RE.search(cmd):
                    teardown_bonus = 5
                if open_b and VERIFY_RE.search(cmd):
                    claude_ok = True
            elif open_b and ev.get("server") in VERIFY_SERVERS:
                claude_ok = True           # Claude looked at the running app in a browser
        if open_b and code:
            close(None)
    if bursts == 0:
        return 50.0, {"n": 0, "episodes": 0, "verified": 0, "rate": None}, []
    claude_rate = (who["claude"] + who["both"]) / bursts
    you_rate = (you_looked / you_judged) if you_judged else None
    blind_rate = blind_ok / bursts
    claude_part = 100 * squash(claude_rate, 0.60)
    you_part = 100 * you_rate if you_judged >= REVIEW_MIN_N else claude_part
    score = clamp(0.5 * claude_part + 0.5 * you_part + teardown_bonus - 100 * blind_rate, 0, 100)
    return score, {"n": bursts, "episodes": bursts, "verified": who["claude"] + who["both"], "rate": claude_rate,
                   "who": dict(who), "you_rate": you_rate, "you_judged": you_judged, "blind_ok": blind_ok,
                   "teardown_bonus": teardown_bonus}, unverified_examples[:4]


# Planning & closure cues (bilingual).
PLAN_CUE = re.compile(
    # A bare "plan" / "estructura" / "propuesta" also shows up in pasted answers ("ajuste al plan
    # original"), so only the word used as a request counts (the detector audit measured 40%).
    r"\b((dame|danos|haz|hazme|arma|armemos|hagamos|dime|cu[aá]l (es|ser[ií]a)|el siguiente|primero|"
    r"c[oó]mo hacemos) (el |un |tu )?plan|planifica|planea|primero (dame|dime|expl[ií]ca|ver|pensemos|el plan|la estructura)|"
    r"antes de (hacer|tocar|programar|empezar|implementar)|qu[eé] (recomiendas|opinas|propones|me sugieres)|"
    r"c[oó]mo lo (har[ií]as|ves|plantear[ií]as)|opciones|alternativas|pros y contras|"
    r"(tu|qu[eé]|una) propuesta|(qu[eé]|la|una) (estructura|arquitectura)( (propones|recomiendas|primero|antes))?\?|"
    r"por d[oó]nde (empiezo|empezamos|lo ver[ií]as)|"
    r"how would you|what do you (suggest|recommend|think)|options|trade-?offs|pros and cons|first,? (plan|outline))\b",
    re.I)
WHY_CUE = re.compile(
    r"\b(por qu[eé]|expl[ií]ca(me)?|c[oó]mo funciona|qu[eé] (es|significa|hace)|para qu[eé] sirve|"
    r"why|explain|how does)\b", re.I)
GIT_RE = re.compile(r"\bgit\b")
COMMIT_RE = re.compile(r"\bgit\b[^\n]*\bcommit\b")
PUSH_RE = re.compile(r"\bgit\b[^\n]*\bpush\b")
COMMIT_ASK_RE = re.compile(r"\b(commit|comm?itea|comitea|haz (el )?commit)\b", re.I)
PLAN_TOOLS = {"enterplanmode", "exitplanmode"}
WORK_SESSION_EDITS = 3     # a session with fewer edits than this is a quick fix, not a piece of work


def _work_sessions(corpus):
    for sid, project, timeline in _iter_sessions(corpus):
        if sum(1 for ev in timeline if ev["kind"] == "tool" and ev["name"] in EDIT_TOOLS) >= WORK_SESSION_EDITS:
            yield sid, project, timeline


def score_planning(corpus):
    """Vision → structure → requirements: before the first change of a real piece of work, did you
    talk it through (ask for a plan, options or a structure, or use plan mode) instead of firing
    the build order cold? Plus how often you ask why — the judgement that lets you steer."""
    n = planned = conversation = 0
    examples = []
    for sid, project, timeline in _work_sessions(corpus):
        n += 1
        before = []
        plan_tool = False
        for ev in timeline:
            if ev["kind"] == "tool":
                if ev["name"] in PLAN_TOOLS:
                    plan_tool = True
                if ev["name"] in EDIT_TOOLS:
                    break
                continue
            before.append(ev["text"])
        talked = plan_tool or any(PLAN_CUE.search(_cue_text(t)) for t in before)
        planned += talked
        conversation += len(before) >= 2
        if not talked and before and len(examples) < 6:
            examples.append({"session": sid, "project": project, "text": before[0]})
    prompts = corpus.real_prompts
    why_rate = sum(1 for p in prompts if WHY_CUE.search(p["text"])) / len(prompts) if prompts else 0.0
    if n == 0:
        return 50.0, {"n": 0, "planned": 0, "conversation": 0, "why_rate": why_rate}, []
    score = 100 * (0.5 * squash(planned / n, 0.5) + 0.25 * squash(conversation / n, 0.6)
                   + 0.25 * squash(why_rate, 0.06))
    return score, {"n": n, "planned": planned, "conversation": conversation, "why_rate": why_rate}, examples[:4]


def score_closure(corpus):
    """Does the work get closed? In projects that use git, a real piece of work should end in a
    commit — and you deciding when to commit is part of owning the result. A session whose last
    word is an unresolved complaint ("sigue sin funcionar") counts against."""
    git_projects = {project for _, project, tl in _iter_sessions(corpus)
                    if any(ev["kind"] == "tool" and GIT_RE.search(ev.get("cmd") or "") for ev in tl)}
    n = repo = committed = you_asked = claude_alone = pushes = ended_bad = 0
    examples = []
    for sid, project, timeline in _work_sessions(corpus):
        n += 1
        last_prompt, asked, did_commit = None, False, False
        for ev in timeline:
            if ev["kind"] == "prompt":
                last_prompt = ev["text"]
                asked = asked or bool(COMMIT_ASK_RE.search(ev["text"]))
                continue
            cmd = ev.get("cmd") or ""
            if COMMIT_RE.search(cmd) and not did_commit:
                did_commit = True
                you_asked += asked
                claude_alone += not asked
            pushes += bool(PUSH_RE.search(cmd))
        if project in git_projects:
            repo += 1
            committed += did_commit
            if not did_commit and len(examples) < 6:
                examples.append({"session": sid, "project": project, "text": last_prompt or ""})
        head = (last_prompt or "")[:160]
        ended_bad += bool(CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head))
    if n == 0:
        return 50.0, {"n": 0}, []
    commit_part = squash(committed / repo, 0.6) if repo else 0.5
    score = 100 * (0.6 * commit_part + 0.4 * (1 - ended_bad / n))
    return score, {"n": n, "repo_sessions": repo, "committed": committed, "you_asked": you_asked,
                   "claude_alone": claude_alone, "pushes": pushes, "ended_bad": ended_bad}, examples[:4]


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


class _MemoRe:
    """A compiled pattern whose .search() remembers its answer per text. The margin of error
    re-scores the same prompts many times; with this, only the first pass pays for the regexes."""

    def __init__(self, rx):
        self.rx = rx
        self.search = functools.lru_cache(maxsize=None)(rx.search)

    def __getattr__(self, name):
        return getattr(self.rx, name)


for _name in ("VERIFY_RE", "TEARDOWN_RE", "ARTIFACT_RE", "CONSTRAINT_CUE", "INTENT_CUE", "ACTION_VERB",
              "ACK_RE", "CORRECTION_CUE", "PRAISE_CUE", "CHECKED_CUE", "ASK_VERIFY_RE", "PLAN_CUE",
              "WHY_CUE", "GIT_RE", "COMMIT_RE", "PUSH_RE", "COMMIT_ASK_RE"):
    globals()[_name] = _MemoRe(globals()[_name])


# --------------------------------------------------------------------------- #
# Analysis orchestration
# --------------------------------------------------------------------------- #

def analyze(corpus):
    raw, detail, evidence = {}, {}, {}
    for name, fn in (("Direction", score_direction), ("Verification", score_verification),
                     ("Context", score_context), ("Iteration", score_iteration),
                     ("Toolcraft", score_toolcraft), ("Planning", score_planning),
                     ("Closure", score_closure)):
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


def corpus_subset(corpus, session_ids):
    """The same corpus with only these sessions — enough of it for analyze() to score."""
    keep = set(session_ids)
    sub = Corpus()
    sub.sessions = {k: v for k, v in corpus.sessions.items() if k in keep}
    sub.real_prompts = [p for p in corpus.real_prompts if p["session"] in keep]
    sub.titles = corpus.titles
    sub.projects = {v["project"] for v in sub.sessions.values()}
    for v in sub.sessions.values():
        st = v.get("stats") or {}
        sub.tool_usage.update(st.get("tools") or {})
        sub.delegation_events += st.get("deleg", 0)
        sub.active_seconds += st.get("active", 0.0)
    sub.total_tool_calls = sum(sub.tool_usage.values())
    return sub


JACKKNIFE_FOLDS = 12
MIN_SESSIONS_FOR_MARGIN = 5


def score_margin(corpus, folds=JACKKNIFE_FOLDS, seed=5):
    """How much the score would move with a different slice of your history (a grouped
    jackknife: leave out one of `folds` groups of sessions at a time). Returns the standard
    error of the overall score and of each dimension, or None when there are too few sessions."""
    sids = sorted(corpus.sessions)
    if len(sids) < MIN_SESSIONS_FOR_MARGIN:
        return None
    random.Random(seed).shuffle(sids)
    k = min(folds, len(sids))
    runs = []
    for i in range(k):
        left_out = set(sids[i::k])
        r = analyze(corpus_subset(corpus, [x for x in sids if x not in left_out]))
        runs.append((sum(WEIGHTS[n] * r["shrunk"][n] for n in WEIGHTS), r["shrunk"]))

    def se(vals):
        m = sum(vals) / len(vals)
        return math.sqrt((k - 1) / k * sum((v - m) ** 2 for v in vals))
    return {"overall": se([o for o, _ in runs]), "dims": {n: se([d[n] for _, d in runs]) for n in WEIGHTS}}


def margin_points(se):
    """± for a 95% range, in whole points (at least 1)."""
    return max(1, round(1.96 * se)) if se is not None else None


def is_real_change(delta, se_a, se_b):
    """A change counts only when it is bigger than the noise of both measurements together."""
    if se_a is None or se_b is None:         # a month too thin to measure its noise can't prove a change
        return False
    return abs(delta) > 1.96 * math.sqrt(se_a ** 2 + se_b ** 2)


RECENT_DAYS = 30


def recent_score(corpus, days=RECENT_DAYS):
    """The score of just the sessions started in your last `days` days of activity."""
    if not corpus.last_ts:
        return None
    since = corpus.last_ts - timedelta(days=days)
    sids = [k for k, v in corpus.sessions.items() if (v.get("stats") or {}).get("start") and v["stats"]["start"] >= since]
    sub = corpus_subset(corpus, sids)
    if len(sub.real_prompts) < HISTORY_MIN_PROMPTS:
        return None
    r = analyze(sub)
    m = score_margin(sub)
    return {"overall": r["overall"], "margin": margin_points(m["overall"]) if m else None,
            "sessions": len(sids), "prompts": len(sub.real_prompts)}


HISTORY_MIN_PROMPTS = 30   # a month with fewer real prompts is too thin to score on its own


def _pct(a, b):
    return round(100 * a / b) if b else None


def _dim_metrics(detail):
    """The measures behind each dimension's score, as percentages, so a change in the score can
    be explained by the habit that moved (e.g. “changes nobody checked 25% → 41%”)."""
    d = detail
    v, who = d["Verification"], d["Verification"].get("who") or {}
    eps = v.get("episodes") or 0
    m = {
        "Direction": [("prompts with a constraint", _pct(d["Direction"].get("constraint_rate", 0), 1)),
                      ("prompts naming a file or error", _pct(d["Direction"].get("artifact_rate", 0), 1)),
                      ("prompts that say why", _pct(d["Direction"].get("intent_rate", 0), 1))],
        "Verification": [("changes Claude checked", _pct(who.get("claude", 0) + who.get("both", 0), eps)),
                         ("changes you checked", _pct(who.get("you", 0) + who.get("both", 0), eps)),
                         ("changes nobody checked", _pct(who.get("none", 0), eps))],
        "Iteration": [("prompts that were corrections", _pct(d["Iteration"].get("correction_rate", 0), 1)),
                      ("specific corrections", _pct(d["Iteration"].get("specificity", 0), 1))],
        "Planning": [("work that started with a plan", _pct(d["Planning"].get("planned", 0), d["Planning"].get("n", 0))),
                     ("work talked through first", _pct(d["Planning"].get("conversation", 0), d["Planning"].get("n", 0))),
                     ("prompts asking why", _pct(d["Planning"].get("why_rate", 0), 1))],
        "Closure": [("sessions ended in a commit", _pct(d["Closure"].get("committed", 0), d["Closure"].get("repo_sessions", 0))),
                    ("sessions ended on a complaint", _pct(d["Closure"].get("ended_bad", 0), d["Closure"].get("n", 0)))],
        "Toolcraft": [("tool balance", _pct(d["Toolcraft"].get("evenness", 0), 1))],
        "Context": [("edits after reading the file", _pct(d["Context"].get("rate", 0) or 0, 1))],
    }
    return {dim: [(label, val) for label, val in rows if val is not None] for dim, rows in m.items()}


def monthly_history(corpus, on_month=None):
    """Your score month by month, recomputed from the transcripts themselves: each session
    counts in the month it started, and each month is scored with exactly the same rules as
    the whole report — so the evolution is there from the first run, no saved runs needed.
    Works from the corpus already parsed (no second read of the transcripts)."""
    by_month = defaultdict(list)
    for sid, s in corpus.sessions.items():
        start = (s.get("stats") or {}).get("start")
        if start:
            by_month[start.astimezone().strftime("%Y-%m")].append(sid)
    rows = []
    this_month = datetime.now().astimezone().strftime("%Y-%m")
    for month in sorted(by_month):
        c = corpus_subset(corpus, by_month[month])
        row = {"month": month, "sessions": len(by_month[month]), "prompts": len(c.real_prompts), "overall": None,
               "current": month == this_month}
        if len(c.real_prompts) >= HISTORY_MIN_PROMPTS:
            r = analyze(c)
            row.update(overall=r["overall"], band=r["band"],
                       dims={n: round(r["shrunk"][n]) for n in WEIGHTS}, metrics=_dim_metrics(r["detail"]),
                       se=score_margin(c, folds=8))
        rows.append(row)
        if on_month:
            on_month()
    return rows


def build_action_plan(corpus, result):
    """Growth cards ranked by impact = (target - score) * weight. The teaching copy
    comes from SKILL_TEACH; user-specific evidence comes from result['evidence']."""
    TARGET = 85
    cards = []
    for name in WEIGHTS:
        if name in UNSCORED:
            continue
        score = result["shrunk"][name]
        impact = (TARGET - score) * WEIGHTS[name]
        cards.append({"dim": name, "score": round(score), "impact": impact,
                      "weak": result["evidence"].get(name, []),
                      "detail": result["detail"][name]})
    cards.sort(key=lambda c: c["impact"], reverse=True)
    # strength callout = highest shrunk score
    strength = max((n for n in WEIGHTS if n not in UNSCORED), key=lambda n: result["shrunk"][n])
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


# --------------------------------------------------------------------------- #
# Deep read — every conversation as an episode
# --------------------------------------------------------------------------- #
# The numbers above judge prompts one by one. The deep read follows each task as a story:
# what you asked, what Claude did, how you answered, how it ended. insight.py cuts the
# stories and picks which to read; the ai-fluency-deep workflow reads them; insight.py then
# checks every quote against your real transcripts before anything reaches the report.

DEEP_SCHEMA = "claude-insight-deep/1"
DEPTHS = {"rapida": 20, "normal": 50, "recientes": 50, "justa": 50, "completa": None}
DOUBLE_READ_BATCHES = 2        # batches a second, independent reader also reads (to measure agreement)
DEEP_BATCH_SIZE = 8            # episodes per reading agent
EPISODE_CHARS = 6000           # budget for one episode's condensed transcript (~1.5k tokens)
EPISODE_GAP_MIN = 30           # a pause this long starts a new episode
_ES_HINT_RE = re.compile(r"\b(que|de|el|la|los|las|para|con|por|una?|esto|eso|haz|hazlo|pero|como|ahora)\b", re.I)
_IMAGE_REF_RE = re.compile(r"\[Image: source: [^\]]*\]")


def _opens_task(text):
    """Does this prompt start a new task (vs. answer, correct or approve the current one)?"""
    head = text[:160]
    if _is_ack(text) or (CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head)):
        return False
    return _is_action_prompt(text) or len(text.split()) >= 8


def _episode_lines(events):
    """A compact, readable transcript of one episode: your words in full, Claude's actions
    collapsed to one line per burst, errors marked."""
    lines, burst = [], []

    def flush():
        if burst:
            shown = ", ".join(burst[:8]) + (f" (+{len(burst) - 8} más)" if len(burst) > 8 else "")
            lines.append(("tool", f"· Claude usó: {shown}"))
            burst.clear()

    for ev in events:
        k = ev["kind"]
        if k == "tool":
            what = ev["name"]
            if ev.get("file"):
                what += f" {os.path.basename(str(ev['file']))}"
            elif ev.get("cmd"):
                what += f": {_scrub_paths(ev['cmd'])[:90]}"
            burst.append(what)
            continue
        flush()
        if k == "prompt":
            said = _IMAGE_REF_RE.sub("[imagen]", ev["text"])
            lines.append(("prompt", f"TÚ: {_scrub_paths(said)[:900]}"))
        elif k == "say":
            lines.append(("say", f"CLAUDE: {_scrub_paths(ev['text'])}"))
        elif k == "error":
            lines.append(("error", f"✗ error: {_scrub_paths(ev['text'])[:200]}"))
    flush()
    # Over budget: your prompts and Claude's last word always stay; trim the middle.
    last_say = max((i for i, (k, _) in enumerate(lines) if k == "say"), default=-1)
    keep = lambda i: lines[i][0] == "prompt" or i == last_say  # noqa: E731
    size = lambda: sum(len(t) for _, t in lines)  # noqa: E731
    if size() > EPISODE_CHARS:
        lines = [(k, t if keep(i) or len(t) <= 160 else t[:160] + " […]") for i, (k, t) in enumerate(lines)]
    while size() > EPISODE_CHARS:
        middle = [i for i in range(len(lines)) if not keep(i) and lines[i][0] != "gap"]
        if not middle:
            break
        lines[middle[len(middle) // 2]] = ("gap", "· […]")
        # merge consecutive gaps so the marker doesn't repeat
        lines = [x for j, x in enumerate(lines) if not (x[0] == "gap" and j and lines[j - 1][0] == "gap")]
        last_say = max((i for i, (k, _) in enumerate(lines) if k == "say"), default=-1)
    return "\n".join(t for _, t in lines)


def _summarize_episode(ep):
    events = ep["events"]
    prompts = [e["text"] for e in events if e["kind"] == "prompt"]
    tools = [e for e in events if e["kind"] == "tool"]
    errors = sum(1 for e in events if e["kind"] == "error")
    corrections = vague = stamps = 0
    edited = False
    for e in events:
        if e["kind"] == "tool":
            edited = edited or e["name"] in EDIT_TOOLS
            continue
        if e["kind"] != "prompt" or e is events[0]:
            continue
        head = e["text"][:160]
        if CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head):
            corrections += 1
            if not (re.search(r"\d", e["text"]) or ARTIFACT_RE.search(e["text"]) or len(e["text"].split()) >= 8):
                vague += 1
        elif edited and _is_ack(e["text"]) and not CHECKED_CUE.search(head):
            stamps += 1
        edited = False
    stamps_ts = [e["ts"] for e in events if e.get("ts")]
    start, end = (min(stamps_ts), max(stamps_ts)) if stamps_ts else (None, None)
    friction = corrections + vague + 0.5 * min(errors, 6) + 0.5 * max(0, len(prompts) - 4)
    return {
        "id": ep["id"], "session": ep["session"], "project": _project_label(ep["project"]),
        "date": f"{start:%Y-%m-%d}" if start else None,
        "start": start.isoformat() if start else None,
        "minutes": round((end - start).total_seconds() / 60) if start else None,
        "prompts": prompts,
        "metrics": {"prompts": len(prompts), "tool_calls": len(tools),
                    "edits": sum(1 for t in tools if t["name"] in EDIT_TOOLS), "errors": errors,
                    "corrections": corrections, "vague_corrections": vague, "rubber_stamps": stamps},
        "friction": round(friction, 1),
        "transcript": _episode_lines(events),
    }


def build_episodes(corpus):
    """Cut every session into tasks. A new episode starts on a prompt that opens fresh work
    after Claude already did something, or after a long pause."""
    out = []
    for sid, s in corpus.sessions.items():
        cur, work, last_ts, n = None, 0, None, 0
        for ev in s.get("story") or []:
            if ev["kind"] == "prompt":
                ts = ev.get("ts")
                gap = bool(ts and last_ts and (ts - last_ts).total_seconds() > EPISODE_GAP_MIN * 60)
                if cur is None or gap or (work and _opens_task(ev["text"])):
                    n += 1
                    cur = {"id": f"{sid[:8]}-{n}", "session": sid, "project": s["project"], "events": []}
                    out.append(cur)
                    work = 0
            if cur is None:
                continue
            cur["events"].append(ev)
            work += ev["kind"] == "tool"
            last_ts = ev.get("ts") or last_ts
    return [{**_summarize_episode(e), "title": corpus.titles.get(e["session"])} for e in out]


def select_episodes(episodes, depth="normal"):
    """Which episodes get a full read: half the roughest, a quarter the most recent, a
    quarter that went smoothly (so the read isn't only about failures). Deterministic."""
    limit = DEPTHS.get(depth, DEPTHS["normal"])
    pool = [e for e in episodes if e["metrics"]["tool_calls"] or e["metrics"]["prompts"] >= 2]
    if depth == "recientes":
        # Your latest chats, one conversation each — the one where the most work happened —
        # so the deep read matches the "your last chats" table one to one.
        best = {}
        for e in pool:
            cur = best.get(e["session"])
            if cur is None or (e["metrics"]["tool_calls"], e["metrics"]["prompts"]) > (cur["metrics"]["tool_calls"], cur["metrics"]["prompts"]):
                best[e["session"]] = e
        last = {}
        for e in pool:
            last[e["session"]] = max(last.get(e["session"], ""), e["start"] or "")
        sessions = sorted(best, key=lambda sid: last[sid], reverse=True)[:limit]
        return sorted((best[sid] for sid in sessions), key=lambda e: e["start"] or "")
    if limit is None or len(pool) <= limit:
        return sorted(pool, key=lambda e: e["start"] or "")
    if depth == "justa":
        return _stratified(pool, limit)
    picked, seen = [], set()

    def take(items, k):
        for e in items:
            if k <= 0:
                break
            if e["id"] not in seen:
                seen.add(e["id"])
                picked.append(e)
                k -= 1

    take(sorted(pool, key=lambda e: -e["friction"]), (limit + 1) // 2)
    take(sorted(pool, key=lambda e: e["start"] or "", reverse=True), limit // 4)
    smooth = [e for e in pool if e["friction"] == 0 and e["metrics"]["edits"]]
    take(sorted(smooth, key=lambda e: hashlib.sha256(e["id"].encode()).hexdigest()), limit - len(picked))
    take(sorted(pool, key=lambda e: hashlib.sha256(e["id"].encode()).hexdigest()), limit - len(picked))
    return sorted(picked, key=lambda e: e["start"] or "")


def _stratified(pool, limit, seed="justa-1"):
    """A fair sample: every (project, kind of work) group gets seats in proportion to its size
    (largest remainder, at least one while seats last), and within a group the pick is random
    but reproducible. No preference for the roughest or the newest conversations."""
    groups = defaultdict(list)
    for e in pool:
        kind = "build" if e["metrics"]["edits"] else "talk"
        groups[(e["project"], kind)].append(e)
    total = len(pool)
    quota = {g: limit * len(v) / total for g, v in groups.items()}
    seats = {g: int(q) for g, q in quota.items()}
    order = sorted(groups, key=lambda g: (-(quota[g] - seats[g]), -len(groups[g]), str(g)))
    for g in order:                                   # hand out the seats left by rounding down
        if sum(seats.values()) >= limit:
            break
        seats[g] += 1
    rank = lambda e: hashlib.sha256(f"{seed}:{e['id']}".encode()).hexdigest()  # noqa: E731
    picked = [e for g, v in groups.items() for e in sorted(v, key=rank)[:seats[g]]]
    return sorted(picked, key=lambda e: e["start"] or "")


def _prompt_language(corpus):
    texts = [p["text"] for p in corpus.real_prompts[-400:]]
    es = sum(1 for t in texts if len(_ES_HINT_RE.findall(t)) >= 2)
    return "es" if texts and es / len(texts) >= 0.3 else "en"


def write_deep_bundle(corpus, result, out_dir, depth="normal"):
    """Write the episodes picked for a deep read as small batch files (one per reading
    agent) plus an index. Old batches are removed first so a stale one is never read."""
    out_dir = os.path.abspath(os.path.expanduser(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    for old in glob.glob(os.path.join(out_dir, "batch-*.json")):
        os.remove(old)
    episodes = build_episodes(corpus)
    chosen = select_episodes(episodes, depth)
    batches = []
    for i in range(0, len(chosen), DEEP_BATCH_SIZE):
        name = f"batch-{i // DEEP_BATCH_SIZE + 1:02d}.json"
        payload = [{k: e[k] for k in ("id", "title", "project", "date", "minutes", "metrics", "transcript")}
                   for e in chosen[i:i + DEEP_BATCH_SIZE]]
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            json.dump({"episodes": payload}, f, ensure_ascii=False, indent=1)
        batches.append(os.path.join(out_dir, name))
    index = {
        "schema": DEEP_SCHEMA, "run_fingerprint": result.get("fingerprint"), "depth": depth,
        "lang": _prompt_language(corpus), "episodes_total": len(episodes), "episodes_read": len(chosen),
        "batches": batches, "double": min(DOUBLE_READ_BATCHES, len(batches)) if depth == "justa" else 0,
    }
    with open(os.path.join(out_dir, "bundle.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    return index


def _norm_quote(s):
    s = _scrub_paths(str(s or "")).replace("“", "").replace("”", "").replace('"', "").replace("…", "")
    return " ".join(s.lower().split())


def _guess_habit(you_said):
    """Which dimension a rewrite trains, for reads made before the workflow tagged it:
    rewriting a correction trains Iteration; rewriting anything else trains the brief."""
    head = str(you_said or "")[:160]
    return "Iteration" if CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head) else "Direction"


def _chat_ref_html(title, project, date, session):
    """'Chat name · project · date' plus the command that reopens that chat."""
    bits = [b for b in (title and f"“{title}”", project, date) if b]
    resume = f'<code class="resume" title="Reopen this chat">claude --resume {_esc(session)}</code>' if session else ""
    return f'<span class="loc">{_esc(" · ".join(bits))}</span>{resume}'


def verify_deep_analysis(deep, corpus):
    """Keep only what is grounded: every 'you said' and every pattern example must be text
    you really typed in the episode it names; every rule must cite a real episode. Anything
    else is dropped and counted, so the report can say how much was thrown out."""
    episodes = {e["id"]: e for e in build_episodes(corpus)}
    said = {eid: [_norm_quote(p) for p in e["prompts"]] for eid, e in episodes.items()}

    def grounded(eid, quote):
        q = _norm_quote(quote)
        return bool(q) and len(q) >= 4 and any(q in p for p in said.get(eid, []))

    dropped = Counter()
    reads = []
    for r in deep.get("reads") or []:
        if not isinstance(r, dict) or r.get("episode_id") not in episodes:
            dropped["unknown episode"] += 1
            continue
        if r.get("you_said") and not grounded(r["episode_id"], r["you_said"]):
            dropped["quote not found"] += 1
            r = {**r, "you_said": None, "better": None}
        e = episodes[r["episode_id"]]
        habit = r.get("habit") if r.get("habit") in WEIGHTS else _guess_habit(r.get("you_said"))
        if "habit" in (r.get("disputed") or []):
            habit = None                                  # two readers, two answers: no label
        reads.append({**r, "habit": habit, "project": e["project"], "date": e["date"], "title": e.get("title"),
                      "session": e["session"], "friction_score": e["friction"]})
    patterns = []
    for p in deep.get("patterns") or []:
        if not isinstance(p, dict):
            continue
        ex = [x for x in p.get("examples") or [] if isinstance(x, dict) and grounded(x.get("episode_id"), x.get("quote"))]
        dropped["quote not found"] += len(p.get("examples") or []) - len(ex)
        if ex:
            patterns.append({**p, "examples": [{**x, "project": episodes[x["episode_id"]]["project"]} for x in ex]})
        else:
            dropped["pattern without evidence"] += 1
    rules = []
    for r in deep.get("claude_md_rules") or []:
        if isinstance(r, dict) and any(i in episodes for i in r.get("episode_ids") or []):
            rules.append(r)
        else:
            dropped["rule without evidence"] += 1
    return {"summary": deep.get("summary") or "", "depth": deep.get("depth"), "lang": deep.get("lang"),
            "episodes_total": len(episodes), "reads": reads, "patterns": patterns, "rules": rules,
            "dropped": dict(dropped), "agreement": deep.get("agreement") or {}}


_OUTCOME_LABEL = {"resolved": ("resolved", "s"), "partial": ("partly resolved", "w"),
                  "abandoned": ("abandoned", "w"), "unclear": ("unclear outcome", "ld")}
_FRICTION_LABEL = {"you": "your brief", "claude": "Claude", "environment": "the environment", "none": "no friction"}


_HL_QUOTE = re.compile(r"(&quot;[^&]{1,80}?&quot;|“[^”]{1,80}?”)")
_HL_NUM = re.compile(r"\b\d+(?:[.,]\d+)?(?: de (?:las |los |tus )?\d+| of (?:the |your )?\d+)?%?")
_HL_SEQ = re.compile(r"[\wáéíóúñ]+(?: → [\wáéíóúñ]+)+")


def _highlight(text):
    """Escape a written summary and mark what the eye should catch: numbers (“38 de las 50”),
    your quoted words and arrow sequences (visión → estructura → requisitos)."""
    out = _esc(text)
    out = _HL_SEQ.sub(lambda m: f'<mark class="hl-k">{m[0]}</mark>', out)
    out = _HL_QUOTE.sub(lambda m: f'<mark class="hl-q">{m[0]}</mark>', out)
    return _HL_NUM.sub(lambda m: f'<b class="hl-n">{m[0]}</b>', out)


def _deep_section_html(deep):
    if not deep or not (deep.get("reads") or deep.get("patterns")):
        return ""
    reads = deep["reads"]
    parts = ['<section><h3>Your chats in depth</h3>']
    dropped = sum((deep.get("dropped") or {}).values())
    if deep.get("depth") == "justa":
        parts.append(f'<p class="def">Read {len(reads)} of your {deep["episodes_total"]} conversations end to end — '
                     f'picked at random, in proportion to each project and kind of work.</p>')
    else:
        parts.append(f'<p class="def">Read {len(reads)} of your {deep["episodes_total"]} conversations end to end — '
                     f'the roughest, some that went well, and the most recent.</p>')
    if deep.get("summary"):
        parts.append(f'<p class="assess deep-sum" style="margin-top:12px">{_highlight(deep["summary"])}</p>')
    if dropped:
        parts.append(f'<p class="rate">{dropped} quotes were discarded because they did not match your transcripts word for word.</p>')

    by = Counter(r.get("friction_by") for r in reads if r.get("friction_by"))
    if by:
        top = max(by.values())
        hue = {"you": "#d95926", "claude": "#3987e5", "environment": "#c98500", "none": "#199e70"}
        rows = _hbars([(_FRICTION_LABEL.get(k, k), by[k]) for k in ("you", "claude", "environment", "none") if by.get(k)],
                      " chats", colors=[hue[k] for k in ("you", "claude", "environment", "none") if by.get(k)],
                      total=len(reads))
        parts.append(f'<p class="def" style="margin-top:14px">Where the friction came from:</p>{rows}')

    # Vision → structure → requirements, and who caught the errors (reads from the newer workflow).
    judged = [r for r in reads if "vision" in r]
    if judged:
        rows = "".join(
            f'<div class="bar-item has-tip" data-c="{hue}"><span class="tt">{_esc(label)} · {k} of {len(judged)} chats</span>'
            f'<div class="bl">{_esc(label)}</div>'
            f'<div class="bt"><i style="width:{100 * k / len(judged):.0f}%;background:{hue}"></i></div>'
            f'<div class="bv">{k}/{len(judged)}</div></div>'
            for (label, k), hue in zip((("vision", sum(bool(r.get("vision")) for r in judged)),
                             ("structure before code", sum(bool(r.get("structure")) for r in judged)),
                             ("precise requirements", sum(bool(r.get("requirements")) for r in judged)),
                             ("you caught an error", sum(bool(r.get("caught_error")) for r in judged)),
                             ("ended in a commit", sum(bool(r.get("committed")) for r in judged))),
                            ("#3987e5", "#9085e9", "#d55181", "#d95926", "#e66767")))
        parts.append('<p class="def" style="margin-top:14px">How the conversations were built '
                     '(vision → structure → requirements):</p>' + rows)

    agree = deep.get("agreement") or {}
    if agree.get("episodes"):
        labels = {"outcome": "outcome", "friction_by": "who caused the friction", "habit": "habit to train",
                  "vision": "vision", "structure": "structure before code", "requirements": "precise requirements",
                  "caught_error": "you caught an error", "committed": "ended in a commit"}
        fields = [(labels[f], agree["fields"][f]) for f in labels if f in (agree.get("fields") or {})]
        rows = _hbars([(lab, round(100 * a / n)) for lab, (a, n) in fields if n], "%",
                      colors=["#3ccf6e" if a / n >= 0.8 else ("#7fb2ff" if a / n >= 0.6 else "#f0a35e")
                              for _, (a, n) in fields if n])
        parts.append(f'<p class="def" style="margin-top:14px">How often two independent readers agreed '
                     f'({agree["episodes"]} conversations read twice):</p>{rows}'
                     '<p class="rate">Where they disagreed, that answer is treated as uncertain (a case whose '
                     'habit they disagreed on is shown without one).</p>')

    if deep.get("patterns"):
        parts.append('<p class="def" style="margin-top:18px"><b>Your recurring habits</b></p>')
        for p in deep["patterns"]:
            ex = "".join(f'<li>“{_esc(x["quote"][:160])}” <span class="loc">— {_esc(x["project"])}</span></li>'
                         for x in p["examples"][:3])
            parts.append(
                f'<div class="card prio"><div class="ph">Seen in {int(p.get("count") or len(p["examples"]))} chats</div>'
                f'<h4>{_esc(p.get("title", ""))}</h4><p class="how">{_esc(p.get("description", ""))}</p>'
                f'<ul class="ev">{ex}</ul></div>')

    if deep.get("rules"):
        text = "\n".join(f"- {r['rule']}" for r in deep["rules"])
        why = "".join(f'<li>{_esc(r["rule"])} <span class="loc">— {_esc(r.get("why", ""))}</span></li>'
                      for r in deep["rules"])
        parts.append('<p class="def" style="margin-top:18px"><b>Rules you keep repeating</b> — '
                     'paste them into your CLAUDE.md so you never have to say them again:</p>'
                     f'<pre class="rules">{_esc(text)}</pre><ul class="ev">{why}</ul>')

    cards = ""
    for r in sorted(reads, key=lambda r: -r.get("friction_score", 0)):
        oc, cls = _OUTCOME_LABEL.get(r.get("outcome"), _OUTCOME_LABEL["unclear"])
        fb = r.get("friction_by")
        ba = ""
        if r.get("you_said") and r.get("better"):
            ba = (f'<div class="ba"><div class="before"><span>You said</span>“{_esc(r["you_said"][:400])}”</div>'
                  f'<div class="after"><span>Better</span>“{_esc(r["better"][:600])}”</div></div>')
        cards += (
            f'<div class="card" id="read-{_esc(r["episode_id"])}"><div class="ph">{_esc((r.get("title") + " · ") if r.get("title") else "")}{_esc(r["project"])} · {_esc(r.get("date") or "")}'
            f'<span class="tag {cls}">{_esc(oc)}</span>'
            + (f'<span class="tag ld">friction: {_esc(_FRICTION_LABEL.get(fb, fb))}</span>' if fb and fb != "none" else "")
            + "".join(f'<span class="tag {"s" if r.get(k) else "ld"}">{"✓" if r.get(k) else "✗"} {_esc(lab)}</span>'
                      for k, lab in (("vision", "vision"), ("structure", "structure"), ("requirements", "requirements"))
                      if k in r)
            + ('<span class="tag s">you caught an error</span>' if r.get("caught_error") else "")
            + ('<span class="tag s">commit</span>' if r.get("committed") else "")
            + f'</div><h4>{_esc(r.get("goal", ""))}</h4>'
            + (f'<p class="rate">Reopen it: <code class="resume">claude --resume {_esc(r["session"])}</code></p>' if r.get("session") else "")
            + (f'<p class="why"><b>What was missing.</b> {_esc(r["missing"])}</p>' if r.get("missing") else "")
            + (f'<p class="why"><b>Key moment.</b> {_esc(r["key_moment"])}</p>' if r.get("key_moment") else "")
            + ba
            + (f'<p class="why" style="margin-top:8px"><b>Why.</b> {_esc(r["why"])}</p>' if r.get("why") and ba else "")
            + '</div>')
    parts.append(f'<details style="margin-top:14px"><summary>Every conversation read ({len(reads)})</summary>'
                 f'<div style="margin-top:12px">{cards}</div></details>')
    parts.append('</section>')
    return "".join(parts)


CASES_LIMIT = 40


def _archived_case_reads(deep, deep_dir, corpus):
    """The cases list may also draw on earlier deep reads kept as analysis-*.json next to the
    current one — every quote re-checked against today's transcripts. Only the cases use them:
    the friction, agreement and vision → structure numbers stay on the current (fair) sample."""
    reads = list(deep.get("reads") or [])
    seen = {r["episode_id"] for r in reads}
    for path in sorted(glob.glob(os.path.join(deep_dir, "analysis-*.json")), reverse=True):
        try:
            with open(path, encoding="utf-8") as f:
                old = verify_deep_analysis(json.load(f), corpus)
        except (OSError, json.JSONDecodeError):
            continue
        for r in old["reads"]:
            if r["episode_id"] not in seen:
                seen.add(r["episode_id"])
                reads.append(r)
    return reads


def _cases_html(deep, limit=CASES_LIMIT):
    """Every verified “you said → better” from the deep read, as a filterable list: what you
    were after, what you typed, how to ask it, and why — tinted with the habit it trains."""
    cases = [r for r in (deep or {}).get("case_reads") or (deep or {}).get("reads") or []
             if r.get("you_said") and r.get("better")]
    if not cases:
        return ""
    order = sorted(WEIGHTS, key=lambda d: -WEIGHTS[d])
    rank = lambda r: (order.index(r["habit"]) if r.get("habit") in order else 99, -r.get("friction_score", 0))  # noqa: E731
    # Over the limit, share the seats round-robin across habits so the rarer ones aren't cut.
    groups = defaultdict(list)
    for r in sorted(cases, key=rank):
        groups[r.get("habit")].append(r)
    picked = []
    while len(picked) < limit and any(groups.values()):
        for h in list(groups):
            if groups[h] and len(picked) < limit:
                picked.append(groups[h].pop(0))
    cases = sorted(picked, key=rank)
    counts = Counter(r.get("habit") for r in cases)
    chips = (f'<button type="button" class="chip on" data-filter="all">All <b>{len(cases)}</b></button>'
             + "".join(f'<button type="button" class="chip" data-filter="{d}" style="--hue:{DIM_HUES[d]}">'
                       f'<i></i>{_esc(DISPLAY_NAMES[d])} <b>{counts[d]}</b></button>' for d in order if counts[d]))
    items = ""
    for i, r in enumerate(cases, 1):
        habit = r.get("habit") if r.get("habit") in DIM_HUES else None
        hue = DIM_HUES.get(habit, "var(--ac-soft)")
        where = " · ".join(x for x in (f"“{r['title']}”" if r.get("title") else "", _project_short(r["project"]),
                                       r.get("date") or "") if x)
        items += (
            f'<article class="case" data-habit="{habit or ""}" style="--hue:{hue}">'
            f'<div class="case-h"><span class="case-n">{i:02d}</span>'
            + (f'<span class="case-tag">{_esc(DISPLAY_NAMES[habit])}</span>' if habit else "")
            + f'<span class="case-w">{_esc(where)}</span></div>'
            + (f'<p class="case-goal"><b>You wanted:</b> {_esc(r["goal"])}</p>' if r.get("goal") else "")
            + f'<div class="ba"><div class="before"><span>You said</span>“{_esc(r["you_said"][:500])}”</div>'
            f'<div class="after"><span>Better</span>“{_esc(r["better"][:700])}”</div></div>'
            + (f'<p class="why"><b>Why.</b> {_esc(r["why"])}</p>' if r.get("why") else "")
            + (f'<code class="resume">claude --resume {_esc(r["session"])}</code>' if r.get("session") else "")
            + '</article>')
    return (f'<section><h3>{len(cases)} cases from your chats: what you asked and how to ask it</h3>'
            '<p class="def">Every quote was checked word for word against your transcripts. Filter by the habit '
            'each case trains.</p>'
            f'<div class="chips">{chips}</div><div class="cases">{items}</div></section>')


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


# claude.ai connectors are logged under a UUID; the tools they expose say which service it is.
_CONNECTOR_HINTS = [
    ("Notion", ("notion-",)),
    ("Gmail", ("create_draft", "search_threads", "label_thread", "mark_thread_spam")),
    ("Google Calendar", ("create_event", "list_events", "suggest_time", "respond_to_event")),
    ("Google Sheets", ("get_spreadsheet", "append_values", "update_values", "batch_clear_values")),
    ("Google Slides", ("read_presentation", "update_presentation", "read_slide_page")),
    ("Google Drive", ("search_files", "download_file_content", "share_file", "list_recent_files")),
    ("Google Docs", ("read_doc", "update_doc")),
    ("Claude Docs", ("guide", "batch")),
    ("Visualizer", ("show_widget",)),
]
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SERVER_KINDS = [("Browser & screen", "#c98500"), ("Claude app", "#3987e5"),
                ("Connectors", "#d55181"), ("Other MCP", "#199e70")]


def _connector_name(server, tools):
    """A readable name for a UUID connector, guessed from the tools it exposes."""
    names = set(tools or ())
    for label, hints in _CONNECTOR_HINTS:
        if any(h in names or any(n.startswith(h) for n in names if h.endswith("-")) for h in hints):
            return label
    return f"Connector {server[:8]}"


def _server_kind(server):
    s = server.lower()
    if s in {"claude_browser", "claude-in-chrome", "computer-use"} or "browser" in s or "chrome" in s:
        return "Browser & screen"
    if s.startswith("ccd_"):
        return "Claude app"
    if _UUID_RE.match(server) or s.startswith("claude_ai_"):
        return "Connectors"
    return "Other MCP"


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
        hue = dict(SERVER_KINDS)
        shown = servers.most_common(12)
        for server, n in shown:
            name, cat = _server_display(server)
            if _UUID_RE.match(server):
                name = _connector_name(server, (usage.get("mcp_server_tools") or {}).get(server))
            tag = f' <span class="loc">{_esc(cat)}</span>' if cat != "—" else ""
            rows += (f'<div class="bar-item" title="{_esc(server)}"><div class="bl">{_esc(name)}{tag}</div>'
                     f'<div class="bt"><i style="width:{100 * n / top:.0f}%;background:{hue[_server_kind(server)]}"></i></div>'
                     f'<div class="bv">{n:,}</div></div>')
        kinds = [k for k, _ in SERVER_KINDS if any(_server_kind(sv) == k for sv, _ in shown)]
        parts.append(f'<p class="def">MCP-server calls {_esc(span)}:</p>'
                     + _legend([(k, hue[k]) for k in kinds]) + rows)
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
        # Seven buckets, seven fixed hues (the validated five, then violet and red); Other stays gray.
        work_hue = dict(zip(WORK_TYPE_BUCKETS, SERIES_HUES + ["#9085e9", OTHER_HUE]))
        for bucket in WORK_TYPE_BUCKETS:
            p = pct.get(bucket, 0.0)
            n = (wt.get("counts") or {}).get(bucket, 0)
            # A real-but-tiny share must not read "0%" (e.g. 60 subagent calls among 35k).
            label = "<1%" if 0 < p < 0.5 else f"{p:.0f}%"
            bars += (f'<div class="bar-item"><div class="bl">{_esc(bucket)}</div>'
                     f'<div class="bt"><i style="width:{p:.0f}%;background:{work_hue[bucket]}"></i></div>'
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
    if "scratch-workspaces" in name:                         # desktop-app chats started without a folder
        return "No folder"
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
    "Planning": "Right now most real work starts with the build order — a short “how would you approach it?” first would save the rounds of steering.",
    "Closure": "Right now a lot of finished work never reaches a commit — closing each piece when you've checked it keeps your history safe.",
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


# --------------------------------------------------------------------------- #
# Tabs: usage charts, your recent chats, how we evaluate
# --------------------------------------------------------------------------- #

RECENT_CHATS = 50


def _column_chart(values, labels, tip, label_every=1, height=150, label_all=False, scale_max=None):
    """One-series column chart as inline SVG: one hue, values on hover (with a hit area
    taller than the bar), the peak labelled (every bar with `label_all`), a recessive
    baseline. No legend — the heading names the series. `scale_max` fixes the top of the
    scale (a score out of 100) instead of stretching the tallest bar to the top."""
    n = len(values)
    if not n or not max(values):
        return '<p class="rate">No data.</p>'
    w, top, bottom = 640, 18, 22
    slot = w / n
    bar = max(2.0, min(28.0, slot - 3))
    peak = max(values)
    scale = scale_max or peak
    plot = height - top - bottom
    out = [f'<svg class="chart" viewBox="0 0 {w} {height}" role="img" preserveAspectRatio="none">']
    out.append(f'<line x1="0" y1="{top + plot}" x2="{w}" y2="{top + plot}" class="axis"/>')
    peak_i = values.index(peak)
    for i, (v, lab) in enumerate(zip(values, labels)):
        x = i * slot + (slot - bar) / 2
        h = plot * v / scale
        y = top + plot - h
        out.append(f'<g class="col"><title>{_esc(tip(lab, v))}</title>'
                   f'<rect class="hit" x="{i * slot:.1f}" y="{top}" width="{slot:.1f}" height="{plot}"/>'
                   + (f'<rect class="colbar" x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{h:.1f}" rx="2"/>' if v else "")
                   + '</g>')
        if i % label_every == 0:
            out.append(f'<text class="tick" x="{i * slot + slot / 2:.1f}" y="{height - 6}" text-anchor="middle">{_esc(lab)}</text>')
        if i == peak_i or (label_all and v):
            out.append(f'<text class="peak" x="{i * slot + slot / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle">{v}</text>')
    out.append('</svg>')
    return "".join(out)


def _hbars(rows, unit="", colors=None, total=None):
    """Horizontal bars for ranked categories (projects, tools): label, bar, value. `colors`
    gives each bar the hue of its series, so it matches the chart next to it; with `total`,
    the hover card also says what share of everything that bar is."""
    if not rows:
        return '<p class="rate">No data.</p>'
    top = max(v for _, v in rows) or 1
    out = ""
    for i, (name, v) in enumerate(rows):
        tip = f"{name} · {v:,}{unit}" + (f" · {100 * v / total:.0f}% of the total" if total else "")
        hue = colors[i] if colors else ""
        out += (f'<div class="bar-item has-tip"' + (f' data-c="{hue}"' if hue else "") + '>'
                f'<span class="tt">{_esc(tip)}</span><div class="bl">{_esc(name)}</div>'
                f'<div class="bt"><i style="width:{100 * v / top:.0f}%' + (f';background:{hue}' if hue else "")
                + f'"></i></div><div class="bv">{v:,}</div></div>')
    return out


# One fixed hue per dimension, the same in every chart (validated together on the dark panel:
# CVD ΔE ≥ 8.4, ≥ 3:1). Delegation, an archetype-only axis, takes green.
DIM_HUES = {"Direction": "#3987e5", "Verification": "#d95926", "Context": "#199e70", "Iteration": "#c98500",
            "Toolcraft": "#d55181", "Planning": "#9085e9", "Closure": "#e66767"}

# Categorical hues for the dark report, in fixed order (validated: CVD ΔE ≥ 8, ≥ 3:1 on the
# panel). A sixth group never gets a new hue — it folds into OTHER_HUE.
SERIES_HUES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
OTHER_HUE = "#6b6b78"
# Sequential steps (one hue, dim → bright on a dark surface) for the day × hour heat map.
HEAT_STEPS = ["#184f95", "#256abf", "#3987e5", "#5598e7", "#86b6ef"]
_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_GENERIC_DIRS = {"dev", "projects", "project", "code", "src", "repos", "repositories", "documents", "desktop",
                 "workspace", "work", "git", "github", "desarrollo", "proyectos", "documentos", "escritorio",
                 "repositorios"}

# What kind of work each tool call was, for the "what Claude did" mix.
TOOL_GROUPS = [
    ("Terminal", {"bash", "powershell", "monitor", "bashoutput", "killshell", "taskstop", "run_in_terminal"}),
    ("Edit files", {"edit", "write", "multiedit", "notebookedit"}),
    ("Read & search", {"read", "grep", "glob", "ls", "webfetch", "websearch"}),
    ("Browser & screen", {"computer", "navigate", "javascript_tool", "find", "read_page", "get_page_text",
                          "form_input", "browser_batch", "screenshot", "read_console_messages",
                          "read_network_requests", "resize_window", "zoom", "left_click", "type", "key"}),
    ("Plan & delegate", {"update_plan", "todowrite", "agent", "task", "workflow", "enterplanmode", "exitplanmode",
                         "sendmessage", "skill", "wrap_up"}),
]


def _tool_group(name):
    n = name.lower()
    for group, names in TOOL_GROUPS:
        if n in names:
            return group
    if n.startswith(("tabs_", "preview_", "browser_")):
        return "Browser & screen"
    return "Other"


def _project_short(name):
    """A chart-sized project name: drop container folders like Dev / Projects and keep the end."""
    words = _project_label(name).split()
    while len(words) > 1 and words[0].lower() in _GENERIC_DIRS:
        words.pop(0)
    short = " ".join(words)
    if len(short) <= 26:
        return short
    tail = [words.pop()]                                    # keep the end: as many words as fit
    while words and len(" ".join([words[-1]] + tail)) <= 24:
        tail.insert(0, words.pop())
    return "… " + " ".join(tail)


def _legend(items):
    return ('<div class="legend">' + "".join(
        f'<span><i style="background:{c}"></i>{_esc(name)}</span>' for name, c in items) + '</div>')


def _rhythm_tiles(local):
    days = sorted({d.date() for d in local})
    span = (days[-1] - days[0]).days + 1
    streak = best = 1
    for a, b in zip(days, days[1:]):
        streak = streak + 1 if (b - a).days == 1 else 1
        best = max(best, streak)
    weekday = Counter(d.weekday() for d in local).most_common(1)[0][0]
    hour = Counter(d.hour for d in local).most_common(1)[0][0]
    per_day = statistics.median(Counter(d.date() for d in local).values())
    night = round(100 * sum(1 for d in local if d.hour < 6) / len(local))
    tiles = [(f"{len(days)} / {span}", "days you worked, out of the whole span"),
             (f"{best}", "longest streak (days in a row)"),
             (WEEKDAYS[weekday], "busiest day of the week"),
             (f"{hour}h", "peak hour"),
             (f"{per_day:.0f}", "prompts on a typical active day"),
             (f"{night}%", "of your prompts after midnight (0h–6h)")]
    return '<div class="ingest rhythm">' + "".join(
        f'<div class="ing"><div class="n">{_esc(n)}</div><div class="l">{_esc(l)}</div></div>' for n, l in tiles) + "</div>"


def _weekly_by_project(local_prompts, colors):
    """Prompts per week, stacked by project (top five in color, the rest as Other)."""
    first = min(d.date() for d, _ in local_prompts)
    weeks = defaultdict(Counter)
    real = defaultdict(Counter)                           # the hover card names every project, Other included
    for d, proj in local_prompts:
        wk = (d.date() - first).days // 7
        weeks[wk][proj if proj in colors else "Other"] += 1
        real[wk][proj] += 1
    n = max(weeks) + 1
    order = list(colors) + ["Other"]
    totals = [sum(weeks[i].values()) for i in range(n)]
    peak = max(totals) or 1
    w, height, top, bottom = 640, 170, 18, 22
    plot = height - top - bottom
    slot = w / n
    bar = max(2.0, min(28.0, slot - 3))
    every = max(1, n // 8)
    out = [f'<svg class="chart stack" viewBox="0 0 {w} {height}" role="img" preserveAspectRatio="none">',
           f'<line x1="0" y1="{top + plot}" x2="{w}" y2="{top + plot}" class="axis"/>']
    for i in range(n):
        x = i * slot + (slot - bar) / 2
        y = top + plot
        label = f"{first + timedelta(days=7 * i):%d/%m}"
        # One hover card per week: the total, then every project in it, biggest first.
        tip = [f"week of {label}: {totals[i]} prompts"]
        tip += [f"{proj}: {v} ({100 * v / totals[i]:.0f}%)" for proj, v in real[i].most_common(6)]
        if len(real[i]) > 6:
            tip.append(f"+{len(real[i]) - 6} more projects")
        out.append(f'<g class="wk has-tip"><title>{_esc(" · ".join(tip))}</title>'
                   f'<rect class="hit" x="{i * slot:.1f}" y="{top}" width="{slot:.1f}" height="{plot}"/>')
        for proj in order:
            v = weeks[i].get(proj, 0)
            if not v:
                continue
            h = plot * v / peak
            seg_h = max(h - 2, 1.0) if h > 3 else h          # 2px surface gap between stacked fills
            y -= h
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{seg_h:.1f}" '
                       f'style="fill:{colors.get(proj, OTHER_HUE)}"/>')
        out.append("</g>")
        if i % every == 0:
            out.append(f'<text class="tick" x="{i * slot + slot / 2:.1f}" y="{height - 6}" text-anchor="middle">{label}</text>')
        if totals[i] == peak:
            out.append(f'<text class="peak" x="{i * slot + slot / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle">{peak}</text>')
    out.append("</svg>")
    rest = len({p for _, p in local_prompts} - set(colors))
    return (_legend([(p, colors[p]) for p in colors] + [("Other", OTHER_HUE)])
            + "".join(out) + (f'<p class="rate">Other = your other {rest} projects together.</p>' if rest else ""))


def _heatmap(local):
    """Day of the week × hour of the day: where your prompts cluster."""
    grid = Counter((d.weekday(), d.hour) for d in local)
    # Quintiles of the busy cells, not shares of the peak: one huge hour would wash out the rest.
    vals = sorted(grid.values())
    cuts = [vals[len(vals) * k // 5] for k in range(1, 5)]
    total = sum(vals)
    left, cw, ch, top = 40, 25, 20, 4
    w, height = left + 24 * cw, top + 7 * ch + 20
    out = [f'<svg class="heat" viewBox="0 0 {w} {height}" role="img">']
    for r, day in enumerate(WEEKDAYS):
        out.append(f'<text x="{left - 8}" y="{top + r * ch + 14}" text-anchor="end">{day[:3]}</text>')
        for hr in range(24):
            v = grid.get((r, hr), 0)
            fill = HEAT_STEPS[sum(v >= c for c in cuts)] if v else "var(--p2)"
            busier = round(100 * sum(1 for x in vals if x < v) / len(vals)) if v else 0
            tip = f"{day} {hr}h: {v} prompts" + (f" · {100 * v / total:.1f}% of all your prompts"
                                                 f" · busier than {busier}% of your hours" if v else "")
            out.append(f'<g class="has-tip" data-c="{fill}"><title>{tip}</title><rect x="{left + hr * cw + 1}" '
                       f'y="{top + r * ch + 1}" width="{cw - 2}" height="{ch - 2}" rx="3" style="fill:{fill}"/></g>')
    for hr in range(0, 24, 3):
        out.append(f'<text x="{left + hr * cw + cw / 2}" y="{height - 4}" text-anchor="middle">{hr}h</text>')
    out.append("</svg>")
    scale = "".join(f'<i style="background:{c}"></i>' for c in HEAT_STEPS)
    return (f'<div class="tbl">{"".join(out)}</div>'
            f'<div class="heat-key"><span>fewer</span>{scale}<span>more</span></div>')


def _tool_mix(corpus):
    groups = Counter()
    tools = Counter()
    for name, cnt in corpus.tool_usage.items():
        groups[_tool_group(name)] += cnt
        tools[name.lower()] += cnt
    total = sum(groups.values())
    if not total:
        return '<p class="rate">No data.</p>'
    hue = {g: SERIES_HUES[i] for i, (g, _) in enumerate(TOOL_GROUPS)}
    hue["Other"] = OTHER_HUE
    order = [g for g, _ in TOOL_GROUPS if groups[g]] + (["Other"] if groups["Other"] else [])
    bar = "".join(f'<i class="has-tip" data-c="{hue[g]}" style="width:{100 * groups[g] / total:.2f}%;background:{hue[g]}">'
                  f'<span class="tt">{_esc(g)} · {groups[g]:,} calls · {100 * groups[g] / total:.0f}% of the total</span></i>'
                  for g in order)
    key = ('<div class="legend">' + "".join(
        f'<span><i style="background:{hue[g]}"></i>{_esc(g)} <b>{100 * groups[g] / total:.0f}%</b></span>'
        for g in order) + "</div>")
    top = tools.most_common(10)
    return (f'<div class="mix">{bar}</div>{key}'
            '<p class="def" style="margin-top:18px"><b>Tools Claude used the most</b> (calls)</p>'
            + _hbars(top, " calls", colors=[hue[_tool_group(n)] for n, _ in top], total=total))


def _usage_charts_html(corpus):
    local_prompts = [(p["ts"].astimezone(), _project_short(p["project"])) for p in corpus.real_prompts if p.get("ts")]
    local = [d for d, _ in local_prompts]
    projects = Counter(_project_short(p["project"]) for p in corpus.real_prompts)
    colors = {name: SERIES_HUES[i] for i, (name, _) in enumerate(projects.most_common(len(SERIES_HUES)))}
    parts = []
    if local:
        parts.append('<p class="def"><b>Your rhythm</b></p>' + _rhythm_tiles(local))
        parts.append('<p class="def" style="margin-top:22px"><b>Prompts per week, by project</b></p>'
                     + _weekly_by_project(local_prompts, colors))
        parts.append('<p class="def" style="margin-top:22px"><b>When you work</b> (local time, by day and hour)</p>'
                     + _heatmap(local))
    top = projects.most_common(8)
    parts.append('<p class="def" style="margin-top:22px"><b>Projects where you write the most</b> (prompts)</p>'
                 + _hbars(top, " prompts", colors=[colors.get(n, OTHER_HUE) for n, _ in top],
                          total=len(corpus.real_prompts)))
    parts.append('<p class="def" style="margin-top:22px"><b>What Claude did for you</b> (tool calls by type)</p>'
                 + _tool_mix(corpus))
    return "".join(parts)


def _score_months_svg(scored):
    """The overall score per month on a fixed 0–100 scale; each bar says whether the month
    went up or down against the one before, and the prompts behind it sit under the month."""
    n = len(scored)
    w, height, top, bottom = 640, 190, 22, 40
    plot = height - top - bottom
    slot = w / n
    bar = min(64.0, slot * 0.5)
    out = [f'<svg class="chart months" viewBox="0 0 {w} {height}" role="img" preserveAspectRatio="none">']
    for pct in (50, 100):                                  # quiet guides at 50 and 100
        y = top + plot - plot * pct / 100
        out.append(f'<line x1="0" y1="{y:.1f}" x2="{w}" y2="{y:.1f}" class="guide"/>')
    out.append(f'<line x1="0" y1="{top + plot}" x2="{w}" y2="{top + plot}" class="axis"/>')
    prev = prev_se = None
    for i, r in enumerate(scored):
        v = r["overall"]
        se = (r.get("se") or {}).get("overall")
        real = prev is not None and is_real_change(v - prev, prev_se, se)
        kind = "first" if prev is None else (("up" if v > prev else "down") if real else "same")
        h = plot * v / 100
        x, y = i * slot + (slot - bar) / 2, top + plot - h
        cx = i * slot + slot / 2
        ranked = sorted(((d, v) for d, v in r["dims"].items() if d not in UNSCORED), key=lambda kv: kv[1])
        tip = [f"{r['month']}: {v}/100", f"{r['prompts']} prompts"]
        if se is not None:
            tip[0] += f" ±{margin_points(se)}"
        if prev is not None:
            tip.append(f"{v - prev:+d} vs the month before" + ("" if real else " (within noise)"))
        tip += [f"strongest: {DISPLAY_NAMES[ranked[-1][0]]} {ranked[-1][1]}",
                f"weakest: {DISPLAY_NAMES[ranked[0][0]]} {ranked[0][1]}"]
        if r.get("current"):
            tip.append("month still in progress")
        now = " now" if r.get("current") else ""
        out.append(f'<g class="col has-tip"><title>{_esc(" · ".join(tip))}</title><rect class="hit" x="{i * slot:.1f}" y="{top}" '
                   f'width="{slot:.1f}" height="{plot}"/><rect class="mb {kind}{now}" '
                   f'x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{h:.1f}" rx="5"/></g>')
        out.append(f'<text class="peak" x="{cx:.1f}" y="{y - 7:.1f}" text-anchor="middle">{v}</text>')
        out.append(f'<text class="tick" x="{cx:.1f}" y="{height - 22}" text-anchor="middle">{r["month"]}</text>')
        out.append(f'<text class="tick sub" x="{cx:.1f}" y="{height - 7}" text-anchor="middle">{r["prompts"]} prompts</text>')
        prev, prev_se = v, se
    out.append("</svg>")
    return "".join(out)


# Hover cards: any `.has-tip` element shows its text (an SVG <title> or a hidden `.tt` span,
# both translated with the rest of the page) as a styled card that follows the pointer.
# Lines are split on " · "; the first is the heading, with the element's color as a swatch.
_TIP_JS = r"""
(function () {
  var box;
  function hide() { if (box) box.style.display = 'none'; }
  function text(el) {
    var t = el.getAttribute('data-tip');
    if (t !== null) return t;
    var tt = el.querySelector(':scope > .tt');
    return tt ? tt.textContent : '';
  }
  function render(el, x, y) {
    var t = text(el);
    if (!t) return hide();
    if (!box) { box = document.createElement('div'); box.className = 'tipbox'; document.body.appendChild(box); }
    box.textContent = '';
    var c = el.getAttribute('data-c');
    t.split(' · ').forEach(function (line, i) {
      var d = document.createElement('div');
      if (i === 0) {
        d.className = 'tip-h';
        if (c) { var sw = document.createElement('i'); sw.style.background = c; d.appendChild(sw); }
      }
      d.appendChild(document.createTextNode(line));
      box.appendChild(d);
    });
    box.style.display = 'block';
    var r = box.getBoundingClientRect(), left = x + 16, top = y + 16;
    if (left + r.width > innerWidth - 8) left = x - r.width - 16;
    if (left < 8) left = 8;
    if (top + r.height > innerHeight - 8) top = y - r.height - 16;
    if (top < 8) top = 8;
    box.style.left = left + 'px'; box.style.top = top + 'px';
  }
  document.addEventListener('DOMContentLoaded', function () {
    // Native SVG tooltips are slow and plain: move each <title> into data-tip instead.
    document.querySelectorAll('.has-tip > title').forEach(function (t) {
      t.parentNode.setAttribute('data-tip', t.textContent);
      t.parentNode.setAttribute('aria-label', t.textContent);
      t.remove();
    });
  });
  document.addEventListener('pointermove', function (e) {
    var el = e.target.closest && e.target.closest('.has-tip');
    if (el) render(el, e.clientX, e.clientY); else hide();
  });
  document.addEventListener('pointerleave', hide);
  // Long written paragraphs get the same marks as the deep-read summary: numbers, quoted words
  // and arrow sequences. Done here, after the EN/ES copies exist, so translation is untouched.
  var HL = /(\u201c[^\u201d]{1,80}\u201d|"[^"]{1,80}")|([A-Za-z\u00c0-\u00ff]+(?: ?\u2192 ?[A-Za-z\u00c0-\u00ff]+)+)|(\b\d[\d.,]*(?:\/100|%| de (?:las |los |tus )?\d[\d.,]*| of (?:the |your )?\d[\d.,]*)?)/g;
  var CLS = ['hl-q', 'hl-k', 'hl-n'];
  function mark(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), nodes = [], n;
    while ((n = walker.nextNode())) {
      if (!n.parentNode.closest('code, mark, .hl-n, .resume, svg')) nodes.push(n);
    }
    nodes.forEach(function (node) {
      var text = node.nodeValue, last = 0, m, frag = null;
      HL.lastIndex = 0;
      while ((m = HL.exec(text))) {
        if (!frag) frag = document.createDocumentFragment();
        frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var i = m[1] ? 0 : (m[2] ? 1 : 2);
        var el = document.createElement(i === 2 ? 'b' : 'mark');
        el.className = CLS[i]; el.textContent = m[0];
        frag.appendChild(el);
        last = m.index + m[0].length;
      }
      if (frag) { frag.appendChild(document.createTextNode(text.slice(last))); node.parentNode.replaceChild(frag, node); }
    });
  }
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.assess:not(.deep-sum), .band-meaning, .card .why, .card .how, .card p, ' +
                              '.dim .rate, .case-goal, .evo-why, .sk-now, .sk-next, .card .tgt').forEach(mark);
  });
  document.addEventListener('click', function (e) {
    var chip = e.target.closest && e.target.closest('[data-filter]');
    if (!chip) return;
    var box = chip.closest('section'), f = chip.getAttribute('data-filter');
    box.querySelectorAll('[data-filter]').forEach(function (c) { c.classList.toggle('on', c === chip); });
    box.querySelectorAll('.case').forEach(function (c) {
      c.hidden = f !== 'all' && c.getAttribute('data-habit') !== f;
    });
  });
  document.addEventListener('scroll', hide, true);
})();
"""


def _size_html(n_bytes):
    """Transcript size for a narrow tile: the number big, the unit small (GB past 1000 MB)."""
    mb = n_bytes / 1e6
    return f"{mb / 1000:.2f}<s>GB</s>" if mb >= 1000 else f"{mb:.1f}<s>MB</s>"


# Letter grades for a 0–100 score (an ordinal green → red ramp; the letter always rides along,
# so the grade never depends on color alone).
GRADES = [("S", 90, "#3ccf6e"), ("A", 80, "#3ad6c9"), ("B", 65, "#7fb2ff"), ("C", 50, "#a99bff"),
          ("D", 35, "#f0a35e"), ("F", 0, "#e5534b")]


def _grade(score):
    for letter, lo, hue in GRADES:
        if score >= lo:
            return letter, hue
    return GRADES[-1][0], GRADES[-1][2]


def _grade_bar_html(result):
    """A thin line split by grade and the count per letter: how your seven dimensions spread."""
    got = Counter(_grade(round(result["shrunk"][n]))[0] for n in WEIGHTS)
    total = len(WEIGHTS)
    good = sum(got[g] for g in ("S", "A", "B"))
    segs = "".join(f'<i style="flex:{got[g]};background:{h}"></i>' for g, _, h in GRADES if got[g])
    letters = "".join(f'<span style="color:{h}">{g} {got[g]}</span>' for g, _, h in GRADES if got[g])
    return (f'<div class="grades"><p class="gr-head">{good} of {total} dimensions at B or better</p>'
            f'<div class="gr-line">{segs}</div><div class="gr-letters">{letters}</div></div>')


def _recent_html(recent):
    if not recent:
        return ""
    pm = f" ±{recent['margin']}" if recent.get("margin") else ""
    return (f'<div class="recent"><span>Last {RECENT_DAYS} days</span><b>{recent["overall"]}{pm}</b></div>')


def _history_html(history):
    """Your evolution: the score of every month with enough prompts, then one small card per
    dimension (its own color) with its monthly bars and how far it moved — no wide table."""
    scored = [r for r in history or [] if r["overall"] is not None]
    if not scored:
        return ""
    parts = []
    # A month that has only just begun would swing the headline; compare closed months.
    closed = [r for r in scored if not r.get("current")] or scored
    first, last = closed[0], closed[-1]
    if len(closed) >= 2:
        diff = last["overall"] - first["overall"]
        moves = sorted(((last["dims"][n] - first["dims"][n], n) for n in WEIGHTS if n not in UNSCORED),
                       key=lambda t: abs(t[0]), reverse=True)
        big, name = moves[0]
        noise = "" if is_real_change(diff, (first.get("se") or {}).get("overall"), (last.get("se") or {}).get("overall")) \
            else ' <span class="noise-note">That change is within the noise.</span>'
        parts.append(f'<p class="assess"><span>Since {first["month"]} your score went from {first["overall"]} to '
                     f'{last["overall"]} ({diff:+d}). What moved the most: {DISPLAY_NAMES[name]} ({big:+d}).</span>{noise}</p>')
    parts.append(_legend([("went up", "var(--q-hi)"), ("went down", "var(--q-lo)"),
                          ("no real change, or first month", "var(--ac-soft)")])
                 + _score_months_svg(scored))
    closed_idx = [i for i, r in enumerate(scored) if not r.get("current")] or list(range(len(scored)))
    a_row, b_row = scored[closed_idx[0]], scored[closed_idx[-1]]
    since = _MONTH_ABBR[int(a_row["month"][5:7]) - 1]

    def why_line(dim):
        """The measure that moved the most between the first and the last closed month."""
        if len(closed) < 2:
            return ""
        a = dict((a_row.get("metrics") or {}).get(dim) or [])
        b = dict((b_row.get("metrics") or {}).get(dim) or [])
        moves = sorted(((abs(b[k] - a[k]), k) for k in a if k in b), reverse=True)
        if not moves or moves[0][0] == 0:
            return f"Since {since}: no measure changed"
        k = moves[0][1]
        return f"Since {since}: {k} {a[k]}% → {b[k]}%"

    def se_at(row, dim):
        se = row.get("se") or {}
        return se.get("overall") if dim is None else (se.get("dims") or {}).get(dim)

    def card(label, hue, values, extra="", why="", dim=None):
        """One tile: name, latest closed value, change since the first month (≈ when it is within
        the noise of the two months), a bar per month."""
        latest = values[closed_idx[-1]]
        d = latest - values[closed_idx[0]]
        real = is_real_change(d, se_at(a_row, dim), se_at(b_row, dim))
        if not d or len(closed) < 2:
            delta = '<span class="flat">=</span>'
        elif real:
            delta = f'<span class="{"up" if d > 0 else "down"}">{d:+d}</span>'
        else:
            delta = f'<span class="flat has-tip"><span class="tt">within noise</span>≈{d:+d}</span>'
            why = (why + " · within noise") if why else why
        bars, labels, prev_v = "", "", None
        for r, v in zip(scored, values):
            tip = f"{label} · {r['month']}: {v}/100"
            if prev_v is not None:
                tip += f" · {v - prev_v:+d} vs the month before"
            if r.get("current"):
                tip += " · month still in progress"
            bars += (f'<i class="has-tip{" now" if r.get("current") else ""}" data-c="{hue}" '
                     f'style="height:{max(4, v)}%;background:{hue}"><span class="tt">{_esc(tip)}</span></i>')
            labels += f'<span>{_MONTH_ABBR[int(r["month"][5:7]) - 1]}</span>'
            prev_v = v
        return (f'<div class="evo{extra}" style="--hue:{hue}"><div class="evo-top"><span class="evo-name">'
                f'<b style="background:{hue}"></b>{_esc(label)}</span>{delta}</div>'
                f'<div class="evo-n">{latest}<s>/100</s></div><div class="spark">{bars}</div>'
                f'<div class="spark-l">{labels}</div>'
                + (f'<p class="evo-why">{_esc(why)}</p>' if why else "") + '</div>')

    total_why = ""
    if len(closed) >= 2:
        big = max((n for n in WEIGHTS if n not in UNSCORED), key=lambda n: abs(b_row["dims"][n] - a_row["dims"][n]))
        total_why = f"Since {since}: {DISPLAY_NAMES[big]} moved the most ({b_row['dims'][big] - a_row['dims'][big]:+d})"
    cards = card("Overall score", "var(--ac-soft)", [r["overall"] for r in scored], " total", total_why)
    for n in sorted(WEIGHTS, key=lambda d: -WEIGHTS[d]):
        cards += card(DISPLAY_NAMES[n], DIM_HUES[n], [r["dims"][n] for r in scored], why=why_line(n), dim=n)
    parts.append(f'<p class="def" style="margin-top:20px"><b>Each dimension, month by month</b> '
                 f'(big number: {_esc(last["month"])}; change since {_esc(first["month"])})</p><div class="evo-grid">{cards}</div>')
    thin = [r for r in history if r["overall"] is None]
    parts.append(f'<p class="rate">Each session counts in the month it started, and each month is scored with the same '
                 f'rules as the whole report. A month needs at least {HISTORY_MIN_PROMPTS} prompts to get a score; the '
                 f'current month is still in progress. “≈” marks a change smaller than the noise of the two months.</p>'
                 + (f'<p class="rate">Not scored: {", ".join(r["month"] for r in thin)}.</p>' if thin else ""))
    return "<section><h3>Your evolution, month by month</h3>" + "".join(parts) + "</section>"


def _recent_chats_html(corpus, deep, limit=RECENT_CHATS):
    """Your latest chats, newest first, so every claim in the report can be traced back to a
    real conversation: name, project, size, and a jump to its deep read when there is one."""
    read_by_session = {}
    for r in (deep or {}).get("reads") or []:
        read_by_session.setdefault(r.get("session"), r["episode_id"])
    rows = []
    for sid, s in corpus.sessions.items():
        ts = [e["ts"] for e in s.get("story") or [] if e.get("ts")]
        prompts = sum(1 for e in s.get("story") or [] if e["kind"] == "prompt")
        if not ts or not prompts:
            continue
        rows.append((max(ts), min(ts), sid, s["project"], prompts,
                     sum(1 for e in s["timeline"] if e["kind"] == "tool")))
    rows.sort(reverse=True)
    shown = rows[:limit]
    n_read = sum(1 for r in shown if r[2] in read_by_session)
    if not deep:
        note = ""
    elif (deep.get("depth") or "") == "recientes":
        note = (f'<p class="def">{n_read} of these {len(shown)} chats were read in depth — the deep read covered '
                f'your latest chats, one conversation each.</p>')
    else:
        note = (f'<p class="def">{n_read} of these {len(shown)} chats were read in depth. The last deep read did not '
                f'pick your latest chats: it picked the roughest conversations, some that went well and a few recent ones.</p>'
                f'<p class="rate">To read exactly these chats, ask Claude Code to run <code>/ai-fluency</code> in depth '
                f'with <code>--depth recientes</code>.</p>')
    body = ""
    for last, first, sid, project, prompts, tools in shown:
        title = corpus.titles.get(sid) or "(untitled)"
        rid = read_by_session.get(sid)
        read = (f'<a href="#read-{_esc(rid)}" class="tag s nowrap" data-goto="chats">read in depth</a>' if rid
                else ('<span class="tag ld">not read</span>' if deep else ""))
        body += (f'<tr><td class="nowrap">{last.astimezone():%Y-%m-%d}</td>'
                 f'<td><b>{_esc(title)}</b> {read}<br><span class="loc">{_esc(_project_short(project))}</span>'
                 f'<br><code class="resume">claude --resume {_esc(sid)}</code></td>'
                 f'<td class="num">{prompts}</td><td class="num">{tools}</td></tr>')
    return (note + f'<div class="tbl"><table><thead><tr><th>Last activity</th><th>Chat</th>'
            f'<th class="num">Your prompts</th><th class="num">Claude actions</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


_DIM_HOW = {
    "Direction": "Of the prompts that ask for work (go-aheads like “sí, hazlo”, questions and notes like “sale eso” are left out): how many carry a "
                 "constraint, name a file or error, or say why — in English or Spanish — and how few rules you "
                 "only reveal later in a correction.",
    "Verification": "Each burst of code changes gets one verdict: checked by Claude (a test, build, lint, script run "
                    "or looking at the app in a browser), by you (you say what you saw, name what broke, or ask for a "
                    "check), both, or nobody. Half is Claude's checks, half is yours; an unchecked change you approve "
                    "with “ok” counts against. Edits to notes (.md, .txt) are left out.",
    "Context": "Of every edit Claude made: was the file read (or created) earlier in that same session. "
               "Shown, not scored: Claude does this on its own.",
    "Iteration": "How often you have to correct (fewer is better) and how many of your corrections say what "
                 "broke instead of just “no”.",
    "Toolcraft": "How many different tools get used, how evenly, and how often work is handed to subagents, "
                 "background jobs or plan mode.",
    "Planning": "In sessions with real work (3+ edits): before the first change, did you ask for a plan, options or "
                "a structure, or use plan mode (50%); was there a conversation first (25%); and how often you ask why (25%).",
    "Closure": "In projects that use git: how many working sessions end in a commit (60%), and how few sessions end "
               "on an unresolved complaint (40%). Who asked for the commit is shown, not scored.",
}
AXIS_HUES = {**{ax: DIM_HUES[ax] for ax in ARCHETYPE_AXES if ax in DIM_HUES}, "Delegation": "#2f9e44"}
_AXIS_LABEL = {"Direction": "Briefing", "Verification": "Verification", "Context": "Context-setting",
               "Iteration": "Iteration", "Toolcraft": "Toolcraft", "Delegation": "Delegation"}


# What each detector looks for, in the words the method table shows.
_AUDIT_LABEL = {
    "constraint": "A rule or limit (only X, don't touch Y)",
    "artifact": "A file, path, function or error named",
    "intent": "Why you want it",
    "go_ahead": "Only an approval or a “continue”",
    "correction": "Correcting what Claude just did",
    "checked": "Reporting what you saw in the result",
    "plan": "Asking for a plan first",
}


def _audit_html(audit):
    """How often each keyword detector is right, from the hand-labeled sample (empty without one)."""
    if not audit:
        return ""

    def cell(part, whole, good):
        if not whole:
            return '<td class="num">—</td>'
        hue = _grade(round(100 * good))[1]
        return (f'<td class="num nowrap"><b style="color:{hue}">{round(100 * part / whole)}%</b>'
                f'<span class="frac">{part}/{whole}</span></td>')

    rows = ""
    for name in AUDIT_DETECTORS:
        r = audit.get(name)
        if not r:
            continue
        right = cell(r["right_hits"], r["hits"], r["precision"] or 0)
        missed = cell(r["missed"], r["misses"], 1 - (r["miss_rate"] or 0))
        rows += f'<tr><td>{_esc(_AUDIT_LABEL[name])}</td>{right}{missed}</tr>'
    if not rows:
        return ""
    return f"""
  <section>
    <h3>How often each detector is right</h3>
    <p class="assess">The numbers come from keyword detectors. Each one was checked by hand against a separate sample of
    your own prompts, one that was not used to tune it.</p>
    <div class="tbl"><table><thead><tr><th>What it looks for</th><th class="num">Right when it counts one</th>
    <th class="num">Ones it missed</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
    <p class="rate" style="margin-top:8px">Right when it counts one: of the prompts the detector counted, how many really
    had it. Ones it missed: of the prompts it skipped, how many it should have counted. There are few labels per
    detector, so read these as rough numbers.</p>
  </section>"""


def _method_html(result, audit=None):
    a = result["archetype"]
    sims = {nm: sim for sim, nm in a["all"]}
    you_row = ' class="you"'   # outside the f-string: a backslash inside {…} needs Python 3.12
    dims = "".join(
        f'<tr><td><b>{_esc(disp(d))}</b></td><td>{_esc(_DIM_HOW[d])}</td>'
        f'<td class="num">{round(WEIGHTS[d] * 100)}%</td><td class="num">{round(result["shrunk"][d])}</td></tr>'
        for d in sorted(WEIGHTS, key=lambda d: -WEIGHTS[d]))
    bands = "".join(
        f'<tr{you_row if name == result["band"] else ""}><td><b>{_esc(name)}</b></td>'
        f'<td class="nowrap">{lo}–{hi}</td><td>{_esc(meaning)}</td></tr>'
        for name, lo, hi, meaning in BANDS)
    me = {**result["shrunk"], "Delegation": float(a["delegation_score"])}
    arch = ""
    for nm, p in sorted(PROTOTYPES.items(), key=lambda kv: -sims.get(kv[0], -9)):
        axes = "".join(
            f'<div class="bar-item" title="{_esc(_AXIS_LABEL[ax])}: {v} · you {round(me[ax])}">'
            f'<div class="bl">{_esc(_AXIS_LABEL[ax])}</div>'
            f'<div class="bt me-track"><i style="width:{v}%;background:{AXIS_HUES[ax]}"></i><b class="me" style="left:{min(99, round(me[ax]))}%"></b></div>'
            f'<div class="bv">{v}</div></div>'
            for ax, v in zip(ARCHETYPE_AXES, p["vec"]))
        mine = nm == a["primary"]
        arch += (f'<div class="card{" keep" if mine else ""}"><div class="ph">Similarity to you {sims.get(nm, 0):+.2f}'
                 + (' <span class="tag s">your archetype</span>' if mine else "")
                 + f'</div><h4>{p["emoji"]} {_esc(nm)}</h4><p class="how">{_esc(p["blurb"])}</p>'
                 f'<p class="exgen">Typical profile (0–100) — the white mark is you:</p>{axes}</div>')
    return f"""
  <section>
    <h3>Two layers</h3>
    <p class="assess"><b>The numbers</b> are computed on your computer from your Claude Code transcripts, with the same
    rules for everyone: no AI is involved, and the same data always gives the same score.</p>
    <p class="assess"><b>The deep read</b> (only when you ask for it) has Claude read whole conversations. Every quote it
    attributes to you is checked word for word against your transcripts; anything that doesn't match is thrown out.</p>
  </section>
  <section>
    <h3>The seven dimensions</h3>
    <div class="tbl"><table><thead><tr><th>Dimension</th><th>What is measured</th><th class="num">Weight</th><th class="num">You</th></tr></thead>
    <tbody>{dims}</tbody></table></div>
    <p class="rate" style="margin-top:8px">Your overall score is the weighted average. With little data, a dimension
    is pulled toward 50 until there is enough evidence (“low data”).</p>
    <p class="rate">The ± is the margin of error: the score is recomputed 12 times, each time leaving out a different
    twelfth of your sessions, and the spread gives a 95% range. In your evolution, a change smaller than the margins of
    both months is marked “≈” — it could be noise.</p>
  </section>{_audit_html(audit)}
  <section>
    <h3>Levels</h3>
    <div class="tbl"><table><thead><tr><th>Level</th><th>Score</th><th>What it means</th></tr></thead>
    <tbody>{bands}</tbody></table></div>
  </section>
  <section>
    <h3>Archetypes</h3>
    <p class="assess">The archetype describes your <b>style</b>, not your level. Each archetype has a typical profile
    across six signals. Your profile is compared with each one (cosine similarity, from −1 to +1) and the closest wins;
    when two are very close it's shown as a blend. Habits that Claude does on its own — reading before editing, running
    the tests — count much less here, so the archetype reflects how <b>you</b> drive.</p>
    {arch}
  </section>
  <section>
    <h3>How the deep read works</h3>
    <ul class="facts">
      <li>Each session is cut into conversations: your request, what Claude did, how you replied, how it ended.</li>
      <li>It picks which to read: half the roughest, a quarter the most recent, a quarter that went smoothly.</li>
      <li>Claude Sonnet reads each one whole; Claude Opus looks for patterns across all of them.</li>
      <li>It also checks, chat by chat, whether there was a vision, a structure agreed before code and precise requirements; whether you caught an error Claude missed; and whether it ended in a commit.</li>
      <li>Every quote is checked against your transcripts before it reaches the report.</li>
    </ul>
  </section>"""


def build_html(corpus, result, cards, strength, archive_info=None, analysis=None, analysis_note=None,
               usage=None, adoption=None, generated_at=None, deep=None, history=None, margin=None, recent=None,
               audit=None):
    # Testability seam: tests pass a fixed datetime; the real run defaults to now.
    # (Phase 2 renders it; Phase 1 only threads it.)
    generated_at = generated_at or datetime.now()
    a = result["archetype"]
    d = result["dist"]
    analysis_section = _analysis_section_html(analysis)
    deep_section = _deep_section_html(deep)
    deep_missing = ('<section><h3>Your chats in depth</h3><p class="assess">No deep read yet. Ask Claude Code to run '
                    '<code>/ai-fluency</code> in depth: it reads your conversations end to end and fills this tab with your '
                    'habits, the rules worth saving to CLAUDE.md and a “you said → better” for each chat.</p></section>')
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
    n_cases = min(CASES_LIMIT, sum(1 for r in (deep or {}).get("case_reads") or (deep or {}).get("reads") or []
                                   if r.get("you_said") and r.get("better")))
    improve_badge = f'<span class="badge">{n_cases}</span>' if n_cases else '<span class="badge">new</span>'
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
        "Planning": "Talking real work through before the first change: vision, structure, then requirements.",
        "Closure": "Ending work checked and committed when you decide — not abandoned mid-problem.",
    }

    def dim_rate_line(name):
        det = result["detail"][name]
        if name == "Verification" and det.get("rate") is not None:
            w = det.get("who") or {}
            line = (f"{det['episodes']} changes: Claude checked {w.get('claude', 0)}, you checked {w.get('you', 0)}, "
                    f"both {w.get('both', 0)}, nobody {w.get('none', 0)}")
            if det.get("blind_ok"):
                line += f" · {det['blind_ok']} unchecked changes you approved with “ok”"
            return line
        if name == "Planning" and det.get("n"):
            return (f"{det['planned']} of {det['n']} work sessions talked it through before the first change · "
                    f"{det['conversation']} had a conversation first · {det['why_rate']*100:.0f}% of your prompts ask why")
        if name == "Closure" and det.get("n"):
            line = (f"{det['committed']} of {det['repo_sessions']} work sessions in git projects ended in a commit "
                    f"({det['you_asked']} you asked for, {det['claude_alone']} Claude did alone)")
            if det.get("ended_bad"):
                line += f" · {det['ended_bad']} sessions ended on an unresolved complaint"
            return line
        if name == "Context" and det.get("rate") is not None:
            return f"{det['grounded']} of {det['total_edits']} edits were grounded in a prior read ({det['rate']*100:.0f}%)"
        if name == "Direction":
            line = (f"{det['constraint_rate']*100:.0f}% carry a constraint · "
                    f"{det['artifact_rate']*100:.0f}% name a file/error · {det['intent_rate']*100:.0f}% state a why")
            if det.get("followups") or det.get("questions"):
                line += (f" · {det['n']} task prompts ({det.get('followups', 0)} go-aheads like “sí, hazlo” "
                         f"and {det.get('questions', 0)} questions or notes left out)")
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
        # Each dimension keeps its own hue across the report; quality reads from the number,
        # the bar length and the tags, never from color.
        hue = DIM_HUES[name]
        dim_html += f"""
      <div class="dim" style="--hue:{hue}">
        <div class="top"><span class="name"><b class="dot" style="background:{hue}"></b>{_esc(disp(name))} {tag}{ld}</span><span class="sval">{sc}<span class="hint">/100</span><span class="grade" style="color:{_grade(sc)[1]};border-color:{_grade(sc)[1]}">{_grade(sc)[0]}</span></span></div>
        <div class="bar"><i style="width:{sc}%;background:{hue}"></i></div>
        <p class="def">{_esc(DIM_BLURB[name])}</p>
        <p class="rate">{_esc(dim_rate_line(name))}<span class="wt"> · {"not scored — Claude does this on its own" if name in UNSCORED else f"weight {round(WEIGHTS[name] * 100)}%"}</span></p>
      </div>"""

    # archetype affinity
    aff = ""
    arch_hue = {nm: (SERIES_HUES + ["#9085e9", "#e66767"])[i] for i, nm in enumerate(PROTOTYPES)}
    for sim, nm in a["all"]:
        pct = max(0, round((sim + 1) / 2 * 100))
        aff += f"""<div class="bar-item"><div class="bl">{PROTOTYPES[nm]['emoji']} {_esc(nm)}</div>
          <div class="bt"><i style="width:{pct}%;background:{arch_hue[nm]}"></i></div><div class="bv">{sim:+.2f}</div></div>"""

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
                title = corpus.titles.get(e.get("session"))
                where = (f"“{title}” · " if title else "") + _project_label(proj)
                items += f'<li>“{_esc(txt[:140])}” <span class="loc">— {_esc(where)}{small}</span></li>'
            elif name == "Context":
                small = " <em>(illustrative)</em>" if proj_counts.get(e["project"], 0) < 10 else ""
                items += f'<li>Edited <code>{_esc(os.path.basename(e["file"]))}</code> without reading it first <span class="loc">— {_esc(_project_label(e["project"]))}{small}</span></li>'
            elif name in ("Planning", "Closure"):
                title = corpus.titles.get(e.get("session"))
                where = (f"“{title}” · " if title else "") + _project_label(e["project"])
                lead = "Started with" if name == "Planning" else "Ended without a commit on"
                items += (f'<li>{lead} “{_esc(_scrub_paths(e["text"])[:140])}” '
                          f'<span class="loc">— {_esc(where)}</span></li>')
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
        ex_intro = '<p class="exgen">Generic illustrations of the habit — <b>not</b> from your sessions:</p>'
        # With a deep read, show this person's own chats instead: what they asked, their message
        # rewritten, and why it would have gone better. Generic pairs only fill in when none exist.
        real = sorted((r for r in (deep or {}).get("reads") or []
                       if r.get("habit") == name and r.get("you_said") and r.get("better")),
                      key=lambda r: -r.get("friction_score", 0))[:3]
        if real and n_cases:
            # The cases right below already show your chats; keep the card short.
            ex_intro, ex_html = "", ""
        elif real:
            ex_intro = '<p class="exgen">From your own chats — what you asked and how it could have gone:</p>'
            ex_html = "".join(
                f'<div class="real-ex"><div class="chat">{_chat_ref_html(r.get("title"), r["project"], r.get("date"), r.get("session"))}</div>'
                + (f'<p class="why"><b>You wanted:</b> {_esc(r["goal"])}</p>' if r.get("goal") else "")
                + f'<div class="ba"><div class="before"><span>You said</span>“{_esc(r["you_said"][:400])}”</div>'
                f'<div class="after"><span>Better</span>“{_esc(r["better"][:600])}”</div></div>'
                + (f'<p class="why" style="margin-top:6px"><b>Why.</b> {_esc(r["why"])}</p>' if r.get("why") else "")
                + '</div>'
                for r in real)
        cards_html += f"""
      <div class="card prio">
        <div class="ph">Priority {i+1} · {_esc(disp(name))} <span class="pscore">now {card['score']}/100</span></div>
        <h4>{_esc(t['what_it_is'])}</h4>
        <p class="why"><b>Why it matters.</b> {_esc(t['why_it_matters'])}</p>
        <div class="wwh"><span class="lab">Where this shows up in your sessions</span>{evidence_html(card)}</div>
        <div class="wwh"><span class="lab">How to grow it</span><p class="how">{_esc(t['how_to_improve'])}</p>
          {ex_intro}
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
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist+Mono:wght@300..700&display=swap" rel="stylesheet">
<style>
:root{{--bg:#000;--p:#0a0a0a;--p2:#121214;--ink:#ededef;--mut:#8b8b95;--line:#222;
--sunset:linear-gradient(90deg,#ffb454,#f0718f 50%,#a99bff);
--ac:#7c5cff;--ac2:#3ad6c9;--ac-soft:#a99bff;--ac2-soft:#5fd6c9;
--q-hi:#7fb2ff;--q-lo:#f0a35e;--warn:#ffb454;--pink:#f08dbb;
--display:'Geist Pixel','Departure Mono','Geist Mono',ui-monospace,monospace;
/* 8-pt spacing scale */
--s1:8px;--s2:16px;--s3:24px;--s4:32px;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--ink);
font:15px/1.65 'Geist Mono',ui-monospace,'Cascadia Mono',Consolas,monospace;font-weight:350;padding-bottom:80px}}
.wrap{{max-width:880px;margin:0 auto;padding:0 var(--s3)}}
header{{text-align:center;padding:40px 0 var(--s2)}}
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
.band{{margin-top:var(--s2);font-size:22px;font-weight:700;display:inline-block}}
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
.ing .n{{font-size:26px;font-weight:800;color:var(--ac2-soft);letter-spacing:-.01em}}
.ingest .ing{{border-top:3px solid var(--ac)}}
.ingest.data{{grid-template-columns:repeat(auto-fit,minmax(112px,1fr))}}
.ingest.data .ing{{padding:14px 12px}} .ingest.data .ing .n{{font-size:21px;white-space:nowrap}}
.ingest .ing .n s{{text-decoration:none;font-size:.6em;color:var(--mut);margin-left:4px}}
.ingest.data .ing .l{{font-size:12px;line-height:1.4;margin-top:4px}}
.ingest .ing:nth-child(6n+2){{border-top-color:var(--ac2)}} .ingest .ing:nth-child(6n+2) .n{{color:var(--ac2)}}
.ingest .ing:nth-child(6n+1) .n{{color:var(--ac-soft)}}
.ingest .ing:nth-child(6n+3){{border-top-color:var(--q-hi)}} .ingest .ing:nth-child(6n+3) .n{{color:var(--q-hi)}}
.ingest .ing:nth-child(6n+4){{border-top-color:var(--q-lo)}} .ingest .ing:nth-child(6n+4) .n{{color:var(--q-lo)}}
.ingest .ing:nth-child(6n+5){{border-top-color:var(--pink)}} .ingest .ing:nth-child(6n+5) .n{{color:var(--pink)}}
.ingest .ing:nth-child(6n){{border-top-color:var(--warn)}} .ingest .ing:nth-child(6n) .n{{color:var(--warn)}}
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
.bl{{flex:0 0 220px;min-width:0;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}} .bt{{flex:1;height:8px;background:var(--p2);border-radius:3px;overflow:hidden}}
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
pre.rules{{background:var(--p2);border:1px solid var(--line);border-radius:10px;padding:var(--s2);margin:var(--s1) 0;
font-size:13.5px;white-space:pre-wrap;overflow-wrap:anywhere}}
.card .ph .tag{{text-transform:none;letter-spacing:0}}
.tabs{{position:sticky;top:0;z-index:5;display:flex;align-items:flex-end;gap:4px;overflow-x:auto;scrollbar-width:none;
background:rgba(8,8,10,.94);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);margin:0;padding:12px 96px 0 0}}
.tabs .brand{{flex:none;display:inline-flex;align-items:center;gap:8px;margin-right:10px;padding-left:0;
font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink);border-bottom-color:transparent}}
.tabs .brand:hover{{color:#fff}} .tabs .brand .logo{{color:#3ccf6e;flex:none}}
.tabs::-webkit-scrollbar{{display:none}}
.tabs button{{all:unset;cursor:pointer;white-space:nowrap;padding:9px 10px;border-radius:10px 10px 0 0;color:var(--mut);
font-size:13.5px;font-weight:600;border-bottom:2px solid transparent}}
.tabs button:hover{{color:var(--ink)}}
.tabs button[aria-selected="true"]{{color:var(--ink);border-bottom-color:var(--ac-soft)}}
.tabs button .badge{{display:none}}
.tabs button.hot{{color:var(--ink);position:relative;animation:tabglow 1.6s ease-in-out infinite}}
.tabs button.hot .badge{{display:inline-block;margin-left:7px;padding:1px 7px;border-radius:99px;font-size:11px;
line-height:1.5;background:var(--warn);color:#121214;animation:badgepop 1.6s ease-in-out infinite}}
@keyframes tabglow{{0%,100%{{box-shadow:inset 0 0 0 1px rgba(255,180,84,.0);background:transparent}}
50%{{box-shadow:inset 0 0 0 1px rgba(255,180,84,.55);background:rgba(255,180,84,.10)}}}}
@keyframes badgepop{{0%,100%{{transform:scale(1)}}50%{{transform:scale(1.15)}}}}
.panel:not([hidden]){{animation:panelin .32s ease-out}}
@keyframes panelin{{from{{opacity:0;transform:translateY(6px)}}to{{opacity:1;transform:none}}}}
.case{{animation:panelin .35s ease-out both}}
@media(prefers-reduced-motion:reduce){{.tabs button.hot,.tabs button.hot .badge,.panel,.case{{animation:none}}}}
.tabs button:focus-visible{{outline:2px solid var(--ac2-soft);outline-offset:-2px}}
.panel-head{{margin:var(--s4) 0 var(--s2);padding-left:14px;border-left:4px solid var(--ac-soft)}}
.panel-title{{font-size:30px;line-height:1.15;font-weight:800;color:var(--ac-soft);letter-spacing:-.01em}}
.panel-sub{{color:var(--mut);font-size:14.5px;margin-top:4px}}
.panel>section:first-of-type{{margin-top:var(--s2)}}
.me-track{{position:relative;overflow:visible}}
.me-track .me{{position:absolute;top:-3px;width:3px;height:14px;border-radius:2px;background:var(--ink);
box-shadow:0 0 0 2px var(--p)}}
svg.chart{{width:100%;height:150px;display:block;margin-top:6px;overflow:visible}}
svg.chart .colbar{{fill:var(--ac2-soft)}} svg.chart .hit{{fill:transparent}}
svg.chart .col:hover .colbar{{fill:var(--ac-soft)}} svg.chart .col:hover .hit{{fill:rgba(255,255,255,.04)}}
svg.chart .axis{{stroke:var(--line);stroke-width:1}}
svg.chart .tick{{fill:var(--mut);font-size:11px}} svg.chart .peak{{fill:var(--ink);font-size:11px;font-weight:700}}
.tbl{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-size:14px}}
.ingest.rhythm{{grid-template-columns:repeat(3,1fr)}} .legend{{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--mut);margin:8px 0 4px}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}}
.legend b{{color:var(--ink);font-weight:700}}
svg.chart.stack{{height:170px}} svg.chart .seg:hover rect{{opacity:.75}}
svg.heat{{display:block;width:100%;min-width:560px;height:auto;margin-top:6px}}
svg.heat text{{fill:var(--mut);font-size:10px}} svg.heat g:hover rect{{stroke:var(--ink);stroke-width:1.5}}
.heat-key{{display:flex;align-items:center;gap:3px;font-size:12px;color:var(--mut);margin-top:6px}}
.heat-key i{{width:18px;height:10px;border-radius:2px}} .heat-key span{{margin:0 6px}}
.mix{{display:flex;gap:2px;height:16px;border-radius:5px;overflow:hidden;margin-top:8px}} .mix>i{{display:block;height:100%}}
@font-face{{font-family:'Departure Mono';font-display:swap;
src:url(https://cdn.jsdelivr.net/gh/rektdeckard/departure-mono@v1.500/public/assets/DepartureMono-Regular.woff2) format('woff2')}}
@font-face{{font-family:'Geist Pixel';font-display:swap;
src:url(https://cdn.jsdelivr.net/npm/geist@1.7.0/dist/fonts/geist-pixel/GeistPixel-Square.woff2) format('woff2')}}
h1,h2,h3,h4,.panel-title,.tabs button,.tabs .brand,.kick,.band,.ing .n,.ring .n b,.sval,.pscore,.lang-switch,
.ph,.tag,.before span,.after span,summary,th,.legend b,
svg.chart .peak{{font-family:var(--display);font-weight:400;letter-spacing:.01em}}
h1{{font-size:32px}} .ring .n b{{font-size:50px}} .sval{{font-size:26px}}
.ba>*,.card,.assess,.def,.rate{{min-width:0;overflow-wrap:anywhere}}
.tt{{display:none}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 16px}}
.chip{{all:unset;cursor:pointer;font-family:var(--display);font-size:13px;padding:6px 12px;border-radius:99px;
border:1px solid var(--line);background:var(--p);color:var(--mut)}}
.chip i{{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:7px;background:var(--hue,var(--ac-soft))}}
.chip b{{font-weight:400;color:var(--ink);margin-left:4px}}
.chip.on{{background:color-mix(in srgb,var(--hue,var(--ac)) 22%,var(--p));border-color:var(--hue,var(--ac));color:var(--ink)}}
.chip:focus-visible{{outline:2px solid var(--ac2-soft);outline-offset:2px}}
.cases{{display:grid;gap:12px}}
.case{{background:var(--p);border:1px solid var(--line);border-left:4px solid var(--hue);border-radius:12px;padding:16px 18px}}
.case-h{{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px;margin-bottom:8px}}
.case-n{{font-family:var(--display);font-size:22px;color:var(--hue);line-height:1}}
.case-tag{{font-family:var(--display);font-size:12px;padding:2px 9px;border-radius:6px;color:#121214;background:var(--hue)}}
.case-w{{font-size:12.5px;color:var(--mut)}}
.case-goal{{font-size:14px;margin:4px 0 2px}} .case-goal b{{color:var(--hue)}}
.case .after{{border-left-color:var(--hue);background:color-mix(in srgb,var(--hue) 10%,transparent)}}
.case .after span{{color:var(--hue)}}
.case .resume{{display:inline-block;margin-top:10px}}
.nowrap{{white-space:nowrap}}
.tipbox{{position:fixed;z-index:50;display:none;pointer-events:none;max-width:290px;padding:9px 12px;
background:#26262e;border:1px solid #44444f;border-radius:10px;font-size:12.5px;line-height:1.55;color:var(--mut);
box-shadow:0 10px 28px rgba(0,0,0,.5)}}
.tipbox .tip-h{{font-family:var(--display);font-size:14px;color:var(--ink);margin-bottom:3px}}
.tipbox .tip-h i{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:7px}}
svg .wk .hit{{fill:transparent}} svg .wk:hover .hit{{fill:rgba(255,255,255,.05)}} svg .wk:hover rect:not(.hit){{filter:brightness(1.15)}}
svg.heat .has-tip:hover rect{{stroke:var(--ink);stroke-width:1.5}}
.bar-item.has-tip:hover .bt>i{{filter:brightness(1.2)}} .bar-item.has-tip:hover .bl{{color:var(--ink)}}
.mix>i:hover{{filter:brightness(1.2)}}
/* canirun-style flat surfaces: black page, near-black panels, 1px #222 lines, small radius */
.score-card,.arch,.card,.dim,.assess,.band-meaning,.ing,.evo,.case,.before,.after,.tipbox{{border-radius:10px}}
.score-card{{background:var(--p)}}
@media(max-width:1100px){{.tabs .brand em{{display:none}}}}
.tabs .brand em{{font-style:normal;color:var(--mut);font-size:10.5px;letter-spacing:.02em;text-transform:none}}
.deep-sum{{line-height:1.8}}
.band-name{{background:linear-gradient(90deg,#a99bff,#3ad6c9);-webkit-background-clip:text;background-clip:text;color:transparent}}
body .hl-n,body b .hl-n{{color:var(--warn);font-weight:600}}
mark.hl-q{{background:rgba(127,178,255,.12);color:var(--q-hi);border-radius:4px;padding:0 3px}}
mark.hl-k{{background:rgba(60,207,110,.12);color:#3ccf6e;border-radius:4px;padding:0 3px}}
.grades{{margin-top:16px;text-align:center}}
.gr-head{{font-size:12.5px;color:var(--mut)}} .gr-head b{{color:var(--ink);font-weight:600}}
.gr-line{{display:flex;gap:2px;height:3px;margin:8px auto 7px;max-width:220px;border-radius:2px;overflow:hidden}}
.gr-line i{{display:block}}
.gr-letters{{display:flex;justify-content:center;gap:12px;font-family:var(--display);font-size:13px}}
.grade{{display:inline-block;margin-left:10px;padding:0 6px;border:1px solid;border-radius:5px;font-size:15px;
line-height:1.35;vertical-align:4px}}
.evo-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:var(--s1);margin-top:8px}}
@media(max-width:760px){{.evo-grid{{grid-template-columns:repeat(2,1fr)}}}}
.evo.total{{background:linear-gradient(160deg,rgba(124,92,255,.16),var(--p) 60%)}}
.evo-name{{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-right:8px}}
.ring .n s.pm{{display:block;margin-top:2px;color:var(--ac2-soft)}}
.recent{{display:inline-flex;gap:10px;align-items:baseline;margin-top:10px;padding:5px 12px;border:1px solid var(--line);
border-radius:99px;font-size:12.5px;color:var(--mut)}} .recent b{{font-family:var(--display);font-weight:400;color:var(--ink);font-size:15px}}
.noise-note{{color:var(--mut)}}
.evo-why{{font-size:11.5px;line-height:1.45;color:var(--mut);margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}}
.spark-l{{display:flex;gap:4px;margin-top:4px}} .spark-l span{{flex:1;text-align:center;font-size:10px;color:var(--mut)}}
.evo{{background:var(--p);border:1px solid var(--line);border-top:3px solid var(--hue);border-radius:12px;padding:14px 14px 10px}}
.evo-top{{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:var(--mut)}}
.evo-name b{{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:7px}}
.evo-n{{font-family:var(--display);font-size:30px;color:var(--hue);margin:4px 0 6px;line-height:1}}
.evo-n s{{text-decoration:none;font-size:12px;color:var(--mut);margin-left:3px}}
.spark{{display:flex;align-items:flex-end;gap:4px;height:46px;border-bottom:1px solid var(--line)}}
.spark i{{flex:1;border-radius:3px 3px 0 0;min-height:3px}} .spark i.now{{opacity:.45}} .spark i:hover{{filter:brightness(1.25)}}
.evo .up,.evo .down,.evo .flat{{font-family:var(--display);font-size:14px;font-weight:400}}
.up{{color:var(--q-hi)}} .down{{color:var(--q-lo)}} .flat{{color:var(--mut)}}
svg.chart.months{{height:190px}} svg.chart .guide{{stroke:var(--line);stroke-dasharray:3 4}}
svg.chart .mb.up{{fill:var(--q-hi)}} svg.chart .mb.down{{fill:var(--q-lo)}} svg.chart .mb.same,svg.chart .mb.first{{fill:var(--ac-soft)}}
svg.chart .mb.now{{opacity:.55}} svg.chart .col:hover .mb{{filter:brightness(1.2)}} svg.chart .tick.sub{{font-size:9.5px;opacity:.75}}
.dim{{border-left:3px solid var(--hue,var(--line))}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:9px;vertical-align:0}}
.hist .up{{color:var(--q-hi);font-size:12px;font-weight:700}} .hist .down{{color:var(--q-lo);font-size:12px;font-weight:700}}
th,td{{text-align:left;vertical-align:top;padding:10px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em}}
td.num,th.num{{text-align:right}} .nowrap{{white-space:nowrap}} .frac{{color:var(--mut);font-size:.85em;margin-left:6px}} tr.you td{{background:rgba(169,155,255,.08)}}
.real-ex{{border-top:1px solid var(--line);padding-top:12px;margin-top:12px}}
.real-ex .chat{{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;margin-bottom:4px}}
code.resume{{font-size:11.5px;color:var(--mut);user-select:all;overflow-wrap:anywhere}}
@media(max-width:640px){{.ingest.data{{grid-template-columns:repeat(2,1fr)}}.ingest.data .ing:last-child:nth-child(odd){{grid-column:span 2}}.ingest.rhythm{{grid-template-columns:repeat(2,1fr)}}.bl{{flex-basis:130px}}.bar-item{{gap:10px}}.ba{{grid-template-columns:1fr}}.bl{{min-width:120px}}.tabs .brand{{display:none}}}}
</style>
<script>
document.addEventListener('DOMContentLoaded', function () {{
  function show(tab) {{
    document.querySelectorAll('[data-panel]').forEach(function (p) {{ p.hidden = p.getAttribute('data-panel') !== tab; }});
    document.querySelectorAll('.tabs [data-tab]').forEach(function (b) {{
      b.setAttribute('aria-selected', String(b.getAttribute('data-tab') === tab)); }});
    try {{ localStorage.setItem('ai-fluency-tab', tab); }} catch (e) {{}}
    seen(tab);
  }}
  // A tab stops calling for attention once you have opened it (remembered in this browser).
  function seen(tab) {{
    document.querySelectorAll('.tabs [data-tab="' + tab + '"].hot').forEach(function (b) {{ b.classList.remove('hot'); }});
    try {{ localStorage.setItem('ai-fluency-seen-' + tab, '1'); }} catch (e) {{}}
  }}
  ['improve', 'usage'].forEach(function (t) {{
    var was = null; try {{ was = localStorage.getItem('ai-fluency-seen-' + t); }} catch (e) {{}}
    if (was) seen(t);
  }});
  document.addEventListener('click', function (ev) {{
    var b = ev.target.closest('.tabs [data-tab]');
    if (b) {{
      show(b.getAttribute('data-tab'));
      var lang = b.closest('[data-lang]') || document;
      var panel = lang.querySelector('[data-panel="' + b.getAttribute('data-tab') + '"]');
      var nav = b.closest('.tabs');
      if (panel && nav) {{
        window.scrollTo(0, 0);
      }}
      return;
    }}
    if (ev.target.closest('[data-home]')) {{
      show('summary');
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
      return;
    }}
    var g = ev.target.closest('[data-goto]');
    if (!g) return;
    ev.preventDefault();
    show(g.getAttribute('data-goto'));
    var id = g.getAttribute('href').slice(1);
    var target = Array.prototype.find.call(document.querySelectorAll('[id="' + id + '"]'),
      function (el) {{ return !el.closest('[hidden]'); }});
    if (!target) return;
    var d = target.closest('details'); if (d) d.open = true;
    target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }});
  // Every opening starts on the Summary: the score and your evolution come first.
  show('summary');
}});
</script>
<script>{_TIP_JS}</script>
</head><body><div class="wrap">

<nav class="tabs" role="tablist">
  <button type="button" class="brand" data-home title="Back to the start"><svg class="logo" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M9 13a4.5 4.5 0 0 0 3-4"/><path d="M6.003 5.125A3 3 0 0 0 6.401 6.5"/><path d="M3.477 10.896a4 4 0 0 1 .585-.396"/><path d="M6 18a4 4 0 0 1-1.967-.516"/><path d="M12 13h4"/><path d="M12 18h6a2 2 0 0 1 2 2v1"/><path d="M12 8h8"/><path d="M16 8V5a2 2 0 0 1 2-2"/><circle cx="16" cy="13" r=".5"/><circle cx="18" cy="3" r=".5"/><circle cx="20" cy="21" r=".5"/><circle cx="20" cy="8" r=".5"/></svg><span>Claude Insight</span><em>{generated_at:%Y-%m-%d}</em></button>
  <button type="button" role="tab" data-tab="summary">Summary</button>
  <button type="button" role="tab" data-tab="dims">Dimensions</button>
  <button type="button" role="tab" data-tab="improve" class="hot">What to improve{improve_badge}</button>
  <button type="button" role="tab" data-tab="chats">Your chats</button>
  <button type="button" role="tab" data-tab="usage" class="hot">Usage</button>
  <button type="button" role="tab" data-tab="method">How we evaluate</button>
</nav>

<div class="panel" data-panel="summary" role="tabpanel">
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
      <div class="n"><b>{result['overall']}</b><s>/ 100</s>{f'<s class="pm">±{margin_points(margin["overall"])}</s>' if margin else ""}</div>
    </div>
    <div class="band"><span class="band-name">{_esc(result['band'])}</span><span class="grade" style="color:{_grade(result['overall'])[1]};border-color:{_grade(result['overall'])[1]}">{_grade(result['overall'])[0]}</span></div>
    <div class="rawnote">raw {result['overall_raw']} · confidence-adjusted {result['overall']}</div>
    {_recent_html(recent)}
    {_grade_bar_html(result)}
  </div>
  <div class="arch">
    <h2>{_esc(a['label'])}</h2>
    <p>{_esc(a['blurb'])}</p>
    <p style="margin-top:10px;font-size:13px">Closest match {a['primary_sim']:+.2f}, next is {_esc(a['secondary'].replace('The ',''))} {a['secondary_sim']:+.2f}{' — close, so this is a blend' if a['blended'] else ''}. Built from how <b>you</b> drive — your briefs, corrections, tool choices and how much you hand off ({a['delegation_score']}/100 delegation) — and it deliberately discounts the read-before-edit and run-the-tests habits Claude does on its own, so it reflects you, not the agent.</p>
    <p style="margin-top:8px;font-size:12.5px;color:var(--mut)">Your <b>score</b> measures the quality of the collaboration (you + Claude); your <b>archetype</b> measures your driving style alone — so they can differ on purpose.</p>
    {arch_hedge}
  </div>
</div>

<div class="band-meaning" style="margin-top:22px;border-left-color:{_grade(result['overall'])[1]}"><b>{_esc(result['band'])} ({result['overall']}/100).</b> {_esc(result['band_meaning'])}</div>
{meta_line}
{_history_html(history)}

<section>
  <h3>Professional assessment</h3>
  {assessment_html}
</section>
{analysis_section}
{analysis_status_html}
<section>
  <h3>Your foundation</h3>
  {strength_html}
</section>
</div>

<div class="panel" data-panel="dims" role="tabpanel" hidden>
<div class="panel-head"><h2 class="panel-title">Dimensions</h2><p class="panel-sub">Your score in each of the seven dimensions and the level of each skill.</p></div>
<section>
  <h3>The seven dimensions</h3>
  {dim_html}
</section>
<section>
  <h3>Where you stand</h3>
  {skill_html}
</section>
<section>
  <h3>Archetype affinity — how close you are to each builder style</h3>
  {aff}
</section>
</div>

<div class="panel" data-panel="improve" role="tabpanel" hidden>
<div class="panel-head"><h2 class="panel-title">What to improve</h2><p class="panel-sub">Your highest-impact habits, with examples from your own chats.</p></div>
<section>
  <h3>What to improve — and exactly how</h3>
  {improve_intro}
  {improve_cards}
</section>
{_cases_html(deep)}
</div>

<div class="panel" data-panel="chats" role="tabpanel" hidden>
<div class="panel-head"><h2 class="panel-title">Your chats</h2><p class="panel-sub">The deep read of your conversations and your latest chats.</p></div>
{deep_section or deep_missing}
<section>
  <h3>Your last {RECENT_CHATS} chats</h3>
  <p class="def">Where the report's data comes from. Paste the command into a terminal in that project's folder to reopen a chat.</p>
  {_recent_chats_html(corpus, deep)}
</section>
</div>

<div class="panel" data-panel="usage" role="tabpanel" hidden>
<div class="panel-head"><h2 class="panel-title">Usage</h2><p class="panel-sub">Your activity over time and the tools Claude used.</p></div>
<section>
  <h3>How much data this is based on</h3>
  <div class="ingest data">
    <div class="ing"><div class="n">{corpus.files}</div><div class="l">sessions scanned</div></div>
    <div class="ing"><div class="n">{len(corpus.projects)}</div><div class="l">projects</div></div>
    <div class="ing"><div class="n">{_size_html(corpus.total_bytes)}</div><div class="l">transcript data parsed</div></div>
    <div class="ing"><div class="n">{days}<s>days</s></div><div class="l">span of activity</div></div>
    <div class="ing"><div class="n">{len(corpus.real_prompts)}</div><div class="l">real prompts you typed</div></div>
    <div class="ing"><div class="n">{active_h:.0f}<s>h</s></div><div class="l">hands-on active time</div></div>
    {archive_tile}
  </div>
</section>
<section>
  <h3>Your activity</h3>
  {_usage_charts_html(corpus)}
</section>
{usage_section}
<section>
  <h3>Honest numbers at a glance</h3>
  <ul class="facts">{facts_html}</ul>
</section>
</div>

<div class="panel" data-panel="method" role="tabpanel" hidden>
<div class="panel-head"><h2 class="panel-title">How we evaluate</h2><p class="panel-sub">How every number in this report is calculated.</p></div>
{_method_html(result, audit)}
</div>

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
        ("Planning before building", "Planning",
         "for anything bigger than a quick fix, ask for the plan or options before any code",
         {1: "Fires build orders cold.", 2: "Occasionally asks how to approach it.",
          3: "Talks some work through before building.", 4: "Usually agrees a plan before the first change.",
          5: "Vision → structure → requirements is the habit."}),
        ("Closing the work", "Closure",
         "end each checked piece of work with a commit you ask for",
         {1: "Work rarely gets committed.", 2: "Commits now and then.",
          3: "About half the work ends in a commit.", 4: "Most work ends checked and committed.",
          5: "Every piece closes checked, committed or with a clear note."}),
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
           "Iteration": "Iteración", "Toolcraft": "Herramientas", "Planning": "Planificación", "Closure": "Cierre"}

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
    # deep read
    "Your chats in depth": "Tus chats a fondo",
    "Where the friction came from:": "De dónde vino la fricción:",
    "your brief": "tu pedido", "the environment": "el entorno", "no friction": "sin fricción",
    "Your recurring habits": "Tus hábitos que se repiten",
    "Rules you keep repeating": "Reglas que repites",
    "— paste them into your CLAUDE.md so you never have to say them again:":
        "— pégalas en tu CLAUDE.md para no tener que decirlas nunca más:",
    "resolved": "resuelto", "partly resolved": "a medias", "abandoned": "abandonado", "unclear outcome": "final incierto",
    # tabs
    "Summary": "Resumen", "Dimensions": "Dimensiones", "What to improve": "Qué mejorar", "Your chats": "Tus chats",
    "Usage": "Uso", "How we evaluate": "Cómo evaluamos", "Your foundation": "Tu base", "Your activity": "Tu actividad",
    # method: detector audit
    "How often each detector is right": "Cuánto acierta cada detector",
    "The numbers come from keyword detectors. Each one was checked by hand against a separate sample of your own prompts, one that was not used to tune it.":
        "Los números salen de detectores de palabras clave. Cada uno se revisó a mano con una muestra aparte de tus propios prompts, que no se usó para ajustarlo.",
    "What it looks for": "Qué busca", "Right when it counts one": "Acierta cuando cuenta uno", "Ones it missed": "Los que se le escapan",
    "Right when it counts one: of the prompts the detector counted, how many really had it. Ones it missed: of the prompts it skipped, how many it should have counted. There are few labels per detector, so read these as rough numbers.":
        "Acierta cuando cuenta uno: de los prompts que el detector contó, cuántos sí lo tenían. Los que se le escapan: de los que dejó pasar, cuántos debió contar. Hay pocas etiquetas por detector, así que tómalos como números aproximados.",
    "A rule or limit (only X, don't touch Y)": "Una regla o límite (solo X, no toques Y)",
    "A file, path, function or error named": "Nombra un archivo, ruta, función o error",
    "Why you want it": "Para qué lo quieres",
    "Only an approval or a “continue”": "Solo un visto bueno o un “sigue”",
    "Correcting what Claude just did": "Corrige lo que Claude acaba de hacer",
    "Reporting what you saw in the result": "Cuenta lo que viste en el resultado",
    "Asking for a plan first": "Pide un plan antes",
    "The written read of how you work with Claude, and your strongest habit.":
        "La evaluación escrita de cómo trabajas con Claude y tu hábito más fuerte.",
    "Your score in each of the seven dimensions and the level of each skill.":
        "Tu puntaje en cada una de las siete dimensiones y el nivel de cada habilidad.",
    "Your highest-impact habits, with examples from your own chats.":
        "Los hábitos que más impacto tienen, con ejemplos de tus propios chats.",
    "The deep read of your conversations and your latest chats.":
        "La lectura a fondo de tus conversaciones y tus últimos chats.",
    "Your activity over time and the tools Claude used.": "Tu actividad en el tiempo y las herramientas que usó Claude.",
    "How every number in this report is calculated.": "Cómo se calcula cada número de este reporte.",
    "Where the report's data comes from. Paste the command into a terminal in that project's folder to reopen a chat.":
        "De aquí salen los datos del reporte. Pega el comando en una terminal abierta en la carpeta de ese proyecto para reabrir el chat.",
    "No deep read yet. Ask Claude Code to run": "Todavía no hay lectura a fondo. Pídele a Claude Code que corra",
    "in depth: it reads your conversations end to end and fills this tab with your habits, the rules worth saving to CLAUDE.md and a “you said → better” for each chat.":
        "a fondo: lee tus conversaciones de principio a fin y llena esta pestaña con tus hábitos, las reglas que conviene guardar en CLAUDE.md y un “dijiste → mejor” por chat.",
    "Your evolution, month by month": "Tu evolución mes a mes", "Month": "Mes", "Score": "Puntaje",
    "too few prompts to score": "muy pocos prompts para puntuar", "so far": "en curso",
    "All": "Todos", "You wanted:": "Querías:", "You said": "Dijiste", "Better": "Mejor", "Why.": "Por qué.",
    "Every quote was checked word for word against your transcripts. Filter by the habit each case trains.":
        "Cada cita se comprobó palabra por palabra contra tus transcripciones. Filtra por el hábito que entrena cada caso.",
    "days": "días", "new": "nuevo", "Back to the start": "Volver al inicio",
    "prompts with a constraint": "prompts con una restricción",
    "prompts naming a file or error": "prompts que nombran un archivo o error",
    "prompts that say why": "prompts que dicen el porqué",
    "changes Claude checked": "cambios que comprobó Claude", "changes you checked": "cambios que comprobaste tú",
    "changes nobody checked": "cambios que nadie comprobó",
    "prompts that were corrections": "prompts que fueron correcciones", "specific corrections": "correcciones específicas",
    "work that started with a plan": "trabajos que empezaron con un plan",
    "work talked through first": "trabajos conversados antes", "prompts asking why": "prompts que preguntan por qué",
    "sessions ended in a commit": "sesiones que terminaron en commit",
    "sessions ended on a complaint": "sesiones que terminaron con una queja",
    "tool balance": "equilibrio de herramientas", "edits after reading the file": "ediciones después de leer el archivo",
    "not scored — Claude does this on its own": "no puntúa — lo hace Claude solo",
    "· not scored — Claude does this on its own": "· no puntúa — lo hace Claude solo",
    "within noise": "dentro del ruido", "no real change, or first month": "sin cambio real, o primer mes",
    "That change is within the noise.": "Ese cambio está dentro del ruido.",
    "The ± is the margin of error: the score is recomputed 12 times, each time leaving out a different twelfth of your sessions, and the spread gives a 95% range. In your evolution, a change smaller than the margins of both months is marked “≈” — it could be noise.":
        "El ± es el margen de error: el puntaje se recalcula 12 veces, cada vez dejando fuera una doceava parte distinta de tus sesiones, y la dispersión da un rango del 95%. En tu evolución, un cambio menor que los márgenes de los dos meses se marca con “≈”: puede ser ruido.",
    "outcome": "resultado", "who caused the friction": "quién causó la fricción", "habit to train": "hábito a entrenar",
    "Where they disagreed, that answer is treated as uncertain (a case whose habit they disagreed on is shown without one).":
        "Donde no coincidieron, esa respuesta se trata como dudosa (un caso en el que no coincidieron en el hábito se muestra sin hábito).",
    "went up": "subió", "went down": "bajó", "first month": "primer mes", "Overall score": "Puntaje total",
    "month still in progress": "mes todavía en curso",
    "Jan": "ene", "Feb": "feb", "Mar": "mar", "Apr": "abr", "May": "may", "Jun": "jun", "Jul": "jul",
    "Aug": "ago", "Sep": "sep", "Oct": "oct", "Nov": "nov", "Dec": "dic",
    "Each dimension, month by month": "Cada dimensión, mes a mes",
    "Your rhythm": "Tu ritmo", "Prompts per week, by project": "Prompts por semana, por proyecto",
    "When you work": "Cuándo trabajas", "(local time, by day and hour)": "(hora local, por día y hora)",
    "What Claude did for you": "Qué hizo Claude por ti", "(tool calls by type)": "(llamadas a herramientas por tipo)",
    "fewer": "menos", "more": "más",
    "days you worked, out of the whole span": "días que trabajaste, del periodo completo",
    "longest streak (days in a row)": "racha más larga (días seguidos)",
    "busiest day of the week": "día de la semana más activo", "peak hour": "hora pico",
    "prompts on a typical active day": "prompts en un día activo típico",
    "of your prompts after midnight (0h–6h)": "de tus prompts después de medianoche (0h–6h)",
    "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles", "Thursday": "Jueves",
    "Friday": "Viernes", "Saturday": "Sábado", "Sunday": "Domingo",
    "Mon": "Lun", "Tue": "Mar", "Wed": "Mié", "Thu": "Jue", "Fri": "Vie", "Sat": "Sáb", "Sun": "Dom",
    "Claude app": "App de Claude", "Connectors": "Conectores", "Other MCP": "Otros MCP",
    "Terminal": "Terminal", "Edit files": "Editar archivos", "Read & search": "Leer y buscar",
    "Browser & screen": "Navegador y pantalla", "Plan & delegate": "Planificar y delegar",
    "Prompts per week": "Prompts por semana", "What time you work": "A qué hora trabajas", "(local time)": "(hora local)",
    "Projects where you write the most": "Proyectos donde más escribes", "(prompts)": "(prompts)",
    "Tools Claude used the most": "Herramientas que más usó Claude", "(calls)": "(llamadas)", "No data.": "Sin datos.",
    "Last activity": "Última actividad", "Chat": "Chat", "Your prompts": "Tus prompts", "Claude actions": "Acciones de Claude",
    "(untitled)": "(sin nombre)", "read in depth": "leído a fondo", "not read": "no leído",
    "To read exactly these chats, ask Claude Code to run": "Para leer justo estos chats, pídele a Claude Code que corra",
    "in depth with": "a fondo con",
    # how we evaluate
    "Two layers": "Dos capas", "The numbers": "Los números", "The deep read": "La lectura a fondo",
    "are computed on your computer from your Claude Code transcripts, with the same rules for everyone: no AI is involved, and the same data always gives the same score.":
        "se calculan en tu computadora a partir de tus transcripciones de Claude Code, con las mismas reglas para todos: no interviene ninguna IA, y los mismos datos siempre dan el mismo puntaje.",
    "(only when you ask for it) has Claude read whole conversations. Every quote it attributes to you is checked word for word against your transcripts; anything that doesn't match is thrown out.":
        "(solo cuando la pides) hace que Claude lea conversaciones completas. Cada cita que te atribuye se comprueba palabra por palabra contra tus transcripciones; lo que no coincide se descarta.",
    "Dimension": "Dimensión", "You": "Tú", "No folder": "Sin carpeta",
    "How the conversations were built (vision → structure → requirements):":
        "Cómo se armaron las conversaciones (visión → estructura → requisitos):",
    "vision": "visión", "structure before code": "estructura antes del código",
    "precise requirements": "requisitos precisos", "you caught an error": "detectaste un error",
    "ended in a commit": "terminó en commit",
    "It also checks, chat by chat, whether there was a vision, a structure agreed before code and precise requirements; whether you caught an error Claude missed; and whether it ended in a commit.":
        "Además revisa, chat por chat, si hubo visión, una estructura acordada antes del código y requisitos precisos; si detectaste un error que Claude no vio; y si terminó en commit.",
    "The seven dimensions": "Las siete dimensiones",
    "Talking real work through before the first change: vision, structure, then requirements.":
        "Conversar el trabajo real antes del primer cambio: visión, estructura y después requisitos.",
    "Ending work checked and committed when you decide — not abandoned mid-problem.":
        "Terminar el trabajo comprobado y con commit cuando tú decides — no abandonado a medio problema.",
    "Planning before building": "Planificar antes de construir", "Closing the work": "Cerrar el trabajo",
    "Fires build orders cold.": "Lanza órdenes de construir en frío.",
    "Occasionally asks how to approach it.": "A veces pregunta cómo encararlo.",
    "Talks some work through before building.": "Conversa parte del trabajo antes de construir.",
    "Usually agrees a plan before the first change.": "Suele acordar un plan antes del primer cambio.",
    "Vision → structure → requirements is the habit.": "Visión → estructura → requisitos es el hábito.",
    "Work rarely gets committed.": "El trabajo casi nunca llega a commit.",
    "Commits now and then.": "Hace commit de vez en cuando.",
    "About half the work ends in a commit.": "Cerca de la mitad del trabajo termina en commit.",
    "Most work ends checked and committed.": "Casi todo el trabajo termina comprobado y con commit.",
    "Every piece closes checked, committed or with a clear note.": "Cada parte cierra comprobada, con commit o con una nota clara.",
    "for anything bigger than a quick fix, ask for the plan or options before any code":
        "para todo lo que sea más que un arreglo rápido, pide el plan u opciones antes de cualquier código",
    "end each checked piece of work with a commit you ask for": "cierra cada trabajo comprobado con un commit que pidas tú",
    "Started with": "Empezó con", "Ended without a commit on": "Terminó sin commit en",
    _DIM_HOW["Planning"]: "En sesiones con trabajo real (3+ cambios): antes del primer cambio, si pediste un plan, opciones o una estructura, o usaste el modo plan (50%); si hubo conversación antes (25%); y qué tan seguido preguntas el porqué (25%).",
    _DIM_HOW["Closure"]: "En proyectos con git: cuántas sesiones de trabajo terminan en commit (60%) y qué tan pocas terminan con una queja sin resolver (40%). Quién pidió el commit se muestra, pero no se puntúa.", "What is measured": "Qué se mide", "Weight": "Peso",
    "Your overall score is the weighted average. With little data, a dimension is pulled toward 50 until there is enough evidence (“low data”).":
        "Tu puntaje general es el promedio ponderado. Con pocos datos, una dimensión se acerca a 50 hasta que haya evidencia suficiente (“pocos datos”).",
    "Levels": "Niveles", "Level": "Nivel", "What it means": "Qué significa", "Archetypes": "Arquetipos",
    "The archetype describes your": "El arquetipo describe tu", "style": "estilo",
    ", not your level. Each archetype has a typical profile across six signals. Your profile is compared with each one (cosine similarity, from −1 to +1) and the closest wins; when two are very close it's shown as a blend. Habits that Claude does on its own — reading before editing, running the tests — count much less here, so the archetype reflects how":
        ", no tu nivel. Cada arquetipo tiene un perfil típico en seis señales. Tu perfil se compara con cada uno (similitud coseno, de −1 a +1) y gana el más cercano; si dos quedan muy cerca, se muestra como mezcla. Los hábitos que Claude hace solo — leer antes de editar, correr los tests — cuentan mucho menos aquí, para que el arquetipo refleje cómo manejas",
    "drive.": ".", "your archetype": "tu arquetipo", "Typical profile (0–100) — the white mark is you:": "Perfil típico (0–100) — la marca blanca eres tú:",
    "Delegation": "Delegación",
    "How the deep read works": "Cómo funciona la lectura a fondo",
    "Each session is cut into conversations: your request, what Claude did, how you replied, how it ended.":
        "Cada sesión se corta en conversaciones: tu pedido, lo que hizo Claude, cómo respondiste y cómo terminó.",
    "It picks which to read: half the roughest, a quarter the most recent, a quarter that went smoothly.":
        "Elige cuáles leer: la mitad las más difíciles, un cuarto las más recientes y un cuarto que salieron bien.",
    "Claude Sonnet reads each one whole; Claude Opus looks for patterns across all of them.":
        "Claude Sonnet lee cada una completa; Claude Opus busca patrones entre todas.",
    "Every quote is checked against your transcripts before it reaches the report.":
        "Cada cita se comprueba contra tus transcripciones antes de llegar al reporte.",
    _DIM_HOW["Direction"]: "De los prompts que piden trabajo (no cuentan los “sí, hazlo”, las preguntas ni avisos como “sale eso”): cuántos traen una restricción, nombran un archivo o error, o dicen el porqué — en inglés o español — y qué tan pocas reglas revelas recién en una corrección.",
    _DIM_HOW["Verification"]: "Cada tanda de cambios de código recibe un veredicto: la comprobó Claude (un test, build, lint, correr un script o mirar la app en el navegador), tú (dices qué viste, qué se rompió o pides que se compruebe), ambos o nadie. La mitad son las comprobaciones de Claude y la mitad las tuyas; un cambio sin comprobar que apruebas con “ok” resta. Los cambios en notas (.md, .txt) no cuentan.",
    _DIM_HOW["Context"]: "De cada cambio que hizo Claude: si el archivo se leyó (o se creó) antes en esa misma sesión. "
                         "Se muestra, pero no puntúa: lo hace Claude solo.",
    _DIM_HOW["Iteration"]: "Qué tan seguido tienes que corregir (menos es mejor) y cuántas de tus correcciones dicen qué se rompió en vez de solo “no”.",
    _DIM_HOW["Toolcraft"]: "Cuántas herramientas distintas se usan, qué tan parejo, y qué tan seguido se delega trabajo a subagentes, tareas en segundo plano o modo plan.",
    "From your own chats — what you asked and how it could have gone:":
        "De tus propios chats — qué pediste y cómo pudo haber salido:",
    "You wanted:": "Querías:", "Reopen it:": "Ábrelo de nuevo:", "Better": "Mejor",
    "What was missing.": "Qué faltó.", "Key moment.": "Momento clave.", "You said": "Dijiste", "Why.": "Por qué.",
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
    "Planning": ("Conversar un trabajo antes del primer cambio: la visión (qué y para qué), luego la estructura (un plan u opciones) y al final los requisitos precisos.",
                 "Un plan acordado en dos mensajes sale más barato que un build que tienes que corregir en diez; el agente propone opciones y pregunta lo que necesita en vez de adivinar.",
                 "Para todo lo que sea más que un arreglo rápido, empieza contando la idea y pide el plan o las opciones antes de cualquier código. Decide tú, y después pide el build con los detalles.",
                 ["hazme un bot que mande ofertas de trabajo junior de Python a Discord", "Quiero un bot para mi portafolio que me avise por Discord de ofertas junior de Python en mi zona. No sé si hacer scraping o usar una API. ¿Cómo lo plantearías? Dame dos opciones con pros y contras — todavía sin código.",
                  "agrega login a la app", "Antes de tocar código: propón cómo agregarías login a esta app (sesión o JWT, qué archivos cambian, qué probar) en una tabla corta. Yo elijo y después lo implementas."],
                 "Antes de tu próximo build, manda un mensaje que pregunte “¿cómo lo plantearías?” y recién después pide código.",
                 "El trabajo real empieza con una conversación corta y un plan acordado; la orden de construir llega al final, sobre un contexto compartido."),
    "Closure": ("Terminar el trabajo: comprobarlo, dejarlo en un commit que decides tú y no dejar que una sesión muera con un problema sin resolver.",
                "El trabajo sin commit es fácil de perder y difícil de deshacer; una sesión que termina en “sigue sin funcionar” le deja el problema a tu yo de mañana.",
                "Cuando un trabajo está hecho y comprobado, pide tú el commit con un mensaje claro. Si al parar algo sigue roto, pide una nota corta de cómo quedó.",
                ["ok", "Funciona: el formulario guarda y la lista se refresca. Haz commit como “Guardar el formulario y refrescar la lista” y dime qué queda para mañana.",
                 "sigue sin funcionar, lo dejo", "Sigue fallando con el mismo error al guardar. Escribe una nota de tres líneas en NOTAS.md con lo que probamos y qué intentar después, y no hagas commit."],
                "Termina tu próxima sesión de trabajo con “haz commit como …” o con una nota de cómo quedó.",
                "Cada trabajo termina comprobado y con commit cuando tú lo dices, o con una nota clara de lo pendiente."),
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
    _GROWTH_LINE["Planning"]: "Ahora mismo casi todo el trabajo real empieza con la orden de construir — un “¿cómo lo plantearías?” antes te ahorraría las rondas de corrección.",
    _GROWTH_LINE["Closure"]: "Ahora mismo mucho trabajo terminado nunca llega a un commit — cerrar cada parte cuando la comprobaste mantiene tu historial a salvo.",
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
    # Rate lines join several facts with " · "; translate them piece by piece.
    if " · " in s:
        parts = s.split(" · ")
        done = [_tr_es(p) for p in parts]
        if any(d is not None for d in done):
            return " · ".join(p if d is None else d for p, d in zip(parts, done))
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
    (r"(\d+)% carry a constraint · (\d+)% name a file/error · (\d+)% state a why(?: · (\d+) task prompts \((\d+) go-aheads like “sí, hazlo” and (\d+) questions or notes left out\))?",
     lambda m: f"{m[1]}% traen una restricción · {m[2]}% nombran un archivo/error · {m[3]}% dicen el porqué"
               + (f" · {m[4]} pedidos de trabajo (no cuentan {m[5]} vistos buenos tipo “sí, hazlo” ni {m[6]} "
                  f"preguntas o avisos)" if m[4] else "")),
    (r"(\d+) changes: Claude checked (\d+), you checked (\d+), both (\d+), nobody (\d+)",
     lambda m: f"{m[1]} tandas de cambios: comprobó Claude {m[2]}, comprobaste tú {m[3]}, ambos {m[4]}, nadie {m[5]}"),
    (r"(\d+) unchecked changes you approved with “ok”",
     lambda m: f"{m[1]} cambios sin comprobar que aprobaste con “ok”"),
    (r"(\d+) of (\d+) work sessions talked it through before the first change",
     lambda m: f"{m[1]} de {m[2]} sesiones de trabajo se conversaron antes del primer cambio"),
    (r"(\d+) had a conversation first", lambda m: f"{m[1]} tuvieron conversación antes"),
    (r"(\d+)% of your prompts ask why", lambda m: f"el {m[1]}% de tus prompts pregunta el porqué"),
    (r"(\d+) of (\d+) work sessions in git projects ended in a commit \((\d+) you asked for, (\d+) Claude did alone\)",
     lambda m: f"{m[1]} de {m[2]} sesiones de trabajo en proyectos con git terminaron en commit "
               f"({m[3]} que pediste tú, {m[4]} que hizo Claude solo)"),
    (r"(\d+) sessions ended on an unresolved complaint", lambda m: f"{m[1]} sesiones terminaron con una queja sin resolver"),
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
    (r"Read (\d+) of your (\d+) conversations end to end — the roughest, some that went well, and the most recent\.",
     lambda m: f"Se leyeron {m[1]} de tus {m[2]} conversaciones de principio a fin — las más difíciles, algunas que "
               "salieron bien y las más recientes."),
    (r"(\d+) quotes were discarded because they did not match your transcripts word for word\.",
     lambda m: f"Se descartaron {m[1]} citas porque no coincidían palabra por palabra con tus transcripciones."),
    (r"Seen in (\d+) chats", lambda m: f"Visto en {m[1]} chats"),
    (r"([✓✗]) (vision|structure|requirements)",
     lambda m: f"{m[1]} " + {"vision": "visión", "structure": "estructura", "requirements": "requisitos"}[m[2]]),
    (r"Other = your other (\d+) projects together\.", lambda m: f"Otro = tus otros {m[1]} proyectos juntos."),
    (r"\(big number: (\S+); change since (\S+)\)",
     lambda m: f"(número grande: {m[1]}; cambio desde {m[2]})"),
    (r"Not scored: (.+)\.", lambda m: f"Sin puntaje: {m[1]}."),
    (r"(\d+) cases from your chats: what you asked and how to ask it",
     lambda m: f"{m[1]} casos de tus chats: qué pediste y cómo pedirlo"),
    (r"(.+) · (\d+) of (\d+) chats", lambda m: f"{_tr_or_keep(m[1])} · {m[2]} de {m[3]} chats"),
    (r"([\d,]+) chats", lambda m: f"{m[1]} chats"),
    (r"Since (\w+): no measure changed", lambda m: f"Desde {_tr_or_keep(m[1])}: ninguna medida cambió"),
    (r"Since (\w+): (.+) moved the most \(([+-]\d+)\)",
     lambda m: f"Desde {_tr_or_keep(m[1])}: lo que más cambió fue {_tr_or_keep(m[2])} ({m[3]})"),
    (r"Since (\w+): (.+) (\d+)% → (\d+)%",
     lambda m: f"Desde {_tr_or_keep(m[1])}: {_tr_or_keep(m[2])} {m[3]}% → {m[4]}%"),
    (r"(\d+) of (\d+) dimensions at B or better", lambda m: f"{m[1]} de {m[2]} dimensiones en B o mejor"),
    (r"Last (\d+) days", lambda m: f"Últimos {m[1]} días"),
    (r"([+-]\d+) vs the month before \(within noise\)", lambda m: f"{m[1]} contra el mes anterior (dentro del ruido)"),
    (r"(\d+)/100 ±(\d+)", lambda m: f"{m[1]}/100 ±{m[2]}"),
    (r"How often two independent readers agreed \((\d+) conversations read twice\):",
     lambda m: f"Cuántas veces coincidieron dos lectores independientes ({m[1]} conversaciones leídas dos veces):"),
    (r"Read (\d+) of your (\d+) conversations end to end — picked at random, in proportion to each project and kind of work\.",
     lambda m: f"Se leyeron {m[1]} de tus {m[2]} conversaciones de principio a fin — elegidas al azar, en proporción a "
               f"cada proyecto y tipo de trabajo."),
    (r"\+(\d+) more projects", lambda m: f"+{m[1]} proyectos más"),
    (r"([+-]\d+) vs the month before", lambda m: f"{m[1]} contra el mes anterior"),
    (r"(strongest|weakest): (.+) (\d+)",
     lambda m: f"{'más fuerte' if m[1] == 'strongest' else 'más débil'}: {_tr_or_keep(m[2])} {m[3]}"),
    (r"([\d.]+)% of all your prompts", lambda m: f"{m[1]}% de todos tus prompts"),
    (r"busier than (\d+)% of your hours", lambda m: f"más activa que el {m[1]}% de tus horas"),
    (r"(\d+)% of the total", lambda m: f"{m[1]}% del total"),
    (r"([\d,]+) calls", lambda m: f"{m[1]} llamadas"),
    (r"(.+): (\d+) \((\d+)%\)", lambda m: f"{_tr_or_keep(m[1])}: {m[2]} ({m[3]}%)"),
    (r"Connector ([0-9a-f]{8})", lambda m: f"Conector {m[1]}"),
    (r"Your last (\d+) chats", lambda m: f"Tus últimos {m[1]} chats"),
    (r"(\d+) of these (\d+) chats were read in depth — the deep read covered your latest chats, one conversation each\.",
     lambda m: f"{m[1]} de estos {m[2]} chats se leyeron a fondo — la lectura cubrió tus últimos chats, una conversación por chat."),
    (r"(\d+) of these (\d+) chats were read in depth\. The last deep read did not pick your latest chats: it picked the roughest conversations, some that went well and a few recent ones\.",
     lambda m: f"{m[1]} de estos {m[2]} chats se leyeron a fondo. La última lectura no eligió tus últimos chats: eligió "
               "las conversaciones más difíciles, algunas que salieron bien y unas pocas recientes."),
    (r"week of (\S+): (\d+) prompts", lambda m: f"semana del {m[1]}: {m[2]} prompts"),
    (r"week of (\S+) · (.+): (\d+) prompts", lambda m: f"semana del {m[1]} · {_tr_or_keep(m[2])}: {m[3]} prompts"),
    (r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) (\d+h): (\d+) prompts",
     lambda m: f"{ES[m[1]]} {m[2]}: {m[3]} prompts"),
    (r"(.+): ([\d,]+) calls \((\d+)%\)", lambda m: f"{_tr_or_keep(m[1])}: {m[2]} llamadas ({m[3]}%)"),
    (r"Since (\S+) your score went from (\d+) to (\d+) \(([+-]\d+)\)\. What moved the most: (.+) \(([+-]\d+)\)\.",
     lambda m: f"Desde {m[1]} tu puntaje pasó de {m[2]} a {m[3]} ({m[4]}). Lo que más cambió: {_tr_or_keep(m[5])} ({m[6]})."),
    (r"Each session counts in the month it started, and each month is scored with the same rules as the whole report\. "
     r"A month needs at least (\d+) prompts to get a score; the current month is still in progress\.",
     lambda m: f"Cada sesión cuenta en el mes en que empezó, y cada mes se puntúa con las mismas reglas que todo el "
               f"reporte. Un mes necesita al menos {m[1]} prompts para tener puntaje; el mes actual todavía está en curso."),
    (r"(\d+h): (\d+) prompts", lambda m: f"{m[1]}: {m[2]} prompts"),
    (r"Similarity to you (\S+)", lambda m: f"Similitud contigo {m[1]}"),
    (r"friction: (.+)", lambda m: f"fricción: {_tr_or_keep(m[1])}"),
    (r"Every conversation read \((\d+)\)", lambda m: f"Todas las conversaciones leídas ({m[1]})"),
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
    # SVG ids must be unique: a url(#g) that points into the hidden copy paints nothing.
    body_es = body_es.replace('id="g"', 'id="g-es"').replace("url(#g)", "url(#g-es)")
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
    ap.add_argument("-o", "--out", default=DEFAULT_REPORT,
                    help="HTML output path (default ~/.claude/insight/ai_fluency_report.html, so a run "
                         "never drops files in whatever folder you happen to be in)")
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
    ap.add_argument("--deep", metavar="DIR",
                    help="deep read: cut your chats into conversations and write the ones to read "
                         "(bundle.json + batch-*.json) to DIR for the ai-fluency-deep workflow")
    ap.add_argument("--depth", choices=sorted(DEPTHS), default="normal",
                    help="which conversations the deep read covers: rapida (20 mixed), normal (50 mixed), "
                         "recientes (your latest 50 chats, one conversation each), completa (all)")
    ap.add_argument("--deep-analysis", metavar="PATH", dest="deep_analysis",
                    help="merge the ai-fluency-deep workflow's output; every quote is checked against "
                         "your transcripts and anything that doesn't match is dropped")
    ap.add_argument("--audit", metavar="DIR",
                    help="write a sample of every keyword detector's decisions (hits and misses) to "
                         "DIR/audit.json for labeling, then exit")
    ap.add_argument("--audit-score", metavar="PATH", dest="audit_score",
                    help="score a labeled audit.json against the current detectors and exit")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the terminal summary (the skill's internal measure pass uses this "
                         "so the score isn't surfaced before the full AI report is ready)")
    args = ap.parse_args(argv)

    if args.audit_score:
        with open(os.path.expanduser(args.audit_score), encoding="utf-8") as f:
            rows = audit_score(json.load(f))
        print(f"  {'detector':<12}{'precision':>11}{'miss rate':>11}   hits/misses labeled")
        for name in AUDIT_DETECTORS:
            r = rows.get(name)
            if not r:
                continue
            pr = "—" if r["precision"] is None else f"{100 * r['precision']:.0f}%"
            mr = "—" if r["miss_rate"] is None else f"{100 * r['miss_rate']:.0f}%"
            print(f"  {name:<12}{pr:>11}{mr:>11}   {r['hits']}/{r['misses']}"
                  + (f"   ({r['filtered']} not yours, now filtered)" if r.get("filtered") else ""))
        return 0

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

    # One pass over the transcripts, then the scoring (whole + month by month) and the render.
    progress = Progress(len(files) + 4, enabled=not (args.quiet or args.json), spanish=_spanish_locale())
    progress.stage("Reading your chats", "Leyendo tus chats")
    corpus = parse(files, progress.tick)
    if not corpus.real_prompts:
        print("Found transcripts but no real human-typed prompts to analyze.", file=sys.stderr)
        return 1

    progress.stage("Scoring the seven dimensions", "Calculando las siete dimensiones")
    if args.audit:
        progress.finish()
        out_dir = os.path.abspath(os.path.expanduser(args.audit))
        os.makedirs(out_dir, exist_ok=True)
        items = audit_sample(corpus)
        with open(os.path.join(out_dir, "audit.json"), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        print(f"  Audit sample: {len(items)} decisions -> {os.path.join(out_dir, 'audit.json')}")
        return 0

    result = analyze(corpus)
    cards, strength = build_action_plan(corpus, result)
    progress.tick()

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

    if args.deep:
        info = write_deep_bundle(corpus, result, args.deep, args.depth)
        if not args.quiet:
            print(f"  Deep read: {info['episodes_read']} of {info['episodes_total']} conversations in "
                  f"{len(info['batches'])} batches -> {os.path.join(os.path.abspath(os.path.expanduser(args.deep)), 'bundle.json')}",
                  file=sys.stderr)

    deep = None
    # The last deep read is reused on every run: its quotes are re-checked against today's
    # transcripts, so the section stays honest without paying for a new read each time.
    deep_path = args.deep_analysis
    if not deep_path and not args.path and os.path.exists(os.path.expanduser(DEFAULT_DEEP_ANALYSIS)):
        deep_path = DEFAULT_DEEP_ANALYSIS
    if args.deep and not args.quiet:
        print("  The AI reading itself runs inside Claude Code: ask it to run /ai-fluency in depth.",
              file=sys.stderr)
    if deep_path:
        try:
            with open(os.path.expanduser(deep_path), encoding="utf-8") as f:
                deep = verify_deep_analysis(json.load(f), corpus)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Could not read --deep-analysis {deep_path}: {e}", file=sys.stderr)
            return 1
        deep["case_reads"] = _archived_case_reads(deep, os.path.dirname(os.path.expanduser(deep_path)), corpus)
        lost = sum(deep["dropped"].values())
        if not args.quiet:
            progress.note(f"  Deep read merged: {len(deep['reads'])} conversations, {len(deep['patterns'])} habits, "
                          f"{len(deep['rules'])} CLAUDE.md rules" + (f" ({lost} ungrounded items dropped)" if lost else ""))

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
        progress.finish()
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
        "mcp_server_tools": corpus.mcp_server_tools,
        "commands": corpus.commands,
        "work_types": classify_work_types(corpus),
    }
    adoption = build_adoption(corpus)

    # Render fully before touching the file, so a render error can't leave a 0-byte report.
    progress.stage("Scoring month by month", "Puntaje mes a mes")
    history = monthly_history(corpus)
    progress.tick()
    progress.stage("Drawing the report", "Armando el reporte")
    progress.stage("Measuring the margin of error", "Calculando el margen de error")
    margin = score_margin(corpus)
    recent = recent_score(corpus)
    # The hand-labeled holdout from --audit, re-scored with today's detectors (only on a default run).
    audit = None
    if not args.path and os.path.exists(os.path.expanduser(DEFAULT_AUDIT)):
        try:
            with open(os.path.expanduser(DEFAULT_AUDIT), encoding="utf-8") as f:
                audit = audit_score(json.load(f))
        except (OSError, ValueError, KeyError, TypeError) as e:
            progress.note(f"  Note: could not read the detector audit ({e}); leaving it out.")
    html_doc = build_html(corpus, result, cards, strength, archive_info, analysis, analysis_note,
                          usage=usage, adoption=adoption, deep=deep, history=history, margin=margin, recent=recent,
                          audit=audit)
    html_doc = bilingual_html(html_doc)
    out_path = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    # Content-only sibling the /ai-fluency skill publishes via the Artifact tool.
    art_path = os.path.splitext(out_path)[0] + ".artifact.html"
    with open(art_path, "w", encoding="utf-8") as f:
        f.write(_artifact_html(html_doc))
    progress.tick(2)
    progress.finish()

    if not args.quiet:
        print(terminal_summary(corpus, result))
        if archive_info and archive_info["enabled"]:
            print(f"  Archive: {archive_info['merged_sessions']} sessions preserved at "
                  f"{archive_info['dir']} ({archive_info['new']} new, {archive_info['updated']} updated this run).")
        print(f"  Report: {out_path}\n")
    if not args.no_open:
        try:
            webbrowser.open(Path(out_path).as_uri())   # a proper file:/// URI on Windows too
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
