"""Velas públicas de OKX EU para la gráfica de cada operación (bot/velas_okx.py): con un OKX simulado, sin red."""
import asyncio

import pytest

from bot import velas_okx as V

STEP = 300_000
NOW = 1_793_000_000_000 // STEP * STEP + 120_000   # 2 min dentro de la vela de 5 min en curso


class FakeOKX:
    """Las rutas públicas candles (las 1.440 últimas, 300 por página) y history-candles (todas, 100 por página), con las
    velas de la más nueva a la más vieja como texto y confirm = "0" en la que está en curso."""

    def __init__(self, clock, desde, step=STEP):
        self.clock, self.desde, self.step = clock, desde, step
        self.calls: list[tuple[str, dict]] = []
        self.fallo = None

    def filas(self):
        now = self.clock()
        out, t = [], self.desde
        while t <= now:
            px = 100 + (t - self.desde) / self.step
            out.append([str(t), str(px), str(px + 1), str(px - 1), str(px + 0.5), "1", "1", "1",
                        "0" if t + self.step > now else "1"])
            t += self.step
        return out[::-1]

    async def get(self, url, params):
        self.calls.append((url, dict(params)))
        if self.fallo == "red":
            raise OSError("sin red")
        if self.fallo == "codigo":
            return {"code": "50011", "msg": "Too Many Requests", "data": []}
        rows = self.filas()
        if url.endswith(V.RECIENTES):
            rows, lim = rows[:1440], min(int(params["limit"]), 300)
        else:
            lim = min(int(params["limit"]), 100)
        after = int(params["after"])
        rows = [r for r in rows if int(r[0]) < after][:lim]
        return {"code": "0", "msg": "", "data": rows}


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


