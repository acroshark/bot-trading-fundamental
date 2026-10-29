"""Ranking de estrategias (29-10-2026). Lo pidió el usuario: «un ranking de estrategias ordenadas de más beneficio a
menos, con el número de operaciones medio y otros datos relevantes; todas las que vaya obteniendo el agente entran solas
según su posición; da igual si no llegan al 1 % diario, siempre que no estén dando pérdidas».

Fila común de una estrategia (contrato en docs/04, 2.20):
- id, nombre, familia, origen («laboratorio_vivo», «estudio», «laboratorio_2000», «curso», «selectiva», «salidas»,
  «en_vivo»…), estudio, regla (texto llano), cumple_reglas, perfil_costes, fecha (AAAA-MM-DD), archivo (de procedencia).
- dentro {n, R, gana_pct}, fuera {n, R, gana_pct, p?, q?}: R medio por operación NETO de costes.
- op_dia: operaciones medias por jornada con aperturas (de lunes a viernes salvo el 1 de enero: 306 jornadas en el fuera
  de muestra de los estudios). pct_dia: % de la cuenta por jornada con 0,5 % de riesgo por operación, neto de costes y
  fuera de muestra (R × 0,5 × op_dia, sin interés compuesto). R_dia = R × op_dia. Lo que opera también en fin de
  semana se pasa a jornadas con su mismo periodo (lo de un día natural × días naturales / jornadas).
- caida_max_pct, robustez {sin_5_mayores, dentro_positivo, corregida {tipo, metodo, valor, pasa}, mitades}, en_vivo
  {n, R, neto_usdc, desde…} si opera o ha operado en el paper o en una prueba en avance, estado («backtest», «en_avance»,
  «en_paper», «descartada_por_el_usuario») y fragil (si gana fuera y pierde dentro, si sin sus 5 mayores pierde, si no
  pasa la corrección por número de pruebas o si no la tiene siendo una de varias configuraciones), con sus motivos.
- corregida.tipo, corto para la web: «holm», «bh» (q de Benjamini-Hochberg), «dsr» (Sharpe deflactado), «bonferroni»,
  «p» o «sin_p» (una de N configuraciones de su estudio sin p ni corrección: pasa = false). El método largo va en metodo.

Todo lo que entra se limpia (números finitos, textos cortos): una fila o una tarjeta rota se salta y se apunta en
«errores» de su fuente, sin tumbar el ranking ni dejar NaN en el JSON.

Fuentes (todas en solo lectura; toleran que falten y se recargan solas por mtime):
- BACKTEST_DIR/ranking/*.json: una fila común por archivo (también {"filas": [...]}); los que empiezan por «_» no son
  filas. Así cualquier estudio futuro entra con solo dejar su archivo (scripts/ranking_importar.py pasa los ya hechos).
- El laboratorio vivo: las tarjetas de bot/lab_vivo_lectura.py (docs/07, 13.3), sin importar bot.lab_vivo.
- En vivo: las operaciones del paper por preset (tags.preset) y la prueba en avance (vigilancia).

Orden: pct_dia de mayor a menor; desempate op_dia y n fuera. Entran las que NO pierden fuera de muestra (fuera.R ≥ 0).
La que está en marcha en el motor se enseña siempre aparte (en_marcha): con su puesto o por qué no entra («fuera del
ranking: pierde» solo si pierde; si no pierde, qué le falta). GET /api/ranking lleva ETag (Ranking.firma).
Cada fila lleva su puesto, el de antes y si es nueva (entró en las últimas 24 h; lo visto se guarda en
DATA_DIR/ranking_estado.json, que es lo único que escribe este módulo).
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .lab_vivo_lectura import LabVivoLectura
from .models import now_ms

log = logging.getLogger("ranking")
MAD = ZoneInfo("Europe/Madrid")
RIESGO_PCT = 0.5            # % de la cuenta por operación con el que se expresa todo (el de las reglas; no se sube)
ESTADOS = ("backtest", "en_avance", "en_paper", "descartada_por_el_usuario")
ORIGENES = {"laboratorio_vivo": "Laboratorio vivo", "estudio": "Réplicas del motor",
            "laboratorio_2000": "Laboratorio de 2.000", "curso": "Técnicas del curso",
            "selectiva": "Selectivas (Murphy, Fibonacci y Ruptura)", "salidas": "Salidas, stops y objetivos",
            "en_vivo": "Solo en vivo (sin backtest)"}
PERFILES_TEXTO = {"backtest": "backtest con la gestión del paper: 0,10 % por lado y 2 pb (también en los perpetuos), con "
                              "su funding",
                  "laboratorio": "laboratorio de 2.000: 0,10 % por lado, 2 pb y 5-20 pb más en los stops, todo con el "
                                 "modelo del contado (también los cortos) y sin funding",
                  "okx_eu_con_xperps": "OKX EU con X-Perps: contado 0,10 / 0,08 %, X-Perps 0,05 / 0,02 %, 2 pb y el "
                                       "funding de los perpetuos",
                  "okx_eu_sin_xperps": "OKX EU sin X-Perps: contado 0,35 / 0,20 % y sin cortos",
                  "sin_costes": "sin costes (solo para comparar)", "paper": "las del paper (perfil en uso)"}
# Lo mismo en corto, para la tabla de la web (un chip por fila: el orden mezcla estudios con costes distintos)
PERFILES_CORTO = {"backtest": "costes del backtest", "laboratorio": "costes del laboratorio (sin funding)",
                  "okx_eu_con_xperps": "costes OKX EU con X-Perps", "okx_eu_sin_xperps": "costes OKX EU sin X-Perps",
                  "sin_costes": "sin costes", "paper": "costes del paper"}
TIPOS_CORR = ("holm", "bh", "dsr", "bonferroni", "p", "sin_p")
DIAS_MAX = 3650             # tope de días de un periodo del laboratorio vivo (10 años; lo de más es un archivo roto)
# Preset del motor (apertura.PRESETS) → fila del ranking
PRESET_FILA = {"ruptura3": "ruptura3", "seis2": "seis2", "seis": "seis", "impulso": "e2_impulso_7d"}
VIGILANCIA_FILA = {"E2-7d": "e2_impulso_7d"}   # la variante de 7 días de E2 es la de impulso_7d.json
EN_VIVO_NOMBRES = {"en_vivo:noticia_fiable": "Entrada por noticia fiable (paper)",
                   "en_vivo:motor": "Motor de aperturas con parámetros cambiados (paper)",
                   "en_vivo:paper_otras": "Otras operaciones del paper (fase 2)"}
Q_MAX, P_MAX, DSR_MIN = 0.10, 0.05, 0.95
POCA_MUESTRA = 30           # menos operaciones fuera de muestra que esto: poca muestra (se marca, no se quita)
BUS_CADA_S = 60             # el mensaje «ranking» sale como mucho cada 60 s, y solo si cambia
TOP_BUS = 20
NUEVA_MS = 24 * 3600_000
LIMITE_DEF, LIMITE_MAX = 300, 5000
CACHE_S = 2.0
ID_RE = re.compile(r"^.{1,300}$", re.S)
ORIGEN_RE = re.compile(r"^[a-z0-9_]{1,40}$")
DIA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LAB_VIVO_ID = re.compile(r"^h-(\d{4})(\d{2})(\d{2})-")
NOTA = ("Ordenado por beneficio fuera de muestra con costes: % de la cuenta por jornada con 0,5 % de riesgo por "
        "operación, sin interés compuesto. Entran las que no pierden fuera de muestra. Cada fila va con los costes de su "
        "estudio, que no son los mismos para todas. Lo de arriba con poca muestra o frágil suele ser suerte: ninguna ha "
        "demostrado ventaja y nada de esto promete rentabilidad (todo es paper).")
CRITERIO = {"orden": "pct_dia (fuera de muestra, neto de costes) de mayor a menor; desempate op_dia y n fuera",
            "entra": "fuera.R ≥ 0 (no pierde fuera de muestra, neto de costes)",
            "riesgo_pct": RIESGO_PCT, "jornada": "día de lunes a viernes con aperturas (salvo el 1 de enero)",
            "fragil": "gana fuera y pierde dentro, sin sus 5 mayores pierde, no pasa la corrección por número de "
                      "pruebas o no la tiene siendo una de varias configuraciones de su estudio",
            "poca_muestra": f"menos de {POCA_MUESTRA} operaciones fuera de muestra",
            "nueva": "entró en el ranking en las últimas 24 h",
            "costes": "cada fila con los costes con los que la midió su estudio (perfil_costes): las réplicas, las "
                      "selectivas, el curso y las salidas, 0,10 % por lado y 2 pb con funding; el laboratorio de 2.000, "
                      "5-20 pb más en los stops y sin funding; el laboratorio vivo, OKX EU con X-Perps (más barato)"}


# ---------------------------------------------------------------- utilidades

def _num(x) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        x = float(x)
    except OverflowError:   # un entero de JSON con cientos de cifras
        return None
    return x if math.isfinite(x) else None


def _ent(x) -> int | None:
    v = _num(x)
    return int(v) if v is not None and v >= 0 and abs(v - round(v)) < 1e-9 else None


def _txt(x, n: int = 600) -> str | None:
    if not isinstance(x, str):
        return None
    s = " ".join(x.split())
    return s[:n] if s else None


def _r(x, d: int = 4) -> float | None:
    v = _num(x)
    return round(v, d) if v is not None else None


def _n(x, d: int = 2, signo: bool = False) -> str:
    v = _num(x)
    if v is None:
        return "–"
    s = f"{v:+.{d}f}" if signo else f"{v:.{d}f}"
    return s.replace("-", "−").replace(".", ",")


def _dia(x) -> date | None:
    if isinstance(x, date):
        return x
    if isinstance(x, str) and DIA_RE.fullmatch(x[:10]):
        try:
            return date.fromisoformat(x[:10])
        except ValueError:
            return None
    return None


def _laborables(ordinal: int) -> int:
    """Días de lunes a viernes del 1-1-1 (lunes, ordinal 1) al día de ese ordinal, incluido."""
    semanas, resto = divmod(ordinal, 7)
    return semanas * 5 + min(resto, 5)


def jornadas_entre(desde, hasta) -> int:
    """Jornadas con aperturas entre dos días (incluidos): de lunes a viernes salvo el 1 de enero, cuando no abre
    ninguna bolsa de las del laboratorio. Da las 306 jornadas del fuera de muestra (16-08-2025 a 20-10-2026) y las 468
    del dentro (30-10-2023 a 15-08-2025) de los estudios. Sin recorrer día a día: vale para cualquier fecha válida."""
    a, b = _dia(desde), _dia(hasta)
    if a is None or b is None or b < a:
        return 0
    n = _laborables(b.toordinal()) - _laborables(a.toordinal() - 1)
    for y in range(a.year if (a.month, a.day) == (1, 1) else a.year + 1, b.year + 1):
        if date(y, 1, 1).weekday() < 5:
            n -= 1
    return n


def _mas_dias(d: date, n: int) -> date:
    try:
        return d + timedelta(days=n)
    except OverflowError:
        return date.max if n > 0 else date.min


def dias_entre(desde, hasta) -> int:
    a, b = _dia(desde), _dia(hasta)
    return (b - a).days + 1 if a and b and b >= a else 0


def holm(ps: list) -> list:
    """p corregidos por Holm (los None se quedan en None; m = los que tienen p)."""
    idx = sorted((i for i, p in enumerate(ps) if _num(p) is not None), key=lambda i: ps[i])
    m = len(idx)
    out: list = [None] * len(ps)
    run = 0.0
    for k, i in enumerate(idx):
        run = max(run, min(1.0, (m - k) * float(ps[i])))
        out[i] = round(run, 6)
    return out


# ---------------------------------------------------------------- la fila común

def _tramo(x) -> dict | None:
    """{n, R, gana_pct, …} limpio: n entero, números finitos; lo que no sea número se queda fuera."""
    if not isinstance(x, dict):
        return None
    out = {"n": _ent(x.get("n")), "R": _num(x.get("R")), "gana_pct": _num(x.get("gana_pct"))}
    for k in ("p", "q", "dsr", "p_holm", "p_bh", "dias", "jornadas", "R_bruto"):
        v = _num(x.get(k))
        if v is not None:
            out[k] = v
    ic = x.get("ic95")
    if isinstance(ic, (list, tuple)) and len(ic) == 2 and all(_num(v) is not None for v in ic):
        out["ic95"] = [float(ic[0]), float(ic[1])]
    for k in ("desde", "hasta"):
        if _dia(x.get(k)):
            out[k] = str(x[k])[:10]
    return out if out["n"] is not None or out["R"] is not None else None


def tipo_correccion(c: dict) -> str | None:
    """El tipo corto de una corrección (TIPOS_CORR): el que trae o, si no, el que se deduce de su método."""
    t = c.get("tipo")
    if isinstance(t, str) and t in TIPOS_CORR:
        return t
    m = c.get("metodo").lower() if isinstance(c.get("metodo"), str) else ""
    if "holm" in m:
        return "holm"
    if "benjamini" in m or "(q)" in m or m in ("q", "bh"):
        return "bh"
    if "sharpe" in m or m == "dsr":
        return "dsr"
    if "bonferroni" in m:
        return "bonferroni"
    return "p" if m == "p" else None


def _corregida(c) -> dict | None:
    if not isinstance(c, dict):
        return None
    valor = _num(c.get("valor"))
    pasa = c.get("pasa") if isinstance(c.get("pasa"), bool) else None
    metodo = _txt(c.get("metodo"), 200)
    tipo = tipo_correccion(c)
    if tipo == "sin_p":
        return {"tipo": "sin_p", "metodo": metodo or "sin corrección por número de pruebas", "valor": None,
                "pasa": False}
    if valor is None and pasa is None:
        return None
    return {"tipo": tipo, "metodo": metodo, "valor": valor, "pasa": pasa}


def _miles(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def sin_corregir(probadas: int, p: float | None = None) -> dict:
    """La corrección de una fila sin ella que es una de varias configuraciones de su estudio: no la pasa."""
    que = f"p {_n(p, 3)} sin corregir" if p is not None else "sin p ni corrección"
    return {"tipo": "sin_p", "metodo": f"{que}: una de {_miles(probadas)} configuraciones de su estudio", "valor": None,
            "pasa": False}


def _vivo_limpio(v, hondo: int = 0) -> dict | None:
    """en_vivo sin nada raro: claves de texto cortas; números finitos, booleanos, textos cortos y null, y un nivel más
    para los objetos de dentro («avance»). Lo demás (listas, NaN, infinitos…) se queda fuera."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k, x in list(v.items())[:40]:
        if not isinstance(k, str) or not 0 < len(k) <= 40:
            continue
        if x is None or isinstance(x, bool):
            out[k] = x
        elif isinstance(x, (int, float)):
            n = _num(x)
            if n is not None:
                out[k] = x if isinstance(x, int) and abs(x) <= 2 ** 53 else n
        elif isinstance(x, str):
            out[k] = x[:200]
        elif isinstance(x, dict) and hondo == 0:
            out[k] = _vivo_limpio(x, 1)
    return out


