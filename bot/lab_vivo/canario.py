"""Canario del laboratorio vivo (docs/07, 7.6): antes de medir nada, el libro tiene que reproducir seis2_resumen.json
y cobrar las comisiones reales como el paper.

| Canario | Qué reproduce | Con qué |
|---|---|---|
| Libro | dentro 2.389 operaciones y −0,0569 R (± 0,00006); fuera 1.464 (± 2) y −0,075 R (± 0,0015) | Las entradas de ImpulseRun con el preset SEIS (news_mode «etiqueta», familia apertura_impulso), la salida A con stop de 2 % y la comprobación del plan de SalidaRef de medir_selectiva.py; aperturas del 28-10-2023 al 20-10-2026, corte el 16-08-2025 y comisiones.costes_de_backtest(0.10) |
| Comisiones | La tabla de bot/comisiones.py (OKX EU con X-Perps: contado 0,10 % / 0,08 %, X-Perps 0,05 % / 0,02 %; sin X-Perps: contado 0,35 % / 0,20 % y sin cortos) y, en las MISMAS operaciones del canario (largos y cortos, con parcial, objetivo, cerrojo, stop y tiempo), que lo que cobra el libro con el perfil real (tomador y devolución del creador) sea el bruto menos la comisión de la fórmula independiente de controles/costes/bruto_neto.py (comision_pct), al 1e-9 | El libro con el perfil okx_eu_con_xperps (el de todas las medidas) |

El canario del libro va con las comisiones planas del backtest (las de seis2_resumen.json); las medidas van con las
reales. Sin el segundo canario, un cambio en comisiones.py (otra tabla, otra forma de cobrar el creador) cambiaría
todas las medidas sin que nada avisara.

Si falla, el estado pasa a «canario_fallido» y se para toda medida (solo sigue la admisión). simulate_plan,
impulse_plan, exit_rules y compañía las está tocando otro flujo: el canario es lo que dice si siguen haciendo lo mismo."""
import logging
import time
from datetime import date, timedelta

import numpy as np

from .. import comisiones, config
from . import ajustes, datos, libro, salidas

log = logging.getLogger("lab_vivo.canario")
FROM, TO = date(2023, 10, 28), date(2026, 10, 20)
REF = {"dentro_n": 2389, "dentro_R": -0.0569, "fuera_n": 1464, "fuera_R": -0.075}
TOL = {"dentro_R": 6e-5, "fuera_n": 2, "fuera_R": 1.5e-3}
PERFIL = "backtest:0.10"
P_SEIS = {"measure_min": 15, "min_move_pct": 0.0, "entry": "t:15", "rvol_min": None, "vol_filter": "todos",
          "btc_filter": "ninguno", "lados": "ambos"}


def cifras(ops: list[dict], corte: int) -> dict:
    a_ = [r for r in ops if r["t"] < corte]
    b_ = [r for r in ops if r["t"] >= corte]
    m = lambda v, k: round(float(np.mean([r[k] for r in v])), 4) if v else None   # noqa: E731
    return {"dentro_n": len(a_), "dentro_R": m(a_, "R"), "dentro_R_funding": m(a_, "R_f"), "fuera_n": len(b_),
            "fuera_R": m(b_, "R"), "fuera_R_funding": m(b_, "R_f")}


def reproduce(c: dict) -> bool:
    return bool(c["dentro_n"] == REF["dentro_n"] and c["dentro_R"] is not None
                and abs(c["dentro_R"] - REF["dentro_R"]) < TOL["dentro_R"]
                and abs(c["fuera_n"] - REF["fuera_n"]) <= TOL["fuera_n"] and c["fuera_R"] is not None
                and abs(c["fuera_R"] - REF["fuera_R"]) < TOL["fuera_R"])