def nuevo(desde_dias=10):
    clock, mono = Clock(NOW), Mono()
    okx = FakeOKX(clock, NOW // STEP * STEP - desde_dias * 288 * STEP)
    dormido = []

    async def dormir(s):
        dormido.append(s)
        mono.t += s

    v = V.VelasOKX(get_json=okx.get, clock=clock, mono=mono, dormir=dormir)
    return v, okx, clock, mono, dormido


async def test_velas_del_par_de_okx_eu_con_la_vela_en_curso():
    v, okx, *_ = nuevo()
    desde = NOW - 3 * 3600_000
    r = await v.respuesta("BTC", desde, NOW, "5m", 2000)
    assert r["fuente"] == "okx" and r["instId"] == "BTC-USDC" and r["nota"] == "velas de OKX EU, BTC-USDC"
    assert r["n"] == 37 and r["velas"][0][0] == desde // STEP * STEP and r["velas"][-1][0] == NOW // STEP * STEP
    assert all(b[0] - a[0] == STEP for a, b in zip(r["velas"], r["velas"][1:]))
    assert r["velas"][-1][2] >= r["velas"][-1][4] >= r["velas"][-1][3]   # máximo ≥ cierre ≥ mínimo
    assert r["hasta_datos"] == NOW            # la última está en curso: llega hasta ahora, no hasta su fin (futuro)
    fin = NOW // STEP * STEP - STEP                                       # un tramo ya pasado: el fin de su última vela
    antes = await v.respuesta("BTC", desde, fin, "5m", 2000)
    assert antes["velas"][-1][0] == fin - STEP and antes["hasta_datos"] == fin
    [(url, params)] = okx.calls
    assert url == "https://eea.okx.com/api/v5/market/candles"
    assert params == {"instId": "BTC-USDC", "bar": "5m", "after": str(NOW), "limit": "300"}


async def test_cache_solo_pide_lo_nuevo_y_la_vela_en_curso_cada_5_s():
    v, okx, clock, mono, _ = nuevo()
    desde = NOW - 3 * 3600_000
    await v.velas("ETH", desde, NOW, "5m")
    assert len(okx.calls) == 1
    await v.velas("ETH", desde, NOW, "5m")                    # al momento: nada nuevo, sin pedir
    assert len(okx.calls) == 1
    mono.t += V.VIVA_S
    clock.t += 1000
    out = await v.velas("ETH", desde, clock.t, "5m")          # pasados 5 s: la vela en curso, una página
    assert len(okx.calls) == 2 and okx.calls[-1][1]["after"] == str(clock.t)
    clock.t += 2 * STEP                                       # se cerraron velas: solo lo nuevo, aunque no pasen 5 s
    out = await v.velas("ETH", desde, clock.t, "5m")
    assert len(okx.calls) == 3 and out[-1][0] == clock.t // STEP * STEP
    assert len(out) == len({x[0] for x in out}) and all(b[0] - a[0] == STEP for a, b in zip(out, out[1:]))


async def test_lo_antiguo_sale_de_la_historia_y_se_recorta_a_las_mas_recientes():
    v, okx, *_ = nuevo(desde_dias=10)
    desde = NOW - 8 * 86400_000                               # 2.304 velas de 5 min: más que las 1.440 recientes
    r = await v.respuesta("SOL", desde, NOW, "5m", 2000)
    assert r["n"] == 2000 and r["recortada"] is True
    rutas = [u.rsplit("/", 1)[1] for u, _ in okx.calls]
    assert rutas[:5] == ["candles"] * 5 and set(rutas[5:]) == {"history-candles"}
    assert all(p["limit"] == "100" for u, p in okx.calls if u.endswith("history-candles"))
    assert all(b[0] - a[0] == STEP for a, b in zip(r["velas"], r["velas"][1:]))
    v2, okx2, *_ = nuevo(desde_dias=1)                        # OKX no tiene nada más antiguo: se para
    r = await v2.respuesta("XRP", NOW - 5 * 86400_000, NOW, "5m", 2000)
    assert r["n"] == 289 and len(okx2.calls) <= 4


async def test_limite_de_peticiones_espera_su_turno():
    v, okx, clock, mono, dormido = nuevo(desde_dias=10)
    await v.velas("BTC", NOW - 9 * 86400_000, NOW, "5m")     # 5 recientes + 11 de historia: pasa de 10 en 2 s
    assert len(okx.calls) > V.MAX_PETICIONES and dormido and sum(dormido) >= V.VENTANA_S - 1e-9


async def test_si_okx_falla_lo_dice_y_no_inventa_velas():
    v, okx, *_ = nuevo()
    okx.fallo = "red"
    with pytest.raises(V.ErrorVelas, match="sin red"):
        await v.velas("BTC", NOW - 3600_000, NOW, "5m")
    assert v.estado()["ultimo_error"].startswith("OSError")
    okx.fallo = "codigo"
    with pytest.raises(V.ErrorVelas, match="50011"):
        await v.respuesta("BTC", NOW - 3600_000, NOW, "5m", 2000)
    okx.fallo = None
    assert (await v.respuesta("BTC", NOW - 3600_000, NOW, "5m", 2000))["n"] == 13 and v.estado()["ultimo_error"] is None
    with pytest.raises(V.ErrorVelas):
        await v.velas("DOGE", NOW - 3600_000, NOW, "5m")


def test_filas_rotas_no_valen():
    assert V._fila(["1", "1", "2", "0.5", "1.5", "0", "0", "0", "1"]) == ([1, 1.0, 2.0, 0.5, 1.5], True)
    assert V._fila(["1", "1", "0.9", "1.2", "1.1", "0", "0", "0", "0"]) == ([1, 1.0, 1.1, 1.0, 1.1], False)
    for bad in (["x", "1", "1", "1", "1"], ["1", "0", "1", "1", "1"], ["1", "nan", "1", "1", "1"], ["1", "1"], None):
        assert V._fila(bad) is None, bad


async def test_peticion_http_sin_firmar_y_sin_seguir_redirecciones():
    """El GET va a eea.okx.com sin cabeceras de autenticación ni redirecciones."""
    seen = []

    class Resp:
        status = 200

        async def json(self, content_type=None):
            return {"code": "0", "data": []}

    class Ctx:
        async def __aenter__(self):
            return Resp()

        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, url, **kw):
            seen.append((url, kw))
            return Ctx()

    v = V.VelasOKX(Session())
    assert await v.velas("BTC", NOW - 3600_000, NOW, "15m") == []   # sin velas recientes, mira la historia y para
    assert [u for u, _ in seen] == ["https://eea.okx.com/api/v5/market/candles",
                                    "https://eea.okx.com/api/v5/market/history-candles"]
    for _url, kw in seen:
        assert kw["allow_redirects"] is False and "headers" not in kw
        assert kw["params"]["instId"] == "BTC-USDC" and kw["params"]["bar"] == "15m"
    with pytest.raises(V.ErrorVelas, match="sin sesión"):
        await V.VelasOKX(None).velas("BTC", NOW - 3600_000, NOW, "1m")


def test_el_modulo_no_toca_claves():
    import inspect
    src = inspect.getsource(V)
    for prohibido in ("OK-ACCESS", "okx.env", "okx_firma", "Claves", "/api/v5/account", "/api/v5/trade"):
        assert prohibido not in src, prohibido
    assert asyncio.iscoroutinefunction(V.VelasOKX.respuesta)