def fragilidad(f: dict) -> list[str]:
    """Por qué una fila es frágil (vacío si no lo es)."""
    out = []
    fu, de = f.get("fuera") or {}, f.get("dentro") or {}
    rob = f.get("robustez") or {}
    if (_num(fu.get("R")) or 0) > 0 and _num(de.get("R")) is not None and de["R"] < 0:
        out.append("gana fuera de muestra pero pierde dentro")
    s5 = _num(rob.get("sin_5_mayores"))
    if s5 is not None and s5 < 0:
        out.append("sin sus 5 mejores operaciones pierde")
    c = rob.get("corregida") or {}
    n = _ent(f.get("probadas"))
    if c.get("tipo") == "sin_p":
        out.append("sin corrección por número de pruebas" + (
            f": una de {_miles(n)} configuraciones de su estudio, que sube en el ranking por su resultado fuera de "
            "muestra" if n and n > 1 else ""))
    elif c.get("pasa") is False:
        out.append("no pasa la corrección por número de pruebas")
    return out


def normalizar(f, archivo: str | None = None) -> dict | None:
    """Fila común limpia, con lo que falte calculado (op_dia con las jornadas, pct_dia y R_dia con el R de fuera) y
    la fragilidad. None si no tiene id."""
    if not isinstance(f, dict):
        return None
    fid = f.get("id")
    if not isinstance(fid, str) or not fid.strip() or not ID_RE.fullmatch(fid):
        return None
    fid = fid.strip()
    origen = f.get("origen") if isinstance(f.get("origen"), str) and ORIGEN_RE.fullmatch(f["origen"]) else "estudio"
    fuera, dentro = _tramo(f.get("fuera")), _tramo(f.get("dentro"))
    op = _num(f.get("op_dia"))
    if op is None and fuera and fuera.get("n") is not None and (fuera.get("jornadas") or 0) > 0:
        op = fuera["n"] / fuera["jornadas"]
    Rf = (fuera or {}).get("R")
    pct = _num(f.get("pct_dia"))
    if pct is None and Rf is not None and op is not None:
        pct = Rf * RIESGO_PCT * op
    rdia = _num(f.get("R_dia"))
    if rdia is None and Rf is not None and op is not None:
        rdia = Rf * op
    rob_in = f.get("robustez") if isinstance(f.get("robustez"), dict) else {}
    mit = rob_in.get("mitades")
    probadas = _ent(f.get("probadas"))
    corr = _corregida(rob_in.get("corregida"))
    if corr is None and probadas and probadas > 1:   # una de varias configuraciones y sin corrección: no la pasa
        corr = sin_corregir(probadas, (fuera or {}).get("p"))
    rob = {"sin_5_mayores": _num(rob_in.get("sin_5_mayores")),
           "dentro_positivo": (rob_in["dentro_positivo"] if isinstance(rob_in.get("dentro_positivo"), bool)
                               else (dentro["R"] > 0 if dentro and dentro.get("R") is not None else None)),
           "corregida": corr,
           "mitades": ([_num(x) for x in mit[:2]] if isinstance(mit, (list, tuple)) and len(mit) >= 2 else None)}
    for k in ("sin_mejor_mes", "sin_mejor_semana", "desliz_x2", "base_p"):
        v = _num(rob_in.get(k))
        if v is not None:
            rob[k] = v
    estado = f.get("estado") if isinstance(f.get("estado"), str) and f["estado"] in ESTADOS else "backtest"
    perfiles = {}
    if isinstance(f.get("perfiles"), dict):
        for k, v in list(f["perfiles"].items())[:10]:
            if isinstance(k, str) and isinstance(v, dict):
                perfiles[k[:40]] = {kk[:40]: _num(vv) for kk, vv in list(v.items())[:20]
                                    if isinstance(kk, str) and _num(vv) is not None}
    fecha = f.get("fecha") if isinstance(f.get("fecha"), str) and _dia(f["fecha"]) else None
    pc = _txt(f.get("perfil_costes"), 40)
    out = {"id": fid, "nombre": _txt(f.get("nombre"), 300) or fid[:300], "familia": _txt(f.get("familia"), 120),
           "origen": origen, "estudio": _txt(f.get("estudio"), 60), "regla": _txt(f.get("regla"), 2000),
           "cumple_reglas": f["cumple_reglas"] if isinstance(f.get("cumple_reglas"), bool) else None,
           "perfil_costes": pc, "perfil_texto": _txt(f.get("perfil_texto"), 200) or PERFILES_TEXTO.get(pc or ""),
           "perfil_corto": _txt(f.get("perfil_corto"), 60) or PERFILES_CORTO.get(pc or "") or (
               f"costes: {pc}" if pc else None),
           "fecha": fecha[:10] if fecha else None, "archivo": _txt(f.get("archivo"), 300),
           "dentro": dentro, "fuera": fuera, "op_dia": _r(op), "pct_dia": _r(pct), "R_dia": _r(rdia),
           "op_dia_natural": _r(f.get("op_dia_natural")), "pct_dia_natural": _r(f.get("pct_dia_natural")),
           "caida_max_pct": _r(f.get("caida_max_pct"), 2), "robustez": rob,
           "en_vivo": _vivo_limpio(f.get("en_vivo")),
           "estado": estado, "probadas": probadas, "nota": _txt(f.get("nota"), 600),
           "perfiles": perfiles or None, "igual_que": _txt(f.get("igual_que"), 300)}
    if archivo:
        out["archivo_ranking"] = archivo
    motivos = fragilidad(out)
    if f.get("fragil") is True:   # el estudio puede marcarla por otras razones (p. ej., el laboratorio vivo la descartó)
        fm = f.get("fragil_motivos") if isinstance(f.get("fragil_motivos"), list) else []
        extra = [m[:200] for m in fm if isinstance(m, str) and m.strip() and m[:200] not in motivos][:5]
        motivos += extra or ([] if motivos else ["marcada frágil por su estudio"])
    out["fragil"] = bool(motivos)
    out["fragil_motivos"] = motivos
    out["poca_muestra"] = bool(fuera and fuera.get("n") is not None and fuera["n"] < POCA_MUESTRA)
    return out


