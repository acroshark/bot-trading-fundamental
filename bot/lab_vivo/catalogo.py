"""Siembra del laboratorio vivo (docs/07, 6.7) y conversión del catálogo inicial del investigador al formato final.

- Siembra prerregistrada el 29-10-2026 (sin ver ningún resultado): la referencia (SEIS2 exacto, que no cuenta en N) y
  las de ruptura_canal, compresion_ruptura y tendencia_diaria de docs/07.
- Catálogo inicial: los 38 borradores de lab_vivo\\cola\\H01…H38 («lab_vivo.hipotesis/0.1») se convierten al formato 1.
  Sus reglas se respetan; lo que el formato final obliga a cambiar se dice en «nota» y se apunta en «eventos» (se hace
  ANTES de medir nada):
  · Salidas F3 / F2,5 / F2 → H (horquilla) con su objetivo y su tope; A y C con sus topes; OBJ → OBJ.
  · El suelo del cerrojo es el F del catálogo de salidas (≥ 2R y ≥ 3 % neto por operación) en vez de «2R con s ≥ 2 % y
    2,5R con s < 2 %»: deja ≥ 3 % neto con el perfil en uso, también en el estrés sin X-Perps (el suelo se recalcula).
  · El tope del stop estructural es el 10 % del borrador (sl_max 10).
  · H18 cierra por tiempo a las 00:00 UTC de d + 2 con el tope propio de la señal (fin_ms).
  · H29 (régimen como filtro de las madres) se parte en una hipótesis por madre, con el filtro común regimen_cripto.
  · H38 decide 15 min después del cierre de NYSE (la regla del laboratorio para los cierres de bolsa), no 5.
  · Pendientes (no se pueden medir con este libro o sin historia): H21 y H37 (dos patas: pares y carry), H33-H35
    (noticias en vivo: esperan la familia noticias_vivas de la fase 2) y H25-V6 (mide el «clavado», sin operación).

Uso: python -X utf8 -m bot.lab_vivo sembrar (escribe en cola\\entrada; las que ya están en lab_vivo.db no se repiten)."""
import json
import os
import re
from pathlib import Path

from . import ajustes
from .hipotesis import REGLAS_USUARIO

CREADA_CAT = "2026-10-29T11:30:00+01:00"
CREADA_SIEMBRA = "2026-10-29T00:00:00+01:00"
COINS = ["BTC", "ETH", "SOL", "XRP"]


def _salida(s: dict, stop: dict, sl_max: float = 10.0) -> dict:
    """Salida del borrador → catálogo final."""
    regla = str((stop or {}).get("regla") or "")
    fijo = re.search(r"s = (\d+(?:,\d+)?) % fijo", regla)
    sl = float(fijo.group(1).replace(",", ".")) if fijo else "estructural"
    t = s.get("tipo")
    if t in ("F3", "F2,5", "F2"):
        return {"tipo": "H", "sl": sl, "target_r": float(s.get("objetivo_R") or {"F3": 3, "F2,5": 2.5, "F2": 2}[t]),
                "max_hold_h": int(s.get("tope_h") or 48), **({"sl_max": sl_max} if sl == "estructural" else {})}
    if t == "A":
        hh = int(s.get("tope_h") or 72)
        return {"tipo": "A", "sl": sl, "trail_r": 2.0, "suelo": "F", "max_hold_h": hh,
                "max_hold_active_h": int(s.get("tope_cerrojo_h") or 168), **({"sl_max": sl_max} if sl == "estructural" else {})}
    if t == "C":
        hh = int(s.get("tope_h") or 168)
        return {"tipo": "C", "sl": sl, "lock_r": 3.0, "trail_r": float(s.get("seguimiento_R") or 2), "suelo": "F",
                "max_hold_h": hh, "max_hold_active_h": max(168, hh), **({"sl_max": sl_max} if sl == "estructural" else {})}
    if t == "OBJ":
        return {"tipo": "OBJ", "sl": sl, "max_hold_h": int(s.get("tope_h") or 120),
                **({"sl_max": sl_max} if sl == "estructural" else {})}
    if t == "INFO":
        return {"tipo": "INFO", "max_hold_h": 168}
    raise ValueError(f"salida desconocida {t}")


