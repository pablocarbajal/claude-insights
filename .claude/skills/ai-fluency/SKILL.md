---
name: ai-fluency
description: Analyze how the developer collaborates with Claude Code and produce an "AI fluency" skill map — overall score, archetype, the four AI-fluency competencies (Delegation, Description, Discernment, Diligence), the five measured dimensions, and clear what/where/how direction. Use when the user asks to analyze their Claude Code usage, AI fluency, builder profile, prompting style, or "how do I use Claude / AI", or runs /ai-fluency.
argument-hint: "[PATH | --no-open]"
allowed-tools: Bash(python3 *), Read, Write, Workflow
---

# AI Fluency Analysis — one command, full run

You produce a reliable AI-fluency **skill map** for this developer from their real
Claude Code transcripts. One run, three parts:

1. **Measure (deterministic).** `insight.py` parses transcripts, de-contaminates and
   scrubs them, and computes the numbers — rate-based, confidence-hedged, archive-backed
   so it sees **more than Claude Code's 30-day window**.
2. **Explore (Sonnet 4.6).** Parallel explorers read the evidence, one per AI-fluency competency.
3. **Analyze (Opus 4.8).** A senior assessor writes the skill map, **grounded in the bundled
   AI Fluency framework**, then verifies it is evidence-grounded.

The skill is self-contained: the engine and the framework are bundled next to this file at
`~/.claude/skills/ai-fluency/`, and all working files land in `~/.claude/insight/`.

## Step 1 — Measure + emit evidence

These working files live at fixed, reused paths, so first delete any leftovers from a
previous run (or a different person on a shared machine) — a stale `analysis.json` must
never survive into this run and get merged as if it were this user's:

```bash
rm -f ~/.claude/insight/evidence.json ~/.claude/insight/analysis.json
```

Then measure (use `--quiet` so the score is NOT surfaced yet — this is one run that should
end in a single finished report, not a score now and a report later):

```bash
python3 ~/.claude/skills/ai-fluency/insight.py --evidence ~/.claude/insight/evidence.json --no-open --quiet -o ~/.claude/insight/ai_fluency_report.html $ARGUMENTS
```

This computes the de-contaminated evidence bundle and writes a fallback deterministic
report. **Do not report the score, archetype, or any result to the user yet** — keep going
to Steps 2–3 and only present the final, AI-personalized report. If it reports no
transcripts, tell the user to pass their transcript directory as `$ARGUMENTS` (default
`~/.claude/projects`). The evidence bundle carries a `meta.run_fingerprint` that binds any
analysis built from it back to this exact run.

## Step 2 — Run the two-model analysis workflow

Print the absolute paths the workflow needs (it reads them with its own Read tool):

```bash
python3 -c "import os; print(os.path.expanduser('~/.claude/insight/evidence.json')); print(os.path.expanduser('~/.claude/skills/ai-fluency/reference/ai-fluency-framework.md'))"
```

Then call the **Workflow** tool with:
- `name`: `ai-fluency`
- `args`: `{ "evidence": "<first line above>", "framework": "<second line above>" }`

The workflow returns the analysis as a JSON object (overall_read, skill_map of the four
competencies, top_growth, strengths). **Sonnet 4.6** explores, **Opus 4.8** analyzes +
verifies — model selection is baked into the workflow.

## Step 2b — Deep read of the conversations (when the user asks for depth)

Run this when the user asks to analyze their chats in depth ("a fondo", "en profundidad", "deep"),
or passes `--depth`. It reads whole conversations — the user's brief, what Claude did, how they
answered, how it ended — instead of isolated prompts.

1. Cut the chats into conversations and write the ones to read (`rapida` = 20, `normal` = 50,
   `completa` = all; default `normal`):

   ```bash
   python3 ~/.claude/skills/ai-fluency/insight.py --deep ~/.claude/insight/deep --depth normal --no-open --quiet -o ~/.claude/insight/ai_fluency_report.html $ARGUMENTS
   ```

2. Read `~/.claude/insight/deep/bundle.json` and call the **Workflow** tool with
   `name`: `ai-fluency-deep` and `args`: `{ "batches": <bundle.batches>, "lang": <bundle.lang>,
   "depth": <bundle.depth>, "run_fingerprint": <bundle.run_fingerprint> }` — pass the list as a
   real JSON array.
3. Write the returned JSON to `~/.claude/insight/deep/analysis.json` and add
   `--deep-analysis ~/.claude/insight/deep/analysis.json` to the Step 3 command. The engine checks
   every quote against the real transcripts and drops what doesn't match, so the report can't put
   invented words in the user's mouth.

## Step 3 — Render the final report

Only do this if Step 2 actually returned an analysis. Write the workflow's returned JSON to
`~/.claude/insight/analysis.json` (absolute path; the directory exists from Step 1), then
merge it — passing the evidence bundle it was built from so the engine can confirm the
analysis belongs to this exact run:

```bash
python3 ~/.claude/skills/ai-fluency/insight.py --analysis ~/.claude/insight/analysis.json --analysis-evidence ~/.claude/insight/evidence.json -o ~/.claude/insight/ai_fluency_report.html $ARGUMENTS
```

This Step-3 run is the FIRST time the score is printed (Step 1 was `--quiet`), so the user
sees one finished, AI-personalized report — not a score up front and a report later. The
engine fingerprints this run's data and compares it to the evidence bundle's
`run_fingerprint`; if they don't match (a stale or foreign analysis), it prints a note and
renders the deterministic report instead — so one run's verdict can never leak into another.
On success the report carries Opus's tailored, framework-grounded skill map AND your
highest-leverage growth moves (each rewriting one of your real prompts) on top of the
deterministic numbers. Point the user to `~/.claude/insight/ai_fluency_report.html`.

## Step 3b — Always publish the report as an Artifact

Every render also writes a content-only sibling at
`~/.claude/insight/ai_fluency_report.artifact.html` (the engine strips the
`<!doctype>/<head>/<body>` wrapper the Artifact tool adds itself). Publish it so the user
gets a shareable, cache-proof hosted report instead of only a local `file://` link — call
the **Artifact** tool with:
- `file_path`: `~/.claude/insight/ai_fluency_report.artifact.html`
- `favicon`: `📊`

The Artifact is **default-private** on the user's claude.ai account, but it does upload the
report (which contains their scrubbed prompt excerpts and tool usage) there — so this only
runs as part of the report they explicitly asked for. If the Artifact tool isn't available
in this environment, skip this step and point the user to the local file instead.

## Step 4 — Narrate (don't re-derive)

Only now, after the final report exists, give a short, encouraging read in chat: the
**overall score + band + archetype** in one sentence, the **single highest-leverage growth
move** grounded in one of their real prompts, and their **strongest competency** as the
foundation. Keep it to a paragraph or two; the report has the depth. Give them the Artifact
URL from Step 3b as the primary link (with the local file path as a fallback).

## Fallbacks

- **No Workflow capability available?** The deterministic report from Step 1 is complete on
  its own — skip steps 2–3. Since Step 1 ran `--quiet`, read the numbers from
  `~/.claude/insight/evidence.json` (or re-run Step 1 without `--quiet`) to narrate, then
  open the report. It will say plainly that the AI skill-map stage didn't run.
- **Explicit path given?** Pass it as `$ARGUMENTS` in steps 1 and 3 (archiving is skipped
  for explicit paths by design).

## Notes

- Original transcripts are never modified. They're copied into an archive
  (`~/.claude/insight-archive`) so history outlives Claude Code's 30-day cleanup.
- Scores measure observable behavior, not intent; thin signals are flagged "low data" and
  hedged — don't over-claim on those.
