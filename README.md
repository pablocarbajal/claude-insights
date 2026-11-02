# Claude Insight

See how you *actually* build with AI. Claude Insight reads your local Claude Code
transcripts and turns them into one self-contained HTML report: a fluency score, your
builder archetype, a 4-competency skill map, and the few highest-leverage things to change
next — with the "before/after" rewrites drawn from your own prompts.

It all runs on your machine. Your transcripts never leave it, and the originals are never
touched.

## ⚡ Install in 5 seconds

macOS, Linux or Git Bash on Windows:

```bash
curl -fsSL https://raw.githubusercontent.com/pablocarbajal/claude-insights/main/install.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/pablocarbajal/claude-insights/main/install.ps1 | iex
```

That one command installs the **`/ai-fluency`** skill into Claude Code **and generates your
first report**, which opens in your browser. It finds your chats on its own — in `~/.claude`,
in the folder set by `CLAUDE_CONFIG_DIR`, or on the other side of WSL — and if there are
none it says where it looked and what to do. For the AI-written read, open Claude Code (any
folder) and run:

```text
/ai-fluency
```

The report lands at `~/.claude/insight/ai_fluency_report.html`. Requires
Claude Code and Python 3.8+ — **if Python is missing, the installer offers to install it**
(winget on Windows, Homebrew on macOS, apt/dnf/pacman on Linux) and only does so after you
say yes. Options, as environment variables:

| Variable | What it does |
|---|---|
| `CLAUDE_INSIGHT_REPO=owner/name` | install from a fork instead of `pablocarbajal/claude-insights` |
| `CLAUDE_INSIGHT_REF=branch-or-tag` | install that branch or tag instead of the latest release |
| `CLAUDE_INSIGHT_INSTALL_PYTHON=1` | install Python without asking (for scripts and CI) |
| `CLAUDE_INSIGHT_NO_RUN=1` | only install; don't generate the report at the end |

```bash
CLAUDE_INSIGHT_REPO=you/claude-insights CLAUDE_INSIGHT_REF=main \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/you/claude-insights/main/install.sh)"
```

## 🚀 What you get

A single self-contained HTML report, **structured score-first** in five groups that read
top-to-bottom: **the verdict** (score + band + archetype) → **why** (assessment + skill map
+ five dimensions) → **where you stand** (strengths + L1–L5 map) → **what to do next**
(growth levers) → **trust & method** (the data, your tool adoption, the honest numbers). A
provenance meta line near the top records **when it was evaluated**, the **activity window**
(`first → last (N days)`), and the **Claude Code version** that produced your transcripts.

- **A fluency score (0–100)** with a band — Operator → Developing → Proficient → Advanced → Expert — and what it means.
- **Your builder archetype** — Autonomous Agent, Architect, Debugger, Collaborator, or Sprinter — picked from *your* behavior, not from keywords.
- **A 4-competency skill map** — **Delegation · Description · Discernment · Diligence** (the AI Fluency framework) — each placed on a 1–5 level with one concrete next move.
- **Five measured dimensions** behind the map — Briefing, Verification, Context-setting, Iteration, Toolcraft — each a defensible rate, not a vanity count.
- **What / Where / How** — your top growth levers, each tied to real moments in your transcripts and (when you run the full skill) a rewrite of one of *your own* prompts.
- **Usage & Crabi-tool adoption** — how your tool time splits across work types (Build · Debug · Plan · Investigate · Shell/Ops · Delegate · Other), your most-used MCP servers and slash commands, and a **two-section plugin adoption** view: *Crabi suggested plugins* (engram, obsidian, ponytail, desplega, superpowers, context7) and the *Crabi AI marketplace* (every catalog plugin) — each tagged not-installed / installed-idle / installed-used, with an **outdated** marker when your installed version trails the latest in your local marketplace clone.
- **Honest data accounting** — how many real prompts you typed, projects, MB, and hands-on time — across **more than the 30 days Claude Code keeps on disk** (see below).

## 🎯 How the score works (and what it won't do)

The whole point is to measure *skill*, not activity — so a few things are deliberate:

- **Everything is a rate, then saturated.** Each dimension is a per-prompt or
  per-opportunity rate run through `min(1, rate / target)`. Doing *more* of the same
  thing doesn't move the number — only doing it *better* does.
- **Thin data is hedged, not faked.** When you have little history, each dimension is
  pulled toward a neutral 50 in proportion to how few opportunities it had. So your first
  runs read conservatively and then **firm up over your first few dozen prompts as the tool
  gets confident, and settle** (each dimension reaches full confidence at its own count —
  e.g. ~60 briefing-prompts, ~15 edit-bursts). If an early score creeps up run-over-run,
  that's the hedge lifting toward your real level — it plateaus once there's enough data,
  and from then on only changing your habits moves it. Thin dimensions are flagged **low data**.
