"""Filtros comunes que cualquier familia declara como parámetros (docs/07, 6.3). Cada uno cuenta como 1 filtro para el
tope de 3 cuando no está en su valor neutro. Todos son causales: miran solo lo cerrado antes de la decisión.

| Filtro | Valores | Cómo |
|---|---|---|
| regimen_btc | ninguno · a_favor · no_contra | Cierre del último día UTC completo de BTC frente a sus medias de 20 y 50 días (laboratorio.btc_trend) |
| volatilidad | todos · no_baja · alta · baja | ATR de 14 h en percentil de las 720 h previas (apertura.atr_pct_at) |
| funding | todos · no_extremo_en_contra | Percentil de 30 días de la última tasa (laboratorio.funding_features); no entra si el extremo va contra la operación |
| dias | todos · mar-jue · sin_fin_de_semana | Día de Madrid de la decisión |
| aperturas | todas_horas · solo_en_apertura · fuera_de_apertura | Si la decisión cae entre la apertura y +120 min de una ventana del laboratorio |
| regimen_cripto | ninguno · no_en_contra · a_favor_estricto | Zona del crypto-regime-analyzer con las 4 monedas (como medir_selectiva.regimen) |
| noticias_en_contra | no | Solo en vivo (exploratoria, fase 2) |"""
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from . import indicadores as ind
from .familias.base import DAY, H, MIN, Param, Senales

MAD_TZ = None


def _mad():
    global MAD_TZ
    if MAD_TZ is None:
        from zoneinfo import ZoneInfo
        MAD_TZ = ZoneInfo("Europe/Madrid")
    return MAD_TZ


PARAMS = {
    "regimen_btc": Param("str", "ninguno", ("ninguno", "a_favor", "no_contra"), filtro=True, neutro="ninguno",
                         texto="tendencia diaria de BTC"),
    "volatilidad": Param("str", "todos", ("todos", "no_baja", "alta", "baja"), filtro=True, neutro="todos",
                         texto="volatilidad (ATR de 14 h en percentil de 30 días)"),
    "funding": Param("str", "todos", ("todos", "no_extremo_en_contra"), filtro=True, neutro="todos",
                     texto="funding"),
    "dias": Param("str", "todos", ("todos", "mar-jue", "sin_fin_de_semana"), filtro=True, neutro="todos",
                  texto="días"),
    "aperturas": Param("str", "todas_horas", ("todas_horas", "solo_en_apertura", "fuera_de_apertura"), filtro=True,
                       neutro="todas_horas", texto="aperturas de bolsa"),
    "regimen_cripto": Param("str", "ninguno", ("ninguno", "no_en_contra", "a_favor_estricto"), filtro=True,
                            neutro="ninguno", texto="régimen cripto (crypto-regime-analyzer)"),
    "noticias_en_contra": Param("str", "no", ("no",), filtro=True, neutro="no",
                                texto="noticias en contra (solo en vivo, fase 2)"),
}
TODOS = tuple(PARAMS)


# ---------------------------------------------------------------- cada filtro: True = la señal pasa

def tendencia_btc(m, t: np.ndarray) -> np.ndarray:
    """+1 / −1 / 0 de BTC en cada t: cierre del último día UTC completo frente a sus medias de 20 y 50 días."""
    if "BTC" not in m.monedas:
        return np.zeros(len(t), np.int8)
    b = m.barras("BTC", 1440)
    if len(b) < 50:
        return np.zeros(len(t), np.int8)
    c = np.asarray(b.c, float)
    m20, m50 = ind.sma(c, 20), ind.sma(c, 50)
    k = ind.indice_en(b.t_cierre, t)
    ok = k >= 49
    kk = np.clip(k, 0, len(c) - 1)
    up = ok & (c[kk] > m20[kk]) & (c[kk] > m50[kk])
    dn = ok & (c[kk] < m20[kk]) & (c[kk] < m50[kk])
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def percentil_atr(m, moneda: str, t: np.ndarray, n: int = 14, look: int = 720) -> np.ndarray:
    """ATR de n horas (rango verdadero / cierre anterior) de la última hora cerrada, en percentil (0-1) de las look
    horas anteriores (apertura.atr_pct_at). NaN sin historia."""
    b = m.barras(moneda, 60)
    out = np.full(len(t), np.nan)
    if len(b) < 2:
        return out
    c = np.asarray(b.c, float)
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(np.asarray(b.h) - np.asarray(b.l), np.maximum(np.abs(np.asarray(b.h) - pc),
                                                                np.abs(np.asarray(b.l) - pc))) / pc
    a = ind.sma(tr, n)
    pct = ind.percentil_previo(a, look)
    k = ind.indice_en(b.t_cierre, t)
    ok = k >= 0
    out[ok] = pct[k[ok]]
    return out


