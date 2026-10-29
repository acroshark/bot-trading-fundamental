"""Modo real con OKX EU (bot/okx_real.py y bot/okx_firma.py) con un OKX simulado: ninguna prueba usa la red, ninguna
usa claves de verdad (las de aquí son falsas) y ninguna manda nada a OKX."""
import ast
import asyncio
import copy
import itertools
import json
import logging
import pickle
import re
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
import pytest

from bot import config, okx_real
from bot.bus import Bus
from bot.okx_firma import Claves, enmascarar, firma_login_ws, firmar, hora_iso, limpiar, texto_a_firmar
from bot.okx_real import (APAGADO, DEMO, PREFIJOS_PROHIBIDOS, REAL, RUTAS_PERMITIDAS, SOLO_LECTURA, ClienteOKX,
                          Instrumento, RealBroker, RutaProhibida, _s, aplicar_limites, comision_pct, confirmar_real,
                          crear_plantilla, evaluar_modo, instrumento_de, leer_env, px_tick, redondear_abajo,
                          ruta_prohibida, techos)
from bot.store import Store

UTC = timezone.utc
T0 = int(datetime(2026, 10, 23, 8, 0, tzinfo=UTC).timestamp() * 1000)   # viernes, 10:00 en Madrid
MIN = 60_000
H = 3_600_000
CLAVE = "CLAVEFALSAkey9876"
SECRETO = "SECRETO_FALSO_SOLO_PARA_PRUEBAS"
FRASE = "FrasePaso!42"
SECRETOS = (CLAVE, SECRETO, FRASE)
ROOT = Path(__file__).resolve().parent.parent

SPOT = {"BTC": ("0.00000001", "0.00001", "0.1"), "ETH": ("0.000001", "0.001", "0.01"),
        "SOL": ("0.00001", "0.01", "0.01"), "XRP": ("0.001", "1", "0.0001")}
PERP = {"BTC": ("0.0001", "0.1"), "ETH": ("0.001", "0.01"), "SOL": ("0.01", "0.01"), "XRP": ("1", "0.0001")}
PX0 = {"BTC": 80000.0, "ETH": 2350.0, "SOL": 102.0, "XRP": 1.32}


def perp_id(coin: str) -> str:
    return f"{coin}-USD_UM_XPERP-310404"


def ok(data):
    return {"code": "0", "msg": "", "data": data}


NO_EXISTE = {"code": "51603", "msg": "Order does not exist", "data": []}


class Resp:
    def __init__(self, status, body):
        self.status = status
        self._text = body if isinstance(body, str) else json.dumps(body)

    async def text(self):
        return self._text


class _Ctx:
    def __init__(self, okx, *args):
        self.okx, self.args = okx, args

    async def __aenter__(self):
        return self.okx.responder(*self.args)

    async def __aexit__(self, *exc):
        return False


class FakeOKX:
    """OKX de mentira con las rutas de la lista blanca: las órdenes a mercado se llenan al momento al precio de px y
    las algo solo saltan con disparar(). fallos: respuestas o cortes de red inyectados, en orden."""

    def __init__(self, clock):
        self.clock = clock
        self.px = dict(PX0)
        self.calls: list[dict] = []
        self.cfg = {"perm": "read_only,trade", "ip": "203.0.113.7", "acctLv": "2", "posMode": "net_mode"}
        self.perps = True
        self.usdc = 1000.0
        self.total = 1000.0
        self.monedas = {c: 0.0 for c in config.COINS}
        self.orders: dict[str, dict] = {}
        self.algos: dict[str, dict] = {}
        self.pos: dict[str, float] = {}
        self.fallos: list[dict] = []
        self.seq = 1000
        self.fee = "-0.001"
        self.parcial: dict[str, float] = {}   # instId → fracción que se llena en la próxima orden a mercado
        self.diferidas: list[list] = []       # [n, método, ruta, cuerpo]: OKX la procesa tras n peticiones más

    def request(self, method, url, headers=None, data=None, timeout=None, allow_redirects=True):
        assert allow_redirects is False   # nunca se siguen redirecciones (no se reenvían las cabeceras firmadas)
        return _Ctx(self, method, str(url), headers or {}, data)

    def responder(self, method, url, headers, data):
        assert url.startswith("https://eea.okx.com/api/v5/"), url
        camino = url[len("https://eea.okx.com"):]
        path, _, qs = camino.partition("?")
        params = dict(parse_qsl(qs))
        raw = data.decode() if data else ""
        body = json.loads(raw) if raw else None
        self.calls.append({"m": method, "path": path, "params": params, "body": body, "headers": dict(headers),
                           "raw": raw, "camino": camino})
        for x in list(self.diferidas):   # una orden que OKX procesa tarde (después de que el bot pregunte)
            x[0] -= 1
            if x[0] <= 0:
                self.diferidas.remove(x)
                self.manejar(x[1], x[2], {}, x[3])
        fallo = next((f for f in self.fallos if f["m"] == method and f["p"] == path), None)
        if fallo:
            self.fallos.remove(fallo)
            if fallo.get("crear"):
                self.manejar(method, path, params, body)
            if fallo.get("crear_tras"):
                self.diferidas.append([fallo["crear_tras"], method, path, body])
            if fallo.get("red"):
                raise aiohttp.ClientConnectionError("conexión cortada")
            return Resp(fallo.get("status", 200), fallo["resp"])
        return Resp(200, self.manejar(method, path, params, body))

    def fila_spot(self, c):
        lot, mn, tick = SPOT[c]
        return {"instId": f"{c}-USDC", "instType": "SPOT", "state": "live", "lotSz": lot, "minSz": mn, "tickSz": tick,
                "ctVal": "", "ruleType": "normal", "tradeQuoteCcyList": ["USDG", "USD", "USDC", "RLUSD"]}

    def fila_perp(self, c):
        ct, tick = PERP[c]
        return {"instId": perp_id(c), "instType": "FUTURES", "instFamily": f"{c}-USD_UM_XPERP", "state": "live",
                "ruleType": "xperp", "lotSz": "1", "minSz": "1", "tickSz": tick, "ctVal": ct, "ctType": "linear",
                "settleCcy": "USD", "expTime": "1933056000000"}

    def llenar(self, inst, side, sz, px, cl=""):
        coin = inst.split("-")[0]
        lote = Decimal(SPOT[coin][0]) if inst.endswith("-USDC") else Decimal(1)
        pedido = Decimal(str(sz))
        llenado = (pedido * Decimal(str(self.parcial.pop(inst, 1.0))) / lote).to_integral_value(ROUND_FLOOR) * lote
        estado = "filled" if llenado >= pedido else "partially_filled"
        sz = format(llenado.normalize(), "f")
        q = float(sz)
        if inst.endswith("-USDC"):
            if side == "buy":
                fee, ccy = -q * 0.001, coin
                self.monedas[coin] += q + fee
                self.usdc -= q * px
            else:
                fee, ccy = -q * px * 0.001, "USDC"
                self.monedas[coin] -= q
                self.usdc += q * px + fee
        else:
            fee, ccy = -q * float(PERP[coin][0]) * px * 0.0005, "USD"
            self.pos[inst] = self.pos.get(inst, 0) + (q if side == "buy" else -q)
        self.seq += 1
        oid = str(self.seq)
        self.orders[oid] = {"ordId": oid, "clOrdId": cl, "instId": inst, "side": side, "sz": format(pedido, "f"),
                            "state": estado, "accFillSz": sz, "avgPx": str(px), "fee": format(fee, ".12f"), "feeCcy": ccy}
        return oid

    @staticmethod
    def buscar(params, tabla, clave, clave_cl):
        if params.get(clave):
            return tabla.get(params[clave])
        return next((x for x in tabla.values() if x.get(clave_cl) and x.get(clave_cl) == params.get(clave_cl)), None)

    def manejar(self, m, path, q, body):
        if path == "/api/v5/public/time":
            return ok([{"ts": str(self.clock())}])
        if path == "/api/v5/public/instruments":
            if q["instType"] == "SPOT":
                return ok([self.fila_spot(c) for c in config.COINS])
            return ok([self.fila_perp(q["instFamily"].split("-")[0])])
        if path == "/api/v5/account/config":
            return ok([dict(self.cfg)])
        if path == "/api/v5/account/instruments":
            if q["instType"] == "SPOT":
                return ok([{"instId": f"{c}-USDC"} for c in config.COINS])
            return ok([{"instId": perp_id(c)} for c in config.COINS] if self.perps else [])
        if path == "/api/v5/account/trade-fee":
            return ok([{"taker": self.fee, "maker": "-0.0008", "takerUSDC": self.fee}])
        if path == "/api/v5/account/balance":
            det = [{"ccy": "USDC", "availBal": str(self.usdc), "frozenBal": "0"}]
            det += [{"ccy": c, "availBal": format(v, ".10f"), "frozenBal": "0"} for c, v in self.monedas.items()]
            return ok([{"totalEq": str(self.total), "details": det}])
        if path == "/api/v5/account/positions":
            return ok([{"instId": k, "pos": str(v), "avgPx": "100", "mgnMode": "isolated"}
                       for k, v in self.pos.items() if v])
        if path == "/api/v5/account/set-leverage":
            return ok([{"lever": body["lever"], "instId": body["instId"], "mgnMode": body["mgnMode"]}])
        if path == "/api/v5/trade/order" and m == "POST":
            oid = self.llenar(body["instId"], body["side"], body["sz"], self.px[body["instId"].split("-")[0]],
                              body.get("clOrdId", ""))
            return ok([{"ordId": oid, "clOrdId": body.get("clOrdId", ""), "sCode": "0", "sMsg": ""}])
        if path == "/api/v5/trade/order":
            o = self.buscar(q, self.orders, "ordId", "clOrdId")
            return ok([dict(o)]) if o else NO_EXISTE
        if path == "/api/v5/trade/orders-pending":
            return ok([dict(o) for o in self.orders.values() if o["state"] in ("live", "partially_filled")])
        if path == "/api/v5/trade/cancel-order":
            self.orders[body["ordId"]]["state"] = "canceled"
            return ok([{"ordId": body["ordId"], "sCode": "0", "sMsg": ""}])
        if path == "/api/v5/trade/order-algo" and m == "POST":
            self.seq += 1
            aid = str(self.seq)
            self.algos[aid] = {**body, "algoId": aid, "state": "live", "tpTriggerPx": body.get("tpTriggerPx", "")}
            return ok([{"algoId": aid, "algoClOrdId": body.get("algoClOrdId", ""), "sCode": "0", "sMsg": ""}])
        if path == "/api/v5/trade/order-algo":
            a = self.buscar(q, self.algos, "algoId", "algoClOrdId")
            return ok([dict(a)]) if a else NO_EXISTE
        if path == "/api/v5/trade/orders-algo-pending":
            return ok([dict(a) for a in self.algos.values() if a["state"] == "live" and a["ordType"] == q["ordType"]])
        if path == "/api/v5/trade/amend-algos":
            a = self.algos.get(body["algoId"])
            if not a or a["state"] != "live":
                return {"code": "1", "msg": "", "data": [{"algoId": body["algoId"], "sCode": "51603", "sMsg": "no"}]}
            a["slTriggerPx"] = body["newSlTriggerPx"]
            return ok([{"algoId": a["algoId"], "sCode": "0", "sMsg": ""}])
        if path == "/api/v5/trade/cancel-algos":
            for x in body:
                if x["algoId"] in self.algos and self.algos[x["algoId"]]["state"] == "live":
                    self.algos[x["algoId"]]["state"] = "canceled"
            return ok([{"algoId": x["algoId"], "sCode": "0", "sMsg": ""} for x in body])
        raise AssertionError(f"ruta no simulada: {m} {path}")

    def disparar(self, algo_id, lado, px):
        """La algo salta en OKX: queda «effective» y su orden a mercado se llena a px."""
        a = self.algos[algo_id]
        oid = self.llenar(a["instId"], a["side"], a["sz"], px)
        a.update(state="effective", actualSide=lado, actualPx=str(px), ordId=oid)

    def post(self, path=None):
        return [c for c in self.calls if c["m"] == "POST" and (path is None or c["path"] == path)]

    def vivas(self):
        return [a for a in self.algos.values() if a["state"] == "live"]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def escribir_claves(carpeta: Path, demo: str | None = "0", **cambios):
    vals = {"OKX_API_KEY": CLAVE, "OKX_API_SECRET": SECRETO, "OKX_API_PASSPHRASE": FRASE, "OKX_DEMO": demo, **cambios}
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "okx.env").write_text("# pruebas\n" + "".join(f"{k}={v}\n" for k, v in vals.items() if v is not None),
                                     encoding="utf-8")


class Env:
    """Bus, base, reloj, OKX simulado y archivos de secretos de mentira alrededor de un RealBroker."""

    def __init__(self, tmp_path, modo="real", store=None):
        self.tmp = tmp_path
        self.dir = tmp_path / "secretos"
        self.env = {}
        if modo in ("real", "solo_lectura", "demo"):
            escribir_claves(self.dir, "1" if modo == "demo" else "0")
        if modo == "real":
            self.env["BOT_MODO_REAL"] = "1"
            assert confirmar_real(self.dir, "activo el modo real", T0)[0]
        self.store = store or Store(tmp_path / "real.db")
        self.bus = Bus(self.store)
        self.q = self.bus.subscribe()
        self.clock = Clock(T0)
        self.mono = Mono()
        self.okx = FakeOKX(self.clock)
        self.slept: list[float] = []
        self.msgs: list[dict] = []
        self.spread: dict[str, float] = {}
        self.broker = self.nuevo()

    async def dormir(self, s):
        self.slept.append(s)
        self.mono.t += s

    def market(self):
        return {"prices": dict(self.okx.px), "updated": {c: self.clock() for c in self.okx.px},
                "spread": dict(self.spread)}

    def nuevo(self) -> RealBroker:
        return RealBroker(self.bus, self.store, self.market, session=self.okx, clock=self.clock, carpeta=self.dir,
                          env=self.env, dormir=self.dormir, mono=self.mono)

    async def reiniciar(self) -> RealBroker:
        """El bot se para y vuelve: otro RealBroker con la misma base y los mismos archivos."""
        self.broker = self.nuevo()
        await self.broker.arrancar()
        return self.broker

    async def ronda(self, seg=1.0, **px):
        self.clock.t += int(seg * 1000)
        self.mono.t += seg
        self.okx.px.update(px)
        await self.broker.ronda()

    def mensajes(self):
        while not self.q.empty():
            self.msgs.append(json.loads(self.q.get_nowait()))
        return self.msgs

    def evs(self, kind=None, story=None):
        return [m for m in self.mensajes() if m["type"] == "event" and (kind is None or m["kind"] == kind)
                and (story is None or m["story"] == story)]

    def veto(self, story):
        e = self.evs("riskko", story)
        assert e, f"no se vetó {story}"
        return e[-1]["patch"]["risk"]["why"]

    async def abrir(self, t, story) -> dict:
        assert self.broker.submit(t, story, "titular", "Impulso", "Apertura Londres"), self.evs("riskko")[-1:]
        assert self.broker.decide(story, True)
        await self.ronda()
        rec = self.broker.positions[story]
        assert rec["estado"] == "abierta", rec
        return rec


