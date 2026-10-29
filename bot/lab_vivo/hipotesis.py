"""Hipótesis del laboratorio vivo (docs/07, 5 y 10.4): formato, admisión sin datos (A1-A7 y A10), firma, reglas del
usuario por variante y a_params_motor.

La admisión no mira ningún resultado y no cuenta en el número de pruebas. A8 (prueba del recorte con datos reales) y A9
(frecuencia de señales dentro de muestra) los hace un trabajo de admisión (etapas.admision)."""
import hashlib
import json
import math
import re
from datetime import datetime

from .. import config
from . import criterios, salidas

ID_RE = re.compile(r"^h-\d{8}-\d{4}-[a-z0-9_-]{3,60}$")
VID_RE = re.compile(r"^V[1-6]$")
AUTORES = ("generador-claude", "usuario", "coordinador", "implementador")
TIPOS_FUENTE = ("articulo", "libro", "estrategia_publica", "resultado_propio")
DATOS = {   # docs/07, 4.1: clave → (tiene historia de 3 años, texto)
    "velas_1m": (True, "velas de 1 min de BTC, ETH, SOL y XRP (Binance al contado en USDT)"),
    "funding": (True, "funding de Binance USDT-M cada 8 h"),
    "aperturas": (True, "aperturas reales de las bolsas (con festivos)"),
    "bolsas": (True, "cierres diarios de las 9 bolsas (conocidos 15 min después)"),
    "fng": (True, "miedo y codicia (conocido 1 h después del día)"),
    "macro": (True, "calendario macro (IPC, empleo y FOMC)"),
    "flujo_tomador": (True, "compra del tomador de las velas de 1 min (columna 9)"),
    "noticias_historia": (False, "91 días de titulares (dentro del fuera de muestra: solo exploratoria)"),
    "noticias_vivas": (False, "noticias del bot en vivo (solo prueba en avance)"),
}
REGLAS_USUARIO = {"riesgo_pct": 0.5, "max_entradas_dia": 6, "max_riesgo_dia_pct": 3.0, "largos": "spot",
                  "cortos": "perp", "una_por_moneda": True}
ESTADOS_INICIALES = ("normal", "esperando_datos")


