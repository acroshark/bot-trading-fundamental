"""Utilidades comunes de las pruebas del laboratorio vivo (docs/07, 17): carpeta temporal en M: (conftest), velas
sintéticas y reglas del usuario activadas. Este archivo no tiene pruebas propias."""
import os
from datetime import datetime, timezone

import pytest

from bot import config
from bot.lab_vivo import ajustes

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
T0 = int(datetime(2025, 3, 3, tzinfo=timezone.utc).timestamp() * 1000)   # lunes


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """lab_vivo en una carpeta temporal (nunca en M:\\BotFundamental-datos\\lab_vivo)."""
    monkeypatch.setenv("BOT_LAB_VIVO_DIR", str(tmp_path / "lab_vivo"))
    ajustes.recargar()
    ajustes.preparar_carpetas()
    yield ajustes.DIR
    monkeypatch.delenv("BOT_LAB_VIVO_DIR", raising=False)
    ajustes.recargar()


@pytest.fixture
def reglas(monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    yield


_FUENTES: dict = {}


def fuente(dias: int = 90, semilla: int = 7, **kw):
    """Fuente sintética (se guarda entre pruebas: tarda ≈ 1 s)."""
    from bot.lab_vivo import datos
    k = (dias, semilla, tuple(sorted(kw.items())))
    if k not in _FUENTES:
        _FUENTES[k] = datos.fuente_sintetica(dias=dias, semilla=semilla, **kw)
    return _FUENTES[k]


def velas_mano(precios, t0: int = T0, monedas=("BTC", "ETH", "SOL", "XRP"), vol: float = 10.0, rango: float = 0.0005):
    """Fuente con velas de 1 min hechas a mano: el mismo camino de precios de cierre en cada moneda (escalado), con
    máximo y mínimo a ± rango del cuerpo. Sin funding ni bolsas (las familias que los piden no dan señal)."""
    import numpy as np
    from bot.lab_vivo import datos
    c = np.asarray(precios, float)
    n = len(c)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rango)
    l = np.minimum(o, c) * (1 - rango)
    t = t0 + np.arange(n, dtype=np.int64) * MIN
    velas, densos = {}, {}
    for i, m in enumerate(monedas):
        k = 1.0 + i
        v = datos.Velas(t, o * k, h * k, l * k, c * k, np.full(n, vol), np.full(n, vol / 2))
        velas[m], densos[m] = v, datos.denso_de(v)
    fund = {m: (np.zeros(0, np.int64), np.zeros(0)) for m in monedas}
    per = {"epoca": "mano", "inicio": t0, "fin": t0 + n * MIN, "corte": t0 + int(n * 0.6) * MIN}
    return datos.Fuente(tuple(monedas), per, velas, densos, {}, fund, (np.zeros(0, np.int64), np.zeros(0)), {},
                        (np.zeros(0, np.int64), []), [], "mano", None, {})


def entorno_limpio() -> None:
    for k in ("BOT_LAB_VIVO_DIR",):
        os.environ.pop(k, None)
    ajustes.recargar()