def ticket(coin="BTC", side="long", **kw) -> dict:
    """Ticket de «6 diarias v2» (salida A): stop 2 %, la mitad en 3R y el resto con cerrojo en +2R y seguimiento a 2R."""
    t = {"coin": coin, "side": side, "mode": "Impulso de apertura", "risk": 0.5, "stopPct": 2.0, "trailPct": None,
         "timing": "now", "win": "Apertura Londres", "winAt": None, "tp1R": 3.0, "tp2R": None, "tp1Frac": 0.5,
         "beR": None, "trailStartR": None, "lockR": 3.0, "lockFloorR": 2.0, "lockTrailR": 2.0,
         "winEnd": T0 + 24 * H, "holdLabel": "tope de 24 h", "winEndActive": T0 + 7 * 24 * H,
         "holdActiveLabel": "7 días de seguridad", "maxAgeMs": 5 * MIN,
         "instrument": "spot" if side == "long" else "perp"}
    t.update(kw)
    return t


@pytest.fixture
async def env(tmp_path):
    e = Env(tmp_path)
    assert await e.broker.arrancar(), e.broker.ultimo_error
    return e


# ---------------------------------------------------------------- firma

HORA_EJEMPLO = "2020-12-08T09:08:57.715Z"
VECTORES = [
    (f"{HORA_EJEMPLO}GET/api/v5/account/balance?ccy=BTC", "hmUuaby5SkxPIOoaU720J2F14BPNwWnb+vHnznQPzwk="),
    (f'{HORA_EJEMPLO}POST/api/v5/trade/order{{"instId":"BTC-USDC","tdMode":"cash","side":"buy","ordType":"market",'
     '"sz":"25","tgtCcy":"quote_ccy","tradeQuoteCcy":"USDC"}', "qFbT5fQQnyqojR2aPUiUf1ESmLqp5nlRQOgU3K7AlH0="),
    ("1538054050GET/users/self/verify", "hPKFlqJ1WTfTZSeGBCr1Z925EvAzblK3avhRds+BIBc="),
]


@pytest.mark.parametrize("mensaje,esperada", VECTORES)
def test_firma_vectores_de_docs06(mensaje, esperada):
    """El ejemplo oficial (hora + GET + /api/v5/account/balance?ccy=BTC) y los vectores de docs/06 (comprobados con
    openssl) con el secreto falso de las pruebas."""
    assert firmar(SECRETO, mensaje) == esperada


def test_firma_cabeceras_y_login():
    ms = int(datetime(2020, 12, 8, 9, 8, 57, tzinfo=UTC).timestamp()) * 1000 + 715
    assert hora_iso(ms) == HORA_EJEMPLO
    assert texto_a_firmar(HORA_EJEMPLO, "get", "/api/v5/account/balance?ccy=BTC") == VECTORES[0][0]
    cab = Claves(CLAVE, SECRETO, FRASE).cabeceras(HORA_EJEMPLO, "GET", "/api/v5/account/balance?ccy=BTC")
    assert cab == {"OK-ACCESS-KEY": CLAVE, "OK-ACCESS-SIGN": VECTORES[0][1], "OK-ACCESS-TIMESTAMP": HORA_EJEMPLO,
                   "OK-ACCESS-PASSPHRASE": FRASE}
    assert firma_login_ws(SECRETO, 1538054050) == VECTORES[2][1]
    assert Claves(CLAVE, SECRETO, FRASE).login_ws(1538054050)["sign"] == VECTORES[2][1]


async def test_el_cliente_firma_los_mismos_bytes_que_envia(tmp_path):
    """El cuerpo se serializa una vez: se firma y se envía lo mismo (vector 2 de docs/06)."""
    okx = FakeOKX(Clock(0))
    ms = int(datetime(2020, 12, 8, 9, 8, 57, tzinfo=UTC).timestamp()) * 1000 + 715
    cli = ClienteOKX(okx, Claves(CLAVE, SECRETO, FRASE), demo=False, clock=lambda: ms)
    cli.escritura = True
    cuerpo = {"instId": "BTC-USDC", "tdMode": "cash", "side": "buy", "ordType": "market", "sz": "25",
              "tgtCcy": "quote_ccy", "tradeQuoteCcy": "USDC"}
    await cli.privado("POST", "/api/v5/trade/order", cuerpo=cuerpo)
    c = okx.calls[-1]
    assert c["headers"]["OK-ACCESS-SIGN"] == VECTORES[1][1]
    assert c["headers"]["OK-ACCESS-TIMESTAMP"] == HORA_EJEMPLO
    assert "x-simulated-trading" not in c["headers"]


async def test_todas_las_peticiones_privadas_van_firmadas_y_las_publicas_no(env):
    privadas = [c for c in env.okx.calls if not c["path"].startswith(("/api/v5/public/", "/api/v5/market/"))]
    publicas = [c for c in env.okx.calls if c not in privadas]
    assert privadas and publicas
    for c in privadas:
        h = c["headers"]
        assert h["OK-ACCESS-KEY"] == CLAVE and h["OK-ACCESS-PASSPHRASE"] == FRASE
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", h["OK-ACCESS-TIMESTAMP"])
        assert h["OK-ACCESS-SIGN"] == firmar(SECRETO, h["OK-ACCESS-TIMESTAMP"] + c["m"] + c["camino"] + c["raw"])
        assert "x-simulated-trading" not in h
    assert all(not any(k.startswith("OK-ACCESS") for k in c["headers"]) for c in publicas)


async def test_demo_lleva_la_cabecera_de_demo(tmp_path):
    e = Env(tmp_path, "demo")
    assert e.broker.estado.modo == DEMO
    assert await e.broker.arrancar()
    assert all(c["headers"].get("x-simulated-trading") == "1" for c in e.okx.calls)
    assert e.broker.puede_abrir()


# ---------------------------------------------------------------- modos: nada se activa sin todo

@pytest.mark.parametrize("claves,demo0,var,conf", list(itertools.product([True, False], repeat=4)))
def test_solo_es_real_con_las_tres_claves_okx_demo_0_la_variable_y_la_confirmacion(tmp_path, claves, demo0, var, conf):
    d = tmp_path / "s"
    escribir_claves(d, "0")   # la confirmación solo se guarda con las claves reales completas
    if conf:
        assert confirmar_real(d, "ACTIVO EL MODO REAL", T0)[0]
    escribir_claves(d, "0" if demo0 else "1", **({} if claves else {"OKX_API_PASSPHRASE": ""}))
    est = evaluar_modo(d, {"BOT_MODO_REAL": "1"} if var else {})
    assert (est.modo == REAL) == (claves and demo0 and var and conf)
    if not claves:
        assert est.modo == APAGADO and "OKX_API_PASSPHRASE" in " ".join(est.motivos)
    elif not demo0:
        assert est.modo == DEMO
    elif not (var and conf):
        assert est.modo == SOLO_LECTURA
        txt = " ".join(est.motivos)
        assert ("BOT_MODO_REAL=1" in txt) == (not var)
        assert ("confirmación" in txt) == (not conf)


@pytest.mark.parametrize("caso", ["sin_archivo", "falta_secreto", "falta_clave", "sin_okx_demo", "okx_demo_raro",
                                  "variable_true", "parada", "parada_demo", "confirmacion_falsa"])
def test_modos_que_no_son_real(tmp_path, caso):
    d = tmp_path / "s"
    env = {"BOT_MODO_REAL": "1"}
    if caso != "sin_archivo":
        extra = {"falta_secreto": {"OKX_API_SECRET": ""}, "falta_clave": {"OKX_API_KEY": None},
                 "sin_okx_demo": {"OKX_DEMO": None}, "okx_demo_raro": {"OKX_DEMO": "si"}}.get(caso, {})
        escribir_claves(d, "1" if caso == "parada_demo" else "0", **extra)
        confirmar_real(d, "ACTIVO EL MODO REAL", T0)
    if caso == "variable_true":
        env = {"BOT_MODO_REAL": "true"}
    if caso in ("parada", "parada_demo"):
        (d / "PARAR_REAL").write_text("", encoding="utf-8")
    if caso == "confirmacion_falsa":
        (d / "real_confirmado").write_text(json.dumps({"frase": "otra cosa", "ms": T0}), encoding="utf-8")
    est = evaluar_modo(d, env)
    esperado = {"sin_archivo": APAGADO, "falta_secreto": APAGADO, "falta_clave": APAGADO}.get(caso, SOLO_LECTURA)
    assert est.modo == esperado, est
    assert not est.opera()
    txt = " ".join(est.motivos)
    assert not any(s in txt for s in SECRETOS)
    if caso == "falta_secreto":
        assert "OKX_API_SECRET" in txt
    if caso.startswith("parada"):
        assert "Parada de emergencia" in txt


def test_confirmacion_escrita(tmp_path):
    d = tmp_path / "s"
    ok_, why = confirmar_real(d, "activar", T0)
    assert not ok_ and "ACTIVO EL MODO REAL" in why
    assert not (d / "real_confirmado").exists()
    ok_, why = confirmar_real(d, "ACTIVO EL MODO REAL", T0)   # sin claves no se guarda
    assert not ok_ and "claves" in why and not (d / "real_confirmado").exists()
    escribir_claves(d, "0")
    (d / "PARAR_REAL").write_text("", encoding="utf-8")
    ok_, why = confirmar_real(d, "  activo   el modo REAL ", T0)
    assert ok_
    body = json.loads((d / "real_confirmado").read_text(encoding="utf-8"))
    assert body["frase"] == "ACTIVO EL MODO REAL" and body["ms"] == T0 and body["fecha"].startswith("23-10-2026")
    assert body["huella"] == Claves(CLAVE, SECRETO, FRASE).huella()
    assert not any(s in json.dumps(body) for s in SECRETOS)
    assert not (d / "PARAR_REAL").exists()
    assert okx_real.leer_confirmacion(d)["ms"] == T0


def test_la_confirmacion_vale_solo_para_esas_claves_y_nunca_en_demo(tmp_path):
    """Una confirmación dada en demo no se guarda, y una dada con otras claves no vale para el modo real."""
    d = tmp_path / "s"
    escribir_claves(d, "1")
    ok_, why = confirmar_real(d, "ACTIVO EL MODO REAL", T0)
    assert not ok_ and "OKX_DEMO=0" in why and not (d / "real_confirmado").exists()
    escribir_claves(d, "0")
    assert confirmar_real(d, "ACTIVO EL MODO REAL", T0)[0]
    assert evaluar_modo(d, {"BOT_MODO_REAL": "1"}).modo == REAL
    escribir_claves(d, "0", OKX_API_KEY="OTRACLAVEreal1234")   # el usuario cambia de claves
    est = evaluar_modo(d, {"BOT_MODO_REAL": "1"})
    assert est.modo == SOLO_LECTURA and "confirmación" in " ".join(est.motivos)
    escribir_claves(d, "0")   # vuelve a las de antes: la confirmación de esas claves sigue valiendo
    assert evaluar_modo(d, {"BOT_MODO_REAL": "1"}).modo == REAL


async def test_apagado_no_hace_ninguna_peticion(tmp_path):
    e = Env(tmp_path, "apagado")
    assert e.broker.estado.modo == APAGADO and e.broker.cliente is None
    assert not await e.broker.arrancar()
    await e.ronda(10)
    assert not e.broker.submit(ticket(), "s1")
    assert e.veto("s1").startswith("Modo real no activo")
    assert e.okx.calls == []
    pf = e.broker.portfolio()
    assert pf["halted"] is True and pf["positions"] == []


async def test_solo_lectura_lee_y_nunca_ordena(tmp_path):
    e = Env(tmp_path, "solo_lectura")
    assert e.broker.estado.modo == SOLO_LECTURA
    assert await e.broker.arrancar()
    assert e.broker.cuenta["equity"] == 1000.0 and e.broker.comisiones["spot"] == pytest.approx(0.1)
    assert not e.broker.cliente.escritura
    assert not e.broker.submit(ticket(), "s1")
    assert "BOT_MODO_REAL=1" in e.veto("s1")
    antes = len(e.okx.calls)
    with pytest.raises(RutaProhibida):
        await e.broker.cliente.privado("POST", "/api/v5/trade/order", cuerpo={"instId": "BTC-USDC"})
    assert len(e.okx.calls) == antes
    await e.ronda(400)
    assert e.okx.post() == []


async def test_falta_una_clave_es_apagado_y_no_dice_las_otras(tmp_path):
    d = tmp_path / "secretos"
    escribir_claves(d, "0", OKX_API_SECRET=None)
    est = evaluar_modo(d, {"BOT_MODO_REAL": "1"})
    assert est.modo == APAGADO and "OKX_API_SECRET" in est.motivos[0]
    assert CLAVE not in repr(est) and FRASE not in repr(est)


def test_leer_env():
    vals = leer_env('# comentario\nexport OKX_API_KEY="abc"\nOKX_API_SECRET = def\n\nOKX_API_PASSPHRASE=a#b=c\n'
                    "OKX_DEMO='1'\nbasura\n")
    assert vals == {"OKX_API_KEY": "abc", "OKX_API_SECRET": "def", "OKX_API_PASSPHRASE": "a#b=c", "OKX_DEMO": "1"}


# ---------------------------------------------------------------- secretos