def _norm(v):
    """Normalización de la firma: números a 6 decimales, listas sin orden (ordenadas), textos tal cual."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    if isinstance(v, (list, tuple, set)):
        return sorted((_norm(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in sorted(v.items())}
    return str(v)


def firma_variante(familia: str, version: int, params: dict, salida: dict, universo: dict) -> str:
    key = {"familia": familia, "version": int(version), "params": _norm(params), "salida": _norm(salida),
           "universo": _norm({"monedas": universo.get("monedas"), "ventanas": universo.get("ventanas")})}
    return hashlib.sha1(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def firma_hipotesis(h: dict) -> str:
    return hashlib.sha1(json.dumps(_norm({k: h.get(k) for k in ("familia", "familia_version", "variantes", "universo")}),
                                   sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _texto(x, n: int) -> bool:
    return isinstance(x, str) and len(x.strip()) >= n


def validar(h: dict, firmas_probadas: dict[str, str] | None = None, familias_db: dict | None = None) -> dict:
    """Admisión sin datos (A1-A7 y A10). firmas_probadas: {firma: id de la variante ya medida}; familias_db: {nombre:
    {version: sha}} registradas. Devuelve {ok, motivos, variantes: [...], exploratoria, fuera_de_reglas, ...}."""
    from .familias import cargar, errores, sha_archivo
    mot: list[str] = []
    out = {"ok": False, "motivos": mot, "variantes": [], "id": h.get("id") if isinstance(h, dict) else None}
    if not isinstance(h, dict):
        mot.append("A1: la hipótesis tiene que ser un objeto JSON")
        return out
    # A1: formato y campos
    if h.get("formato") != 1:
        mot.append("A1: «formato» tiene que ser 1")
    if not isinstance(h.get("id"), str) or not ID_RE.match(h["id"]):
        mot.append("A1: «id» tiene que ser h-AAAAMMDD-HHMM-nombre (minúsculas, cifras, _ o -; de 3 a 60)")
    try:
        datetime.fromisoformat(str(h.get("creada")))
        if datetime.fromisoformat(str(h.get("creada"))).tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        mot.append("A1: «creada» tiene que ser una fecha ISO con zona (p. ej. 2026-10-29T12:15:00+01:00)")
    if h.get("autor") not in AUTORES:
        mot.append(f"A1: «autor» tiene que ser {', '.join(AUTORES)}")
    if not _texto(h.get("titulo"), 5) or len(h.get("titulo", "")) > 120:
        mot.append("A1: «titulo» de 5 a 120 caracteres, en español")
    for k in ("familia", "logica", "datos", "universo", "variantes", "reglas"):
        if k not in h:
            mot.append(f"A1: falta el campo «{k}»")
    if mot:
        return out
    pr = h.get("prioridad", 3)
    if not isinstance(pr, int) or not 1 <= pr <= 5:
        mot.append("A1: «prioridad» de 1 (antes) a 5")
    expl = bool(h.get("exploratoria", False))
    fdr_h = bool(h.get("fuera_de_reglas", False))
    estado_ini = h.get("estado_inicial", "normal")
    if estado_ini not in ESTADOS_INICIALES:
        mot.append(f"A1: «estado_inicial» tiene que ser {', '.join(ESTADOS_INICIALES)}")
    # A10: lógica
    lg = h["logica"] if isinstance(h["logica"], dict) else {}
    nmin = criterios.c("admision", "min_texto_logica")
    for k in ("mecanismo", "prediccion", "falsacion"):
        if not _texto(lg.get(k), nmin):
            mot.append(f"A10: logica.{k} tiene que explicar la idea (≥ {nmin} caracteres)")
    fu = lg.get("fuentes")
    if not isinstance(fu, list) or not fu or not all(isinstance(x, dict) and _texto(x.get("cita"), 10) for x in fu):
        mot.append("A10: logica.fuentes: al menos una fuente con «cita» (artículo, libro, estrategia pública o "
                   "resultado propio)")
    elif any(x.get("tipo") not in TIPOS_FUENTE for x in fu):
        mot.append(f"A10: el «tipo» de cada fuente tiene que ser {', '.join(TIPOS_FUENTE)}")
    if lg.get("parecidas_ya_probadas") and not _texto(lg.get("que_aporta"), 20):
        mot.append("A10: con «parecidas_ya_probadas» hace falta «que_aporta» (en qué se diferencia de lo probado)")
    # A2: familia registrada y vigente
    reg = cargar()
    fam = reg.get(h["familia"])
    if fam is None:
        e = errores().get(h["familia"])
        mot.append(f"A2: la familia «{h['familia']}» " + (f"no se puede cargar ({e})" if e else "no está registrada"))
        return out
    if h.get("familia_version") != fam.version:
        mot.append(f"A2: la familia {fam.nombre} está en la versión {fam.version} (la hipótesis dice "
                   f"{h.get('familia_version')})")
    if familias_db is not None:
        sha = sha_archivo(fam.nombre)
        reg_sha = (familias_db.get(fam.nombre) or {}).get(fam.version)
        if reg_sha and reg_sha != sha:
            mot.append(f"A2: familia cambiada sin subir versión ({fam.nombre} v{fam.version}): sus resultados no "
                       "serían comparables")
    # A6: datos
    dts = h["datos"] if isinstance(h["datos"], list) else []
    if not dts:
        mot.append("A6: «datos» tiene que listar las claves que usa (docs/07, 4.1)")
    for k in dts:
        if k not in DATOS:
            mot.append(f"A6: dato desconocido «{k}» (hay: {', '.join(DATOS)})")
        elif k not in fam.datos:
            mot.append(f"A6: la familia {fam.nombre} no usa «{k}» (usa: {', '.join(fam.datos)})")
    sin_historia = [k for k in dts if k in DATOS and not DATOS[k][0]]
    if (sin_historia or not fam.historia) and not expl:
        mot.append("A6: con datos sin 3 años de historia (" + ", ".join(sin_historia or ["la familia"]) +
                   ") solo puede ser exploratoria («exploratoria»: true)")
    # universo
    u = h["universo"] if isinstance(h["universo"], dict) else {}
    mons = u.get("monedas") or []
    if not mons or any(c not in config.COINS for c in mons):
        mot.append(f"A1: universo.monedas ⊆ {', '.join(config.COINS)}")
    if u.get("momento") not in ("aperturas", "continuo"):
        mot.append("A1: universo.momento tiene que ser «aperturas» o «continuo»")
    elif u.get("momento") != fam.momento:
        mot.append(f"A1: la familia {fam.nombre} es de momento «{fam.momento}»")
    if u.get("ventanas") is not None:
        from .. import apertura as ap
        if u.get("momento") != "aperturas" or any(w not in ap.LAB_WINDOWS for w in u["ventanas"]):
            mot.append(f"A1: universo.ventanas solo con aperturas y ⊆ {', '.join(ap.LAB_WINDOWS)}")
    # A5 (parte común): reglas del bloque y riesgo
    rg = h["reglas"] if isinstance(h["reglas"], dict) else {}
    reglas_ok = all(rg.get(k) == v for k, v in REGLAS_USUARIO.items()) and \
        math.isclose(float(rg.get("riesgo_pct", 0) or 0), float(config.RISK_PER_TRADE_PCT))
    if not reglas_ok and not fdr_h:
        mot.append("A5: «reglas» tienen que ser exactamente las del usuario (riesgo 0,5 %, 6 entradas y 3 % al día, "
                   "largos al contado, cortos en perpetuo, una posición por moneda); otra cosa solo con "
                   "«fuera_de_reglas»: true")
    # A3: variantes
    vs = h["variantes"] if isinstance(h["variantes"], list) else []
    if not 1 <= len(vs) <= criterios.c("admision", "max_variantes"):
        mot.append("A3: de 1 a 6 variantes")
    vids, firmas = set(), set()
    nmax_f = criterios.c("admision", "max_filtros")
    for v in vs:
        if not isinstance(v, dict) or not VID_RE.match(str(v.get("id", ""))):
            mot.append("A3: cada variante con «id» V1 a V6")
            continue
        vid = v["id"]
        if vid in vids:
            mot.append(f"A3: la variante {vid} está repetida")
        vids.add(vid)
        pv = dict(v.get("params") or {})
        if "monedas" not in pv:
            pv["monedas"] = list(mons)
        elif any(c not in mons for c in pv["monedas"]):
            mot.append(f"A3: {vid}: sus monedas tienen que estar en el universo")
        pc, perr = fam.completar(pv)
        for e in perr:
            mot.append(f"A3: {vid}: {e}")
        sal, serr = salidas.normalizar(v.get("salida"))
        for e in serr:
            mot.append(f"A3: {vid}: salida: {e}")
        # A4: filtros
        fa = fam.filtros_activos(pc) if not perr else []
        if len(fa) > nmax_f:
            mot.append(f"A4: {vid}: {len(fa)} filtros activos ({', '.join(fa)}); como mucho {nmax_f}")
        fdr = bool(v.get("fuera_de_reglas", False)) or fdr_h
        cumple, why_r, sl_min = (False, ["fuera de reglas"], None)
        if not serr and not perr:
            cumple, why_r, sl_min = salidas.comprobar_reglas(sal, pc.get("lados", "ambos"))
            cumple = cumple and reglas_ok
        if not cumple and not fdr:
            mot.append(f"A5: {vid}: no cumple las reglas del usuario ({'; '.join(why_r) or 'reglas'}); solo se puede "
                       "medir como «fuera_de_reglas»: true (informativa, nunca pasa a la cartera)")
        if salidas.es_info(sal) and not fdr:
            mot.append(f"A5: {vid}: la salida INFO solo vale fuera de reglas")
        fv = firma_variante(fam.nombre, fam.version, pc, sal, u) if not perr and not serr else None
        if fv and fv in firmas:
            mot.append(f"A3: {vid}: igual que otra variante de la misma hipótesis")
        firmas.add(fv)
        dup = (firmas_probadas or {}).get(fv) if fv else None
        out["variantes"].append({"vid": vid, "params": pc, "salida": sal, "firma": fv, "cumple_reglas": bool(cumple),
                                 "fuera_de_reglas": bool(fdr or not cumple), "filtros": fa, "sl_min": sl_min,
                                 "duplicada_de": dup, "texto": v.get("texto")})
    # A7: duplicadas
    if out["variantes"] and all(x["duplicada_de"] for x in out["variantes"]):
        mot.append("A7: ya probada: todas sus variantes tienen la firma de otras ya medidas ("
                   + ", ".join(sorted({x['duplicada_de'] for x in out['variantes']})) + ")")
    out.update(ok=not mot, exploratoria=expl, fuera_de_reglas=fdr_h, familia=fam.nombre,
               familia_version=fam.version, prioridad=pr if isinstance(pr, int) else 3, estado_inicial=estado_ini,
               referencia=bool(h.get("referencia", False)))
    return out


def a_params_motor(familia: str, params: dict, salida: dict) -> tuple[dict | None, str]:
    """Cómo se operaría (10.4): solo la familia apertura_impulso se traduce a parámetros del motor de aperturas (como
    evolucion.lab_a_params). Si evolucion.cumple_reglas los acepta, el usuario puede aplicarla desde Evolución."""
    if familia != "apertura_impulso":
        return None, ("Necesita un modelo nuevo en el motor de aperturas (lo programa el coordinador). Hasta entonces "
                      "sigue en la prueba en avance.")
    from .. import evolucion
    from .familias import obtener
    fam = obtener(familia)
    p, err = fam.completar(params)
    if err:
        return None, "; ".join(err)
    s, serr = salidas.normalizar(salida)
    if serr or s["tipo"] not in ("A", "H", "C") or not isinstance(s.get("sl"), float):
        return None, "el motor necesita una salida A, H o C con stop fijo"
    over = fam.params_motor(p)
    mot = salidas.params_motor(s, s["sl"])
    over.update({k: mot[k] for k in ("sl_pct", "target_r", "tp1_r", "tp1_frac", "lock_r", "floor_r", "trail_r",
                                     "trail_atr", "max_hold_h", "max_hold_active_h", "avg_hold_h")})
    over["news_mode"] = "fiables"   # en vivo, el veto de noticias fiables en contra (como SEIS y SEIS2)
    ok, fallos = evolucion.cumple_reglas(over)
    if not ok:
        return None, "rompe las reglas del usuario: " + "; ".join(fallos)
    return over, "Se puede aplicar desde Evolución (estrategia_activa.json, el mecanismo de siempre)."