- **The score rates the *collaboration*; the archetype rates *you*.** The fluency score
  is the quality of you-and-Claude together — and that includes habits Claude often does
  on its own, like reading a file before editing (Context-setting) or running tests
  (Verification), which are ~44% of the weight. Your **archetype** is built from a
  separate, *agency-weighted* vector that discounts those Claude-driven habits, so it
  reflects how *you* drive. The two can differ on purpose: a thorough agent lifts the
  collaboration score more than it lifts the archetype.
- **Noise is stripped before anything is scored.** Tool results, subagent (sidechain)
  turns, slash-command stubs, injected system text, and pasted walls of text (over ~6k
  chars) don't count as your prompts. Idle gaps longer than 5 minutes are excluded from
  "active time," so it's hands-on time, not wall-clock.

Both the raw and the confidence-adjusted scores are shown in the report.

## 🧠 One command, the full analysis

`/ai-fluency` runs the complete pass as **one pass that ends in a single finished report**
— it won't flash a score first and a report later:

1. **Measure** — `insight.py` de-contaminates and scrubs your transcripts and computes
   every number (rate-based, confidence-hedged, archive-backed so it sees more than the
   30-day window). This step is silent on purpose.
2. **Explore — Claude Sonnet 4.6** — four explorers run in parallel, one per AI-fluency
   competency, reading only your de-contaminated evidence.
3. **Analyze — Claude Opus 4.8** — a senior assessor writes the skill map and your growth
   levers grounded in [`reference/ai-fluency-framework.md`](reference/ai-fluency-framework.md),
   then a verifier checks every claim against your evidence and repairs anything ungrounded.

The numbers are always computed deterministically; the models add judgement and direction
on top and never change the math. It runs on your existing Claude Code session — **no
separate API key** — and the models are pinned per stage in
[`.claude/workflows/ai-fluency.js`](.claude/workflows/ai-fluency.js).

Two guarantees worth calling out:

- **Your "how to grow" cards are written from your real prompts** — the "before" is
  something you actually typed and the "after" is Opus's tailored rewrite of it, not a
  stock example.
- **An analysis can't leak across runs or people.** The evidence bundle carries a
  fingerprint of the exact run it came from; the report engine refuses to merge an
  analysis whose fingerprint doesn't match, and falls back to the deterministic report
  (and says so) instead.

If the Workflow capability isn't available, the skill still produces the complete
deterministic report — scores, archetype, dimensions, and skill levels — with generic,
clearly-labeled growth examples instead of the Opus-written ones.

### Data export & flags

```bash
python3 insight.py --json                                          # metrics + data breakdown as JSON
python3 insight.py --evidence ev.json                              # write the de-contaminated evidence bundle (carries a run fingerprint)
python3 insight.py --analysis an.json --analysis-evidence ev.json  # merge an Opus analysis, bound to this run
python3 insight.py /path/to/transcripts                            # analyze a specific directory or .jsonl file
python3 insight.py --quiet                                         # suppress the terminal summary (the skill's measure step uses this)
python3 insight.py --no-open                                       # don't auto-open the browser
python3 insight.py --archive ~/my-archive                          # keep history in a PRIVATE, per-person durable folder
python3 insight.py --no-archive                                    # analyze without copying anything new
```

## ⏳ Analyzing more than 30 days

By default Claude Code **deletes transcripts older than its `cleanupPeriodDays` setting
(default `30`)**, so only ~30 days of history is ever on disk — that's a limit of the
*data*, not of this tool, which reads everything available. Two things let it see more:

1. **Stop the deletion.** Add this to `~/.claude/settings.json` so Claude Code keeps a
   full year (set it before more history ages out):
   ```json
   { "cleanupPeriodDays": 365 }
   ```
2. **The built-in archive (automatic).** On every default run, Claude Insight copies your
   transcripts into a persistent archive (`~/.claude/insight-archive` by default) *before*
   the cleanup can remove them, then analyzes **live + archive**, de-duplicated. From your
   first run on, your history accumulates indefinitely. It only ever grows files, copies
   atomically, and stays 100% on your machine.

   Keep the archive **private to you**. A single archive folder shared between people
   (e.g. a synced team Dropbox) would merge everyone's transcripts into one report — so
   point `--archive` at your own location, not a shared one:
   ```bash
   python3 insight.py --archive ~/Dropbox/claude-archive   # your own, private folder — survives reinstalls
   # or set CLAUDE_INSIGHT_ARCHIVE in your shell. Use --no-archive to skip a run.
   ```

