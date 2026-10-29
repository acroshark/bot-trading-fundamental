"""Comisiones de OKX EU dentro del sistema (29-10-2026): perfiles, el que está en uso y lo que cuesta cada lado.

El usuario lo pidió así: «las comisiones debes incluirlas en este sistema, no vale solo con que me las menciones».
Perfiles (ayuda oficial de OKX EU, docs/06-modo-real.md, apartado 4):
- «okx_eu_con_xperps» (POR DEFECTO, lo que suponía el paper hasta hoy): con la cuenta de X-Perps abierta, el contado
  cuesta 0,08 % creador / 0,10 % tomador y los X-Perps 0,02 % / 0,05 %. Hay cortos (en X-Perps).
- «okx_eu_sin_xperps»: sin la cuenta de X-Perps, el contado cuesta 0,20 % creador / 0,35 % tomador y NO hay cortos ni
  perpetuos (el motor no abre cortos y el paper los veta con el código «sin_cortos»).
- «cuenta»: la comisión real de la cuenta del usuario, si el conector del modo real la ha leído en solo lectura
  (GET /api/v5/account/trade-fee, solo con las claves del usuario). Mientras exista, manda ella. Solo vale la de la
  cuenta de verdad (modo solo lectura o real, no la demo de OKX, que no es la del usuario). Hay cortos si la cuenta tiene
  X-Perps («xperps» de cuenta_real), aunque el conector no pueda usarlos (acctLv o posMode): eso es de la operativa real,
  no del paper; si no se ha podido leer la comisión del perpetuo, vale la última leída o la oficial con X-Perps.
Se elige con BOT_COMISIONES (por defecto okx_eu_con_xperps; un valor desconocido usa el más caro, okx_eu_sin_xperps, y
la web lo avisa) y desde la web ({"type": "comisiones", "perfil"}; el paper lo guarda en paper_state, ver
PaperBroker.set_comisiones). El estado es de este módulo (proceso): las funciones puras del
paper, del motor de aperturas y de la evolución lo consultan sin tener el broker a mano.

Qué paga cada lado (paper): la entrada (a mercado) y el stop, el cierre por tiempo y el manual (a mercado), comisión de
tomador; el objetivo y la salida parcial (órdenes límite), de creador. Más el deslizamiento de siempre (2 pb por lado
en el coste previsto; el simulado en el precio de las órdenes a mercado) y, en perpetuos, el funding.
La regla del 3 % neto (paper.rule_win_ok, apertura.impulse_plan, evolucion.cumple_reglas) usa el coste de ida y vuelta
de coste_rt_pct: tomador a la entrada Y a la salida más 2 pb por lado, por el lado seguro (si el objetivo sale como
creador, deja algo más). Los backtests siguen con config.FEE_PCT (0,10 % por lado = tomador del contado con X-Perps):
dentro de costes_de_backtest(), las funciones del motor y del paper que miran el perfil (cost_rt_pct, exit_rules,
stop_floor_pct, target_price, entry_plan) usan esa misma comisión, sin mezclar el perfil del bot con la del backtest."""
import contextvars
import logging
from contextlib import contextmanager

from . import config

log = logging.getLogger("comisiones")

CON_XPERPS, SIN_XPERPS, CUENTA = "okx_eu_con_xperps", "okx_eu_sin_xperps", "cuenta"
POR_DEFECTO = CON_XPERPS
SLIP_BPS = 2.0   # deslizamiento supuesto por lado en el coste previsto (como paper.COST_SLIP_BPS y los backtests)
FUENTE = "Ayuda oficial de OKX EU (docs/06-modo-real.md, apartado 4)"

