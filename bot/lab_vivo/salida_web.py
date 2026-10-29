"""Salidas del laboratorio vivo para la web y para el generador (docs/07, apartado 13), en M:\\BotFundamental-datos\\
lab_vivo. Todas se escriben de forma atómica (.tmp y os.replace); los números van como números y los textos, en español
con coma decimal. Cada archivo lleva formato: 1 y ts.

El servidor del bot NO importa bot.lab_vivo: solo lee estos archivos (14.1)."""
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

from . import ajustes, almacen, criterios, medidas

ID_RE = re.compile(r"^h-\d{8}-\d{4}-[a-z0-9_-]{3,60}$")
DIA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Lo que el agente generador NO lee (llevan el detalle del fuera de muestra y de cada tarjeta): docs/07, 15.1
NO_LEER_GENERADOR = ["estado.json", "mejores.json", "hipotesis.json", "hipotesis\\", "propuestas\\", "trabajos\\",
                     "lab_vivo.db", "primera_tanda.md"]
LO_QUE_NO = [
    "E2 (impulso de 15 min con filtros): descartada por el usuario el 29-10-2026",
    "Entrar en las 4 monedas en cada apertura sin filtro («6 diarias»): pierde",
    "Medios de cripto como señal de sentido: no anticipan (docs/05, sección 22)",
    "Objetivos < 2R o < 3 % neto, stops < 1,5 %, break-even, cierres a las 4 h",
    "Efectos de pocos pb (estacionalidad horaria, desfases de minutos): no pagan 0,24 % de costes con objetivos ≥ 3 %",
    "GDELT y ratio largo/corto como historia: no hay datos",
]


def _limpio(x):
    if isinstance(x, float):
        return round(x, 6) if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _limpio(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_limpio(v) for v in x]
    try:
        import numpy as np
        if isinstance(x, np.floating):
            return _limpio(float(x))
        if isinstance(x, np.integer):
            return int(x)
    except ImportError:
        pass
    return x


