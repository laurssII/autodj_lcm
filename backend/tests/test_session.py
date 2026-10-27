import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from autodj.agent import DJAgent, LLMError, ToolCall
from autodj.agent.llm import LLMResponse
from autodj.config import Settings
from autodj.db import Database
from autodj.library import Library
from autodj.session import Session, Vibe
from tests.helpers import make_track

KEYS = ["8A", "9A", "8B", "7A", "10A", "9B", "7B", "11A"]


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ScriptedLLM:
    """LLM falso: devuelve respuestas preparadas en orden."""

    name = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, system, messages, tools, force_tool=None):
        self.calls.append((system, messages, [t.name for t in tools], force_tool))
        if not self.responses:
            return LLMResponse(text="")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Base(unittest.IsolatedAsyncioTestCase):
    n_tracks = 30

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(data_dir=Path(self.dir), enable_lyrics=False, analysis_workers=1)
        self.db = Database(":memory:")
        self.lib = Library(self.settings, self.db)
        for i in range(self.n_tracks):
            t = make_track(f"t{i:02d}", bpm=95 + (i % 7), camelot=KEYS[i % len(KEYS)],
                           energy=3 + (i % 7), duration=200, genre="pop" if i % 2 else "reggaeton",
                           mood=-0.5 if i % 3 == 0 else 0.4)
            self.lib._save(t)
        self.sent = []
        self.clock = FakeClock()

    def tearDown(self):
        self.lib.close()
        self.db.close()
        shutil.rmtree(self.dir)

    async def send(self, msg):
        self.sent.append(msg)

    def make_session(self, llm=None, seed=None):
        s = Session(self.lib, self.db, DJAgent(llm), self.send, seed=seed, clock=self.clock)
        return s

    def last(self, kind):
        for m in reversed(self.sent):
            if m["type"] == kind:
                return m
        return None

    async def play_through(self, s: Session, n: int) -> list[str]:
        """Simula el navegador: arranca la primera y ejecuta n transiciones."""
        load = self.last("load")
        order = [load["track"]["id"]]
        await s.handle({"type": "track_started", "track_id": order[0], "rate": 1.0, "offset": 0})
        for _ in range(n - 1):
            await s.wait_planned()
            plan = self.last("plan")["plan"]
            await s.handle({"type": "transition_started", "plan_id": plan["plan_id"]})
            await s.handle({"type": "track_started", "track_id": plan["track"]["id"], "rate": plan["rate_b"],
                            "offset": plan["in_start"]})
            order.append(plan["track"]["id"])
        await s.wait_planned()
        return order