PERFILES = {
    CON_XPERPS: {"nombre": "OKX EU con cuenta X-Perps",
                 "spot": {"creador": 0.08, "tomador": 0.10}, "perp": {"creador": 0.02, "tomador": 0.05},
                 "cortos": True, "fuente": FUENTE},
    SIN_XPERPS: {"nombre": "OKX EU sin cuenta X-Perps",
                 "spot": {"creador": 0.20, "tomador": 0.35}, "perp": None,
                 "cortos": False, "fuente": FUENTE},
}
AVISO_SIN_XPERPS = ("Sin cuenta X-Perps en OKX EU no hay cortos y el contado cuesta 0,35 % por lado como tomador "
                    "(0,20 % como creador); con ella, 0,10 % / 0,08 % y los X-Perps 0,05 % / 0,02 %")
TIPOS = {"tomador": "a mercado (entrada, stop, cierre por tiempo o manual)",
         "creador": "orden límite (objetivo y salida parcial)"}


SI_DESCONOCIDO = SIN_XPERPS   # BOT_COMISIONES mal escrito: el perfil más caro (por el lado seguro), con aviso en la web


def _config_valor() -> str:
    return str(config.COMISIONES or POR_DEFECTO).strip().lower()


def _config() -> str:
    """Perfil de la configuración (BOT_COMISIONES); uno desconocido vale el más caro (SI_DESCONOCIDO): una errata en un
    ajuste de dinero no puede hacer que el paper cobre de menos. Se avisa en el registro y en vista()["aviso_config"]."""
    v = _config_valor()
    if v not in PERFILES:
        if v not in _avisados:
            _avisados.add(v)
            log.warning("BOT_COMISIONES=%s no es un perfil conocido (%s): se usa el más caro, %s", v,
                        ", ".join(PERFILES), SI_DESCONOCIDO)
        return SI_DESCONOCIDO
    return v


def aviso_config() -> str | None:
    """Texto para la web si BOT_COMISIONES no es un perfil conocido (None si lo es)."""
    v = _config_valor()
    if v in PERFILES:
        return None
    return (f"BOT_COMISIONES={v} no es un perfil conocido ({' o '.join(PERFILES)}"
            f"): se usa el más caro, «{PERFILES[SI_DESCONOCIDO]['nombre']}». Corrígelo en tu configuración")


_avisados: set[str] = set()


# Estado del proceso: el perfil elegido (configuración o web) y la comisión real de la cuenta, si se ha leído.
_estado: dict = {"elegido": None, "cuenta": None}
# Comisión fija de un backtest (costes_de_backtest): por contexto, así no toca al bot en vivo aunque el backtest corra a
# la vez en otro hilo o en otra tarea.
BACKTEST = "backtest"
_fijo: contextvars.ContextVar = contextvars.ContextVar("comisiones_backtest", default=None)


@contextmanager
def costes_de_backtest(fee_pct: float | None = None):
    """Dentro del bloque, el perfil es el del backtest: fee_pct (por defecto config.FEE_PCT) por lado en todo, creador
    y tomador, contado y perpetuo, con cortos. Lo usan backtest_aperturas.run_coin y apertura.session_case, que cobran
    esa comisión en simulate_plan: así la puerta R:R, el suelo del stop, el objetivo y la regla del 3 % neto van con los
    mismos costes que el resultado, sea cual sea el perfil del bot."""
    fee = config.FEE_PCT if fee_pct is None else float(fee_pct)
    lado = {"creador": fee, "tomador": fee}
    tok = _fijo.set({"nombre": f"Backtest: {_num(fee)} % por lado", "spot": dict(lado), "perp": dict(lado),
                     "cortos": True, "fuente": "config.FEE_PCT (los backtests)"})
    try:
        yield
    finally:
        _fijo.reset(tok)


def config_perfil() -> str:
    return _config()


def elegido() -> str:
    return _estado["elegido"] or _config()


def activo() -> str:
    """El perfil que se aplica: dentro de un backtest, el suyo; la cuenta real si se ha leído (manda ella); si no, el
    elegido."""
    if _fijo.get() is not None:
        return BACKTEST
    return CUENTA if _estado["cuenta"] else elegido()


def perfil(nombre: str | None = None) -> dict:
    """Datos de un perfil (por defecto, el activo), con su «id»."""
    nombre = nombre or activo()
    p = (_estado["cuenta"] if nombre == CUENTA else _fijo.get() if nombre == BACKTEST else PERFILES.get(nombre))
    if p is None:
        nombre, p = POR_DEFECTO, PERFILES[POR_DEFECTO]
    return {"id": nombre, **p}