def _lados(pp: dict) -> dict:
    s = pp.get("solo")
    return {"lados": {"largos": "largos", "cortos": "cortos"}[s]} if s else {}


def _monedas(pp: dict, defecto=None) -> dict:
    if pp.get("monedas"):
        return {"monedas": list(pp["monedas"])}
    return {"monedas": list(defecto)} if defecto else {}


# hipótesis → (familia, momento, datos, función params(v) -> dict); la salida sale de _salida
MAPA = {
    "H01": ("momento_serie", "continuo", ["velas_1m"],
            lambda p: {"tf_min": 1440, "L": p["L"], "z_min": p["z_min"], "sigma_n": 60, "modo": "estado",
                       "k_atr": p["k_atr"], "atr_tf": 1440, **_lados(p)}),
    "H02": ("momento_serie", "continuo", ["velas_1m"],
            lambda p: {"tf_min": 240, "L": p["N"], "z_min": p["z_min"], "sigma_n": 180, "modo": "flanco",
                       "k_atr": 1.5, "atr_tf": 240, **_lados(p)}),
    "H03": ("ruptura_canal", "continuo", ["velas_1m"],
            lambda p: {"tf_min": 240, "horas": p["N"] * 4, "stop": "atr", "stop_atr_k": 2.0, "stop_atr_tf": 240,
                       **({"vol_x": p["vol_x"]} if p.get("vol_x") else {})}),
    "H04": ("donchian_conjunto", "continuo", ["velas_1m"],
            lambda p: {"u": p["u"], "k_atr": p["k_atr"], **_lados(p)}),
    "H05": ("precio_media", "continuo", ["velas_1m"],
            lambda p: {"N": p["N"], "banda": p["banda"], "k_atr": 1.0, **_monedas(p)}),
    "H06": ("keltner", "continuo", ["velas_1m"],
            lambda p: {"k": p["k"], "filtro_4h": p["filtro_4h"], "rvol": p["rvol"], "k_atr_stop": 2.0}),
    "H07": ("squeeze_bollinger", "continuo", ["velas_1m"],
            lambda p: {"p": float(p.get("p") or 10), "marco": 60 if p.get("marco") == "1h" else 240,
                       "compresion": "ttm" if p.get("compresion") == "ttm" else "bbw",
                       **({"keltner_k": p["keltner_k"]} if p.get("keltner_k") else {})}),
    "H08": ("ruptura_volatilidad", "continuo", ["velas_1m"],
            lambda p: {"k": p["k"] if isinstance(p["k"], float) else 0.5,
                       "k_modo": "ruido20" if p["k"] == "ruido20" else "fijo",
                       **({"filtro_sma20": True} if p.get("filtro") == "sma20" else {}), **_lados(p)}),
    "H09": ("rango_horario", "continuo", ["velas_1m", "aperturas"],
            lambda p: {"rango": "asia", "q": float(p["q"]) if p.get("q") else None, "stop_modo": "extremo",
                       **({"rvol15": p["rvol15"]} if p.get("rvol15") else {})}),
    "H10": ("fuerza_relativa", "continuo", ["velas_1m"],
            lambda p: {"modo": "momento", "L": p["L"], "ventaja": float(p.get("ventaja") or 0.0), "k_atr": 0.75,
                       **_lados(p)}),
    "H11": ("fuerza_relativa", "continuo", ["velas_1m"],
            lambda p: {"modo": "reversion", "z0": p["z0"], "dias_r": int(p.get("dias") or 1), "k_atr": 0.75,
                       **_lados(p)}),
    "H12": ("funding_extremo", "continuo", ["velas_1m", "funding"],
            lambda p: {"p_hi": p.get("p_hi"), "p_lo": p.get("p_lo"), "f_hi": p.get("f_hi", 0.0003),
                       "f_lo": p.get("f_lo", -0.0001), "confirmacion": p.get("confirmacion", True), **_lados(p)}),
    "H13": ("funding_ruptura", "continuo", ["velas_1m", "funding"],
            lambda p: {"p_hi": float(p.get("p_hi") or 90), "p_lo": float(p.get("p_lo") or 10), "velas": p["velas"],
                       **_lados(p)}),
    "H14": ("funding_hora", "continuo", ["velas_1m", "funding"],
            lambda p: {"f_min": p.get("f_min"), "c": p["c"], "salida_min": int(p.get("salida_min") or 0)}),
    "H15": ("extremo_1h", "continuo", ["velas_1m", "funding"],
            lambda p: {"modo": "cascada", "k": float(p["k"]), "vol_x": float(p["vol_x"]),
                       "estabilizacion": p["estabilizacion"], "funding_p": None, "lados": "largos",
                       **_monedas(p)}),
    "H16": ("extremo_1h", "continuo", ["velas_1m", "funding"],
            lambda p: {"modo": "euforia", "k": float(p["k"]), "vol_x": 3.0, "estabilizacion": True,
                       "funding_p": float(p["funding_p"]) if p.get("funding_p") else None, "lados": "cortos",
                       **_monedas(p)}),
    "H17": ("sesion_extrema", "continuo", ["velas_1m"],
            lambda p: {"c": p["c"], "modo": p["modo"], "lado": p["lado"]}),
    "H18": ("dia_anormal", "continuo", ["velas_1m"],
            lambda p: {"c": p["c"], "excepciones": bool(p.get("excepciones", False)),
                       "desde_h": int(p.get("desde_h") or 1)}),
    "H19": ("lider_btc", "continuo", ["velas_1m"],
            lambda p: ({"senal": "donchian4h_btc", "N": p["N"]} if p.get("senal") == "donchian4h_btc" else
                       {"senal": "impulso15", "u": p["u"], "solo_mayor_beta": bool(p.get("solo_mayor_beta", False))})
            | {"monedas": ["ETH", "SOL", "XRP"]}),
    "H20": ("cociente_alt_btc", "continuo", ["velas_1m"],
            lambda p: {"monedas": p["pares"], "z0": p["z0"], "ventana": p["ventana"],
                       "filtro_btc": p.get("filtro_btc", True), "k_atr": 0.75}),
    "H22": ("calendario", "continuo", ["velas_1m"],
            lambda p: {"modo": "hora", "hora": p["hora"], "dia": "viernes" if p.get("dias") else "todos",
                       "fin": "+2h" if p.get("salida_h") else "ninguno"}),
    "H23": ("rango_horario", "continuo", ["velas_1m"],
            lambda p: {"rango": "fin_de_semana", "disparo_h": int(p.get("ventana_h") or 24),
                       "desde": "viernes21" if p.get("rango_desde") else "sabado", "stop_modo": "extremo_atr",
                       **({"rvol15": p["rvol15"]} if p.get("rvol15") else {})}),
    "H24": ("fin_de_semana_nyse", "continuo", ["velas_1m", "aperturas"],
            lambda p: {"umbral": p["umbral"], "modo": p["modo"],
                       "decision": "domingo_cme" if p.get("decision") == "domingo_cme" else "nyse", **_monedas(p)}),
    "H25": ("rango_horario", "continuo", ["velas_1m"],
            lambda p: {"rango": "deribit", "vencimientos": p.get("vencimientos", "todos"), "stop_modo": "extremo_atr",
                       **_monedas(p)}),
    "H26": ("calendario", "continuo", ["velas_1m"],
            lambda p: {"modo": "cambio_de_mes", "desde": int(p["desde"]), "fin": "dia4" if p.get("hasta") else "ninguno",
                       **_monedas(p)}),
    "H27": ("reaccion_macro", "continuo", ["velas_1m", "macro"],
            lambda p: {"eventos": p["eventos"], "W": p["W"], "modo": p["modo"], **_monedas(p)}),
    "H28": ("calendario", "continuo", ["velas_1m", "macro"],
            lambda p: {"modo": "previa_macro", "eventos": p["eventos"], "antes_h": p["antes_h"]}),
    "H30": ("flujo_tomador", "continuo", ["velas_1m", "flujo_tomador"],
            lambda p: {"senal": p["senal"], "z0": p["z0"], "modo": p["modo"],
                       "confirmacion_r": p.get("confirmacion_r"), **_monedas(p)}),
    "H31": ("volumen_vela", "continuo", ["velas_1m"],
            lambda p: {"v": float(p["v"]), "ruptura_24h": bool(p.get("ruptura_24h", False)), **_monedas(p)}),
    "H32": ("rsi2_tendencia", "continuo", ["velas_1m"],
            lambda p: {"marco": {"4h": 240, "1h": 60, "1d": 1440}[p["marco"]], "lo": float(p["lo"]),
                       "hi": float(p.get("hi") or 90), "k_atr": 1.0 if p["marco"] == "1d" else 1.5,
                       "atr_tf": 1440 if p["marco"] == "1d" else 240, **_lados(p)}),
    "H36": ("media_hora", "continuo", ["velas_1m", "aperturas"],
            lambda p: {"sesion": p["sesion"], "z_min": float(p.get("z_min") or 0.0),
                       "hasta": "00:30 d+1" if p.get("hasta") else "fin"}),
    "H38": ("cierre_nyse", "continuo", ["velas_1m", "bolsas", "aperturas"],
            lambda p: {"u": p["u"], "modo": p["modo"], "rezagadas": bool(p.get("rezagadas", False)), "k_atr": 0.75,
                       **_lados(p)}),
}
CALENDARIO_DATOS = {"H22": ["velas_1m"], "H26": ["velas_1m"]}
PENDIENTES = {
    "H21": "dos patas (largo una moneda y corto la otra): el libro del paper opera una pata por operación",
    "H37": "dos patas (contado y perpetuo de la misma moneda): el libro del paper opera una pata por operación",
    "H33": "noticias en vivo: espera la familia noticias_vivas (fase 2) y su historia grabada",
    "H34": "noticias en vivo: espera la familia noticias_vivas (fase 2) y su historia grabada",
    "H35": "noticias en vivo: espera la familia noticias_vivas (fase 2) y su historia grabada",
}
VARIANTES_FUERA = {("H25", "H25-V6"): "mide el «clavado» al precio de ejercicio, sin operación: no es medible con el libro"}
MADRES_H29 = {   # H29: una hipótesis por madre, con el filtro común regimen_cripto
    "H03-V1": ("a", "ruptura_canal"), "H01-V2": ("b", "momento_serie"), "H08-V2": ("c", "ruptura_volatilidad"),
    "H15-V1": ("d", "extremo_1h"), "H09-V1": ("e", "rango_horario"),
}


