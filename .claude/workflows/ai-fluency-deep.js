export const meta = {
  name: 'ai-fluency-deep',
  description: 'Deep read of your Claude Code chats: Sonnet reads each conversation end to end, Opus finds your recurring habits and the rules worth saving to CLAUDE.md.',
  whenToUse: 'Run by the /ai-fluency skill after `insight.py --deep DIR` writes bundle.json + batch-*.json. Args: {batches: [absolute paths], lang, depth, run_fingerprint, double}.',
  phases: [
    { title: 'Read', detail: 'Sonnet — one reader per batch of ~8 conversations, two on the first `double` batches' },
    { title: 'Patterns', detail: 'Opus — recurring habits + CLAUDE.md rules across every read', model: 'opus' },
  ],
}

// Inputs come from bundle.json, which insight.py writes. Paths must be absolute: a bare
// relative path would let an agent "helpfully" read a stale copy somewhere else on disk.
const BATCHES = (args && args.batches) || []
const LANG = (args && args.lang) === 'en' ? 'English' : 'Spanish'
// The first DOUBLE batches are also read by a second, independent reader; comparing the two
// says how far each judgment can be trusted.
const DOUBLE = Math.min((args && args.double) || 0, BATCHES.length)
const COMPARED = ['outcome', 'friction_by', 'habit', 'vision', 'structure', 'requirements', 'caught_error', 'committed']
if (!BATCHES.length) throw new Error('ai-fluency-deep: no batches given (run insight.py --deep first)')

const READ_ITEM = {
  type: 'object', additionalProperties: false,
  required: ['episode_id', 'goal', 'outcome', 'friction_by', 'missing', 'key_moment', 'you_said', 'better', 'why', 'habit',
    'vision', 'structure', 'requirements', 'caught_error', 'committed'],
  properties: {
    episode_id: { type: 'string' },
    goal: { type: 'string', description: 'what the person wanted, one sentence' },
    outcome: { type: 'string', enum: ['resolved', 'partial', 'abandoned', 'unclear'] },
    friction_by: { type: 'string', enum: ['you', 'claude', 'environment', 'none'] },
    missing: { type: 'string', description: 'what their brief lacked that would have saved turns; empty if nothing' },
    key_moment: { type: 'string', description: 'the turn where it went right or wrong, and why' },
    you_said: { type: 'string', description: 'VERBATIM copy of one of their TÚ lines from THIS episode (without the "TÚ: " prefix); empty if none is worth rewriting' },
    better: { type: 'string', description: 'that same message rewritten, ready to paste; empty if you_said is empty' },
    why: { type: 'string', description: 'why the rewrite would have gone better, tied to what actually happened next' },
    habit: {
      type: 'string', enum: ['Direction', 'Iteration', 'Verification', 'Context', 'Toolcraft', 'Planning', 'Closure'],
      description: 'the habit the rewrite trains: Direction = the brief (goal, file, rule, done-when); Iteration = how a ' +
        'correction is phrased; Verification = asking for or doing a check of the result; Context = pointing Claude ' +
        'at the right file/area first; Toolcraft = telling Claude which tool or action to use',
    },
    vision: { type: 'boolean', description: 'the developer said what they wanted and why (the idea, the goal) before or while asking' },
    structure: { type: 'boolean', description: 'a plan, options or structure was discussed BEFORE the first change' },
    requirements: { type: 'boolean', description: 'precise requirements were given (files, rules, how to know it works)' },
    caught_error: { type: 'boolean', description: 'the developer spotted a concrete problem Claude had missed or claimed was fixed' },
    committed: { type: 'boolean', description: 'the work ended in a git commit' },
  },
}
const READS = {
  type: 'object', additionalProperties: false, required: ['reads'],
  properties: { reads: { type: 'array', items: READ_ITEM } },
}
const SYNTH = {
  type: 'object', additionalProperties: false, required: ['summary', 'patterns', 'claude_md_rules'],
  properties: {
    summary: { type: 'string' },
    patterns: {
      type: 'array', maxItems: 5,
      items: {
        type: 'object', additionalProperties: false, required: ['title', 'description', 'count', 'examples'],
        properties: {
          title: { type: 'string' }, description: { type: 'string' }, count: { type: 'integer', minimum: 2 },
          examples: {
            type: 'array', minItems: 1, maxItems: 4,
            items: {
              type: 'object', additionalProperties: false, required: ['episode_id', 'quote'],
              properties: { episode_id: { type: 'string' }, quote: { type: 'string' } },
            },
          },
        },
      },
    },
    claude_md_rules: {
      type: 'array', maxItems: 10,
      items: {
        type: 'object', additionalProperties: false, required: ['rule', 'why', 'episode_ids'],
        properties: {
          rule: { type: 'string' }, why: { type: 'string' },
          episode_ids: { type: 'array', items: { type: 'string' }, minItems: 1 },
        },
      },
    },
  },
}

const GROUND = `Every quote you give is checked word for word against the person's real transcripts ` +
  `afterwards; anything that doesn't match is thrown away. So copy quotes exactly — typos included — ` +
  `from the "TÚ:" lines. Write everything else in ${LANG}, plainly, like a kind and exacting teacher.`