def elegir(nombre: str) -> tuple[bool, str | None]:
    """Elige un perfil fijo (la web o el arranque). Con la comisión real de la cuenta leída, manda ella: la elección
    queda guardada para cuando no la haya."""
    if nombre not in PERFILES:
        return False, f"Perfil de comisiones desconocido: {nombre} (usa {' o '.join(PERFILES)})"
    _estado["elegido"] = nombre
    if _estado["cuenta"]:
        return True, ("Elegido; mientras se lea la comisión real de tu cuenta de OKX (solo lectura), manda esa")
    return True, None


def reiniciar() -> None:
    """Vuelve al perfil de la configuración y olvida la cuenta (arranque y pruebas)."""
    _estado.update(elegido=None, cuenta=None)


def poner_cuenta(spot_tomador: float | None, spot_creador: float | None = None, perp_tomador: float | None = None,
                 perp_creador: float | None = None, cortos: bool = False, por_que: str | None = None) -> bool:
    """Comisión real de la cuenta (en % por lado, positiva = se paga). Sin la del contado no hay perfil «cuenta». Un
    creador que falta vale como el tomador (por el lado seguro). Devuelve True si ha cambiado algo."""
    def lado(t, m):
        if t is None:
            return None
        t = max(0.0, float(t))
        return {"creador": max(0.0, float(m)) if m is not None else t, "tomador": t}

    spot = lado(spot_tomador, spot_creador)
    if spot is None:
        return quitar_cuenta()
    perp = lado(perp_tomador, perp_creador) if cortos else None
    nuevo = {"nombre": "Comisión real de tu cuenta de OKX EU (leída en solo lectura)", "spot": spot, "perp": perp,
             "cortos": bool(cortos and perp), "fuente": "GET /api/v5/account/trade-fee con tus claves",
             **({"por_que": por_que} if por_que else {})}
    if nuevo == _estado["cuenta"]:
        return False
    _estado["cuenta"] = nuevo
    log.warning("Comisiones: manda la comisión real de tu cuenta de OKX (contado %s %% tomador, %s)",
                spot["tomador"], "con cortos" if nuevo["cortos"] else "sin cortos")
    return True


def quitar_cuenta() -> bool:
    if _estado["cuenta"] is None:
        return False
    _estado["cuenta"] = None
    log.warning("Comisiones: ya no hay comisión leída de la cuenta de OKX; vuelve el perfil %s", elegido())
    return True


CUENTA_MODOS = ("solo_lectura", "real")   # modos del conector cuya comisión es la de la cuenta de verdad del usuario


def observar(msg: dict) -> None:
    """Gancho del bus: con cada «cuenta_real» (RealBroker.vista_real) pone o quita el perfil «cuenta». Solo vale la
    cuenta de verdad (demo False, modo solo lectura o real): la demo de OKX no es la cuenta del usuario. Los cortos del
    paper dependen de que la cuenta tenga X-Perps («xperps»), no de lo que necesita el conector para operar (acctLv,
    posMode); sin la comisión del perpetuo leída, vale la última leída o la oficial con X-Perps. No lanza."""
    try:
        if not isinstance(msg, dict) or msg.get("type") != "cuenta_real":
            return
        if msg.get("demo") is not False or msg.get("modo") not in CUENTA_MODOS:
            quitar_cuenta()
            return
        com = msg.get("comisiones") or {}
        cre = msg.get("comisiones_creador") or {}
        xp = msg.get("xperps")
        cortos = bool(xp) if isinstance(xp, list) else bool((msg.get("cortos") or {}).get("ok"))
        perp_t, perp_m = com.get("perp"), cre.get("perp")
        if cortos and perp_t is None:   # tiene X-Perps pero no se ha leído su comisión: la última o la oficial
            prev = (_estado["cuenta"] or {}).get("perp") or PERFILES[CON_XPERPS]["perp"]
            perp_t, perp_m = prev["tomador"], prev["creador"]
        por_que = ("Cortos en X-Perps: " + ", ".join(xp) if cortos and isinstance(xp, list)
                   else (msg.get("cortos") or {}).get("por_que"))
        poner_cuenta(com.get("spot"), cre.get("spot"), perp_t, perp_m, cortos, por_que)
    except Exception:
        log.exception("Comisiones: no pude leer la comisión de la cuenta real")


