"""Incrusta un resumen del backtest real en dashboard/radar.html para la versión de demostración (sin bot).

Uso: M:\\BotFundamental-runtime\\venv\\Scripts\\python.exe scripts\\incrustar_backtest.py
Con el bot en marcha la web no usa esto: pide /api/backtest.
También incrusta un resumen ligero del laboratorio (labData): el ejemplo de la pestaña Evolución lo usa para poner los
nombres y las cifras fuera de muestra reales de sus estrategias prometedoras (todo lo demás del ejemplo es inventado).
Y el backtest del motor en vivo (impulsoData, E2) y los de las estrategias «6 diarias» (seisData, de
seis_diarias_resumen.json), «6 diarias v2» (seis2Data, de seis2_resumen.json), «Ruptura V3» (ruptura3Data, de
ruptura3_resumen.json), «6 diarias 4R» (seis4rData, de seis4r_resumen.json) y «6 diarias 4R · stop 3 %» (seis4r3Data, de
seis4r3_resumen.json, la de la cartera paper desde el 29-10-2026), con la curva de R reducida. Lo que falte se deja tal cual.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import config  # noqa: E402
from bot.resumen_backtest import (RUPTURA3_FILE, SEIS2_FILE, SEIS4R3_FILE, SEIS4R_FILE, SEIS_FILE,  # noqa: E402
                                  impulso_summary, lab_summary, seis_summary)

PAGE = ROOT / "dashboard" / "radar.html"
BLOCK = re.compile(r'<script type="application/json" id="btData">.*?</script>', re.S)
BLOCK_OPEN = re.compile(r'<script type="application/json" id="btOpenData">.*?</script>', re.S)
BLOCK_LAB = re.compile(r'<script type="application/json" id="labData">.*?</script>', re.S)
BLOCK_IMP = re.compile(r'<script type="application/json" id="impulsoData">.*?</script>', re.S)
BLOCK_SEIS = re.compile(r'<script type="application/json" id="seisData">.*?</script>', re.S)
BLOCK_SEIS2 = re.compile(r'<script type="application/json" id="seis2Data">.*?</script>', re.S)
BLOCK_RUP3 = re.compile(r'<script type="application/json" id="ruptura3Data">.*?</script>', re.S)
BLOCK_S4R = re.compile(r'<script type="application/json" id="seis4rData">.*?</script>', re.S)
BLOCK_S4R3 = re.compile(r'<script type="application/json" id="seis4r3Data">.*?</script>', re.S)
POINTS = 40


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
    src_imp = config.BACKTEST_DIR / "impulso_7d.json"
    if src_imp.exists():   # la estrategia que opera el motor en vivo (pestaña Backtest)
        tag4 = _tag("impulsoData", impulso_summary(json.loads(src_imp.read_text(encoding="utf-8"))))
        if BLOCK_IMP.search(html):
            html = BLOCK_IMP.sub(lambda _: tag4, html)
        else:
            m = BLOCK_LAB.search(html) or BLOCK_OPEN.search(html) or BLOCK.search(html)
            html = html[:m.end()] + "\n" + tag4 + html[m.end():]
        print(f"Incrustado el backtest del motor en vivo ({len(tag4) / 1024:.1f} KB)")
    src_seis = config.BACKTEST_DIR / SEIS_FILE
    if src_seis.exists():   # la estrategia «6 diarias» (réplica de 3 años; pierde fuera de muestra, se enseña tal cual)
        tag5 = _tag("seisData", seis_summary(json.loads(src_seis.read_text(encoding="utf-8"))))
        if BLOCK_SEIS.search(html):
            html = BLOCK_SEIS.sub(lambda _: tag5, html)
        else:
            m = BLOCK_IMP.search(html) or BLOCK_LAB.search(html) or BLOCK_OPEN.search(html) or BLOCK.search(html)
            html = html[:m.end()] + "\n" + tag5 + html[m.end():]
        print(f"Incrustado el backtest de «6 diarias» ({len(tag5) / 1024:.1f} KB)")
    src_seis2 = config.BACKTEST_DIR / SEIS2_FILE
    if src_seis2.exists():   # «6 diarias v2»: la mitad en 3R y el resto con el cerrojo (también pierde: tal cual)
        tag6 = _tag("seis2Data", seis_summary(json.loads(src_seis2.read_text(encoding="utf-8"))))
        if BLOCK_SEIS2.search(html):
            html = BLOCK_SEIS2.sub(lambda _: tag6, html)
        else:
            m = (BLOCK_SEIS.search(html) or BLOCK_IMP.search(html) or BLOCK_LAB.search(html)
                 or BLOCK_OPEN.search(html) or BLOCK.search(html))
            html = html[:m.end()] + "\n" + tag6 + html[m.end():]
        print(f"Incrustado el backtest de «6 diarias v2» ({len(tag6) / 1024:.1f} KB)")
    html = incrustar_seis4r3(incrustar_seis4r(incrustar_ruptura3(html)))
    PAGE.write_text(html, encoding="utf-8")


def incrustar_ruptura3(html: str) -> str:
    """El bloque ruptura3Data (la Ruptura V3, réplica de 3 años: fuera de muestra gana y dentro no, tal cual), justo
    detrás del último bloque de datos. Sin ruptura3_resumen.json, la página tal cual."""
    src = config.BACKTEST_DIR / RUPTURA3_FILE
    if not src.exists():
        return html
    tag = _tag("ruptura3Data", seis_summary(json.loads(src.read_text(encoding="utf-8"))))
    if BLOCK_RUP3.search(html):
        return BLOCK_RUP3.sub(lambda _: tag, html)
    m = (BLOCK_SEIS2.search(html) or BLOCK_SEIS.search(html) or BLOCK_IMP.search(html) or BLOCK_LAB.search(html)
         or BLOCK_OPEN.search(html) or BLOCK.search(html))
    if not m:
        raise SystemExit("No encuentro ningún bloque de datos del backtest en la web")
    print(f"Incrustado el backtest de la Ruptura V3 ({len(tag) / 1024:.1f} KB)")
    return html[:m.end()] + "\n" + tag + html[m.end():]


def incrustar_seis4r(html: str) -> str:
    """El bloque seis4rData («6 diarias 4R», réplica de 3 años: fuera de muestra gana y dentro pierde; frágil, tal
    cual), justo detrás del último bloque de datos. Sin seis4r_resumen.json, la página tal cual."""
    src = config.BACKTEST_DIR / SEIS4R_FILE
    if not src.exists():
        return html
    tag = _tag("seis4rData", seis_summary(json.loads(src.read_text(encoding="utf-8"))))
    if BLOCK_S4R.search(html):
        return BLOCK_S4R.sub(lambda _: tag, html)
    m = (BLOCK_RUP3.search(html) or BLOCK_SEIS2.search(html) or BLOCK_SEIS.search(html) or BLOCK_IMP.search(html)
         or BLOCK_LAB.search(html) or BLOCK_OPEN.search(html) or BLOCK.search(html))
    if not m:
        raise SystemExit("No encuentro ningún bloque de datos del backtest en la web")
    print(f"Incrustado el backtest de «6 diarias 4R» ({len(tag) / 1024:.1f} KB)")
    return html[:m.end()] + "\n" + tag + html[m.end():]


def incrustar_seis4r3(html: str) -> str:
    """El bloque seis4r3Data («6 diarias 4R · stop 3 %», la de la cartera paper desde el 29-10-2026: réplica de 3 años con
    scripts/replica_seis4r.py --preset seis4r3; gana en los dos tramos, pero se eligió mirándolos entre 50 combinaciones),
    justo detrás de seis4rData o del último bloque de datos. Sin seis4r3_resumen.json, la página tal cual."""
    src = config.BACKTEST_DIR / SEIS4R3_FILE
    if not src.exists():
        return html
    tag = _tag("seis4r3Data", seis_summary(json.loads(src.read_text(encoding="utf-8"))))
    if BLOCK_S4R3.search(html):
        return BLOCK_S4R3.sub(lambda _: tag, html)
    m = (BLOCK_S4R.search(html) or BLOCK_RUP3.search(html) or BLOCK_SEIS2.search(html) or BLOCK_SEIS.search(html)
         or BLOCK_IMP.search(html) or BLOCK_LAB.search(html) or BLOCK_OPEN.search(html) or BLOCK.search(html))
    if not m:
        raise SystemExit("No encuentro ningún bloque de datos del backtest en la web")
    print(f"Incrustado el backtest de «6 diarias 4R · stop 3 %» ({len(tag) / 1024:.1f} KB)")
    return html[:m.end()] + "\n" + tag + html[m.end():]


if __name__ == "__main__":
    main()