class SessionFlowTest(Base):
    async def test_autonomous_start_and_plan(self):
        s = self.make_session(seed=1)
        await s.start("auto")
        load = self.last("load")
        self.assertIsNotNone(load)
        order = await self.play_through(s, 5)
        self.assertEqual(len(set(order)), 5)  # sin repeticiones
        plan = self.last("plan")["plan"]
        self.assertGreaterEqual(plan["out_start"], 8.0 - 1e-6)
        self.assertIn(plan["strategy"], ("bars", "eq", "wordplay", "fade"))
        kinds = {e["kind"] for e in s.logs}
        self.assertTrue({"decision", "plan"} <= kinds)  # observabilidad

    async def test_messages_carry_what_the_browser_needs_to_draw_the_mix(self):
        s = self.make_session(seed=1)
        await s.start("auto")
        load = self.last("load")["track"]
        await self.play_through(s, 2)
        plan = self.last("plan")["plan"]
        for track in (load, plan["track"]):
            # rejilla de la pista: compases, fase de frases, intro y outro (tiempos del audio original)
            self.assertTrue(track["downbeats"])
            self.assertEqual(track["downbeats"], sorted(track["downbeats"]))
            self.assertIn("phrase_offset", track)
            self.assertIn("intro_end", track)
            self.assertIn("outro_start", track)
        self.assertTrue(plan["label"])  # etiqueta lista para mostrar
        self.assertIn("swap_at", plan)
        self.assertIn("aligned", plan)
        if plan["strategy"] in ("bars", "eq"):
            self.assertIsNotNone(plan["swap_at"])
            self.assertLess(plan["swap_at"], plan["duration"])

    async def test_queue_has_priority_and_ai_picks_never_queued(self):
        s = self.make_session(seed=2)
        await s.start("auto")
        await self.play_through(s, 1)
        await s.queue_add("t05")
        await s.queue_add("t07")
        await s.wait_planned()
        plan = self.last("plan")["plan"]
        self.assertEqual(plan["track"]["id"], "t05")
        self.assertEqual(s.snapshot()["queue"][0]["id"], "t05")
        # reordenar antes de que entre en la mezcla cambia la siguiente
        await s.reorder(["t07", "t05"])
        await s.wait_planned()
        self.assertEqual(self.last("plan")["plan"]["track"]["id"], "t07")
        # al entrar en la mezcla sale de la cola y ya no se puede mover
        await s.handle({"type": "transition_started", "plan_id": self.last("plan")["plan"]["plan_id"]})
        self.assertEqual([q["id"] for q in s.snapshot()["queue"]], ["t05"])
        await s.reorder(["t07", "t05"])
        self.assertEqual(s.queue, ["t05"])

    async def test_upload_start_waits_for_analysis(self):
        t = make_track("local:abc")
        t.status = "analyzing"
        self.lib._save(t)
        s = self.make_session()
        await s.start("upload", track_id="local:abc")
        self.assertIsNone(self.last("load"))
        t.status = "ready"
        await s.library_changed(t)
        self.assertEqual(self.last("load")["track"]["id"], "local:abc")

    async def test_start_with_empty_library_waits(self):
        for t in self.lib.all():
            t.status = "analyzing"
        s = self.make_session()
        await s.start("auto")
        self.assertIsNone(self.last("load"))
        self.assertTrue(s.active)
        t = self.lib.get("t04")
        t.status = "ready"
        await s.library_changed(t)
        self.assertEqual(self.last("load")["track"]["id"], "t04")

    async def test_plan_failed_recovers(self):
        s = self.make_session(seed=3)
        await s.start("auto")
        await self.play_through(s, 1)
        bad = self.last("plan")["plan"]
        await s.handle({"type": "plan_failed", "plan_id": bad["plan_id"], "error": "404"})
        await s.wait_planned()
        self.assertNotEqual(self.last("plan")["plan"]["track"]["id"], bad["track"]["id"])
        self.assertTrue(any("No he podido cargar" in m["message"]["text"] for m in self.sent if m["type"] == "chat"))

    async def test_variety_across_sessions(self):
        openers, pairs = [], {}
        for i in range(10):
            s = self.make_session(seed=100 + i)
            self.clock.t += 3600
            await s.start("auto")
            order = await self.play_through(s, 20)
            openers.append(order[0])
            for a, b in zip(order, order[1:]):
                pairs[(a, b)] = pairs.get((a, b), 0) + 1
        self.assertGreaterEqual(len(set(openers)), 6, openers)
        self.assertLessEqual(max(pairs.values()), 3)

    async def test_mix_now_is_urgent(self):
        s = self.make_session(seed=4)
        await s.start("auto")
        await self.play_through(s, 1)
        normal = self.last("plan")["plan"]["out_start"]
        self.clock.t += 20
        await s.handle({"type": "mix_now"})
        await s.wait_planned()
        urgent = self.last("plan")["plan"]["out_start"]
        self.assertLess(urgent, normal)
        self.assertLess(urgent - 20, 5 + 8 * 4 * 60 / 95 + 0.1)


class HistoryTest(Base):
    async def test_history_lists_what_has_played_most_recent_first(self):
        s = self.make_session(seed=1)
        await s.start("auto")
        self.assertEqual(s.snapshot()["history"], [])  # la primera aún suena: no hay nada anterior
        order = await self.play_through(s, 4)
        hist = s.snapshot()["history"]
        self.assertEqual([h["id"] for h in hist], order[-2::-1])  # sin la que suena, la última primero
        self.assertEqual(len(hist), 3)
        self.assertIsNone(hist[-1]["via"])  # la primera no entró por ninguna transición
        self.assertTrue(all(h["via"] for h in hist[:-1]))  # el resto, sí (con su nombre)
        self.assertGreater(hist[0]["played_at"], 0)
        self.assertEqual(s.snapshot()["now_playing"]["id"], order[-1])

    async def test_the_same_track_started_twice_is_not_duplicated(self):
        s = self.make_session(seed=1)
        await s.start("auto")
        order = await self.play_through(s, 2)
        await s.handle({"type": "track_started", "track_id": order[-1], "rate": 1.0, "offset": 3})
        self.assertEqual(len(s.history), 2)

    async def test_the_history_is_bounded(self):
        s = self.make_session(seed=1)
        await s.start("auto")
        await self.play_through(s, 2)
        s.history = [dict(s.history[0], id=f"t{i % 30:02d}") for i in range(500)] + s.history[-1:]
        self.assertLessEqual(len(s.snapshot()["history"]), 100)