# ---------------------------------------------------------------- tasas y costes

def hay_cortos(nombre: str | None = None) -> bool:
    """¿Hay cortos (y perpetuos) con este perfil? Sin cuenta X-Perps, no."""
    p = perfil(nombre)
    return bool(p.get("cortos") and p.get("perp"))


def tasas(inst: str = "spot", nombre: str | None = None) -> dict:
    """Comisión en % por lado de un instrumento: {perfil, inst, creador, tomador}. Un perpetuo sin X-Perps no existe:
    se devuelven las del contado (por el lado seguro: son las más altas) con «sin_instrumento»."""
    p = perfil(nombre)
    lado = p.get("perp") if inst == "perp" else p.get("spot")
    out = {"perfil": p["id"], "inst": "perp" if inst == "perp" else "spot"}
    if lado is None:
        return {**out, **p["spot"], "sin_instrumento": True}
    return {**out, **lado}


def tasa(inst: str = "spot", tipo: str = "tomador", nombre: str | None = None) -> float:
    return float(tasas(inst, nombre)["creador" if tipo == "creador" else "tomador"])


def coste_rt_pct(inst: str = "spot", nombre: str | None = None) -> float:
    """Coste de ida y vuelta previsto en % del nominal para la regla del 3 % neto: tomador a la entrada y a la salida
    más SLIP_BPS por lado (0,24 % al contado con X-Perps; 0,14 % en X-Perps; 0,74 % al contado sin X-Perps)."""
    return 2 * (tasa(inst, "tomador", nombre) + SLIP_BPS / 100)


def _num(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def texto(nombre: str | None = None) -> str:
    p = perfil(nombre)
    s = f"contado {_num(p['spot']['creador'])} % creador / {_num(p['spot']['tomador'])} % tomador"
    if p.get("perp"):
        s += f"; X-Perps {_num(p['perp']['creador'])} % / {_num(p['perp']['tomador'])} %"
    return s + ("; cortos en X-Perps" if hay_cortos(nombre) else "; sin cortos")


def vista() -> dict:
    """Lo que enseñan la cartera y Resultados: el perfil en uso, sus tasas, si hay cortos, el coste de la regla y el
    aviso de lo que pasa sin cuenta X-Perps. Nunca lleva nada de las claves."""
    p = perfil()
    return {"perfil": p["id"], "nombre": p["nombre"], "elegido": elegido(), "config": _config(), "por_defecto": POR_DEFECTO,
            "cuenta": p["id"] == CUENTA, "spot": dict(p["spot"]), "perp": dict(p["perp"]) if p.get("perp") else None,
            "cortos": hay_cortos(), "texto": texto(), "fuente": p.get("fuente"),
            "regla_coste_pct": {"spot": round(coste_rt_pct("spot"), 4),
                                **({"perp": round(coste_rt_pct("perp"), 4)} if p.get("perp") else {})},
            "deslizamiento_bps": SLIP_BPS, "tipos": dict(TIPOS), "aviso": AVISO_SIN_XPERPS,
            "aviso_config": aviso_config(),
            "perfiles": [{"id": k, "nombre": v["nombre"], "texto": texto(k), "cortos": v["cortos"]}
                         for k, v in PERFILES.items()],
            "backtests": {"comision_pct": config.FEE_PCT, "deslizamiento_bps": SLIP_BPS,
                          "texto": f"Los backtests van con {_num(config.FEE_PCT)} % por lado (tomador del contado con "
                                   "cuenta X-Perps) y 2 pb de deslizamiento"}}