async def test_los_secretos_nunca_salen_en_logs_eventos_base_ni_web(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    e = Env(tmp_path)
    assert await e.broker.arrancar()
    rec = await e.abrir(ticket(), "s1")
    p1, p2 = rec["partes"]
    e.okx.disparar(p1["algoId"], "tp", 84800.0)
    await e.ronda(5, BTC=84900.0)
    # OKX devuelve un error que repite la clave, el secreto y la frase: no sale en ningún sitio
    e.okx.fallos.append({"m": "GET", "p": "/api/v5/account/balance",
                         "resp": {"code": "50000", "msg": f"Body error {CLAVE} {SECRETO} {FRASE}", "data": []}})
    await e.ronda(31)
    assert e.broker.ultimo_error and "50000" in e.broker.ultimo_error["texto"]
    okx_real.log.warning("prueba del filtro: %s %s %s", CLAVE, SECRETO, FRASE)
    try:
        raise ValueError(f"excepción con {SECRETO}")
    except ValueError:
        okx_real.log.exception("traza con %s", FRASE)
    e.broker.parada_emergencia("prueba")
    await e.ronda()
    # lo que sí lleva la clave: las cabeceras que van a OKX (si no, la prueba no probaría nada)
    assert any(c["headers"].get("OK-ACCESS-KEY") == CLAVE for c in e.okx.calls)
    textos = [caplog.text, json.dumps(e.mensajes(), ensure_ascii=False), repr(e.broker), repr(e.broker.cliente),
              repr(e.broker.estado), str(e.broker.estado.claves), json.dumps(e.broker.vista_real(), ensure_ascii=False),
              json.dumps(e.broker.portfolio(), ensure_ascii=False, default=str),
              json.dumps(e.broker.results(), ensure_ascii=False, default=str)]
    for (tabla,) in e.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        textos.append(json.dumps(e.store.db.execute(f"SELECT * FROM {tabla}").fetchall(), ensure_ascii=False))
    todo = "\n".join(textos)
    for s in SECRETOS:
        assert s not in todo, f"secreto a la vista: {s[:4]}…"
    assert "prueba del filtro" in caplog.text and "***" in caplog.text


def test_las_claves_no_se_ensenan_ni_se_copian():
    c = Claves(CLAVE, SECRETO, FRASE)
    for txt in (repr(c), str(c), f"{c}", repr([c]), str(limpiar({"x": c}))):
        assert not any(s in txt for s in SECRETOS)
    with pytest.raises(TypeError):
        json.dumps(c)
    with pytest.raises(TypeError):
        pickle.dumps(c)
    with pytest.raises(TypeError):
        copy.copy(c)
    assert Claves(CLAVE, "", FRASE).faltan() == ["OKX_API_SECRET"]


@pytest.mark.parametrize("texto", [
    "OK-ACCESS-KEY: algo-que-no-se-ve", "OKX_API_SECRET=otro_valor", '{"passphrase": "frase de paso"}',
    "apiKey=abcd1234efgh", "clave 0123abcd-1111-2222-3333-444455556666 suelta", "secreto 0123456789ABCDEF0123456789ABCDEF",
    "firma hPKFlqJ1WTfTZSeGBCr1Z925EvAzblK3avhRds+BIBc= suelta", '"sign":"xyz123"'])
def test_enmascarar_tapa_lo_que_se_parece_a_un_secreto(texto):
    out = enmascarar(texto)
    assert "***" in out
    for trozo in ("algo-que-no-se-ve", "otro_valor", "frase", "abcd1234efgh", "0123abcd-1111", "0123456789ABCDEF",
                  "hPKFlqJ1", "xyz123"):
        assert trozo not in out


# ---------------------------------------------------------------- salida A en el exchange

async def test_salida_A_parcial_cerrojo_y_seguimiento_en_okx(env):
    rec = await env.abrir(ticket(), "s1")
    # entrada a mercado por cantidad y niveles desde el precio medio real, redondeados a tickSz
    entrada = env.okx.post("/api/v5/trade/order")[0]["body"]
    assert entrada == {"instId": "BTC-USDC", "side": "buy", "ordType": "market", "sz": "0.000125",
                       "clOrdId": rec["clOrdId"], "tdMode": "cash", "tgtCcy": "base_ccy", "tradeQuoteCcy": "USDC",
                       "banAmend": True}
    assert rec["entry"] == 80000.0 and rec["sl"] == 78400.0 and rec["tp1"] == 84800.0 and rec["floor"] == 83200.0
    assert rec["sz"] == "0.00012487"   # lo comprado menos la comisión (en BTC), hacia abajo
    p1, p2 = rec["partes"]
    assert (p1["sz"], p2["sz"]) == ("0.00006243", "0.00006244")
    a1, a2 = env.okx.algos[p1["algoId"]], env.okx.algos[p2["algoId"]]
    assert a1["ordType"] == "oco" and a1["slTriggerPx"] == "78400" and a1["tpTriggerPx"] == "84800"
    assert a2["ordType"] == "conditional" and a2["slTriggerPx"] == "78400" and not a2["tpTriggerPx"]
    assert a1["side"] == a2["side"] == "sell" and a1["tdMode"] == "cash" and "reduceOnly" not in a1
    assert env.evs("filled", "s1")
    # antes del cerrojo no se mueve nada
    await env.ronda(5, BTC=82000.0)
    await env.ronda(5, BTC=84600.0)
    assert env.okx.post("/api/v5/trade/amend-algos") == []
    # OKX ejecuta la parcial (a mercado, algo por debajo del disparo): el cerrojo sube el stop de lo que queda al suelo
    env.okx.disparar(p1["algoId"], "tp", 84790.0)
    await env.ronda(5, BTC=84900.0)
    am = env.okx.post("/api/v5/trade/amend-algos")
    assert [(c["body"]["algoId"], c["body"]["newSlTriggerPx"]) for c in am] == [(p2["algoId"], "83200")]
    rec = env.broker.positions["s1"]
    assert rec["locked"] and rec["sl"] == 83200.0 and rec["hold_until"] == T0 + 7 * 24 * H
    parcial = [m for m in env.evs("manage", "s1") if "partial" in m["patch"]]
    assert parcial and parcial[0]["patch"]["partial"]["px"] == 84790.0
    # seguimiento a 2R del máximo, solo hacia arriba
    await env.ronda(5, BTC=88000.0)
    await env.ronda(5, BTC=86000.0)
    await env.ronda(5, BTC=89000.0)
    await env.ronda(1, BTC=89100.0)   # sube menos de 0,1 R y antes de 5 s: no se toca
    assert [c["body"]["newSlTriggerPx"] for c in env.okx.post("/api/v5/trade/amend-algos")] == ["83200", "84800", "85800"]
    assert env.okx.algos[p2["algoId"]]["slTriggerPx"] == "85800"
    # salta el stop de seguimiento en OKX: la operación se cierra con las dos partes
    env.okx.disparar(p2["algoId"], "sl", 85790.0)
    await env.ronda(5, BTC=85700.0)
    assert "s1" not in env.broker.positions
    t = env.broker.closed[-1]
    assert t["reason"] == "Stop de seguimiento tras salida parcial"
    assert t["pnl"] > 0 and 2.8 < t["R"] < 3.6
    closed = env.evs("closed", "s1")[-1]
    assert closed["patch"]["result"]["why"] == t["reason"] and closed["patch"]["real"]["modo"] == REAL


async def test_el_cerrojo_por_precio_mueve_todas_las_partes_vivas(env):
    rec = await env.abrir(ticket(), "s1")
    await env.ronda(5, BTC=84850.0)   # nuestro precio llega a +3R antes de que OKX ejecute la parcial
    am = env.okx.post("/api/v5/trade/amend-algos")
    assert sorted(c["body"]["algoId"] for c in am) == sorted(p["algoId"] for p in rec["partes"])
    assert {c["body"]["newSlTriggerPx"] for c in am} == {"83200"}


async def test_corto_en_xperp_y_cierre_por_tiempo(env):
    rec = await env.abrir(ticket("ETH", "short"), "s1")
    lev = env.okx.post("/api/v5/account/set-leverage")[0]["body"]
    assert lev == {"instId": perp_id("ETH"), "lever": "2", "mgnMode": "isolated"}
    entrada = env.okx.post("/api/v5/trade/order")[0]["body"]
    assert entrada["side"] == "sell" and entrada["sz"] == "4" and entrada["tdMode"] == "isolated"
    assert rec["sl"] == 2397.0 and rec["tp1"] == 2209.0 and rec["floor"] == 2256.0
    assert [p["sz"] for p in rec["partes"]] == ["2", "2"]
    for p in rec["partes"]:
        a = env.okx.algos[p["algoId"]]
        assert a["side"] == "buy" and a["reduceOnly"] is True and a["tdMode"] == "isolated"
    await env.ronda(24 * 3600 + 60)
    assert "s1" not in env.broker.positions
    posts = [c["path"] for c in env.okx.post()]
    cierre = max(i for i, p in enumerate(posts) if p == "/api/v5/trade/order")
    assert posts.index("/api/v5/trade/cancel-algos") > cierre   # en X-Perps: primero se cierra, luego se cancelan
    body = env.okx.post("/api/v5/trade/order")[-1]["body"]
    assert body["side"] == "buy" and body["reduceOnly"] is True and body["sz"] == "4"
    assert env.broker.closed[-1]["reason"] == "Cierre por tiempo (tope de 24 h)"
    assert env.okx.vivas() == [] and not env.okx.pos.get(perp_id("ETH"))


async def test_cierre_por_tiempo_al_contado_cancela_antes_de_vender(env):
    await env.abrir(ticket(), "s1")
    await env.ronda(24 * 3600 + 60)
    posts = [c["path"] for c in env.okx.post()]
    venta = max(i for i, p in enumerate(posts) if p == "/api/v5/trade/order")
    assert posts.index("/api/v5/trade/cancel-algos") < venta
    body = env.okx.post("/api/v5/trade/order")[-1]["body"]
    assert body["side"] == "sell" and body["sz"] == "0.00012487" and body["tgtCcy"] == "base_ccy"
    assert env.broker.closed[-1]["reason"] == "Cierre por tiempo (tope de 24 h)"


async def test_cierre_manual(env):
    await env.abrir(ticket(), "s1")
    assert env.broker.close("s1", "Cierre manual desde la web")
    await env.ronda()
    assert env.broker.closed[-1]["reason"] == "Cierre manual desde la web"
    assert not env.broker.close("s1")


async def test_un_contrato_no_se_parte(env):
    """BTC en X-Perps con 10 USDC: 1 contrato (≈ 8 USD). Sin parcial: toda la posición sigue al cerrojo, y se dice."""
    rec = await env.abrir(ticket("BTC", "short"), "s1")
    assert len(rec["partes"]) == 1 and rec["partes"][0]["sz"] == "1"
    assert env.okx.algos[rec["partes"][0]["algoId"]]["ordType"] == "conditional"
    assert "sin salida parcial" in env.evs("filled", "s1")[-1]["text"]


async def test_si_no_se_puede_poner_el_stop_se_cierra_al_momento(env):
    for _ in range(4 * 3):
        env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order-algo",
                               "resp": {"code": "1", "msg": "", "data": [{"sCode": "51000", "sMsg": "Parameter error"}]}})
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    assert "s1" not in env.broker.positions
    assert env.broker.closed[-1]["reason"].startswith("Sin stop en OKX")
    assert env.okx.monedas["BTC"] < 1e-7


# ---------------------------------------------------------------- límites duros

async def test_importe_maximo_y_techos_de_los_limites(env, monkeypatch):
    b = env.broker
    r = b.set_limites({"max_usdc": 50})
    assert not r["ok"] and "no se puede subir" in r["why"] and b.limites["max_usdc"] == 10
    for k, v in (("perdida_dia_pct", 5), ("max_posiciones", 6), ("max_entradas_dia", 7)):
        assert not b.set_limites({k: v})["ok"]
    assert b.set_limites({"max_usdc": 5, "perdida_dia_pct": 2, "max_posiciones": 3, "max_entradas_dia": 3})["ok"]
    assert b.limites == {"max_usdc": 5.0, "perdida_dia_pct": 2.0, "max_posiciones": 3, "max_entradas_dia": 3}
    await env.abrir(ticket(), "s1")
    assert env.evs("filled", "s1")[-1]["patch"]["exec"]["notional"] <= 5.0
    assert env.nuevo().limites == b.limites   # guardados
    assert b.set_limites({"max_usdc": 10})["ok"] and b.limites["max_usdc"] == 10
    assert not b.set_limites({"max_usdc": 0})["ok"] and not b.set_limites({"otro": 1})["ok"]
    monkeypatch.setenv("BOT_REAL_MAX_USDC", "20")   # el techo del importe solo se sube en el PC del usuario
    assert techos()["max_usdc"] == 20 and b.set_limites({"max_usdc": 15})["ok"]


def test_aplicar_limites_nunca_por_encima_del_techo():
    techo = {"max_usdc": 10.0, "perdida_dia_pct": 3.0, "max_posiciones": 4, "max_entradas_dia": 6}
    out, malos = aplicar_limites({"max_usdc": 99, "max_posiciones": 9}, {}, techo)
    assert out["max_usdc"] == 10.0 and out["max_posiciones"] == 4 and not malos


async def test_una_por_moneda_y_posiciones_maximas(env):
    env.broker.set_limites({"max_posiciones": 2})
    await env.abrir(ticket(), "s1")
    assert not env.broker.submit(ticket(), "s2")
    assert env.veto("s2").startswith("Ya hay una operación en BTC")
    assert env.evs("riskko", "s2")[-1]["patch"]["risk"]["motivo_codigo"] == "moneda_ocupada"
    await env.abrir(ticket("ETH"), "s3")
    assert not env.broker.submit(ticket("SOL"), "s4")
    assert env.veto("s4").startswith("Posiciones abiertas o en curso 2 de 2")


async def test_tope_de_entradas_del_dia(env):
    env.broker.set_limites({"max_entradas_dia": 2})
    rec = await env.abrir(ticket(), "s1")
    for p in rec["partes"]:
        env.okx.disparar(p["algoId"], "sl", 78400.0)
    await env.ronda(5, BTC=78400.0)
    assert env.broker.closed[-1]["reason"] == "Stop de pérdida"
    await env.abrir(ticket("ETH"), "s2")
    assert not env.broker.submit(ticket("SOL"), "s3")
    assert env.veto("s3").startswith("Tope diario de entradas: 2 de 2")
    assert env.evs("riskko", "s3")[-1]["patch"]["risk"]["motivo_codigo"] == "tope_dia"


