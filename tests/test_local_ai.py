import asyncio
import io
import json
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot import config, local_ai
from bot.bus import Bus
from bot.local_ai import (ACTIVOS, APETITO, BOLSAS, SCHEMA, SYSTEM, TEMAS, TIPOS, LocalAI, importance_cap,
                          normalize_triage, upcoming_windows)
from bot.regions import MARKETS
from bot.server import recent_news, state_summary
from bot.store import Store
from bot.topics import TOPICS

# Respuestas reales del modelo (Qwen3.5 9B, prompt actual, 23-10-2026), tal cual llegaron.
# Titular coreano del hackeo de Upbit: importancia 9 con magnitud 70, que el tope deja en 8.
UPBIT = {"resumen": "El exchange coreano Upbit suspende operaciones tras un hackeo que robó 580 millones de wones en Ethereum.",
         "tipo": "seguridad", "temas": ["cripto", "banca", "tecnología"], "credibilidad": 95,
         "alcance": {"nivel": "global", "puntuacion": 100}, "temporalidad": {"horizonte": "horas", "urgencia": 100},
         "apetito_riesgo": "baja", "repercusion": {"direccion": -85, "magnitud": 70, "activos": ["ETH"], "bolsas": []},
         "novedad": 100, "motivo": "Robo masivo de fondos y suspensión total de operaciones en un exchange líder de Asia.",
         "importancia": 9}
# «Pedro Sánchez convoca elecciones…» de una fuente T3 (antes salía con credibilidad 30): ya cuadra y sale igual.
ELECCIONES = {"resumen": "El gobierno español anuncia elecciones generales anticipadas para el 14 de diciembre.",
              "tipo": "política", "temas": ["elecciones", "política"], "credibilidad": 85,
              "alcance": {"nivel": "nacional", "puntuacion": 50}, "temporalidad": {"horizonte": "semanas", "urgencia": 50},
              "apetito_riesgo": "baja", "repercusion": {"direccion": -20, "magnitud": 30, "activos": [], "bolsas": ["madrid"]},
              "novedad": 60, "motivo": "La incertidumbre política y la posible parálisis legislativa generan volatilidad en los mercados locales.",
              "importancia": 5}
# Los 6 casos difíciles del signo (el precio que cita el titular va al revés que el apetito por el riesgo):
# titular, respuesta del prompt actual (apetito, direccion) y dirección que dio el prompt anterior, sin apetito
# (5 de 6 con el signo al revés).
DIFICILES = [
    ("Israel launches strikes on Iranian oil terminals, Brent jumps 6%", "baja", -65, 85),
    ("Russia launches massive missile attack on Kyiv; oil climbs 4%", "baja", -85, 65),
    ("Houthi rebels attack tanker in Red Sea, crude futures rise 3%", "baja", -65, 65),
    ("Israel and Hamas agree to ceasefire; oil slides 5%", "sube", 75, -60),
    ("Gold falls 3% as US and China sign trade deal", "sube", 65, -80),
    ("Treasury yields fall after Fed cuts rates by 25 basis points", "sube", 85, 85),
]
KEYS = ["tipo", "temas", "credibilidad", "alcance", "temporalidad", "repercusion", "novedad", "importancia", "sentimiento",
        "monedas", "resumen", "motivo"]


