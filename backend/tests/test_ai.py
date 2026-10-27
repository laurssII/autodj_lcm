"""La IA decide de verdad: cadena de proveedores, opciones vinculantes, una llamada por canción, reserva."""

import asyncio
import random
import sys
import types
import unittest

from autodj.agent import ChainLLM, DJAgent, LLMError, ToolCall
from autodj.agent.llm import (OpenAICompatLLM, Provider, Tool, build_chain, classify, parse_chain)
from autodj.agent.llm import LLMResponse
from autodj.config import Settings
from autodj.models import TrackView
from autodj.options import describe, transition_options
from autodj.strategies import RenderCtx
from tests.helpers import make_track
from tests.test_session import Base, ScriptedLLM


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeClient:
    """Un proveedor falso: cada llamada devuelve (o lanza) lo siguiente de su guion."""

    def __init__(self, script, delay=0.0):
        self.script = list(script)
        self.delay = delay
        self.calls = 0

    async def complete(self, system, messages, tools, force_tool=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        r = self.script.pop(0) if self.script else LLMResponse(text="ok")
        if isinstance(r, Exception):
            raise r
        return r


def chain(*clients, timeout=1.0, budget=0, clock=None):
    provs = [Provider(f"p{i}:m{i}", f"P{i}", f"m{i}", c) for i, c in enumerate(clients)]
    return ChainLLM(provs, timeout=timeout, daily_budget=budget, clock=clock or Clock())


class ChainTest(unittest.IsolatedAsyncioTestCase):
    async def test_overload_jumps_to_the_next_provider_and_cools_the_first(self):
        clock = Clock()
        a = FakeClient([LLMError("503: The model is overloaded", 503)])
        b = FakeClient([LLMResponse(text="hola"), LLMResponse(text="otra")])
        c = chain(a, b, clock=clock)
        res = await c.complete("s", [], [])
        self.assertEqual(res.provider, "p1:m1")
        self.assertEqual(len(res.attempts), 1)
        self.assertIn("overloaded", res.attempts[0])
        await c.complete("s", [], [])  # el primero sigue enfriando: ni se le llama
        self.assertEqual((a.calls, b.calls), (1, 2))
        st = {p["key"]: p for p in c.status()}
        self.assertEqual(st["p0:m0"]["state"], "enfriando")
        clock.t += 1000
        self.assertEqual({p["state"] for p in c.status()}, {"listo"})

    async def test_a_slow_provider_is_skipped(self):
        slow = FakeClient([LLMResponse(text="tarde")], delay=0.5)
        fast = FakeClient([LLMResponse(text="a tiempo")])
        res = await chain(slow, fast, timeout=0.05).complete("s", [], [])
        self.assertEqual(res.text, "a tiempo")
        self.assertIn("tardó", res.attempts[0])

    async def test_429_honours_retry_after(self):
        clock = Clock()
        a = FakeClient([LLMError("429: slow down", 429, retry_after=12)])
        c = chain(a, FakeClient([]), clock=clock)
        await c.complete("s", [], [])
        self.assertAlmostEqual(c.providers[0].cooldown_until - clock.t, 12, places=3)

    async def test_daily_quota_and_bad_config_cool_for_long(self):
        self.assertEqual(classify(LLMError("429: Daily request limit reached", 429)), "daily")
        self.assertEqual(classify(LLMError("404: model not found", 404)), "config")
        self.assertEqual(classify(LLMError("503", 503)), "overload")
        self.assertEqual(classify(LLMError("sin conexión", 0)), "timeout")

    async def test_when_everything_fails_the_error_says_why(self):
        c = chain(FakeClient([LLMError("503: overloaded", 503)]), FakeClient([LLMError("401: bad key", 401)]))
        with self.assertRaises(LLMError) as e:
            await c.complete("s", [], [])
        self.assertIn("overloaded", str(e.exception))
        self.assertIn("bad key", str(e.exception))
        with self.assertRaises(LLMError) as e:  # todos enfriando: falla al momento, sin esperar
            await c.complete("s", [], [])
        self.assertIn("enfriando", str(e.exception))

    async def test_daily_budget_protects_the_free_quota(self):
        c = chain(FakeClient([]), budget=2)
        await c.complete("s", [], [])
        await c.complete("s", [], [])
        with self.assertRaises(LLMError) as e:
            await c.complete("s", [], [])
        self.assertIn("presupuesto", str(e.exception))


class BuildChainTest(unittest.TestCase):
    def test_providers_come_from_the_chain_and_their_own_keys(self):
        env = {"GROQ_API_KEY": "gsk_x", "GITHUB_TOKEN": ""}.get
        st = Settings(llm_chain="groq,gemini:gemini-3.5-flash-lite,github", llm_provider="none")
        c = build_chain(st, env)
        self.assertEqual([p.key for p in c.providers],
                         ["groq:openai/gpt-oss-120b", "gemini:gemini-3.5-flash-lite", "github:openai/gpt-4.1-mini"])
        self.assertEqual([p.key for p in c.usable], ["groq:openai/gpt-oss-120b"])
        self.assertEqual([p["state"] for p in c.status()], ["listo", "sin clave", "sin clave"])
        self.assertEqual(c.providers[0].client.reasoning, "low")  # sin «modo pensar» a fondo

    def test_the_old_single_provider_config_still_works_as_last_resort(self):
        st = Settings(llm_chain="groq", llm_provider="openai", llm_model="gemini-3.8-flash",
                      llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai", llm_api_key="k")
        c = build_chain(st, {"GROQ_API_KEY": "g"}.get)
        self.assertEqual([p.key for p in c.providers], ["groq:openai/gpt-oss-120b", "custom:gemini-3.8-flash"])

    def test_parse_chain(self):
        self.assertEqual(parse_chain(" groq , gemini:x-y ,"), [("groq", ""), ("gemini", "x-y")])

    def test_no_keys_means_the_agent_is_off(self):
        c = build_chain(Settings(llm_chain="groq,gemini", llm_provider="none"), {}.get)
        self.assertFalse(DJAgent(c).enabled)
        self.assertEqual(len(DJAgent(c).status()), 2)


class FakeHttpx:
    """Sustituye a httpx para probar OpenAICompatLLM sin red."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []
        outer = self

        class Resp:
            def __init__(self, code, data):
                self.status_code = code
                self._data = data
                self.text = data if isinstance(data, str) else ""
                self.headers = {}

            def json(self):
                return self._data

        class AsyncClient:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                outer.bodies.append(json)
                code, data = outer.responses.pop(0)
                return Resp(code, data)

        self.module = types.SimpleNamespace(AsyncClient=AsyncClient)


class OpenAICompatTest(unittest.IsolatedAsyncioTestCase):
    def tool_ok(self):
        return (200, {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "choose", "arguments": '{"option": "1b", "reason": "sube"}'}}]}}]})

    async def run_with(self, fake, llm):
        old = sys.modules.get("httpx")
        sys.modules["httpx"] = fake.module
        try:
            return await llm.complete("s", [{"role": "user", "content": "x"}],
                                      [Tool("choose", "d", {"type": "object", "properties": {"option": {"type": "string"}}})],
                                      "choose")
        finally:
            if old is None:
                sys.modules.pop("httpx", None)
            else:
                sys.modules["httpx"] = old

    async def test_reasoning_is_dropped_if_the_provider_does_not_know_it(self):
        fake = FakeHttpx([(400, "Unrecognized request argument supplied: reasoning_effort"), self.tool_ok()])
        llm = OpenAICompatLLM("m", "k", "https://x/v1", reasoning="low")
        res = await self.run_with(fake, llm)
        self.assertEqual(fake.bodies[0]["reasoning_effort"], "low")
        self.assertNotIn("reasoning_effort", fake.bodies[1])
        self.assertEqual(res.tool_calls[0].args["option"], "1b")
        self.assertEqual(llm.reasoning, "")  # ya no se vuelve a enviar

    async def test_errors_carry_the_status(self):
        fake = FakeHttpx([(503, "The model is overloaded")])
        with self.assertRaises(LLMError) as e:
            await self.run_with(fake, OpenAICompatLLM("m", "k"))
        self.assertEqual(e.exception.status, 503)


class OptionsTest(unittest.TestCase):
    def test_variants_are_viable_distinct_and_described(self):
        a = make_track("a", bpm=100, camelot="8A")
        b = make_track("b", bpm=101, camelot="9A")
        plans = transition_options(TrackView(a), b, 20.0, False, random.Random(1), RenderCtx(shorten=True), max_n=3)
        self.assertGreaterEqual(len(plans), 2)
        keys = {(p.strategy, round(p.out_start)) for p in plans}
        self.assertEqual(len(keys), len(plans))
        text = describe(TrackView(a), b, plans[0], RenderCtx(shorten=True))
        self.assertIn("empieza", text)
        self.assertIn("dura unos", text)

    def test_a_requested_technique_goes_first(self):
        a = make_track("a", bpm=100, camelot="8A")
        b = make_track("b", bpm=101, camelot="8A")
        plans = transition_options(TrackView(a), b, 20.0, False, random.Random(2), None, max_n=3, prefer="eq")
        self.assertEqual(plans[0].strategy, "eq")

    def test_without_a_real_mix_there_is_a_single_last_resort_option(self):
        a = make_track("a", bpm=100, camelot="8A")
        b = make_track("b", bpm=150, camelot="2B", drop_at=None)
        plans = transition_options(TrackView(a), b, 20.0, False, random.Random(3), None, max_n=3)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].strategy, "fade")


class RateLimitTest(unittest.TestCase):
    def test_per_minute_and_per_day(self):
        from autodj.ratelimit import VisitorLimiter

        clock = Clock()
        lim = VisitorLimiter(per_minute=2, per_day=3, clock=clock, wall=lambda: 0.0)
        self.assertTrue(lim.check("a")[0])
        self.assertTrue(lim.check("a")[0])
        ok, why = lim.check("a")
        self.assertFalse(ok)
        self.assertIn("por minuto", why)
        self.assertTrue(lim.check("b")[0])  # cada visitante tiene su cupo
        clock.t += 61
        self.assertTrue(lim.check("a")[0])
        clock.t += 61
        ok, why = lim.check("a")
        self.assertFalse(ok)
        self.assertIn("por hoy", why)

    def test_zero_means_no_limit(self):
        from autodj.ratelimit import VisitorLimiter

        lim = VisitorLimiter(0, 0)
        self.assertTrue(all(lim.check("a")[0] for _ in range(100)))

    def test_the_visitor_ip_cannot_be_spoofed_through_trusted_proxies(self):
        from autodj.ratelimit import visitor_id

        # El cliente manda su propia cabecera falsa; nuestro nginx añade la IP real al final.
        h = {"x-forwarded-for": "1.2.3.4, 81.0.0.7"}
        self.assertEqual(visitor_id(h, "172.18.0.3", 1), "81.0.0.7")
        # Con otro proxy delante (p. ej. el del hosting), la real es la penúltima.
        h = {"x-forwarded-for": "81.0.0.7, 10.0.0.2"}
        self.assertEqual(visitor_id(h, "172.18.0.3", 2), "81.0.0.7")
        self.assertEqual(visitor_id({}, "172.18.0.3", 1), "172.18.0.3")


class GateLLM:
    """LLM que no contesta hasta que se le abre la puerta; elige siempre la última opción."""

    name = "gate"

    def __init__(self):
        self.gate = asyncio.Event()
        self.calls = 0

    async def complete(self, system, messages, tools, force_tool=None):
        self.calls += 1
        await self.gate.wait()
        block = messages[0]["content"].split("OPCIONES:\n", 1)[1]
        ids = [ln.split(":")[0].strip() for ln in block.splitlines() if ":" in ln]
        return LLMResponse(tool_calls=[ToolCall("1", "choose", {"option": ids[-1], "reason": "me la pide el cuerpo"})],
                           provider="groq:openai/gpt-oss-120b", latency_ms=420)


class SessionDecisionTest(Base):
    async def start_playing(self, s):
        await s.start("auto")
        load = self.last("load")["track"]["id"]
        await s.handle({"type": "track_started", "track_id": load, "rate": 1, "offset": 0})

    async def test_automatic_replans_do_not_cancel_or_repeat_the_ai_call(self):
        s = self.make_session(seed=4)
        gate = GateLLM()
        s.agent.llm = None
        await s.start("auto")
        s.agent.llm = gate
        load = self.last("load")["track"]["id"]
        await s.handle({"type": "track_started", "track_id": load, "rate": 1, "offset": 0})
        await asyncio.sleep(0)
        for why in ("ya están los stems de la pareja", "la pista pedida terminó de analizarse", "falló el clip"):
            s.replan(why)  # replans automáticos mientras la IA piensa
            await asyncio.sleep(0)
        gate.gate.set()
        await s.wait_planned()
        self.assertEqual(gate.calls, 1)  # una sola consulta, no cancelada
        plan = self.last("plan")["plan"]
        self.assertEqual(plan["decided"]["by"], "ia")
        self.assertEqual(plan["decided"]["provider"], "Groq")
        self.assertEqual(plan["reason"], "me la pide el cuerpo")
        # Otro replan automático: se reutiliza la decisión, sin llamar otra vez
        s.replan("otro motivo automático")
        await s.wait_planned()
        self.assertEqual(gate.calls, 1)
        self.assertEqual(self.last("plan")["plan"]["track"]["id"], plan["track"]["id"])
        # Un cambio que importa (el usuario cambia el ambiente) sí vuelve a preguntar
        s.replan("el usuario cambió el ambiente", rethink=True)
        await s.wait_planned()
        self.assertEqual(gate.calls, 2)

    async def test_a_relevant_change_while_the_ai_thinks_asks_again_and_still_plans(self):
        s = self.make_session(seed=11)
        gate = GateLLM()
        s.agent.llm = None
        await s.start("auto")
        s.agent.llm = gate
        load = self.last("load")["track"]["id"]
        await s.handle({"type": "track_started", "track_id": load, "rate": 1, "offset": 0})
        for _ in range(50):  # hasta que la primera consulta esté esperando a la IA
            if gate.calls:
                break
            await asyncio.sleep(0)
        self.assertEqual(gate.calls, 1)
        s.replan("el usuario cambió el ambiente", rethink=True)  # mientras la primera consulta espera
        await asyncio.sleep(0)
        gate.gate.set()
        await s.wait_planned()
        self.assertEqual(gate.calls, 2)
        self.assertIsNotNone(self.last("plan"))
        self.assertEqual(self.last("plan")["plan"]["decided"]["by"], "ia")

    async def test_the_queue_head_is_played_but_the_ai_chooses_how(self):
        s = self.make_session(seed=5)
        gate = GateLLM()
        gate.gate.set()
        s.agent.llm = None
        await self.start_playing(s)
        await s.wait_planned()
        want = next(t for t in ("t02", "t04", "t06") if t != s.current.id)
        s.agent.llm = gate
        await s.queue_add(want)
        await s.wait_planned()
        plan = self.last("plan")["plan"]
        self.assertEqual(plan["track"]["id"], want)
        d = s.decision
        self.assertTrue(d.from_queue)
        self.assertIn(d.by, ("ia", "única"))
        if d.by == "ia":
            self.assertEqual(gate.calls, 1)
            self.assertTrue(d.option_id.startswith("1"))  # solo opciones de la canción de la cola

    async def test_without_ai_the_fallback_is_labelled_and_explained(self):
        s = self.make_session(seed=6, llm=ScriptedLLM([LLMError("503: overloaded", 503)] * 4))
        await self.start_playing(s)
        await s.wait_planned()
        plan = self.last("plan")["plan"]
        self.assertEqual(plan["decided"]["by"], "reserva")
        self.assertIn("saturada", plan["decided"]["why"])  # la frase para el usuario, sin códigos
        self.assertNotIn("503", plan["decided"]["why"])
        raw = [e["detail"].get("error", "") for e in s.logs if e["kind"] == "error"]
        self.assertTrue(any("overloaded" in r for r in raw))  # el detalle técnico queda en la observabilidad
        state = s.snapshot()
        self.assertEqual(state["next"]["decided"]["by"], "reserva")
        self.assertIn("ai", state)

    async def test_no_ai_configured_says_so(self):
        s = self.make_session(seed=7)
        await self.start_playing(s)
        await s.wait_planned()
        self.assertIn("no configurada", self.last("plan")["plan"]["decided"]["why"])

    async def test_an_invalid_answer_falls_back(self):
        s = self.make_session(seed=8)
        s.agent.llm = None
        await self.start_playing(s)
        s.agent.llm = ScriptedLLM([LLMResponse(tool_calls=[ToolCall("1", "choose", {"option": "99z", "reason": "x"})])])
        s.replan("prueba", rethink=True)
        await s.wait_planned()
        self.assertEqual(self.last("plan")["plan"]["decided"]["by"], "reserva")

    async def test_the_first_song_is_chosen_by_the_ai(self):
        class First:
            name = "first"

            async def complete(self, system, messages, tools, force_tool=None):
                return LLMResponse(tool_calls=[ToolCall("1", "choose", {"option": "3", "reason": "arranque suave"})],
                                   provider="gemini:gemini-3.5-flash-lite")

        s = self.make_session(seed=10, llm=First())
        await s.start("auto")
        d = s._first_decided
        self.assertEqual((d["by"], d["provider"]), ("ia", "Gemini"))
        log = next(e for e in s.logs if e["title"].startswith("Primera canción"))
        self.assertEqual(log["detail"]["chosen_option"], "3")



class ThoughtSignatureTest(unittest.TestCase):
    """Gemini 3 rechaza (400) una llamada a herramienta devuelta sin su firma de pensamiento."""

    def history(self, extra):
        return [{"role": "user", "content": "hola"},
                {"role": "assistant", "content": "", "tool_calls": [ToolCall("1", "search_library", {"query": "x"}, extra)]},
                {"role": "tool", "tool_call_id": "1", "content": "{}"}]

    def test_signature_is_sent_back_untouched(self):
        sig = {"google": {"thought_signature": "abc=="}}
        out = OpenAICompatLLM.convert("sys", self.history(sig), gemini=True)
        self.assertEqual(out[2]["tool_calls"][0]["extra_content"], sig)

    def test_missing_signature_gets_a_placeholder_only_for_gemini(self):
        gem = OpenAICompatLLM.convert("sys", self.history(None), gemini=True)
        self.assertIn("thought_signature", gem[2]["tool_calls"][0]["extra_content"]["google"])
        other = OpenAICompatLLM.convert("sys", self.history(None))
        self.assertNotIn("extra_content", other[2]["tool_calls"][0])


class FriendlyErrorTest(unittest.TestCase):
    def test_limits_become_a_plain_sentence(self):
        from autodj.agent.llm import explain
        e = LLMError("ningún proveedor respondió: Groq (x): 429: {...}", 503, None, "chain", ["rate", "daily"])
        text = explain(e)
        self.assertNotIn("429", text)
        self.assertNotIn("Groq", text)
        self.assertIn("límite", text)

    def test_timeouts_and_unknown_failures(self):
        from autodj.agent.llm import explain
        self.assertIn("tarda", explain(asyncio.TimeoutError()))
        self.assertIn("tarda", explain(LLMError("x", 0, None, "chain", ["timeout", "timeout"])))
        self.assertIn("no está disponible", explain(LLMError("x", 400, None, "chain", ["rate", "config"])))

if __name__ == "__main__":
    unittest.main()