async def test_perdida_maxima_del_dia(env):
    await env.abrir(ticket(), "s1")
    env.okx.total = 965.0   # el saldo de OKX manda: −3,5 %
    await env.ronda(31)
    assert env.broker.halt_until
    assert not env.broker.submit(ticket("ETH"), "s2")
    assert env.evs("riskko", "s2")[-1]["patch"]["risk"]["motivo_codigo"] == "perdida_dia"
    pf = env.broker.portfolio()
    assert pf["halted"] and pf["limits"]["halted"]
    assert "s1" in env.broker.positions and len(env.okx.post("/api/v5/trade/order")) == 1   # no cierra nada


@pytest.mark.parametrize("cambios,fee,texto", [
    ({"stopPct": 1.0}, "-0.001", "Regla del usuario: stop"),
    ({"tp2R": 1.5, "tp1Frac": 0.0, "lockR": None}, "-0.001", "Regla del usuario: objetivo de 1,50 R"),
    ({"tp1Frac": 0.0, "lockR": None}, "-0.001", "Regla del usuario: sin objetivo fijo"),
    ({"stopPct": 1.5, "tp2R": 2.0, "tp1Frac": 0.0, "lockR": None}, "-0.001", "neto con la comisión real"),
    ({"stopPct": 1.5}, "-0.001", "Regla del usuario: sin objetivo fijo"),   # suelo de 2R con stop 1,5 %: < 3 % neto
    ({"stopPct": 1.5, "tp2R": 2.4, "tp1Frac": 0.0, "lockR": None}, "-0.0035", "neto con la comisión real"),
    ({"timing": "window", "winAt": T0 + H}, "-0.001", "solo entradas inmediatas"),
    ({"side": "short", "instrument": "spot"}, "-0.001", "Corto al contado"),
])
async def test_reglas_del_usuario_siempre_aunque_esten_apagadas_en_config(tmp_path, cambios, fee, texto):
    assert config.USER_RULES is False   # conftest las apaga: el modo real las aplica igual
    e = Env(tmp_path)
    e.okx.fee = fee
    assert await e.broker.arrancar()
    assert not e.broker.submit(ticket(**cambios), "s1")
    assert texto in e.veto("s1")
    assert e.okx.post() == []


async def test_la_comision_real_cambia_la_regla(tmp_path):
    """Con la comisión del paper (0,10 %) el objetivo de 2,4R con stop 1,5 % pasa; con la del contado sin X-Perps
    (0,35 %), no."""
    e = Env(tmp_path)
    assert await e.broker.arrancar()
    assert e.broker.submit(ticket(stopPct=1.5, tp2R=2.4, tp1Frac=0.0, lockR=None), "s1")


async def test_por_debajo_del_minimo_de_okx_y_sin_saldo(env, tmp_path):
    env.broker.set_limites({"max_usdc": 1})
    assert not env.broker.submit(ticket("XRP"), "s1")
    assert env.veto("s1").startswith("Por debajo del mínimo de OKX en XRP-USDC")
    e = Env(tmp_path / "b")
    e.okx.total, e.okx.usdc = 0.0, 0.0   # la cuenta de hoy: sin dinero
    assert await e.broker.arrancar()
    assert not e.broker.submit(ticket(), "s1")
    assert e.veto("s1").startswith("Saldo 0 en la cuenta de trading")


async def test_sin_perpetuos_no_hay_cortos(tmp_path):
    e = Env(tmp_path)
    e.okx.perps = False
    assert await e.broker.arrancar()
    assert not e.broker.cortos_ok and "X-Perps" in e.broker.cortos_por_que
    assert all(k[1] == "spot" for k in e.broker.instrumentos)
    assert not e.broker.submit(ticket("ETH", "short"), "s1")
    assert e.veto("s1").startswith("Sin cortos")
    assert e.broker.vista_real()["cortos"]["ok"] is False
    assert e.broker.submit(ticket("ETH"), "s2")   # los largos al contado sí


async def test_modo_de_posicion_distinto_de_net_mode_sin_cortos(tmp_path):
    e = Env(tmp_path)
    e.okx.cfg["posMode"] = "long_short_mode"
    assert await e.broker.arrancar()
    assert not e.broker.cortos_ok and "net_mode" in e.broker.cortos_por_que


async def test_permiso_de_retirada_bloquea_y_no_ordena(tmp_path):
    e = Env(tmp_path)
    e.okx.cfg.update(perm="read_only,withdraw,trade", ip="")
    await e.broker.arrancar()
    assert "retirada" in e.broker.bloqueo and not e.broker.puede_escribir() and not e.broker.cliente.escritura
    assert any("IP" in a for a in e.broker.avisos)
    assert not e.broker.submit(ticket(), "s1")
    assert "retirada" in e.veto("s1")
    await e.ronda(400)
    assert e.okx.post() == []


async def test_clave_rechazada_no_reintenta_y_bloquea(tmp_path):
    e = Env(tmp_path)
    e.okx.fallos.append({"m": "GET", "p": "/api/v5/account/config",
                         "resp": {"code": "50111", "msg": f"Invalid OK-ACCESS-KEY {CLAVE}", "data": []}})
    assert not await e.broker.arrancar()
    assert "50111" in e.broker.bloqueo and CLAVE not in e.broker.bloqueo
    assert len([c for c in e.okx.calls if c["path"] == "/api/v5/account/config"]) == 1


# ---------------------------------------------------------------- aprobación

async def test_aprobacion_automatica_solo_en_demo(env, tmp_path):
    assert not env.broker.set_mode("auto") and env.broker.aprobacion == "veto"
    assert env.broker.submit(ticket(), "s1")
    assert env.evs("humanwait", "s1")
    await env.ronda(31)   # sin veto en 30 s: aprobada y ejecutada
    assert env.broker.positions["s1"]["estado"] == "abierta"
    assert env.broker.set_mode("manual")
    assert env.broker.submit(ticket("ETH"), "s2")
    await env.ronda(11 * 60)
    assert "s2" not in env.broker.positions and env.evs("humanko", "s2")
    d = Env(tmp_path / "demo", "demo")
    assert await d.broker.arrancar()
    assert d.broker.set_mode("auto")
    assert d.broker.submit(ticket(), "s1")
    await d.ronda()
    assert d.broker.positions["s1"]["estado"] == "abierta"


async def test_cancelar_y_rechazar(env):
    assert env.broker.submit(ticket(), "s1")
    assert env.broker.cancel("s1")
    assert env.broker.submit(ticket("ETH"), "s2")
    assert env.broker.decide("s2", False)
    await env.ronda(31)
    assert env.okx.post() == [] and not env.broker.en_curso


async def test_una_aprobacion_tardia_no_ejecuta_un_plan_viejo(env):
    assert env.broker.submit(ticket(maxAgeMs=10_000), "s1")
    env.clock.t += 20_000
    env.broker.decide("s1", True)
    await env.ronda()
    assert "después de enviarla" in env.evs("blocked", "s1")[-1]["text"]
    assert env.okx.post() == []


# ---------------------------------------------------------------- parada de emergencia

async def test_parada_cancela_entradas_y_no_cierra_posiciones(env):
    b = env.broker
    await env.abrir(ticket(), "s1")
    assert b.submit(ticket("ETH"), "s2")   # pendiente de aprobar
    env.okx.orders["999"] = {"ordId": "999", "clOrdId": "bfrviejaentrada0e", "instId": "SOL-USDC", "state": "live",
                             "accFillSz": "0", "sz": "1"}
    env.okx.orders["998"] = {"ordId": "998", "clOrdId": "manual1", "instId": "XRP-USDC", "state": "live",
                             "accFillSz": "0", "sz": "5"}
    vivas = len(env.okx.vivas())
    assert b.parada_emergencia("prueba")
    assert env.evs("humanko", "s2") and (env.dir / "PARAR_REAL").exists()
    assert not (env.dir / "real_confirmado").exists()
    await env.ronda()
    assert [c["body"]["ordId"] for c in env.okx.post("/api/v5/trade/cancel-order")] == ["999"]
    assert env.okx.orders["998"]["state"] == "live"
    assert env.okx.post("/api/v5/trade/cancel-algos") == [] and len(env.okx.post("/api/v5/trade/order")) == 1
    assert len(env.okx.vivas()) == vivas and "s1" in b.positions
    assert b.estado.modo == SOLO_LECTURA and not b.cliente.escritura and not b.puede_abrir()
    assert evaluar_modo(env.dir, env.env).modo == SOLO_LECTURA
    assert not b.submit(ticket("SOL"), "s3") and "Parada de emergencia" in env.veto("s3")
    assert not b.close("s1")
    with pytest.raises(RutaProhibida):
        await b.cliente.privado("POST", "/api/v5/trade/order", cuerpo={})
    n = len(env.okx.post())
    await env.ronda(10, BTC=90000.0)   # en solo lectura no se mueve el stop aunque el precio lo pida
    assert len(env.okx.post()) == n
    assert env.evs("real_aviso")
    # para volver: confirmar otra vez
    assert b.confirmar("ACTIVO EL MODO REAL")["ok"]
    await env.ronda(6)
    assert b.estado.modo == REAL and b.puede_abrir()


async def test_parada_con_cierre_pedido(env):
    await env.abrir(ticket(), "s1")
    env.broker.parada_emergencia("cerrar todo", cerrar_posiciones=True)
    await env.ronda()
    assert "s1" not in env.broker.positions
    assert env.broker.closed[-1]["reason"].startswith("Parada de emergencia")
    assert env.broker.estado.modo == SOLO_LECTURA


@pytest.mark.parametrize("contenido,cierra", [("", False), ("CERRAR", True)])
async def test_parada_por_archivo(env, contenido, cierra):
    await env.abrir(ticket(), "s1")
    (env.dir / "PARAR_REAL").write_text(contenido, encoding="utf-8")
    await env.ronda(6)
    assert env.broker.estado.modo == SOLO_LECTURA and not env.broker.cliente.escritura
    assert ("s1" in env.broker.positions) is not cierra
    assert not (env.dir / "real_confirmado").exists()
    (env.dir / "PARAR_REAL").unlink()   # quitar el archivo no basta: hay que confirmar otra vez
    await env.ronda(6)
    assert env.broker.estado.modo == SOLO_LECTURA


# ---------------------------------------------------------------- reconciliación: manda OKX

async def test_reconciliacion_toma_el_stop_de_okx(env):
    rec = await env.abrir(ticket(), "s1")
    p2 = rec["partes"][1]
    env.okx.algos[p2["algoId"]]["slTriggerPx"] = "79000"
    b = await env.reiniciar()
    rec = b.positions["s1"]
    assert rec["partes"][1]["sl"] == 79000.0 and rec["sl"] == 79000.0
    assert any("manda OKX" in m["text"] for m in env.evs("real_aviso"))


async def test_reconciliacion_cierra_lo_que_salto_con_el_bot_parado(env):
    rec = await env.abrir(ticket(), "s1")
    for p in rec["partes"]:
        env.okx.disparar(p["algoId"], "sl", 78390.0)
    b = await env.reiniciar()
    assert "s1" not in b.positions
    t = b.closed[-1]
    assert t["reason"] == "Stop de pérdida" and t["pnl"] < 0 and -1.2 < t["R"] < -1.0


async def test_reconciliacion_posiciones_externas_y_ordenes_viejas(env):
    env.okx.pos[perp_id("SOL")] = -3
    env.okx.orders["999"] = {"ordId": "999", "clOrdId": "bfrviejaentrada0e", "instId": "BTC-USDC", "state": "live",
                             "accFillSz": "0", "sz": "0.0001"}
    env.okx.orders["998"] = {"ordId": "998", "clOrdId": "", "instId": "XRP-USDC", "state": "live", "accFillSz": "0",
                             "sz": "5"}
    b = await env.reiniciar()
    assert [x["coin"] for x in b.externas] == ["SOL"]
    assert env.okx.orders["999"]["state"] == "canceled" and env.okx.orders["998"]["state"] == "live"
    assert not b.submit(ticket("SOL"), "s1")
    assert "fuera del bot" in env.veto("s1")
    pf = b.portfolio()
    assert any(p.get("externa") and p["coin"] == "SOL" for p in pf["positions"])
    assert b.reconciliacion["ordenes_ajenas"] == 1


async def test_entrada_enviada_sin_confirmar_se_protege_al_reconciliar(env):
    for _ in range(2):   # falla la consulta del llenado y también la reconciliación de esa misma ronda
        env.okx.fallos.append({"m": "GET", "p": "/api/v5/trade/order",
                               "resp": {"code": "50026", "msg": "System error", "data": []}})
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    rec = env.broker.positions["s1"]
    assert rec["estado"] == "enviando" and rec["ordId"] and env.okx.algos == {}
    b = await env.reiniciar()
    rec = b.positions["s1"]
    assert rec["estado"] == "abierta" and len(env.okx.vivas()) == 2


async def test_entrada_enviada_sin_confirmar_se_protege_en_la_misma_ronda(env):
    env.okx.fallos.append({"m": "GET", "p": "/api/v5/trade/order",
                           "resp": {"code": "50026", "msg": "System error", "data": []}})
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    assert env.broker.positions["s1"]["estado"] == "abierta" and len(env.okx.vivas()) == 2
    assert any("aún no se sabe" in m["text"] for m in env.evs("real_aviso"))


# ---------------------------------------------------------------- idempotencia, reintentos y bucle

@pytest.mark.parametrize("llego", [True, False])
async def test_idempotencia_tras_un_corte_de_red(env, llego):
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order", "red": True, "crear": llego})
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    assert len(env.okx.post("/api/v5/trade/order")) == 1   # nunca se repite a ciegas
    assert any(c["m"] == "GET" and c["params"].get("clOrdId") for c in env.okx.calls)
    if llego:
        assert env.broker.positions["s1"]["estado"] == "abierta"
    else:
        assert "s1" not in env.broker.positions
        assert "no llegó a OKX" in env.evs("blocked", "s1")[-1]["text"]
        assert env.store.db.execute("SELECT COUNT(*) FROM real_trades").fetchone()[0] == 0


