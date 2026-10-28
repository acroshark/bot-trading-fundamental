"""Vigilancia del reloj (hallazgo n.º 53), sin red: las respuestas de OKX y Binance son de mentira."""
import asyncio

import pytest

from bot import reloj
from bot.reloj import Reloj, aviso, desfase_ms


class FakeBus:
    def __init__(self):
        self.msgs = []

    def publish(self, m):
        self.msgs.append(m)
        return m


class FakeResp:
    def __init__(self, data, fail=None):
        self.data, self.fail = data, fail

    async def __aenter__(self):
        if self.fail:
            raise self.fail
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self.data


class FakeSession:
    def __init__(self, by_host, reloj_local):
        self.by_host, self.reloj, self.urls = by_host, reloj_local, []

    def get(self, url, timeout=None):
        self.urls.append(url)
        self.reloj[0] += 0.2   # 200 ms de ida y vuelta
        for host, r in self.by_host.items():
            if host in url:
                return r
        return FakeResp(None, OSError("sin red"))


def test_desfase_y_aviso():
    assert desfase_ms(1_000_000, 1_000_100, 1_000_300) == 200          # la mitad de la petición frente al servidor
    assert desfase_ms(1_000_000, 999_000, 999_200) == -900
    assert aviso(2000) is None and aviso(-1500) is None and aviso(None) is None
    assert "3,5 s adelantado" in aviso(3500) and "Sincroniza la hora de Windows" in aviso(3500)
    assert "2,1 s atrasado" in aviso(-2100)


async def test_mide_con_okx_y_avisa_si_pasa_de_2_s(caplog):
    local = [1_800_000_000.0]                                          # segundos, como time.time
    servidor = int((local[0] + 0.1) * 1000) - 3000                      # el PC va 3 s adelantado
    s = FakeSession({"okx.com": FakeResp({"code": "0", "data": [{"ts": str(servidor)}]})}, local)
    bus, waits = FakeBus(), []

    async def sleep(x):
        waits.append(x)
        raise asyncio.CancelledError

    r = Reloj(bus, s, clock=lambda: local[0], sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await r.run()
    [m] = bus.msgs
    assert (m["type"], m["fuente"], m["desfase_ms"], m["ok"], m["error"]) == ("reloj", "OKX", 3000, False, None)
    assert m["ida_vuelta_ms"] == 200 and "3,0 s adelantado" in m["aviso"] and waits == [reloj.CADA_S]
    assert "adelantado" in caplog.text and s.urls == [reloj.FUENTES[0][1]]


async def test_sin_okx_mira_binance_y_sin_nadie_reintenta_antes():
    local = [1_800_000_000.0]
    bn = FakeResp({"serverTime": int((local[0] + 0.3) * 1000) + 500})   # tras el intento de OKX; 0,5 s atrasado
    s = FakeSession({"okx.com": FakeResp(None, OSError("caído")), "binance": bn}, local)
    m = await Reloj(FakeBus(), s, clock=lambda: local[0]).medir()
    assert (m["fuente"], m["desfase_ms"], m["ok"], m["aviso"]) == ("Binance", -500, True, None)
    nadie = FakeSession({}, local)
    bus, waits = FakeBus(), []

    async def sleep(x):
        waits.append(x)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await Reloj(bus, nadie, clock=lambda: local[0], sleep=sleep).run()
    [m] = bus.msgs
    assert m["fuente"] is None and m["ok"] is None and "OKX: sin red" in m["error"] and "Binance" in m["error"]
    assert waits == [reloj.REINTENTO_S]
