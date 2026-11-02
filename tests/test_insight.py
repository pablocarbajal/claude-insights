"""
Tests for the v2 single-file engine (insight.py).

Focus: the accuracy guarantees that v1 violated — prompt de-contamination,
rate-based scoring that can't be inflated by volume, gap-capped active time,
and confidence shrinkage of thin signals. Pure stdlib unittest.
"""
import contextlib
import datetime
import glob
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import insight  # noqa: E402


def _rec(**kw):
    return json.dumps(kw)


def write_session(dirpath, name, records):
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(r + "\n")
    return path


def user_text(text, **extra):
    e = {"type": "user", "timestamp": extra.pop("ts", "2026-01-01T00:00:00Z"),
         "message": {"role": "user", "content": text}}
    e.update(extra)
    return _rec(**e)


def user_tool_result(ts="2026-01-01T00:00:01Z"):
    return _rec(type="user", timestamp=ts,
                message={"role": "user", "content": [{"type": "tool_result", "content": "ok"}]})


def assistant_tool(name, ts="2026-01-01T00:00:02Z", **inp):
    return _rec(type="assistant", timestamp=ts,
                message={"role": "assistant",
                         "content": [{"type": "tool_use", "name": name, "input": inp}]})



class TestSpanishAndReview(unittest.TestCase):
    """Spanish prompts are scored with Spanish cues, go-aheads don't count as weak briefs,
    and the user's reply after an edit is read as the human half of verification."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _corpus(self, recs):
        write_session(self.tmp, "s.jsonl", recs)
        return insight.parse(insight.discover_files(self.tmp))

    def test_spanish_cues(self):
        self.assertTrue(insight.INTENT_CUE.search("cámbialo para que cargue más rápido"))
        self.assertTrue(insight.CONSTRAINT_CUE.search("solo toca el archivo de login"))
        self.assertTrue(insight.ACTION_VERB.search("corrígelo por favor"))
        self.assertTrue(insight.CORRECTION_CUE.search("no se ve el icono"))
        self.assertTrue(insight.CORRECTION_CUE.search("no, así no"))
        # mid-sentence Spanish negation is not a rejection
        self.assertFalse(insight.CORRECTION_CUE.search("creo que no hace falta, sigue"))

    def test_go_aheads_left_out_of_briefing(self):
        corpus = self._corpus([
            user_text("arregla el login en auth.py para que no se cierre la sesión"),
            user_text("sí, hazlo", ts="2026-01-01T00:01:00Z"),
            user_text("sigue, página 26", ts="2026-01-01T00:02:00Z"),
        ])
        _s, det, _ev = insight.score_direction(corpus)
        self.assertEqual(det["n"], 1)
        self.assertEqual(det["followups"], 2)
        self.assertEqual(det["intent_rate"], 1.0)

    def test_review_replies(self):
        corpus = self._corpus([
            user_text("cambia el título en index.html"),
            assistant_tool("Edit", file_path="/p/index.html"),
            user_text("ok, sigue", ts="2026-01-01T00:01:00Z"),
            assistant_tool("Edit", ts="2026-01-01T00:01:05Z", file_path="/p/index.html"),
            user_text("funciona, ahora el footer", ts="2026-01-01T00:02:00Z"),
            assistant_tool("Edit", ts="2026-01-01T00:02:05Z", file_path="/p/index.html"),
            user_text("no se ve el icono en la barra de arriba", ts="2026-01-01T00:03:00Z"),
            assistant_tool("Edit", ts="2026-01-01T00:03:05Z", file_path="/p/index.html"),
            user_text("hazlo de nuevo", ts="2026-01-01T00:04:00Z"),
        ])
        r = insight.review_replies(corpus)
        c = r["counts"]
        self.assertEqual((c["rubber_stamp"], c["checked"], c["specific_fix"], c["vague_reject"]),
                         (1, 1, 1, 1))
        self.assertEqual(r["rate"], 0.5)

    def test_pasted_and_plugin_text_not_scored(self):
        corpus = self._corpus([
            user_text('resume esto <pasted_content id="a1">La idea es que porque para que</pasted_content>'),
            user_text('<pasted_content id="b2">solo texto pegado</pasted_content>', ts="2026-01-01T00:01:00Z"),
            user_text("The mapa plugin sent a message: haz commit", ts="2026-01-01T00:02:00Z"),
        ])
        self.assertEqual([p["text"] for p in corpus.real_prompts], ["resume esto"])

    def test_windows_project_label(self):
        self.assertEqual(insight._project_label("C--Dev-Projects-Web-Astro"), "Dev Projects Web Astro")
        self.assertEqual(insight._project_label("C--Users-pablo-code-app"), "code app")
        self.assertEqual(insight._project_label(
            "C--Users-pablo-AppData-Local-Claude-Data-x-scratch-workspaces-a-b-scratch-2026-10-31-1b2e18"), "No folder")


class TestDecontamination(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_filters_noise_keeps_real_prompts(self):
        recs = [
            user_text("add a login endpoint to api.py, only touch that file"),  # real
            user_tool_result(),                                                 # tool result
            user_text("<task-notification>\n<task-id>abc</task-id>"),           # injection marker
            user_text("You are a senior engineer wiring a trading bot. " + "x" * 50),  # subagent leak
            _rec(type="user", isSidechain=True, timestamp="2026-01-01T00:00:03Z",
                 message={"role": "user", "content": "subagent internal prompt"}),  # sidechain
            _rec(type="user", isMeta=True, timestamp="2026-01-01T00:00:04Z",
                 message={"role": "user", "content": "meta injected"}),             # meta
            user_text("y" * 7000),                                              # > 6KB paste
            user_text("run the tests"),                                         # real
        ]
        write_session(self.tmp, "s1.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        texts = [p["text"] for p in corpus.real_prompts]
        self.assertEqual(len(texts), 2)
        self.assertIn("add a login endpoint to api.py, only touch that file", texts)
        self.assertIn("run the tests", texts)
        # everything else was filtered, and the breakdown is recorded
        self.assertGreaterEqual(corpus.filtered["tool results"], 1)
        self.assertGreaterEqual(corpus.filtered["subagent turns"], 1)
        self.assertGreaterEqual(corpus.filtered["meta-injected"], 1)
        self.assertGreaterEqual(corpus.filtered["injected / pasted"], 2)


class TestNoVolumeInflation(unittest.TestCase):
    """Doing MORE of the same must not raise the score (rate-based)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_repeating_a_weak_prompt_does_not_help(self):
        few = [user_text("do it")] * 3
        many = [user_text("do it")] * 60
        write_session(self.tmp, "few.jsonl", few)
        c1 = insight.parse(insight.discover_files(self.tmp))
        d1, _, _ = insight.score_direction(c1)

        tmp2 = tempfile.mkdtemp()
        write_session(tmp2, "many.jsonl", many)
        c2 = insight.parse(insight.discover_files(tmp2))
        d2, _, _ = insight.score_direction(c2)
        # 20x the volume of the same weak prompt -> not a higher score
        self.assertLessEqual(d2, d1 + 1.0)