async def test_limite_de_peticiones_se_reintenta_con_espera(env):
    for _ in range(2):
        env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order-algo",
                               "resp": {"code": "50011", "msg": "Rate limit reached", "data": []}})
    rec = await env.abrir(ticket(), "s1")
    assert len(rec["partes"]) == 2 and all(p["estado"] == "viva" for p in rec["partes"])
    assert len(env.okx.post("/api/v5/trade/order-algo")) == 4 and env.slept


async def test_el_limitador_espera_su_turno():
    okx = FakeOKX(Clock(T0))
    mono, dormidos = Mono(), []

    async def dormir(s):
        dormidos.append(s)
        mono.t += s

    cli = ClienteOKX(okx, Claves(CLAVE, SECRETO, FRASE), False, clock=lambda: T0, dormir=dormir, mono=mono)
    for _ in range(6):   # balance: 10 / 2 s en la documentación; el cliente usa la mitad
        await cli.privado("GET", "/api/v5/account/balance")
    assert len(dormidos) == 1 and dormidos[0] > 1.9


async def test_hora_desfasada_resincroniza_y_reintenta(tmp_path):
    e = Env(tmp_path)
    e.okx.fallos.append({"m": "GET", "p": "/api/v5/account/config",
                         "resp": {"code": "50102", "msg": "Timestamp request expired", "data": []}})
    assert await e.broker.arrancar()
    rutas = [c["path"] for c in e.okx.calls]
    i = rutas.index("/api/v5/account/config")
    assert rutas[i + 1] == "/api/v5/public/time" and rutas[i + 2] == "/api/v5/account/config"


async def test_ninguna_excepcion_tumba_el_bucle(env):
    await env.abrir(ticket(), "s1")
    env.broker.get_market = lambda: 1 / 0
    await env.ronda(5)
    for _ in range(5):
        env.okx.fallos.append({"m": "GET", "p": "/api/v5/account/balance", "red": True})
    await env.ronda(31)
    assert env.broker.ultimo_error and "red" in env.broker.ultimo_error["texto"]
    llamadas = []

    async def ronda_rota(now=None):
        llamadas.append(1)
        raise RuntimeError("fallo inventado")

    async def dormir(s):
        if len(llamadas) >= 3:
            raise asyncio.CancelledError
    env.broker.ronda = ronda_rota
    env.broker._dormir = dormir
    with pytest.raises(asyncio.CancelledError):
        await env.broker.run()
    assert len(llamadas) == 3


# ---------------------------------------------------------------- redondeos

def test_redondeos_y_formato():
    assert redondear_abajo("0.000124875", "0.00000001") == Decimal("0.00012487")
    assert redondear_abajo("7.5757", "0.001") == Decimal("7.575")
    assert redondear_abajo("1.999999", "1") == Decimal("1")
    assert px_tick(78400.04, "0.1", False) == Decimal("78400.0")
    assert px_tick(78400.04, "0.1", True) == Decimal("78400.1")
    assert px_tick(2208.995, "0.01", False) == Decimal("2208.99")
    assert _s(Decimal("84800.0")) == "84800" and _s(Decimal("1E-8")) == "0.00000001" and _s("0") == "0"
    assert _s(Decimal("0.000125000")) == "0.000125"


def test_instrumentos_y_contratos():
    okx = FakeOKX(Clock(T0))
    btc = instrumento_de(okx.fila_perp("BTC"), "BTC", "perp")
    eth = instrumento_de(okx.fila_perp("ETH"), "ETH", "perp")
    assert btc.sz_de_qty(0.000125) == 1 and eth.sz_de_qty(10 / 2350) == 4
    assert eth.qty_de_sz(4) == Decimal("0.004")
    spot = instrumento_de(okx.fila_spot("BTC"), "BTC", "spot")
    assert spot.sz_de_qty(10 / 80000) == Decimal("0.00012500") and spot.ct_val == 1
    assert instrumento_de({**okx.fila_spot("BTC"), "state": "suspend"}, "BTC", "spot") is None
    assert instrumento_de({**okx.fila_spot("BTC"), "lotSz": "0"}, "BTC", "spot") is None
    xrp = instrumento_de(okx.fila_spot("XRP"), "XRP", "spot")
    partes, nota = RealBroker._partes(None, xrp, Decimal("1.5"), {"tp1_frac": 0.5, "tp2R": None})
    assert len(partes) == 1 and "sin salida parcial" in nota
    partes, nota = RealBroker._partes(None, spot, Decimal("0.00012487"), {"tp1_frac": 0.5, "tp2R": None})
    assert [p["sz"] for p in partes] == ["0.00006243", "0.00006244"] and nota is None


def test_comision_de_la_cuenta():
    assert comision_pct({"taker": "-0.001"}) == pytest.approx(0.1)
    assert comision_pct({"taker": "-0.001", "takerUSDC": "-0.0035"}) == pytest.approx(0.35)
    assert comision_pct({"taker": "0.0002"}) == 0.0   # positivo: reembolso
    assert comision_pct({}) is None and comision_pct(None) is None


# ---------------------------------------------------------------- ni retiradas ni transferencias

PROHIBIDAS = ["/api/v5/asset/withdrawal", "/api/v5/asset/transfer", "/api/v5/asset/cancel-withdrawal",
              "/api/v5/fiat/create-withdrawal", "/api/v5/asset/subaccount/transfer", "/api/v5/users/subaccount/list",
              "/api/v5/account/subaccount/balances", "/api/v5/asset/convert/trade", "/api/v5/finance/savings/purchase-redempt",
              "/api/v5/account/spot-manual-borrow-repay", "/api/v5/account/position/margin-balance",
              "/api/v5/asset/deposit-address", "/api/v5/trade/order/../../asset/transfer", "/api/v5/trade/order?x=1",
              "https://eea.okx.com/api/v5/asset/transfer", "/api/v5/account/bills"]


def test_la_lista_blanca_no_tiene_nada_que_mueva_dinero():
    for metodo, ruta in RUTAS_PERMITIDAS:
        assert not ruta_prohibida(ruta), ruta
        assert not ruta.startswith(("/api/v5/asset/", "/api/v5/users/", "/api/v5/fiat/", "/api/v5/finance/"))
        assert not any(p in ruta for p in ("withdraw", "transfer", "borrow", "repay", "subaccount"))
        assert metodo in ("GET", "POST")


@pytest.mark.parametrize("ruta", PROHIBIDAS)
async def test_las_rutas_prohibidas_se_rechazan_antes_de_firmar(ruta):
    okx = FakeOKX(Clock(T0))
    cli = ClienteOKX(okx, Claves(CLAVE, SECRETO, FRASE), False, clock=lambda: T0)
    cli.escritura = True   # ni con escritura
    for metodo in ("GET", "POST"):
        with pytest.raises(RutaProhibida):
            await cli.privado(metodo, ruta, cuerpo={"amt": "1", "ccy": "USDC"})
    assert okx.calls == []