def entra(f: dict) -> bool:
    """Entra en el ranking si no pierde fuera de muestra (R neto ≥ 0) con al menos una operación y su % al día."""
    fu = f.get("fuera") or {}
    return (_num(fu.get("R")) is not None and fu["R"] >= 0 and (fu.get("n") or 0) >= 1
            and _num(f.get("pct_dia")) is not None)


def _orden(f: dict) -> tuple:
    fu = f.get("fuera") or {}
    return (-(f.get("pct_dia") or 0.0), -(f.get("op_dia") or 0.0), -(fu.get("n") or 0), f["id"])


def ordenar(filas: list[dict]) -> list[dict]:
    """Las que entran, ordenadas y con su puesto (1, 2, …); detrás, las que no entran (puesto None)."""
    dentro = sorted((f for f in filas if entra(f)), key=_orden)
    for i, f in enumerate(dentro, 1):
        f["posicion"] = i
    resto = sorted((f for f in filas if not entra(f)), key=lambda f: (_num((f.get("fuera") or {}).get("R")) is None,
                                                                      -((f.get("fuera") or {}).get("R") or 0), f["id"]))
    for f in resto:
        f["posicion"] = None
    return dentro + resto


# ---------------------------------------------------------------- laboratorio vivo (tarjetas de docs/07, 13.3)

