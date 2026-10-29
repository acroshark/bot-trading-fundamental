"""Lo que calcula cada trabajo del laboratorio vivo (docs/07, apartado 8 y 11.2). Cada trabajo corre en su subproceso
(python -X utf8 -m bot.lab_vivo tarea <id>), lee su encargo (trabajos\\<id>.trabajo.json) y deja su resultado en
<id>.resultado.json, que ingiere el corredor (el único que escribe en lab_vivo.db). Tras cada variante guarda su punto
(<id>.punto.json) y mira pausa.ahora: si existe, sale con el código 75 («pausado») y se reanuda desde ese punto.

| Trabajo | Qué calcula |
|---|---|
| datos | La caché de la época (datos.preparar_epoca) |
| canario | canario.correr |
| admision | A8 (prueba del recorte con datos reales, determinismo, por flanco y velocidad) y A9 (señales por día dentro) |
| rapida | Cada variante SOLO con datos anteriores al corte (Mercado.recortado(corte)): R1 y R2 |
| completa | Las supervivientes en todo el periodo: K1 a K5 (K4, el avance trimestral, es de la hipótesis) |
| robustez | Una variante: B1 a B6 y lo que se informa (sin X-Perps, objetivo superado en 2 pb, solo largos…) |
| avance | Las variantes en prueba en avance, con las velas que van llegando (Mercado vivo) |"""
import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from . import ajustes, criterios, datos, libro, medidas, salidas

log = logging.getLogger("lab_vivo.etapas")
MIN, H, DAY = 60_000, 3_600_000, 86_400_000


class Pausado(Exception):
    """La tarea se detiene de forma cooperativa (pausa de una apertura): el corredor la reanuda desde su punto."""


def semilla(vid: str) -> int:
    return (int(hashlib.sha1(vid.encode()).hexdigest()[:8], 16) + 20261029) % (2 ** 31)


def comprobar_pausa() -> None:
    if ajustes.PAUSA_AHORA.exists() or ajustes.PARAR.exists():
        raise Pausado()


def _redondea(x, d=6):
    if isinstance(x, float):
        return round(x, d) if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _redondea(v, d) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_redondea(v, d) for v in x]
    if isinstance(x, (np.floating,)):
        return _redondea(float(x), d)
    if isinstance(x, (np.integer,)):
        return int(x)
    return x


def _mercado(tr: dict):
    return datos.Mercado(datos.abrir_fuente(Path(tr["cache"])))


def _senales(m, v: dict, desde: int, hasta: int):
    from .familias import obtener
    fam = obtener(v["familia"])
    return fam.senales(m, v["params"], desde, hasta)


def _medir(m, s, v: dict, desde=None, hasta=None, estres=None, perfil=libro.PERFIL, D=None):
    from .. import apertura as ap
    return libro.medir(m, s, v["salida"], perfil=perfil, reglas=not v.get("fuera_de_reglas"), desde_ms=desde,
                       hasta_ms=hasta, estres=estres, datos=D, ventanas=ap.LAB_WINDOWS)


def _dias(a_ms: int, b_ms: int) -> tuple[str, str]:
    return medidas.dia_de(a_ms), medidas.dia_de(b_ms - 1)


def calentamiento_d(v: dict) -> int:
    """Días de datos antes del tramo que se mide: los de la familia y los que piden sus filtros (el régimen cripto de
    la skill, 420 cierres diarios; la tendencia diaria de BTC, 50). Sin ellos, la prueba del recorte de una variante con
    el régimen cripto no veía ninguna señal (no probaba nada)."""
    from .familias import obtener
    p = v.get("params") or {}
    d = int(obtener(v["familia"]).calentamiento_d)
    if p.get("regimen_cripto") not in (None, "ninguno"):
        d = max(d, ajustes.CALENTAMIENTO_MAX_D)
    if p.get("regimen_btc") not in (None, "ninguno"):
        d = max(d, 55)
    return d