class SoundcloudChatTest(Base):
    META = {"id": "soundcloud:123", "title": "Nuevo tema", "artist": "Alguien", "duration": 201.0,
            "url": "https://soundcloud.com/alguien/nuevo-tema", "thumbnail": None}

    async def test_the_dj_finds_a_song_on_soundcloud_and_queues_it(self):
        from unittest import mock

        llm = ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("1", "search_soundcloud", {"query": "Alguien Nuevo tema"})]),
            LLMResponse(tool_calls=[ToolCall("2", "add_soundcloud", {"url": self.META["url"], "position": 0})]),
            LLMResponse(text="La busco en SoundCloud y te la pongo la siguiente."),
        ])
        s = self.make_session(llm=llm)
        s.agent.llm = None
        await s.start("auto")
        await self.play_through(s, 1)
        s.agent.llm = llm
        with mock.patch("autodj.sources.search_mainstream", return_value=[dict(self.META)]) as search, \
                mock.patch("autodj.sources.resolve_soundcloud_url", return_value=dict(self.META)), \
                mock.patch.object(self.lib, "_enqueue"):
            reply = await s.chat("ponme Nuevo tema de Alguien")
        search.assert_called_once()
        self.assertEqual(s.queue[0], "soundcloud:123")
        track = self.lib.get("soundcloud:123")
        self.assertEqual((track.source, track.title, track.artist), ("soundcloud", "Nuevo tema", "Alguien"))
        self.assertIn("SoundCloud", reply)
        # el resultado de la búsqueda que ve el modelo lleva el enlace y si ya estaba en la biblioteca
        tool_msgs = [m for call in llm.calls for m in call[1] if isinstance(m, dict) and m.get("role") == "tool"]
        self.assertTrue(any("nuevo-tema" in str(m) for m in tool_msgs))

    async def test_a_search_failure_is_reported_to_the_model_not_raised(self):
        from unittest import mock

        s = self.make_session()
        await s.start("auto")
        tools = __import__("autodj.session", fromlist=["_SessionTools"])._SessionTools(s)
        with mock.patch("autodj.sources.search_mainstream",
                        side_effect=__import__("autodj.sources", fromlist=["x"]).MainstreamError("sin red")):
            res = await tools.call("search_soundcloud", {"query": "algo"})
        self.assertFalse(res["ok"])
        self.assertIn("sin red", res["message"])
        res = await tools.call("add_soundcloud", {"url": "https://example.com/x"})
        self.assertFalse(res["ok"])  # solo se aceptan enlaces de SoundCloud

    async def test_a_track_already_in_the_library_is_not_added_twice(self):
        from unittest import mock

        s = self.make_session()
        await s.start("auto")
        tools = __import__("autodj.session", fromlist=["_SessionTools"])._SessionTools(s)
        with mock.patch("autodj.sources.resolve_soundcloud_url", return_value=dict(self.META)), \
                mock.patch.object(self.lib, "_enqueue") as enq:
            await tools.call("add_soundcloud", {"url": self.META["url"]})
            await tools.call("add_soundcloud", {"url": self.META["url"]})
        self.assertEqual(enq.call_count, 1)
        self.assertEqual(s.queue.count("soundcloud:123"), 1)


