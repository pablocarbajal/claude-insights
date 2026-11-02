# Changelog

All notable changes to Claude Insight are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/), and the
project aims for [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **A more useful Usage tab** — "Your rhythm" tiles (days worked, longest streak, busiest
  weekday, peak hour, prompts on a typical day, share after midnight); prompts per week
  stacked by your top five projects; a day × hour heat map; project bars in the same colors;
  and "What Claude did for you", a mix of tool calls by type (terminal, editing, reading,
  browser, planning) with the top tools colored by type. Project names drop container
  folders like Dev / Projects. Colors are a fixed, colorblind-checked categorical order.
- **Your evolution, month by month** — the Summary tab scores every calendar month on its own
  (each session counts in the month it started, same rules as the whole report) and shows a
  0–100 column chart, the change against the month before and every dimension per month.
  Months with fewer than 30 prompts aren't scored, and the current month is marked "so far"
  and left out of the headline comparison. Recomputed from the transcripts + archive, so the
  history is there from the first run with no saved runs.
- **Deep read of your conversations** — `--deep DIR --depth rapida|normal|completa` cuts every
  session into episodes (your brief → what Claude did → your replies → how it ended), picks the
  roughest, some smooth and the most recent, and writes them as batches. The new
  `ai-fluency-deep` workflow reads each one end to end (Sonnet) and finds recurring habits and
  CLAUDE.md rules (Opus). `--deep-analysis PATH` merges the result into a "Your chats in depth"
  section: who caused the friction (you, Claude or the environment), "you said → better" per
  conversation, habits with real examples, and rules ready to paste. Every quote is checked word
  for word against the transcripts; anything that doesn't match is dropped and counted.
- **Stricter scoring, seven dimensions.** New weights: Briefing 20, Verification 20, Iteration 15,
  Planning 15, Closure 10, Toolcraft 10, Context 10 — what only you can do weighs more than
  Claude's own habits.
  - **Verification** now gives every burst of code changes one verdict: checked by Claude (tests,
    build, lint, a script run, or looking at the app in a browser), by you (your next message
    reports what you saw, names what broke, or asks for a check), both, or nobody. Half is
    Claude's checks, half is yours; an unchecked change you approve with "ok" counts against.
    Doc-only edits (.md, .txt) are left out; re-reading the file no longer counts as a check.
  - **Planning** (new): in sessions with real work, did you ask for a plan, options or a
    structure (or use plan mode) before the first change; was there a conversation first; how
    often you ask why — vision → structure → requirements.
  - **Closure** (new): in projects that use git, how many working sessions end in a commit, who
    asked for it, and how many sessions end on an unresolved complaint.
- The deep read also judges each chat for vision, structure before code, precise requirements,
  whether you caught an error Claude missed, and whether it ended in a commit.
- `--depth recientes` reads your latest 50 chats (one conversation each); the Your chats tab
  says how many of them were read in depth and why.
- **Tabs** — the report is split into Summary, Dimensions, What to improve, Your chats, Usage and
  How we evaluate; the last tab opened is remembered.
- **Your chats tab** lists your latest 50 chats (name, project, prompts, Claude actions, the
  command to reopen them) and jumps to the deep read of any chat that has one.
- **Usage tab** with charts: prompts per week, what time you work, projects where you write the
  most, and the tools Claude used the most (inline SVG, values on hover).
- **How we evaluate tab** — the two layers (numbers vs. AI reading), what each dimension measures
  and weighs, the levels, and every archetype with its description and typical profile, with
  your own value marked on each signal.
- Desktop chats started without a folder show as "No folder" instead of a long internal path.
- **Your own chats instead of generic examples** — with a deep read, each "what to improve" card
  shows up to 3 of your real conversations for that habit: the chat's name (your rename, else
  Claude's title), project, date, `claude --resume <id>` to reopen it, what you wanted, your
  message and its rewrite. The generic pairs only appear when there's no real example. The
  workflow now tags which habit each rewrite trains.
- The report opens in the browser by default, is always written to
  `~/.claude/insight/ai_fluency_report.html`, and reuses the last deep read (re-checked).
- Corrections like "no funciona" no longer count as specific just because they mention the result.
- **EN/ES language switch** — the HTML report ships in English and Spanish in one file, with a
  toggle in the top-right corner. It opens in Spanish when the browser is set to Spanish and
  remembers the choice. Only report copy is translated (exact phrases and anchored patterns);
  your own prompts, project names and the AI-written skill map are left as they are.
- **Spanish prompt support** — every Briefing and Iteration cue (constraint, intent, action
  verb, correction, praise, "run it / verify") now matches Spanish too, accent-tolerant and
  with attached pronouns ("corrígelo"). A bare Spanish "no" only counts as a rejection at the
  start of a prompt, so ordinary negation stops inflating the correction count.
- **Human review of the result** — your first reply after the agent edits is classified as
  *checked* ("funciona", "no se ve el icono"), *specific fix*, *vague reject* or *rubber
  stamp* (bare "ok, sigue"). It now makes up 30% of Verification, so the score stops crediting
  you only for builds the agent runs on its own; the dimension line shows both halves.
- Briefing leaves out go-aheads ("sí, hazlo", "sigue, página 26"): being brief while
  answering the agent's own proposal is correct, not a weak brief.
- Verification recognizes more build/test commands (dotnet, msbuild, cmake, flutter,
  gradle, `pnpm -C <dir> build`).

### Fixed
- Pasted material (`<pasted_content>`) and plugin-sent messages are no longer scored as
  prompts the user typed.
- Windows: project labels drop the `C--` drive prefix; the terminal summary no longer
  crashes on the archetype emoji under cp1252; skill tests read files as UTF-8.
- MCP-server bars are now sized relative to the top server (they were all 100%).
- Work-type mix shows `<1%` instead of a misleading `0%` for small but real shares.
- Toolcraft level 4 no longer advises "reach past the shell" to someone already using
  100+ tools.
- Adoption: track Context7 (MCP server) as a Crabi-suggested tool.
- **Report provenance fields** — the report now records, on a meta line under the title
  and in the footer, the **evaluated date**, the **activity window** (`first → last
  (N days)`), and the **Claude Code version** (read off the latest transcript event that
  carries one). All deterministic; the activity-window segment is omitted for a
  timestamp-less corpus rather than crashing.
- **Two-section plugin adoption** — the adoption panel now splits into *Crabi suggested
  plugins* (engram, obsidian, ponytail, desplega, **superpowers** as a new suggested tool,
  shown not-installed until added) and *Crabi AI marketplace*, which lists **every plugin
  in the catalog** — including ones you don't have — each tagged not-installed /
  installed-idle / installed-used.
- **Per-plugin outdated detection** — an `update available · vX → vY` marker when an
  installed plugin's version trails the latest in your **local marketplace clone**
  (offline, never a network call). Versions that can't be compared (missing / `unknown` /
  unparseable) render no claim rather than a false up-to-date/outdated one.