## 📦 Run from source (no install)

`insight.py` is a single, pure-standard-library file — clone and run it, nothing to
`pip install`:

```bash
git clone https://github.com/pablocarbajal/claude-insights.git
cd claude-insights
python3 insight.py                 # analyze ~/.claude/projects, then write + open the report
```

On Windows use `python insight.py` (or `py -3 insight.py`): a fresh Windows ships a
`python3` alias that only opens the Microsoft Store. If `python --version` says *Python was
not found*, install Python with `winget install --id Python.Python.3.12 -e` (or run
`install.ps1`, which does it for you) and open a new terminal.

> Requires Python 3.8+ and nothing else — no `pip install`, no virtualenv. On its own,
> `insight.py` produces the complete deterministic report (scores, archetype, dimensions,
> month-by-month evolution, margin of error, growth levers with generic examples). The
> AI-written parts come from running `/ai-fluency` inside Claude Code. A run over a few GB
> of transcripts takes about ten seconds; the transcripts are read once.

## 🪟 Build and run on Windows, step by step

There is nothing to compile: `insight.py` is plain Python and runs as it is. "Building" the
project on Windows means getting Python, getting the code, checking it with the tests, and
running it. In **PowerShell**:

1. **Install Python 3.8+** (skip if `python --version` already prints a version):

   ```powershell
   winget install --id Python.Python.3.12 -e --source winget
   ```

   Then **close and reopen PowerShell** so it sees the new `python`. Without winget, download
   it from <https://www.python.org/downloads/> and tick *Add python.exe to PATH*.
   Use `python` (or `py -3`), not `python3`: on Windows `python3` is a shortcut that only
   opens the Microsoft Store.

2. **Get the code** (with Git, or download the ZIP from GitHub and unzip it):

   ```powershell
   git clone https://github.com/pablocarbajal/claude-insights.git
   ```

3. **Check that it works** — the test suite uses only the standard library:

   ```powershell
   python -m unittest discover -s claude-insights\tests
   ```

   Every test should end in `OK`.

4. **Generate your report**:

   ```powershell
   python claude-insights\insight.py
   ```

   It reads `%USERPROFILE%\.claude\projects`, writes
   `%USERPROFILE%\.claude\insight\ai_fluency_report.html` and opens it in your browser.
   Add `--no-open` to skip opening it, or `--no-archive` to read your chats without copying
   them into the archive.

5. **Optional — the `/ai-fluency` skill in Claude Code**: from the cloned folder run
   `powershell -ExecutionPolicy Bypass -File claude-insights\install.ps1`, or the one-line
   `irm … | iex` from the top of this page.

If the console shows strange characters instead of accents or emoji, run
`$env:PYTHONIOENCODING = "utf-8"` first; the report itself is always UTF-8.

## 📊 Example output

```
  AI Fluency Score: 78/100  (Advanced)
  Archetype: 🤖 Autonomous Agent
  Based on 156 real prompts across 16 projects, 156 sessions (53.8 MB).
  Archive: 156 sessions preserved at ~/.claude/insight-archive (0 new, 1 updated this run).
  Report: ai_fluency_report.html
```

*(Illustrative — your numbers will differ.)* The HTML report opens score-first and flows
through five groups: a headline score ring + band + archetype (with the evaluated date,
activity window, and Claude Code version on a meta line just below the title), then the
assessment and four-competency skill map (your level and next move for each) and the five
dimensions, then where you stand (strengths + L1–L5 map + archetype affinity), then your
top growth levers with before/after rewrites, and finally a collapsible "trust & method"
group holding the data accounting, the two-section Usage & Crabi-tool adoption view, and
the honest numbers.

## 🏗️ Architecture

```
insight.py                       # the whole engine: parse → de-contaminate → score → report
                                 #   (pure stdlib, zero install; --evidence / --analysis hooks)
reference/
└── ai-fluency-framework.md      # the 4D framework the Opus analysis stage is grounded in
.claude/
├── skills/ai-fluency/SKILL.md   # /ai-fluency — orchestrates the one-command pipeline
└── workflows/ai-fluency.js      # Sonnet 4.6 explore → Opus 4.8 analyze → verify
tests/                           # stdlib unittest (de-contamination, scoring, archive,
                                 #   analysis-provenance, personalized growth)
```

## 📈 What's measured