class ChatTest(Base):
    async def test_no_llm_replies_and_queues_attachment(self):
        s = self.make_session()
        await s.start("auto")
        reply = await s.chat("", track_id="t03")
        self.assertIn("t03", s.queue)
        self.assertTrue(reply.startswith("Hecho:"))
        self.assertIn(".env", reply)  # el aviso de configuración, una sola vez
        reply = await s.chat("", track_id="t05")
        self.assertTrue(reply.startswith("Hecho:"))
        self.assertNotIn(".env", reply)
        reply = await s.chat("algo más oscuro")
        self.assertIn("no puedo entender", reply)

    async def test_plain_attachment_is_queued_without_asking_the_ai(self):
        s = self.make_session(seed=1)
        await s.start("auto")
        llm = ScriptedLLM([LLMResponse(tool_calls=[ToolCall("1", "search_soundcloud", {"query": "esta"})])])
        s.agent.llm = llm
        reply = await s.chat("", track_id="t03")
        self.assertIn("t03", s.queue)
        self.assertTrue(reply.startswith("Hecho:"))
        self.assertEqual(llm.calls, [])

    async def test_tool_loop(self):
        s = self.make_session(seed=1)
        await s.start("auto")
        await self.play_through(s, 1)
        want = next(t for t in ("t09", "t11", "t13") if t != s.current.id)
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("1", "search_library", {"query": f"Song {want}"})]),
            LLMResponse(tool_calls=[ToolCall("2", "queue_add", {"track_id": want})]),
            LLMResponse(text="¡Marchando! Suena después de esta."),
        ])
        s.agent.llm = llm
        reply = await s.chat(f"ponme Song {want}")
        self.assertEqual(reply, "¡Marchando! Suena después de esta.")
        self.assertEqual(s.queue, [want])
        tool_logs = [e for e in s.logs if e["kind"] == "tool"]
        self.assertEqual(len(tool_logs), 2)
        found = tool_logs[0]["detail"]["result"]["results"]
        self.assertEqual(found[0]["id"], want)
        # El chat lleva el estado en texto compacto, no en JSON
        self.assertIn("Sonando:", llm.calls[0][1][0]["content"])

    async def test_vibe_changes_targets(self):
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("1", "set_vibe", {"energy_delta": 3, "mood": -0.8})]),
            LLMResponse(text="Subimos y oscurecemos."),
        ])
        s = self.make_session(llm=llm)
        s.agent.llm = None
        await s.start("auto")
        await self.play_through(s, 1)
        before = s.targets().energy
        s.agent.llm = llm
        await s.chat("algo más oscuro y con más ritmo")
        self.assertGreater(s.targets().energy, before)
        self.assertEqual(s.targets().mood, -0.8)

    async def test_llm_failure_keeps_playing(self):
        llm = ScriptedLLM([LLMError("timeout"), LLMError("timeout")])
        s = self.make_session(llm=llm)
        s.agent.llm = None
        await s.start("auto")
        await self.play_through(s, 1)
        s.agent.llm = llm
        reply = await s.chat("hola")
        self.assertIn("no responde", reply)
        s.replan("test")
        await s.wait_planned()  # decide() falla → sorteo
        self.assertIsNotNone(self.last("plan"))

    async def test_ai_decides_next(self):
        """La IA ve opciones concretas (canción + variante) y la elegida es la que se ejecuta."""
        picked = {}

        class Picker:
            name = "picker"

            async def complete(self, system, messages, tools, force_tool=None):
                block = messages[0]["content"].split("OPCIONES:\n", 1)[1]
                ids = [ln.strip().split(":")[0] for ln in block.splitlines() if ln.startswith("   ")]
                oid = next(i for i in ids if i.startswith("4")) if any(i.startswith("4") for i in ids) else ids[-1]
                picked["option"] = oid
                return LLMResponse(tool_calls=[ToolCall("1", "choose", {"option": oid, "reason": "subo con intención"})],
                                   provider="groq:openai/gpt-oss-120b")

        s = self.make_session(seed=9)
        await s.start("auto")
        s.agent.llm = Picker()
        load = self.last("load")["track"]["id"]
        await s.handle({"type": "track_started", "track_id": load, "rate": 1, "offset": 0})
        await s.wait_planned()
        plan = self.last("plan")["plan"]
        d = s.decision
        self.assertEqual(d.option_id, picked["option"])
        self.assertEqual(plan["track"]["id"], d.target_id)  # no siempre la de mayor puntuación
        self.assertEqual(plan["strategy"], d.plan.strategy)  # la variante elegida, tal cual
        self.assertAlmostEqual(plan["out_start"], d.plan.out_start, places=3)
        self.assertEqual(plan["reason"], "subo con intención")
        self.assertEqual(plan["decided"]["by"], "ia")


class BlockingLLM:
    """LLM que no contesta nunca: simula una consulta lenta a la IA."""

    name = "blocking"

    def __init__(self):
        self.started = asyncio.Event()
        self.calls = 0

    async def complete(self, system, messages, tools, force_tool=None):
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()