def de_tarjeta(t: dict, periodo: dict | None = None, ts=None, probadas=None, archivo: str | None = None) -> dict | None:
    """Fila común de una tarjeta de variante del laboratorio vivo. El laboratorio da lo de un día natural (opera
    también en fin de semana): se pasa a jornadas con su periodo fuera de muestra (del corte al fin de la época; sin
    estado.json, los días que salen de n / op_dia desde el 16-08-2025)."""
    if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not t["id"].strip():
        return None
    vid = t["id"][:240]
    fu = t.get("fuera") if isinstance(t.get("fuera"), dict) else None
    de = t.get("dentro") if isinstance(t.get("dentro"), dict) else {}
    cu = t.get("cuenta") if isinstance(t.get("cuenta"), dict) else {}
    rob = t.get("robustez") if isinstance(t.get("robustez"), dict) else {}
    op_nat = _num(t.get("op_dia"))
    n_f = _ent((fu or {}).get("n"))
    per = periodo if isinstance(periodo, dict) else {}
    hoy = datetime.now(MAD).date()
    corte = _dia(per.get("corte")) or date(2025, 8, 16)
    fin = _dia(per.get("hasta"))
    if fin is None and op_nat and op_nat > 0 and n_f:
        dias = n_f / op_nat
        fin = _mas_dias(corte, max(1, min(DIAS_MAX, round(dias))) - 1 if math.isfinite(dias) else DIAS_MAX - 1)
    if fin is not None:   # un periodo roto (9999-12-31, mil años…) no puede inflar los días: hasta hoy y 10 años
        fin = min(fin, hoy, _mas_dias(corte, DIAS_MAX - 1))
    nat, jor = dias_entre(corte, fin), jornadas_entre(corte, fin)
    factor = nat / jor if nat and jor else None
    conv = (lambda x: _r(_num(x) * factor) if _num(x) is not None and factor else None)
    fuera = None
    if fu:
        fuera = {"n": n_f, "R": _num(fu.get("R")), "gana_pct": None, "p": _num(fu.get("p")), "q": _num(fu.get("q")),
                 "dsr": _num(fu.get("dsr")), "ic95": fu.get("ic95"), "desde": corte.isoformat(),
                 "hasta": fin.isoformat() if fin else None, "jornadas": jor or None, "dias": nat or None}
    q, dsr = (fuera or {}).get("q"), (fuera or {}).get("dsr")
    corr = ({"tipo": "bh", "metodo": "Benjamini-Hochberg (q) del laboratorio vivo", "valor": q, "pasa": q <= Q_MAX}
            if q is not None else {"tipo": "dsr", "metodo": "Sharpe deflactado del laboratorio vivo", "valor": dsr,
                                   "pasa": dsr >= DSR_MIN} if dsr is not None else None)
    mit = (fu or {}).get("mitades")
    clase = t.get("clase")[:40] if isinstance(t.get("clase"), str) else None
    estado = ("descartada_por_el_usuario" if clase == "descartada_usuario"
              else "en_avance" if clase in ("en_avance", "lista", "propuesta", "aceptada") else "backtest")
    av = t.get("avance") if isinstance(t.get("avance"), dict) else None
    en_vivo = None
    if av and av.get("desde"):
        en_vivo = {"fuente": "lab_vivo", "virtual": True, "n": _ent(av.get("completas")), "R": _num(av.get("R_medio")),
                   "abiertas": _ent(av.get("abiertas")), "desde": _num(av.get("desde")), "neto_usdc": None,
                   "p_holm": _num(av.get("p_holm")), "caida_max_pct": _num(av.get("caida_max_pct")),
                   "faltan": _txt(av.get("faltan"), 200)}
    hip = t.get("hipotesis") if isinstance(t.get("hipotesis"), str) else None
    m = LAB_VIVO_ID.match(hip or vid)
    fecha = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m and _dia(f"{m.group(1)}-{m.group(2)}-{m.group(3)}") else None
    if fecha is None and _num(ts):
        try:
            fecha = datetime.fromtimestamp(ts / 1000, MAD).date().isoformat()
        except (OverflowError, OSError, ValueError):
            fecha = None
    titulo = _txt(t.get("titulo"), 240) or vid
    sufijo = vid.rsplit(":", 1)[-1][:40] if ":" in vid else None
    motivos = [x[:200] for x in (t.get("motivos") if isinstance(t.get("motivos"), list) else [])
               if isinstance(x, str)][:3]
    nota = "; ".join(filter(None, [f"clase {str(clase).replace('_', ' ')}" if clase else None,
                                   ("no ha pasado: " + motivos[0]) if motivos and str(clase).startswith("descartada")
                                   else None]))
    descartada = (str(clase).startswith("descartada") and clase != "descartada_usuario") or clase in ("revocada",
                                                                                                     "invalidada")
    fr_mot = ([f"el laboratorio vivo la descartó ({motivos[0]})" if motivos else
               f"el laboratorio vivo la dejó como {str(clase).replace('_', ' ')}"] if descartada else [])
    return normalizar({
        "id": f"lab_vivo:{vid}", "nombre": f"{titulo} ({sufijo})" if sufijo else titulo, "familia": t.get("familia"),
        "origen": "laboratorio_vivo", "estudio": "lab_vivo", "regla": t.get("regla"),
        "cumple_reglas": bool(t.get("cumple_reglas")) and not t.get("fuera_de_reglas"),
        "perfil_costes": "okx_eu_con_xperps", "fecha": fecha, "archivo": archivo,
        "dentro": {"n": _ent(de.get("n")), "R": _num(de.get("R")), "gana_pct": None},
        "fuera": fuera, "op_dia": conv(op_nat) if fu else None, "pct_dia": conv(cu.get("pct_dia")) if fu else None,
        "R_dia": conv(cu.get("R_dia")) if fu else None, "op_dia_natural": op_nat if fu else None,
        "pct_dia_natural": _num(cu.get("pct_dia")) if fu else None, "caida_max_pct": cu.get("caida_max_pct"),
        "robustez": {"sin_5_mayores": rob.get("sin_5_mayores"), "corregida": corr,
                     "mitades": mit if isinstance(mit, list) else None, "sin_mejor_semana": rob.get("sin_mejor_semana"),
                     "desliz_x2": rob.get("desliz_x2"), "base_p": rob.get("base_p")},
        "en_vivo": en_vivo, "estado": estado, "probadas": probadas, "nota": nota or None,
        "fragil": bool(fr_mot), "fragil_motivos": fr_mot})


# ---------------------------------------------------------------- en vivo: paper y prueba en avance