def percentil_funding(m, moneda: str, t: np.ndarray) -> np.ndarray:
    """Percentil (0-1) de la última tasa publicada frente a las de los 30 días previos (laboratorio.funding_features)."""
    ft, fr = m.funding(moneda)
    out = np.full(len(t), np.nan)
    if not len(ft):
        return out
    for i, x in enumerate(t):
        k = int(np.searchsorted(ft, x, "right")) - 1
        if k < 0:
            continue
        a = int(np.searchsorted(ft, x - 30 * DAY, "right"))
        w = fr[a:k]
        if len(w) >= 20:
            out[i] = float((w < fr[k]).mean())
    return out


def en_apertura(m, t: np.ndarray, minutos: int = 120) -> np.ndarray:
    om = np.array(sorted(o["open_ms"] for o in m.aperturas), np.int64)
    if not len(om):
        return np.zeros(len(t), bool)
    k = np.searchsorted(om, t, "right") - 1
    ok = k >= 0
    res = np.zeros(len(t), bool)
    res[ok] = t[ok] < om[k[ok]] + minutos * MIN
    return res


def dia_semana_mad(t: np.ndarray) -> np.ndarray:
    tz = _mad()
    return np.array([datetime.fromtimestamp(int(x) / 1000, tz).weekday() for x in t], np.int8)


# ---------------------------------------------------------------- régimen cripto (la skill, con 4 monedas)

_SKILL = None


def _skill():
    """Calculadoras de la skill crypto-regime-analyzer (biblioteca estándar), con las adaptaciones declaradas de
    medir_selectiva.py: 3 alts y 4 monedas."""
    global _SKILL
    if _SKILL is None:
        raiz = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "crypto-regime-analyzer" / "scripts"
        if str(raiz) not in sys.path:
            sys.path.insert(0, str(raiz))
        from calculators import alt_breadth_calculator as rc2   # noqa: E402
        from calculators import btc_trend_calculator as rc1   # noqa: E402
        from calculators import drawdown_vol_calculator as rc5   # noqa: E402
        from calculators import funding_calculator as rc4   # noqa: E402
        from calculators import momentum_thrust_calculator as rc6   # noqa: E402
        import scorer   # noqa: E402
        rc2.MIN_UNIVERSE = 3
        rc6.MIN_UNIVERSE = 4
        _SKILL = (rc1, rc2, rc4, rc5, rc6, scorer)
    return _SKILL


def _funding8(ft: np.ndarray, fr: np.ndarray, t0: int) -> float | None:
    i = int(np.searchsorted(ft, t0, "right")) - 1
    if i < 1:
        return None
    per = round((ft[i] - ft[i - 1]) / H)
    return float(fr[i]) * (8 / per if per in (1, 2, 4) else 1.0)