// ---- Read: one Sonnet reader per batch (two on the first DOUBLE batches) -----
phase('Read')
const readPrompt = (path) =>
    `Use your Read tool to read EXACTLY this file and nothing else: ${path}\n` +
    `It holds several conversations ("episodes") between a developer and Claude Code. Each has an id, ` +
    `project, date, metrics, and a condensed transcript: "TÚ:" lines are what the developer typed, ` +
    `"CLAUDE:" lines are Claude's replies, "· Claude usó:" lines list the tools it ran, "✗ error:" marks a ` +
    `failed tool call and "· […]" marks trimmed text.\n\n` +
    `Read every episode end to end and return one entry per episode:\n` +
    `- goal: what they wanted.\n` +
    `- outcome: resolved / partial / abandoned / unclear — judge from how it ended, not from Claude's claims.\n` +
    `- friction_by: who caused the wasted turns. "you" = the brief was ambiguous or missing info only the ` +
    `developer had; "claude" = Claude misread a clear request, broke something, or claimed success without ` +
    `checking; "environment" = tools, installs, permissions, flaky systems; "none" = it went smoothly. ` +
    `Be fair: do not blame the developer for Claude's mistakes.\n` +
    `- missing: the concrete detail their brief lacked (a file, the symptom they saw, a rule, how to know it ` +
    `worked). Empty if nothing was missing.\n` +
    `- key_moment: the turn that decided it.\n` +
    `- you_said / better / why: pick the ONE developer message whose rewrite would have saved the most ` +
    `turns (often the opening brief, or a vague correction). you_said is that message copied verbatim; ` +
    `better is the rewrite, in the same language they wrote in, keeping their voice — add what was missing, ` +
    `do not pad. If every message was already good, leave all three empty.\n` +
    `- habit: which habit that rewrite trains (Direction, Iteration, Verification, Context, Toolcraft, ` +
    `Planning or Closure).\n` +
    `- vision / structure / requirements: be strict. Good work with an agent goes vision (the idea and why) → ` +
    `structure (a plan or options agreed BEFORE code) → requirements (precise details on top of that context). ` +
    `Mark each true only if it clearly happened in this episode; a long prompt is not automatically good.\n` +
    `- caught_error: true only if the developer found a concrete problem Claude missed (or claimed was fixed).\n` +
    `- committed: true only if a git commit happened in this episode.\n\n${GROUND}`
const readBatch = (path, label) =>
  agent(readPrompt(path), { label, phase: 'Read', model: 'sonnet', schema: READS }).then(r => (r && r.reads) || [])

const reads = await pipeline(
  BATCHES,
  (path, _item, i) => {
    const first = readBatch(path, `read:batch-${i + 1}`)
    if (i >= DOUBLE) return first
    // Same instructions, separate agent: it never sees the first reader's answers.
    return Promise.all([first, readBatch(path, `read2:batch-${i + 1}`)]).then(([a, b]) => {
      const other = Object.fromEntries(b.map(r => [r.episode_id, r]))
      return a.map(r => {
        const o = other[r.episode_id]
        if (!o) return r
        return { ...r, double: true, disputed: COMPARED.filter(f => r[f] !== o[f]) }
      })
    })
  },
)
const allReads = reads.filter(Boolean).flat()
log(`Read ${allReads.length} conversations across ${BATCHES.length} batches (${DOUBLE} of them read twice)`)

const twice = allReads.filter(r => r.double)
const agreement = {
  episodes: twice.length,
  fields: Object.fromEntries(COMPARED.map(f => [f, [twice.filter(r => !r.disputed.includes(f)).length, twice.length]])),
}
if (!allReads.length) return { schema: 'claude-insight-deep-analysis/1', reads: [], patterns: [], claude_md_rules: [], summary: '' }

// ---- Patterns: Opus across every read --------------------------------------
phase('Patterns')
const synth = await agent(
  `You are reviewing how one developer works with Claude Code. Below are per-conversation reads of ` +
  `${allReads.length} of their conversations. For exact quotes, you may Read these batch files (and only ` +
  `these): ${BATCHES.join(', ')}.\n\nREADS:\n${JSON.stringify(allReads)}\n\n` +
  `Produce:\n` +
  `- summary: 3–4 sentences on how they actually work with Claude — what goes well, what repeatedly costs ` +
  `them turns, and how much of the friction was theirs versus Claude's or the environment's. Be strict and ` +
  `concrete: say how often they go vision → structure → requirements versus firing the build order cold, ` +
  `whether they check results themselves or wave them through, and whether work ends committed.\n` +
  `- patterns: up to 5 recurring habits that show up in at least 2 conversations (count = how many). Each ` +
  `with a short title, a description of the habit and its cost, and 1–4 examples (episode_id + a verbatim ` +
  `quote of what they typed). Include a positive pattern if one is clear.\n` +
  `- claude_md_rules: instructions they keep giving by hand — preferences or corrections repeated across ` +
  `conversations (e.g. language of commits, folder conventions, tools to use or avoid). Write each as a ` +
  `short imperative line ready to paste into CLAUDE.md, with why and the episode_ids where it came up. ` +
  `Only rules the evidence supports; skip one-off requests.\n\n${GROUND}`,
  { label: 'patterns', phase: 'Patterns', model: 'opus', schema: SYNTH },
)

return {
  schema: 'claude-insight-deep-analysis/1',
  run_fingerprint: (args && args.run_fingerprint) || null,
  depth: (args && args.depth) || null,
  lang: (args && args.lang) || null,
  reads: allReads,
  agreement,
  summary: (synth && synth.summary) || '',
  patterns: (synth && synth.patterns) || [],
  claude_md_rules: (synth && synth.claude_md_rules) || [],
}