def _tipo_fuente(ref: str) -> str:
    r = ref.lower()
    if any(k in r for k in ("wiley", "libro", "book", "rules", "secrets")):
        return "libro"
    if any(k in r for k in ("luxalgo", "wikidocs", "tradingview", "quantpedia", "concretum", "coinglass", "skill",
                            "deribit,", "macro hive", "k33", "presto", "amberdata", "block scholes", "cointelegraph",
                            "digital today")):
        return "estrategia_publica"
    return "articulo"


def _slug(x: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", x.lower()).strip("-")[:48]


def _logica(d: dict) -> dict:
    fu = [{"tipo": _tipo_fuente(f.get("ref", "")), "cita": f.get("ref", ""), "url": f.get("url"),
           "nota": f.get("dice")} for f in d.get("fuentes") or [] if f.get("ref")]
    fe = d.get("frecuencia_esperada") or {}
    pred = (f"{fe.get('texto') or ''} Entradas al día esperadas con las 4 monedas: {fe.get('entradas_dia')}.").strip()
    fals = ("Se descarta si fuera de muestra no bate a su base emparejada (" + str(d.get("base_emparejada") or
            "el sentido al azar en las mismas horas y monedas") + "), si lo que gana depende de sus 5 mayores "
            "operaciones o de su mejor semana, o si no aguanta el estrés de costes.")
    out = {"mecanismo": d.get("logica") or "", "fuentes": fu, "prediccion": pred, "falsacion": fals,
           "revision": {"prior": d.get("prior"), "riesgos": d.get("riesgos"), "ya_medido": d.get("ya_medido")}}
    if d.get("ya_medido") and "No medido" not in str(d.get("ya_medido")):
        out["parecidas_ya_probadas"] = [str(d["ya_medido"])]
        out["que_aporta"] = "Variantes del catálogo inicial fijadas antes de medir nada; ver «revision.ya_medido»."
    return out


def _freq(d: dict) -> dict:
    return {"texto": (d.get("frecuencia_esperada") or {}).get("entradas_dia"), "como": "recuento de señales del investigador"}


def convertir(d: dict) -> list[dict]:
    """Un borrador → hipótesis finales (H29 da varias). Lanza KeyError si es pendiente."""
    hid = d["id"]
    if hid in PENDIENTES:
        raise KeyError(PENDIENTES[hid])
    base = {"formato": 1, "creada": CREADA_CAT, "autor": "implementador", "familia_version": 1,
            "reglas": dict(REGLAS_USUARIO), "prioridad": int(d.get("prioridad") or 3),
            "frecuencia_esperada": _freq(d), "exploratoria": False, "origen": hid,
            "logica": _logica(d)}
    if hid == "H29":
        out = []
        for v in d["variantes"]:
            madre = v["param"]["madre"]
            letra, fam = MADRES_H29[madre]
            bd = json.loads((_cola_cat() / _archivo_de(madre[:3])).read_text(encoding="utf-8"))
            mv = next(x for x in bd["variantes"] if x["id"] == madre)
            pm = MAPA[madre[:3]][3](mv["param"])
            pm["regimen_cripto"] = v["param"]["filtro"]
            out.append((letra, fam, madre, v, pm, _salida(mv["salida"], mv["stop"])))
        res = {}
        for letra, fam, madre, v, pm, sal in out:
            h = res.setdefault(letra, {**base, "id": f"h-20261029-1130-h29{letra}-regimen-{_slug(fam)}",
                                       "titulo": f"Régimen cripto como filtro de {madre} ({fam})"[:120],
                                       "familia": fam, "datos": ["velas_1m", "funding"] if fam == "extremo_1h"
                                       else ["velas_1m", "aperturas"] if fam == "rango_horario" else ["velas_1m"],
                                       "universo": {"monedas": COINS, "momento": "continuo"}, "variantes": [],
                                       "nota": (f"Convertida del catálogo inicial (H29, madre {madre}): el régimen va "
                                                "como filtro común regimen_cripto de la familia madre.")})
            h["variantes"].append({"id": f"V{len(h['variantes']) + 1}", "params": pm, "salida": sal,
                                   "texto": v.get("texto")})
        return list(res.values())
    fam, mom, datos, fp = MAPA[hid]
    vs, quitadas = [], []
    for v in d["variantes"]:
        if (hid, v["id"]) in VARIANTES_FUERA:
            quitadas.append(f"{v['id']}: {VARIANTES_FUERA[(hid, v['id'])]}")
            continue
        sal = _salida(v["salida"], v.get("stop") or {})
        pv = fp(v.get("param") or {})
        x = {"id": f"V{len(vs) + 1}", "params": pv, "salida": sal, "texto": v.get("texto"), "origen": v["id"]}
        if v.get("fuera_de_reglas") or sal["tipo"] == "INFO":
            x["fuera_de_reglas"] = True
        if hid == "H14" and sal["tipo"] == "INFO":
            x["params"]["salida_min"] = 60
        if hid == "H28" and sal["tipo"] == "INFO":
            x["params"]["fin"] = "evento"
        if fam == "calendario" and sal.get("sl") == "estructural":   # H26-V4: máx(1,5 %, 0,75 × ATR diario)
            k = re.search(r"(\d+,\d+) × ATR_1d", str((v.get("stop") or {}).get("regla")))
            x["params"]["k_atr"] = float(k.group(1).replace(",", ".")) if k else 0.75
        vs.append(x)
    todas_fuera = all(x.get("fuera_de_reglas") for x in vs)
    nota = f"Convertida del catálogo inicial ({hid}, cola/{_archivo_de(hid)})."
    if quitadas:
        nota += " Variantes que no se miden: " + "; ".join(quitadas) + "."
    if hid == "H38":
        nota += " Decisión 15 min después del cierre de NYSE (regla del laboratorio para los cierres de bolsa)."
    if hid == "H18":
        nota += " Cierre por tiempo a las 00:00 UTC de d + 2 con el tope propio de la señal."
    universo = {"monedas": COINS, "momento": mom}
    return [{**base, "id": f"h-20261029-1130-{hid.lower()}-{_slug(d.get('slug') or d['nombre'])}",
             "titulo": d["nombre"][:120], "familia": fam, "datos": datos, "universo": universo, "variantes": vs,
             "fuera_de_reglas": bool(todas_fuera or d.get("solo_informativa")), "nota": nota}]


_COLA_CAT: Path | None = None


def _cola_cat() -> Path:
    return _COLA_CAT or ajustes.COLA


def _archivo_de(hid: str) -> str:
    for p in sorted(_cola_cat().glob(f"{hid}_*.json")):
        return p.name
    raise FileNotFoundError(hid)


def siembra() -> list[dict]:
    """Las 4 hipótesis prerregistradas de docs/07 (6.7)."""
    comun = {"formato": 1, "creada": CREADA_SIEMBRA, "autor": "implementador", "familia_version": 1,
             "reglas": dict(REGLAS_USUARIO), "prioridad": 3, "exploratoria": False}
    A = {"tipo": "A", "sl": "estructural", "trail_r": 2.0, "suelo": "F", "max_hold_h": 24, "max_hold_active_h": 168}
    C = {"tipo": "C", "sl": "estructural", "lock_r": 3.0, "trail_r": 2.0, "suelo": "F", "max_hold_h": 72,
         "max_hold_active_h": 168}
    ref = {**comun, "id": "h-20261029-0000-apertura_impulso-referencia", "prioridad": 2, "referencia": True,
           "titulo": "Referencia: «6 diarias v2» (SEIS2 exacto) con comisiones reales",
           "familia": "apertura_impulso", "datos": ["velas_1m", "aperturas"],
           "universo": {"monedas": COINS, "momento": "aperturas"},
           "logica": {"mecanismo": ("El impulso de los primeros 15 min de cada apertura de bolsa, sin filtros, con la salida A "
                                    "de «6 diarias v2»: es la cartera de prueba del usuario medida con el libro del laboratorio."),
                      "fuentes": [{"tipo": "resultado_propio", "cita": "seis2_resumen.json (docs/05, sección 21): −0,075 R fuera de muestra"}],
                      "prediccion": "Pierde fuera de muestra como en la réplica (−0,075 R) también con las comisiones reales.",
                      "falsacion": "Si con comisiones reales ganara fuera de muestra, la réplica de docs/05 estaría mal: se revisaría.",
                      "parecidas_ya_probadas": ["«6 diarias v2» (seis2_resumen.json)"],
                      "que_aporta": "Es la referencia: no cuenta en N (ya está en las previas) y sirve para comparar."},
           "variantes": [{"id": "V1", "params": {"measure_min": 15, "min_move_pct": 0.0, "entry": "t:15"},
                          "salida": {"tipo": "A", "sl": 2.0, "trail_r": 2.0, "suelo": "2R", "max_hold_h": 24,
                                     "max_hold_active_h": 168}}],
           "nota": "Siembra de docs/07 (6.7): el preset SEIS2 exacto con salida A y stop de 2 %."}
    rc = {**comun, "id": "h-20261029-0000-ruptura_canal-siembra",
          "titulo": "Ruptura del canal de 48 y 96 h en velas de 1 h, 24/7", "familia": "ruptura_canal",
          "datos": ["velas_1m"], "universo": {"monedas": COINS, "momento": "continuo"},
          "logica": {"mecanismo": ("Por encima de los máximos de varios días se acumulan stops de cortos y órdenes de compra por "
                                   "ruptura; al romper, su ejecución y las liquidaciones de los apalancados empujan el precio, y la "
                                   "cripto tiende a seguir su tendencia de días (momento de series temporales)."),
                     "por_que_ahora": ("El momento de 1 a 4 semanas está documentado en cripto y el apalancamiento en perpetuos "
                                       "sigue siendo alto: las cascadas de liquidaciones alargan las rupturas."),
                     "fuentes": [{"tipo": "libro", "cita": "Faith, C. (2007). Way of the Turtle (reglas de ruptura de 20 y 55 días)."},
                                 {"tipo": "articulo", "cita": "Hurst, Ooi y Pedersen (2017). A Century of Evidence on Trend-Following Investing."},
                                 {"tipo": "articulo", "cita": "Liu y Tsyvinski (2021). Risks and Returns of Cryptocurrency (momento de series temporales)."}],
                     "prediccion": ("Ganadoras por debajo del 40 % y pocas operaciones de +4R o más que pagan las pérdidas; mejor "
                                    "con el cerrojo que con la horquilla; 0,5 a 1,5 operaciones al día entre las 4 monedas."),
                     "falsacion": ("Se descarta si fuera de muestra no bate a su base emparejada (el sentido al azar en las mismas "
                                   "horas y monedas) o si lo que gana depende de 5 operaciones."),
                     "parecidas_ya_probadas": ["laboratorio: ruptura del rango de apertura (2.000)",
                                               "selectiva: Ruptura V3 (rango de apertura)"],
                     "que_aporta": ("No es la ruptura del rango de los primeros minutos de una apertura: es la de varios días, a "
                                    "cualquier hora, y deja correr con el cerrojo.")},
          "variantes": [
              {"id": "V1", "params": {"horas": 48, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5}, "salida": A},
              {"id": "V2", "params": {"horas": 96, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5}, "salida": A},
              {"id": "V3", "params": {"horas": 48, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5, "regimen_btc": "a_favor"}, "salida": A},
              {"id": "V4", "params": {"horas": 96, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5, "regimen_btc": "a_favor"}, "salida": A},
              {"id": "V5", "params": {"horas": 48, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5}, "salida": C},
              {"id": "V6", "params": {"horas": 96, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5}, "salida": C}],
          "frecuencia_esperada": {"op_dia": 0.8, "como": "estimación del diseño"},
          "nota": "Siembra de docs/07 (6.7): la del ejemplo de 5.2."}
    base_c = {"ventana_h": 24, "percentil": 0.2, "tf_min": 60, "caduca_h": 24}
    cr = {**comun, "id": "h-20261029-0000-compresion_ruptura-siembra",
          "titulo": "Ruptura tras una compresión del rango (24 h en el percentil 20 de 30 días)",
          "familia": "compresion_ruptura", "datos": ["velas_1m"], "universo": {"monedas": COINS, "momento": "continuo"},
          "logica": {"mecanismo": ("La volatilidad se agrupa y vuelve a su media: tras un rango estrecho, los stops y las órdenes "
                                   "acumulados a los dos lados hacen que la ruptura del rango se extienda (compresión y expansión)."),
                     "fuentes": [{"tipo": "libro", "cita": "Crabel, T. (1990). Day Trading with Short Term Price Patterns and Opening Range Breakout (NR4/NR7)."},
                                 {"tipo": "libro", "cita": "Bollinger, J. (2001). Bollinger on Bollinger Bands (compresión de las bandas)."}],
                     "prediccion": ("Pocas operaciones (≈ 0,3 a 0,8 al día entre las 4 monedas) con más ganadoras que una ruptura "
                                    "cualquiera; el stop al otro lado del rango es corto."),
                     "falsacion": ("Se descarta si fuera de muestra no bate a su base emparejada o si lo que gana depende de 5 "
                                   "operaciones o de una semana.")},
          "variantes": [
              {"id": "V1", "params": dict(base_c), "salida": A},
              {"id": "V2", "params": {**base_c, "ventana_h": 12}, "salida": A},
              {"id": "V3", "params": {**base_c, "ventana_h": 48}, "salida": A},
              {"id": "V4", "params": {**base_c, "percentil": 0.1}, "salida": A},
              {"id": "V5", "params": dict(base_c), "salida": C},
              {"id": "V6", "params": {**base_c, "regimen_btc": "a_favor"}, "salida": A}],
          "frecuencia_esperada": {"op_dia": 0.5, "como": "estimación del diseño"},
          "nota": "Siembra de docs/07 (6.7)."}
    C3 = {**C, "sl": 3.0}
    base_t = {"retorno_d": 14, "umbral_pct": 0.0, "media": "ema50"}
    td = {**comun, "id": "h-20261029-0000-tendencia_diaria-siembra",
          "titulo": "Tendencia diaria: retorno de 14 días a favor de la EMA50, cerrojo y stop de 3 %",
          "familia": "tendencia_diaria", "datos": ["velas_1m"], "universo": {"monedas": COINS, "momento": "continuo"},
          "logica": {"mecanismo": ("Infrarreacción, atención y flujos graduales: los rendimientos de 1 a 4 semanas de cada moneda "
                                   "tienden a continuar; entrar a favor y dejar correr con el cerrojo captura los movimientos de días."),
                     "fuentes": [{"tipo": "articulo", "cita": "Moskowitz, Ooi y Pedersen (2012). Time series momentum. JFE 104(2)."},
                                 {"tipo": "articulo", "cita": "Liu y Tsyvinski (2021). Risks and Returns of Cryptocurrency. RFS 34(6)."}],
                     "prediccion": ("Pocas operaciones (≈ 0,2 a 0,5 al día entre las 4 monedas), ganadoras por debajo del 40 % y "
                                    "algunas de varios R que pagan las pérdidas."),
                     "falsacion": ("Se descarta si fuera de muestra no bate a su base emparejada o si lo que gana depende de 5 "
                                   "operaciones o de una semana.")},
          "variantes": [
              {"id": "V1", "params": dict(base_t), "salida": C3},
              {"id": "V2", "params": {**base_t, "retorno_d": 28}, "salida": C3},
              {"id": "V3", "params": {**base_t, "retorno_d": 7}, "salida": C3},
              {"id": "V4", "params": {**base_t, "umbral_pct": 5.0}, "salida": C3},
              {"id": "V5", "params": {**base_t, "lados": "largos"}, "salida": C3},
              {"id": "V6", "params": dict(base_t), "salida": {"tipo": "H", "sl": 5.0, "target_r": 2.0, "max_hold_h": 168}}],
          "frecuencia_esperada": {"op_dia": 0.3, "como": "estimación del diseño"},
          "nota": "Siembra de docs/07 (6.7)."}
    return [ref, rc, cr, td]


def catalogo(carpeta: Path | None = None) -> tuple[list[dict], dict[str, str]]:
    """(hipótesis convertidas, {borrador: motivo} de las pendientes)."""
    global _COLA_CAT
    _COLA_CAT = Path(carpeta) if carpeta else ajustes.COLA
    out, pend = [], {}
    for p in sorted(_COLA_CAT.glob("H*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        try:
            out += convertir(d)
        except KeyError as e:
            pend[d["id"]] = str(e).strip("'\"")
    return out, pend


def escribir(hs: list[dict], carpeta: Path | None = None, ya: set | None = None) -> list[Path]:
    """Escribe cada hipótesis en el buzón (primero .tmp y luego se renombra: el corredor nunca lee una a medias)."""
    carpeta = carpeta or ajustes.ENTRADA
    carpeta.mkdir(parents=True, exist_ok=True)
    out = []
    for h in hs:
        if ya and h["id"] in ya:
            continue
        p = carpeta / f"{h['id']}.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        out.append(p)
    return out


def escribir_pendientes(pend: dict[str, str]) -> Path:
    car = ajustes.COLA / "esperando_datos"
    car.mkdir(parents=True, exist_ok=True)
    p = car / "pendientes.json"
    p.write_text(json.dumps({"formato": 1, "ts": ajustes.ahora_ms(), "pendientes": pend,
                             "nota": "Borradores del catálogo inicial que este laboratorio aún no puede medir (docs/07)."},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return p