def zonas_regimen(m, t: np.ndarray) -> list[str]:
    """Zona (RISK_ON, NEUTRAL, RISK_OFF, UNKNOWN) en cada t con los cierres diarios cerrados y el funding liquidado."""
    if not all(c in m.monedas for c in ("BTC", "ETH", "SOL", "XRP")):
        return ["UNKNOWN"] * len(t)
    rc1, rc2, rc4, rc5, rc6, scorer = _skill()
    dd = {c: m.barras(c, 1440) for c in ("BTC", "ETH", "SOL", "XRP")}
    fu = {c: m.funding(c) for c in dd}
    cache: dict = {}
    out = []
    for x in t:
        nn = {c: int(np.searchsorted(dd[c].t_cierre, x, "right")) for c in dd}
        fk = tuple(int(np.searchsorted(fu[c][0], x, "right")) for c in dd)
        key = (tuple(nn.values()), fk)
        if key not in cache:
            cl = {c: np.asarray(dd[c].c[max(0, nn[c] - 420):nn[c]], float).tolist() for c in dd}
            if min(len(v) for v in cl.values()) < 60:
                cache[key] = "UNKNOWN"
            else:
                comps = {"btc_trend": rc1.calculate_btc_trend(cl["BTC"]),
                         "alt_breadth": rc2.calculate_alt_breadth({c: cl[c] for c in ("ETH", "SOL", "XRP")}),
                         "funding": rc4.calculate_funding_regime({f"{c}USDT": _funding8(*fu[c], int(x)) for c in dd}),
                         "drawdown_vol": rc5.calculate_drawdown_vol(cl["BTC"]),
                         "momentum_thrust": rc6.calculate_momentum_thrust(cl)}
                cache[key] = scorer.calculate_composite_score(comps)["zone"]
        out.append(cache[key])
    return out


# ---------------------------------------------------------------- aplicar

def aplicar(m, s: Senales, p: dict, admitidos=TODOS) -> Senales:
    """Quita las señales que no pasan los filtros activos de p (solo los que la familia admite)."""
    if not len(s):
        return s
    mask = np.ones(len(s), bool)
    t, d = s.t, s.dir.astype(int)
    if "regimen_btc" in admitidos and p.get("regimen_btc", "ninguno") != "ninguno":
        tr = tendencia_btc(m, t).astype(int)
        mask &= (tr == d) if p["regimen_btc"] == "a_favor" else (tr != -d)
    if "volatilidad" in admitidos and p.get("volatilidad", "todos") != "todos":
        v = np.full(len(s), np.nan)
        for i, c in enumerate(m.monedas):
            sel = s.moneda == i
            if sel.any():
                v[sel] = percentil_atr(m, c, t[sel])
        with np.errstate(invalid="ignore"):
            ok = {"no_baja": v >= 1 / 3, "alta": v >= 2 / 3, "baja": v < 1 / 3}[p["volatilidad"]]
        mask &= np.isfinite(v) & ok
    if "funding" in admitidos and p.get("funding", "todos") == "no_extremo_en_contra":
        f = np.full(len(s), np.nan)
        for i, c in enumerate(m.monedas):
            sel = s.moneda == i
            if sel.any():
                f[sel] = percentil_funding(m, c, t[sel])
        with np.errstate(invalid="ignore"):
            contra = ((d > 0) & (f >= 0.9)) | ((d < 0) & (f <= 0.1))
        mask &= ~contra
    if "dias" in admitidos and p.get("dias", "todos") != "todos":
        wd = dia_semana_mad(t)
        mask &= np.isin(wd, (1, 2, 3)) if p["dias"] == "mar-jue" else (wd < 5)
    if "aperturas" in admitidos and p.get("aperturas", "todas_horas") != "todas_horas":
        ea = en_apertura(m, t)
        mask &= ea if p["aperturas"] == "solo_en_apertura" else ~ea
    if "regimen_cripto" in admitidos and p.get("regimen_cripto", "ninguno") != "ninguno":
        z = np.array(zonas_regimen(m, t))
        if p["regimen_cripto"] == "no_en_contra":
            ok = (z != "UNKNOWN") & ~((d > 0) & (z == "RISK_OFF")) & ~((d < 0) & (z == "RISK_ON"))
        else:
            ok = ((d > 0) & (z == "RISK_ON")) | ((d < 0) & (z == "RISK_OFF"))
        mask &= ok
    return s.filtrar(mask)