### The four AI-fluency competencies (the skill map)
Adapted from Anthropic's *AI Fluency: Frameworks & Foundations* (the 4 Ds):

1. **Delegation** — deciding what to hand to the agent, and how to split the work.
2. **Description** — telling the agent what you want (goal + constraint + acceptance test).
3. **Discernment** — evaluating what comes back (verify, ground edits, correct precisely).
4. **Diligence** — being responsible: verify before it ships, tear down, own the result.

### The five dimensions behind the map (with weights)
1. **Briefing / Direction** *(24%)* — how concretely you frame requests (constraint / artifact / intent rates).
2. **Verification** *(22%)* — running tests / build / app after a burst of edits.
3. **Context-setting** *(22%)* — grounding edits in a prior read, instead of blind edits.
4. **Iteration** *(18%)* — correcting precisely instead of vague rejection.
5. **Toolcraft** *(14%)* — reaching for a healthy range of tools, not forcing everything through one.

(Verification and Context-setting are largely habits Claude drives on its own — counted in
the collaboration score, discounted in the archetype.)

### Usage & Crabi-tool adoption
Beyond the score, the report shows how you *spend* Claude Code (additive — none of it
changes the scoring):
- **Work-type mix** — every tool action bucketed into Build, Debug, Plan, Investigate
  (reads + observability queries), Shell/Ops, Delegate (sub-agent / workflow spawns), or
  Other. Prompts are excluded — this measures actions, not chat.
- **MCP & slash-command usage** — your most-called MCP servers (grouped, plugin infix
  stripped) and top slash commands over the measured window.
- **Crabi plugin adoption** — two sub-sections, read live and offline from your local
  plugin config (nothing leaves your machine):
  - **Crabi suggested plugins** — the tools we recommend leaning on (engram, obsidian,
    ponytail, desplega, superpowers, context7) as aggregate rows: installed / enabled / used,
    flagging any that are installed but never reached for (and superpowers as not-installed
    until you add it). Context7 is tracked as an MCP server rather than a plugin: *installed*
    reads your `~/.claude.json` / project MCP config, *used* counts both direct and claude.ai
    connector calls, and it carries no version tracking.
  - **Crabi AI marketplace** — *every* plugin in the catalog (not just the ones you have),
    each tagged **not installed** / **installed · idle** / **installed · used N×**, with
    usage counted across slash-command, MCP, and sub-agent channels.
  - Either list also shows an **outdated** marker (`update available · vX → vY`) when your
    installed version trails the latest the catalog offers. "Latest" is your **local
    marketplace clone's** version — only as fresh as your last marketplace sync — so it's an
    offline, honest comparison, not a live upstream check. When versions can't be compared
    (missing or `unknown`), no version claim is made.

### Archetypes (from *your* behavior, not keywords)
- **🤖 Autonomous Agent** — delegates whole, end-to-end jobs and trusts the agent to run them.
- **🏗️ Architect** — plans and explores before building; reads and designs first.
- **🐛 Debugger** — methodical: read to diagnose, change, verify, repeat.
- **🤝 Collaborator** — works with the agent like a teammate: asks for options, gives feedback.
- **⚡ Sprinter** — fast and direct, terse prompts, low ceremony; verification is the growth edge.

The archetype is the nearest match to your **agency-weighted** behavior vector — it counts
what *you* do (briefing, correcting, tool choice, delegation) and discounts the
read-before-edit and run-the-tests habits Claude does on its own, so it reflects you, not
the agent. Near-ties are reported as a blend, never a coin-flip.

## 🔒 Privacy

Everything is local. Transcripts are read from `~/.claude/projects` (and your archive),
analyzed on your machine, and written only to the report path you choose (the `/ai-fluency`
skill keeps everything under `~/.claude/insight/`). Nothing is uploaded; there's no API key
and no telemetry. The Sonnet/Opus stages run inside your own Claude Code session. Your
original transcripts are never modified, and the working files that hold your real prompts
(`evidence.json`, `analysis.json`, the report) are git-ignored so they can't be committed.

## 🔧 Development

```bash
# Run the test suite — standard library only, no pytest required (python3 → python on Windows)
python3 -m unittest discover -s tests
```

CI runs the same suite on Ubuntu, Windows and macOS (Python 3.8 to 3.12) on every push.

## 📄 License

MIT License — see [LICENSE](LICENSE).

Based on [arodriguezcb/claude-insights](https://github.com/arodriguezcb/claude-insights) and
the original Claude Insight by Felipe Guarín, both MIT; this copy adds the Spanish report, the
month-by-month evolution, the margin of error, the two-reader deep read and the Windows installer.