def escribir(ruta: Path, d: dict) -> None:
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    d = {"formato": 1, "ts": ajustes.ahora_ms(), **d}
    tmp = ruta.with_name(ruta.name + ".tmp")
    tmp.write_text(json.dumps(_limpio(d), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, ruta)


def leer(ruta: Path) -> dict | None:
    try:
        return json.loads(Path(ruta).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _n(x, d=2) -> str:
    return "–" if x is None else f"{x:.{d}f}".replace(".", ",")


# ---------------------------------------------------------------- reglas del usuario: el cierre por tiempo

def excepcion_tiempo(salida: dict | None, cierres: dict | None = None) -> dict | None:
    """Toda salida del catálogo (A, H, C y OBJ) cierra por tiempo ANTES de la ganancia mínima (max_hold_h sin activar,
    o el fin_ms de la señal): esa parte puede cerrar entre −1R y +2R, y una que gana, por debajo de 2R y del 3 % neto.
    Rompe «o gano más de 2 o pierdo 1»: es la excepción que el usuario solo ha aceptado en «6 diarias» (CLAUDE.md).
    Lo que cierra por tiempo con el cerrojo activado sí cumple (sale ≥ su suelo). Devuelve el aviso, con lo medido fuera
    (cierres: medidas.cierres_por_tiempo), o None si la salida no cierra por tiempo antes del cerrojo."""
    s = dict(salida or {})
    if s.get("tipo") in (None, "INFO"):
        return None
    tope = s.get("max_hold_h")
    out = {"tope_sin_activar_h": tope, "fuera": cierres or None,
           "texto": (f"Cierra por tiempo a las {_n(float(tope), 0) if tope else '?'} h si no ha llegado a la ganancia "
                     "mínima: esa parte puede salir entre −1R y +2R (y si gana, con menos de 2R y del 3 % neto). Es la "
                     "excepción que solo has aceptado en «6 diarias»: para operarla tendrías que aceptarla también aquí.")}
    if cierres and cierres.get("n"):
        out["texto"] += (f" Fuera de muestra cerraron así {cierres['n']} de {cierres['de']} operaciones "
                         f"({_n(cierres['pct'], 0)} %): {cierres['entre']} entre −1R y +2R y {cierres['ganan_menos_2R']} "
                         "ganando menos de 2R.")
    return out


# ---------------------------------------------------------------- tarjeta de una variante (13.3)

def tarjeta(con, vid: str, completa: bool = True) -> dict | None:
    v = almacen.fila_dict(con.execute("SELECT * FROM variantes WHERE id = ?", (vid,)).fetchone())
    if not v:
        return None
    h = almacen.fila_dict(con.execute("SELECT * FROM hipotesis WHERE id = ?", (v["hipotesis"],)).fetchone()) or {}
    pr = {r["etapa"]: almacen.fila_dict(r) for r in con.execute(
        "SELECT * FROM pruebas WHERE variante = ? ORDER BY id", (vid,))}
    rap, com, rob = pr.get("rapida"), pr.get("completa"), pr.get("robustez")
    mc = (com or {}).get("metricas") or {}
    fu = mc.get("fuera") or {}
    de = mc.get("dentro") or ((rap or {}).get("metricas") or {}).get("dentro") or {}
    mr = (rob or {}).get("metricas") or {}
    try:
        from .familias import obtener
        regla = obtener(h.get("familia")).describir(v["params"])
    except Exception:
        regla = h.get("titulo")
    cons = almacen.kv_get(con, "consolidacion") or {}
    qd = (cons.get("q") or {}).get(vid)
    dsr = (cons.get("dsr") or {}).get(vid)
    op_dia = fu.get("op_dia") if fu else de.get("op_dia")
    R_dia = fu.get("R_dia") if fu else None
    ic = fu.get("ic95")
    out = {"id": vid, "hipotesis": v["hipotesis"], "titulo": h.get("titulo"), "familia": h.get("familia"),
           "regla": regla, "clase": v["clase"], "etapa": v.get("etapa"), "cumple_reglas": bool(v["cumple_reglas"]),
           "fuera_de_reglas": v["clase"] == "fuera_de_reglas" or not v["cumple_reglas"],
           "frecuencia": v.get("frecuencia"), "op_dia": op_dia,
           "dentro": {"n": (rap or {}).get("n_dentro") if rap else de.get("n"), "R": (rap or {}).get("R_dentro"),
                      "ic95": de.get("ic95")},
           "fuera": ({"n": fu.get("n"), "R": fu.get("R"), "ic95": ic, "p": (com or {}).get("p_fuera"), "q": qd,
                      "dsr": dsr, "mitades": mc.get("mitades")} if com else None),
           "avance_trimestral": ({"R": (mc.get("k4") or {}).get("R"), "positivos": (mc.get("k4") or {}).get("positivos")}
                                 if mc.get("k4") else None),
           "robustez": ({"base_p": (mr.get("B1") or {}).get("p"), "sin_5_mayores": mr.get("B2"),
                         "sin_mejor_semana": (mr.get("B3") or {}).get("R"),
                         "desliz_x2": ((mr.get("estres") or {}).get("desliz_x2_y_stops") or {}).get("R"),
                         "entrada_1_min": ((mr.get("estres") or {}).get("entrada_1_min_tarde") or {}).get("R"),
                         "estabilidad": mr.get("estabilidad"), "sin_xperps": (mr.get("info") or {}).get("sin_xperps"),
                         "objetivo_2pb": (mr.get("info") or {}).get("objetivo_2pb"),
                         "solo_largos": (mr.get("info") or {}).get("solo_largos")} if rob else None),
           "cuenta": ({"R_dia": R_dia, "pct_dia": fu.get("pct_dia"), "pct_dia_compuesto": fu.get("pct_dia_compuesto"),
                       "caida_max_pct": fu.get("caida_max_pct")} if fu else None),
           "meta": medidas.meta(R_dia, fu.get("pct_dia_compuesto")) if fu else medidas.meta(None),
           "motivos": v.get("motivos") or [], "puntuacion": ic[0] if ic else None}
    fdr = out["fuera_de_reglas"]
    out["reglas"] = {"cumple": not fdr,
                     "excepcion_tiempo": None if fdr else excepcion_tiempo(v.get("salida"), fu.get("cierres_por_tiempo"))}
    av = avance_resumen(con, vid, v.get("avance_desde"))
    if av:
        out["avance"] = av if completa else {k: x for k, x in av.items() if k != "curva"}
    if completa:
        out["checks"] = checks(pr)
    return out


def checks(pr: dict) -> dict:
    out = {}
    for et in ("rapida", "completa", "robustez"):
        p = pr.get(et)
        if not p:
            continue
        mot = p.get("motivos") or []
        cods = {m.split(":")[0] for m in mot if isinstance(m, str)}
        for c in {"rapida": ("R1", "R2"), "completa": ("K1", "K2", "K3", "K4", "K5"),
                  "robustez": ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8")}[et]:
            out[c] = c not in cods
    return out


def avance_resumen(con, vid: str, desde: int | None) -> dict | None:
    if not desde:
        return None
    rows = [dict(r) for r in con.execute("SELECT * FROM avance WHERE variante = ? AND previa = 0 ORDER BY t_entrada",
                                         (vid,))]
    comp = [r for r in rows if r["completa"]]
    abiertas = [r for r in rows if not r["completa"]]
    ca = criterios.CRITERIOS["avance"]
    dias = max(0, (ajustes.ahora_ms() - int(desde)) // 86_400_000)
    acc, curva = 0.0, []
    e, pk, dd = 100.0, 100.0, 0.0
    for r in sorted(comp, key=lambda r: r["t_salida"] or 0):
        acc += r["R_f"]
        curva.append([int(r["t_salida"] or r["t_entrada"]), round(acc, 3)])
        e += r["pct"] or 0.0
        pk = max(pk, e)
        dd = max(dd, (pk - e) / pk * 100)
    faltan = []
    if len(comp) < ca["min_ops"]:
        faltan.append(f"{ca['min_ops'] - len(comp)} operaciones")
    if dias < ca["min_dias"]:
        faltan.append(f"{ca['min_dias'] - dias} días")
    cons = almacen.kv_get(con, "consolidacion") or {}
    return {"desde": int(desde), "completas": len(comp), "abiertas": len(abiertas),
            "R_medio": round(sum(r["R_f"] for r in comp) / len(comp), 4) if comp else None,
            "p_holm": (cons.get("p_holm") or {}).get(vid), "caida_max_pct": round(dd, 2),
            "faltan": " y ".join(faltan) if faltan else "nada: se juzga con V1 a V5", "curva": curva[-300:]}


# ---------------------------------------------------------------- estado.json (13.1)

def contadores(con) -> dict:
    hoy = ajustes.dia_mad(ajustes.ahora_ms())
    tot = con.execute("SELECT COUNT(*) FROM hipotesis").fetchone()[0]
    hoy_n = sum(1 for r in con.execute("SELECT recibida FROM hipotesis") if ajustes.dia_mad(r[0]) == hoy)
    en_cola = con.execute("SELECT COUNT(*) FROM hipotesis WHERE estado IN ('admitida', 'en_admision', "
                          "'lista_para_rapida')").fetchone()[0]
    cons = almacen.kv_get(con, "consolidacion") or {}
    mot = {}
    for r in con.execute("SELECT motivos FROM variantes WHERE clase LIKE 'descartada%'"):
        try:
            for m in json.loads(r[0] or "[]"):
                c = str(m).split(":")[0]
                if re.match(r"^[ARKBV]\d+$", c):
                    mot[c] = mot.get(c, 0) + 1
        except (TypeError, ValueError):
            pass
    return {"hipotesis": {"total": tot, "hoy": hoy_n, "en_cola": en_cola,
                          "cupo_hoy": ajustes.CUPO_DIA},
            "n": {"total": almacen.n_total(con), "fuera": almacen.n_fuera(con), "efectivo": cons.get("n_efectivo"),
                  "previo": almacen.n_previo(con), "dsr": cons.get("n_dsr")},
            "descartes_por_motivo": dict(sorted(mot.items()))}


def embudo(con) -> list[dict]:
    q = lambda s, *a: con.execute(s, a).fetchone()[0]   # noqa: E731
    return [{"etapa": "recibidas", "n": q("SELECT COUNT(*) FROM hipotesis")},
            {"etapa": "admitidas", "n": q("SELECT COUNT(*) FROM hipotesis WHERE estado NOT IN ('rechazada')")},
            {"etapa": "pasan_rapida", "n": q("SELECT COUNT(DISTINCT variante) FROM pruebas WHERE etapa='rapida' AND pasa=1")},
            {"etapa": "pasan_completa", "n": q("SELECT COUNT(DISTINCT variante) FROM pruebas WHERE etapa='completa' AND pasa=1")},
            {"etapa": "candidatas", "n": q("SELECT COUNT(*) FROM variantes WHERE clase IN ('candidata', 'en_avance', "
                                           "'candidata_hermana', 'lista', 'propuesta', 'aceptada', 'revocada')")},
            {"etapa": "en_avance", "n": q("SELECT COUNT(*) FROM variantes WHERE clase IN ('en_avance', 'revocada') "
                                          "AND avance_desde IS NOT NULL")},
            {"etapa": "listas", "n": q("SELECT COUNT(*) FROM variantes WHERE clase IN ('lista', 'propuesta', 'aceptada')")}]


def mejores(con, n: int = 20) -> list[dict]:
    """Las mejores por el límite inferior del IC 95 % del R medio fuera (no por el R medio a secas, que premia la
    suerte), con su motivo si no han pasado."""
    ids = [r[0] for r in con.execute("SELECT DISTINCT variante FROM pruebas WHERE etapa = 'completa'")]
    tj = [t for t in (tarjeta(con, i, completa=False) for i in ids) if t and t.get("fuera")]
    tj.sort(key=lambda t: -(t["puntuacion"] if t["puntuacion"] is not None else -9))
    for t in tj:
        if t["clase"].startswith("descartada") and t["motivos"]:
            t["no_ha_pasado"] = "no ha pasado: " + str(t["motivos"][0])
    return tj[:n]


def meta_global(con, mejores_50: list[dict] | None = None) -> dict:
    """Lo más cerca que ha estado algo de la meta (fuera de muestra o en avance), con su clase: una descartada no
    vale aunque su cifra parezca buena (la mejor de muchas siempre lo parece)."""
    best = None
    for t in (mejores(con, 50) if mejores_50 is None else mejores_50):
        rd = (t.get("cuenta") or {}).get("R_dia")
        if rd is not None and (best is None or rd > best[0]):
            best = (rd, t["id"], "fuera", t["clase"], (t.get("motivos") or [None])[0])
    for r in con.execute("SELECT id, avance_desde, clase FROM variantes WHERE avance_desde IS NOT NULL"):
        av = avance_resumen(con, r[0], r[1])
        if av and av["completas"]:
            dias = max(1, (ajustes.ahora_ms() - r[1]) // 86_400_000)
            rd = (av["R_medio"] or 0) * av["completas"] / dias
            if best is None or rd > best[0]:
                best = (rd, r[0], "avance", r[2], None)
    mt = medidas.meta(best[0] if best else None)
    texto = mt["texto"]
    if best:
        estado = (f"{best[3].replace('_', ' ')}" + (f": {best[4]}" if best[4] else ""))
        texto = (f"Lo más cerca que ha estado algo de la meta ({best[1]}, {best[2]} de muestra" if best[2] == "fuera"
                 else f"Lo más cerca que ha estado algo de la meta ({best[1]}, en avance") + f"; {estado}): " + mt["texto"]
    return {"objetivo_pct_dia": medidas.META_PCT, "riesgo_pct": medidas.RISK, "objetivo_R_dia": medidas.META_R,
            "mejor": ({"variante": best[1], "R_dia": round(best[0], 4), "pct_dia": round(best[0] * medidas.RISK, 4),
                       "tramo": best[2], "clase": best[3]} if best else None),
            "falta_R_dia": mt["falta_R_dia"], "falta_pct_dia": mt["falta_pct_dia"], "texto": texto,
            "nota": "con 0,5 % de riesgo por operación (no se sube)"}


def pesado(con) -> dict:
    """Lo que cuesta de estado.json: las tarjetas (mejores, en avance y listas), el embudo, los contadores, la meta y la
    consolidación. El corredor lo recalcula solo cuando cambia algo (o cada 10 min) y NUNCA en una pausa de las
    aperturas: entonces reutiliza el último (11.5: en la pausa, CPU del laboratorio ≈ 0)."""
    cons = almacen.kv_get(con, "consolidacion") or {}
    can = almacen.kv_get(con, "canario") or {"estado": "pendiente"}
    m50 = mejores(con, 50)
    en_av = [t for t in (tarjeta(con, r[0], completa=False) for r in con.execute(
        "SELECT id FROM variantes WHERE avance_desde IS NOT NULL AND clase IN ('en_avance', 'revocada')")) if t]
    listas = [t for t in (tarjeta(con, r[0], completa=False) for r in con.execute(
        "SELECT id FROM variantes WHERE clase IN ('lista', 'propuesta')")) if t]
    return {"canario": {"estado": can.get("estado"), "ts": can.get("ts"), "libro": can.get("libro"),
                        "comisiones": (can.get("comisiones") or {}).get("ok")},
            "contadores": contadores(con), "embudo": embudo(con), "meta": meta_global(con, m50),
            "mejores": m50[:10], "mejores_20": m50[:20], "en_avance": en_av[:10], "listas": listas[:10],
            "consolidacion": {"ts": cons.get("ts"), "umbral_dsr_z": cons.get("umbral_z"), "bh_m": cons.get("bh_m"),
                              "n_dsr": cons.get("n_dsr"), "aviso_n": cons.get("aviso_n"),
                              "cuenta_honesta": cons.get("cuenta_honesta") or {"trimestres": [], "R_medio": None}},
            "ts": ajustes.ahora_ms()}


def estado(con, runtime: dict, pesado_: dict | None = None) -> dict:
    pz = pesado_ or pesado(con)
    d = {"latido": ajustes.ahora_ms(), "version": runtime.get("version"), "pid": runtime.get("pid"),
         "arranque": runtime.get("arranque"), "estado": runtime.get("estado", "activo"), "motivo": runtime.get("motivo"),
         "aviso": ajustes.AVISO, "proxima_pausa": runtime.get("proxima_pausa"), "agenda": runtime.get("agenda_origen"),
         "recursos": runtime.get("recursos") or {}, "datos": runtime.get("datos") or {},
         "codigo": {"firma": runtime.get("firma_codigo"), "canario": pz["canario"]},
         "criterios": criterios.info(), "contadores": pz["contadores"], "embudo": pz["embudo"], "meta": pz["meta"],
         "en_curso": runtime.get("en_curso") or [], "cola": runtime.get("cola") or {},
         "mejores": pz["mejores"], "en_avance": pz["en_avance"], "listas": pz["listas"],
         "consolidacion": pz["consolidacion"], "tarjetas_ts": pz.get("ts"),
         "diario_hoy": {"dia": ajustes.dia_mad(ajustes.ahora_ms()), "lineas": (runtime.get("diario_lineas") or [])[-20:]},
         "errores": runtime.get("errores") or []}
    return d


def escribir_estado(con, runtime: dict, pesado_: dict | None = None) -> dict:
    d = estado(con, runtime, pesado_)
    escribir(ajustes.ESTADO, d)
    escribir(ajustes.DIR / "para_el_generador_estado.json", estado_generador(con, d))
    return d


def estado_generador(con, est: dict) -> dict:
    """Lo único del estado que lee el agente generador (15.2): si toca proponer y el cupo de hoy. Sin tarjetas ni cifras
    del fuera de muestra (estado.json las lleva: el generador no lo lee)."""
    hoy = ajustes.dia_mad(ajustes.ahora_ms())
    hoy_n = sum(1 for r in con.execute("SELECT recibida FROM hipotesis WHERE estado != 'rechazada'")
                if ajustes.dia_mad(r[0]) == hoy)
    return {"estado": est.get("estado"), "motivo": est.get("motivo"), "latido": est.get("latido"),
            "canario": (est.get("codigo") or {}).get("canario", {}).get("estado"),
            "cupo": {"pasada": ajustes.CUPO_PASADA, "hoy_libres": max(0, ajustes.CUPO_DIA - hoy_n),
                     "por_familia_max": ajustes.CUPO_FAMILIA},
            "proxima_pausa": est.get("proxima_pausa")}


# ---------------------------------------------------------------- diario (13.2)

def diario(con, dia: str, extra: dict | None = None) -> dict:
    a = int(datetime.fromisoformat(dia).replace(tzinfo=ajustes.MAD).timestamp() * 1000)
    b = a + 86_400_000
    hs = [almacen.fila_dict(r) for r in con.execute("SELECT * FROM hipotesis WHERE recibida >= ? AND recibida < ?", (a, b))]
    rech = [{"id": h["id"], "motivos": (h.get("admision") or {}).get("motivos", [])[:5]} for h in hs
            if h["estado"] == "rechazada"]
    cnt = lambda et: con.execute("SELECT COUNT(*) FROM pruebas WHERE etapa = ? AND ts >= ? AND ts < ?", (et, a, b)).fetchone()[0]   # noqa: E731
    ev = [dict(r) for r in con.execute("SELECT * FROM eventos WHERE ts >= ? AND ts < ? ORDER BY id", (a, b))]
    cand = [e["sujeto"] for e in ev if e["tipo"] == "candidata"]
    rev = [e["sujeto"] for e in ev if e["tipo"] == "revocada"]
    lst = [e["sujeto"] for e in ev if e["tipo"] == "lista"]
    ext = extra or {}
    n_var = cnt("rapida")
    texto = [f"Hoy: {len(hs) - len(rech)} hipótesis admitidas de {len(hs)} recibidas, {n_var} variantes medidas en la "
             f"etapa rápida; {'ninguna candidata nueva' if not cand else f'{len(cand)} candidatas nuevas'}."]
    texto += [e["texto"] for e in ev if e["tipo"] in ("lección", "canario", "época", "error", "revocada", "lista")][-15:]
    return {"dia": dia, "hipotesis": {"recibidas": len(hs), "admitidas": len(hs) - len(rech), "rechazadas": rech},
            "variantes": {"rapida": n_var, "completa": cnt("completa"), "robustez": cnt("robustez"),
                          "n_fin_del_dia": almacen.n_total(con)},
            "candidatas_nuevas": cand, "revocadas": rev, "listas_nuevas": lst, "avance": ext.get("avance") or [],
            "pausas": ext.get("pausas") or [], "cpu_horas": ext.get("cpu_horas"), "errores": sum(1 for e in ev if e["tipo"] == "error"),
            "lecciones": ext.get("lecciones") or [], "generador": generador_notas(dia), "texto": texto}


def generador_notas(dia: str) -> list[str]:
    out = []
    car = ajustes.GENERADOR
    if not car.exists():
        return out
    for p in sorted(car.glob(f"{dia}-*.json")):
        g = leer(p) or {}
        n = len(g.get("propuestas") or g.get("hipotesis") or [])
        out.append(f"{p.stem}: {n} hipótesis" + (f" ({g.get('resumen')})" if g.get("resumen") else ""))
    return out


def escribir_diario(con, dia: str, extra: dict | None = None) -> dict:
    d = diario(con, dia, extra)
    escribir(ajustes.DIARIO / f"{dia}.json", d)
    return d


# ---------------------------------------------------------------- cola, hipótesis y generador (13.4 y 13.5)

def escribir_cola(con, en_curso: list[dict]) -> dict:
    pend = [dict(r) for r in con.execute("SELECT id, tipo, objetivo, prioridad, estado, intentos, creado, error "
                                         "FROM trabajos WHERE estado IN ('pendiente', 'en_curso', 'fallido') "
                                         "ORDER BY prioridad, creado")]
    por_tipo = {}
    for r in pend:
        if r["estado"] == "pendiente":
            por_tipo[r["tipo"]] = por_tipo.get(r["tipo"], 0) + 1
    hace = ajustes.ahora_ms() - 86_400_000
    d = {"pendientes": sum(1 for r in pend if r["estado"] == "pendiente"), "por_tipo": por_tipo, "en_curso": en_curso,
         "fallidos_24h": [r for r in pend if r["estado"] == "fallido" and (r["creado"] or 0) >= hace],
         "siguientes": [{"trabajo": r["id"], "tipo": r["tipo"], "objetivo": r["objetivo"], "prioridad": r["prioridad"]}
                        for r in pend if r["estado"] == "pendiente"][:50]}
    escribir(ajustes.DIR / "cola.json", d)
    return d


def escribir_hipotesis(con) -> None:
    filas = []
    for h in con.execute("SELECT * FROM hipotesis ORDER BY recibida DESC LIMIT 1000"):
        h = almacen.fila_dict(h)
        vs = [almacen.fila_dict(v) for v in con.execute("SELECT * FROM variantes WHERE hipotesis = ?", (h["id"],))]
        orden = ["lista", "propuesta", "aceptada", "en_avance", "candidata", "revocada", "candidata_hermana",
                 "descartada_avance", "descartada_robustez", "descartada_completa", "descartada_rapida",
                 "fuera_de_reglas", "pendiente", "duplicada", "rechazada", "error", "invalidada", "descartada_usuario"]
        mejor = min((v["clase"] for v in vs), key=lambda c: orden.index(c) if c in orden else 99, default=None)
        motivo = next((m for v in vs for m in (v.get("motivos") or [])), None)
        filas.append({"id": h["id"], "titulo": h["titulo"], "familia": h["familia"], "autor": h["autor"],
                      "recibida": h["recibida"], "estado": h["estado"], "mejor_clase": mejor, "motivo": motivo,
                      "variantes": len(vs)})
        det = {"hipotesis": h["cuerpo"], "admision": h.get("admision"), "estado": h["estado"],
               "variantes": [{"id": v["id"], "tarjeta": tarjeta(con, v["id"]),
                              "pruebas": [_prueba_lite(almacen.fila_dict(p)) for p in con.execute(
                                  "SELECT * FROM pruebas WHERE variante = ? ORDER BY id", (v["id"],))],
                              "operaciones": _ops_de(con, v["id"])} for v in vs]}
        escribir(ajustes.HIPOTESIS_DIR / f"{h['id']}.json", det)
    escribir(ajustes.DIR / "hipotesis.json", {"hipotesis": filas})


def _prueba_lite(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "etapa", "ts", "epoca", "cuenta_en_n", "n_dentro", "n_fuera", "R_dentro",
                                  "R_fuera", "p_fuera", "sr_fuera", "pasa", "motivos")}


def _ops_de(con, vid: str) -> dict:
    out = {}
    for tramo in ("fuera", "dentro"):
        rows = [json.loads(r[0]) for r in con.execute(
            "SELECT cuerpo FROM operaciones WHERE variante = ? AND tramo = ? ORDER BY k", (vid, tramo))]
        if rows:
            out[tramo] = rows[-200:]
    av = [dict(r) for r in con.execute("SELECT * FROM avance WHERE variante = ? ORDER BY t_entrada", (vid,))]
    if av:
        out["avance"] = av
    return out


# Lo que puede saber el generador de un descarte: la etapa y el código, con las cifras DENTRO de muestra (admisión,
# A; rápida, R). Del fuera de muestra (K, B y V) solo el código y su texto sin cifras: con el detalle (por moneda, año,
# trimestre, solo largos…) propondría el recorte que mejor salió fuera, sobre los mismos 411 días (docs/07, 15.1).
TEXTO_CODIGO = {
    "K1": "pocas operaciones o semanas fuera de muestra", "K2": "R medio fuera de muestra ≤ 0",
    "K3": "alguna mitad del fuera de muestra con R medio ≤ 0", "K4": "no pasa el avance trimestral de la hipótesis",
    "K5": "menos de 0,25 operaciones al día fuera de muestra", "B1": "no bate a su base emparejada",
    "B2": "sin sus 5 mayores operaciones no gana", "B3": "sin su mejor semana no gana",
    "B4": "no aguanta el estrés de deslizamiento y stops", "B5": "no aguanta la entrada 1 min tarde",
    "B6": "inestable: menos de la mitad de sus hermanas ganan fuera de muestra",
    "B7": "no pasa Benjamini-Hochberg con el número de pruebas acumulado",
    "B8": "no pasa el Sharpe deflactado con el número de pruebas acumulado", "V1": "revocada con el N de hoy",
}


def motivo_para_generador(m) -> str | None:
    """El motivo tal cual si es de la admisión o de la etapa rápida (dentro de muestra); si es del fuera de muestra o
    del avance, solo su código y un texto sin cifras."""
    if m is None:
        return None
    m = str(m)
    cod = m.split(":")[0].strip()
    if re.match(r"^[AR]\d+$", cod):
        return m
    if re.match(r"^[KBV]\d+$", cod):
        return f"{cod}: {TEXTO_CODIGO.get(cod, 'no pasa')}"
    if m.startswith("en avance"):
        return "en avance: descartada"
    if m.startswith("fuera de reglas"):
        return "fuera de reglas: informativa"
    return cod[:40] if cod else None


def escribir_generador(con, estado_txt: str) -> None:
    """para_el_generador.json y ya_probado.json: lo que lee el agente generador. Nada del detalle del fuera de muestra:
    de cada variante, la etapa a la que llegó, el código del motivo y sus cifras DENTRO de muestra (etapa rápida)."""
    from .familias import cargar
    from . import salidas
    cons = almacen.kv_get(con, "consolidacion") or {}
    hoy = ajustes.dia_mad(ajustes.ahora_ms())
    por_fam = {}
    for r in con.execute("SELECT familia, recibida FROM hipotesis WHERE estado != 'rechazada'"):
        if ajustes.dia_mad(r[1]) == hoy:
            por_fam[r[0]] = por_fam.get(r[0], 0) + 1
    hoy_n = sum(por_fam.values())
    fams = []
    lec = almacen.kv_get(con, "lecciones") or {}
    for n, f in sorted(cargar().items()):
        fi = f.ficha()
        st = lec.get(n) or {}
        fi.update(probadas=st.get("probadas", 0), por_etapa=st.get("por_etapa", {}), fallo_mas_comun=st.get("fallo_mas_comun"))
        fams.append(fi)
    nd = cons.get("n_dsr") or (almacen.n_previo(con) and 381 + 1917)
    z = medidas.z_umbral(nd) if nd else None
    ya = {}
    rap = {r[0]: (r[1], r[2]) for r in con.execute("SELECT variante, n_dentro, R_dentro FROM pruebas WHERE etapa = 'rapida'")}
    for v in con.execute("SELECT v.id, v.firma, v.params, v.salida, v.clase, h.familia, v.etapa, v.motivos FROM variantes v "
                         "JOIN hipotesis h ON v.hipotesis = h.id"):
        try:
            mot = (json.loads(v[7] or "[]") or [None])[0]
        except ValueError:
            mot = None
        nd, rd = rap.get(v[0], (None, None))
        ya.setdefault(v[5], []).append({"variante": v[0], "firma": v[1], "params": json.loads(v[2]),
                                        "salida": json.loads(v[3]), "clase": v[4], "etapa": v[6],
                                        "motivo": motivo_para_generador(mot),
                                        "dentro": {"n": nd, "R": rd} if nd is not None else None})
    desc = []
    for r in con.execute("SELECT id, motivos FROM variantes WHERE clase LIKE 'descartada%' ORDER BY actualizada DESC "
                         "LIMIT 30"):
        try:
            desc.append({"id": r[0], "motivo": motivo_para_generador((json.loads(r[1] or "[]") or [None])[0])})
        except ValueError:
            desc.append({"id": r[0], "motivo": None})
    escribir(ajustes.DIR / "para_el_generador.json", {
        "estado": estado_txt, "criterios_version": criterios.VERSION,
        "reglas_usuario": {"stop_min_pct": 1.5, "rr_min": 2.0, "neto_min_pct": 3.0, "riesgo_pct": 0.5,
                           "max_entradas_dia": 6, "max_riesgo_dia_pct": 3.0, "largos": "contado", "cortos": "perpetuo",
                           "sin_break_even": True},
        "cupo": {"pasada": ajustes.CUPO_PASADA, "hoy_libres": max(0, ajustes.CUPO_DIA - hoy_n), "por_familia_hoy": por_fam,
                 "por_familia_max": ajustes.CUPO_FAMILIA},
        "familias": fams, "salidas": {t: {"texto": salidas.TEXTO[t], "parametros": {k: list(o) for k, (o, _) in
                                                                                    salidas.ESQ[t].items()}}
                                     for t in salidas.TIPOS},
        "datos": [{"clave": k, "historia": v[0], "texto": v[1]} for k, v in __import__(
            "bot.lab_vivo.hipotesis", fromlist=["DATOS"]).DATOS.items()],
        "umbral": {"n_dsr": nd, "z": round(z, 3) if z else None, "aviso_n": cons.get("aviso_n"),
                   "texto": (f"Con N efectivo ≈ {int(nd)}, una estrategia necesita un Sharpe diario ≈ "
                             f"{_n(z / math.sqrt(440), 2)} fuera de muestra" if z else "Sin consolidación aún")},
        "lecciones": [x.get("texto") for x in lec.values() if isinstance(x, dict) and x.get("texto")],
        "candidatas": [r[0] for r in con.execute("SELECT id FROM variantes WHERE clase IN ('candidata', 'en_avance')")],
        "en_avance": [r[0] for r in con.execute("SELECT id FROM variantes WHERE avance_desde IS NOT NULL")],
        "descartes_recientes": desc, "lo_que_no": LO_QUE_NO,
        "estado_al_dia": "para_el_generador_estado.json",
        "no_leer": NO_LEER_GENERADOR,
        "nota": ("De lo probado solo se da la etapa a la que llegó cada variante, el código del motivo y sus cifras dentro "
                 "de muestra. El detalle del fuera de muestra no se mira para proponer: una idea sacada de ahí se "
                 "eligió entre muchos cortes de los mismos 411 días y su fuera de muestra ya no vale como prueba.")})
    escribir(ajustes.DIR / "ya_probado.json", {"familias": ya})


# ---------------------------------------------------------------- propuestas y decisiones (13.6)

def escribir_propuesta(con, vid: str) -> dict:
    """La propuesta de una variante lista (13.6). Si la salida cierra por tiempo antes de la ganancia mínima, lleva la
    excepción («reglas.excepcion_tiempo») y para aceptarla la decisión tiene que traer acepta_excepcion_tiempo: true.
    Las propuestas pendientes anteriores de la misma variante se retiran (solo vale la última)."""
    from .hipotesis import a_params_motor
    t = tarjeta(con, vid)
    v = almacen.fila_dict(con.execute("SELECT * FROM variantes WHERE id = ?", (vid,)).fetchone())
    h = almacen.fila_dict(con.execute("SELECT * FROM hipotesis WHERE id = ?", (v["hipotesis"],)).fetchone())
    pm, txt = a_params_motor(h["familia"], v["params"], v["salida"])
    ahora = ajustes.ahora_ms()
    hora = datetime.fromtimestamp(ahora / 1000, ajustes.MAD).strftime("%H%M%S")
    pid = f"lv-{v['firma'][:12]}-{ajustes.dia_mad(ahora).replace('-', '')}-{hora}"   # una por propuesta: nunca se reutiliza
    retirar_propuestas(con, vid, f"sustituida por la propuesta {pid}", salvo=pid)
    reglas = (t or {}).get("reglas") or {"cumple": True, "excepcion_tiempo": excepcion_tiempo(v.get("salida"))}
    d = {"id": pid, "tipo": "estrategia_lab_vivo", "estado": "pendiente", "creada": ahora, "variante": vid,
         "hipotesis": h["id"], "titulo": h["titulo"], "familia": h["familia"], "regla": t.get("regla") if t else None,
         "criterios_version": criterios.VERSION, "tarjeta": t, "reglas": reglas,
         "requiere": ["acepta_excepcion_tiempo"] if reglas.get("excepcion_tiempo") else [],
         "como_operarla": {"motor": pm is not None, "params_motor": pm, "texto": txt},
         "aviso": ("Propuesta del laboratorio: no es una ventaja garantizada. Si la aceptas, pasa a la cartera paper "
                   "(dinero ficticio) cuando esté programada; nada se activa sin ti.")}
    escribir(ajustes.PROPUESTAS / f"{pid}.json", d)
    return d


def propuestas_de(vid: str) -> list[dict]:
    """Las propuestas de una variante (de la más vieja a la más reciente)."""
    out = []
    if not ajustes.PROPUESTAS.exists():
        return out
    for p in ajustes.PROPUESTAS.glob("*.json"):
        d = leer(p)
        if isinstance(d, dict) and d.get("variante") == vid and d.get("id"):
            out.append(d)
    return sorted(out, key=lambda d: (d.get("creada") or d.get("ts") or 0, d["id"]))


def ultima_propuesta(vid: str) -> str | None:
    ps = propuestas_de(vid)
    return ps[-1]["id"] if ps else None


def retirar_propuestas(con, vid: str, motivo: str, salvo: str | None = None) -> list[str]:
    """Las propuestas PENDIENTES de una variante pasan a «retirada» con su motivo (al revocarla, al descartarla en
    avance o al proponerla otra vez): una propuesta retirada no se puede aceptar."""
    hechas = []
    for d in propuestas_de(vid):
        if d.get("estado") != "pendiente" or d["id"] == salvo:
            continue
        d = {k: x for k, x in d.items() if k not in ("formato", "ts")}
        d.update(estado="retirada", motivo_retirada=motivo, retirada=ajustes.ahora_ms())
        escribir(ajustes.PROPUESTAS / f"{d['id']}.json", d)
        almacen.evento(con, "propuesta_retirada", f"Propuesta {d['id']} retirada: {motivo}", vid)
        hechas.append(d["id"])
    return hechas


def leer_decisiones() -> list[dict]:
    out = []
    car = ajustes.DECISIONES
    if not car.exists():
        return out
    for p in sorted(car.glob("*.json")):
        d = leer(p)
        if isinstance(d, dict) and d.get("decision") in ("aceptada", "descartada") and d.get("propuesta"):
            out.append({**d, "_archivo": p.name})
    return out