class TestActiveTimeCapsIdle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_idle_gap_is_capped(self):
        recs = [
            user_text("start", ts="2026-01-01T00:00:00Z"),
            user_text("end after a week of idle", ts="2026-01-08T00:00:00Z"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        # one ~7-day gap must be capped at GAP_CAP_SECONDS, not counted as a week
        self.assertLessEqual(corpus.active_seconds, insight.GAP_CAP_SECONDS + 1)


class TestConfidenceShrinkage(unittest.TestCase):
    def test_thin_signal_pulled_toward_50(self):
        # a high raw score on tiny n must shrink toward 50
        shrunk, c = insight.shrink(90.0, n=3, target_n=12)
        self.assertLess(shrunk, 90.0)
        self.assertGreater(shrunk, 50.0)
        self.assertAlmostEqual(c, 0.25, places=3)
        # full data -> no shrink
        shrunk2, c2 = insight.shrink(90.0, n=60, target_n=12)
        self.assertEqual(round(shrunk2), 90)
        self.assertEqual(c2, 1.0)


class TestContextGrounding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_blind_edit_scores_lower_than_grounded(self):
        grounded = [
            user_text("fix the bug"),
            assistant_tool("Read", file_path="/x/a.py"),
            assistant_tool("Edit", file_path="/x/a.py"),
        ]
        blind = [
            user_text("fix the bug"),
            assistant_tool("Edit", file_path="/x/a.py"),  # edited without reading
        ]
        write_session(self.tmp, "g.jsonl", grounded)
        cg = insight.parse(insight.discover_files(self.tmp))
        sg, dg, _ = insight.score_context(cg)

        tmp2 = tempfile.mkdtemp()
        write_session(tmp2, "b.jsonl", blind)
        cb = insight.parse(insight.discover_files(tmp2))
        sb, db, _ = insight.score_context(cb)
        self.assertGreater(sg, sb)
        self.assertEqual(dg["rate"], 1.0)
        self.assertEqual(db["rate"], 0.0)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_full_run_and_html(self):
        recs = [
            user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            assistant_tool("Bash", command="python -m pytest -q"),
            user_text("run it"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        result = insight.analyze(corpus)
        self.assertIn(result["band"], [b[0] for b in insight.BANDS])
        self.assertTrue(0 <= result["overall"] <= 100)
        cards, strength = insight.build_action_plan(corpus, result)
        html = insight.build_html(corpus, result, cards, strength)
        self.assertIn("AI Fluency", html)
        self.assertIn("How much data this is based on", html)
        self.assertIn(result["band"], html)


class TestSpanishToggle(unittest.TestCase):
    """The report ships an EN/ES switch; the Spanish half translates report copy only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        recs = [
            user_text("add a /health endpoint to server.py, only touch that file"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            assistant_tool("Bash", command="python -m pytest -q"),
            user_text("no se ve el botón nuevo"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        result = insight.analyze(corpus)
        cards, strength = insight.build_action_plan(corpus, result)
        self.en = insight.build_html(corpus, result, cards, strength)
        self.both = insight.bilingual_html(self.en)

    def test_both_languages_and_switch(self):
        self.assertIn('data-lang="en"', self.both)
        self.assertIn('data-lang="es" lang="es" hidden', self.both)
        self.assertIn('class="lang-switch"', self.both)
        es = self.both.split('data-lang="es"', 1)[1]
        for phrase in ("Las siete dimensiones", "Evaluación profesional", "Qué mejorar",
                       "Con cuántos datos se calculó", "Números honestos de un vistazo"):
            self.assertIn(phrase, es)
        # the English half stays English
        en = self.both.split('data-lang="es"', 1)[0]
        self.assertIn("The seven dimensions", en)
        self.assertNotIn("Las siete dimensiones", en)

    def test_tabs_charts_and_method(self):
        for tab in ("summary", "dims", "improve", "chats", "usage", "method"):
            self.assertIn(f'data-tab="{tab}"', self.en)
            self.assertIn(f'data-panel="{tab}"', self.en)
        self.assertIn('<svg class="chart stack"', self.en)      # prompts per week, by project
        self.assertIn('<svg class="heat"', self.en)             # day × hour
        self.assertIn('<div class="mix">', self.en)             # what Claude did, by type
        self.assertIn('data-tab="improve" class="hot"', self.en)  # the tabs to open call for attention
        self.assertIn('class="gr-line"', self.en)                   # grades S..F under the score
        self.assertIn("dimensiones en B o mejor", self.both)
        self.assertEqual(insight._grade(92)[0], "S")
        self.assertEqual(insight._grade(41)[0], "D")
        self.assertEqual(insight._grade(10)[0], "F")
        self.assertIn('class="band-name"', self.en)                # the level wears the ring's gradient
        self.assertIn('data-tab="usage" class="hot"', self.en)
        self.assertIn("Your last 50 chats", self.en)
        for proto in insight.PROTOTYPES:                        # every archetype is explained
            self.assertIn(insight._esc(insight.PROTOTYPES[proto]["blurb"]), self.en)
        es = self.both.split('data-lang="es"', 1)[1]
        for phrase in ("Cómo evaluamos", "Tus últimos 50 chats", "Prompts por semana, por proyecto", "Arquetipos",
                       "Tu ritmo", "Cuándo trabajas", "Qué hizo Claude por ti", "racha más larga (días seguidos)"):
            self.assertIn(phrase, es)

    def test_usage_helpers(self):
        self.assertEqual(insight._project_short("C--Dev-Projects-Mapa"), "Mapa")
        self.assertEqual(insight._project_short("C--Dev"), "Dev")
        self.assertEqual(insight._project_short("C--Dev-Projects-Educacion-CursosOnline-Skool-com-Ezequiel"),
                         "… Skool com Ezequiel")
        uuid = "8f5556ca-95f3-4abf-8f3d-8fbbfdac36c2"
        self.assertEqual(insight._connector_name(uuid, {"search_files": 3, "share_file": 1}), "Google Drive")
        self.assertEqual(insight._connector_name(uuid, {"notion-fetch": 2}), "Notion")
        self.assertEqual(insight._connector_name(uuid, {"mystery": 1}), "Connector 8f5556ca")
        self.assertEqual(insight._server_kind(uuid), "Connectors")
        self.assertEqual(insight._server_kind("Claude_Browser"), "Browser & screen")
        self.assertEqual(insight._server_kind("ccd_turn"), "Claude app")
        self.assertEqual(insight._server_kind("penpot"), "Other MCP")
        self.assertEqual(insight._tool_group("Bash"), "Terminal")
        self.assertEqual(insight._tool_group("tabs_context_mcp"), "Browser & screen")
        self.assertEqual(insight._tool_group("something_new"), "Other")
        self.assertEqual(insight._tr_es("Tuesday 15h: 23 prompts"), "Martes 15h: 23 prompts")
        self.assertEqual(insight._tr_es("week of 01/08 · Other: 4 prompts"), "semana del 01/08 · Otro: 4 prompts")
        self.assertEqual(insight._tr_es("week of 01/08: 9 prompts · Mapa: 5 (56%) · Other: 4 (44%)"),
                         "semana del 01/08: 9 prompts · Mapa: 5 (56%) · Otro: 4 (44%)")
        self.assertEqual(insight._tr_es("Monday 15h: 23 prompts · 1.2% of all your prompts · busier than 85% of your hours"),
                         "Lunes 15h: 23 prompts · 1.2% de todos tus prompts · más activa que el 85% de tus horas")

    def test_user_text_is_never_translated(self):
        # a prompt that happens to equal a report phrase must stay as typed
        self.assertIsNone(insight._tr_es("“no se ve el botón nuevo”"))
        self.assertEqual(insight._tr_es("“fix the login bug”"), "“arregla el bug del login”")

    def test_every_teaching_text_has_spanish(self):
        for dim, t in insight.SKILL_TEACH.items():
            for key in ("what_it_is", "why_it_matters", "how_to_improve", "practice", "good_looks_like"):
                self.assertIn(t[key], insight.ES, f"{dim}.{key} has no Spanish")
        for _name, _lo, _hi, meaning in insight.BANDS:
            self.assertIn(meaning, insight.ES)
        for proto in insight.PROTOTYPES.values():
            self.assertIn(proto["blurb"], insight.ES)


def assistant_say(text, ts="2026-01-01T00:00:03Z"):
    return _rec(type="assistant", timestamp=ts,
                message={"role": "assistant", "content": [{"type": "text", "text": text}]})


def tool_error(text, ts="2026-01-01T00:00:04Z"):
    return _rec(type="user", timestamp=ts, message={"role": "user", "content": [
        {"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": text}]}]})


class TestDeepRead(unittest.TestCase):
    """Conversations are cut into episodes, picked deterministically, and every quote the
    AI returns is checked against what the user really typed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        recs = [
            user_text("arregla el login en auth.py, no guarda la sesión", ts="2026-01-01T10:00:00Z"),
            assistant_tool("Read", ts="2026-01-01T10:00:05Z", file_path="/p/auth.py"),
            assistant_tool("Edit", ts="2026-01-01T10:00:10Z", file_path="/p/auth.py"),
            tool_error("SyntaxError: invalid syntax", ts="2026-01-01T10:00:11Z"),
            assistant_say("Listo, ya guarda la sesión.", ts="2026-01-01T10:00:12Z"),
            user_text("no funciona", ts="2026-01-01T10:01:00Z"),
            assistant_tool("Edit", ts="2026-01-01T10:01:10Z", file_path="/p/auth.py"),
            user_text("ok", ts="2026-01-01T10:02:00Z"),
            # a new task in the same session
            user_text("ahora agrega un botón de salir en navbar.tsx", ts="2026-01-01T10:03:00Z"),
            assistant_tool("Edit", ts="2026-01-01T10:03:10Z", file_path="/p/navbar.tsx"),
            # a long pause also starts a new episode
            user_text("sí", ts="2026-01-01T14:00:00Z"),
            _rec(type="ai-title", aiTitle="Arreglar login", sessionId="abcdef123456"),
            _rec(type="custom-title", customTitle="Login roto", sessionId="abcdef123456"),
        ]
        write_session(self.tmp, "abcdef123456.jsonl", recs)
        self.corpus = insight.parse(insight.discover_files(self.tmp))

    def test_episodes(self):
        eps = insight.build_episodes(self.corpus)
        self.assertEqual([e["id"] for e in eps], ["abcdef12-1", "abcdef12-2", "abcdef12-3"])
        self.assertEqual(eps[0]["title"], "Login roto")      # your rename wins over Claude's title
        first = eps[0]
        self.assertEqual(first["prompts"], ["arregla el login en auth.py, no guarda la sesión", "no funciona", "ok"])
        m = first["metrics"]
        self.assertEqual((m["edits"], m["errors"], m["corrections"], m["vague_corrections"], m["rubber_stamps"]),
                         (2, 1, 1, 1, 1))
        self.assertIn("TÚ: no funciona", first["transcript"])
        self.assertIn("CLAUDE: Listo, ya guarda la sesión.", first["transcript"])
        self.assertIn("✗ error: SyntaxError", first["transcript"])
        self.assertGreater(first["friction"], eps[1]["friction"])

    def test_selection_and_bundle(self):
        eps = insight.build_episodes(self.corpus)
        self.assertEqual(insight.select_episodes(eps, "rapida"), insight.select_episodes(eps, "rapida"))
        result = insight.analyze(self.corpus)
        info = insight.write_deep_bundle(self.corpus, result, os.path.join(self.tmp, "deep"), "rapida")
        self.assertEqual(info["lang"], "es")
        self.assertEqual(info["episodes_total"], 3)
        with open(info["batches"][0], encoding="utf-8") as f:
            batch = json.load(f)
        self.assertNotIn("prompts", batch["episodes"][0])   # agents get the transcript, not raw records

    def test_quotes_are_verified(self):
        deep = {
            "reads": [
                {"episode_id": "abcdef12-1", "goal": "arreglar login", "outcome": "partial", "friction_by": "you",
                 "you_said": "no funciona", "better": "Sigue sin guardar la cookie en Safari", "why": "x"},
                {"episode_id": "abcdef12-2", "goal": "botón", "outcome": "resolved", "friction_by": "none",
                 "you_said": "algo que nunca escribí", "better": "y", "why": "z"},
                {"episode_id": "nope-9", "goal": "?"},
            ],
            "patterns": [
                {"title": "Correcciones vagas", "description": "d", "count": 2,
                 "examples": [{"episode_id": "abcdef12-1", "quote": "“No  funciona”"}]},
                {"title": "Inventado", "description": "d", "count": 3,
                 "examples": [{"episode_id": "abcdef12-1", "quote": "esto no existe"}]},
            ],
            "claude_md_rules": [
                {"rule": "Commits en español", "why": "w", "episode_ids": ["abcdef12-1"]},
                {"rule": "Sin evidencia", "why": "w", "episode_ids": ["zzz"]},
            ],
        }
        v = insight.verify_deep_analysis(deep, self.corpus)
        self.assertEqual(len(v["reads"]), 2)
        self.assertEqual(v["reads"][0]["you_said"], "no funciona")
        self.assertIsNone(v["reads"][1]["you_said"])         # invented quote removed
        self.assertEqual([p["title"] for p in v["patterns"]], ["Correcciones vagas"])
        self.assertEqual([r["rule"] for r in v["rules"]], ["Commits en español"])
        self.assertEqual(v["dropped"], {"unknown episode": 1, "quote not found": 2,
                                        "pattern without evidence": 1, "rule without evidence": 1})
        # the rewrite of a correction trains Iteration; chat name and resume command come along
        self.assertEqual(v["reads"][0]["habit"], "Iteration")
        self.assertEqual(v["reads"][0]["session"], "abcdef123456")
        self.assertIn("claude --resume abcdef123456",
                      insight._chat_ref_html("Login roto", "p", "2026-01-01", "abcdef123456"))
        html_doc = insight._deep_section_html(v)
        self.assertIn("Your chats in depth", html_doc)
        self.assertIn("Commits en español", html_doc)
        es = insight.translate_html_es("<body>" + html_doc + "</body>")
        self.assertIn("Tus chats a fondo", es)
        self.assertIn("Reglas que repites", es)


class TestHighlight(unittest.TestCase):
    def test_numbers_quotes_and_sequences_are_marked(self):
        out = insight._highlight('38 de las 50 acaban bien; visión → estructura → requisitos; "ahora esto," <b>')
        self.assertIn('<b class="hl-n">38 de las 50</b>', out)
        self.assertIn('<mark class="hl-k">visión → estructura → requisitos</mark>', out)
        self.assertIn('<mark class="hl-q">&quot;ahora esto,&quot;</mark>', out)
        self.assertIn("&lt;b&gt;", out)                       # the summary text itself stays escaped


class TestProgress(unittest.TestCase):
    def test_bar_draws_on_a_terminal_and_stays_silent_otherwise(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        out = Tty()
        p = insight.Progress(10, stream=out, spanish=True)
        p.stage("Reading", "Leyendo tus chats")
        p.tick(5)
        p.finish()
        text = out.getvalue()
        self.assertIn("Leyendo tus chats", text)
        self.assertIn(" 50%", text)
        self.assertIn("100%", text)
        self.assertIn("Listo en", text)
        out2 = Tty()
        p2 = insight.Progress(4, stream=out2)
        p2.stage("Reading your chats", "x")
        p2.note("  a message")
        self.assertIn("\n", out2.getvalue())
        self.assertTrue(out2.getvalue().split("\n")[0].endswith("  a message"))   # wiped, then the note
        quiet = io.StringIO()                           # not a terminal: nothing written
        q = insight.Progress(10, stream=quiet)
        q.stage("Reading", "Leyendo")
        q.tick(10)
        q.finish()
        self.assertEqual(quiet.getvalue(), "")


class TestCases(unittest.TestCase):
    """The “you said → better” cases section in What to improve."""

    def test_cases_filter_and_spanish(self):
        reads = [{"episode_id": f"e{i}", "you_said": f"hazlo {i}", "better": f"mejor {i}", "goal": "algo",
                  "why": "porque", "habit": h, "project": "C--Dev-Projects-Mapa", "date": "2026-10-01",
                  "title": "Chat", "session": "s1", "friction_score": i}
                 for i, h in enumerate(["Direction", "Direction", "Closure"])]
        reads.append({**reads[0], "episode_id": "x", "you_said": None})        # no quote: not a case
        html_en = insight._cases_html({"reads": reads})
        self.assertIn("3 cases from your chats", html_en)
        self.assertEqual(html_en.count('<article class="case"'), 3)
        self.assertIn('data-filter="Closure"', html_en)
        self.assertLess(html_en.index('data-habit="Direction"'), html_en.index('data-habit="Closure"'))
        es = insight.translate_html_es(html_en)
        self.assertIn("3 casos de tus chats: qué pediste y cómo pedirlo", es)
        self.assertIn("Querías:", es)
        self.assertEqual(insight._cases_html({"reads": []}), "")


class TestStricterMeasures(unittest.TestCase):
    """Who checked each change, planning before building, and closing work with a commit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _corpus(self, sessions):
        for name, recs in sessions.items():
            write_session(self.tmp, name, recs)
        return insight.parse(insight.discover_files(self.tmp))

    def test_who_checked_each_change(self):
        c = self._corpus({"a.jsonl": [
            user_text("arregla el botón en app.tsx"),
            assistant_tool("Edit", file_path="/p/app.tsx"),
            assistant_tool("Bash", command="npm run build"),                 # Claude checked
            user_text("ok"),
            assistant_tool("Edit", file_path="/p/app.tsx"),
            _rec(type="assistant", timestamp="2026-01-01T00:00:05Z", message={"role": "assistant", "content": [
                {"type": "tool_use", "name": "mcp__Claude_Browser__computer", "input": {}}]}),  # browser check
            user_text("funciona, se ve bien"),                                # ...and you checked: both
            assistant_tool("Edit", file_path="/p/app.tsx"),
            user_text("ok"),                                                  # nobody checked, you said ok
            assistant_tool("Edit", file_path="/p/NOTAS.md"),                  # docs only: not counted
            user_text("ok"),
        ]})
        _s, det, _ev = insight.score_verification(c)
        self.assertEqual(det["episodes"], 3)
        self.assertEqual(det["who"], {"both": 1, "claude": 1, "you": 0, "none": 1})
        self.assertEqual(det["blind_ok"], 1)

    def test_monthly_history(self):
        def month(day, n, careful):
            recs = []
            for i in range(n):
                ts = f"{day}T10:{i // 60:02d}:{i % 60:02d}Z"
                text = (f"arregla el error en app{i}.py porque falla el login, sin tocar la api" if careful
                        else "hazlo")
                recs += [user_text(text, ts=ts), assistant_tool("Edit", ts=ts, file_path=f"/p/app{i}.py")]
            return recs
        write_session(self.tmp, "mar.jsonl", month("2026-03-15", 40, careful=False))
        write_session(self.tmp, "abr.jsonl", month("2026-04-15", 40, careful=True))
        write_session(self.tmp, "may.jsonl", month("2026-05-15", 3, careful=True))
        hist = insight.monthly_history(insight.discover_files(self.tmp))
        self.assertEqual([r["month"] for r in hist], ["2026-03", "2026-04", "2026-05"])
        self.assertEqual([r["prompts"] for r in hist], [40, 40, 3])
        self.assertIsNone(hist[2]["overall"])                          # too thin to score
        self.assertGreater(hist[1]["dims"]["Direction"], hist[0]["dims"]["Direction"])
        html_en = insight._history_html(hist)
        self.assertIn("Since 2026-03 your score went from", html_en)
        self.assertIn("Not scored: 2026-05.", html_en)
        self.assertIn('class="evo-grid"', html_en)                # one card per dimension
        self.assertEqual(html_en.count('class="evo"'), len(insight.WEIGHTS))   # + the total card
        self.assertEqual(html_en.count('class="evo total"'), 1)
        self.assertIn("vs the month before", html_en)                    # hover cards carry the change
        self.assertNotIn("<table", html_en)                       # no wide table to scroll
        es = insight.translate_html_es(html_en)
        self.assertIn("Tu evolución mes a mes", es)
        self.assertIn("Desde 2026-03 tu puntaje pasó de", es)
        self.assertIn("Lo que más cambió: Cómo pides", es)
        self.assertIn("Sin puntaje: 2026-05.", es)
        self.assertIn("Cada dimensión, mes a mes", es)
        self.assertIn("contra el mes anterior", es)
        self.assertIn("<span>abr</span>", es)                            # month under every bar
        self.assertIn("Since Mar: prompts with a constraint 0% → 100%", html_en)   # why the score moved
        self.assertIn("Desde mar: prompts con una restricción 0% → 100%", es)
        self.assertEqual(insight._history_html([]), "")

    def test_planning_and_closure(self):
        planned = [user_text("antes de tocar código, ¿cómo lo plantearías? dame opciones")]
        planned += [assistant_tool("Edit", file_path=f"/p/f{i}.py") for i in range(3)]
        planned += [user_text("listo, haz commit"), assistant_tool("Bash", command='git commit -m "x"')]
        cold = [user_text("hazme la api de usuarios")]
        cold += [assistant_tool("Edit", file_path=f"/p/g{i}.py") for i in range(3)]
        cold += [user_text("sigue sin funcionar")]
        c = self._corpus({"plan.jsonl": planned, "cold.jsonl": cold})
        _s, det, ev = insight.score_planning(c)
        self.assertEqual((det["n"], det["planned"]), (2, 1))
        self.assertEqual(ev[0]["text"], "hazme la api de usuarios")
        _s, det, _ev = insight.score_closure(c)
        self.assertEqual((det["repo_sessions"], det["committed"], det["you_asked"], det["ended_bad"]), (2, 1, 1, 1))


class TestEdgeCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_real_prompts_but_zero_tool_calls_renders(self):
        # regression: zero tool calls used to crash build_html with KeyError: 'evenness'
        write_session(self.tmp, "chat.jsonl", [user_text("hi"), user_text("what can you do?")])
        rc = insight.main([self.tmp, "-o", os.path.join(self.tmp, "r.html"), "--no-open"])
        self.assertEqual(rc, 0)
        html = open(os.path.join(self.tmp, "r.html"), encoding="utf-8").read()
        self.assertIn("AI Fluency", html)
        self.assertNotIn("{", html.split("<style>")[0])  # no template leaks before CSS

    def test_self_authored_file_edit_is_grounded(self):
        # regression: editing a file the agent WROTE this session must count as grounded
        recs = [
            user_text("make a config"),
            assistant_tool("Write", file_path="/x/conf.py"),
            assistant_tool("Edit", file_path="/x/conf.py"),   # never Read — but we wrote it
            assistant_tool("Edit", file_path="/x/conf.py"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        _, detail, blind = insight.score_context(corpus)
        self.assertEqual(detail["rate"], 1.0)
        self.assertEqual(blind, [])

    def test_injected_head_allows_casual_youre(self):
        self.assertFalse(insight._looks_injected("you're right, fix the login bug in auth.py"))
        self.assertTrue(insight._looks_injected("You are a senior engineer. Your task is ..."))

    def test_archetype_reflects_user_not_claude(self):
        # A heavy delegator with terse prompts must read as the Autonomous Agent even when
        # Claude's read-before-edit / verify habits are maxed — those Claude-driven
        # dimensions are agency-discounted.
        dims = {"Direction": 48, "Verification": 100, "Context": 100, "Iteration": 62, "Toolcraft": 84}
        a = insight.classify_archetype(dims, delegation_score=100)
        self.assertEqual(a["primary"], "Autonomous Agent")
        # the same profile with NO delegation should NOT read as the Autonomous Agent
        b = insight.classify_archetype(dims, delegation_score=0)
        self.assertNotEqual(b["primary"], "Autonomous Agent")


class TestArchive(unittest.TestCase):
    """The archive is what lets analysis exceed Claude Code's 30-day on-disk retention."""

    def setUp(self):
        self.live = tempfile.mkdtemp()
        self.arch = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.live, "proj"), exist_ok=True)
        self.f = write_session(os.path.join(self.live, "proj"), "sess.jsonl",
                               [user_text("first prompt")])

    def test_copies_new_then_skips_unchanged_then_updates_on_growth(self):
        new, updated = insight.archive_transcripts([self.f], self.arch)
        self.assertEqual((new, updated), (1, 0))
        dest = os.path.join(self.arch, "proj", "sess.jsonl")
        self.assertTrue(os.path.exists(dest))
        # second run, unchanged -> no copy
        new, updated = insight.archive_transcripts([self.f], self.arch)
        self.assertEqual((new, updated), (0, 0))
        # the live file grows (a new turn) -> archive copy is refreshed
        with open(self.f, "a", encoding="utf-8") as fh:
            fh.write(user_text("second prompt") + "\n")
        new, updated = insight.archive_transcripts([self.f], self.arch)
        self.assertEqual((new, updated), (0, 1))
        self.assertEqual(os.path.getsize(dest), os.path.getsize(self.f))

    def test_archive_never_truncates_on_smaller_live(self):
        # if a fresh (smaller) file ever shadows an older richer archive copy, we keep the big one
        insight.archive_transcripts([self.f], self.arch)
        dest = os.path.join(self.arch, "proj", "sess.jsonl")
        big = os.path.getsize(dest)
        # archive holds the full history; a truncated live copy must NOT shrink it via dedupe
        merged = insight._dedupe_sessions([self.f, dest])
        self.assertEqual(len(merged), 1)

    def test_dedupe_prefers_largest_and_keeps_distinct_sessions(self):
        # same session in two roots, different sizes -> the larger (more complete) wins
        d2 = os.path.join(self.arch, "proj")
        os.makedirs(d2, exist_ok=True)
        small = write_session(d2, "sess.jsonl", [user_text("x")])  # smaller copy of same session
        merged = insight._dedupe_sessions([self.f, small])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0], self.f)  # the bigger live file, not the small archive copy
        # a genuinely different session is preserved
        other = write_session(os.path.join(self.live, "proj"), "other.jsonl", [user_text("y")])
        merged2 = insight._dedupe_sessions([self.f, small, other])
        self.assertEqual(len(merged2), 2)

    def test_main_merges_archive_so_old_sessions_still_count(self):
        # An "old" session that exists ONLY in the archive (Claude Code already deleted the live
        # copy) must still be analyzed. Live dir is empty; the archive supplies the history.
        empty_live = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.arch, "oldproj"), exist_ok=True)
        write_session(os.path.join(self.arch, "oldproj"), "old.jsonl",
                      [user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
                       user_text("now run the tests to confirm it works")])
        out = os.path.join(empty_live, "r.html")
        os.environ["CLAUDE_PROJECTS_DIR"] = empty_live  # discover_files reads the empty live dir
        try:
            # no positional path -> archive logic engages; --archive supplies the old session
            rc = insight.main(["--archive", self.arch, "-o", out, "--no-open"])
        finally:
            del os.environ["CLAUDE_PROJECTS_DIR"]
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("AI Fluency", html)
        # the archive-only prompts were actually analyzed (live had none)
        self.assertIn("sessions in your archive", html)

    def test_smaller_live_never_truncates_larger_archive(self):
        # If the live file is SMALLER than the archive (corruption / truncation), the archive
        # must NOT be overwritten — the bigger copy is the more complete history.
        insight.archive_transcripts([self.f], self.arch)
        dest = os.path.join(self.arch, "proj", "sess.jsonl")
        with open(dest, "a", encoding="utf-8") as fh:        # grow the ARCHIVE past live
            fh.write(user_text("extra archived turn that live no longer has") + "\n")
        big = os.path.getsize(dest)
        new, updated = insight.archive_transcripts([self.f], self.arch)
        self.assertEqual((new, updated), (0, 0))             # skipped — archive already bigger
        self.assertEqual(os.path.getsize(dest), big)         # archive untouched

    def test_dedupe_survives_project_folder_rename(self):
        # Same session (same UUID filename) under two DIFFERENT project folders must dedupe to one.
        a = write_session(os.path.join(self.live, "proj"), "uuid-1.jsonl", [user_text("one"), user_text("two")])
        d2 = os.path.join(self.arch, "renamed-proj")
        os.makedirs(d2, exist_ok=True)
        b = write_session(d2, "uuid-1.jsonl", [user_text("one")])   # smaller copy, different folder
        merged = insight._dedupe_sessions([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0], a)                       # the larger one wins

    def test_no_archive_flag_does_not_write(self):
        live_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(live_dir, "proj"), exist_ok=True)
        write_session(os.path.join(live_dir, "proj"), "s.jsonl",
                      [user_text("add a /health route to server.py and run the tests")])
        out = os.path.join(live_dir, "r.html")
        os.environ["CLAUDE_PROJECTS_DIR"] = live_dir
        try:
            rc = insight.main(["--no-archive", "--archive", self.arch, "-o", out, "--no-open"])
        finally:
            del os.environ["CLAUDE_PROJECTS_DIR"]
        self.assertEqual(rc, 0)
        self.assertEqual(glob.glob(os.path.join(self.arch, "**", "*.jsonl"), recursive=True), [])

    def test_explicit_path_does_not_touch_archive(self):
        # Seed an archive, then analyze an explicit dir: the archive must be neither written nor merged.
        os.makedirs(os.path.join(self.arch, "old"), exist_ok=True)
        write_session(os.path.join(self.arch, "old"), "old.jsonl", [user_text("archived only")])
        before = sorted(glob.glob(os.path.join(self.arch, "**", "*.jsonl"), recursive=True))
        explicit = tempfile.mkdtemp()
        write_session(explicit, "live.jsonl",
                      [user_text("add a /health route to server.py and run the tests")])
        out = os.path.join(explicit, "r.html")
        rc = insight.main([explicit, "--archive", self.arch, "-o", out, "--no-open"])
        self.assertEqual(rc, 0)
        after = sorted(glob.glob(os.path.join(self.arch, "**", "*.jsonl"), recursive=True))
        self.assertEqual(before, after)                      # archive untouched
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("sessions in your archive", html)   # archive not merged into analysis


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_subagent_transcripts_are_excluded(self):
        # Agent-to-agent transcripts under .../subagents/... are NOT the user's prompts and
        # must not be discovered — otherwise running workflows would inflate the analysis.
        proj = os.path.join(self.tmp, "proj")
        sub = os.path.join(proj, "uuid", "subagents")
        os.makedirs(sub, exist_ok=True)
        main_f = write_session(proj, "main.jsonl", [user_text("a real user prompt about server.py")])
        sub_f = write_session(sub, "agent-x.jsonl", [user_text("do the assigned subtask")])
        found = insight.discover_files(self.tmp)
        self.assertIn(main_f, found)
        self.assertNotIn(sub_f, found)

    def test_explicit_single_subagent_file_is_still_honored(self):
        sub = os.path.join(self.tmp, "uuid", "subagents")
        os.makedirs(sub, exist_ok=True)
        sub_f = write_session(sub, "agent-x.jsonl", [user_text("explicitly requested file")])
        self.assertEqual(insight.discover_files(sub_f), [sub_f])


