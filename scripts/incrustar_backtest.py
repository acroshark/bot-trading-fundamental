"""Incrusta un resumen del backtest real en dashboard/radar.html para la versión de demostración (sin bot).

Uso: M:\\BotFundamental-runtime\\venv\\Scripts\\python.exe scripts\\incrustar_backtest.py
Con el bot en marcha la web no usa esto: pide /api/backtest.
También incrusta un resumen ligero del laboratorio (labData): el ejemplo de la pestaña Evolución lo usa para poner los
nombres y las cifras fuera de muestra reales de sus estrategias prometedoras (todo lo demás del ejemplo es inventado).
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import config  # noqa: E402

PAGE = ROOT / "dashboard" / "radar.html"
BLOCK = re.compile(r'<script type="application/json" id="btData">.*?</script>', re.S)
BLOCK_OPEN = re.compile(r'<script type="application/json" id="btOpenData">.*?</script>', re.S)
BLOCK_LAB = re.compile(r'<script type="application/json" id="labData">.*?</script>', re.S)
POINTS = 40
LAB_MAX = 10   # estrategias prometedoras o robustas como mucho


def compact(res: dict) -> dict:
    out = {k: res[k] for k in ("generated", "data", "leadlag") if k in res}
    strategies = []
    for s in res.get("strategies", []):
        s = dict(s)
        eq = s.get("equity") or []
        if len(eq) > POINTS:   # basta con 40 puntos para dibujar la curva
            step = len(eq) / POINTS
            eq = [eq[int(i * step)] for i in range(POINTS)] + [eq[-1]]
        s["equity"] = [[int(t), round(v, 2)] for t, v in eq]
        strategies.append(s)
    out["strategies"] = strategies
    return out


def lab_summary(lab: dict) -> dict:
    """Lo justo de laboratorio.json para la demostración: periodo, recuento y las prometedoras y robustas con sus cifras
    dentro y fuera de muestra (sin operaciones, rejillas ni vecinos: unos pocos KB)."""
    def strat(s: dict) -> dict:
        out = {k: s[k] for k in ("id", "name", "family", "class", "rules") if k in s}
        for part in ("is", "oos"):
            x = s.get(part) or {}
            out[part] = {k: x[k] for k in ("trades", "mean_R", "win_rate") if k in x}
        return out

    sm = lab.get("summary") or {}
    return {"generated": lab.get("generated"), "period": lab.get("period"), "frozen": (lab.get("frozen") or {}).get("date"),
            "summary": {k: sm[k] for k in ("tested", "promising", "robust", "expected_by_chance") if k in sm},
            "promising": [strat(s) for s in (lab.get("promising") or [])[:LAB_MAX] if isinstance(s, dict)],
            "robust": [strat(s) for s in (lab.get("robust") or [])[:LAB_MAX] if isinstance(s, dict)]}


def _tag(id_: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/json" id="{id_}">{payload}</script>'


def main() -> None:
    src = config.BACKTEST_DIR / "resultados.json"
    data = compact(json.loads(src.read_text(encoding="utf-8")))
    tag = _tag("btData", data)
    html = PAGE.read_text(encoding="utf-8")
    if BLOCK.search(html):
        html = BLOCK.sub(lambda _: tag, html)
    elif "<!--BT-->" in html:
        html = html.replace("<!--BT-->", tag)
    else:
        raise SystemExit("No encuentro el marcador <!--BT--> ni un bloque btData en la web")
    print(f"Incrustadas {len(data['strategies'])} estrategias ({len(tag) / 1024:.0f} KB)")
    src_open = config.BACKTEST_DIR / "aperturas.json"
    if src_open.exists():   # el motor de aperturas: sin la rejilla completa ni el detalle ventana × moneda
        op = json.loads(src_open.read_text(encoding="utf-8"))
        for v in op.get("variants", {}).values():
            v.pop("grid", None)
            v.pop("by_window_coin", None)
        op.get("exits", {}).pop("grid", None)   # 432 filas de la rejilla de salidas: la web no las pinta
        op.pop("trades_sample", None)
        tag2 = _tag("btOpenData", op)
        html = BLOCK_OPEN.sub(lambda _: tag2, html) if BLOCK_OPEN.search(html) else html.replace(tag, tag + "\n" + tag2)
        print(f"Incrustado el backtest de aperturas ({len(tag2) / 1024:.0f} KB)")
    src_lab = config.BACKTEST_DIR / "laboratorio.json"
    if src_lab.exists():   # resumen ligero del laboratorio para el ejemplo de la pestaña Evolución
        tag3 = _tag("labData", lab_summary(json.loads(src_lab.read_text(encoding="utf-8"))))
        if BLOCK_LAB.search(html):
            html = BLOCK_LAB.sub(lambda _: tag3, html)
        else:   # justo detrás del último bloque de datos del backtest
            m = BLOCK_OPEN.search(html) or BLOCK.search(html)
            html = html[:m.end()] + "\n" + tag3 + html[m.end():]
        print(f"Incrustado el resumen del laboratorio ({len(tag3) / 1024:.1f} KB)")
    PAGE.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