def _cadenas(modulo: Path) -> list[str]:
    return [n.value for n in ast.walk(ast.parse(modulo.read_text(encoding="utf-8")))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


@pytest.mark.parametrize("nombre", ["okx_real.py", "okx_firma.py"])
def test_el_codigo_no_llama_a_retiradas_ni_transferencias(nombre):
    """Toda ruta /api/ escrita en el código es de la lista blanca (o de la lista de prefijos que se rechazan), y toda
    llamada a privado/publico lleva la ruta escrita tal cual (nada montado por partes)."""
    modulo = ROOT / "bot" / nombre
    blancas = {r for _, r in RUTAS_PERMITIDAS} | set(PREFIJOS_PROHIBIDOS) | {"/api/v5/"}
    for s in _cadenas(modulo):
        for ruta in re.findall(r"/api/v5/[A-Za-z0-9/_\-.]*", s):
            assert ruta in blancas, f"{nombre}: ruta fuera de la lista blanca: {ruta}"
        if s not in PREFIJOS_PROHIBIDOS:   # /users/self/verify es el login del WebSocket, no una ruta REST
            assert not re.search(r"/(asset|fiat|finance)/|/users/subaccount|withdrawal|subaccount/|borrow-repay", s), s
    for n in ast.walk(ast.parse(modulo.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("privado", "publico"):
            arg = n.args[1] if n.func.attr == "privado" else n.args[0]
            assert isinstance(arg, ast.Constant) and arg.value.startswith("/api/v5/"), ast.dump(n)[:200]


# ---------------------------------------------------------------- plantilla y archivos

def test_plantilla_vacia_y_nunca_okx_env(tmp_path):
    d = tmp_path / "secretos"
    ruta = crear_plantilla(d)
    assert ruta == d / "okx.env.ejemplo" and not (d / "okx.env").exists()
    txt = ruta.read_text(encoding="utf-8")
    for nombre in ("OKX_API_KEY=\n", "OKX_API_SECRET=\n", "OKX_API_PASSPHRASE=\n", "OKX_DEMO=0\n"):
        assert nombre in txt
    assert "NUNCA «Retirar»" in txt and "IP" in txt and "frase de paso" in txt and "PARAR_REAL" in txt
    assert "SOLO «Lectura»" in txt and "OKX_DEMO=1" in txt   # para empezar, solo lectura; la demo es aparte
    assert crear_plantilla(d) is None
    assert evaluar_modo(d, {"BOT_MODO_REAL": "1"}).modo == APAGADO   # el ejemplo no se lee nunca
    e = Env(tmp_path / "otro", "apagado")
    assert (e.dir / "okx.env.ejemplo").exists() and not (e.dir / "okx.env").exists()


async def test_el_codigo_no_escribe_el_archivo_de_claves(env):
    antes = (env.dir / "okx.env").read_bytes()
    mt = (env.dir / "okx.env").stat().st_mtime_ns
    await env.abrir(ticket(), "s1")
    env.broker.parada_emergencia("x")
    await env.ronda(6)
    env.broker.confirmar("ACTIVO EL MODO REAL")
    await env.ronda(6)
    assert (env.dir / "okx.env").read_bytes() == antes and (env.dir / "okx.env").stat().st_mtime_ns == mt


# ---------------------------------------------------------------- interfaz para el motor y la web

async def test_la_cartera_tiene_el_formato_del_paper(env):
    from bot.apertura import paper_day_entries
    await env.abrir(ticket(), "s1")
    assert env.broker.submit(ticket("ETH"), "s2")
    pf = env.broker.portfolio()
    assert {"type", "ts", "equity", "start_equity", "day_pnl", "day_pnl_pct", "positions", "pending", "scheduled",
            "closed", "limits", "mode", "halted", "caps"} <= set(pf)
    assert pf["halted"] is False and pf["positions"][0]["coin"] == "BTC" and pf["pending"][0]["coin"] == "ETH"
    assert set(paper_day_entries(pf, env.clock())) == {"s1", "s2"}
    json.dumps(pf)
    for nombre in ("submit", "portfolio", "decide", "close", "cancel", "set_mode", "halt", "resume", "results", "run",
                   "announce", "publish_portfolio"):
        assert callable(getattr(env.broker, nombre))
    assert env.broker.halt() and env.broker.portfolio()["halted"] is True
    assert not env.broker.submit(ticket("SOL"), "s3")
    assert env.broker.resume() and env.broker.portfolio()["halted"] is False


async def test_mensaje_cuenta_real_para_el_panel(env):
    await env.abrir(ticket(), "s1")
    env.broker.publish_cuenta(force=True)
    msg = [m for m in env.mensajes() if m["type"] == "cuenta_real"][-1]
    assert msg["modo"] == REAL and msg["puede_abrir"] and msg["frase_confirmacion"] == "ACTIVO EL MODO REAL"
    assert msg["limites"]["max_usdc"] == 10 and msg["techos"]["max_entradas_dia"] == 6
    assert msg["posiciones"][0]["story"] == "s1" and msg["archivos"]["claves"] is True
    assert msg["cuenta"]["equity"] == 1000.0 and msg["comisiones"]["spot"] == pytest.approx(0.1)
    assert "portfolio" not in [m["type"] for m in env.mensajes()]   # el portfolio del bus sigue siendo del paper


async def test_resultados(env):
    rec = await env.abrir(ticket(), "s1")
    for p in rec["partes"]:
        env.okx.disparar(p["algoId"], "sl", 78400.0)
    await env.ronda(5)
    r = env.broker.results()
    assert r["real"] and r["trades"][0]["story"] == "s1" and r["by_coin"][0]["coin"] == "BTC"
    assert len(r["equity_curve"]) == 2 and r["daily"][0]["n"] == 1


# ---------------------------------------------------------------- órdenes en duda: nunca se olvidan ni se duplican

async def test_entrada_en_duda_con_la_consulta_tambien_cortada_no_se_olvida(env):
    """El POST de entrada se corta (la orden SÍ llega) y las consultas por clOrdId también fallan un rato: el registro
    se queda «enviando», cuenta en los límites y, en cuanto OKX contesta, la posición recibe su stop."""
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order", "red": True, "crear": True})
    for _ in range(40):
        env.okx.fallos.append({"m": "GET", "p": "/api/v5/trade/order", "red": True})
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    rec = env.broker.positions["s1"]
    assert rec["estado"] == "enviando" and env.okx.algos == {}
    assert env.broker.entradas_hoy(env.clock()) == 1 and not env.broker.submit(ticket(), "s2")
    assert env.store.db.execute("SELECT COUNT(*) FROM real_trades").fetchone()[0] == 1
    assert env.broker.vista_real()["sin_stop"] == ["BTC"]
    env.okx.fallos.clear()   # OKX vuelve a contestar
    await env.ronda(3)
    rec = env.broker.positions["s1"]
    assert rec["estado"] == "abierta" and len(env.okx.vivas()) == 2
    assert len(env.okx.post("/api/v5/trade/order")) == 1   # nunca se repitió


async def test_entrada_que_okx_procesa_tarde_se_encuentra_y_se_protege(env):
    """Corte de red y OKX procesa la orden después de la primera consulta (que dice que no existe): no se da por
    perdida antes de su expTime."""
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order", "red": True, "crear_tras": 3})
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    rec = env.broker.positions["s1"]
    assert rec["estado"] == "abierta" and len(env.okx.vivas()) == 2
    consultas = [c for c in env.okx.calls if c["m"] == "GET" and c["params"].get("clOrdId") == rec["clOrdId"]]
    assert len(consultas) >= 2 and len(env.okx.post("/api/v5/trade/order")) == 1


async def test_las_ordenes_a_mercado_llevan_exptime(env):
    rec = await env.abrir(ticket(), "s1")
    entrada = env.okx.post("/api/v5/trade/order")[0]
    assert entrada["headers"]["expTime"] == str(rec["exp_ms"]) == str(T0 + 1000 + okx_real.EXP_ORDEN_MS)
    assert all("expTime" not in c["headers"] for c in env.okx.post("/api/v5/trade/order-algo"))
    await env.ronda(24 * 3600 + 60)   # cierre por tiempo: también con expTime
    assert "expTime" in env.okx.post("/api/v5/trade/order")[-1]["headers"]


@pytest.mark.parametrize("resp,status", [({"code": "50013", "msg": "Systems are busy", "data": []}, 200),
                                         ({"code": "50001", "msg": "Service temporarily unavailable", "data": []}, 200),
                                         ({"code": "50000", "msg": "Error", "data": []}, 503),
                                         ("<html>bad gateway</html>", 502)])
async def test_okx_ocupado_en_un_post_no_se_repite_a_ciegas(env, resp, status):
    """Con OKX ocupado la orden quizá se procesó: se consulta por clOrdId, nunca se manda otra vez."""
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order", "crear": True, "status": status, "resp": resp})
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    assert len(env.okx.post("/api/v5/trade/order")) == 1 and len(env.okx.orders) == 1
    rec = env.broker.positions["s1"]
    assert rec["estado"] == "abierta" and sum(Decimal(p["sz"]) for p in rec["partes"]) == Decimal("0.00012487")


async def test_ocupado_en_un_get_si_se_repite():
    okx = FakeOKX(Clock(T0))

    async def dormir(s):
        pass
    cli = ClienteOKX(okx, Claves(CLAVE, SECRETO, FRASE), False, clock=lambda: T0, dormir=dormir)
    okx.fallos.append({"m": "GET", "p": "/api/v5/account/balance", "resp": {"code": "50013", "msg": "busy", "data": []}})
    assert await cli.privado("GET", "/api/v5/account/balance")
    assert len(okx.calls) == 2


async def test_una_stop_en_duda_no_se_duplica(env):
    """El POST de una algo se corta y OKX la crea un poco después: no se pone otra; se busca por su algoClOrdId y se
    enlaza."""
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order-algo", "red": True, "crear_tras": 3})
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    rec = env.broker.positions["s1"]
    assert [p["estado"] for p in rec["partes"]] == ["pendiente", "viva"]
    assert any("SIN STOP" in m["text"] for m in env.evs("real_aviso"))
    assert env.broker.vista_real()["sin_stop"] == ["BTC"]
    await env.ronda(11)
    rec = env.broker.positions["s1"]
    assert all(p["estado"] == "viva" for p in rec["partes"]) and len(env.okx.vivas()) == 2
    assert len(env.okx.post("/api/v5/trade/order-algo")) == 2 and env.okx.post("/api/v5/trade/order")[1:] == []
    assert env.broker.vista_real()["sin_stop"] == []


# ---------------------------------------------------------------- nunca una posición sin stop olvidada

async def test_partes_sin_stop_se_vuelven_a_proteger_cuando_okx_vuelve(env):
    """La algo no se puede poner y el cierre a mercado tampoco (OKX rechaza todo): las partes quedan «pendiente», el
    aviso dice SIN STOP (no «sigue con su stop») y, cuando OKX vuelve, se les pone el stop."""
    orig = env.okx.manejar
    roto = {"si": True}

    def manejar(m, path, q, body):
        if roto["si"] and m == "POST" and path == "/api/v5/trade/order-algo":
            return {"code": "1", "msg": "", "data": [{"sCode": "51000", "sMsg": "Parameter error"}]}
        if roto["si"] and m == "POST" and path == "/api/v5/trade/order" and body["side"] == "sell":
            return {"code": "1", "msg": "", "data": [{"sCode": "51008", "sMsg": "Insufficient balance"}]}
        return orig(m, path, q, body)
    env.okx.manejar = manejar
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    rec = env.broker.positions["s1"]
    assert [p["estado"] for p in rec["partes"]] == ["pendiente", "pendiente"] and env.okx.vivas() == []
    textos = [m["text"] for m in env.evs("real_aviso", "s1")]
    assert any("SIN STOP EN OKX" in t for t in textos)
    assert not any("sigue con su stop" in t for t in textos)
    assert "SIN STOP" in env.broker._texto_estado()
    assert env.broker.vista_real()["posiciones"][0]["sin_stop"] is True
    n = len(env.okx.post("/api/v5/trade/order-algo"))
    await env.ronda(1)   # espera creciente: no machaca a OKX cada segundo
    assert len(env.okx.post("/api/v5/trade/order-algo")) == n
    roto["si"] = False
    await env.ronda(10)
    rec = env.broker.positions["s1"]
    assert all(p["estado"] == "viva" for p in rec["partes"]) and len(env.okx.vivas()) == 2
    assert not rec.get("sin_stop_desde") and env.broker.vista_real()["sin_stop"] == []


async def test_si_no_se_puede_reponer_el_stop_se_cierra_esa_parte(env):
    """Una algo cancelada fuera del bot que OKX no deja volver a poner: esa parte se cierra a mercado."""
    rec = await env.abrir(ticket(), "s1")
    p1 = rec["partes"][0]
    env.okx.algos[p1["algoId"]]["state"] = "canceled"
    for _ in range(3):
        env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order-algo",
                               "resp": {"code": "1", "msg": "", "data": [{"sCode": "51000", "sMsg": "Parameter error"}]}})
    await env.ronda(5)
    rec = env.broker.positions["s1"]
    assert rec["partes"][0]["estado"] == "cerrada" and rec["partes"][0]["why"].startswith("Sin stop en OKX")
    assert rec["partes"][1]["estado"] == "viva"
    venta = env.okx.post("/api/v5/trade/order")[-1]["body"]
    assert venta["side"] == "sell" and venta["sz"] == p1["sz"]


async def test_una_parada_con_una_entrada_en_vuelo_la_protege(env):
    """Entrada sin confirmar (las consultas fallan) y parada de emergencia sin cierre: cuando OKX contesta, la
    entrada llena recibe su stop aunque el modo ya sea solo lectura (es una orden que solo reduce el riesgo)."""
    orig = env.okx.manejar
    corte = {"si": True}

    def manejar(m, path, q, body):
        if corte["si"] and m == "GET" and path == "/api/v5/trade/order":
            raise aiohttp.ClientConnectionError("corte")
        return orig(m, path, q, body)
    env.okx.manejar = manejar
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    assert env.broker.positions["s1"]["estado"] == "enviando" and env.okx.monedas["BTC"] > 0
    env.broker.parada_emergencia("parada sin cerrar")
    await env.ronda()
    assert env.broker.estado.modo == SOLO_LECTURA and not env.broker.cliente.escritura
    corte["si"] = False
    await env.ronda(3)
    rec = env.broker.positions["s1"]
    assert rec["estado"] == "abierta" and len(env.okx.vivas()) == 2
    # en solo lectura no se abre nada
    with pytest.raises(RutaProhibida):
        await env.broker.cliente.privado("POST", "/api/v5/trade/order",
                                         cuerpo={"instId": "BTC-USDC", "side": "buy", "tdMode": "cash"})


async def test_una_parada_con_cierre_y_una_entrada_en_vuelo_la_cierra(env):
    orig = env.okx.manejar
    st = {"ocultar": True}

    def manejar(m, path, q, body):
        r = orig(m, path, q, body)
        if m == "GET" and path == "/api/v5/trade/order" and st["ocultar"] and r.get("data"):
            r = {"code": "0", "msg": "", "data": [{**r["data"][0], "state": "live", "accFillSz": "0"}]}
        return r
    env.okx.manejar = manejar
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    assert env.broker.positions["s1"]["estado"] == "enviando"
    env.broker.parada_emergencia("parada", cerrar_posiciones=True)
    st["ocultar"] = False
    await env.ronda()
    assert "s1" not in env.broker.positions and env.okx.monedas["BTC"] < 1e-7 and env.okx.vivas() == []
    assert env.broker.closed[-1]["reason"].startswith("Parada de emergencia")


# ---------------------------------------------------------------- cuentas y entornos separados

async def test_los_registros_de_la_demo_nunca_se_tocan_con_las_claves_reales(tmp_path):
    """Posición abierta en demo; el usuario pasa okx.env a real (otra cuenta, con BTC suyos): el bot no la gestiona,
    no la cierra por tiempo y no vende nada en la cuenta real."""
    e = Env(tmp_path, "demo")
    assert await e.broker.arrancar()
    await e.abrir(ticket(), "s1")
    escribir_claves(e.dir, "0")
    e.env["BOT_MODO_REAL"] = "1"
    assert confirmar_real(e.dir, "ACTIVO EL MODO REAL", e.clock())[0]
    real = FakeOKX(e.clock)
    real.monedas["BTC"] = 0.01   # los BTC del usuario en la cuenta real
    e.broker.session = real
    await e.ronda(6)
    assert e.broker.estado.modo == REAL
    await e.ronda(61)
    b = e.broker
    assert b.listo and b.positions == {} and len(b.apartadas) == 1
    assert b.vista_real()["apartadas"][0]["entorno"] == "demo"
    assert any("otra cuenta o de otro entorno" in m["text"] for m in e.evs("real_aviso"))
    await e.ronda(25 * 3600)
    assert real.post() == [] and real.monedas["BTC"] == 0.01
    assert b.entradas_hoy(e.clock()) == 0 and b.closed == [] and b.results()["trades"] == []
    assert b.submit(ticket(), "s2")   # la moneda no está ocupada por lo de la demo
    e.okx = real
    b2 = await e.reiniciar()   # tras reiniciar, lo mismo
    assert b2.positions == {} and len(b2.apartadas) == 1


async def test_el_capital_de_partida_es_de_cada_cuenta(tmp_path):
    e = Env(tmp_path, "demo")
    assert await e.broker.arrancar()
    assert e.broker.inicio == 1000.0
    escribir_claves(e.dir, "0")
    e.env["BOT_MODO_REAL"] = "1"
    confirmar_real(e.dir, "ACTIVO EL MODO REAL", e.clock())
    e.okx.total = 0.0   # la cuenta real de hoy: sin dinero
    await e.ronda(6)
    await e.ronda(61)
    assert e.broker.listo and e.broker.estado.modo == REAL
    assert e.broker.inicio is None and e.broker.portfolio()["start_equity"] is None


# ---------------------------------------------------------------- parada de emergencia sin agujeros

async def test_la_parada_no_se_levanta_sola_si_no_se_puede_escribir_parar_real(env, monkeypatch):
    orig = Path.write_text

    def wt(self, *a, **k):
        if self.name == "PARAR_REAL":
            raise OSError("disco lleno")
        return orig(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", wt)
    env.broker.parada_emergencia("web")
    assert not (env.dir / "real_confirmado").exists()   # la confirmación se borra aunque PARAR_REAL no se escriba
    assert any("no se pudo escribir PARAR_REAL" in m["text"] for m in env.evs("real_aviso"))
    await env.ronda()
    for _ in range(3):
        await env.ronda(61)
        assert env.broker.estado.modo == SOLO_LECTURA and not env.broker.puede_abrir()
    b2 = await env.reiniciar()   # la parada está también en la base: tras reiniciar, sigue
    assert b2.parada and b2.estado.modo == SOLO_LECTURA and not b2.puede_abrir()
    monkeypatch.setattr(Path, "write_text", orig)
    assert b2.confirmar("ACTIVO EL MODO REAL")["ok"]
    await env.ronda(6)
    await env.ronda(61)
    assert b2.estado.modo == REAL and b2.puede_abrir()


async def test_una_segunda_parada_con_cierre_si_cierra(env):
    await env.abrir(ticket(), "s1")
    assert env.broker.parada_emergencia("primera", cerrar_posiciones=False)
    await env.ronda()
    assert "s1" in env.broker.positions and env.broker.estado.modo == SOLO_LECTURA
    assert not env.broker.parada_emergencia("otra sin cierre")   # nada nuevo que hacer
    assert env.broker.parada_emergencia("segunda, cierra", cerrar_posiciones=True)
    await env.ronda()
    assert "s1" not in env.broker.positions and env.okx.monedas["BTC"] < 1e-7
    aviso = [m["text"] for m in env.evs("real_aviso") if "Parada de emergencia hecha" in m["text"]][-1]
    assert "1 posiciones cerradas" in aviso


async def test_cerrar_en_parar_real_cierra_tambien_en_solo_lectura(tmp_path):
    """Arrancado sin BOT_MODO_REAL=1 (solo lectura) con una posición del bot de esa cuenta: PARAR_REAL con CERRAR la
    cierra (la plantilla lo promete)."""
    e = Env(tmp_path)
    assert await e.broker.arrancar()
    await e.abrir(ticket(), "s1")
    e.env.pop("BOT_MODO_REAL")
    b = await e.reiniciar()
    assert b.estado.modo == SOLO_LECTURA and "s1" in b.positions
    (e.dir / "PARAR_REAL").write_text("CERRAR", encoding="utf-8")
    await e.ronda(6)
    assert "s1" not in b.positions and e.okx.monedas["BTC"] < 1e-7


async def test_los_cierres_de_la_parada_se_cuentan_por_resultado(env):
    await env.abrir(ticket(), "s1")
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/cancel-algos", "red": True})
    env.broker.parada_emergencia("cierra", cerrar_posiciones=True)
    await env.ronda()
    aviso = [m["text"] for m in env.evs("real_aviso") if "Parada de emergencia hecha" in m["text"]][-1]
    assert "NO se pudieron cerrar todavía: BTC" in aviso and "posiciones cerradas" not in aviso
    assert "s1" in env.broker.positions and len(env.okx.vivas()) == 2   # sigue protegida por sus stops
    await env.ronda(10)   # el cierre pedido se reintenta (solo reduce el riesgo) hasta que sale
    assert "s1" not in env.broker.positions and env.okx.vivas() == []


# ---------------------------------------------------------------- cierres que no dejan nada colgando

async def test_al_contado_no_se_vende_si_no_se_pueden_cancelar_los_stops(env):
    await env.abrir(ticket(), "s1")
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/cancel-algos", "red": True})
    assert env.broker.close("s1", "Cierre manual")
    await env.ronda()
    assert "s1" in env.broker.positions and len(env.okx.vivas()) == 2
    assert len(env.okx.post("/api/v5/trade/order")) == 1   # no vendió con los stops vivos
    await env.ronda(10)
    assert "s1" not in env.broker.positions and env.okx.vivas() == []
    assert env.broker.closed[-1]["reason"] == "Cierre manual"


async def test_nunca_se_cierra_lo_que_no_consta_en_okx(env):
    await env.abrir(ticket(), "s1")
    env.okx.monedas["BTC"] = 0.0   # las monedas ya no están (vendidas a mano, por ejemplo)
    assert env.broker.close("s1")
    await env.ronda()
    assert len(env.okx.post("/api/v5/trade/order")) == 1 and "s1" in env.broker.positions
    assert any("no consta en OKX" in m["text"] for m in env.evs("real_aviso", "s1"))
    assert env.okx.post("/api/v5/trade/cancel-algos") == []


async def test_en_xperps_las_stops_que_no_se_cancelan_se_limpian_al_reconciliar(env):
    await env.abrir(ticket("ETH", "short"), "s1")
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/cancel-algos", "red": True})
    assert env.broker.close("s1")
    await env.ronda()
    assert "s1" not in env.broker.positions and len(env.okx.vivas()) == 2   # reduceOnly sin posición
    await env.ronda(400)
    assert env.okx.vivas() == []


async def test_stop_del_bot_sin_registro_no_se_toca(env):
    env.okx.algos["7777"] = {"algoId": "7777", "algoClOrdId": "bfr" + "a" * 14 + "p1", "instId": "BTC-USDC",
                             "ordType": "conditional", "state": "live", "side": "sell", "sz": "0.0001",
                             "slTriggerPx": "70000", "tpTriggerPx": ""}
    await env.ronda(400)
    assert env.okx.algos["7777"]["state"] == "live"
    assert env.broker.reconciliacion["algos_huerfanas"] == 1
    assert any("sin registro" in m["text"] for m in env.evs("real_aviso"))


async def test_una_stop_puesta_justo_antes_de_caer_se_enlaza_al_volver(env):
    """El bot cae entre poner la algo y guardarla: al volver, la reconciliación la casa con su parte por su
    algoClOrdId (no pone otra)."""
    rec = await env.abrir(ticket(), "s1")
    p2 = rec["partes"][1]
    aid = p2["algoId"]
    p2.update(estado="pendiente")
    p2.pop("algoId")
    env.broker._guardar(rec)
    n = len(env.okx.post("/api/v5/trade/order-algo"))
    b = await env.reiniciar()
    rec = b.positions["s1"]
    assert rec["partes"][1]["estado"] == "viva" and rec["partes"][1]["algoId"] == aid
    assert len(env.okx.post("/api/v5/trade/order-algo")) == n and len(env.okx.vivas()) == 2


async def test_cierre_en_duda_no_vuelve_a_poner_stops_sobre_lo_vendido(env):
    await env.abrir(ticket(), "s1")
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order", "red": True, "crear": True})
    for _ in range(80):
        env.okx.fallos.append({"m": "GET", "p": "/api/v5/trade/order", "red": True})
    assert env.broker.close("s1", "Cierre manual")
    await env.ronda()
    rec = env.broker.positions["s1"]
    assert rec.get("cierres_dudosos") and all(p.get("cierre_dudoso") for p in rec["partes"])
    assert env.okx.vivas() == [] and len(env.okx.post("/api/v5/trade/order-algo")) == 2   # no se repusieron
    env.okx.fallos.clear()
    await env.ronda(10)
    assert "s1" not in env.broker.positions and env.okx.vivas() == []
    assert len([c for c in env.okx.post("/api/v5/trade/order") if c["body"]["side"] == "sell"]) == 1


# ---------------------------------------------------------------- cerrojo con el precio ya pasado

async def test_cerrojo_con_el_precio_ya_por_debajo_del_suelo_cierra_a_mercado(env):
    """OKX ejecuta la parcial (+3R) y, cuando el bot lo ve, el precio ya está por debajo del suelo (+2R): no puede
    subir el stop ahí; cierra a mercado (el paper cerraría en el suelo), en vez de seguir con el stop en −1R."""
    rec = await env.abrir(ticket(), "s1")
    p1 = rec["partes"][0]
    env.okx.disparar(p1["algoId"], "tp", 84800.0)
    await env.ronda(5, BTC=83000.0)
    assert "s1" not in env.broker.positions
    t = env.broker.closed[-1]
    assert t["reason"] == "Stop del cerrojo tras salida parcial" and t["R"] > 2
    assert env.okx.post("/api/v5/trade/amend-algos") == [] and env.okx.vivas() == []


async def test_stop_rechazado_por_cruzar_el_precio_cierra_a_mercado(env):
    rec = await env.abrir(ticket(), "s1")
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/amend-algos",
                           "resp": {"code": "1", "msg": "", "data": [{"sCode": "51280", "sMsg": "SL trigger price"}]}})
    await env.ronda(5, BTC=84850.0)
    assert "s1" not in env.broker.positions
    assert env.broker.closed[-1]["reason"] == "Stop del cerrojo"
    assert rec["partes"][0]["why"] == "Stop del cerrojo"


