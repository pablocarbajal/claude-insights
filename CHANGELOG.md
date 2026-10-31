# Changelog

All notable changes to Claude Insight are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/), and the
project aims for [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
