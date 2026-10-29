"""Resúmenes ligeros de los backtests para la pestaña Backtest de la web.

- `lab_summary`: lo justo de laboratorio.json (periodo, recuento, prometedoras y robustas con sus cifras dentro y fuera de
  muestra y la cuenta honesta en avance), unos pocos KB.
- `impulso_summary`: impulso_7d.json, que escribe BACKTEST_DIR/controles/compara_impulso.py, con la curva de R acumulado
  reducida a unos cientos de puntos y, si la curva trae un punto por operación, la mejor y la peor operación y cuántas
  ganadoras ganan menos de 2R y de 1R (per_trade). Es la variante de 7 días que opera el motor en vivo (E2, preset IMPULSO).
- `seis_summary`: seis_diarias_resumen.json (misma forma que impulso_7d.json más «nombre» y «params»): la réplica del
  motor con el preset SEIS («6 diarias», apertura.SEIS) en 3 años. Pierde fuera de muestra: se enseña tal cual.
- «seis2» (con la misma seis_summary): seis2_resumen.json, la réplica del motor con el preset SEIS2 («6 diarias v2»:
  las entradas de SEIS y la salida «A», la mitad en 3R y el resto con el cerrojo) en 3 años. También pierde: tal cual.

Los usa /api/backtest/impulso (bot/server.py) y scripts/incrustar_backtest.py para la demostración.
"""
import json
from pathlib import Path

from . import config

LAB_MAX = 10       # estrategias prometedoras o robustas como mucho
CURVE_POINTS = 150


def lab_summary(lab: dict) -> dict:
    def strat(s: dict) -> dict:
        out = {k: s[k] for k in ("id", "name", "family", "class", "rules") if k in s}
        for part in ("is", "oos"):
            x = s.get(part) or {}
            out[part] = {k: x[k] for k in ("trades", "mean_R", "win_rate") if k in x}
        return out

    sm = lab.get("summary") or {}
    wf = ((lab.get("walk_forward") or {}).get("account_quarter") or {})
    acc = wf.get("riesgo_0_5_lev1") or {}
    avance = {k: acc[k] for k in ("days", "trades", "total_pct", "max_dd_pct") if k in acc}
    if avance:
        avance.update({"desde": wf.get("from"), "top": wf.get("top"), "periodo": wf.get("period")})
    return {"generated": lab.get("generated"), "period": lab.get("period"), "frozen": (lab.get("frozen") or {}).get("date"),
            "summary": {k: sm[k] for k in ("tested", "promising", "robust", "expected_by_chance", "beat_base", "base_tested",
                                           "expected_by_chance_all") if k in sm},
            "promising": [strat(s) for s in (lab.get("promising") or [])[:LAB_MAX] if isinstance(s, dict)],
            "robust": [strat(s) for s in (lab.get("robust") or [])[:LAB_MAX] if isinstance(s, dict)],
            "avance": avance or None}


def _thin(curve: list, points: int) -> list:
    if len(curve) <= points:
        return curve
    step = len(curve) / points
    return [curve[int(i * step)] for i in range(points)] + [curve[-1]]


def per_trade(curve: list, n) -> dict:
    """Con un punto de la curva por operación (len == n), el R de cada una es la resta de dos puntos seguidos: la mejor
    (max_R), la peor (min_R), cuántas ganan (ganadoras_n) y cuántas de ellas ganan menos de 2R y de 1R (el cierre por
    tiempo sale con lo que haya: la regla «o gano más de 2 o pierdo 1» no vale para el reloj). Sin eso, {}."""
    if not curve or not isinstance(n, int) or len(curve) != n:
        return {}
    rs, prev = [], 0.0
    for _, v in curve:
        rs.append(v - prev)
        prev = v
    win = [r for r in rs if r > 1e-9]
    return {"max_R": round(max(rs), 3), "min_R": round(min(rs), 3), "ganadoras_n": len(win),
            "ganadoras_bajo_2R": sum(1 for r in win if r < 2 - 1e-9), "ganadoras_bajo_1R": sum(1 for r in win if r < 1 - 1e-9)}


def impulso_summary(d: dict, points: int = CURVE_POINTS) -> dict:
    out = {k: d.get(k) for k in ("generated", "split", "sl_pct", "nota")}
    for part in ("fuera", "dentro"):
        b = dict(d.get(part) or {})
        curve = b.get("curva_R") or []
        dd, peak = 0.0, 0.0
        for _, v in curve:   # caída máxima del R acumulado, con la curva completa
            peak = max(peak, v)
            dd = max(dd, peak - v)
        b["max_dd_R"] = round(dd, 2) if curve else None
        b["total_R"] = curve[-1][1] if curve else None
        b.update(per_trade(curve, b.get("n")))   # también con la curva completa, antes de reducirla
        b["curva_R"] = _thin(curve, points)
        out[part] = b
    return out


def seis_summary(d: dict, points: int = CURVE_POINTS) -> dict:
    """Como impulso_summary, con el nombre y los parámetros de la estrategia «6 diarias»."""
    out = impulso_summary(d, points)
    out["nombre"] = d.get("nombre")
    out["params"] = d.get("params") if isinstance(d.get("params"), dict) else None
    return out


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


SEIS_FILE = "seis_diarias_resumen.json"
SEIS2_FILE = "seis2_resumen.json"


def load() -> dict:
    """{impulso, lab, seis, seis2} desde BACKTEST_DIR; lo que falte va como None."""
    imp = _read(config.BACKTEST_DIR / "impulso_7d.json")
    lab = _read(config.BACKTEST_DIR / "laboratorio.json")
    seis = _read(config.BACKTEST_DIR / SEIS_FILE)
    seis2 = _read(config.BACKTEST_DIR / SEIS2_FILE)
    return {"impulso": impulso_summary(imp) if isinstance(imp, dict) else None,
            "lab": lab_summary(lab) if isinstance(lab, dict) else None,
            "seis": seis_summary(seis) if isinstance(seis, dict) else None,
            "seis2": seis_summary(seis2) if isinstance(seis2, dict) else None}