class StopSessionTest(Base):
    """«Terminar sesión» debe dejarlo todo como recién empezado: nada de la anterior sobrevive."""

    async def _busy_session(self):
        llm = ScriptedLLM([LLMResponse(text="Marchando, te subo la energía.")])
        s = self.make_session(llm=llm, seed=3)
        s.agent.llm = None
        await s.start("auto")
        await self.play_through(s, 3)
        s.agent.llm = llm
        await s.queue_add("t05")
        await s.queue_add("t07")
        await s.chat("más energía")
        await s.handle({"type": "position", "track_id": s.current.id, "position": 42.0})
        s.vibes.append(Vibe(energy=9))
        s.banned_tracks.add("t09")
        s.banned_artists.add("artist t10")
        s.preferred_strategy = "eq"
        # precondiciones: de verdad hay algo que borrar
        self.assertTrue(s.logs and s.chat_log and s.history and s.queue and s.current)
        return s

    async def test_stop_wipes_everything_from_the_session(self):
        s = await self._busy_session()
        old_id = s.id
        await s.handle({"type": "stop"})
        self.assertFalse(s.active)
        self.assertNotEqual(s.id, old_id)
        for name in ("logs", "chat_log", "history", "queue", "played", "vibes"):
            self.assertEqual(getattr(s, name), [], name)
        self.assertEqual((s.banned_tracks, s.banned_artists, s.failed), (set(), set(), set()))
        for name in ("current", "pending", "transitioning", "decision", "waiting_first", "preferred_strategy",
                     "last_decided"):
            self.assertIsNone(getattr(s, name), name)
        self.assertEqual(s.position(), 0.0)

    async def test_stop_tells_every_screen_to_clear_itself(self):
        s = await self._busy_session()
        self.sent.clear()
        await s.handle({"type": "stop"})
        kinds = [m["type"] for m in self.sent]
        self.assertEqual(kinds[:2], ["reset", "state"])  # primero vaciar, luego el estado ya limpio
        snap = self.sent[1]
        self.assertFalse(snap["active"])
        self.assertIsNone(snap["now_playing"])
        self.assertIsNone(snap["next"])
        self.assertEqual((snap["queue"], snap["history"]), ([], []))

    async def test_a_reconnecting_client_gets_no_old_session(self):
        s = await self._busy_session()
        await s.handle({"type": "stop"})
        # lo que la API manda en el «hello» al conectarse un navegador
        self.assertEqual((s.logs[-100:], s.chat_log[-100:]), ([], []))

    async def test_a_new_session_starts_clean(self):
        s = await self._busy_session()
        await s.handle({"type": "stop"})
        self.sent.clear()
        s.agent.llm = None
        await s.handle({"type": "start", "mode": "auto"})
        kinds = [m["type"] for m in self.sent]
        self.assertEqual(kinds[0], "reset")  # antes que cualquier otra cosa de la sesión nueva
        self.assertEqual(s.history, [])
        self.assertEqual(len(s.logs) >= 1, True)
        self.assertTrue(all(e["title"] != "Añadida a la cola: Song t05" for e in s.logs))
        self.assertEqual(s.queue, [])
        self.assertEqual(s.chat_log, [])

    async def test_stop_cancels_a_chat_still_waiting_for_the_ai(self):
        llm = BlockingLLM()
        s = self.make_session(llm=llm, seed=3)
        s.agent.llm = None
        await s.start("auto")
        await self.play_through(s, 1)
        s.agent.llm = llm
        chat = asyncio.create_task(s.handle({"type": "chat", "text": "ponme algo"}))
        await asyncio.wait_for(llm.started.wait(), 2)
        await s.handle({"type": "stop"})
        with self.assertRaises(asyncio.CancelledError):
            await chat
        self.assertEqual(s.chat_log, [])  # ni la pregunta ni una respuesta tardía
        self.assertEqual(s._busy, set())

    async def test_late_browser_messages_after_stop_are_ignored(self):
        llm = ScriptedLLM([LLMResponse(text="hola")])
        s = self.make_session(llm=llm, seed=3)
        s.agent.llm = None
        await s.start("auto")
        track = self.last("load")["track"]["id"]
        s.agent.llm = llm
        await s.handle({"type": "stop"})
        self.sent.clear()
        await s.handle({"type": "track_started", "track_id": track, "rate": 1.0, "offset": 0})
        await s.handle({"type": "position", "track_id": track, "position": 10})
        await s.handle({"type": "ended"})
        await s.handle({"type": "mix_now"})
        await s.handle({"type": "chat", "text": "¿sigues ahí?"})
        self.assertEqual(self.sent, [])
        self.assertEqual((s.logs, s.chat_log, llm.calls), ([], [], []))
        self.assertIsNone(s.current)

    async def test_stop_is_harmless_when_nothing_is_running(self):
        s = self.make_session(seed=3)
        await s.handle({"type": "stop"})
        await s.handle({"type": "stop"})
        self.assertFalse(s.active)

    async def test_stop_deletes_the_rendered_mix_clips(self):
        from autodj.renderer import Renderer

        s = self.make_session(seed=3)
        s.renderer = Renderer(self.lib, self.settings)
        self.settings.ensure_dirs()
        clips = [self.settings.render_dir / f"{n}.wav" for n in ("a", "b")] + [self.settings.render_dir / "c.tmp"]
        for c in clips:
            c.write_bytes(b"RIFF")
        await s.handle({"type": "stop"})
        self.assertEqual([c.exists() for c in clips], [False, False, False])


if __name__ == "__main__":
    unittest.main()