async def test_si_okx_no_deja_mover_el_stop_se_cambia_la_orden(env):
    rec = await env.abrir(ticket(), "s1")
    orig = env.okx.manejar

    def manejar(m, path, q, body):
        if path == "/api/v5/trade/amend-algos":
            return {"code": "1", "msg": "", "data": [{"sCode": "51000", "sMsg": "Parameter error"}]}
        return orig(m, path, q, body)
    env.okx.manejar = manejar
    for _ in range(3):
        await env.ronda(10, BTC=rec["entry"] * 1.075)
    rec = env.broker.positions["s1"]
    assert rec["locked"] and rec["sl"] == 83200.0
    assert sorted(a["slTriggerPx"] for a in env.okx.vivas()) == ["83200", "83200"]
    assert any("se ha cambiado la orden de stop" in m["text"] for m in env.evs("real_aviso", "s1"))
    assert env.evs("manage", "s1")


# ---------------------------------------------------------------- llenados parciales

async def test_llenado_parcial_de_la_entrada_cancela_el_resto_y_protege_lo_llenado(env):
    env.okx.parcial["BTC-USDC"] = 0.6
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    rec = env.broker.positions["s1"]
    assert rec["estado"] == "abierta"
    assert env.okx.post("/api/v5/trade/cancel-order")   # el resto de la orden, cancelado
    protegido = sum(Decimal(p["sz"]) for p in rec["partes"])
    assert protegido == Decimal("0.00007492") and len(env.okx.vivas()) == 2
    assert Decimal(str(env.okx.monedas["BTC"])) >= protegido


async def test_cierre_parcial_deja_el_resto_abierto_y_con_stop(env):
    await env.abrir(ticket(), "s1")
    env.okx.parcial["BTC-USDC"] = 0.5
    assert env.broker.close("s1", "Cierre manual")
    await env.ronda()
    rec = env.broker.positions["s1"]
    cerradas = [p for p in rec["partes"] if p["estado"] == "cerrada"]
    abiertas = [p for p in rec["partes"] if p["estado"] != "cerrada"]
    assert cerradas and abiertas and all(p["estado"] == "viva" for p in abiertas)
    en_okx = Decimal(str(env.okx.monedas["BTC"]))
    assert sum(Decimal(p["sz"]) for p in abiertas) <= en_okx
    assert sum(Decimal(a["sz"]) for a in env.okx.vivas()) == sum(Decimal(p["sz"]) for p in abiertas)
    assert any("solo cerró" in m["text"] for m in env.evs("real_aviso", "s1"))
    await env.ronda(10)   # el cierre pedido sigue hasta que sale del todo
    assert "s1" not in env.broker.positions and env.okx.vivas() == []
    assert env.okx.monedas["BTC"] < 1e-7



async def test_cierre_parcial_en_xperps_rehace_la_stop_de_la_parte_partida(env):
    """En X-Perps las algo son reduceOnly y OKX no las recorta: tras un cierre parcial, la parte que queda a medias
    necesita otra algo con su tamaño nuevo (aunque cancelar la vieja falle la primera vez)."""
    rec = await env.abrir(ticket("ETH", "short"), "s1")
    assert [p["sz"] for p in rec["partes"]] == ["2", "2"]
    env.okx.parcial[perp_id("ETH")] = 0.75
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/cancel-algos", "red": True})
    assert env.broker.close("s1")
    await env.ronda()
    rec = env.broker.positions["s1"]
    assert env.okx.pos[perp_id("ETH")] == -1
    viva = [p for p in rec["partes"] if p["estado"] == "viva"]
    assert len(viva) == 1 and viva[0]["sz"] == "1" and viva[0].get("rehacer")
    assert env.broker.vista_real()["sin_stop"] == ["ETH"]
    await env.ronda(1)
    p = next(p for p in rec["partes"] if p["estado"] == "viva")
    assert not p.get("rehacer") and env.okx.algos[p["algoId"]]["sz"] == "1"
    await env.ronda(400)   # el cierre pedido termina y la reconciliación quita la algo sobrante
    assert "s1" not in env.broker.positions and not env.okx.pos.get(perp_id("ETH")) and env.okx.vivas() == []


# ---------------------------------------------------------------- los controles del paper también en real

async def test_diferencial_y_pausa_macro_como_el_paper(env):
    env.spread["BTC"] = 8.0
    assert not env.broker.submit(ticket(), "s1")
    assert env.veto("s1") == "Diferencial de BTC: 8,0 pb > 5 pb"
    env.spread["BTC"] = 1.0
    env.bus.state["calendar"] = {"type": "calendar",
                                 "events": [{"impact": "alto", "t": T0 + 5 * MIN, "name": "IPC de EE. UU."}]}
    assert not env.broker.submit(ticket(), "s2")
    assert env.veto("s2") == "Dato macro de alto impacto en 5 min: IPC de EE. UU."
    env.bus.state.pop("calendar")
    assert env.broker.submit(ticket(), "s3")


async def test_exposicion_como_el_paper(env):
    env.okx.pos[perp_id("SOL")] = -2000   # posición fuera del bot: 2.000 contratos × 0,01 SOL × 102 ≈ 2.040 USD
    await env.ronda(400)
    assert [x["coin"] for x in env.broker.externas] == ["SOL"]
    assert not env.broker.submit(ticket(), "s1")
    why = env.veto("s1")
    assert why.startswith("Exposición total") and "> 100 % del capital" in why
    assert env.evs("riskko", "s1")[-1]["patch"]["risk"]["motivo_codigo"] == "exposicion"


# ---------------------------------------------------------------- capital de referencia

async def test_un_ingreso_no_tapa_la_perdida_del_dia(env):
    env.broker.set_limites({"perdida_dia_pct": 0.01})
    rec = await env.abrir(ticket(), "s1")
    for p in rec["partes"]:
        env.okx.disparar(p["algoId"], "sl", 78400.0)
    await env.ronda(5)
    assert env.broker.closed[-1]["pnl"] < 0
    env.okx.total = 1500.0   # el usuario mete 500 USDC el mismo día
    await env.ronda(31)
    pnl, pct = env.broker.day_pnl()
    assert pnl < 0 and env.broker.halt_until   # manda lo que ha hecho el bot, no el saldo
    assert not env.broker.submit(ticket("ETH"), "s2")


async def test_con_la_cuenta_a_cero_no_se_fija_el_capital_de_partida(tmp_path):
    e = Env(tmp_path)
    e.okx.total, e.okx.usdc = 0.0, 0.0
    assert await e.broker.arrancar()
    assert e.broker.inicio is None and not e.broker.day_base
    assert e.broker.portfolio()["start_equity"] is None
    e.okx.total, e.okx.usdc = 50.0, 50.0   # mete dinero
    await e.ronda(31)
    assert e.broker.inicio == 50.0 and e.broker.day_base["equity"] == 50.0
    assert e.broker.day_pnl()[0] == 0.0
    b2 = await e.reiniciar()
    assert b2.inicio == 50.0


# ---------------------------------------------------------------- cliente: lo que se firma es lo que se envía

async def test_la_consulta_se_firma_y_se_envia_sin_recodificar():
    okx = FakeOKX(Clock(T0))
    cli = ClienteOKX(okx, Claves(CLAVE, SECRETO, FRASE), False, clock=lambda: T0)
    await cli.privado("GET", "/api/v5/account/balance", {"ccy": "USDC,BTC,ETH"})
    c = okx.calls[-1]
    assert c["camino"] == "/api/v5/account/balance?ccy=USDC,BTC,ETH"
    assert c["headers"]["OK-ACCESS-SIGN"] == firmar(SECRETO, c["headers"]["OK-ACCESS-TIMESTAMP"] + "GET" + c["camino"])
    for malo in ("a b", "x&y=1", "é", "a/b", "%2C"):
        with pytest.raises(RutaProhibida):
            await cli.privado("GET", "/api/v5/account/balance", {"ccy": malo})
    assert len(okx.calls) == 1


@pytest.mark.parametrize("ruta,cuerpo,pasa", [
    ("/api/v5/trade/order", {"instId": "BTC-USDC", "tdMode": "cash", "side": "sell", "sz": "1"}, True),
    ("/api/v5/trade/order", {"instId": "BTC-USDC", "tdMode": "cash", "side": "buy", "sz": "1"}, False),
    ("/api/v5/trade/order", {"instId": "X", "tdMode": "isolated", "side": "sell", "sz": "1"}, False),
    ("/api/v5/trade/order", {"instId": "X", "tdMode": "isolated", "side": "buy", "sz": "1", "reduceOnly": True}, True),
    ("/api/v5/trade/order", {"instId": "X", "tdMode": "isolated", "side": "buy", "reduceOnly": "true"}, False),
    ("/api/v5/trade/order-algo", {"instId": "BTC-USDC", "tdMode": "cash", "side": "sell"}, True),
    ("/api/v5/trade/order-algo", {"instId": "BTC-USDC", "tdMode": "cash", "side": "buy"}, False),
    ("/api/v5/trade/cancel-order", {"instId": "BTC-USDC", "ordId": "1"}, True),
    ("/api/v5/trade/cancel-algos", [{"instId": "BTC-USDC", "algoId": "1"}], True),
    ("/api/v5/trade/amend-algos", {"instId": "BTC-USDC", "algoId": "1", "newSlTriggerPx": "1"}, False),
    ("/api/v5/account/set-leverage", {"instId": "X", "lever": "50", "mgnMode": "isolated"}, False),
])
def test_en_solo_lectura_solo_pasan_las_ordenes_que_reducen_el_riesgo(ruta, cuerpo, pasa):
    assert okx_real.reduce_riesgo("POST", ruta, cuerpo) is pasa


