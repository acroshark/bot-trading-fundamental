"""Pasa los estudios ya hechos a filas comunes del ranking de estrategias (bot/ranking.py; contrato en docs/04, 2.20).

Uso:
    M:\\BotFundamental-runtime\\venv\\Scripts\\python.exe -X utf8 scripts\\ranking_importar.py
    (--seco: solo dice qué haría; --origen DIR: otro BACKTEST_DIR; --destino DIR: otra carpeta de filas;
     --limpiar: quita también las filas de los estudios cuyo archivo ya no está; --demo [RUTA]: rehace también las
     filas reales del ejemplo del ranking de la web, el bloque rankingData de dashboard/radar.html)

Lee BACKTEST_DIR en SOLO LECTURA y solo escribe en BACKTEST_DIR/ranking/ (y, con --demo, el bloque rankingData de la
web): una fila por archivo, con nombre
«<estudio>--<id>-<firma>.json», y _importado.json (resumen; el ranking no lo lee). Es idempotente: con los mismos estudios
deja los mismos archivos y no reescribe los que no cambian (el bot no los vuelve a leer); quita los suyos que ya no salen.
Cifras NETAS de costes, con el perfil con que se calcularon (perfil_costes):
- laboratorio.json (las 2.000): las que no pierden fuera de muestra y las prometedoras (perfil «laboratorio»: 0,10 % en
  contado, 0,05 % en perpetuos, 2 pb y 5-20 pb más en los stops). Nombre y regla con bot.laboratorio.generate (la lista
  es determinista). E2 (apertura.LAB_REF) va como descartada por el usuario.
- impulso_7d.json (E2 con 7 días): descartada por el usuario (29-10-2026) y frágil: su salida se eligió después de ver
  los datos (corrección «sin_p», con la q de E2 en el laboratorio en el texto).
- seis_diarias_resumen.json, seis2_resumen.json y ruptura3_resumen.json: las réplicas del motor (0,10 % por lado y 2 pb,
  también en los perpetuos: perfil «backtest»), con lo que sale de su curva (sin las 5 mayores, mitades y caída de la
  cuenta). La Ruptura V3 de controles/selectiva es la misma (operación a operación): de allí, su base y sus mitades.
- controles/selectiva/medir_selectiva.json: las 23 configuraciones (Murphy, Fibonacci y Ruptura con sus salidas), con
  su p frente a la base emparejada corregido por Holm entre las 23.
- controles/curso/medir_curso.json: toda la tabla menos la referencia (que es «6 diarias v2»), con Holm entre sus p.
- controles/salidas_motor.json (378) y controles/salidas_1a1.json (las 1:1; la actual y la A ya están en salidas_motor):
  sin p, cada una es una de N salidas sobre las mismas entradas (corrección «sin_p»: frágiles).
Cada corrección lleva su tipo corto (holm, bh o sin_p) para la web y el método largo en «metodo».
- controles/costes/bruto_neto.json: los perfiles de «6 diarias v2» y de la Ruptura V3 (sin costes, backtest, los de hoy
  del paper y sin X-Perps), en «perfiles».
- controles/anchos/medir_anchos.json, si ya existe (el de _pruebas no): perfil «backtest» y los demás en «perfiles»; lo
  que ya estaba (las referencias y los objetivos fijos de salidas_motor) no se repite.
Jornada = día de lunes a viernes con aperturas (salvo el 1 de enero): 306 en el fuera de muestra de los estudios.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import config, ranking  # noqa: E402

MAD = ZoneInfo("Europe/Madrid")
RIESGO = ranking.RIESGO_PCT
POR_DEFECTO = {"jornadas": {"dentro": 468, "fuera": 306}, "dias": {"dentro": 656, "fuera": 431},
               "periodo": {"dentro": ("2023-10-30", "2025-08-15"), "fuera": ("2025-08-16", "2026-10-20")}}
LAB_REF = "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig"   # apertura.LAB_REF: E2
COSTE_RT_LAB = 0.24       # ida y vuelta del contado en el laboratorio y en la regla del 3 % neto (0,10 % y 2 pb por lado)
DUR_CORTO = {"sig": "hasta la apertura siguiente", "24h": "como mucho 24 h", "7d": "7 días de seguridad"}
ESTUDIOS = ("laboratorio_2000", "impulso_7d", "seis", "seis2", "ruptura3", "selectiva", "curso", "salidas_motor",
            "salidas_1a1", "anchos")
CURSO_TECNICAS = {"MC": "Mini golden cross de 4 h", "FS": "Fibosi: extensiones desde el primer rebote",
                  "SN": "Vender un poco antes del nivel", "PV": "Pivotes como soportes y resistencias",
                  "PR": "Rejilla «real» de pivotes", "RR": "Segunda oportunidad: límite en el R3/S3 roto",
                  "PN": "Entrada en el P del periodo siguiente", "CP": "Pivotes que se encogen",
                  "CR": "Calidad de la ruptura", "AG": "Agotamiento: roturas seguidas",
                  "GR": "Giro en R1/S1 tras toques repetidos", "seis2": "6 diarias v2"}
CURSO_USOS = (("(a) estrategia propia", "a"), ("(a) filtro sobre", "a_filtro"), ("(b) filtro sobre", "b_filtro"),
              ("(b) veto sobre", "b_veto"), ("(b) esperar", "b_espera"))
SELECTIVA = {"murphy": ("«Murphy»: triple pantalla (4 h · 1 h · 5 min)", "Retroceso en tendencia (Murphy)",
                        "Tendencia clara en 4 h y en 1 h (medias de 20 y 50 alineadas, ADX a favor); en la liquidez de "
                        "una apertura, retroceso a la media de 20 de 1 h o al nivel roto y vela de 5 min que confirma "
                        "con volumen"),
             "fibo": ("«Fibo-tendencia en aperturas»", "Retroceso de Fibonacci en tendencia",
                      "Tendencia clara (diaria y de 4 h, ADX de 4 h a favor); con el empuje de la apertura, entra en el "
                      "giro de un retroceso del 38,2-61,8 % del último tramo, stop detrás del 78,6 % (≥ 2 %), como "
                      "mucho 2 monedas por apertura"),
             "ruptura": ("Ruptura con potencia (RP)", "Ruptura del rango de apertura",
                         "Régimen cripto a favor, tendencia diaria y de 4 h con ADX ≥ 25; una vela de 5 min cierra fuera "
                         "del rango de apertura con volumen ≥ 1,5 × y rango verdadero ≥ 1,5 ATR; stop máx(2 %, el otro "
                         "lado del rango) hasta el 3 %, como mucho 2 por apertura")}
SELECTIVA_VAR = {("murphy", "V1"): "V1, la estrategia: ADX ≥ 25 y volumen ≥ 1,0 × el rango",
                 ("murphy", "V2"): "V2, potencia laxa: ADX ≥ 20", ("murphy", "V3"): "V3, potencia estricta: ADX ≥ 30 "
                 "y volumen ≥ 1,5 ×", ("murphy", "V4"): "V4: zona de retroceso de Fibonacci",
                 ("fibo", "V1"): "V1, la principal", ("fibo", "V2"): "V2: stop detrás del origen del tramo",
                 ("fibo", "V3"): "V3: objetivo en la extensión del 161,8 %",
                 ("fibo", "V4"): "V4, más selectiva: ADX ≥ 25, volumen relativo ≥ 1,5 y 1 moneda por apertura",
                 ("ruptura", "V1"): "V1 «RP-15»: rango de 15 min, disparo hasta +120 min",
                 ("ruptura", "V2"): "V2 «RP-30»: rango de 30 min",
                 ("ruptura", "V3"): "V3 «RP-15 tarde»: disparo hasta +240 min",
                 ("ruptura", "V4"): "V4 «RP-15 Fibonacci»: límite en el retroceso del 38,2 %"}
SALIDA_TXT = {"A": "salida A: la mitad en 3R y el resto con el cerrojo (a +3R el stop sube al suelo y sigue a 2R; "
                   "24 h sin activar y 7 días activado)",
              "H6": "horquilla: stop 3 %, objetivo 6R, como mucho 7 días",
              "FIB": "horquilla con el objetivo en la extensión del 161,8 % (de 2R a 6R), como mucho 72 h",
              "NIV": "salida en los niveles del autor: la mitad en T2 y el resto en T3 (menos 0,25 %), 7 días"}
SEIS_ENTRADAS = ("Entradas de «6 diarias»: impulso de los 15 primeros min de cada apertura (cualquier movimiento ≠ 0, "
                 "sin filtros), entrada a +15 min, en las 4 monedas y las 5 aperturas; ")
MOTOR = {"seis": ("seis_diarias_resumen.json", "seis", "6 diarias", "Impulso de apertura"),
         "seis2": ("seis2_resumen.json", "seis2", "6 diarias v2", "Impulso de apertura"),
         "ruptura3": ("ruptura3_resumen.json", "ruptura3", "Ruptura V3: rango de apertura con tendencia, volumen y "
                                                           "régimen", "Ruptura del rango de apertura"),
         "e2_impulso_7d": ("impulso_7d.json", "impulso", "E2 · impulso (variante de 7 días)", "Impulso de apertura")}
NOTAS_MOTOR = {
    "seis": "Réplica del motor con el preset «6 diarias» (3 años). Pierde fuera de muestra.",
    "seis2": "Réplica del motor con el preset «6 diarias v2» (3 años). Pierde dentro y fuera de muestra: los costes se "
             "llevan más de lo que gana antes de pagarlos.",
    "ruptura3": "Réplica del motor operación a operación (scripts/replica_ruptura3.py). No pasó su puerta prerregistrada "
                "(la V1, frente a su base, no llega) y es la mejor fuera de muestra de 23 configuraciones medidas ese "
                "día: cifra optimista. Fuera gana sobre todo por los cortos de 2026; dentro pierde.",
    "e2_impulso_7d": "Descartada por el usuario el 29-10-2026 («poco beneficio, pocas operaciones»). La salida de 7 días se "
                     "eligió después de ver los datos: el laboratorio nunca la validó."}
E2_7D_MOTIVO = "la salida de 7 días se eligió después de ver los datos: no está validada"


# ---------------------------------------------------------------- utilidades

def leer(ruta: Path):
    try:
        return json.loads(Path(ruta).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def num(x) -> float | None:
    return ranking._num(x)


def r4(x, d: int = 4) -> float | None:
    v = num(x)
    return round(v, d) if v is not None else None


def fecha(x) -> str | None:
    """'AAAA-MM-DD' de ms, de ISO o de 'DD-MM-AAAA HH:MM'."""
    if num(x) is not None and x > 10 ** 11:
        return datetime.fromtimestamp(x / 1000, MAD).date().isoformat()
    if isinstance(x, str):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", x)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.match(r"^(\d{2})-(\d{2})-(\d{4})", x)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def por_operacion(curva) -> list[float]:
    """R de cada operación desde la curva de R acumulado [[t, R acumulado], …]."""
    out, prev = [], 0.0
    for p in curva or []:
        try:
            v = float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        out.append(v - prev)
        prev = v
    return out


def sin_5(rs: list[float]) -> float | None:
    """R medio sin las 5 operaciones con más R (None si no hay más de 5)."""
    if len(rs) <= 5:
        return None
    xs = sorted(rs)[:-5]
    return round(sum(xs) / len(xs), 4)


def mitades(rs: list[float]) -> list | None:
    """R medio de la primera y de la segunda mitad de las operaciones (en orden)."""
    if len(rs) < 4:
        return None
    k = len(rs) // 2
    return [round(sum(rs[:k]) / k, 4), round(sum(rs[k:]) / (len(rs) - k), 4)]


def caida_cuenta(rs: list[float], riesgo: float = RIESGO) -> float | None:
    """Mayor caída desde un máximo de la cuenta con riesgo % por operación, compuesta, en el orden de las operaciones."""
    if not rs:
        return None
    e = pk = 100.0
    dd = 0.0
    for r in rs:
        e *= 1 + riesgo / 100 * r
        pk = max(pk, e)
        dd = max(dd, (pk - e) / pk * 100)
    return round(dd, 2)


def caida_aprox(dd_R, riesgo: float = RIESGO) -> float | None:
    """Caída de la cuenta (compuesta) que corresponde a una caída de dd_R R, sin la secuencia exacta."""
    v = num(dd_R)
    return round(100 * (1 - math.exp(-riesgo / 100 * v)), 2) if v is not None and v >= 0 else None


def pct_dia(R, op) -> float | None:
    return round(R * RIESGO * op, 4) if num(R) is not None and num(op) is not None else None


def cumple_motor(params: dict | None) -> bool | None:
    """evolucion.cumple_reglas con los parámetros completos del motor (sin mezclar con el preset de siempre)."""
    if not isinstance(params, dict):
        return None
    try:
        from bot import evolucion
        return bool(evolucion.cumple_reglas(params, base={})[0])
    except Exception:
        return None


def salida_params(sl: float, fam: str, x: dict) -> dict:
    """Parámetros de salida del motor de una combinación de salidas_motor / salidas_1a1 (F0 horquilla, F1 cerrojo,
    F2 mitad en 3R + cerrojo)."""
    p = {"sl_pct": sl, "target_r": None, "tp1_r": None, "tp1_frac": 0.0, "lock_r": None, "floor_r": None,
         "trail_r": None, "trail_atr": None, "max_hold_h": x.get("hs") or 24, "max_hold_active_h": x.get("hl"),
         "min_net_pct": 3.0}
    if fam == "F0":
        p["target_r"] = x.get("k") or x.get("X")
    else:
        p.update(lock_r=x.get("A"), floor_r=x.get("S"))
        if x.get("Tk") == "ATR":
            p["trail_atr"] = x.get("T")
        else:
            p["trail_r"] = x.get("T")
        if fam == "F2":
            p.update(tp1_r=x.get("X") or 3.0, tp1_frac=0.5)
    return p


def slug(fid: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", fid.lower()
               .translate(str.maketrans("áéíóúüñ½·", "aeiouun__"))).strip("_")[:80]
    return f"{s}-{hashlib.sha1(fid.encode('utf-8')).hexdigest()[:8]}"


def archivo_de(f: dict) -> str:
    return f"{f['estudio']}--{slug(f['id'])}.json"


def tramo(d: dict | None, R_k: str = "R_medio_funding", gana_k: str = "gana_pct") -> dict | None:
    if not isinstance(d, dict):
        return None
    R = num(d.get(R_k))
    if R is None:
        R = num(d.get("R_medio"))
    return {"n": d.get("n"), "R": r4(R), "gana_pct": num(d.get(gana_k))}


# ---------------------------------------------------------------- laboratorio de 2.000

def nombres_laboratorio(d: dict) -> dict:
    """{id: estrategia con «name»} con la lista determinista de bot.laboratorio.generate (si no se puede, {})."""
    try:
        from bot import laboratorio
        av = set((d.get("data") or {}).get("available") or [])
        return {s["id"]: s for s in laboratorio.generate(av, max_n=(d.get("params") or {}).get("max", 2000))}
    except Exception as e:
        print(f"  (sin nombres del laboratorio: {e})")
        return {}


def _sl_r(lid: str) -> tuple[float | None, float | None]:
    partes = lid.split("|")
    try:
        return float(partes[-3].replace(",", ".")), float(partes[-2].replace(",", "."))
    except (IndexError, ValueError):
        return None, None


def filas_laboratorio(d: dict, nombres: dict | None = None) -> list[dict]:
    """Las que no pierden fuera de muestra (R ≥ 0 con al menos una operación) y las prometedoras y robustas."""
    nombres = nombres or {}
    per = d.get("period") or {}
    split, hasta, desde = per.get("split"), per.get("to"), per.get("from")
    if not (split and hasta and desde):
        return []
    dia_antes = (datetime.fromisoformat(split) - timedelta(days=1)).date().isoformat()
    J_f, N_f = ranking.jornadas_entre(split, hasta), ranking.dias_entre(split, hasta)
    J_d = ranking.jornadas_entre(desde, dia_antes)
    prom = {s["id"]: s for s in d.get("promising") or [] if isinstance(s, dict)}
    rob = {s["id"]: s for s in d.get("robust") or [] if isinstance(s, dict)}
    top = {s["id"]: s for s in d.get("top50") or [] if isinstance(s, dict)}
    fam_txt = {}
    try:
        from bot import laboratorio
        fam_txt = {k: v[1] for k, v in laboratorio.FAMILIES.items()}
    except Exception:
        pass
    f_fecha = (d.get("frozen") or {}).get("date") or fecha(d.get("generated"))
    probadas = (d.get("summary") or {}).get("tested") or len(d.get("all") or [])
    out = []
    for x in d.get("all") or []:
        lid = x.get("id")
        if not isinstance(lid, str):
            continue
        oos, ins = x.get("oos") or {}, x.get("is") or {}
        n, R = oos.get("trades"), num(oos.get("mean_R"))
        if not ((n and R is not None and R >= 0) or lid in prom or lid in rob):
            continue
        st = nombres.get(lid) or {}
        sl, r = (st.get("sl"), st.get("r")) if st else _sl_r(lid)
        regla = st.get("name") or lid
        p = regla.split(" · ")   # familia: fuente · entrada · confirmación · filtros · SL · TP · duración
        nombre = (" · ".join([p[0], p[1], p[4], p[5].split(" (")[0], DUR_CORTO.get(lid.rsplit("|", 1)[-1], p[6])])
                  if len(p) >= 7 else regla)
        det = prom.get(lid) or rob.get(lid) or top.get(lid) or {}
        rs = [num(t.get("R")) for t in det.get("trades_oos") or [] if isinstance(t, dict) and num(t.get("R")) is not None]
        q = num(x.get("q"))
        clase = x.get("class")
        op = (n / J_f) if (n and J_f) else None
        notas = ["Laboratorio de 2.000 (3 años, del 01-11-2023 al 23-10-2026): cada estrategia se mide sola, sin el libro "
                 "del paper, y ninguna de las 2.000 pasó todas las pruebas contra la suerte"]
        if clase == "prometedora":
            notas.append("prometedora: positiva dentro y fuera y bate a su base, pero no robusta")
        if lid == LAB_REF:
            notas.append("es E2, descartada por el usuario el 29-10-2026")
        out.append({
            "id": f"lab2000:{lid}", "nombre": nombre, "familia": fam_txt.get(x.get("family")) or x.get("family"),
            "origen": "laboratorio_2000", "estudio": "laboratorio_2000", "regla": regla,
            "cumple_reglas": (sl is not None and r is not None and sl >= 1.5 and r >= 2
                              and sl * r - COSTE_RT_LAB >= 3.0 - 1e-9),
            "perfil_costes": "laboratorio", "fecha": f_fecha, "archivo": "laboratorio.json",
            "dentro": {"n": ins.get("trades"), "R": r4(ins.get("mean_R")),
                       "gana_pct": r4((num(ins.get("win_rate")) or 0) * 100, 1) if num(ins.get("win_rate")) is not None
                       else None, "jornadas": J_d},
            "fuera": {"n": n, "R": r4(R), "gana_pct": r4(num(oos.get("win_rate")) * 100, 1)
                      if num(oos.get("win_rate")) is not None else None,
                      "p": num(oos.get("p_day")), "q": q, "jornadas": J_f, "dias": N_f, "desde": split, "hasta": hasta},
            "op_dia": r4(op), "pct_dia": pct_dia(R, op), "R_dia": r4(R * op) if R is not None and op else None,
            "op_dia_natural": r4(n / N_f) if n and N_f else None,
            "pct_dia_natural": pct_dia(R, n / N_f) if n and N_f else None,
            "caida_max_pct": caida_cuenta(rs) if rs else caida_aprox((det.get("oos") or {}).get("max_dd_R")),
            "robustez": {"sin_5_mayores": sin_5(rs) if rs else None, "mitades": mitades(rs) if rs else None,
                         "corregida": ({"tipo": "bh", "metodo": "Benjamini-Hochberg (q) entre las 2.000 del laboratorio",
                                        "valor": q, "pasa": q <= ranking.Q_MAX} if q is not None else None),
                         "sin_mejor_mes": num(oos.get("mean_R_wo_best_month")), "base_p": num(x.get("p_base"))},
            "estado": "descartada_por_el_usuario" if lid == LAB_REF else "backtest",
            "probadas": probadas, "nota": "; ".join(notas)})
    return out


# ---------------------------------------------------------------- réplicas del motor

def filas_motor(fid: str, d: dict, selectiva: dict | None = None, bruto_neto: dict | None = None,
                jornadas: dict | None = None, e2_q: float | None = None) -> dict | None:
    """Fila de una réplica del motor (seis, seis2, ruptura3 o E2 con 7 días). E2 con 7 días: su salida se eligió
    después de ver los datos (frágil y sin corrección; e2_q, la q de E2 con la salida del laboratorio, va en el texto)."""
    if not isinstance(d, dict) or not isinstance(d.get("fuera"), dict):
        return None
    archivo, preset, nombre, familia = MOTOR[fid]
    J = {**POR_DEFECTO["jornadas"], **(jornadas or {})}
    fu, de = d["fuera"], d.get("dentro") or {}
    rs = por_operacion(fu.get("curva_R")) if len(fu.get("curva_R") or []) == fu.get("n") else []
    R = num(fu.get("paper_R_funding"))
    R = num(fu.get("mean_R")) if R is None else R
    Rd = num(de.get("paper_R_funding"))
    Rd = num(de.get("mean_R")) if Rd is None else Rd
    jf = J["fuera"]
    if fid == "e2_impulso_7d":   # su periodo es el del laboratorio (al 23-10-2026)
        jf = ranking.jornadas_entre("2025-08-16", "2026-10-23")
    op = num(fu.get("op_jornada")) or (fu["n"] / jf if fu.get("n") else None)
    pct = num((fu.get("antes_de_costes") or {}).get("cuenta_jornada_neto_pct"))
    pct = pct_dia(R, op) if pct is None else pct
    params = d.get("params") if isinstance(d.get("params"), dict) else None
    try:
        from bot import apertura
        regla = apertura.PRESETS[preset]["descripcion"]
        params = params or apertura.PRESETS[preset]["params"]
    except Exception:
        regla = d.get("nota")
    rob = {"sin_5_mayores": sin_5(rs), "mitades": mitades(rs), "corregida": None}
    caida = caida_cuenta(rs)
    sel = (selectiva or {}).get("ruptura3") if fid == "ruptura3" else None
    probadas = None
    if sel:   # la misma estrategia en controles/selectiva: base emparejada, Holm entre las 23 y sus mitades
        rob["base_p"] = sel["robustez"].get("base_p")
        rob["corregida"] = sel["robustez"].get("corregida")
        rob["mitades"] = sel["robustez"].get("mitades") or rob["mitades"]
        caida = sel.get("caida_max_pct") or caida
        probadas = sel.get("probadas")
    perfiles = perfiles_bruto_neto(bruto_neto, {"seis2": "seis2", "ruptura3": "ruptura_v3"}.get(fid))
    extra = {}
    if fid == "e2_impulso_7d":   # elegida después de ver los datos: frágil y sin corrección, se diga lo que se diga
        q_txt = f" (con la salida del laboratorio, E2 tiene q {ranking._n(e2_q, 2)} entre las 2.000)" if num(e2_q) is not None \
            else ""
        rob["corregida"] = {"tipo": "sin_p", "valor": None, "pasa": False,
                            "metodo": f"sin corrección: la salida de 7 días se eligió después de ver los datos{q_txt}"}
        extra = {"fragil": True, "fragil_motivos": [E2_7D_MOTIVO]}
    return {**extra, "id": fid, "nombre": d.get("nombre") or nombre, "familia": familia, "origen": "estudio",
            "estudio": "impulso_7d" if fid == "e2_impulso_7d" else fid,
            "regla": regla, "cumple_reglas": cumple_motor(params) if params else None,
            "perfil_costes": "backtest", "fecha": fecha(d.get("generated")), "archivo": archivo,
            "dentro": {"n": de.get("n"), "R": r4(Rd), "gana_pct": r4((num(de.get("win_rate")) or 0) * 100, 1)
                       if num(de.get("win_rate")) is not None else None},
            "fuera": {"n": fu.get("n"), "R": r4(R), "gana_pct": r4((num(fu.get("win_rate")) or 0) * 100, 1)
                      if num(fu.get("win_rate")) is not None else None, "jornadas": jf,
                      "R_bruto": num((fu.get("antes_de_costes") or {}).get("R_bruto"))},
            "op_dia": r4(op), "pct_dia": r4(pct), "R_dia": r4(R * op) if R is not None and op else None,
            "op_dia_natural": r4(fu["n"] / POR_DEFECTO["dias"]["fuera"]) if fu.get("n") and fid != "e2_impulso_7d" else None,
            "caida_max_pct": caida, "robustez": rob,
            "estado": "descartada_por_el_usuario" if fid == "e2_impulso_7d" else "backtest",
            "probadas": probadas, "nota": NOTAS_MOTOR.get(fid), "perfiles": perfiles}


def perfiles_bruto_neto(d: dict | None, clave: str | None) -> dict | None:
    """{perfil: {n, R, gana_pct, op_dia, pct_dia, pct_dia_natural}} fuera de muestra de bruto_neto.json."""
    if not isinstance(d, dict) or not clave:
        return None
    casos = ((d.get("estrategias") or {}).get(clave) or {}).get("casos") or {}
    nombre = {"sin_costes": "sin_costes", "backtest": "backtest", "paper_hoy": "okx_eu_con_xperps",
              "sin_xperps": "okx_eu_sin_xperps"}
    out = {}
    for k, perfil in nombre.items():
        fu = (casos.get(k) or {}).get("fuera")
        if isinstance(fu, dict):
            out[perfil] = {"n": fu.get("n"), "R": num(fu.get("R_medio")), "gana_pct": num(fu.get("gana_pct")),
                           "op_dia": num(fu.get("op_por_jornada")), "pct_dia": num(fu.get("cuenta_por_jornada_pct")),
                           "pct_dia_natural": num(fu.get("cuenta_por_dia_natural_pct"))}
    return out or None


# ---------------------------------------------------------------- selectivas

def filas_selectiva(d: dict) -> tuple[list[dict], dict]:
    """(filas, {"ruptura3": la V3 de Ruptura con la salida A, que es la del motor}) de medir_selectiva.json."""
    est = d.get("estrategias") if isinstance(d.get("estrategias"), dict) else {}
    crudas = []
    for e, info in est.items():
        for var, v in ((info or {}).get("variantes") or {}).items():
            for k, s in v.items():
                if not k.startswith("salida_") or not isinstance(s, dict) or not isinstance(s.get("fuera"), dict):
                    continue
                crudas.append((e, var, k[len("salida_"):], v, s))
    ps = [num(((s.get("base_emparejada") or {}).get("fuera") or {}).get("p_valor")) for *_, s in crudas]
    ph = ranking.holm(ps)
    m = sum(p is not None for p in ps)
    filas, aparte = [], {}
    f_fecha = fecha(d.get("generado"))
    for (e, var, sal, v, s), p, p_h in zip(crudas, ps, ph):
        fu, de = s["fuera"], s.get("dentro") or {}
        rs = [num(o.get("R")) for o in s.get("operaciones") or [] if isinstance(o, dict) and o.get("muestra") == "fuera"
              and num(o.get("R")) is not None]
        mit = s.get("fuera_mitades") or {}
        titulo, familia, regla = SELECTIVA.get(e, (e, e, e))
        R = num(fu.get("R_medio_funding"))
        fila = {"id": f"selectiva:{e}:{var}:{sal}", "nombre": f"{titulo} · {var} · salida {sal}", "familia": familia,
                "origen": "selectiva", "estudio": "selectiva",
                "regla": f"{regla}. {SELECTIVA_VAR.get((e, var), var)}. {SALIDA_TXT.get(sal, s.get('salida') or sal)}",
                "cumple_reglas": True, "perfil_costes": "backtest", "fecha": f_fecha,
                "archivo": "controles/selectiva/medir_selectiva.json",
                "dentro": tramo(de), "fuera": {**tramo(fu), "p": p, "jornadas": (d.get("jornadas") or {}).get("fuera")},
                "op_dia": num(fu.get("op_dia_con_aperturas")), "pct_dia": num(fu.get("cuenta_por_jornada_pct")),
                "R_dia": r4(R * fu["op_dia_con_aperturas"]) if R is not None and num(fu.get("op_dia_con_aperturas"))
                is not None else None,
                "op_dia_natural": num(fu.get("op_dia_natural")), "pct_dia_natural": num(fu.get("cuenta_por_dia_natural_pct")),
                "caida_max_pct": num(fu.get("caida_max_cuenta_pct")),
                "robustez": {"sin_5_mayores": sin_5(rs) if rs else None,
                             "mitades": ([num((mit.get("primera") or {}).get("R")), num((mit.get("segunda") or {}).get("R"))]
                                         if mit.get("primera") and mit.get("segunda") else None),
                             "corregida": ({"tipo": "holm", "metodo": f"Holm entre las {m} configuraciones del estudio (p "
                                                                      "frente a la base emparejada)", "valor": p_h,
                                            "pasa": p_h < ranking.P_MAX} if p_h is not None else None),
                             "base_p": p,
                             "desliz_x2": num((((s.get("estres") or {}).get("desliz_x2_y_stops") or {}).get("fuera") or {})
                                              .get("R_medio_funding"))},
                "estado": "backtest", "probadas": len(crudas),
                "nota": ("Selectiva prerregistrada (29-10-2026): las reglas y las variantes se fijaron antes de medir; "
                         "con el libro verificado del paper")}
        if e == "ruptura" and var == "V3" and sal == "A":
            aparte["ruptura3"] = fila   # es la Ruptura V3 del motor: va en la fila «ruptura3»
            continue
        filas.append(fila)
    return filas, aparte


# ---------------------------------------------------------------- técnicas del curso

def _curso_uso(uso: str) -> str | None:
    for pref, k in CURSO_USOS:
        if uso.startswith(pref):
            return k
    return None


def _curso_detalle(d: dict, uso: str, nombre: str) -> dict:
    if uso == "a":
        tec, _, var = nombre.partition("-")
        return ((d.get("a_estrategias_propias") or {}).get(tec) or {}).get(var) or {}
    if uso == "a_filtro":
        return (d.get("a_filtros_sobre_RR") or {}).get(nombre) or {}
    if uso in ("b_filtro", "b_veto"):
        return (d.get("b_filtros") or {}).get(nombre) or {}
    if uso == "b_espera":
        tec, _, var = nombre.partition("-")
        return ((d.get("b_esperas") or {}).get(tec) or {}).get(var) or {}
    return {}


def filas_curso(d: dict) -> list[dict]:
    tabla = [x for x in d.get("tabla") or [] if isinstance(x, dict) and _curso_uso(str(x.get("uso") or ""))]
    det = [_curso_detalle(d, _curso_uso(x["uso"]), str(x.get("nombre") or "")) for x in tabla]

    def p_de(x: dict) -> float | None:
        b = ((x.get("base_emparejada") or {}).get("fuera") or {}).get("p_valor")
        return num(b) if num(b) is not None else num((x.get("fuera") or {}).get("p"))

    ps = [p_de(x) for x in det]
    ph = ranking.holm(ps)
    m = sum(p is not None for p in ps)
    f_fecha = fecha(d.get("generado"))
    out = []
    for x, dx, p, p_h in zip(tabla, det, ps, ph):
        uso = _curso_uso(x["uso"])
        nombre = str(x.get("nombre"))
        fu, de = x.get("fuera") or {}, x.get("dentro") or {}
        tec = str(x.get("tecnica") or nombre.split("-")[0])
        rs = [num(o.get("R")) for o in dx.get("operaciones") or [] if isinstance(o, dict) and o.get("muestra") == "fuera"
              and num(o.get("R")) is not None]
        mit = dx.get("fuera_mitades") or {}
        sal = nombre.rsplit("_", 1)[-1] if uso == "a" and "_" in nombre else ("A" if uso == "b_espera" else None)
        txt_uso = {"a": "estrategia propia (opera las 24 h, todos los días)",
                   "a_filtro": "filtro sobre la estrategia RR-V2",
                   "b_filtro": "filtro sobre «6 diarias v2»", "b_veto": "veto sobre «6 diarias v2»",
                   "b_espera": "esperar la señal de la técnica entre +15 min y +4 h de la apertura, a favor del impulso "
                               "de los 15 primeros min (stop 2 % y la salida de «6 diarias v2»)"}[uso]
        corto = {"a": "estrategia propia", "a_filtro": "filtro sobre RR-V2", "b_filtro": "filtro sobre «6 diarias v2»",
                 "b_veto": "veto sobre «6 diarias v2»", "b_espera": "espera su señal en la apertura"}[uso]
        R = num(fu.get("R_medio_funding"))
        op = num(fu.get("op_dia_con_aperturas"))
        es_filtro = uso in ("a_filtro", "b_filtro", "b_veto") and num((dx.get("fuera") or {}).get("p")) is not None \
            and not (dx.get("base_emparejada"))
        out.append({
            "id": f"curso:{uso}:{nombre}", "nombre": f"{CURSO_TECNICAS.get(tec, tec)} · {nombre} ({corto})",
            "familia": f"Curso: {CURSO_TECNICAS.get(tec, tec)}", "origen": "curso", "estudio": "curso",
            "regla": f"{CURSO_TECNICAS.get(tec, tec)} ({nombre}): {txt_uso}"
                     + (f". {SALIDA_TXT[sal]}" if sal in SALIDA_TXT else ""),
            "cumple_reglas": True, "perfil_costes": "backtest", "fecha": f_fecha,
            "archivo": "controles/curso/medir_curso.json",
            "dentro": tramo(de), "fuera": {**(tramo(fu) or {}), "p": p, "jornadas": (d.get("jornadas") or {}).get("fuera")},
            "op_dia": op, "pct_dia": num(fu.get("cuenta_por_jornada_pct")),
            "R_dia": r4(R * op) if R is not None and op is not None else None,
            "op_dia_natural": num(fu.get("op_dia_natural")), "pct_dia_natural": num(fu.get("cuenta_por_dia_natural_pct")),
            "caida_max_pct": num(fu.get("caida_max_cuenta_pct")),
            "robustez": {"sin_5_mayores": sin_5(rs) if rs else None,
                         "mitades": ([num((mit.get("primera") or {}).get("R")), num((mit.get("segunda") or {}).get("R"))]
                                     if mit.get("primera") and mit.get("segunda") else None),
                         "corregida": ({"tipo": "holm", "metodo": f"Holm entre las {m} pruebas del estudio"
                                        + (" (prueba del filtro)" if es_filtro else " (p frente a la base)"),
                                        "valor": p_h, "pasa": p_h < ranking.P_MAX} if p_h is not None else None)},
            "estado": "backtest", "probadas": len(tabla),
            "nota": "Técnicas del curso (vídeos 027-029) medidas como se prerregistraron en plan_medida.md, con el libro "
                    "verificado del paper"})
    return out


# ---------------------------------------------------------------- salidas

def _filas_rejilla(d: dict, estudio: str, archivo: str, saltar=(), igual: dict | None = None) -> list[dict]:
    todas = [x for x in d.get("todas") or [] if isinstance(x, dict) and isinstance(x.get("id"), str)]
    f_fecha = fecha(d.get("generado"))
    out = []
    for x in todas:
        if x.get("grupo") in saltar:
            continue
        fu, de = x.get("fuera") or {}, x.get("dentro") or {}
        R = num(fu.get("R_medio_funding"))
        op = num(fu.get("op_dia"))
        cumple = x["reglas"] if isinstance(x.get("reglas"), bool) else cumple_motor(
            salida_params(x.get("sl"), x.get("fam"), x))
        caida = num(fu.get("caida_max_cuenta_pct"))
        nota = (f"Salidas sobre las entradas de «6 diarias» ({len(todas)} combinaciones de una rejilla fijada de "
                "antemano): la mejor fuera de muestra de muchas suele ser suerte")
        if caida is None and num(fu.get("caida_max_R")) is not None:
            caida = caida_aprox(fu["caida_max_R"])
            nota += "; caída de la cuenta aproximada desde la caída en R"
        out.append({
            "id": f"{estudio}:{x['id']}", "nombre": f"6 diarias · {x['id']}", "familia": "Salidas sobre «6 diarias»",
            "origen": "salidas", "estudio": estudio, "regla": SEIS_ENTRADAS + "salida " + x["id"],
            "cumple_reglas": cumple, "perfil_costes": "backtest", "fecha": f_fecha, "archivo": archivo,
            "dentro": tramo(de), "fuera": {**(tramo(fu) or {}), "jornadas": (d.get("dias_jornada") or {}).get("fuera")},
            "op_dia": op, "pct_dia": num(fu.get("cuenta_dia_jornada_pct")),
            "R_dia": r4(R * op) if R is not None and op is not None else None,
            "op_dia_natural": num(fu.get("op_dia_natural")), "pct_dia_natural": num(fu.get("cuenta_dia_natural_pct")),
            "caida_max_pct": caida,
            "robustez": {"sin_5_mayores": None, "mitades": None,
                         "corregida": {"tipo": "sin_p", "valor": None, "pasa": False,
                                       "metodo": f"sin p ni corrección: una de {len(todas)} salidas de una rejilla "
                                                 "sobre las mismas entradas de «6 diarias»"}},
            "estado": "backtest", "probadas": len(todas), "nota": nota,
            "igual_que": (igual or {}).get(x["id"])})
    return out


def filas_salidas_motor(d: dict) -> list[dict]:
    igual = {"F0 stop 1,5 % · objetivo 3R · tope 24h": "seis",
             "F2 stop 2 % · ½ en 3R + ½ cerrojo a +3R (suelo 2R) · sigue a 2R · 24h sin activar / 7d activada": "seis2"}
    return _filas_rejilla(d, "salidas_motor", "controles/salidas_motor.json", igual=igual)


def filas_salidas_1a1(d: dict) -> list[dict]:
    """Las 1:1 (la actual y la A ya están en salidas_motor)."""
    return _filas_rejilla(d, "salidas_1a1", "controles/salidas_1a1.json", saltar=("actual", "A"))


# ---------------------------------------------------------------- stops y objetivos anchos

def _tope_salidas_motor(fid: str) -> tuple | None:
    m = re.match(r"^salidas_motor:F0 stop ([\d,]+) % · objetivo ([\d,]+)R · tope 7d$", fid)
    return (float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))) if m else None


def filas_anchos(d: dict, ya: dict | None = None) -> list[dict]:
    """De medir_anchos.json (final; prueba = false): perfil «backtest» y los demás en «perfiles». Las referencias (la A de
    «6 diarias v2» y de la Ruptura V3 con stop 2 %) y los objetivos fijos de 7 días que ya mide salidas_motor no se
    repiten (ya: {(stop, R): fila de salidas_motor} para añadirle los perfiles)."""
    if not isinstance(d, dict) or d.get("prueba"):
        return []
    combos = [c for c in d.get("combinaciones") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)]
    bt = [(((c.get("referencia") or {}).get("fuera") or {}).get("backtest") or {}) for c in combos]
    ph = ranking.holm([num(b.get("p")) for b in bt])
    m = sum(num(b.get("p")) is not None for b in bt)
    out = []
    for c, b, p_h in zip(combos, bt, ph):
        ref = c.get("referencia") or {}
        fu_c, de_c = ref.get("fuera") or {}, ref.get("dentro") or {}
        perfiles = {}
        for k, perfil in (("sin_costes", "sin_costes"), ("backtest", "backtest"), ("paper_hoy", "okx_eu_con_xperps")):
            x = fu_c.get(k)
            if isinstance(x, dict):
                perfiles[perfil] = {"n": x.get("n"), "R": num(x.get("R_medio")), "gana_pct": num(x.get("gana_pct")),
                                    "op_dia": num(x.get("op_por_jornada")), "pct_dia": num(x.get("cuenta_por_jornada_pct"))}
        sx = (((c.get("sin_xperps") or {}).get("fuera") or {}).get("sin_xperps") or {})
        if sx:
            perfiles["okx_eu_sin_xperps"] = {"n": sx.get("n"), "R": num(sx.get("R_medio")), "gana_pct": num(sx.get("gana_pct")),
                                             "op_dia": num(sx.get("op_por_jornada")),
                                             "pct_dia": num(sx.get("cuenta_por_jornada_pct"))}
        stop, sal = num(c.get("stop_pct")), str(c.get("salida") or "")
        if sal == "A" and stop == 2.0:
            continue   # las referencias: «6 diarias v2» y la Ruptura V3, que ya tienen su fila
        mr = re.fullmatch(r"(\d+(?:[.,]\d+)?)R", sal)
        if c.get("entrada") == "seis" and mr and ya is not None:
            k = (stop, float(mr.group(1).replace(",", ".")))
            if k in ya:
                ya[k].setdefault("perfiles", {}).update({kk: vv for kk, vv in perfiles.items() if kk != "backtest"})
                continue
        de = de_c.get("backtest") or {}
        R = num(b.get("R_medio"))
        op = num(b.get("op_por_jornada"))
        entrada = c.get("entrada_nombre") or c.get("entrada")
        out.append({
            "id": f"anchos:{c['id']}", "nombre": c["id"], "familia": f"Stops y objetivos anchos sobre «{entrada}»",
            "origen": "salidas", "estudio": "anchos",
            "regla": (SEIS_ENTRADAS if c.get("entrada") == "seis" else f"Entradas de {entrada}; ")
                     + f"stop {ranking._n(stop, 1)} % y {c.get('salida_nombre') or sal}",
            "cumple_reglas": True, "perfil_costes": "backtest", "fecha": fecha(d.get("generado")),
            "archivo": "controles/anchos/medir_anchos.json",
            "dentro": {"n": de.get("n"), "R": num(de.get("R_medio")), "gana_pct": num(de.get("gana_pct"))},
            "fuera": {"n": b.get("n"), "R": R, "gana_pct": num(b.get("gana_pct")), "p": num(b.get("p")),
                      "jornadas": (d.get("jornadas") or {}).get("fuera")},
            "op_dia": op, "pct_dia": num(b.get("cuenta_por_jornada_pct")),
            "R_dia": r4(R * op) if R is not None and op is not None else None,
            "op_dia_natural": num(b.get("op_por_dia_natural")), "pct_dia_natural": num(b.get("cuenta_por_dia_natural_pct")),
            "caida_max_pct": num(b.get("caida_max_cuenta_pct")),
            "robustez": {"sin_5_mayores": num(b.get("R_sin_5_mayores")), "mitades": None,
                         "corregida": ({"tipo": "holm", "metodo": f"Holm entre las {m} combinaciones del estudio",
                                        "valor": p_h, "pasa": p_h < ranking.P_MAX} if p_h is not None else None)},
            "estado": "backtest", "probadas": len(combos), "perfiles": perfiles or None,
            "nota": "Rejilla de stops y objetivos fijada de antemano, en el libro verificado del paper"})
    return out


# ---------------------------------------------------------------- todo junto

def construir(origen: Path) -> tuple[dict[str, list[dict]], dict]:
    """{estudio: filas} de todo lo que hay en BACKTEST_DIR y el informe de lo leído."""
    inf: dict[str, str] = {}
    out: dict[str, list[dict]] = {}

    def cargar(rel: str):
        d = leer(origen / rel)
        if d is None:
            inf[rel] = "no está (o no se puede leer)"
        return d

    sel_d = cargar("controles/selectiva/medir_selectiva.json")
    sel, aparte = filas_selectiva(sel_d) if isinstance(sel_d, dict) else ([], {})
    if isinstance(sel_d, dict):
        out["selectiva"] = sel
    bn = cargar("controles/costes/bruto_neto.json")
    jor = (sel_d or {}).get("jornadas") if isinstance(sel_d, dict) else None
    lab = cargar("laboratorio.json")
    todas_lab = lab.get("all") if isinstance(lab, dict) and isinstance(lab.get("all"), list) else []
    e2_q = next((num(x.get("q")) for x in todas_lab if isinstance(x, dict) and x.get("id") == LAB_REF), None)
    for fid, (rel, *_rest) in MOTOR.items():
        d = cargar(rel)
        f = filas_motor(fid, d, aparte, bn, jor, e2_q) if isinstance(d, dict) else None
        if f:
            out[f["estudio"]] = [f]
    if isinstance(lab, dict):
        out["laboratorio_2000"] = filas_laboratorio(lab, nombres_laboratorio(lab))
    cur = cargar("controles/curso/medir_curso.json")
    if isinstance(cur, dict):
        out["curso"] = filas_curso(cur)
    sm = cargar("controles/salidas_motor.json")
    if isinstance(sm, dict):
        out["salidas_motor"] = filas_salidas_motor(sm)
    s11 = cargar("controles/salidas_1a1.json")
    if isinstance(s11, dict):
        out["salidas_1a1"] = filas_salidas_1a1(s11)
    an = leer(origen / "controles/anchos/medir_anchos.json")
    if isinstance(an, dict) and not an.get("prueba"):
        ya = {}
        for f in out.get("salidas_motor") or []:
            k = _tope_salidas_motor(f["id"])
            if k:
                ya[k] = f
        out["anchos"] = filas_anchos(an, ya)
    else:
        inf["controles/anchos/medir_anchos.json"] = ("aún no está (el estudio sigue en marcha)" if an is None
                                                     else "es una prueba (prueba = true): no se importa")
    return out, inf


def escribir(filas_por_estudio: dict[str, list[dict]], destino: Path, seco: bool = False,
             limpiar: bool = False) -> dict:
    """Una fila por archivo, sin reescribir los que no cambian; quita los de los estudios leídos que ya no salen (con
    limpiar, también los de los estudios que faltan)."""
    nuevos: dict[str, str] = {}
    for est, filas in filas_por_estudio.items():
        for f in filas:
            f = {**f, "estudio": f.get("estudio") or est}
            nombre = archivo_de(f)
            nuevos[nombre] = json.dumps({"formato": 1, **f}, ensure_ascii=False, separators=(",", ":"))
    escritos = iguales = borrados = 0
    if not seco:
        destino.mkdir(parents=True, exist_ok=True)
    for nombre, txt in sorted(nuevos.items()):
        ruta = destino / nombre
        try:
            if ruta.read_text(encoding="utf-8") == txt:
                iguales += 1
                continue
        except OSError:
            pass
        escritos += 1
        if not seco:
            tmp = ruta.with_name(ruta.name + ".tmp")
            tmp.write_text(txt, encoding="utf-8")
            os.replace(tmp, ruta)
    leidos = set(filas_por_estudio)
    prefijos = tuple(f"{e}--" for e in (ESTUDIOS if limpiar else leidos))
    try:
        existentes = [e.name for e in os.scandir(destino) if e.is_file() and e.name.endswith(".json")]
    except OSError:
        existentes = []
    for n in existentes:
        if n.startswith(prefijos) and n not in nuevos:
            borrados += 1
            if not seco:
                os.remove(destino / n)
    res = {"archivos": len(nuevos), "escritos": escritos, "iguales": iguales, "borrados": borrados}
    if not seco and (escritos or borrados or not (destino / "_importado.json").exists()):
        man = {"formato": 1, "ts": int(datetime.now(timezone.utc).timestamp() * 1000), "por": "scripts/ranking_importar.py",
               "estudios": {e: len(f) for e, f in filas_por_estudio.items()}, **res}
        (destino / "_importado.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def resumen(destino: Path, filas_por_estudio: dict[str, list[dict]] | None = None) -> dict:
    """El ranking de lo importado (sin lo de en vivo): n_total, n_ranking y las 10 primeras."""
    if filas_por_estudio is not None:
        filas = [ranking.normalizar(f) for fs in filas_por_estudio.values() for f in fs]
    else:
        filas = []
        for e in sorted(os.scandir(destino), key=lambda e: e.name):
            if e.name.endswith(".json") and not e.name.startswith(("_", ".")):
                filas.append(ranking.normalizar(leer(Path(e.path)), e.name))
    por_id = {f["id"]: f for f in filas if f}
    orden = ranking.ordenar(list(por_id.values()))
    dentro = [f for f in orden if f["posicion"]]
    return {"n_total": len(orden), "n_ranking": len(dentro), "top": dentro[:10],
            "por_estudio": {e: (len(fs), sum(1 for f in fs if ranking.entra(ranking.normalizar(f) or {})))
                            for e, fs in (filas_por_estudio or {}).items()}}


# ---------------------------------------------------------------- el ejemplo de la web (rankingData)

LAB_E1 = "btc:ma20|rvol:1,5|todos|ninguno|asia|SOL|todos|pb:15|3|2|sig"   # la otra prometedora del laboratorio (E1)
DEMO_REALES = ("ruptura3", "e2_impulso_7d", "seis2", "seis", f"lab2000:{LAB_REF}", f"lab2000:{LAB_E1}")
DEMO_FUERA = ("archivo_ranking", "posicion", "posicion_antes", "nueva", "entra_desde", "en_marcha")
DEMO_BLOQUE = re.compile(r'(<script type="application/json" id="rankingData">)(.*?)(</script>)', re.S)


def fila_demo(f: dict) -> dict:
    """La fila normalizada sin lo que pone el bot al calcular (puesto, «nueva»…) ni lo que va vacío."""
    return {k: v for k, v in f.items() if k not in DEMO_FUERA and v is not None}


def incrustar_demo(html: Path, destino: Path, seco: bool = False) -> dict:
    """Rehace el ejemplo del ranking de la web (bloque rankingData de dashboard/radar.html): las filas reales
    (DEMO_REALES) salen de las de destino (BACKTEST_DIR/ranking) normalizadas como las enseña el bot, con su perfil de
    costes, su corrección y su fragilidad; las inventadas («Ejemplo: …») se quedan, normalizadas con la misma regla."""
    txt = Path(html).read_text(encoding="utf-8")
    m = DEMO_BLOQUE.search(txt)
    if not m:
        return {"ok": False, "why": "sin bloque rankingData"}
    d = json.loads(m.group(2))
    reales: dict[str, dict] = {}
    for e in sorted(os.scandir(destino), key=lambda e: e.name):
        if e.name.endswith(".json") and not e.name.startswith(("_", ".")):
            x = leer(Path(e.path))
            if isinstance(x, dict) and x.get("id") in DEMO_REALES:
                reales[x["id"]] = fila_demo(ranking.normalizar(x))
    ejemplos = [fila_demo(ranking.normalizar(f)) for f in d.get("filas") or []
                if isinstance(f, dict) and str(f.get("nombre") or "").startswith("Ejemplo")]
    faltan = [i for i in DEMO_REALES if i not in reales]
    filas = ejemplos + [reales[i] for i in DEMO_REALES if i in reales]
    costes: dict[str, int] = {}
    for f in filas:
        if ranking.entra(f):
            costes[f.get("perfil_costes") or "?"] = costes.get(f.get("perfil_costes") or "?", 0) + 1
    d.update(filas=filas, criterio=ranking.CRITERIO,
             perfiles_costes=[{"id": k, "texto": ranking.PERFILES_TEXTO.get(k), "n_ranking": n}
                              for k, n in sorted(costes.items(), key=lambda kv: -kv[1])])
    nuevo = json.dumps(d, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    cambia = nuevo != m.group(2)
    if cambia and not seco:
        Path(html).write_text(txt[:m.start(2)] + nuevo + txt[m.end(2):], encoding="utf-8")
    return {"ok": True, "reales": len(reales), "ejemplos": len(ejemplos), "faltan": faltan, "cambia": cambia,
            "bytes": len(nuevo.encode("utf-8"))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pasa los estudios ya hechos a filas del ranking de estrategias")
    ap.add_argument("--origen", type=Path, default=config.BACKTEST_DIR, help="BACKTEST_DIR (solo se lee)")
    ap.add_argument("--destino", type=Path, default=None, help="carpeta de las filas (por defecto, ORIGEN/ranking)")
    ap.add_argument("--seco", action="store_true", help="no escribe nada: solo dice qué haría")
    ap.add_argument("--limpiar", action="store_true", help="quita también las filas de los estudios que faltan")
    ap.add_argument("--demo", type=Path, nargs="?", const=config.DASHBOARD, default=None,
                    help="rehace también las filas reales del ejemplo del ranking en la web (por defecto, "
                         "dashboard/radar.html) con las importadas")
    a = ap.parse_args(argv)
    destino = a.destino or (a.origen / "ranking")
    print(f"Ranking de estrategias: estudios de {a.origen} (solo lectura) → {destino}" + (" [en seco]" if a.seco else ""))
    filas, inf = construir(a.origen)
    for rel, txt in inf.items():
        print(f"  · {rel}: {txt}")
    res = escribir(filas, destino, a.seco, a.limpiar)
    rs = resumen(destino, filas)
    print(f"Filas: {res['archivos']} ({res['escritos']} escritas, {res['iguales']} iguales, {res['borrados']} quitadas)")
    for e, (n, k) in rs["por_estudio"].items():
        print(f"  {e}: {n} filas, {k} entran en el ranking")
    print(f"En el ranking (no pierden fuera de muestra): {rs['n_ranking']} de {rs['n_total']}")
    print("Las 10 primeras (fuera de muestra, netas de costes, % de la cuenta por jornada con 0,5 % de riesgo):")
    for f in rs["top"]:
        fu = f.get("fuera") or {}
        chips = ", ".join(f["fragil_motivos"]) or "sin señales de fragilidad"
        print(f"  {f['posicion']:>3}. {f['nombre'][:90]} [{f['origen']}] · {ranking._n(f['pct_dia'], 3, True)} %/jornada · "
              f"{ranking._n(fu.get('R'), 3, True)} R · {ranking._n(f['op_dia'], 2)} op/jornada · n fuera {fu.get('n')}"
              f" · {chips}")
    if a.demo is not None:
        dm = incrustar_demo(a.demo, destino, a.seco)
        print(f"Ejemplo de la web ({a.demo}): " + (dm.get("why") or (
            f"{dm['reales']} filas reales y {dm['ejemplos']} inventadas, {dm['bytes']:,} bytes"
            + (f"; faltan {', '.join(dm['faltan'])}" if dm["faltan"] else "")
            + ("" if dm["cambia"] else " (sin cambios)") + (" [en seco]" if a.seco and dm["cambia"] else ""))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