def fuente_canario() -> datos.Fuente:
    """Velas del 27-10-2023 hasta 9 días después del 20-10-2026 (para que acaben las de 7 días), en la caché
    lab_vivo\\cache\\canario-<firma> (mmap)."""
    from .. import apertura as ap
    from .. import backtest_aperturas as bt
    a = datos.medianoche_mad(FROM - timedelta(days=1))
    b = min(datos.medianoche_mad(TO + timedelta(days=9)), (ajustes.ahora_ms() // 60_000) * 60_000)
    meses = datos.meses_entre(a, b)
    fir = datos.firma_archivos([p for c in config.COINS for p in datos.archivos_moneda(c, meses)])
    car = ajustes.CACHE / f"canario-{fir}"
    per = {"epoca": "canario", "inicio": a, "fin": b, "corte": ajustes.CORTE_MS}
    if car.exists() and (car / "meta.json").exists():
        fu = datos.abrir_fuente(car)
    else:
        ajustes.CACHE.mkdir(parents=True, exist_ok=True)
        for p in ajustes.CACHE.glob("canario-*"):
            if p.is_dir():
                import shutil
                shutil.rmtree(p, ignore_errors=True)
        fu = datos.construir_fuente(a, b, per, carpeta=car, aperturas=False, tf_cache=())
    idx = bt.index_data()
    ops = [o for o in bt.build_openings(FROM, TO, idx) if o["id"] in ap.LAB_WINDOWS and not ap.special_session(o)]
    fu.aperturas = sorted(ops, key=lambda o: (o["open_ms"], o["id"]))
    return fu


# La tabla de comisiones que se mide (bot/comisiones.py, 29-10-2026): (tomador, creador) en % por lado
TABLA = {comisiones.CON_XPERPS: {"spot": (0.10, 0.08), "perp": (0.05, 0.02), "cortos": True},
         comisiones.SIN_XPERPS: {"spot": (0.35, 0.20), "perp": None, "cortos": False}}
TOL_COMISION = 1e-9
MUESTRA_COMISIONES = 600


def comprobar_tabla() -> list[str]:
    """Que bot/comisiones.py siga cobrando lo de TABLA en los dos perfiles (si no, todas las medidas cambiarían)."""
    fallos = []
    for perfil, esp in TABLA.items():
        for inst in ("spot", "perp"):
            if esp[inst] is None:
                continue
            try:
                t = comisiones.tasas(inst, perfil)
                got = (round(float(t["tomador"]), 6), round(float(t["creador"]), 6))
            except Exception as e:   # noqa: BLE001
                got = f"error: {e}"
            if got != esp[inst]:
                fallos.append(f"{perfil} {inst}: tomador/creador {got} (se esperaba {esp[inst]})")
        try:
            cortos = bool(comisiones.hay_cortos(perfil))
        except Exception as e:   # noqa: BLE001
            cortos = f"error: {e}"
        if cortos != esp["cortos"]:
            fallos.append(f"{perfil}: cortos {cortos} (se esperaba {esp['cortos']})")
    return fallos


def comision_pct(r: dict, plan: dict, d: int, tk: float, mk: float) -> float:
    """La fórmula independiente de BACKTEST_DIR/controles/costes/bruto_neto.py (comision_pct): la comisión (% del
    nominal) de una operación simulada SIN comisión (r["ret"] = bruto con el deslizamiento). La entrada paga tomador; la
    mitad en 3R (orden límite), creador; la salida final, creador si es el objetivo y tomador si es el stop, el cerrojo,
    el seguimiento o el tiempo. Lo negociado en cada tramo sale del bruto. tk y mk en fracción."""
    from .. import paper
    ru = paper.exit_rules(plan)
    sp = float(plan["stopPct"])
    g = r["ret"] / 100
    if r["th"]:
        q, tp = ru["tp1_frac"], ru["tp1R"]
        t_par, g_par, rem = q * (1 + d * sp * tp / 100), q * sp * tp / 100, 1 - q
    else:
        t_par, g_par, rem = 0.0, 0.0, 1.0
    t_fin = rem + d * (g - g_par)
    return 100 * (tk + mk * t_par + (mk if r["why"] == "objetivo" else tk) * t_fin)


def comprobar_comisiones(m, ops: list[dict], salida, D: libro.Datos | None = None,
                         maximo: int = MUESTRA_COMISIONES) -> dict:
    """En las operaciones dadas (hasta «maximo», por turnos entre largos y cortos, con y sin parcial u objetivo): lo
    que cobra el libro con el perfil real (okx_eu_con_xperps) = el bruto con el deslizamiento menos comision_pct con la
    TABLA. Devuelve {ok, n, por_salida, cortos, parciales, max_dif, tabla (fallos), texto}."""
    from .. import apertura as ap
    D = D or libro.Datos(m)
    grupos: dict = {}   # (con orden límite: parcial u objetivo, largo) → operaciones en orden; se toman por turnos
    for r in sorted(ops, key=lambda r: r["t"]):
        grupos.setdefault((bool(r.get("partial")) or r.get("why") == "objetivo", r["d"] > 0), []).append(r)
    orden = sorted(grupos, key=lambda k: (not k[0], not k[1]))
    sel = []
    while len(sel) < maximo and any(grupos[k] for k in orden):
        for k in orden:
            if grupos[k] and len(sel) < maximo:
                sel.append(grupos[k].pop(0))
    fallos_tabla = comprobar_tabla()
    tabla = TABLA[comisiones.CON_XPERPS]
    difs, por, n_cortos, n_parc, peores = [], {}, 0, 0, []
    for tr in sel:
        coin, j, d = tr["coin"], int(tr["j"]), int(tr["d"])
        inst = ap.impulse_instrument(d, dict(ap.SEIS2))
        tk, mk = (x / 100 for x in tabla[inst])
        sl = max(float(config.RULE_MIN_STOP_PCT), float(tr["sl_in"]))   # como Libro.entrar
        fin = int(tr.get("fin_ms") or 0)
        with libro.perfil_activo(comisiones.CON_XPERPS, True):
            lb = libro.Libro(D, salida)
            E = float(D.A[coin]["o"][j] * (1 + d * lb.slip))
            atr = D.atr_h_pct(coin, int(tr["t"])) if salida.s.get("trail_atr") else None
            plan, hh, ha, _ = salida.plan(d, E, sl, tr.get("rate"), inst, tr.get("objetivo_r"), atr)
            neto = lb.simular(coin, j, d, plan, hh, ha, fin_ms=fin)
        with libro.perfil_activo("backtest:0", True):   # el mismo camino sin ninguna comisión: el bruto
            bruto = libro.Libro(D, salida).simular(coin, j, d, plan, hh, ha, fin_ms=fin)
        if neto["why"] == "fin de datos":
            continue
        esperado = bruto["ret"] - comision_pct(bruto, plan, d, tk, mk)
        dif = abs(neto["ret"] - esperado)
        difs.append(dif)
        por[neto["why"]] = por.get(neto["why"], 0) + 1
        n_cortos += d < 0
        n_parc += bool(neto["th"])
        if dif > TOL_COMISION and len(peores) < 5:
            peores.append({"coin": coin, "t": int(tr["t"]), "why": neto["why"], "libro": round(neto["ret"], 9),
                           "esperado": round(esperado, 9)})
    mx = max(difs) if difs else None
    ok = bool(difs) and mx <= TOL_COMISION and not fallos_tabla
    texto = (f"Canario de comisiones: {len(difs)} operaciones ({n_cortos} cortos, {n_parc} con la mitad en 3R; "
             f"{', '.join(f'{k} {v}' for k, v in sorted(por.items()))}) cobran lo mismo que la fórmula de "
             f"bruto_neto.py (diferencia máxima {mx:.1e} %)" if ok else
             f"Canario de comisiones: NO cuadra ({len(difs)} operaciones, diferencia máxima "
             f"{'—' if mx is None else f'{mx:.1e}'} %{'; tabla: ' + '; '.join(fallos_tabla) if fallos_tabla else ''})")
    return {"ok": ok, "n": len(difs), "cortos": n_cortos, "parciales": n_parc, "por_salida": por, "max_dif": mx,
            "tabla": fallos_tabla,
            "peores": peores, "texto": texto}


def correr(fuente: datos.Fuente | None = None) -> dict:
    """Pasa los dos canarios: el del libro (seis2_resumen.json) y el de las comisiones reales. Devuelve {estado
    ok|fallido, libro: cifras, reproduce, comisiones, vetos, segundos}."""
    from .familias import obtener
    t0 = time.time()
    if not config.USER_RULES:
        return {"estado": "fallido", "motivo": "las reglas del usuario tienen que estar activas (BOT_USER_RULES)",
                "ts": ajustes.ahora_ms()}
    fu = fuente or fuente_canario()
    m = datos.Mercado(fu)
    fam = obtener("apertura_impulso")
    a = min(o["open_ms"] for o in fu.aperturas)
    b = max(o["open_ms"] for o in fu.aperturas) + 3 * 3_600_000
    s = fam.senales(m, dict(P_SEIS), a, b)
    D = libro.Datos(m)
    sal = salidas.SalidaRefSeis2()
    med = libro.medir(m, s, sal, perfil=PERFIL, datos=D)
    c = cifras(med.operaciones, ajustes.CORTE_MS)
    ok = reproduce(c)
    com = comprobar_comisiones(m, med.operaciones, sal, D)
    todo = ok and com["ok"]
    out = {"estado": "ok" if todo else "fallido", "ts": ajustes.ahora_ms(), "reproduce": ok,
           "libro": {**c, "referencia": REF, "abiertas_fin_de_datos": len(med.abiertas), "senales": len(s),
                     "vetos": dict(med.vetos)},
           "comisiones": com,
           "segundos": round(time.time() - t0, 1),
           "texto": (f"Canario del libro: dentro {c['dentro_n']} op {c['dentro_R']} R · fuera {c['fuera_n']} op "
                     f"{c['fuera_R']} R → {'reproduce' if ok else 'NO reproduce'} seis2_resumen.json "
                     f"(2.389 op −0,0569 R · 1.464 op −0,075 R). {com['texto']}")}
    log.info(out["texto"])
    return out