def _clave_paper(t: dict) -> str:
    tags = t.get("tags") if isinstance(t.get("tags"), dict) else {}
    pre = tags.get("preset")
    if pre in PRESET_FILA:
        return PRESET_FILA[pre]
    if tags.get("modelo") == "ruptura3":
        return PRESET_FILA["ruptura3"]
    s = str(t.get("strategy") or "")
    if tags.get("origen") == "noticia_fiable" or s.startswith("noticia-fiable"):
        return "en_vivo:noticia_fiable"
    if s.startswith("impulso-") or tags.get("origen") == "motor_aperturas":
        return "en_vivo:motor"
    return "en_vivo:paper_otras"


def vivo_paper(trades, posiciones=None) -> dict[str, dict]:
    """Las operaciones cerradas del paper agrupadas por la fila del ranking a la que corresponden (el preset de sus
    tags; sin preset, el motor, la entrada por noticia o las demás): {fila: {n, R, gana_pct, neto_usdc, bruto_usdc,
    desde, hasta, abiertas}}."""
    g: dict[str, dict] = {}
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        x = g.setdefault(_clave_paper(t), {"n": 0, "sR": 0.0, "gana": 0, "neto": 0.0, "bruto": 0.0, "desde": None,
                                           "hasta": None, "abiertas": 0})
        x["n"] += 1
        x["sR"] += _num(t.get("R")) or 0.0
        x["gana"] += (_num(t.get("pnl")) or 0.0) > 0
        x["neto"] += _num(t.get("pnl")) or 0.0
        x["bruto"] += _num(t.get("bruto")) if _num(t.get("bruto")) is not None else (_num(t.get("pnl")) or 0.0)
        for k, fn in (("desde", min), ("hasta", max)):
            v = _num(t.get("opened" if k == "desde" else "closed"))
            if v is not None:
                x[k] = v if x[k] is None else fn(x[k], v)
    for p in posiciones or []:
        if isinstance(p, dict):
            x = g.setdefault(_clave_paper(p), {"n": 0, "sR": 0.0, "gana": 0, "neto": 0.0, "bruto": 0.0, "desde": None,
                                               "hasta": None, "abiertas": 0})
            x["abiertas"] += 1
            v = _num(p.get("opened"))
            if v is not None:
                x["desde"] = v if x["desde"] is None else min(x["desde"], v)
    return {k: {"fuente": "paper", "virtual": False, "n": x["n"], "R": round(x["sR"] / x["n"], 4) if x["n"] else None,
                "gana_pct": round(100 * x["gana"] / x["n"], 1) if x["n"] else None, "neto_usdc": round(x["neto"], 2),
                "bruto_usdc": round(x["bruto"], 2), "desde": int(x["desde"]) if x["desde"] else None,
                "hasta": int(x["hasta"]) if x["hasta"] else None, "abiertas": x["abiertas"]}
            for k, x in g.items()}


def _periodo_txt(s) -> tuple[date | None, date | None]:
    m = re.findall(r"\d{4}-\d{2}-\d{2}", s or "") if isinstance(s, str) else []
    return (_dia(m[0]), _dia(m[1])) if len(m) >= 2 else (None, None)


def vivo_vigilancia(vig) -> list[tuple[str, dict, dict]]:
    """[(fila, estrategia, en_vivo)] de la prueba en avance virtual (mensaje «vigilancia», 2.8): la fila es la del
    laboratorio de 2.000 con el mismo id (o la de VIGILANCIA_FILA por su código)."""
    out = []
    for s in (vig or {}).get("estrategias") or [] if isinstance(vig, dict) else []:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str):
            continue
        v = s.get("vivo") if isinstance(s.get("vivo"), dict) else {}
        gan = _num(v.get("ganadoras"))
        ev = {"fuente": "vigilancia", "virtual": True, "n": _ent(v.get("operaciones")) or 0, "R": _num(v.get("R_medio")),
              "gana_pct": round(gan * 100, 1) if gan is not None and gan <= 1 else gan,
              "abiertas": _ent(v.get("abiertas")) or 0, "desde": _num(s.get("desde")), "neto_usdc": None,
              "R_total": _num(v.get("R_total"))}
        fid = VIGILANCIA_FILA.get(s.get("codigo")) or f"lab2000:{s['id']}"
        out.append((fid, s, ev))
    return out


def _fila_vigilancia(s: dict) -> dict | None:
    """Fila propia de una estrategia de la prueba en avance que no está en ningún estudio (p. ej., la variante de 7 días
    de E1): su backtest fuera de muestra, si lo trae."""
    bt = s.get("backtest") if isinstance(s.get("backtest"), dict) else None
    fuera, op = None, None
    if bt and _ent(bt.get("operaciones")):
        a, b = _periodo_txt(bt.get("periodo"))
        jor = jornadas_entre(a, b) if a and b else 0
        gan = _num(bt.get("ganadoras"))
        fuera = {"n": _ent(bt.get("operaciones")), "R": _num(bt.get("R_medio")),
                 "gana_pct": round(gan * 100, 1) if gan is not None and gan <= 1 else gan, "jornadas": jor or None}
        op = fuera["n"] / jor if jor else None
    reglas = s.get("reglas") if isinstance(s.get("reglas"), dict) else {}
    sl, r = _num(reglas.get("sl_pct")), _num(reglas.get("r"))
    cumple = (sl is not None and r is not None and sl >= 1.5 and r >= 2 and (_num(reglas.get("neto_pct")) or 0) >= 3)
    return normalizar({"id": f"vigilancia:{s.get('codigo') or s['id']}", "nombre": s.get("nombre") or s.get("codigo"),
                       "familia": None, "origen": "en_vivo", "estudio": "vigilancia", "regla": s.get("nombre"),
                       "cumple_reglas": cumple, "perfil_costes": "laboratorio", "fuera": fuera, "op_dia": op,
                       "estado": "en_avance",
                       "nota": "Solo en la prueba en avance virtual (vigilancia): no sale de ningún estudio validado"})


def preset_activo(openings, activa_path: Path | None = None) -> dict:
    """{preset, nombre, desde, fuente}: la estrategia que opera el motor (estado «openings»: params.preset o
    estrategia_cargada.preset); sin él, la de DATA_DIR/estrategia_activa.json (solo lectura)."""
    o = openings if isinstance(openings, dict) else {}
    p = o.get("params") if isinstance(o.get("params"), dict) else {}
    car = o.get("estrategia_cargada") if isinstance(o.get("estrategia_cargada"), dict) else {}
    txt = (lambda x, n=40: x[:n] if isinstance(x, str) and x else None)
    if o:
        return {"preset": txt(p.get("preset")) or txt(car.get("preset")), "nombre": txt(p.get("nombre"), 200),
                "desde": _num(car.get("ts")), "fuente": "motor", "pausada": o.get("paused") is True}
    if activa_path is not None:
        try:
            a = json.loads(Path(activa_path).read_text(encoding="utf-8"))
        except Exception:   # que falte, esté a medias o roto: como si no estuviera
            a = None
        if isinstance(a, dict):
            return {"preset": txt(a.get("preset")), "nombre": txt(a.get("nombre"), 200), "desde": _num(a.get("desde")),
                    "fuente": "estrategia_activa.json", "pausada": a.get("pausada") is True}
    return {"preset": None, "nombre": None, "desde": None, "fuente": None, "pausada": False}


