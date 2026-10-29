"""Catálogo de salidas del laboratorio vivo (docs/07, 6.5): fijado y cerrado. Todas cumplen lo que comprueba
evolucion.cumple_reglas (stop ≥ 1,5 %, toda parte que gana ≥ 2R y ≥ 3 % neto, nada que mueva el stop antes) salvo INFO,
que solo existe para variantes «fuera de reglas» (informativas, nunca pasan a la cartera). PERO todas (A, H, C y OBJ)
cierran por tiempo antes de la ganancia mínima (max_hold_h sin activar): esa parte puede salir entre −1R y +2R, la
excepción que el usuario solo ha aceptado en «6 diarias». Se mide (medidas.cierres_por_tiempo), va en la tarjeta y en
la propuesta (salida_web.excepcion_tiempo) y aceptar la propuesta exige aceptar la excepción.

La salida es de la VARIANTE, no de la familia. Se convierte en parámetros de apertura.impulse_plan sobre la base de
apertura.SEIS2 (largos al contado, cortos en perpetuo, 6 entradas y 3 % de riesgo al día, ≥ 3 % neto) y se comprueba
con evolucion.cumple_reglas.

| Tipo | Qué es | Se puede fijar | Topes de tiempo |
|---|---|---|---|
| A | La de «6 diarias v2»: la mitad sale en 3R; la otra mitad, a +3R, sube su stop al suelo y desde ahí lo sigue | trail_r {1,5; 2; 3}, suelo {F; 2R} | max_hold_h {24, 48, 72, 96, 120, 168} sin activar; max_hold_active_h {168, 336} activada |
| H | Horquilla pura: objetivo fijo, sin parcial ni cerrojo | target_r {2; 2,5; 3; 4; 6} | max_hold_h {24, 48, 72, 96, 120, 168} |
| C | Cerrojo y seguimiento sin parcial: a +lock_r el stop sube al suelo y sigue a trail_r (o a trail_atr × ATR de 1 h) | lock_r {2,5; 3; 4; 5}, trail_r {1; 1,5; 2; 3} o trail_atr {3; 6}, suelo {F; 2R} | max_hold_h {24, 72, 96, 168, 336}; max_hold_active_h {168, 336} |
| OBJ | Horquilla con el objetivo en un precio de la regla (señal.objetivo_r, en R): solo si deja ≥ 2R y ≥ 3 % neto | — | max_hold_h como H |
| INFO | Sin stop ni objetivo, cierre por tiempo: SOLO fuera de reglas (informativa) | — | max_hold_h (o señal.fin_ms) |

- Stop («sl»): un número fijo entre 1,5 y el tope (%), o «estructural» (el stop_pct de cada señal). sl_max {5; 10}:
  una señal con un stop más lejano no entra (veto «stop_lejano»). R es el stop de ESA operación.
- Suelo F = máx(2R; (3 % + coste de ida y vuelta del instrumento + funding esperado hasta el tope activado) / stop),
  por operación, como la Salida «F» de medir_selectiva.py: con un stop de 1,5 % sube a ≈ 2,16R y deja 3 % neto.
- avg_hold_h (funding esperado): A 16 h; H 13, 36 o 65 según su tope; C 24 h (las duraciones medidas en las réplicas)."""
import math

from .. import config

TIPOS = ("A", "H", "C", "OBJ", "INFO")
SL_MIN = 1.5
ESQ = {
    "A": {"trail_r": ((1.5, 2.0, 3.0), 2.0), "suelo": (("F", "2R"), "F"), "max_hold_h": ((24, 48, 72, 96, 120, 168), 24),
          "max_hold_active_h": ((168, 336), 168)},
    "H": {"target_r": ((2.0, 2.5, 3.0, 4.0, 6.0), 3.0), "max_hold_h": ((24, 48, 72, 96, 120, 168), 72)},
    "C": {"lock_r": ((2.5, 3.0, 4.0, 5.0), 3.0), "trail_r": ((1.0, 1.5, 2.0, 3.0, None), 2.0),
          "trail_atr": ((3.0, 6.0, None), None), "suelo": (("F", "2R"), "F"), "max_hold_h": ((24, 72, 96, 168, 336), 72),
          "max_hold_active_h": ((168, 336), 168)},
    "OBJ": {"max_hold_h": ((24, 48, 72, 96, 120, 168), 120)},
    "INFO": {"max_hold_h": ((1, 2, 4, 8, 12, 24, 48, 72, 96, 120, 168, 480, 720), 24)},
}
SL_MAX = ((5.0, 10.0), 5.0)
TEXTO = {"A": "la mitad sale en 3R y la otra mitad corre con el cerrojo (a +3R su stop sube al suelo y lo sigue)",
         "H": "horquilla pura (objetivo fijo)", "C": "sin objetivo: cerrojo y seguimiento (dejar correr)",
         "OBJ": "objetivo en un precio de la regla", "INFO": "sin stop ni objetivo, cierre por tiempo (solo informativa)"}