def _ops_lite(ops: list[dict]) -> list[dict]:
    keys = ("coin", "d", "t", "t_fill", "dia", "ext", "ret", "R", "fund_pct", "R_f", "why", "partial", "N", "sl",
            "sl_in", "j", "rate", "objetivo_r", "fin_ms", "inst", "eq", "clave", "ventana", "first", "mfe", "mae")
    return [{k: (float(o[k]) if isinstance(o.get(k), (float, np.floating)) else o.get(k)) for k in keys} for o in ops]


# ---------------------------------------------------------------- admisión (A8 y A9)

def admision(tr: dict, punto: dict | None, por_senal: int | None = None, pausa=None) -> dict:
    """A8: con datos reales, sobre dos tramos de 60 días (con el calentamiento de la familia y de sus filtros), la
    prueba del recorte de recorte.py: un corte en cada señal (todas o una muestra fija) y 20 cortes al azar (semilla
    fija); las señales hasta el corte, idénticas con los datos hasta el corte y con todos. Dos pasadas iguales; menos de
    max_senales_dia por día y una por moneda y minuto; ≤ 60 s con los 3 años. A9: señales por día natural dentro de
    muestra ≥ 0,25. pausa: la función que mira si hay que parar (por defecto, pausa.ahora y parar)."""
    from . import recorte
    from .familias import obtener
    m = _mercado(tr)
    per = m.periodo
    res = dict((punto or {}).get("variantes") or {})
    ncor = criterios.c("admision", "recorte_cortes")
    tramo_d = criterios.c("admision", "recorte_tramo_d")
    por_senal = recorte.POR_SENAL if por_senal is None else por_senal
    pausa = pausa or comprobar_pausa
    for v in tr["variantes"]:
        if v["vid"] in res:
            continue
        fam = obtener(v["familia"])
        mot = []
        t0 = time.time()
        s = fam.senales(m, v["params"], per["inicio"], per["fin"])
        seg = time.time() - t0
        s2 = fam.senales(m, v["params"], per["inicio"], per["fin"])
        if not s.iguales(s2):
            mot.append("A8: dos pasadas iguales dan señales distintas (no es determinista)")
        if seg > criterios.c("admision", "velocidad_max_s"):
            mot.append(f"A8: demasiado lenta ({seg:.0f} s con los 3 años; máximo 60 s)")
        if len(s):
            dias = (s.t // DAY)
            _, cuenta = np.unique(dias, return_counts=True)
            if cuenta.max() > fam.max_senales_dia:
                mot.append(f"A8: {int(cuenta.max())} señales en un día (máximo {fam.max_senales_dia:.0f}): ¿señales por nivel "
                           "en vez de por flanco?")
            clave = s.t * 16 + s.moneda.astype(np.int64)
            if len(np.unique(clave)) < len(s):
                mot.append("A8: más de una señal por moneda y minuto")
            if not np.all(s.t % MIN == 0):
                mot.append("A8: t no es múltiplo de 60.000 ms")
            if not np.all(np.isin(s.dir, (-1, 1))):
                mot.append("A8: dir tiene que ser +1 o −1")
        rng = np.random.default_rng(semilla(v["vid"]))
        cal = (calentamiento_d(v) + 2) * DAY
        tramos = [(per["inicio"] + cal, per["inicio"] + cal + tramo_d * DAY),
                  (per["corte"], per["corte"] + tramo_d * DAY)]
        rc = recorte.comprobar(fam, [(m.ventana(a - cal, b), a, b) for a, b in tramos], v["params"], rng, ncor // 2,
                               por_senal=por_senal)
        mot += recorte.motivos(rc)
        dentro = s.entre(per["inicio"], per["corte"])
        dias_d = max(1, (per["corte"] - per["inicio"]) // DAY)
        sd = len(dentro) / dias_d
        if not v.get("exploratoria") and sd < criterios.c("admision", "min_senales_dia_dentro"):
            mot.append(f"A9: muy pocas señales ({sd:.2f} al día dentro de muestra; mínimo 0,25)".replace(".", ","))
        res[v["vid"]] = {"ok": not mot, "motivos": mot, "senales": int(len(s)), "senales_dia_dentro": round(sd, 4),
                         "segundos": round(seg, 2),
                         "recorte": {k: rc[k] for k in ("cortes_senal", "malos_senal", "cortes_azar", "malos_azar",
                                                        "senales", "tramos_vacios", "segundos")}}
        guardar_punto(tr, {"variantes": res})
        pausa()
    return {"variantes": res}


# ---------------------------------------------------------------- etapa 1: rápida (solo dentro de muestra)

def rapida(tr: dict, punto: dict | None) -> dict:
    m0 = _mercado(tr)
    per = m0.periodo
    m = m0.recortado(per["corte"])      # nada del fuera de muestra: ni señales ni velas
    D = libro.Datos(m)
    res = dict((punto or {}).get("variantes") or {})
    a_d, b_d = _dias(per["inicio"], per["corte"])
    for v in tr["variantes"]:
        if v["vid"] in res:
            continue
        s = _senales(m, v, per["inicio"], per["corte"])
        med = _medir(m, s, v, per["inicio"], per["corte"], D=D)
        ops = med.operaciones
        R = medidas.media(ops)
        mot = []
        if len(ops) < criterios.c("rapida", "min_ops_dentro"):
            mot.append(f"R1: {len(ops)} operaciones dentro (mínimo {criterios.c('rapida', 'min_ops_dentro')})")
        if R is None or R <= criterios.c("rapida", "R_dentro_min"):
            mot.append(f"R2: R medio dentro {R if R is None else round(R, 4)} (tiene que ser > 0)")
        tm = medidas.tramo(ops, a_d, b_d, semilla=semilla(v["vid"]))
        res[v["vid"]] = {"n_dentro": len(ops), "R_dentro": R, "pasa": not mot, "motivos": mot,
                         "metricas": {"dentro": tm, "vetos": dict(med.vetos), "senales": int(len(s)),
                                      "abiertas_fin_de_datos": len(med.abiertas)},
                         "serie": {"desde": a_d, "r": medidas.serie_diaria(ops, a_d, b_d).round(6).tolist()}}
        guardar_punto(tr, {"variantes": res})
        comprobar_pausa()
    return {"variantes": res}


# ---------------------------------------------------------------- etapa 2: completa (todo el periodo)

def _completa_variante(m, D, v, per, ops_all) -> dict:
    corte, fin = per["corte"], per["fin"]
    din = [r for r in ops_all if r["t"] < corte]
    dfu = [r for r in ops_all if r["t"] >= corte]
    a_d, c_d = _dias(per["inicio"], corte)
    _, f_d = _dias(corte, fin)
    cf = medidas.dia_de(corte)
    serie = medidas.serie_diaria(dfu, cf, f_d)
    t, p = medidas.p_hac(serie)
    mo = medidas.momentos(serie)
    dias_fuera = len(serie)
    op_dia = len(dfu) / max(1, dias_fuera)
    h1, h2 = medidas.mitades(dfu, corte, fin)
    sem = medidas.semanas(dfu)
    R = medidas.media(dfu)
    mot = []
    c = lambda k: criterios.c("completa", k)   # noqa: E731
    if len(dfu) < c("min_ops_fuera") or sem < c("min_semanas_fuera"):
        mot.append(f"K1: {len(dfu)} operaciones fuera en {sem} semanas (mínimo {c('min_ops_fuera')} y {c('min_semanas_fuera')})")
    if R is None or R <= c("R_fuera_min"):
        mot.append(f"K2: R medio fuera {None if R is None else round(R, 4)} (tiene que ser > 0)")
    if not (h1 is not None and h2 is not None and h1 > 0 and h2 > 0):
        mot.append(f"K3: R medio de las dos mitades del fuera de muestra: {_r4(h1)} y {_r4(h2)} (las dos > 0)")
    clase = medidas.clase_frecuencia(op_dia)
    if clase is None:
        mot.append(f"K5: {op_dia:.2f} operaciones por día natural fuera (mínimo 0,25)".replace(".", ","))
    tm_d = medidas.tramo(din, a_d, c_d, semilla=semilla(v["vid"]))
    tm_f = medidas.tramo(dfu, cf, f_d, semilla=semilla(v["vid"]) + 1)
    return {"n_dentro": len(din), "n_fuera": len(dfu), "R_dentro": medidas.media(din), "R_fuera": R, "p_fuera": p,
            "t_fuera": t, "sr_fuera": mo["sr"], "dias_fuera": dias_fuera, "asimetria": mo["asimetria"],
            "curtosis": mo["curtosis"], "motivos": mot, "pasa_k": not mot, "frecuencia": clase,
            "metricas": {"dentro": tm_d, "fuera": tm_f, "mitades": [_r4(h1), _r4(h2)], "semanas_fuera": sem,
                         "op_dia_fuera": round(op_dia, 4), "por_trimestre": medidas.por_trimestre(ops_all),
                         "por_moneda": medidas.por(dfu, lambda r: r["coin"]),
                         "por_ano": medidas.por(ops_all, lambda r: r["dia"][:4])},
            "serie": {"desde": cf, "r": serie.round(6).tolist()}}


def _r4(x):
    return None if x is None else round(float(x), 4)


def completa(tr: dict, punto: dict | None) -> dict:
    m = _mercado(tr)
    per = m.periodo
    D = libro.Datos(m)
    res = dict((punto or {}).get("variantes") or {})
    ops_por = {}
    for v in tr["variantes"]:
        s = _senales(m, v, per["inicio"], per["fin"])
        med = _medir(m, s, v, per["inicio"], per["fin"], D=D)
        ops_por[v["vid"]] = med.operaciones
        if v["vid"] not in res:
            r = _completa_variante(m, D, v, per, med.operaciones)
            r["metricas"]["vetos"] = dict(med.vetos)
            res[v["vid"]] = r
            guardar_punto(tr, {"variantes": res})
        comprobar_pausa()
    vivas = {vid: [{"t": o["t"], "R_f": o["R_f"]} for o in ops] for vid, ops in ops_por.items()
             if not next(v for v in tr["variantes"] if v["vid"] == vid).get("fuera_de_reglas")}
    k4 = medidas.avance_trimestral(vivas, per["corte"], per["fin"]) if vivas else {"pasa": False, "trimestres": []}
    for vid, r in res.items():
        r["k4"] = k4
        mot = list(r["motivos"])
        if not k4["pasa"]:
            mot.append(f"K4: avance trimestral de la hipótesis: R {k4.get('R')} con {k4.get('positivos', '0 de 0')} "
                       "trimestres positivos (hace falta R > 0 y al menos la mitad)")
        r["motivos"] = mot
        r["pasa"] = not mot
    return {"variantes": res, "k4": k4}


# ---------------------------------------------------------------- etapa 3: robustez (una variante)

def fondo_azar(m, D, v: dict, sl: float, d: int, desde: int, hasta: int, carpeta: Path | None) -> dict:
    """Fondo de entradas al azar para la base emparejada de un solo sentido: una entrada en cada hora en punto del
    fuera de muestra, por moneda, con la salida de la variante y el stop mediano de la estrategia (simulado una vez por
    época y salida, y guardado). {(moneda, día de la semana, hora de Madrid): [R_f, ...]}."""
    key = hashlib.sha1(json.dumps({"s": v["salida"], "sl": round(sl, 2), "d": d, "a": desde, "b": hasta},
                                  sort_keys=True).encode()).hexdigest()[:16]
    p = (carpeta / f"fondo-{key}.json") if carpeta else None
    if p and p.exists():
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {tuple(json.loads(k)): v_ for k, v_ in raw.items()}
    from .. import apertura as ap
    sal = salidas.Salida(v["salida"])
    out: dict = {}
    with libro.perfil_activo(libro.PERFIL, not v.get("fuera_de_reglas")):
        lb = libro.Libro(D, sal)
        for c in m.monedas:
            x = D.A[c]
            for t in range((desde // H + 1) * H, hasta, H):
                j = int(np.searchsorted(x["t"], t))
                if j >= len(x["t"]) or x["t"][j] - t > 15 * MIN:
                    continue
                inst = ap.impulse_instrument(d, lb.seis2)
                rate = D.tasa_en(c, t)
                E = float(x["o"][j] * (1 + d * lb.slip))
                plan, hh, ha, SL = sal.plan(d, E, sl, rate, inst)
                if not plan["ok"]:
                    continue
                r = lb.simular(c, j, d, plan, hh, ha)
                if r["why"] == "fin de datos":
                    continue
                fl = D.funding_de(c, "long" if d > 0 else "short", r["t_fill"], r["ext"], r["th"]) if inst == "perp" else []
                dt = datetime.fromtimestamp(t / 1000, ajustes.MAD)
                out.setdefault((c, dt.weekday(), dt.hour), []).append(round((r["ret"] + sum(q for _, q in fl)) / SL, 5))
            comprobar_pausa()
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({json.dumps(list(k)): v_ for k, v_ in out.items()}), encoding="utf-8")
    return out


def robustez(tr: dict, punto: dict | None) -> dict:
    m = _mercado(tr)
    per = m.periodo
    D = libro.Datos(m)
    v = tr["variante"]
    corte, fin = per["corte"], per["fin"]
    s = _senales(m, v, per["inicio"], fin)
    med = _medir(m, s, v, per["inicio"], fin, D=D)
    ops = med.operaciones
    dfu = [r for r in ops if r["t"] >= corte]
    cf, f_d = _dias(corte, fin)
    out = dict((punto or {}).get("resultado") or {})
    sem = semilla(v["vid"])
    rb = criterios.CRITERIOS["robustez"]
    mot = []
    if "B1" not in out:
        modo = medidas.modo_base(dfu)
        if modo == "dos_sentidos":
            opp = libro.reestres(m, dfu, v["salida"], "contrario", reglas=not v.get("fuera_de_reglas"), datos=D)
            mapa = {(o["t"], o["coin"]): o["R_f"] for o in opp}
            base = medidas.base_dos_sentidos(dfu, [mapa.get((r["t"], r["coin"])) for r in dfu], sem)
        else:
            d = 1 if sum(r["d"] > 0 for r in dfu) >= len(dfu) / 2 else -1
            sl = float(np.median([r["sl"] for r in dfu])) if dfu else 2.0
            car = Path(tr["cache"]) / "fondos"
            fondo = fondo_azar(m, D, v, sl, d, corte, fin, car)
            base = medidas.base_un_sentido(dfu, fondo, sem)
        out["B1"] = base
        guardar_punto(tr, {"resultado": out})
        comprobar_pausa()
    if "estres" not in out:
        es = {}
        for k in ("desliz_x2_y_stops", "entrada_1_min_tarde", "sin_xperps", "objetivo_superado_2pb"):
            rr = libro.reestres(m, dfu, v["salida"], k, reglas=not v.get("fuera_de_reglas"), datos=D)
            vals = [x["R_f"] for x in rr if x.get("R_f") is not None]
            es[k] = {"n": len(vals), "R": round(float(np.mean(vals)), 4) if vals else None,
                     "sin_plan": sum(1 for x in rr if not x.get("plan", True))}
            comprobar_pausa()
        out["estres"] = es
        guardar_punto(tr, {"resultado": out})
    if "hermanas" not in out:   # B6: el fuera de muestra de todas las variantes (también las que no pasaron la rápida)
        her = {}
        for h in tr.get("hermanas") or []:
            sh = _senales(m, h, per["inicio"], fin)
            mh = _medir(m, sh, h, per["inicio"], fin, D=D)
            r = _completa_variante(m, D, h, per, mh.operaciones)
            her[h["vid"]] = {k: r[k] for k in ("n_dentro", "n_fuera", "R_dentro", "R_fuera", "p_fuera", "sr_fuera",
                                               "dias_fuera", "asimetria", "curtosis", "metricas", "serie")}
            comprobar_pausa()
        out["hermanas"] = her
        guardar_punto(tr, {"resultado": out})
    R_sin5 = medidas.sin_mayores(dfu, rb["sin_mayores"])
    R_sinsem, mejor_sem = medidas.sin_mejor_semana(dfu)
    p_base = out["B1"].get("p", 1.0)
    if not (p_base < rb["base_p_max"]):
        mot.append(f"B1: frente a su base emparejada p = {p_base} (hace falta < 0,05)".replace(".", ","))
    if R_sin5 is None or R_sin5 <= 0:
        mot.append(f"B2: sin sus 5 mayores operaciones R medio {_r4(R_sin5)} (tiene que ser > 0)")
    if R_sinsem is None or R_sinsem <= 0:
        mot.append(f"B3: sin su mejor semana ({mejor_sem}) R medio {_r4(R_sinsem)} (tiene que ser > 0)")
    for cod, k in (("B4", "desliz_x2_y_stops"), ("B5", "entrada_1_min_tarde")):
        rv = out["estres"][k]["R"]
        if rv is None or rv <= 0:
            mot.append(f"{cod}: estrés {k}: R medio fuera {rv} (tiene que ser > 0)")
    fuera_todas = {**{h: x["R_fuera"] for h, x in out["hermanas"].items()}, **(tr.get("fuera_conocidas") or {}),
                   v["vid"]: medidas.media(dfu)}
    en_reglas = [vid for vid in fuera_todas if vid not in set(tr.get("fuera_de_reglas") or [])]
    pos = sum(1 for vid in en_reglas if fuera_todas[vid] is not None and fuera_todas[vid] > 0)
    nec = math.ceil(len(en_reglas) / 2)
    if pos < nec:
        mot.append(f"B6: estabilidad: {pos} de {len(en_reglas)} variantes dentro de reglas con R fuera > 0 (hacen "
                   f"falta {nec})")
    solo_l = [r["R_f"] for r in dfu if r["d"] > 0]
    info = {"sin_xperps": out["estres"]["sin_xperps"]["R"], "objetivo_2pb": out["estres"]["objetivo_superado_2pb"]["R"],
            "solo_largos": round(float(np.mean(solo_l)), 4) if solo_l else None,
            "por_moneda": medidas.por(dfu, lambda r: r["coin"]),
            "por_ventana": medidas.por(dfu, lambda r: str(r.get("ventana"))),
            "por_ano": medidas.por(ops, lambda r: r["dia"][:4]), "por_trimestre": medidas.por(ops, lambda r: medidas.trimestre(r["t"])),
            "colas": medidas.colas(dfu), "vetos": dict(med.vetos)}
    return {"vid": v["vid"], "B1": out["B1"], "B2": _r4(R_sin5), "B3": {"R": _r4(R_sinsem), "semana": mejor_sem},
            "estres": out["estres"], "estabilidad": f"{pos} de {len(en_reglas)}", "B6_pasa": pos >= nec,
            "motivos": mot, "pasa": not mot, "info": info, "hermanas": out["hermanas"],
            "operaciones": {"dentro": libro.compacta([r for r in ops if r["t"] < corte])[-200:],
                            "fuera": libro.compacta(dfu)[-200:]},
            "ops_lite": _ops_lite(dfu)}


# ---------------------------------------------------------------- etapa 4: prueba en avance virtual

def avance(tr: dict, punto: dict | None) -> dict:
    """Las variantes en avance sobre las velas que van llegando: posiciones desde 8 días antes de su inicio
    (calentamiento: las que ya estarían abiertas no cuentan) hasta ahora. Una operación es «completa» cuando su
    entrada es de hace más que su horizonte máximo."""
    ahora = tr.get("ahora") or (ajustes.ahora_ms() // MIN) * MIN
    vs = tr["variantes"]
    if not vs:
        return {"variantes": {}}
    cal = max(calentamiento_d(v) for v in vs)   # el régimen cripto de la skill pide 420 cierres diarios
    desde_min = min(int(v["avance_desde"]) for v in vs)
    m = datos.mercado_vivo(desde_min - (cal + 10) * DAY, ahora) if not tr.get("cache") else _mercado(tr)
    D = libro.Datos(m)
    out = {}
    for v in vs:
        a = int(v["avance_desde"])
        # Las señales de cada variante, con SU ventana de datos (su inicio menos su calentamiento): no cambian porque
        # entre o salga otra variante del avance (antes, la ventana empezaba en la más antigua de todas).
        mv = m.ventana(a - (calentamiento_d(v) + 10) * DAY, None)
        s = _senales(mv, v, a - 8 * DAY, ahora)
        med = _medir(m, s, v, a - 8 * DAY, ahora, D=D)
        hor = salidas.horizonte_h(salidas.normalizar(v["salida"])[0]) * H
        filas = []
        for r in med.operaciones + med.abiertas:
            filas.append({"id": f"{v['vid']}:{r['coin']}:{r['t_fill']}", "variante": v["vid"], "moneda": r["coin"],
                          "lado": "largo" if r["d"] > 0 else "corto", "t_senal": int(r["t"]), "t_entrada": int(r["t_fill"]),
                          "t_salida": int(r["ext"]) if r["why"] != "fin de datos" else None,
                          "R": float(r["R"]), "R_f": float(r["R_f"]), "pct": float(r["N"] * (r["ret"] + r["fund_pct"]) / 100),
                          "motivo": r["why"], "completa": int(r["why"] != "fin de datos" and ahora - r["t_fill"] > hor),
                          "previa": int(r["t_fill"] < a), "cuerpo": {"sl": r["sl"], "N": r["N"], "dia": r["dia"]}})
        out[v["vid"]] = filas
        comprobar_pausa()
    return {"variantes": out, "ahora": ahora}


# ---------------------------------------------------------------- utilidades de las tareas

def ruta(tr_id: str, ext: str) -> Path:
    return ajustes.TRABAJOS / f"{tr_id}.{ext}"


def guardar_punto(tr: dict, punto: dict) -> None:
    p = ruta(tr["id"], "punto.json")
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_redondea(punto), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def leer_punto(tr_id: str) -> dict | None:
    p = ruta(tr_id, "punto.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


ETAPAS = {"admision": admision, "rapida": rapida, "completa": completa, "robustez": robustez, "avance": avance}


def ejecutar(tr: dict) -> dict:
    """Corre un trabajo y devuelve su resultado (lo escribe la orden «tarea»)."""
    tipo = tr["tipo"]
    if tipo == "datos":
        fu = datos.preparar_epoca(tr["epoca"], forzar=bool(tr.get("forzar")))
        return {"carpeta": str(fu.carpeta), "firma": fu.firma, "periodo": fu.periodo}
    if tipo == "canario":
        from . import canario
        return canario.correr()
    if tipo not in ETAPAS:
        raise ValueError(f"tipo de trabajo desconocido: {tipo}")
    if tipo != "avance" or tr.get("cache"):
        car = Path(tr["cache"])
        if not (car / "meta.json").exists():
            raise FileNotFoundError(f"falta la caché de la época ({car})")
    from .familias import firmas
    fs = firmas()
    for v in tr.get("variantes") or ([tr["variante"]] if tr.get("variante") else []):
        f = fs.get(v["familia"])
        if f is None:
            raise RuntimeError(f"la familia {v['familia']} no se puede cargar")
        if v.get("familia_sha") and f["sha"] != v["familia_sha"]:
            raise RuntimeError(f"familia cambiada sin subir versión ({v['familia']} v{f['version']})")
    return ETAPAS[tipo](tr, leer_punto(tr["id"]))