def ligera(f: dict | None) -> dict | None:
    """La fila sin lo que pesa (perfiles y archivo del ranking) para el bus y para en_marcha."""
    if not isinstance(f, dict):
        return None
    return {k: v for k, v in f.items() if k not in ("perfiles", "archivo_ranking")}


# ---------------------------------------------------------------- el ranking

class Ranking:
    def __init__(self, bus=None, get_resultados=None, get_openings=None, get_vigilancia=None, get_cartera=None,
                 carpeta: Path | None = None, lab: LabVivoLectura | None = None, lab_dir: Path | None = None,
                 estado_path: Path | None = None, activa_path: Path | None = None, clock=now_ms):
        self.bus = bus
        st = (lambda k: (lambda: (bus.state.get(k) if bus is not None else None)))
        self.get_resultados = get_resultados
        self.get_openings = get_openings or st("openings")
        self.get_vigilancia = get_vigilancia or st("vigilancia")
        self.get_cartera = get_cartera or st("portfolio")
        self.carpeta = Path(carpeta) if carpeta is not None else config.BACKTEST_DIR / "ranking"
        self.lab = lab if lab is not None else LabVivoLectura(dir=lab_dir)
        self.estado_path = Path(estado_path) if estado_path is not None else config.DATA_DIR / "ranking_estado.json"
        self.activa_path = Path(activa_path) if activa_path is not None else config.DATA_DIR / "estrategia_activa.json"
        self.clock = clock
        self._lock = threading.Lock()
        self._archivos: dict[str, tuple] = {}   # nombre → ((mtime_ns, tamaño), filas, error, mtime_ms)
        self._estado = self._cargar_estado()
        self._ultimo: tuple | None = None        # (monotónico, foto, resultado)
        self._clave_bus = None
        self._lab_malas: list[str] = []

    # ---- estado (lo visto, para «nueva» y el puesto de antes)

    def _cargar_estado(self) -> dict | None:
        try:
            d = json.loads(self.estado_path.read_text(encoding="utf-8"))
        except Exception:   # que falte o esté roto: se empieza de cero (lo que hay se da por visto)
            return None
        if not isinstance(d, dict):
            return None
        sec = (lambda k: d.get(k) if isinstance(d.get(k), dict) else {})
        ok = (lambda v: not isinstance(v, bool) and isinstance(v, (int, float)) and _num(v) is not None and v >= 0)
        return {"vistas": {k: v for k, v in sec("vistas").items() if ok(v)},
                "actual": {k: v for k, v in sec("actual").items() if isinstance(v, int) and ok(v)},
                "anterior": {k: v for k, v in sec("anterior").items() if isinstance(v, int) and ok(v)},
                "cambio": d.get("cambio") if _num(d.get("cambio")) is not None else None}

    def _guardar_estado(self) -> None:
        try:
            self.estado_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.estado_path.with_name(self.estado_path.name + ".tmp")
            tmp.write_text(json.dumps({"formato": 1, "ts": self.clock(), **self._estado}, ensure_ascii=False,
                                      separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.estado_path)
        except OSError as e:
            log.warning("Ranking: no pude guardar %s (%s): «nueva» y el puesto de antes no sobreviven a un reinicio",
                        self.estado_path, e)

    # ---- fuentes

    def _leer_estandar(self) -> tuple[list[dict], dict]:
        """Filas de BACKTEST_DIR/ranking/*.json: solo se vuelve a leer el archivo que cambió (mtime y tamaño)."""
        info = {"carpeta": str(self.carpeta), "archivos": 0, "filas": 0, "errores": [], "ts": None}
        try:
            entradas = [e for e in os.scandir(self.carpeta) if e.name.endswith(".json")
                        and not e.name.startswith(("_", ".")) and e.is_file()]
        except OSError:
            self._archivos.clear()
            info["estado"] = "sin carpeta"
            return [], info
        vistos = set()
        for e in entradas:
            try:
                st = e.stat()
            except OSError:
                continue
            vistos.add(e.name)
            clave = (st.st_mtime_ns, st.st_size)
            hit = self._archivos.get(e.name)
            if hit and hit[0] == clave:
                continue
            filas, err = [], None
            try:
                d = json.loads(Path(e.path).read_text(encoding="utf-8"))
            except Exception as ex:   # que no se pueda leer, JSON roto o tan anidado que no cabe (RecursionError)
                d, err = None, f"{e.name}: no se puede leer ({type(ex).__name__}: {str(ex)[:120]})"
            if err is None:
                crudas = (d.get("filas") if isinstance(d, dict) and isinstance(d.get("filas"), list)
                          else [d] if isinstance(d, dict) else d if isinstance(d, list) else [])
                malas = []
                for i, x in enumerate(crudas):
                    try:   # una fila rota se salta y se apunta: nunca tumba el ranking
                        f = normalizar(x, e.name)
                    except Exception as ex:
                        malas.append(f"fila {i + 1} ({type(ex).__name__}: {str(ex)[:80]})")
                        continue
                    if f:
                        filas.append(f)
                if malas:
                    err = f"{e.name}: {len(malas)} {'fila no vale' if len(malas) == 1 else 'filas no valen'}: " + \
                          "; ".join(malas[:3])
                elif not filas:
                    err = f"{e.name}: sin ninguna fila válida (hace falta al menos «id»)"
            self._archivos[e.name] = (clave, filas, err, int(st.st_mtime * 1000))
        for n in [n for n in self._archivos if n not in vistos]:
            del self._archivos[n]
        por_id: dict[str, tuple] = {}
        dup = 0
        for n, (_, filas, err, mt) in sorted(self._archivos.items()):
            if err:
                info["errores"].append(err)
            for f in filas:
                prev = por_id.get(f["id"])
                if prev is not None:
                    dup += 1
                    if prev[0] > mt:
                        continue
                por_id[f["id"]] = (mt, f)
            info["ts"] = mt if info["ts"] is None else max(info["ts"], mt)
        info.update(archivos=len(self._archivos), filas=len(por_id), duplicadas=dup)
        info["errores"] = info["errores"][:10]
        return [f for _, f in por_id.values()], info

    def _leer_lab(self) -> tuple[list[dict], dict]:
        try:
            d = self.lab.tarjetas()
        except Exception as e:
            log.exception("Ranking: no pude leer las tarjetas del laboratorio vivo")
            return [], {"estado": "error", "filas": 0, "error": str(e)[:160]}
        filas, malas = [], []
        for t, ts, archivo in d.get("tarjetas") or []:
            try:   # una tarjeta rota se salta y se apunta: nunca tumba el ranking
                f = de_tarjeta(t, d.get("periodo"), ts, d.get("probadas"), archivo)
            except Exception as ex:
                vid = t.get("id") if isinstance(t, dict) and isinstance(t.get("id"), str) else "?"
                malas.append(f"{archivo}: tarjeta {vid[:80]} ({type(ex).__name__}: {str(ex)[:80]})")
                continue
            if f:
                filas.append(f)
        info = {"carpeta": d.get("carpeta"), "estado": d.get("estado"), "filas": len(filas), "ts": d.get("ts")}
        if malas:
            info["errores"] = malas[:10]
            if malas != self._lab_malas:   # se avisa cuando cambia, no en cada cálculo
                log.warning("Ranking: %d tarjetas del laboratorio vivo no valen (%s)", len(malas), malas[0])
        self._lab_malas = malas
        return filas, info

    def foto_vivo(self) -> dict:
        """Lo de en vivo, leído en el bucle (no desde un hilo): operaciones cerradas del paper, posiciones abiertas,
        estado del motor y prueba en avance. Cualquiera puede faltar."""
        out = {"trades": None, "posiciones": None, "openings": None, "vigilancia": None}
        try:
            if self.get_resultados is not None:
                res = self.get_resultados() or {}
                out["trades"] = [{k: t.get(k) for k in ("strategy", "tags", "R", "pnl", "bruto", "opened", "closed")}
                                 for t in res.get("trades") or [] if isinstance(t, dict)]
            else:
                pf = self.get_cartera() or {}
                out["trades"] = [{k: t.get(k) for k in ("strategy", "tags", "R", "pnl", "bruto", "opened", "closed")}
                                 for t in pf.get("closed") or [] if isinstance(t, dict)]
            pf = self.get_cartera() or {}
            out["posiciones"] = [{k: p.get(k) for k in ("strategy", "tags", "opened")}
                                 for p in pf.get("positions") or [] if isinstance(p, dict)]
        except Exception:
            log.exception("Ranking: no pude leer las operaciones del paper")
        try:   # del estado «openings», solo lo que dice qué estrategia opera
            o = self.get_openings()
            if isinstance(o, dict) and o:
                p = o.get("params") if isinstance(o.get("params"), dict) else {}
                out["openings"] = {"params": {"preset": p.get("preset"), "nombre": p.get("nombre")},
                                   "estrategia_cargada": o.get("estrategia_cargada"), "paused": o.get("paused")}
        except Exception:
            log.exception("Ranking: no pude leer el estado del motor de aperturas")
        try:   # de la prueba en avance, las estrategias sin sus operaciones
            v = self.get_vigilancia()
            if isinstance(v, dict) and v:
                out["vigilancia"] = {"ts": v.get("ts"), "estado": v.get("estado"),
                                     "estrategias": [{k: s.get(k) for k in ("id", "codigo", "nombre", "desde", "vivo",
                                                                            "backtest", "reglas")}
                                                     for s in v.get("estrategias") or [] if isinstance(s, dict)]}
        except Exception:
            log.exception("Ranking: no pude leer la prueba en avance")
        return out

    # ---- cálculo

    def calcular(self, vivo: dict | None = None) -> dict:
        """El ranking entero (todas las filas, ordenadas) con lo de en vivo de la foto. Se puede llamar desde un hilo."""
        vivo = vivo or {}
        with self._lock:
            now = self.clock()
            estandar, inf_est = self._leer_estandar()
            labv, inf_lab = self._leer_lab()
            por_id: dict[str, dict] = {}
            for f in estandar + labv:
                por_id[f["id"]] = dict(f)
            act = preset_activo(vivo.get("openings"), self.activa_path)
            fid_activa = PRESET_FILA.get(act.get("preset"))
            # paper
            pap = vivo_paper(vivo.get("trades"), vivo.get("posiciones"))
            for fid, ev in pap.items():
                f = por_id.get(fid)
                if f is None:
                    f = normalizar({"id": fid, "nombre": EN_VIVO_NOMBRES.get(fid) or act.get("nombre") or fid,
                                    "origen": "en_vivo", "estudio": "paper", "perfil_costes": "paper",
                                    "nota": "Solo en el paper: sin backtest fuera de muestra"})
                    por_id[fid] = f
                f["en_vivo"] = ev
            # prueba en avance (vigilancia)
            for fid, s, ev in vivo_vigilancia(vivo.get("vigilancia")):
                f = por_id.get(fid)
                if f is None:
                    f = _fila_vigilancia(s)
                    if f is None:
                        continue
                    fid = f["id"]
                    por_id.setdefault(fid, f)
                    f = por_id[fid]
                if f.get("en_vivo") and f["en_vivo"].get("fuente") == "paper":
                    f["en_vivo"] = {**f["en_vivo"], "avance": ev}
                else:
                    f["en_vivo"] = ev
                if f["estado"] == "backtest":
                    f["estado"] = "en_avance"
            # la que está en marcha
            if fid_activa and fid_activa not in por_id:
                por_id[fid_activa] = normalizar({
                    "id": fid_activa, "nombre": act.get("nombre") or fid_activa, "origen": "en_vivo",
                    "estudio": "paper", "perfil_costes": "paper",
                    "nota": "Sin su backtest en el ranking (falta su archivo en BACKTEST_DIR/ranking)"})
            for f in por_id.values():
                f["en_marcha"] = f["id"] == fid_activa
                if f["en_marcha"] and f["estado"] != "descartada_por_el_usuario":
                    f["estado"] = "en_paper"
            filas = ordenar(list(por_id.values()))
            self._marcar(filas, now)
            en_ranking = [f for f in filas if f["posicion"]]
            em = self._en_marcha(por_id.get(fid_activa) if fid_activa else None, act, len(en_ranking))
            n_paper = sum(e["n"] for e in pap.values())
            vig = vivo.get("vigilancia") if isinstance(vivo.get("vigilancia"), dict) else {}
            fuentes = {"estandar": inf_est, "lab_vivo": inf_lab,
                       "paper": {"operaciones": n_paper, "filas": len(pap), "ts": now if vivo.get("trades") is not None
                                 else None},
                       "vigilancia": {"estrategias": len(vig.get("estrategias") or []), "ts": vig.get("ts"),
                                      "estado": vig.get("estado") or ("apagada" if not vig else None)}}
            return {"ts": now, "n_total": len(filas), "n_ranking": len(en_ranking), "filas": filas, "en_marcha": em,
                    "fuentes": fuentes, "nota": NOTA, "criterio": CRITERIO}

    def _marcar(self, filas: list[dict], now: int) -> None:
        """Puesto de antes, «nueva» (entró en las últimas 24 h) y desde cuándo está. En el primer cálculo sin
        ranking_estado.json, todo lo que hay se da por visto (ninguna sale como nueva)."""
        primera = self._estado is None
        st = self._estado or {"vistas": {}, "actual": {}, "anterior": {}, "cambio": None}
        cambio = primera
        pos = {f["id"]: f["posicion"] for f in filas if f["posicion"]}
        for fid in pos:
            if fid not in st["vistas"]:
                st["vistas"][fid] = 0 if primera else now
                cambio = True
        if pos != st["actual"]:
            st["anterior"], st["actual"], st["cambio"] = st["actual"], pos, now
            cambio = True
        for f in filas:
            v = st["vistas"].get(f["id"])
            f["entra_desde"] = int(v) if v else None
            f["nueva"] = bool(f["posicion"] and v and now - v < NUEVA_MS)
            f["posicion_antes"] = st["anterior"].get(f["id"]) if f["posicion"] else None
        if len(st["vistas"]) > 50_000:   # que no crezca sin fin: fuera las más viejas que ya no están
            for k in sorted((k for k in st["vistas"] if k not in pos), key=lambda k: st["vistas"][k])[:10_000]:
                del st["vistas"][k]
        self._estado = st
        if cambio:
            self._guardar_estado()

    def _en_marcha(self, f: dict | None, act: dict, n_ranking: int) -> dict:
        preset = act.get("preset")
        out = {"id": f["id"] if f else PRESET_FILA.get(preset), "preset": preset,
               "nombre": (f or {}).get("nombre") or act.get("nombre"), "desde": act.get("desde"),
               "pausada": act.get("pausada"), "fuente": act.get("fuente"), "posicion": None, "n_ranking": n_ranking,
               "en_ranking": False, "fila": ligera(f)}
        if f is None:
            out["texto"] = ("No se sabe qué estrategia opera: el motor aún no ha publicado su estado" if not act.get("fuente")
                            else "No está en el ranking: la estrategia del motor no es un preset con backtest "
                                 "(parámetros cambiados por la evolución)")
            return out
        fu = f.get("fuera") or {}
        R = _num(fu.get("R"))
        if f.get("posicion"):
            out.update(posicion=f["posicion"], en_ranking=True,
                       texto=f"Puesto {f['posicion']} de {n_ranking} en el ranking")
        elif R is None:
            out["texto"] = "Fuera del ranking: sin medida fuera de muestra"
        elif R < 0:
            out["texto"] = f"Fuera del ranking: pierde fuera de muestra ({_n(R, 3, True)} R por operación)"
        else:   # no pierde, pero no se puede poner en su puesto (sin operaciones, o sin las de cada jornada)
            falta = ("no tiene operaciones fuera de muestra" if not fu.get("n")
                     else "le faltan las operaciones por jornada para calcular su % de la cuenta al día")
            out["texto"] = f"Fuera del ranking: no pierde ({_n(R, 3, True)} R por operación), pero {falta}"
        return out

    def resultado(self, vivo: dict | None = None) -> dict:
        """El último cálculo si es de hace menos de CACHE_S con la misma foto; si no, uno nuevo."""
        try:
            clave = json.dumps(vivo, sort_keys=True, ensure_ascii=False, default=str) if vivo else ""
        except (TypeError, ValueError):   # una foto que no se puede comparar: sin caché
            clave = None
        u = self._ultimo
        if u and clave is not None and time.monotonic() - u[0] < CACHE_S and u[1] == clave:
            return u[2]
        res = self.calcular(vivo)
        self._ultimo = (time.monotonic(), clave, res)
        return res

    def consulta(self, vivo: dict | None = None, origen: str | None = None, cumple: bool | None = None,
                 min_op_dia: float | None = None, limite: int = LIMITE_DEF, fragil: bool | None = None,
                 todas: bool = False) -> dict:
        """GET /api/ranking: {ts, n_total, n_ranking, n_filtradas, filas, en_marcha, fuentes, nota, criterio, origenes,
        filtros}. Los puestos son los del ranking entero (un filtro no los cambia). Con todas, también las que no
        entran (puesto null), detrás."""
        res = self.resultado(vivo)
        origenes = [o for o in (origen.split(",") if origen else []) if o]
        filas = [f for f in res["filas"] if todas or f["posicion"]]
        if origenes:
            filas = [f for f in filas if f["origen"] in origenes]
        if cumple is not None:
            filas = [f for f in filas if f.get("cumple_reglas") is cumple]
        if min_op_dia is not None:
            filas = [f for f in filas if (f.get("op_dia") or 0.0) >= min_op_dia]
        if fragil is not None:
            filas = [f for f in filas if f["fragil"] is fragil]
        limite = max(1, min(LIMITE_MAX, int(limite)))
        cuenta: dict[str, list] = {}
        costes: dict[str, list] = {}
        for f in res["filas"]:
            c = cuenta.setdefault(f["origen"], [0, 0])
            c[0] += 1
            c[1] += bool(f["posicion"])
            if f["posicion"]:
                k = f.get("perfil_costes") or "?"
                costes.setdefault(k, [0, f.get("perfil_texto") or PERFILES_TEXTO.get(k)])[0] += 1
        return {"ts": res["ts"], "n_total": res["n_total"], "n_ranking": res["n_ranking"], "n_filtradas": len(filas),
                "filas": filas[:limite], "en_marcha": res["en_marcha"], "fuentes": res["fuentes"], "nota": res["nota"],
                "criterio": res["criterio"],
                "origenes": [{"id": k, "nombre": ORIGENES.get(k, k.replace("_", " ")), "n": v[0], "n_ranking": v[1]}
                             for k, v in sorted(cuenta.items(), key=lambda kv: -kv[1][1])],
                "perfiles_costes": [{"id": k, "texto": v[1], "n_ranking": v[0]}
                                    for k, v in sorted(costes.items(), key=lambda kv: -kv[1][0])],
                "filtros": {"origen": origenes or None, "cumple": cumple, "min_op_dia": min_op_dia, "fragil": fragil,
                            "limite": limite, "todas": todas}}

    @staticmethod
    def firma(out: dict) -> str:
        """Firma del contenido de una respuesta de consulta() sin sus horas de cálculo (ts): la misma firma, lo mismo.
        GET /api/ranking la manda como ETag y contesta 304 si la web ya lo tiene (la tabla entera pesa ~2 MB)."""
        fu = {k: ({kk: vv for kk, vv in v.items() if kk != "ts"} if isinstance(v, dict) else v)
              for k, v in (out.get("fuentes") or {}).items()}
        base = {**{k: v for k, v in out.items() if k not in ("ts", "fuentes")}, "fuentes": fu}
        txt = json.dumps(base, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(txt.encode("utf-8")).hexdigest()[:24]

    # ---- mensaje del bus

    def mensaje(self, res: dict) -> dict:
        """{type: "ranking", …} con las 20 primeras (sin lo que pesa)."""
        top = [ligera(f) for f in res["filas"] if f["posicion"]][:TOP_BUS]
        return {"type": "ranking", "ts": res["ts"], "n_total": res["n_total"], "n_ranking": res["n_ranking"],
                "filas": top, "en_marcha": res["en_marcha"], "nota": res["nota"],
                "fuentes": {k: {kk: v.get(kk) for kk in ("ts", "filas", "estado") if kk in v}
                            for k, v in res["fuentes"].items() if isinstance(v, dict)}}

    def _clave(self, msg: dict) -> str:
        em = msg.get("en_marcha") or {}
        top = [(f["id"], f["posicion"], f["pct_dia"], f["op_dia"], f["estado"], f["nueva"],
                (f.get("en_vivo") or {}).get("n"), (f.get("en_vivo") or {}).get("R")) for f in msg["filas"]]
        return json.dumps([top, msg["n_total"], msg["n_ranking"], em.get("id"), em.get("posicion"), em.get("texto"),
                           ((em.get("fila") or {}).get("en_vivo") or {}).get("n")], ensure_ascii=False, default=str)

    def publicar(self, res: dict) -> bool:
        """Publica «ranking» si cambió desde el último publicado."""
        if self.bus is None:
            return False
        msg = self.mensaje(res)
        clave = self._clave(msg)
        if clave == self._clave_bus:
            return False
        self._clave_bus = clave
        self.bus.publish(msg)
        return True

    async def run(self) -> None:
        """Al arrancar y cada 60 s: recalcula (en un hilo) y publica «ranking» si cambió."""
        while True:
            try:
                foto = self.foto_vivo()
                res = await asyncio.to_thread(self.resultado, foto)
                self.publicar(res)
            except Exception:
                log.exception("Ranking: el cálculo falló; sigue con el último")
            await asyncio.sleep(BUS_CADA_S)