@pytest.fixture(autouse=True)
def puerto_libre(monkeypatch):
    """Un puerto local libre en vez de LOCAL_AI_PORT: con el bot en marcha, su llama-server responde en el 8094 y las
    pruebas lo usarían (o le pedirían que se apagara)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(config, "LOCAL_AI_PORT", port)
    return port


def mem_bus() -> Bus:
    return Bus(Store(Path(":memory:")))   # nada en disco (pytest dejaría tmp_path en C:)


def drain(q):
    out = []
    while not q.empty():
        out.append(json.loads(q.get_nowait()))
    return out


async def run_worker(ai, jobs):
    for j in jobs:
        ai.submit(*j)
    task = asyncio.create_task(ai._worker())
    while not ai.q.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()


async def ok():
    return True


async def test_triaje_publica_triada_o_archivada():
    bus = mem_bus()
    q = bus.subscribe()
    ai = LocalAI(bus, None)

    async def fake(title, src, tier, coins):
        imp = 8 if "ETF" in title else 2
        return normalize_triage({**UPBIT, "importancia": imp, "apetito_riesgo": "sube",
                                 "repercusion": {"direccion": 60, "magnitud": 80, "activos": coins}}, title, coins)

    ai._ensure, ai.classify = ok, fake
    await run_worker(ai, [("s1", "SEC approves Solana ETF", "SEC", "T0", ["SOL"]), ("s2", "Miner moves coins", "X", "T2", ["BTC"])])
    evs = [m for m in drain(q) if m["type"] == "event"]
    # A igual prioridad sale antes lo más reciente.
    assert sorted((e["story"], e["kind"]) for e in evs) == [("s1", "triaged"), ("s2", "lowprio")]
    evs = sorted(evs, key=lambda e: e["story"])
    tr = evs[0]["patch"]["triage"]
    assert evs[0]["agent"] == "local" and tr["importancia"] == 8 and evs[0]["coins"] == ["SOL"]
    assert list(tr) == KEYS + ["ms", "model"] and tr["sentimiento"] == "alcista" and tr["model"] == "Qwen3.5 9B (local)"
    assert "Importancia 8/10 · seguridad · alcista" in evs[0]["text"]


async def test_sin_ia_local_la_noticia_espera_en_la_cola(monkeypatch):
    # Antes el worker sacaba la pieza más prioritaria y, sin IA, la tiraba en cada intento.
    monkeypatch.setattr(local_ai, "RETRY_MIN_S", 0.01)
    bus = mem_bus()
    q = bus.subscribe()
    ai = LocalAI(bus, None)
    up = {"v": False}

    async def ensure():
        return up["v"]

    async def fake(title, src, tier, coins):
        return normalize_triage(ELECCIONES, title, coins)

    ai._ensure, ai.classify = ensure, fake
    ai.submit("ctx", "contexto", "X", "T2", [], prio=1)
    ai.submit("urgente", "urgente", "X", "T0", [], prio=9)
    task = asyncio.create_task(ai._worker())
    await asyncio.sleep(0.1)   # varios intentos sin IA
    assert ai.q.qsize() == 2 and [m for m in drain(q) if m["type"] == "event"] == []
    up["v"] = True
    while not ai.q.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()
    assert [m["story"] for m in drain(q) if m["type"] == "event"] == ["urgente", "ctx"]


async def test_un_fallo_al_encender_o_al_triar_no_para_el_worker(monkeypatch):
    monkeypatch.setattr(local_ai, "RETRY_MIN_S", 0.01)
    bus = mem_bus()
    q = bus.subscribe()
    ai = LocalAI(bus, None)
    calls = []

    async def flaky():   # el primer arranque lanza (exe bloqueado); el resto va bien
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("acceso denegado")
        return True

    async def fake(title, src, tier, coins):
        if "rota" in title:
            raise ValueError("respuesta sin importancia")
        return normalize_triage(ELECCIONES, title, coins)

    ai._ensure, ai.classify = flaky, fake
    await run_worker(ai, [("s1", "a", "X", "T2", []), ("s2", "respuesta rota", "X", "T2", []), ("s3", "Sánchez", "X", "T3", [])])
    evs = [m for m in drain(q) if m["type"] == "event"]
    # s1 espera al siguiente intento en vez de perderse; s2 sigue sin triaje, sin archivarse.
    assert sorted((e["story"], e["kind"]) for e in evs) == [("s1", "triaged"), ("s3", "triaged")]


async def test_ensure_no_lanza_si_no_puede_arrancar(monkeypatch):
    bus = mem_bus()
    q = bus.subscribe()
    ai = LocalAI(bus, None)

    async def down():
        return False

    def boom(*a, **k):
        raise PermissionError("acceso denegado")

    ai._healthy, ai._gpu_busy = down, lambda: None
    monkeypatch.setattr(config, "LOCAL_AI_AUTOSTART", True)
    monkeypatch.setattr(config, "LLAMA_SERVER", Path(__file__))
    monkeypatch.setattr(config, "LOCAL_MODEL", Path(__file__))
    monkeypatch.setattr(local_ai, "open", lambda *a, **k: io.BytesIO(), raising=False)   # el log, sin tocar el disco
    monkeypatch.setattr(local_ai.subprocess, "Popen", boom)
    assert await ai._ensure() is False and ai.proc is None
    monkeypatch.setattr(local_ai, "open", boom, raising=False)   # tampoco si falla el log
    assert await ai._ensure() is False
    notes = [m["patch"].get("note", "") for m in drain(q) if m["type"] == "agent"]
    assert notes[-1].startswith("No se pudo arrancar llama-server")


def test_cola_llena_descarta_la_mas_antigua():
    ai = LocalAI(mem_bus(), None)
    for i in range(local_ai.QUEUE_MAX + 2):
        ai.submit(f"s{i}", f"t{i}", "X", "T2", [])
    assert ai.q.qsize() == local_ai.QUEUE_MAX
    got = [ai.q.get_nowait()[0] for _ in range(local_ai.QUEUE_MAX)]
    assert got[0] == f"s{local_ai.QUEUE_MAX + 1}" and got[-1] == "s2"   # sale antes lo nuevo; se pierden las dos primeras
    assert "s0" not in got and "s1" not in got


async def test_una_historia_no_se_repite_en_la_cola_ni_se_tria_dos_veces():
    """La mesa reenvía una historia si sube de estado su fuente: si aún espera, sube de prioridad (una sola entrada);
    si ya se trió, no se repite."""
    bus = mem_bus()
    q = bus.subscribe()
    ai = LocalAI(bus, None)
    ai.submit("sil", "silenciada", "X", "T2", [], prio=-3)
    ai.submit("otra", "otra", "X", "T2", [], prio=2)
    ai.submit("sil", "silenciada", "X", "T2", [], prio=1)    # esperaba con menos: la sustituye (una sola entrada)
    ai.submit("sil", "silenciada", "X", "T2", [], prio=-5)   # menos que la que espera: se ignora
    assert ai.q.qsize() == 2 and ai.q.get_nowait()[0] == "otra"
    ai.submit("otra", "otra", "X", "T2", [], prio=2)
    ai.submit("sil", "silenciada", "X", "T2", [], prio=4)    # sube: pasa delante de «otra»
    assert ai.q.qsize() == 2 and ai.q.get_nowait()[0] == "sil" and ai.q.get_nowait()[0] == "otra"

    async def fake(title, src, tier, coins):
        return normalize_triage(ELECCIONES, title, coins)

    ai._ensure, ai.classify = ok, fake
    await run_worker(ai, [("a", "titular", "X", "T2", [])])
    ai.submit("a", "titular", "X", "T1", [], prio=9)          # ya triada
    assert ai.q.empty() and [m["story"] for m in drain(q) if m["type"] == "event"] == ["a"]


def test_cola_con_prioridad_saca_lo_fresco_y_tira_el_contexto():
    ai = LocalAI(mem_bus(), None)
    ai.submit("ctx", "contexto", "X", "T2", [], prio=1)
    ai.submit("fresca", "fresca", "X", "T1", ["BTC"], prio=7, region="jp")
    assert ai.q.get_nowait()[:1] + ai.q.get_nowait()[:1] == ("fresca", "ctx")
    for i in range(local_ai.QUEUE_MAX):
        ai.submit(f"c{i}", "contexto", "X", "T2", [], prio=1)
    ai.submit("urgente", "urgente", "X", "T0", [], prio=9)
    ai.submit("otro-contexto", "contexto", "X", "T2", [], prio=0)   # menos prioritaria que todo: no entra
    assert ai.q.qsize() == local_ai.QUEUE_MAX
    assert ai.q.get_nowait()[0] == "urgente"
    rest = [ai.q.get_nowait()[0] for _ in range(local_ai.QUEUE_MAX - 1)]
    assert rest[0] == f"c{local_ai.QUEUE_MAX - 1}" and "c0" not in rest   # se fue c0, la más antigua de las menos prioritarias


async def test_sin_vram_usa_el_servidor_de_la_fabrica_y_lo_deja_con_gpu_lock(monkeypatch, tmp_path):
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    busy = {"v": False}

    async def models(_r):
        return web.json_response({"data": [{"id": "modelos\\Qwen3.5-9B-Q4_K_M.gguf"}]})

    async def slots(_r):
        return web.json_response([{"id": 0, "is_processing": busy["v"]}])

    async def health(_r):
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_get("/slots", slots)
    app.router.add_get("/health", health)
    srv = TestServer(app, host="127.0.0.1")
    await srv.start_server()
    lock = tmp_path / "gpu.lock"
    monkeypatch.setattr(config, "GPU_LOCK", lock)
    monkeypatch.setattr(config, "LOCAL_AI_SHARE_PORTS", (srv.port,))
    monkeypatch.setattr(config, "LLAMA_SERVER", Path(__file__))
    monkeypatch.setattr(config, "LOCAL_MODEL", Path(__file__))
    monkeypatch.setattr(config, "LOCAL_AI_AUTOSTART", True)
    async with aiohttp.ClientSession() as s:
        ai = LocalAI(mem_bus(), s)
        ai._gpu_busy = lambda: "Solo hay 5000 MB de VRAM libres"
        assert await ai._ensure() and ai.shared == srv.port and ai.base.endswith(str(srv.port))
        assert await ai._shared_idle()
        busy["v"] = True
        assert not await ai._shared_idle()   # la fábrica está generando: se espera
        lock.write_text("x")
        assert not await ai._ensure() and ai.shared is None   # con gpu.lock se le devuelve su servidor
        assert not await ai._try_shared()
    await srv.close()


def test_cerrar_servidor_heredado_no_revienta_con_netstat_en_cp850(monkeypatch):
    calls = []

    class R:
        def __init__(self, out):
            self.stdout = out

    def fake_run(args, **kw):
        calls.append(args[0])
        assert "text" not in kw   # se leen bytes: la cabecera en español viene en cp850
        if args[0] == "netstat":
            return R(b"  Proto  Direcci\xa2n local  Direcci\xa2n remota  Estado  PID\r\n"
                     b"  TCP    127.0.0.1:%d   0.0.0.0:0   LISTENING   4321\r\n" % config.LOCAL_AI_PORT)
        return R(b"")

    monkeypatch.setattr(local_ai.subprocess, "run", fake_run)
    assert LocalAI(mem_bus(), None)._kill_port_owner(4321) is True
    assert calls == ["netstat", "taskkill"]
    calls.clear()   # otro PID en el puerto (el OpenClaw de trading): no se cierra
    assert LocalAI(mem_bus(), None)._kill_port_owner(999) is False and calls == ["netstat"]
    monkeypatch.setattr(local_ai.subprocess, "run", lambda *a, **k: R(None))   # sin salida: no lanza
    assert LocalAI(mem_bus(), None)._kill_port_owner(4321) is False


class FakeProc:
    def __init__(self, pid=4321):
        self.pid, self.code, self.terminated = pid, None, 0

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated += 1
        self.code = 1


def own_ai(tmp_path, proc=None) -> LocalAI:
    """LocalAI con el fichero del PID en tmp_path (nunca el de la carpeta de datos del bot real)."""
    ai = LocalAI(mem_bus(), None)
    ai.pid_file = tmp_path / "llama-server.pid"
    ai.proc = proc
    return ai


def fake_start(monkeypatch, ai, healthy_after_start: bool) -> list:
    """Arranque sin GPU ni proceso real: Popen devuelve un FakeProc y /health responde según healthy_after_start."""
    procs = []

    def popen(*a, **k):
        procs.append(FakeProc())
        return procs[-1]

    async def healthy():
        return healthy_after_start and bool(procs)

    ai._healthy, ai._gpu_busy = healthy, lambda: None
    monkeypatch.setattr(config, "LOCAL_AI_AUTOSTART", True)
    monkeypatch.setattr(config, "LLAMA_SERVER", Path(__file__))
    monkeypatch.setattr(config, "LOCAL_MODEL", Path(__file__))
    monkeypatch.setattr(local_ai, "open", lambda *a, **k: io.BytesIO(), raising=False)
    monkeypatch.setattr(local_ai.subprocess, "Popen", popen)
    return procs


async def test_arranques_a_la_vez_lanzan_un_solo_servidor_y_la_carga_cuenta_como_uso(monkeypatch, tmp_path):
    ai = own_ai(tmp_path)
    procs = fake_start(monkeypatch, ai, healthy_after_start=True)
    ai.last_use = time.monotonic() - 2000   # apagada por inactividad hace rato
    # El triaje y LocalJudge piden la IA a la vez: antes, dos Popen y un llama-server huérfano.
    assert await asyncio.gather(ai._ready(), ai._ready()) == [True, True]
    assert len(procs) == 1 and ai.proc is procs[0] and ai.pid_file.read_text() == "4321"
    assert time.monotonic() - ai.last_use < 60   # _idle_loop ya no ve 15 min sin uso durante la carga


async def test_inactividad_no_corta_la_carga_y_un_apagado_externo_no_revienta(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GPU_LOCK", tmp_path / "gpu.lock")
    monkeypatch.setattr(config, "LOCAL_AI_IDLE_S", 60)   # aunque la inactividad fuera menor que la carga
    ai = own_ai(tmp_path)
    procs = fake_start(monkeypatch, ai, healthy_after_start=False)
    task = asyncio.create_task(ai._ensure())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if procs:
            break
    ai.last_use = time.monotonic() - 2000
    await ai._idle_tick()   # el tic de 30 s llega a media carga
    assert ai.proc is procs[0] and procs[0].terminated == 0
    ai._stop("prueba")   # apagado desde fuera durante la carga (antes: AttributeError en self.proc.poll())
    assert await task is False and procs[0].terminated == 1 and not ai.pid_file.exists()


async def test_con_gpu_lock_el_servidor_propio_cede_la_gpu_al_quedar_libre(monkeypatch, tmp_path):
    lock = tmp_path / "gpu.lock"
    lock.write_text("x")
    monkeypatch.setattr(config, "GPU_LOCK", lock)
    proc = FakeProc()
    ai = own_ai(tmp_path, proc)
    ai._save_pid(proc.pid)
    ai.ready = True
    busy = {"v": True}

    async def slot_busy():
        return busy["v"]

    ai._healthy, ai._slot_busy = ok, slot_busy
    assert await ai._ready() is False and proc.terminated == 0 and not ai.ready   # generando: no se le pide más
    busy["v"] = False
    await ai._idle_tick()   # en cuanto termina, se apaga y deja la GPU a la fábrica
    assert proc.terminated == 1 and ai.proc is None and not ai.pid_file.exists()
    # Uno ajeno en el puerto (sin PID guardado, como el del OpenClaw de trading) no se cierra: solo se deja de usar.
    kills = []
    ai._kill_port_owner = lambda pid: kills.append(pid) or True
    assert await ai._ready() is False and kills == []
    lock.unlink()
    assert await ai._ready() is True   # sin gpu.lock se vuelve a usar


async def test_inactividad_solo_cierra_servidores_del_bot(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GPU_LOCK", tmp_path / "gpu.lock")
    monkeypatch.setattr(config, "LOCAL_AI_IDLE_S", 900)
    ai = own_ai(tmp_path)
    ai.ready = True   # el 8094 responde, pero no lo arrancó este proceso
    kills, busy = [], {"v": False}

    def kill(pid):
        kills.append(pid)
        return pid == 4321

    async def slot_busy():
        return busy["v"]

    ai._kill_port_owner, ai._slot_busy = kill, slot_busy
    ai.last_use = time.monotonic() - 2000
    await ai._idle_tick()
    assert kills == [] and ai.ready   # el del OpenClaw de trading no se mata (antes: taskkill /F)
    ai._save_pid(4321)   # huérfano de una ejecución anterior del bot
    busy["v"] = True
    await ai._idle_tick()
    assert kills == [] and time.monotonic() - ai.last_use < 60   # está generando: cuenta como uso
    busy["v"] = False
    ai.last_use = time.monotonic() - 2000
    await ai._idle_tick()
    assert kills == [4321] and not ai.ready and not ai.pid_file.exists()
    ai.ready = True
    ai._save_pid(999)   # PID guardado que ya no es el del puerto: se olvida y no se cierra nada
    ai.last_use = time.monotonic() - 2000
    await ai._idle_tick()
    assert kills == [4321, 999] and ai.ready and not ai.pid_file.exists()


async def test_acquire_espera_a_que_la_fabrica_quede_libre(monkeypatch, tmp_path):
    lock = tmp_path / "gpu.lock"
    monkeypatch.setattr(config, "GPU_LOCK", lock)
    ai = LocalAI(mem_bus(), None)
    ai._ensure, ai.shared = ok, 8082
    idle = [False, True]

    async def shared_idle():
        return idle.pop(0)

    ai._shared_idle = shared_idle
    assert await ai.acquire() is True and idle == []   # esperó a que la fábrica acabara

    async def lock_appears():   # la fábrica toma la GPU durante la espera
        lock.write_text("x")
        return True

    ai._shared_idle = lock_appears
    assert await ai.acquire() is False and ai.shared is None


async def test_briefing_se_reintenta_hasta_publicarlo():
    bus = mem_bus()
    q = bus.subscribe()
    ai = LocalAI(bus, None)
    up, calls = {"v": False}, []

    async def acquire():
        return up["v"]

    async def brief(name, when):
        calls.append(name)
        return "Plan"

    ai.acquire, ai.brief = acquire, brief
    now = datetime(2026, 10, 26, 7, 40, tzinfo=timezone.utc)   # 20 min antes de Londres
    await ai.brief_due(now)   # la IA no está lista: antes, el briefing de esa apertura se perdía
    up["v"] = True
    await ai.brief_due(now + timedelta(minutes=1))
    await ai.brief_due(now + timedelta(minutes=2))   # ya publicado: no se repite
    assert calls == ["Apertura Londres"] and [m["title"] for m in drain(q) if m.get("kind") == "brief"] == [
        "Plan para Apertura Londres (09:00 Madrid)"]

    async def broken(name, when):
        calls.append(name)
        raise RuntimeError("400: demasiado contexto")

    ai.brief = broken
    calls.clear()
    ny = datetime(2026, 10, 26, 13, 10, tzinfo=timezone.utc)   # 20 min antes de Nueva York
    for k in range(5):
        await ai.brief_due(ny + timedelta(minutes=k))
    assert calls == ["Apertura Nueva York"] * local_ai.BRIEF_TRIES   # un error que se repite: se rinde


def test_contexto_del_briefing_sin_macro_no_confirmada():
    """El briefing de la IA local antes de cada apertura usa las noticias como la señal: una macro o geopolítica solo si
    la cripto la confirmó (bot/reflejo.py); las de cripto, como siempre."""
    from bot.models import now_ms
    bus = mem_bus()
    ai = LocalAI(bus, None)
    t = now_ms() - 30 * 60_000
    tri = lambda d, res, imp: {"tipo": "macro", "importancia": imp, "sentimiento": "bajista", "monedas": [],
                               "resumen": res, "repercusion": {"direccion": d, "magnitud": 70, "activos": []}}
    for sid, clase, title, triage in (("m1", "macro", "Rusia lanza misiles", tri(-70, "Misiles sobre Kiev", 9)),
                                      ("m2", "macro", "La Fed sube tipos", tri(-60, "La Fed sube los tipos", 8)),
                                      ("c1", "cripto", "Hackeo en Bithumb", tri(-50, "Hackeo en un exchange", 6))):
        bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": sid, "ts": t, "title": title,
                     "coins": [], "text": "", "patch": {"headline": title, "clase": clase}})
        bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": sid, "ts": t + 60_000, "title": title,
                     "coins": [], "text": "", "patch": {"triage": triage}})
    bus.publish({"type": "event", "kind": "reflejo", "agent": "flow", "story": "m2", "ts": t + 120_000, "title": "x",
                 "coins": [], "text": "", "patch": {"reflejo": {"estado": "confirmada", "mov_pct": -1.0, "min": 9,
                                                                "ref": "media", "umbral_pct": 0.9}}})
    txt = ai.brief_context("Apertura Londres", datetime(2026, 10, 26, 7, 40, tzinfo=timezone.utc))
    assert "La Fed sube los tipos" in txt and "Hackeo en un exchange" in txt and "Misiles sobre Kiev" not in txt


async def test_bolsa_de_la_region_solo_si_el_modelo_nombra_alguna():
    bus = mem_bus()
    q = bus.subscribe()
    ai = LocalAI(bus, None)

    async def fake(title, src, tier, coins):
        return normalize_triage(UPBIT if "Upbit" in title else ELECCIONES, title, coins)

    ai._ensure, ai.classify = ok, fake
    await run_worker(ai, [("s1", "Upbit hackeado", "Yonhap", "T1", ["ETH"], 0, "kr"),
                          ("s2", "Elecciones", "Yonhap", "T1", [], 0, "kr")])
    bolsas = {m["story"]: m["patch"]["triage"]["repercusion"]["bolsas"] for m in drain(q) if m["type"] == "event"}
    assert bolsas == {"s1": [], "s2": ["madrid", "seul"]}   # solo cripto: no mueve el KOSPI


def test_ventanas_saltan_el_fin_de_semana_y_cambian_de_hora():
    # Viernes 23-10-2026 a las 15:00 UTC: la siguiente es la CME del domingo; Londres ya en horario de invierno el lunes.
    w = dict(upcoming_windows(datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)))
    assert w["Apertura CME (domingo)"] == datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc)
    assert w["Apertura Tokio"] == datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
    assert w["Apertura Londres"] == datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    assert w["Apertura Nueva York"] == datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)


def test_api_noticias_y_estado():
    bus = mem_bus()
    bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": "s1", "title": "SEC approves Solana ETF",
                 "patch": {"headline": "SEC approves Solana ETF", "src": "SEC · prensa", "tier": "T0", "coins": ["SOL"], "url": "u"}})
    tr = normalize_triage({**UPBIT, "tipo": "regulatorio", "importancia": 9, "resumen": "La SEC aprueba", "motivo": "m",
                           "apetito_riesgo": "sube", "novedad": 80,
                           "repercusion": {"direccion": 70, "magnitud": 85, "activos": ["SOL"]}}, "x", ["SOL"])
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": "s1", "title": "x", "patch": {"triage": tr}})
    bus.publish({"type": "event", "kind": "corroborated", "agent": "hunter", "story": "s1", "title": "x", "patch": {"corrob": [{}, {}]}})
    [n] = recent_news(bus, 3, 5)
    assert n["resumen"] == "La SEC aprueba" and n["importancia"] == 9 and n["confirmaciones"] == 2 and not n["archivada"]
    assert n["tipo"] == "regulatorio" and n["sentimiento"] == "alcista"
    assert recent_news(bus, 3, 10) == []
    bus.publish({"type": "market", "prices": {"BTC": 81000.0},
                 "flow": {"BTC": {"ls": 2.13, "funding": 0.0055, "oi": 0.1, "taker": 0.39, "prem": -0.02}}})
    st = state_summary(bus)
    assert st["precios_usdc"]["BTC"] == 81000.0 and st["flujo_minorista"]["BTC"]["sesgo_minorista"] == "largo"


# --- Triaje v2: esquema y prompt ---

def _line(field: str) -> str:
    """La explicación de un campo en el SYSTEM: de «- campo» hasta la siguiente línea que empieza por «- »."""
    i = SYSTEM.index(f"\n- {field}")
    j = SYSTEM.find("\n- ", i + 1)
    return SYSTEM[i:j if j > 0 else None]


def test_esquema_usa_las_listas_del_sistema():
    props = SCHEMA["properties"]
    rep = props["repercusion"]["properties"]
    assert rep["bolsas"]["items"]["enum"] == list(MARKETS) == BOLSAS
    assert ACTIVOS[:len(config.COINS)] == config.COINS
    assert all(m.index_name in rep["activos"]["items"]["enum"] for m in MARKETS.values())
    assert props["tipo"]["enum"] == TIPOS and TIPOS[-1] == "otro"
    assert props["temas"]["items"]["enum"] == TEMAS and len(TEMAS) == len(TOPICS) == 20
    # El apetito va justo antes de la repercusión, para que el modelo lo decida antes de poner el signo.
    keys = list(props)
    assert props["apetito_riesgo"]["enum"] == APETITO and keys.index("apetito_riesgo") == keys.index("repercusion") - 1

    def closed(o):   # todo obligatorio y sin campos extra, también en los objetos anidados
        return all(not isinstance(v, dict) or v.get("type") != "object" or closed(v) for v in o["properties"].values()) \
            and o["required"] == list(o["properties"]) and o["additionalProperties"] is False
    assert closed(SCHEMA)


def test_prompt_explica_cada_campo_con_sus_anclas():
    assert all(k in SYSTEM for k in MARKETS) and "cualquier idioma" in SYSTEM and "español" in SYSTEM
    for field in ("credibilidad", "alcance", "temporalidad", "repercusion", "novedad"):   # notas de 0 a 100
        line = _line(field)
        for a in ("0", "50", "100"):
            assert re.search(rf"(?<![0-9]){a} = ", line), (field, a)
    assert all(r in _line("importancia") for r in ("0-3", "4-6", "7-8", "9-10", "→ 5", "→ 10"))
    assert all(f"{t} = " in _line("tipo") for t in TIPOS)   # cada tipo con su definición
    assert all(f"T{n}" in _line("credibilidad") for n in range(4))   # también T3, el nivel por defecto de los feeds
    assert "mismo signo" in _line("apetito_riesgo")


# --- Triaje v2: normalización ---

def test_normaliza_respuesta_buena_sin_cambiarla():
    tr = normalize_triage(ELECCIONES, "Pedro Sánchez convoca elecciones generales anticipadas para el 14 de diciembre", [])
    assert list(tr) == KEYS and "apetito_riesgo" not in tr   # el formato del contrato 2.7 no cambia
    assert tr["alcance"] == ELECCIONES["alcance"] and tr["temporalidad"] == ELECCIONES["temporalidad"]
    assert tr["repercusion"] == ELECCIONES["repercusion"] and tr["temas"] == ELECCIONES["temas"]
    assert (tr["tipo"], tr["credibilidad"], tr["novedad"], tr["importancia"]) == ("política", 85, 60, 5)
    assert tr["sentimiento"] == "bajista" and tr["monedas"] == []
    assert tr["resumen"] == ELECCIONES["resumen"] and tr["motivo"] == ELECCIONES["motivo"]


def test_el_apetito_por_el_riesgo_fija_el_signo():
    for title, ap, new_dir, old_dir in DIFICILES:
        good = normalize_triage({"apetito_riesgo": ap, "repercusion": {"direccion": new_dir}}, title, [])
        # Con el signo cambiado, como salía con el prompt anterior, el apetito lo corrige.
        fixed = normalize_triage({"apetito_riesgo": ap, "repercusion": {"direccion": old_dir}}, title, [])
        want, sign = ("bajista", -1) if ap == "baja" else ("alcista", 1)
        assert good["repercusion"]["direccion"] == new_dir and good["sentimiento"] == want, title
        assert fixed["repercusion"]["direccion"] == sign * abs(old_dir) and fixed["sentimiento"] == want, title
    for ap in ("igual", "", None, "quizá"):   # sin apetito claro manda la dirección
        assert normalize_triage({"apetito_riesgo": ap, "repercusion": {"direccion": 40}}, "t", [])["repercusion"]["direccion"] == 40


def test_tope_de_importancia_por_magnitud_y_novedad():
    assert [importance_cap(m, 50) for m in (0, 53, 54, 77, 78, 89, 90, 100)] == [2, 6, 7, 8, 9, 9, 10, 10]
    assert importance_cap(90, 29) == 9 and importance_cap(90, 30) == 10   # uno menos si ya se sabía
    tr = normalize_triage(UPBIT, "업비트, 해킹으로 이더리움 580억원 규모 유출", ["ETH"])
    assert tr["importancia"] == 8 and tr["repercusion"]["magnitud"] == 70   # el modelo dijo 9
    assert normalize_triage({"importancia": 9, "repercusion": {"magnitud": 0}}, "t", [])["importancia"] == 2
    assert normalize_triage({"importancia": 3, "repercusion": {"magnitud": 100}}, "t", [])["importancia"] == 3   # solo baja
    assert normalize_triage({"importancia": 9}, "t", [])["importancia"] == 9   # sin magnitud no hay tope


def test_normaliza_valores_fuera_de_rango():
    tr = normalize_triage({"credibilidad": 150, "novedad": -3, "importancia": 14,
                           "alcance": {"nivel": "global", "puntuacion": 101.4},
                           "temporalidad": {"horizonte": "horas", "urgencia": "7O"},   # texto que no es número
                           "repercusion": {"direccion": -250, "magnitud": 99.6}}, "t", [])
    assert (tr["credibilidad"], tr["novedad"]) == (100, 0)
    assert tr["importancia"] == 9   # 14 → 10, y el tope le quita uno por novedad 0
    assert tr["alcance"]["puntuacion"] == 100 and tr["temporalidad"]["urgencia"] == 0
    assert tr["repercusion"]["direccion"] == -100 and tr["repercusion"]["magnitud"] == 100
    assert normalize_triage({"importancia": "7"}, "t", [])["importancia"] == 7
    assert normalize_triage({"importancia": 6.6}, "t", [])["importancia"] == 7
    assert normalize_triage({"importancia": float("inf"), "credibilidad": None}, "t", [])["importancia"] == 0


def test_sentimiento_sale_de_la_direccion():
    s = {d: normalize_triage({"sentimiento": "alcista", "repercusion": {"direccion": d}}, "t", [])["sentimiento"]
         for d in (-100, -16, -15, 0, 15, 16, 100)}
    assert s == {-100: "bajista", -16: "bajista", -15: "neutral", 0: "neutral", 15: "neutral", 16: "alcista", 100: "alcista"}


def test_listas_cerradas_sin_tildes_ni_mayusculas():
    tr = normalize_triage({"tipo": "Geopolitica", "alcance": {"nivel": "Regional", "puntuacion": 70},
                           "temporalidad": {"horizonte": "dias"},
                           "repercusion": {"activos": ["btc", "Nikkei 225", "DOGE", "BTC", "s&p 500"],
                                           "bolsas": ["Nueva York", "Hong Kong", "paris", "tokio", "Fráncfort"]}}, "t", [])
    assert tr["tipo"] == "geopolítica" and tr["alcance"]["nivel"] == "regional" and tr["temporalidad"]["horizonte"] == "días"
    assert tr["repercusion"]["activos"] == ["BTC", "Nikkei 225", "S&P 500"]
    assert tr["repercusion"]["bolsas"] == ["nuevayork", "hongkong", "tokio", "frankfurt"]
    assert tr["monedas"] == ["BTC"]
    bad = normalize_triage({"tipo": "deportes", "temporalidad": {"horizonte": "meses"}}, "t", [])
    assert bad["tipo"] == "otro" and bad["temporalidad"]["horizonte"] == "horas"


def test_alias_sin_gramatica_y_monedas_none():
    tr = normalize_triage({"repercusion": {"activos": ["Bitcoin", "ethereum", "Solana", "Ripple", "Nikkei", "DAX", "IBEX",
                                                       "FTSE", "kospi", "HangSeng", "S&P500", "Bovespa"],
                                           "bolsas": ["Tokyo", "London", "New York", "Wall Street"]}}, "t", None)
    assert tr["repercusion"]["activos"] == ["BTC", "ETH", "SOL", "XRP", "Nikkei 225", "DAX 40", "IBEX 35", "FTSE 100",
                                            "KOSPI", "Hang Seng", "S&P 500", "Ibovespa"]
    assert tr["repercusion"]["bolsas"][:3] == ["tokio", "londres", "nuevayork"]
    assert tr["monedas"] == ["BTC", "ETH", "SOL", "XRP"]
    assert normalize_triage({}, "t", None)["monedas"] == []   # coins=None (OpenClaw) ya no lanza TypeError


def test_el_indice_anade_su_bolsa():
    tr = normalize_triage({"repercusion": {"activos": ["IBEX 35", "KOSPI"], "bolsas": ["madrid"]}}, "t", [])
    assert tr["repercusion"]["bolsas"] == ["madrid", "seul"]


def test_nivel_de_alcance_invalido_sale_de_la_puntuacion():
    nivel = {p: normalize_triage({"alcance": {"nivel": "planetario", "puntuacion": p}}, "t", [])["alcance"]["nivel"]
             for p in (0, 29, 30, 59, 60, 84, 85, 100)}
    assert nivel == {0: "local", 29: "local", 30: "nacional", 59: "nacional", 60: "regional", 84: "regional",
                     85: "global", 100: "global"}


def test_monedas_de_los_activos_o_de_las_reglas():
    assert normalize_triage({"repercusion": {"activos": ["SOL", "S&P 500"]}}, "t", ["BTC"])["monedas"] == ["SOL"]
    assert normalize_triage({"repercusion": {"activos": ["DAX 40"]}}, "t", ["BTC", "DOGE"])["monedas"] == ["BTC"]
    assert normalize_triage({"repercusion": {"activos": []}}, "t", [])["monedas"] == []


def test_temas_canonicos_con_sinonimos():
    tr = normalize_triage({"tipo": "macro", "temas": ["Guerra", "guerra ", "  ", 7, "Tipos  de interés", "tipo_interes",
                                                      "petróleo"]}, "t", [])
    assert tr["temas"] == ["guerra", "banco central", "energía"]   # sin repetir y sin lo que no es un tema
    tr = normalize_triage({"tipo": "macro", "temas": ["inflacion", "banco_central", "Geopolitica", "macro", "deportes"]}, "t", [])
    assert tr["temas"] == ["inflación", "banco central", "geopolítica", "macroeconomía"]   # claves de topics.py y tope de 4
    assert all(t in TEMAS for t in tr["temas"])
    # El ruido no lleva temas aunque el modelo rellene la lista (pasaba con la lista cerrada).
    assert normalize_triage({"tipo": "otro", "temas": ["tecnología", "cripto"]}, "t", [])["temas"] == []


def test_textos_y_respuesta_rota():
    tr = normalize_triage({"resumen": "  ", "motivo": "palabra " * 40}, "  Sánchez convoca elecciones  ", [])
    assert tr["resumen"] == "Sánchez convoca elecciones"
    assert len(tr["motivo"]) <= 160 and tr["motivo"].endswith("palabra…")   # cortado en una palabra entera
    assert len(normalize_triage({"resumen": "x" * 500}, "t", [])["resumen"]) == 200
    for raw in ({}, None, {"alcance": "global", "repercusion": [1, 2], "temas": "guerra"}):
        tr = normalize_triage(raw, "Titular", ["XRP"])
        assert list(tr) == KEYS and tr["resumen"] == "Titular" and tr["monedas"] == ["XRP"] and tr["tipo"] == "otro"
        assert tr["repercusion"] == {"direccion": 0, "magnitud": 0, "activos": [], "bolsas": []} and tr["sentimiento"] == "neutral"
        assert tr["alcance"] == {"nivel": "local", "puntuacion": 0} and tr["temas"] == []


class FakeResp:
    def __init__(self, content):
        self.content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class FakeSession:
    def __init__(self, content):
        self.content, self.calls = content, []

    def post(self, url, json, timeout):
        self.calls.append((url, json))
        return FakeResp(self.content)


async def test_classify_pide_el_esquema_y_normaliza():
    raw = {**UPBIT, "importancia": 12, "credibilidad": 140, "repercusion": {**UPBIT["repercusion"], "direccion": 30, "magnitud": 95}}
    s = FakeSession("```json\n" + json.dumps(raw, ensure_ascii=False) + "\n```")   # aunque llegue envuelto en un bloque
    ai = LocalAI(mem_bus(), s)
    tr = await ai.classify("업비트 해킹", "연합뉴스", "T1", ["ETH"])
    [(url, body)] = s.calls
    assert url.endswith(f":{config.LOCAL_AI_PORT}/v1/chat/completions") and body["model"] == config.LOCAL_AI_ALIAS
    assert body["response_format"]["json_schema"]["schema"] is SCHEMA and body["messages"][0]["content"] == SYSTEM
    assert "Titular: 업비트 해킹" in body["messages"][1]["content"] and "nivel T1" in body["messages"][1]["content"]
    # +30 con apetito «baja» sale bajista; 12 → 10, y la magnitud 95 lo permite.
    assert (tr["importancia"], tr["credibilidad"], tr["sentimiento"], tr["monedas"]) == (10, 100, "bajista", ["ETH"])
    assert tr["repercusion"]["direccion"] == -30 and list(tr) == KEYS
    # Hallazgos n.º 1 y 42: menos tokens por triaje (antes 700) y los límites del prompt también en el esquema.
    assert body["max_tokens"] == 500 and SCHEMA["properties"]["resumen"]["maxLength"] == 160
    assert SCHEMA["properties"]["motivo"]["maxLength"] == 120


@pytest.mark.parametrize("content", [
    json.dumps({"triaje": UPBIT}, ensure_ascii=False),          # objeto envuelto
    json.dumps({"importance": 7, "summary": "x"}),              # claves en inglés
    json.dumps({**UPBIT, "importancia": "alta"}),               # nota que no es un número
    "[1, 2]", "Lo siento, no puedo puntuar este titular.",      # no es un objeto, o no hay JSON
])
async def test_classify_sin_importancia_lanza_en_vez_de_archivar(content):
    ai = LocalAI(mem_bus(), FakeSession(content))
    with pytest.raises(ValueError):
        await ai.classify("t", "X", "T2", [])