class TestPipelineModes(unittest.TestCase):
    """The --evidence (pipeline input) and --analysis (Opus output → report) hooks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        write_session(self.tmp, "s.jsonl", [
            user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            assistant_tool("Bash", command="python -m pytest -q"),
            user_text("run it and tell me if it passes"),
        ])

    def test_evidence_bundle_is_valid_and_self_contained(self):
        ev = os.path.join(self.tmp, "ev.json")
        rc = insight.main([self.tmp, "--evidence", ev, "--no-open", "-o", os.path.join(self.tmp, "r.html")])
        self.assertEqual(rc, 0)
        with open(ev, encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertEqual(d["schema"], "claude-insight-evidence/1")
        for k in ("meta", "scores", "dimension_detail", "behavior", "archetype"):
            self.assertIn(k, d)
        self.assertGreaterEqual(len(d["behavior"]["sample_prompts"]), 1)
        self.assertIn("Direction", d["behavior"]["weak_examples"])
        # evidence must carry file basenames, never absolute paths
        for items in d["behavior"]["weak_examples"].values():
            for e in items:
                self.assertNotIn("/", e.get("file", ""))

    def test_analysis_json_merges_into_report(self):
        analysis = {
            "overall_read": "You hand off whole jobs well; sharpen your briefs next.",
            "skill_map": [
                {"competency": "Delegation", "level": 4, "level_label": "Advanced",
                 "summary": "Hands off end to end.", "evidence": ["one scoped hand-off"],
                 "next_move": "add one sentence of intent per hand-off"},
                {"competency": "Description", "level": 2, "level_label": "Developing",
                 "summary": "Often terse.", "evidence": ["'run it'"],
                 "next_move": "name a file + a constraint"},
            ],
            "top_growth": [{"title": "Brief better", "why": "fewer rounds", "how": "front-load intent",
                            "example_before": "run it", "example_after": "run the server.py tests; report failures"}],
            "strengths": ["clear delegation"],
        }
        ap = os.path.join(self.tmp, "an.json")
        with open(ap, "w", encoding="utf-8") as fh:
            json.dump(analysis, fh)
        out = os.path.join(self.tmp, "r.html")
        rc = insight.main([self.tmp, "--analysis", ap, "--no-open", "-o", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("analyzed against the AI Fluency framework", html)
        self.assertIn("Delegation", html)
        self.assertIn("Advanced", html)
        self.assertIn("name a file + a constraint", html)

    def test_report_without_analysis_has_no_ai_section(self):
        out = os.path.join(self.tmp, "r.html")
        rc = insight.main([self.tmp, "--no-open", "-o", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("analyzed against the AI Fluency framework", html)
        # A plain deterministic run must NOT show the "AI stage didn't run" banner — that
        # banner is only for a run where an analysis was supplied but couldn't be used.
        self.assertNotIn("Deterministic report only", html)


class TestAnalysisProvenance(unittest.TestCase):
    """Regression guard for the leakage bug: a stale/foreign/empty analysis must never
    render in this run's report. An analysis is bound to a run by a fingerprint."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        write_session(self.tmp, "s.jsonl", [
            user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            assistant_tool("Bash", command="python -m pytest -q"),
            user_text("run it and tell me if it passes"),
        ])
        self.fp = insight.analyze(insight.parse(insight.discover_files(self.tmp)))["fingerprint"]

    def _analysis(self, **over):
        a = {
            "overall_read": "UNIQUE-VERDICT-TOKEN: hands off whole jobs well.",
            "skill_map": [{"competency": "Delegation", "level": 4, "level_label": "Advanced",
                           "summary": "Hands off end to end.", "evidence": ["one scoped hand-off"],
                           "next_move": "add one sentence of intent per hand-off"}],
            "top_growth": [], "strengths": ["clear delegation"],
        }
        a.update(over)
        return a

    def _write(self, obj):
        p = os.path.join(self.tmp, "an.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return p

    def _run(self, ap, evidence=None):
        out = os.path.join(self.tmp, "r.html")
        argv = [self.tmp, "--analysis", ap, "--no-open", "-o", out]
        if evidence:
            argv += ["--analysis-evidence", evidence]
        rc = insight.main(argv)
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            return fh.read()

    def _evidence_for(self, src_dir):
        """Write a real evidence bundle (with its run_fingerprint) for a transcript dir."""
        evp = os.path.join(tempfile.mkdtemp(), "ev.json")
        rc = insight.main([src_dir, "--evidence", evp, "--no-open", "--no-archive",
                           "-o", os.path.join(self.tmp, "det.html")])
        self.assertEqual(rc, 0)
        return evp

    def test_matching_fingerprint_merges(self):
        html = self._run(self._write(self._analysis(run_fingerprint=self.fp)))
        self.assertIn("analyzed against the AI Fluency framework", html)
        self.assertIn("UNIQUE-VERDICT-TOKEN", html)

    def test_mismatched_fingerprint_is_rejected_and_does_not_leak(self):
        # The exact reported bug: an analysis from a DIFFERENT run/person must not render.
        html = self._run(self._write(self._analysis(run_fingerprint="deadbeefdeadbeef")))
        self.assertNotIn("UNIQUE-VERDICT-TOKEN", html)              # no foreign verdict leaked
        self.assertNotIn("analyzed against the AI Fluency framework", html)
        self.assertIn("Deterministic report only", html)           # and we say so honestly

    def test_empty_analysis_is_dropped_with_notice(self):
        html = self._run(self._write({}))
        self.assertNotIn("analyzed against the AI Fluency framework", html)
        self.assertIn("Deterministic report only", html)

    def test_fingerprintless_analysis_still_merges_for_backcompat(self):
        # No run_fingerprint (older analyses / manual use) is allowed through unchanged.
        html = self._run(self._write(self._analysis()))
        self.assertIn("analyzed against the AI Fluency framework", html)

    def test_fingerprint_changes_with_the_data(self):
        other = tempfile.mkdtemp()
        write_session(other, "s.jsonl", [user_text("totally different prompt here")])
        fp2 = insight.analyze(insight.parse(insight.discover_files(other)))["fingerprint"]
        self.assertNotEqual(self.fp, fp2)

    def test_evidence_binding_matching_merges(self):
        # The real-pipeline path: --analysis-evidence is the bundle this run produced, so its
        # fingerprint matches and the (fingerprint-less) analysis merges — no LLM copy needed.
        ev = self._evidence_for(self.tmp)
        html = self._run(self._write(self._analysis()), evidence=ev)
        self.assertIn("analyzed against the AI Fluency framework", html)
        self.assertIn("UNIQUE-VERDICT-TOKEN", html)

    def test_evidence_binding_mismatch_rejects_and_does_not_leak(self):
        # Evidence built from DIFFERENT data: the fingerprint won't match this run, so even a
        # well-formed analysis is refused — this is what stops one person's verdict leaking.
        other = tempfile.mkdtemp()
        write_session(other, "s.jsonl", [user_text("an entirely different person's session"),
                                         user_text("with different prompts entirely")])
        foreign_ev = self._evidence_for(other)
        html = self._run(self._write(self._analysis()), evidence=foreign_ev)
        self.assertNotIn("UNIQUE-VERDICT-TOKEN", html)
        self.assertNotIn("analyzed against the AI Fluency framework", html)
        self.assertIn("Deterministic report only", html)


class TestNoTemplateMisframing(unittest.TestCase):
    """The deterministic report's generic teaching examples must be labeled as generic,
    and each user's report must carry that user's own evidence, not a shared template."""

    def _report_for(self, prompts):
        d = tempfile.mkdtemp()
        write_session(d, "s.jsonl", [user_text(p) for p in prompts])
        out = os.path.join(d, "r.html")
        rc = insight.main([d, "--no-open", "-o", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            return fh.read()

    def test_generic_examples_are_labeled_not_personalized(self):
        html = self._report_for(["fix it", "do the thing", "make it work", "change that"])
        # the canned before/after pairs must be explicitly flagged as not-from-your-sessions
        self.assertIn("not</b> from your sessions", html)

    def test_two_different_users_get_their_own_evidence(self):
        a = self._report_for(["fix the frobnicator", "fix the frobnicator now",
                              "update the frobnicator", "redo the frobnicator"])
        b = self._report_for(["build the gizmotron", "build the gizmotron now",
                              "update the gizmotron", "redo the gizmotron"])
        # each report surfaces its OWN distinctive prompts and not the other user's
        self.assertIn("frobnicator", a)
        self.assertNotIn("gizmotron", a)
        self.assertIn("gizmotron", b)
        self.assertNotIn("frobnicator", b)


class TestPersonalizedGrowthAndQuiet(unittest.TestCase):
    """The finished report must show Opus's TAILORED growth moves (not the generic teaching
    examples), and the measure pass must be silent so the score isn't surfaced early."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        write_session(self.tmp, "s.jsonl", [
            user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            user_text("run it"),
        ])

    def test_quiet_suppresses_the_score_summary(self):
        out = os.path.join(self.tmp, "r.html")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = insight.main([self.tmp, "--no-open", "--quiet", "-o", out])
        self.assertEqual(rc, 0)
        self.assertNotIn("AI Fluency Score", buf.getvalue())   # nothing surfaced to the user
        self.assertTrue(os.path.exists(out))                   # but the report was still written

    def test_opus_top_growth_replaces_generic_examples(self):
        analysis = {
            "overall_read": "Strong delegator; sharpen your briefs.",
            "skill_map": [
                {"competency": "Delegation", "level": 4, "level_label": "Advanced",
                 "summary": "Hands off whole jobs.", "evidence": ["scoped hand-off"], "next_move": "name intent"},
                {"competency": "Description", "level": 2, "level_label": "Developing",
                 "summary": "Terse.", "evidence": ["'run it'"], "next_move": "name a file + a constraint"},
                {"competency": "Discernment", "level": 3, "level_label": "Proficient",
                 "summary": "Reads first.", "evidence": ["read before edit"], "next_move": "verify after edits"},
                {"competency": "Diligence", "level": 3, "level_label": "Proficient",
                 "summary": "Owns sequencing.", "evidence": ["phase gate"], "next_move": "tear down"},
            ],
            "top_growth": [
                {"title": "Put a finish line on every hand-off", "why": "Your intent rate is low",
                 "how": "name what 'done' looks like",
                 "example_before": "run it",
                 "example_after": "TAILORED-REWRITE-TOKEN: run the server.py tests and paste the output"},
            ],
            "strengths": ["clear delegation"],
        }
        ap = os.path.join(self.tmp, "an.json")
        with open(ap, "w", encoding="utf-8") as fh:
            json.dump(analysis, fh)
        out = os.path.join(self.tmp, "r.html")
        rc = insight.main([self.tmp, "--analysis", ap, "--no-open", "-o", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        # the personalized growth card is rendered, with Opus's tailored rewrite of a real prompt
        self.assertIn("TAILORED-REWRITE-TOKEN", html)
        self.assertIn("written for you", html)
        self.assertIn("Tailored rewrite for you", html)
        # and the generic stock example is NOT in the improve section anymore
        self.assertNotIn("session cookie", html)

    def test_without_analysis_generic_examples_are_present_but_labeled(self):
        out = os.path.join(self.tmp, "r.html")
        rc = insight.main([self.tmp, "--no-open", "-o", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        # no AI ran -> the generic teaching examples appear, explicitly flagged as generic
        self.assertIn("not</b> from your sessions", html)


class TestMcpServerGrouping(unittest.TestCase):
    """MCP calls are grouped by server-id (plugin infix stripped) without disturbing tool_usage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_mcp_server_grouping(self):
        recs = [
            assistant_tool("mcp__plugin_sre_jaeger-qa__find-traces"),
            assistant_tool("mcp__plugin_engram_engram__mem_save"),
            assistant_tool("mcp__grafana-qa__query_prometheus"),
            assistant_tool("Read", file_path="/x.py"),  # non-MCP, must not appear as a server
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        self.assertEqual(corpus.mcp_servers["jaeger-qa"], 1)
        self.assertEqual(corpus.mcp_servers["engram"], 1)
        self.assertEqual(corpus.mcp_servers["grafana-qa"], 1)
        # the plugin_ infix never leaks through
        self.assertNotIn("plugin_sre_jaeger-qa", corpus.mcp_servers)
        self.assertNotIn("plugin_engram_engram", corpus.mcp_servers)
        # a non-MCP tool contributes no server entry
        self.assertNotIn("Read", corpus.mcp_servers)
        # tool_usage still holds the de-namespaced tool names, unchanged
        self.assertEqual(corpus.tool_usage["find-traces"], 1)
        self.assertEqual(corpus.tool_usage["mem_save"], 1)
        self.assertEqual(corpus.tool_usage["query_prometheus"], 1)
        self.assertEqual(corpus.tool_usage["Read"], 1)

    def test_mcp_server_unit(self):
        self.assertEqual(insight._mcp_server("mcp__plugin_sre_jaeger-qa__find-traces"), "jaeger-qa")
        self.assertEqual(insight._mcp_server("mcp__plugin_engram_engram__mem_save"), "engram")
        self.assertEqual(insight._mcp_server("mcp__grafana-qa__query_prometheus"), "grafana-qa")
        self.assertIsNone(insight._mcp_server("Read"))
        self.assertIsNone(insight._mcp_server("Bash"))


class TestCommandCounting(unittest.TestCase):
    """Slash-command stubs are counted while remaining filtered out of real_prompts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_command_counting(self):
        recs = [
            user_text("<command-name>/foo</command-name>"),
            user_text("<command-name>/desplega:research</command-name>"),
            user_text("<command-name>/foo</command-name>"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        self.assertEqual(corpus.commands["/foo"], 2)
        self.assertEqual(corpus.commands["/desplega:research"], 1)
        # command stubs are still filtered from the scored corpus
        self.assertEqual(len(corpus.real_prompts), 0)

    def test_command_name_unit(self):
        self.assertEqual(insight._command_name("<command-name>/foo</command-name>"), "/foo")
        self.assertEqual(insight._command_name("<command-name> /desplega:research </command-name>"),
                         "/desplega:research")
        self.assertIsNone(insight._command_name("just a normal prompt"))


class TestRealPromptsRegressionGuard(unittest.TestCase):
    """real_prompts is byte-identical with or without command/MCP stubs mixed in."""

    def setUp(self):
        self.tmp_a = tempfile.mkdtemp()
        self.tmp_b = tempfile.mkdtemp()

    def test_real_prompts_regression_identical(self):
        real_only = [
            user_text("add a login endpoint to api.py"),
            user_text("run the tests"),
        ]
        with_stubs = [
            user_text("<command-name>/foo</command-name>"),       # command stub
            user_text("add a login endpoint to api.py"),
            assistant_tool("mcp__plugin_engram_engram__mem_save"),  # mcp call
            user_text("<command-name>/desplega:research</command-name>"),
            user_text("run the tests"),
            assistant_tool("mcp__grafana-qa__query_prometheus"),
        ]
        write_session(self.tmp_a, "s.jsonl", real_only)
        write_session(self.tmp_b, "s.jsonl", with_stubs)
        a = insight.parse(insight.discover_files(self.tmp_a))
        b = insight.parse(insight.discover_files(self.tmp_b))
        a_texts = [p["text"] for p in a.real_prompts]
        b_texts = [p["text"] for p in b.real_prompts]
        # same length AND same contents — the additive guarantee at the corpus level
        self.assertEqual(len(a_texts), len(b_texts))
        self.assertEqual(a_texts, b_texts)
        # and the new counters DID pick up the stubs in the b run
        self.assertEqual(b.commands["/foo"], 1)
        self.assertEqual(b.mcp_servers["engram"], 1)


class TestWorkTypeBuckets(unittest.TestCase):
    """classify_work_types buckets timeline TOOL events into the 7-way work-type mix."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_work_type_buckets(self):
        # 4 edits (Build), 3 verify-cmds (Debug), 2 plan-skills (Plan), 1 read (Investigate) -> 10.
        recs = (
            [assistant_tool("Write", file_path="/a.py")] * 2
            + [assistant_tool("Edit", file_path="/b.py")] * 2
            + [assistant_tool("Bash", command="python -m pytest -q")] * 3
            + [assistant_tool("ExitPlanMode")] * 2          # name 'exitplanmode' contains 'plan'
            + [assistant_tool("Read", file_path="/c.py")]   # read tool -> Investigate
        )
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        mix = insight.classify_work_types(corpus)
        self.assertEqual(mix["counts"]["Build"], 4)
        self.assertEqual(mix["counts"]["Debug"], 3)
        self.assertEqual(mix["counts"]["Plan"], 2)
        self.assertEqual(mix["counts"]["Investigate"], 1)
        self.assertEqual(mix["counts"]["Other"], 0)
        self.assertAlmostEqual(mix["pct"]["Build"], 40.0)
        self.assertAlmostEqual(sum(mix["pct"].values()), 100.0)

    def test_work_type_investigate_shell_delegate(self):
        # The buckets that used to vanish into "Other": MCP queries + reads -> Investigate,
        # non-verify bash -> Shell/Ops, agent spawns -> Delegate.
        recs = [
            assistant_tool("mcp__plugin_sre_grafana-prod__query_prometheus"),  # MCP -> Investigate
            assistant_tool("Grep", pattern="foo"),                            # read tool -> Investigate
            assistant_tool("Bash", command="git status"),                     # non-verify -> Shell/Ops
            assistant_tool("Agent", subagent_type="sre:sre-investigator"),    # spawn -> Delegate
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        mix = insight.classify_work_types(corpus)
        self.assertEqual(mix["counts"]["Investigate"], 2)
        self.assertEqual(mix["counts"]["Shell/Ops"], 1)
        self.assertEqual(mix["counts"]["Delegate"], 1)
        self.assertEqual(mix["counts"]["Other"], 0)

    def test_work_type_excludes_prompts(self):
        # Work-type measures actions, not chat: plain prompts must not land in any bucket.
        recs = [
            user_text("just chatting, no command"),
            user_text("another plain message"),
            assistant_tool("Write", file_path="/a.py"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        mix = insight.classify_work_types(corpus)
        self.assertEqual(sum(mix["counts"].values()), 1)   # only the Write tool event
        self.assertEqual(mix["counts"]["Build"], 1)

    def test_work_type_precedence_build_over_debug(self):
        # an edit tool that somehow also carries a verify cmd is Build, not Debug (precedence).
        recs = [assistant_tool("Write", file_path="/a.py", command="pytest")]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        mix = insight.classify_work_types(corpus)
        # Write does not capture `command` (only Bash does), so cmd is None and this is plain Build;
        # the assertion still guards that an edit lands in Build.
        self.assertEqual(mix["counts"]["Build"], 1)
        self.assertEqual(mix["counts"]["Debug"], 0)

    def test_work_type_plan_from_commands(self):
        # Planning runs through slash commands, not timeline events: a plan-ish command must
        # land in Plan even with no plan-named timeline event. 1 edit (Build) + 1 read (Other)
        # in the timeline, plus /desplega:research x2 and /plan x1 in corpus.commands -> Plan 3.
        recs = [
            user_text("<command-name>/desplega:research</command-name>"),
            assistant_tool("Write", file_path="/a.py"),
            assistant_tool("Read", file_path="/c.py"),
            user_text("<command-name>/desplega:research</command-name>"),
            user_text("<command-name>/plan</command-name>"),
            user_text("<command-name>/commit</command-name>"),   # not plan-ish -> ignored
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        mix = insight.classify_work_types(corpus)
        self.assertEqual(mix["counts"]["Plan"], 3)
        self.assertEqual(mix["counts"]["Build"], 1)
        self.assertEqual(mix["counts"]["Investigate"], 1)   # the Read
        self.assertAlmostEqual(sum(mix["pct"].values()), 100.0)

    def test_work_type_empty_corpus(self):
        corpus = insight.Corpus()
        mix = insight.classify_work_types(corpus)
        self.assertEqual(set(mix["counts"]), set(insight.WORK_TYPE_BUCKETS))
        self.assertEqual(sum(mix["counts"].values()), 0)
        self.assertEqual(sum(mix["pct"].values()), 0.0)


def _write_claude_config(root, installed, marketplaces, enabled, catalogs=None,
                         installed_versions=None):
    """Write fake ~/.claude config (installed_plugins / known_marketplaces / settings).

    Optional:
      installed_versions: {plugin_id -> version} overriding the default "1.0.0" install record.
      catalogs: {marketplace_name -> [{"name","version"}, ...]} — writes each marketplace's
        local clone manifest at plugins/marketplaces/<name>/.claude-plugin/marketplace.json,
        the source build_adoption reads "latest" versions from."""
    plugins_dir = os.path.join(root, "plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    installed_versions = installed_versions or {}
    # installed_plugins: plugins[id] is a LIST of install records (we index [0]).
    inst = {"version": 1,
            "plugins": {pid: [{"version": installed_versions.get(pid, "1.0.0")}]
                        for pid in installed}}
    with open(os.path.join(plugins_dir, "installed_plugins.json"), "w", encoding="utf-8") as f:
        json.dump(inst, f)
    mkt = {name: {"source": {"source": "github", "repo": repo}}
           for name, repo in marketplaces.items()}
    with open(os.path.join(plugins_dir, "known_marketplaces.json"), "w", encoding="utf-8") as f:
        json.dump(mkt, f)
    with open(os.path.join(root, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"enabledPlugins": enabled}, f)
    for name, plugins in (catalogs or {}).items():
        cdir = os.path.join(plugins_dir, "marketplaces", name, ".claude-plugin")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "marketplace.json"), "w", encoding="utf-8") as f:
            json.dump({"plugins": plugins}, f)


class TestAdoption(unittest.TestCase):
    """build_adoption joins offline config provenance with corpus usage into the trichotomy."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp()   # fake ~/.claude
        self.tmp = tempfile.mkdtemp()   # transcript dir
        # Hermetic: point the MCP-server loader at a file inside the tmpdir so context7's
        # `installed` never reads the real host ~/.claude.json (host-dependent / flaky).
        self.claude_json = os.path.join(self.cfg, "claude.json")

    def _corpus(self, recs):
        write_session(self.tmp, "s.jsonl", recs)
        return insight.parse(insight.discover_files(self.tmp))

    def test_adoption_trichotomy(self):
        # engram: installed + enabled + used (mem_* calls)
        # ponytail: installed + DISABLED (used > 0, but enabled flag is False)
        # obsidian: installed + enabled + NEVER USED (used == 0)
        _write_claude_config(
            self.cfg,
            installed=["engram@engram", "ponytail@ponytail", "obsidian@obsidian-skills"],
            marketplaces={"engram": "Gentleman-Programming/engram",
                          "ponytail": "DietrichGebert/ponytail",
                          "obsidian-skills": "kepano/obsidian-skills"},
            enabled={"engram@engram": True, "obsidian@obsidian-skills": True},  # ponytail absent/False
        )
        corpus = self._corpus([
            assistant_tool("mcp__plugin_engram_engram__mem_save"),
            assistant_tool("mcp__plugin_engram_engram__mem_search"),
            user_text("<command-name>/ponytail:ponytail-review</command-name>"),
        ])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)

        # installed + enabled + used
        self.assertTrue(a["engram"]["installed"])
        self.assertTrue(a["engram"]["enabled"])
        self.assertEqual(a["engram"]["used"], 2)

        # installed + disabled (used > 0 but not enabled)
        self.assertTrue(a["ponytail"]["installed"])
        self.assertFalse(a["ponytail"]["enabled"])
        self.assertEqual(a["ponytail"]["used"], 1)

        # installed + never used (the strongest coaching signal)
        self.assertTrue(a["obsidian"]["installed"])
        self.assertTrue(a["obsidian"]["enabled"])
        self.assertEqual(a["obsidian"]["used"], 0)

    def test_adoption_obsidian_cli_used(self):
        # The crabi-obsidian-notes skill drives Obsidian via the `obsidian vault=…` CLI
        # (a Bash command), not `/obsidian:` slash-commands. Those Bash calls must count as
        # USED, else heavy real usage falsely reads as "installed but never used".
        _write_claude_config(
            self.cfg,
            installed=["obsidian@obsidian-skills"],
            marketplaces={"obsidian-skills": "kepano/obsidian-skills"},
            enabled={"obsidian@obsidian-skills": True},
        )
        corpus = self._corpus([
            assistant_tool("Bash", command='obsidian vault="Obsidian Vault" create path="x.md" content="y" silent'),
            assistant_tool("Bash", command='cd "/Users/x/Obsidian Vault" && obsidian vault="Obsidian Vault" search query="z"'),
            assistant_tool("Bash", command='cd "/Users/x/Obsidian Vault" && git commit -m "note"'),  # not the CLI
        ])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertTrue(a["obsidian"]["installed"])
        # Two `obsidian vault=` invocations (one chained after &&) count; the `git commit`
        # whose path contains capital-O "Obsidian Vault" must NOT false-match.
        self.assertEqual(a["obsidian"]["used"], 2)

    def test_adoption_provenance_gate(self):
        # A plugin from a NON-crabi marketplace must NOT flag the crabi/ai-marketplace target,
        # even if it shares a plugin base-name. Provenance is the gate, not the name.
        _write_claude_config(
            self.cfg,
            installed=["sre@some-other-marketplace"],
            marketplaces={"some-other-marketplace": "someone-else/not-crabi"},
            enabled={"sre@some-other-marketplace": True},
        )
        corpus = self._corpus([user_text("<command-name>/sre:diagnose-service</command-name>")])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertFalse(a["crabi/ai-marketplace"]["installed"])
        self.assertIsNone(a["crabi/ai-marketplace"]["enabled"])
        self.assertEqual(a["crabi/ai-marketplace"]["used"], 0)

    def test_adoption_crabi_provenance_join(self):
        # The crabi target is flagged purely by source.repo == crabi/ai-marketplace, and USED
        # counts that plugin's slash-command invocations (namespace = plugin name).
        _write_claude_config(
            self.cfg,
            installed=["sre@crabi-ai-marketplace", "qa@crabi-ai-marketplace",
                       "desplega@desplega-ai-toolbox"],
            marketplaces={"crabi-ai-marketplace": "crabi/ai-marketplace",
                          "desplega-ai-toolbox": "desplega-ai/ai-toolbox"},
            enabled={"sre@crabi-ai-marketplace": True},   # qa present but not enabled
        )
        corpus = self._corpus([
            user_text("<command-name>/sre:diagnose-service</command-name>"),
            user_text("<command-name>/sre:query-logs</command-name>"),
            user_text("<command-name>/desplega:research</command-name>"),  # not a crabi plugin
        ])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        crabi = a["crabi/ai-marketplace"]
        self.assertTrue(crabi["installed"])
        self.assertTrue(crabi["enabled"])           # at least one crabi plugin enabled
        self.assertEqual(crabi["used"], 2)          # two /sre: invocations, /desplega excluded
        self.assertEqual(crabi["plugins"], ["qa", "sre"])
        # desplega counts on its own target, NOT the crabi one.
        self.assertEqual(a["desplega"]["used"], 1)

    def test_adoption_crabi_plugin_breakdown(self):
        # Per-plugin used/never-used across ALL channels: slash (/sre:), MCP (mcp__plugin_data_…),
        # and subagent (qa:crabi-qa). 'docs' is installed but never invoked -> used 0.
        _write_claude_config(
            self.cfg,
            installed=["sre@crabi-ai-marketplace", "data@crabi-ai-marketplace",
                       "qa@crabi-ai-marketplace", "docs@crabi-ai-marketplace"],
            marketplaces={"crabi-ai-marketplace": "crabi/ai-marketplace"},
            enabled={"sre@crabi-ai-marketplace": True, "data@crabi-ai-marketplace": True,
                     "qa@crabi-ai-marketplace": True, "docs@crabi-ai-marketplace": True},
        )
        corpus = self._corpus([
            user_text("<command-name>/sre:query-logs</command-name>"),    # sre via slash
            assistant_tool("mcp__plugin_data_lineage__get_table"),        # data via MCP
            assistant_tool("mcp__plugin_data_lineage__get_table"),        # data via MCP (x2)
            assistant_tool("Agent", subagent_type="qa:crabi-qa"),         # qa via subagent
            # docs: never invoked
        ])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        bd = {p["name"]: p["used"] for p in a["crabi/ai-marketplace"]["plugin_breakdown"]}
        self.assertEqual(bd["sre"], 1)    # slash channel
        self.assertEqual(bd["data"], 2)   # MCP channel — would read 0 under namespace-only
        self.assertEqual(bd["qa"], 1)     # subagent channel
        self.assertEqual(bd["docs"], 0)   # installed but never used
        self.assertEqual(a["crabi/ai-marketplace"]["used"], 4)  # aggregate is the sum

    def test_adoption_missing_config_returns_empty(self):
        # Pointed at a dir with NO config files: readers return {} and build_adoption never raises.
        empty = tempfile.mkdtemp()
        self.assertEqual(insight._load_installed_plugins(empty), {})
        self.assertEqual(insight._load_known_marketplaces(empty), {})
        self.assertEqual(insight._load_enabled_plugins(empty), {})
        a = insight.build_adoption(insight.Corpus(), claude_dir=empty,
                                   claude_json=os.path.join(empty, "claude.json"))
        # crabi target degrades gracefully: not installed, enabled None, zero used.
        self.assertFalse(a["crabi/ai-marketplace"]["installed"])
        self.assertIsNone(a["crabi/ai-marketplace"]["enabled"])
        # named targets likewise: not installed.
        for t in ("engram", "ponytail", "obsidian", "desplega"):
            self.assertFalse(a[t]["installed"])
            self.assertIsNone(a[t]["enabled"])

    def test_adoption_missing_handles_malformed_json(self):
        # Malformed JSON in each config file must NOT raise — readers swallow ValueError.
        plugins_dir = os.path.join(self.cfg, "plugins")
        os.makedirs(plugins_dir, exist_ok=True)
        with open(os.path.join(plugins_dir, "installed_plugins.json"), "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ")
        with open(os.path.join(plugins_dir, "known_marketplaces.json"), "w", encoding="utf-8") as f:
            f.write("]]] broken")
        with open(os.path.join(self.cfg, "settings.json"), "w", encoding="utf-8") as f:
            f.write("not json at all")
        self.assertEqual(insight._load_installed_plugins(self.cfg), {})
        self.assertEqual(insight._load_known_marketplaces(self.cfg), {})
        self.assertEqual(insight._load_enabled_plugins(self.cfg), {})
        # And the full builder still produces a well-formed (all-negative) result.
        a = insight.build_adoption(insight.Corpus(), claude_dir=self.cfg,
                                   claude_json=self.claude_json)
        self.assertFalse(a["crabi/ai-marketplace"]["installed"])

    # --- context7 (MCP-server target) ------------------------------------------------
    def _write_claude_json(self, obj):
        """Write a fake ~/.claude.json at self.claude_json."""
        with open(self.claude_json, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_context7_installed_via_top_level_mcp_servers(self):
        # SECRET SAFETY: the entry value carries a token; only the KEY name may be read.
        self._write_claude_json(
            {"mcpServers": {"context7": {"url": "https://x", "headers": {"Authorization": "SECRET"}}}})
        a = insight.build_adoption(self._corpus([user_text("hi")]),
                                   claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertTrue(a["context7"]["installed"])

    def test_context7_installed_via_project_mcp_servers_only(self):
        self._write_claude_json(
            {"projects": {"/some/proj": {"mcpServers": {"context7": {"command": "npx"}}}}})
        a = insight.build_adoption(self._corpus([user_text("hi")]),
                                   claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertTrue(a["context7"]["installed"])

    def test_context7_installed_via_project_mcp_json_only(self):
        # A project's own .mcp.json declares context7; nothing in ~/.claude.json's mcpServers.
        proj = tempfile.mkdtemp()
        with open(os.path.join(proj, ".mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"context7": {"command": "npx"}}}, f)
        self._write_claude_json({"projects": {proj: {}}})
        a = insight.build_adoption(self._corpus([user_text("hi")]),
                                   claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertTrue(a["context7"]["installed"])

    def test_context7_not_installed_when_absent(self):
        # No .claude.json at all -> loader returns set() -> not installed, enabled None.
        a = insight.build_adoption(self._corpus([user_text("hi")]),
                                   claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertFalse(a["context7"]["installed"])
        self.assertIsNone(a["context7"]["enabled"])
        # And a config that exists but has no context7 key is likewise not installed.
        self._write_claude_json({"mcpServers": {"some-other-server": {"command": "x"}}})
        a2 = insight.build_adoption(self._corpus([user_text("hi")]),
                                    claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertFalse(a2["context7"]["installed"])
        self.assertIsNone(a2["context7"]["enabled"])

    def test_context7_used_counts_direct_and_connector(self):
        # Both the direct MCP server (context7) and the claude.ai connector
        # (claude_ai_Context7) count toward used; a non-context7 server does NOT.
        corpus = self._corpus([
            assistant_tool("mcp__context7__query-docs"),
            assistant_tool("mcp__claude_ai_Context7__query-docs"),
            assistant_tool("mcp__grafana-qa__query_prometheus"),  # unrelated server — no count
        ])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertEqual(a["context7"]["used"], 2)

    def test_context7_version_fields_all_none(self):
        self._write_claude_json({"mcpServers": {"context7": {"command": "npx"}}})
        a = insight.build_adoption(self._corpus([user_text("hi")]),
                                   claude_dir=self.cfg, claude_json=self.claude_json)
        c7 = a["context7"]
        self.assertIsNone(c7["installed_version"])
        self.assertIsNone(c7["latest_version"])
        self.assertIsNone(c7["outdated"])
        self.assertIsNone(c7["enabled"])

    def test_plugin_targets_unaffected_by_mcp_refactor(self):
        # Regression for the sig.get("plugin_id") refactor: plugin-backed targets still
        # resolve installed/enabled/used exactly as before.
        _write_claude_config(
            self.cfg,
            installed=["engram@engram"],
            marketplaces={"engram": "Gentleman-Programming/engram"},
            enabled={"engram@engram": True},
        )
        corpus = self._corpus([
            assistant_tool("mcp__plugin_engram_engram__mem_save"),
        ])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertTrue(a["engram"]["installed"])
        self.assertTrue(a["engram"]["enabled"])
        self.assertEqual(a["engram"]["used"], 1)


class TestUsageSectionHtml(unittest.TestCase):
    """_usage_section_html renders the combined Usage & Adoption block from fixture data."""

    def _usage(self):
        from collections import Counter
        return {
            "mcp_servers": Counter({"engram": 12, "grafana-qa": 3}),
            "commands": Counter({"/desplega:research": 5, "/commit": 2}),
            "work_types": {
                "counts": {"Build": 4, "Debug": 3, "Plan": 2, "Other": 1},
                "pct": {"Build": 40.0, "Debug": 30.0, "Plan": 20.0, "Other": 10.0},
            },
        }

    def test_usage_section_html(self):
        adoption = {
            "crabi/ai-marketplace": {"installed": True, "enabled": True, "used": 7,
                                     "plugins": ["sre", "qa"]},
            "engram": {"installed": True, "enabled": True, "used": 12},
            "obsidian": {"installed": True, "enabled": True, "used": 0},  # installed but never used
        }
        html = insight._usage_section_html(self._usage(), adoption, span_days=47)

        # server display name from SERVER_DISPLAY
        self.assertIn("Engram", html)
        self.assertIn("Grafana (QA)", html)
        # honest "over N days" span label, not a hard-coded 30
        self.assertIn("over the last 47 days", html)
        # a work-type label
        self.assertIn("Build", html)
        self.assertIn("Work-type mix", html)
        # a slash command rendered
        self.assertIn("/desplega:research", html)
        # adoption: installed + the unused marker for the never-used tool
        self.assertIn("installed", html)
        self.assertIn("never used", html)
        # the section heading
        self.assertIn("Usage", html)

    def test_usage_section_empty_payload_renders_nothing(self):
        self.assertEqual(insight._usage_section_html({}, {}, span_days=0), "")

    def test_usage_section_no_span_uses_history_label(self):
        html = insight._usage_section_html(self._usage(), {}, span_days=0)
        self.assertIn("over your history", html)
        self.assertNotIn("over the last 0 days", html)


class TestEvidenceAdoption(unittest.TestCase):
    """build_evidence carries the descriptive usage/adoption block, and that block must
    NOT change run_fingerprint (which is keyed only on real_prompts)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _bundle(self):
        recs = [
            user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            assistant_tool("mcp__plugin_engram_engram__mem_save"),
            user_text("run it"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        result = insight.analyze(corpus)
        cards, _strength = insight.build_action_plan(corpus, result)
        return corpus, insight.build_evidence(corpus, result, cards)

    def test_evidence_adoption_block_present(self):
        _corpus, ev = self._bundle()
        behavior = ev["behavior"]
        self.assertIn("usage", behavior)
        self.assertIn("adoption", behavior)
        usage = behavior["usage"]
        # the engram mem_save call grouped under its server
        self.assertEqual(usage["mcp_servers"].get("engram"), 1)
        for k in ("mcp_servers", "commands", "work_types"):
            self.assertIn(k, usage)
        # adoption carries the crabi target plus the named tools, with the trichotomy keys
        self.assertIn("crabi/ai-marketplace", behavior["adoption"])
        for tgt, state in behavior["adoption"].items():
            self.assertIn("installed", state)
            self.assertIn("used", state)

    def test_evidence_adoption_fingerprint_stable(self):
        # The merge-gate hashes only real_prompts; the descriptive usage/adoption block
        # must never shift it. Compare the fingerprint of the corpus to a re-parse that
        # carries the same prompts but extra MCP/command stubs (which feed usage/adoption).
        corpus, _ev = self._bundle()
        fp_plain = insight._run_fingerprint(corpus)

        other = tempfile.mkdtemp()
        write_session(other, "s.jsonl", [
            user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            assistant_tool("mcp__plugin_engram_engram__mem_save"),
            assistant_tool("mcp__plugin_engram_engram__mem_search"),       # extra usage signal
            user_text("<command-name>/desplega:research</command-name>"),  # extra command signal
            user_text("run it"),
        ])
        corpus2 = insight.parse(insight.discover_files(other))
        # the extra stubs DID feed usage/adoption ...
        self.assertGreater(insight.build_evidence(
            corpus2, insight.analyze(corpus2),
            insight.build_action_plan(corpus2, insight.analyze(corpus2))[0]
        )["behavior"]["usage"]["mcp_servers"].get("engram"), 1)
        # ... but the fingerprint is identical: same real_prompts, byte-for-byte.
        self.assertEqual(fp_plain, insight._run_fingerprint(corpus2))


class TestVersionCapture(unittest.TestCase):
    """corpus.cc_version is the version off the LATEST event that actually carries one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _corpus(self, recs):
        write_session(self.tmp, "s.jsonl", recs)
        return insight.parse(insight.discover_files(self.tmp))

    def test_latest_versioned_event_wins(self):
        corpus = self._corpus([
            user_text("first", ts="2026-01-01T00:00:00Z", version="2.1.100"),
            user_text("later", ts="2026-01-02T00:00:00Z", version="2.1.185"),
        ])
        self.assertEqual(corpus.cc_version, "2.1.185")

    def test_none_when_no_version_key(self):
        corpus = self._corpus([
            user_text("a", ts="2026-01-01T00:00:00Z"),
            user_text("b", ts="2026-01-02T00:00:00Z"),
        ])
        self.assertIsNone(corpus.cc_version)

    def test_earlier_version_wins_when_latest_event_unversioned(self):
        # The MAX-ts event has no `version`; an earlier one does. cc_version must be the
        # earlier (non-None) version, never None (regression guard for the ~28% unversioned).
        corpus = self._corpus([
            user_text("has version", ts="2026-01-01T00:00:00Z", version="2.1.150"),
            user_text("newest, no version", ts="2026-01-03T00:00:00Z"),
        ])
        self.assertEqual(corpus.cc_version, "2.1.150")


class TestOutdated(unittest.TestCase):
    """_version_outdated: dotted-int compare with unknown -> None (never a false claim)."""

    def test_installed_behind_latest_is_true(self):
        self.assertIs(insight._version_outdated("0.1.0", "0.1.1"), True)

    def test_equal_is_false(self):
        self.assertIs(insight._version_outdated("0.1.1", "0.1.1"), False)

    def test_installed_ahead_is_false(self):
        self.assertIs(insight._version_outdated("0.2.0", "0.1.1"), False)

    def test_unknown_or_missing_or_unparseable_is_none(self):
        self.assertIsNone(insight._version_outdated("unknown", "0.1.1"))
        self.assertIsNone(insight._version_outdated("0.1.0", None))
        self.assertIsNone(insight._version_outdated(None, "0.1.0"))
        self.assertIsNone(insight._version_outdated("0.1.0", "unknown"))
        self.assertIsNone(insight._version_outdated("not-a-version", "0.1.0"))

    def test_breakdown_entry_marks_outdated(self):
        cfg = tempfile.mkdtemp()
        tmp = tempfile.mkdtemp()
        _write_claude_config(
            cfg,
            installed=["sre@crabi-ai-marketplace"],
            marketplaces={"crabi-ai-marketplace": "crabi/ai-marketplace"},
            enabled={"sre@crabi-ai-marketplace": True},
            installed_versions={"sre@crabi-ai-marketplace": "0.1.0"},
            catalogs={"crabi-ai-marketplace": [{"name": "sre", "version": "0.1.1"}]},
        )
        write_session(tmp, "s.jsonl", [user_text("hi")])
        corpus = insight.parse(insight.discover_files(tmp))
        a = insight.build_adoption(corpus, claude_dir=cfg,
                                   claude_json=os.path.join(cfg, "claude.json"))
        sre = next(p for p in a["crabi/ai-marketplace"]["plugin_breakdown"] if p["name"] == "sre")
        self.assertEqual(sre["installed_version"], "0.1.0")
        self.assertEqual(sre["latest_version"], "0.1.1")
        self.assertIs(sre["outdated"], True)


class TestFullCatalogAdoption(unittest.TestCase):
    """build_adoption emits a breakdown over the UNION of catalog + installed plugins."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.tmp = tempfile.mkdtemp()
        self.claude_json = os.path.join(self.cfg, "claude.json")  # hermetic MCP-server read

    def _corpus(self, recs):
        write_session(self.tmp, "s.jsonl", recs)
        return insight.parse(insight.discover_files(self.tmp))

    def test_not_installed_catalog_plugin_appears(self):
        # Catalog lists sre + infra; only sre is installed. infra must show up as
        # installed=False / used=0 (the real not-installed coaching case).
        _write_claude_config(
            self.cfg,
            installed=["sre@crabi-ai-marketplace"],
            marketplaces={"crabi-ai-marketplace": "crabi/ai-marketplace"},
            enabled={"sre@crabi-ai-marketplace": True},
            catalogs={"crabi-ai-marketplace": [
                {"name": "sre", "version": "0.1.0"},
                {"name": "infra", "version": "0.2.0"},
            ]},
        )
        corpus = self._corpus([user_text("<command-name>/sre:query-logs</command-name>")])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        bd = {p["name"]: p for p in a["crabi/ai-marketplace"]["plugin_breakdown"]}
        self.assertIn("infra", bd)
        self.assertFalse(bd["infra"]["installed"])
        self.assertEqual(bd["infra"]["used"], 0)
        self.assertIsNone(bd["infra"]["installed_version"])
        self.assertEqual(bd["infra"]["latest_version"], "0.2.0")
        # installed plugin carries installed=True + its install-record version.
        self.assertTrue(bd["sre"]["installed"])
        self.assertEqual(bd["sre"]["used"], 1)
        self.assertEqual(bd["sre"]["installed_version"], "1.0.0")

    def test_missing_manifest_falls_back_to_installed_only(self):
        # No catalog written -> _load_marketplace_catalog returns {} -> breakdown is the
        # installed crabi plugins only, with no error.
        _write_claude_config(
            self.cfg,
            installed=["sre@crabi-ai-marketplace", "qa@crabi-ai-marketplace"],
            marketplaces={"crabi-ai-marketplace": "crabi/ai-marketplace"},
            enabled={"sre@crabi-ai-marketplace": True},
        )
        corpus = self._corpus([user_text("hi")])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        names = sorted(p["name"] for p in a["crabi/ai-marketplace"]["plugin_breakdown"])
        self.assertEqual(names, ["qa", "sre"])  # installed-only, no catalog extras
        for p in a["crabi/ai-marketplace"]["plugin_breakdown"]:
            self.assertTrue(p["installed"])
            self.assertIsNone(p["latest_version"])

    def test_suggested_tool_carries_version_and_outdated(self):
        # A named-tool target (engram) gets installed/latest versions + outdated, resolved
        # via its marketplace NAME (the plugin_id suffix), not a repo slug.
        _write_claude_config(
            self.cfg,
            installed=["engram@engram"],
            marketplaces={"engram": "Gentleman-Programming/engram"},
            enabled={"engram@engram": True},
            installed_versions={"engram@engram": "0.1.0"},
            catalogs={"engram": [{"name": "engram", "version": "0.1.1"}]},
        )
        corpus = self._corpus([user_text("hi")])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertEqual(a["engram"]["installed_version"], "0.1.0")
        self.assertEqual(a["engram"]["latest_version"], "0.1.1")
        self.assertIs(a["engram"]["outdated"], True)

    def test_superpowers_signature_present_and_not_installed(self):
        # superpowers is a suggested tool but not installed here -> installed False, enabled None.
        self.assertIn("superpowers", insight.SIGNATURES)
        corpus = self._corpus([user_text("hi")])
        a = insight.build_adoption(corpus, claude_dir=self.cfg, claude_json=self.claude_json)
        self.assertIn("superpowers", a)
        self.assertFalse(a["superpowers"]["installed"])
        self.assertIsNone(a["superpowers"]["enabled"])


class TestMetaLine(unittest.TestCase):
    """The header meta line surfaces evaluated date + CC version + (guarded) activity window."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _render(self, recs, generated_at, cc_version=None, null_window=False):
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        if cc_version is not None:
            corpus.cc_version = cc_version
        if null_window:
            corpus.first_ts = None
            corpus.last_ts = None
        result = insight.analyze(corpus)
        cards, strength = insight.build_action_plan(corpus, result)
        return insight.build_html(corpus, result, cards, strength, generated_at=generated_at)

    def test_meta_line_renders_date_version_window(self):
        gen = datetime.datetime(2026, 6, 21, 9, 30)
        html = self._render(
            [user_text("add a /health endpoint to server.py", ts="2026-01-01T00:00:00Z"),
             user_text("run it", ts="2026-01-08T00:00:00Z")],
            generated_at=gen, cc_version="2.1.185")
        self.assertIn("Evaluated 2026-06-21", html)
        self.assertIn("Claude Code v2.1.185", html)
        # activity-window range with both endpoints present
        self.assertIn("activity 2026-01-01", html)
        self.assertIn("2026-01-08", html)

    def test_meta_line_without_timestamps_omits_window(self):
        # A corpus with no first/last ts must still render date + version, and NOT crash on a
        # None date or print the activity-window segment.
        gen = datetime.datetime(2026, 6, 21)
        html = self._render([user_text("hi")], generated_at=gen, cc_version="2.0.0",
                            null_window=True)
        # the meta line itself is exactly date + version, with no activity-window segment
        self.assertIn('<p class="meta">Evaluated 2026-06-21 · Claude Code v2.0.0</p>', html)
        # the activity-window segment (` · activity <date> → <date>`) never rendered
        self.assertNotIn("· activity", html)

    def test_meta_line_unknown_version_renders_dash(self):
        gen = datetime.datetime(2026, 6, 21)
        html = self._render([user_text("hi")], generated_at=gen, cc_version=None)
        self.assertIn("Claude Code v—", html)


class TestTwoSectionAdoption(unittest.TestCase):
    """_usage_section_html renders both 'suggested plugins' and 'marketplace' sub-sections,
    with not-installed / superpowers / outdated markers."""

    def _usage(self):
        from collections import Counter
        return {"mcp_servers": Counter({"engram": 3}), "commands": Counter(), "work_types": {}}

    def test_two_sub_sections_with_status_and_outdated(self):
        adoption = {
            # named-tool (suggested) targets
            "engram": {"installed": True, "enabled": True, "used": 3,
                       "installed_version": "0.1.0", "latest_version": "0.1.1", "outdated": True},
            "superpowers": {"installed": False, "enabled": None, "used": 0,
                            "installed_version": None, "latest_version": None, "outdated": None},
            # marketplace catalog
            "crabi/ai-marketplace": {
                "installed": True, "enabled": True, "used": 2,
                "plugin_breakdown": [
                    {"name": "sre", "installed": True, "used": 2,
                     "installed_version": "0.1.0", "latest_version": "0.1.0", "outdated": False},
                    {"name": "infra", "installed": False, "used": 0,
                     "installed_version": None, "latest_version": "0.2.0", "outdated": None},
                    {"name": "qa", "installed": True, "used": 0,
                     "installed_version": "0.1.0", "latest_version": "0.2.0", "outdated": True},
                ],
            },
        }
        html = insight._usage_section_html(self._usage(), adoption, span_days=30)

        # Both sub-section headings
        self.assertIn("Crabi suggested plugins", html)
        self.assertIn("Crabi AI marketplace", html)
        # superpowers appears as not-installed
        self.assertIn("Engram (memory)", html)  # friendly label for the suggested tool
        self.assertIn("not installed", html)
        # a not-installed catalog plugin shows the not-installed label
        self.assertIn("infra", html)
        # outdated marker on the outdated suggested tool + outdated catalog plugin
        self.assertIn("update available", html)
        self.assertIn("v0.1.0 → v0.1.1", html)  # engram, suggested
        self.assertIn("v0.1.0 → v0.2.0", html)  # qa, marketplace
        # a current (outdated=False) plugin makes NO version claim
        self.assertNotIn("v0.1.0 → v0.1.0", html)
        # marketplace status labels
        self.assertIn("installed · used", html)
        self.assertIn("installed · idle", html)

    def test_outdated_none_renders_no_version_claim(self):
        adoption = {
            "crabi/ai-marketplace": {
                "installed": True, "enabled": True, "used": 0,
                "plugin_breakdown": [
                    {"name": "data", "installed": True, "used": 1,
                     "installed_version": "unknown", "latest_version": "0.3.0", "outdated": None},
                ],
            },
        }
        html = insight._usage_section_html(self._usage(), adoption, span_days=30)
        self.assertIn("data", html)
        self.assertNotIn("update available", html)


if __name__ == "__main__":
    unittest.main()