- **Usage & Crabi-tool adoption** report section — entirely additive after `parse()`
  (the scored corpus, fluency score, archetype, and skill map are unchanged):
  - a 7-way **work-type mix** (Build / Debug / Plan / Investigate / Shell-Ops /
    Delegate / Other) over tool actions, with prompts excluded;
  - most-used **MCP servers** and **slash commands** over the measured window;
  - **Crabi marketplace adoption** — per-plugin installed / enabled / used, with usage
    counted across slash-command, MCP, and sub-agent channels and a flag for
    installed-but-never-used tools. Read live and offline from local plugin config.
- `behavior.usage` / `behavior.adoption` in the evidence bundle (outside the run
  fingerprint), plus an `/ai-fluency` coaching nudge that turns an installed-but-unused
  tool into a concrete growth card.
- Continuous integration: the test suite runs on every push to `main` and every
  pull request, across Python 3.8 / 3.10 / 3.12. Nothing merges red.
- `LICENSE` file (MIT) — the README already declared MIT; this makes it real.
- This changelog.

### Changed
- **Report regrouped into five score-first groups** — the HTML report now reads
  verdict → why → where you stand → what to do next → trust & method, instead of the
  previous scattered section order, so the score and its meaning lead and the data /
  methodology collapse into a final group.
- **Visual restyle** — single hero score ring, bullet-style dimension bars, a quality
  encoding that pairs label with color (legible in grayscale, not color-alone), a lifted
  dark surface, 8-pt spacing, and a modular type scale. Presentation only; no scoring or
  data change.
- The installer now downloads the **latest tagged release** instead of bleeding-edge
  `main`, so a work-in-progress commit on `main` can never break a fresh install.
  It falls back to `main` only if no release exists.

### Removed
- The in-report **"Methodology & honesty" appendix** and the **"Why only ~N days?"** /
  **"The honest part"** blocks. The de-contamination still runs exactly as before — only
  the in-report display of those breakdowns was removed.

## [1.0.0] — 2026-06-19

First tagged release — the known-good baseline existing users can pin to.

### Added
- Single-file, pure-stdlib engine (`insight.py`): discover → de-contaminate →
  score → report.
- `/ai-fluency` skill and the Sonnet 4.6 → Opus 4.8 workflow.
- 0–100 fluency score, builder archetype, 4-competency skill map, five measured
  dimensions, and personalized growth levers.
- Built-in private archive so analysis can see beyond Claude Code's 30-day window.
- 38 passing tests.

[Unreleased]: https://github.com/Feloguarin/claude-insight/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Feloguarin/claude-insight/releases/tag/v1.0.0