def _eq(a, b) -> bool:
    if a is None or b is None:
        return a is b
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return a == b


def normalizar(s: dict | None) -> tuple[dict, list[str]]:
    """Salida completa (lo que falta, por defecto) y los errores, en español."""
    s = dict(s or {})
    err = []
    tipo = s.get("tipo")
    if tipo not in TIPOS:
        return {"tipo": tipo}, [f"salida «{tipo}» no está en el catálogo ({', '.join(TIPOS)})"]
    out = {"tipo": tipo}
    sl = s.get("sl", "estructural" if tipo != "INFO" else None)
    if tipo == "INFO":
        out["sl"] = None
    elif sl == "estructural":
        out["sl"] = "estructural"
    else:
        try:
            out["sl"] = float(sl)
            if not math.isfinite(out["sl"]):
                raise ValueError
        except (TypeError, ValueError):
            err.append(f"sl = {sl!r}: un número (%) o «estructural»")
            out["sl"] = sl
    if tipo != "INFO":
        smx = s.get("sl_max", SL_MAX[1])
        if not any(_eq(smx, o) for o in SL_MAX[0]):
            err.append(f"sl_max = {smx!r}: valores permitidos 5 o 10")
        out["sl_max"] = float(smx) if isinstance(smx, (int, float)) else smx
        if isinstance(out.get("sl"), float):
            if out["sl"] < SL_MIN - 1e-9:
                err.append(f"stop fijo de {out['sl']} % por debajo del mínimo del usuario (1,5 %)")
            elif isinstance(out["sl_max"], float) and out["sl"] > out["sl_max"] + 1e-9:
                err.append(f"stop fijo de {out['sl']} % por encima de su tope ({out['sl_max']} %)")
    for k, (opts, dflt) in ESQ[tipo].items():
        v = s.get(k, dflt)
        if not any(_eq(v, o) for o in opts):
            err.append(f"{k} = {v!r}: valores permitidos {', '.join('ninguno' if o is None else str(o) for o in opts)}")
        out[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "suelo" else v
    for k in s:
        if k not in out and k not in ("tipo", "sl", "sl_max"):
            err.append(f"parámetro de salida desconocido «{k}» (la salida {tipo} admite: "
                       f"{', '.join(['sl', 'sl_max', *ESQ[tipo]])})")
    if tipo == "C":
        if out.get("trail_r") is not None and out.get("trail_atr") is not None:
            err.append("trail_r y trail_atr a la vez: elige uno")
    if tipo in ("A", "C") and isinstance(out.get("max_hold_active_h"), float) and isinstance(out.get("max_hold_h"), float):
        if out["max_hold_active_h"] < out["max_hold_h"] - 1e-9:
            out["max_hold_active_h"] = out["max_hold_h"]
    return out, err


def avg_hold(s: dict) -> float:
    t = s["tipo"]
    if t == "A":
        return 16.0
    if t == "C":
        return 24.0
    hh = float(s.get("max_hold_h") or 72)
    return 13.0 if hh <= 24 else 36.0 if hh <= 72 else 65.0


def horizonte_h(s: dict) -> float:
    """Horas tras la entrada en que una operación ya ha terminado seguro (para «completa» en avance)."""
    if s["tipo"] in ("A", "C"):
        return float(max(s.get("max_hold_active_h") or 0, s.get("max_hold_h") or 0))
    return float(s.get("max_hold_h") or 168)


def es_info(s: dict) -> bool:
    return s.get("tipo") == "INFO"


def suelo_f(sl: float, d: int, rate: float | None, inst: str, horas: float) -> float:
    """Suelo F por operación: ≥ 2R y ≥ 3 % neto con el coste de ida y vuelta del instrumento y el funding esperado."""
    from .. import apertura as ap
    from .. import paper
    f = ap.expected_funding_pct(d, rate, horas) if inst == "perp" else 0.0
    return max(config.RULE_MIN_RR, (config.RULE_MIN_NET_PCT + paper.cost_rt_pct(inst) + f) / sl)


def params_motor(s: dict, sl: float, d: int = 1, rate: float | None = None, inst: str = "spot",
                 objetivo_r: float | None = None) -> dict:
    """Parámetros del motor (sobre SEIS2) de la salida con ese stop: los que recibe apertura.impulse_plan."""
    from .. import apertura as ap
    base = dict(ap.SEIS2)
    t = s["tipo"]
    if t == "A":
        act = float(s.get("max_hold_active_h") or 168)
        fl = 2.0 if s.get("suelo") == "2R" else suelo_f(sl, d, rate, inst, act)
        return {**base, "sl_pct": sl, "target_r": None, "tp1_r": 3.0, "tp1_frac": 0.5, "lock_r": 3.0, "floor_r": fl,
                "trail_r": float(s.get("trail_r") or 2.0), "trail_atr": None, "max_hold_h": float(s["max_hold_h"]),
                "max_hold_active_h": act, "avg_hold_h": avg_hold(s)}
    if t in ("H", "OBJ"):
        tr = float(objetivo_r) if t == "OBJ" else float(s["target_r"])
        return {**base, "sl_pct": sl, "target_r": tr, "tp1_r": None, "tp1_frac": 0.0, "lock_r": None,
                "floor_r": None, "trail_r": None, "trail_atr": None, "max_hold_h": float(s["max_hold_h"]),
                "max_hold_active_h": None, "avg_hold_h": avg_hold(s)}
    if t == "C":
        act = float(s.get("max_hold_active_h") or 168)
        fl = 2.0 if s.get("suelo") == "2R" else suelo_f(sl, d, rate, inst, act)
        return {**base, "sl_pct": sl, "target_r": None, "tp1_r": None, "tp1_frac": 0.0,
                "lock_r": max(float(s["lock_r"]), fl), "floor_r": fl,
                "trail_r": float(s["trail_r"]) if s.get("trail_r") is not None else None,
                "trail_atr": float(s["trail_atr"]) if s.get("trail_atr") is not None else None,
                "max_hold_h": float(s["max_hold_h"]), "max_hold_active_h": act, "avg_hold_h": avg_hold(s)}
    raise ValueError(f"la salida {t} no tiene plan del motor")


PLAN_INFO = {"stopPct": 50.0, "tp1R": None, "tp2R": None, "tp1Frac": 0.0, "lockR": None, "lockFloorR": None,
             "lockTrailR": None, "beR": None, "trailPct": None, "trailStartR": None, "ok": True,
             "why": "Informativa: sin stop ni objetivo, cierre por tiempo"}
SL_REF_INFO = 1.5   # R de referencia de una informativa (el stop mínimo del usuario): solo para comparar


class Salida:
    """Plan de salidas de una operación (apertura.impulse_plan) y sus topes de tiempo."""

    def __init__(self, s: dict):
        self.s, err = normalizar(s)
        if err:
            raise ValueError("; ".join(err))
        self.tipo = self.s["tipo"]

    @property
    def sl_fijo(self) -> float | None:
        return self.s["sl"] if isinstance(self.s.get("sl"), float) else None

    @property
    def sl_max(self) -> float:
        return float(self.s.get("sl_max") or 5.0)

    def plan(self, d: int, E: float, sl: float, rate: float | None, inst: str, objetivo_r: float | None = None,
             atr_h_pct: float | None = None) -> tuple[dict, float, float | None, float]:
        """(plan de apertura.impulse_plan, horas sin activar, horas activado o None, stop en %)."""
        from .. import apertura as ap
        if self.tipo == "INFO":
            return dict(PLAN_INFO), float(self.s["max_hold_h"]), None, SL_REF_INFO
        if self.tipo == "OBJ" and (objetivo_r is None or not math.isfinite(objetivo_r)):
            return {"ok": False, "why": "la señal no trae objetivo (salida OBJ)"}, 0.0, None, sl
        p = params_motor(self.s, sl, d, rate, inst, objetivo_r)
        pl = ap.impulse_plan(d, E, p, rate, inst, atr_h_pct=atr_h_pct)
        hh = float(p["max_hold_h"])
        ha = float(p["max_hold_active_h"]) if p.get("max_hold_active_h") and p.get("lock_r") else None
        return pl, hh, ha, sl

    def texto(self) -> str:
        s = self.s
        sl = "estructural (el de la señal)" if s.get("sl") == "estructural" else f"{s.get('sl')} %"
        base = f"{s['tipo']}: {TEXTO[s['tipo']]}"
        if s["tipo"] == "H":
            base += f", objetivo {s['target_r']}R"
        if s["tipo"] in ("A", "C"):
            base += f", {s['max_hold_h']:.0f} h sin activar y {s['max_hold_active_h']:.0f} h activado"
        elif "max_hold_h" in s:
            base += f", como mucho {s['max_hold_h']:.0f} h"
        return base + (f"; stop {sl}" if s["tipo"] != "INFO" else "")


class SalidaRefSeis2(Salida):
    """La A de seis2 con la comprobación del plan de salidas_1a1.py (la que hizo seis2_resumen.json): una horquilla de
    3R con los parámetros de SEIS (plan_r = 3) en vez del plan real de SEIS2. Solo para el canario (7.6), como
    SalidaRef de medir_selectiva.py."""

    def __init__(self):
        super().__init__({"tipo": "A", "sl": 2.0, "suelo": "2R", "trail_r": 2.0, "max_hold_h": 24, "max_hold_active_h": 168})
        from .. import apertura as ap
        over = dict(ap.SEIS)
        over["news_mode"] = "etiqueta"
        self.PS, _ = ap.impulse_params(over)

    def plan(self, d, E, sl, rate, inst, objetivo_r=None, atr_h_pct=None):
        from .. import apertura as ap
        pl, hh, ha, SL = super().plan(d, E, sl, rate, inst, objetivo_r, atr_h_pct)
        ref = ap.impulse_plan(d, E, dict(self.PS, sl_pct=2.0, target_r=3.0), rate, inst)
        return dict(pl, ok=ref["ok"], why=ref["why"]), hh, ha, SL


def comprobar_reglas(s: dict, lados: str = "ambos") -> tuple[bool, list[str], float | None]:
    """A5 de la admisión: la salida, con el stop mínimo posible, cumple las reglas del usuario (evolucion.cumple_reglas)
    y da un plan válido en los sentidos que opera. Con un stop fijo se mira ese; con uno estructural, de 1,5 % al tope.
    Devuelve (ok, motivos, el menor stop que vale)."""
    from .. import apertura as ap
    from .. import evolucion
    s, err = normalizar(s)
    if err:
        return False, err, None
    if s["tipo"] == "INFO":
        return False, ["la salida INFO no cumple las reglas (sin stop ni objetivo): solo vale fuera de reglas"], None
    if s["tipo"] == "OBJ":   # depende de cada señal: se veta operación a operación (veto «plan»)
        return True, [], SL_MIN
    stops = [s["sl"]] if isinstance(s["sl"], float) else [round(SL_MIN + 0.05 * k, 2)
                                                          for k in range(int((s["sl_max"] - SL_MIN) / 0.05) + 1)]
    dirs = (1,) if lados == "largos" else (-1,) if lados == "cortos" else (1, -1)
    motivos = []
    for sl in stops:
        ok_d = True
        for d in dirs:
            inst = "spot" if d > 0 else "perp"
            p = params_motor(s, sl, d, None, inst)
            ok, fallos = evolucion.cumple_reglas(p, base=dict(ap.SEIS2))
            pl = ap.impulse_plan(d, 100.0, p, None, inst)
            if not ok or not pl["ok"]:
                ok_d = False
                if sl == stops[0]:
                    motivos.append("; ".join(fallos) if not ok else pl["why"])
                break
        if ok_d:
            return True, [], sl
    txt = "la salida no deja 3 % neto con ese stop" if isinstance(s["sl"], float) else \
        f"la salida no deja 3 % neto con ningún stop entre 1,5 % y {s['sl_max']} %"
    return False, [txt + (f" ({motivos[0]})" if motivos else "")], None