async def test_el_cliente_en_modo_reducir_rechaza_lo_demas_antes_de_firmar():
    okx = FakeOKX(Clock(T0))
    cli = ClienteOKX(okx, Claves(CLAVE, SECRETO, FRASE), False, clock=lambda: T0)
    cli.reducir = True
    with pytest.raises(RutaProhibida):
        await cli.privado("POST", "/api/v5/trade/order", cuerpo={"instId": "BTC-USDC", "tdMode": "cash",
                                                                   "side": "buy", "ordType": "market", "sz": "1"})
    assert okx.calls == []
    okx.monedas["BTC"] = 1.0
    await cli.privado("POST", "/api/v5/trade/order", cuerpo={"instId": "BTC-USDC", "tdMode": "cash", "side": "sell",
                                                               "ordType": "market", "sz": "0.5"})
    assert len(okx.calls) == 1


# ---------------------------------------------------------------- remate: cuentas, algos perdidas y panel

async def test_una_orden_pedida_en_la_demo_nunca_sale_en_la_cuenta_real(tmp_path):
    """Una orden esperando el veto y otra ya aprobada en la demo; el usuario pasa okx.env a real (y lo tiene todo para
    el modo real): las dos se anulan y no sale nada en la cuenta real."""
    e = Env(tmp_path, "demo")
    assert await e.broker.arrancar()
    assert e.broker.submit(ticket(), "s1")                    # esperando el veto de 30 s
    assert e.broker.submit(ticket("ETH"), "s2")
    assert e.broker.decide("s2", True)                        # aprobada, en cola para la ronda siguiente
    escribir_claves(e.dir, "0")
    e.env["BOT_MODO_REAL"] = "1"
    assert confirmar_real(e.dir, "ACTIVO EL MODO REAL", e.clock())[0]
    real = FakeOKX(e.clock)
    e.broker.session = real
    e.broker._t["vigilar"] = 0
    await e.ronda(0)
    assert e.broker.estado.modo == REAL and not e.broker.pending and not e.broker.en_curso
    assert e.evs("humanko", "s1") and "otra cuenta" in e.evs("blocked", "s2")[-1]["text"]
    for _ in range(40):
        await e.ronda(1)
    assert e.broker.listo and real.post() == [] and e.broker.positions == {}


async def test_una_orden_aprobada_para_otra_cuenta_no_sale(env):
    """Defensa de más: aunque llegara a la cola, una orden pedida con otra cuenta o entorno no se manda."""
    assert env.broker.submit(ticket(), "s1")
    env.broker.pending["s1"]["cuenta"] = "otracuenta00"
    env.broker.decide("s1", True)
    await env.ronda()
    assert env.okx.post() == [] and "otras claves o en otro entorno" in env.evs("blocked", "s1")[-1]["text"]


async def test_una_algo_que_okx_ya_no_encuentra_se_vuelve_a_poner(env):
    """Las algo de una posición desaparecen de OKX (ni vivas ni consultables): la parte no se queda «viva» sin stop;
    pasado ALGO_NO_ESTA_MS se vuelve a proteger."""
    rec = await env.abrir(ticket(), "s1")
    viejas = {p["algoId"] for p in rec["partes"]}
    env.okx.algos.clear()
    await env.ronda(6)   # primera vez que no la encuentra: aún no hace nada (puede ser un retraso de OKX)
    assert env.okx.post("/api/v5/trade/order-algo")[2:] == []
    for _ in range(3):
        await env.ronda(6)
    rec = env.broker.positions["s1"]
    assert all(p["estado"] == "viva" and p["algoId"] not in viejas for p in rec["partes"])
    assert len(env.okx.vivas()) == 2 and env.okx.post("/api/v5/trade/order")[1:] == []
    assert any("ya no está viva (no encontrada)" in m["text"] for m in env.evs("real_aviso", "s1"))


async def test_una_algo_que_no_esta_y_tampoco_la_posicion_se_da_por_cerrada(env):
    rec = await env.abrir(ticket(), "s1")
    env.okx.algos.clear()
    env.okx.monedas["BTC"] = 0.0   # el usuario vendió en la web de OKX
    for _ in range(4):
        await env.ronda(6)
    assert "s1" not in env.broker.positions and env.okx.post("/api/v5/trade/order")[1:] == []
    assert env.broker.closed[-1]["reason"].startswith("Cerrada fuera del bot")


async def test_el_panel_solo_ensena_la_confirmacion_de_las_claves_de_ahora(env):
    assert env.broker.vista_real()["archivos"]["confirmado"]
    escribir_claves(env.dir, "0", OKX_API_KEY="OTRACLAVEfalsa5555")
    await env.ronda(6)
    v = env.broker.vista_real()
    assert env.broker.estado.modo == SOLO_LECTURA and v["archivos"]["confirmado"] is None
    assert "OTRACLAVEfalsa5555" not in json.dumps(v, ensure_ascii=False)


async def test_la_aprobacion_automatica_de_la_demo_no_se_ensena_ni_se_aplica_en_real(tmp_path):
    e = Env(tmp_path, "demo")
    assert await e.broker.arrancar()
    assert e.broker.set_mode("auto") and e.broker.portfolio()["mode"] == "auto"
    escribir_claves(e.dir, "0")
    e.env["BOT_MODO_REAL"] = "1"
    assert confirmar_real(e.dir, "ACTIVO EL MODO REAL", e.clock())[0]
    await e.ronda(6)
    await e.ronda(61)
    assert e.broker.estado.modo == REAL
    assert e.broker.portfolio()["mode"] == "veto" and e.broker.vista_real()["aprobacion"] == "veto"
    assert e.broker.submit(ticket(), "s1") and "s1" in e.broker.pending   # espera el veto, no sale sola


async def test_permiso_de_retirada_a_mitad_de_camino_bloquea(env):
    rec = await env.abrir(ticket(), "s1")
    env.okx.cfg["perm"] = "read_only,trade,withdraw"   # el usuario edita la clave en OKX y le da «Retirar»
    await env.ronda(301)
    assert "retirada" in env.broker.bloqueo and not env.broker.puede_escribir() and not env.broker.cliente.escritura
    assert any("permiso de retirada" in m["text"] for m in env.evs("real_aviso"))
    assert not env.broker.submit(ticket("ETH"), "s2")
    n = len(env.okx.post())
    await env.ronda(3600)   # ni mueve stops ni cierra: las stops siguen en OKX
    assert len(env.okx.post()) == n and len(env.okx.vivas()) == len(rec["partes"])
    env.okx.cfg["perm"] = "read_only,trade"   # se lo quita: en la siguiente revisión vuelve a operar
    await env.ronda(301)
    assert env.broker.bloqueo is None and env.broker.puede_abrir() and env.broker.submit(ticket("ETH"), "s3")


async def test_un_bloqueo_por_clave_no_se_quita_al_revisar_los_permisos(env):
    env.broker._bloqueo_clave(okx_real.ErrorClave("50110", "IP no permitida"))
    env.broker._ajustar_escritura()
    await env.ronda(301)
    assert "50110" in env.broker.bloqueo and not env.broker.puede_escribir()


async def test_un_post_con_una_respuesta_que_no_es_de_okx_queda_en_duda(env):
    """200 con un JSON sin «code» (un proxy, una página rara): la orden quizá llegó; se consulta, no se repite."""
    env.okx.fallos.append({"m": "POST", "p": "/api/v5/trade/order", "crear": True, "status": 200, "resp": {}})
    assert env.broker.submit(ticket(), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    assert len(env.okx.post("/api/v5/trade/order")) == 1 and env.broker.positions["s1"]["estado"] == "abierta"


async def test_exptime_cuenta_desde_el_envio_aunque_set_leverage_tarde(env):
    orig = env.okx.manejar

    def manejar(m, path, q, body):
        if path == "/api/v5/account/set-leverage":
            env.clock.t += 9_000   # OKX tarda 9 s en contestar
        return orig(m, path, q, body)
    env.okx.manejar = manejar
    assert env.broker.submit(ticket("ETH", "short"), "s1")
    env.broker.decide("s1", True)
    await env.ronda()
    entrada = env.okx.post("/api/v5/trade/order")[0]
    assert int(entrada["headers"]["expTime"]) == T0 + 1_000 + 9_000 + okx_real.EXP_ORDEN_MS
    assert env.broker.positions["s1"]["estado"] == "abierta"


async def test_una_parada_con_cierre_sin_hacer_se_hace_tras_reiniciar(env):
    """Parada con cierre pedida y el bot cae antes de hacerla en OKX: al volver, la parada sigue y el cierre se hace
    (no se pierde con el reinicio); una vez hecha, no se repite."""
    await env.abrir(ticket(), "s1")
    assert env.broker.parada_emergencia("cierra", cerrar_posiciones=True)
    b2 = await env.reiniciar()
    assert b2.parada and not b2.parada.get("hecha") and not b2.puede_abrir() and "s1" in b2.positions
    await env.ronda()
    assert "s1" not in b2.positions and env.okx.monedas["BTC"] < 1e-7 and b2.parada.get("hecha")
    assert any("Parada de emergencia hecha" in m["text"] for m in env.evs("real_aviso"))
    b3 = await env.reiniciar()
    assert b3.parada and not b3._cola and not b3._parando


# ---------------------------------------------------------------- comisiones de la cuenta y cableado (29-10-2026)

def test_comision_de_creador_de_la_cuenta():
    assert comision_pct({"taker": "-0.001", "maker": "-0.0008"}, "maker") == pytest.approx(0.08)
    assert comision_pct({"maker": "-0.0008", "makerUSDC": "-0.002"}, "maker") == pytest.approx(0.2)
    assert comision_pct({"taker": "-0.001"}, "maker") is None


async def test_solo_lectura_lee_la_comision_real_y_manda_en_el_perfil_del_paper(tmp_path):
    """Con las claves del usuario (solo lectura) el conector lee la comisión de tomador y de creador de su cuenta
    (account/trade-fee, GET firmado que no mueve nada); el mensaje cuenta_real la lleva y el perfil de comisiones del
    paper pasa a «cuenta». Los tickets siguen yendo al paper (BOT_BROKER=paper) y el panel lo dice."""
    from bot import comisiones
    e = Env(tmp_path, "solo_lectura")
    comisiones_bus = []
    publish = e.bus.publish
    e.bus.publish = lambda m: (publish(m), comisiones.observar(m), comisiones_bus.append(m.get("type")))[0]
    assert await e.broker.arrancar()
    assert e.broker.comisiones_creador == {"spot": pytest.approx(0.08), "perp": pytest.approx(0.08)}
    v = e.broker.vista_real()
    assert v["comisiones_creador"]["spot"] == pytest.approx(0.08) and v["broker"] == "paper"
    assert v["resumen"] == "Modo real en solo lectura: el bot opera en paper (solo lee tu cuenta de OKX)"
    assert "cuenta_real" in comisiones_bus and comisiones.activo() == "cuenta"
    assert comisiones.tasas("spot") == {"perfil": "cuenta", "inst": "spot", "creador": pytest.approx(0.08),
                                        "tomador": pytest.approx(0.1)}
    assert e.okx.post() == []   # solo lectura: ninguna orden


async def test_perfil_cuenta_del_paper_ni_con_la_demo_ni_sin_x_perps_por_el_modo_de_posicion(tmp_path):
    """La demo de OKX no es la cuenta del usuario: su comisión no pasa al paper. Con X-Perps en la cuenta pero en
    long_short_mode, el conector no puede abrir cortos (cortos.ok False), pero el paper sí los mide («xperps»)."""
    from bot import comisiones
    d = Env(tmp_path / "demo", "demo")
    assert await d.broker.arrancar()
    v = d.broker.vista_real()
    assert v["demo"] is True and v["comisiones"]["spot"] is not None
    comisiones.observar({"type": "cuenta_real", **v})
    assert comisiones.activo() == comisiones.CON_XPERPS
    e = Env(tmp_path / "lsm", "solo_lectura")
    e.okx.cfg["posMode"] = "long_short_mode"
    assert await e.broker.arrancar()
    v = e.broker.vista_real()
    assert v["cortos"]["ok"] is False and v["xperps"] == list(config.COINS) and v["comisiones"]["perp"] is None
    comisiones.observar({"type": "cuenta_real", **v})
    assert comisiones.activo() == "cuenta" and comisiones.hay_cortos()
    assert comisiones.tasas("perp")["tomador"] == 0.05                    # la oficial con X-Perps: no la leyó
    a = Env(tmp_path / "apagado", "apagado")
    v = a.broker.vista_real()
    assert v["modo"] == "apagado" and v["etiqueta"] == "apagado" and v["xperps"] is None
    comisiones.observar({"type": "cuenta_real", **v})
    assert comisiones.activo() == comisiones.CON_XPERPS


async def test_resumen_del_panel_segun_el_modo_y_bot_broker(tmp_path, monkeypatch):
    e = Env(tmp_path, "apagado")
    assert e.broker.vista_real()["resumen"] == "Modo real apagado: el bot opera en paper"
    assert e.broker.vista_real()["broker"] == "paper" and e.okx.calls == []
    r = Env(tmp_path / "otro", "real")
    assert await r.broker.arrancar()
    assert r.broker.resumen().startswith("Modo real listo, pero el bot opera en paper (BOT_BROKER=paper")
    monkeypatch.setattr(config, "BROKER", "real")
    assert r.broker.resumen() == "Operando en dinero real" and r.broker.vista_real()["broker"] == "real"
    assert e.broker.resumen().startswith("Modo real sin operar: Modo real no activo")
