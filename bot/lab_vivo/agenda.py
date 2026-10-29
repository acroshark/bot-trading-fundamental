"""Pausas del laboratorio vivo en las aperturas de bolsa (docs/07, 11.5): desde 5 min antes hasta 30 min después de
cada apertura de las ventanas del laboratorio (apertura.LAB_WINDOWS con apertura.openings_on: con su cambio de hora y
sin las ventanas con todas sus bolsas en festivo). Los tramos que se solapan se juntan. Fines de semana: sin pausas.

Si apertura no se puede importar (p. ej., a medio editar por otro flujo), vale la agenda.json anterior, que dura 7
días; sin agenda, el modo prudente: en los días laborables, pausa de 00:50 a 04:05, de 08:50 a 09:35 y de 13:50 a
16:05 de Madrid (cubre invierno y verano). Se avisa en estado.json."""
import json
import logging
import os
from datetime import datetime, timedelta

from . import ajustes

log = logging.getLogger("lab_vivo.agenda")
MIN = 60_000
PRUDENTE = (((0, 50), (4, 5)), ((8, 50), (9, 35)), ((13, 50), (16, 5)))
NOMBRES = {"tokio-seul": "Tokio y Seúl", "china": "Shanghái y Hong Kong", "europa": "Europa", "saopaulo": "São Paulo",
           "nuevayork": "Nueva York"}


def _juntar(tramos: list[dict]) -> list[dict]:
    out = []
    for x in sorted(tramos, key=lambda x: x["desde"]):
        if out and x["desde"] <= out[-1]["hasta"]:
            y = out[-1]
            y["hasta"] = max(y["hasta"], x["hasta"])
            y["ventanas"] = sorted(set(y["ventanas"]) | set(x["ventanas"]), key=list(NOMBRES).index)
            y["aperturas"] = sorted(set(y["aperturas"]) | set(x["aperturas"]))
        else:
            out.append({**x, "ventanas": list(x["ventanas"]), "aperturas": list(x["aperturas"])})
    for y in out:
        y["ventana"] = y["ventanas"][0]
        y["nombre"] = " y ".join(NOMBRES.get(w, w) for w in y["ventanas"])
    return out


def calcular(desde_ms: int, dias: int = ajustes.AGENDA_DIAS, openings_on=None) -> list[dict]:
    """Tramos de pausa de [desde_ms, desde_ms + dias): {desde, hasta, ventanas, nombre, aperturas}."""
    if openings_on is None:
        from .. import apertura as ap
        openings_on, ventanas = ap.openings_on, ap.LAB_WINDOWS
    else:
        ventanas = tuple(NOMBRES)
    d0 = datetime.fromtimestamp(desde_ms / 1000, ajustes.MAD).date() - timedelta(days=1)
    hasta_ms = desde_ms + dias * 86_400_000
    tramos = []
    for k in range(dias + 2):
        for o in openings_on(d0 + timedelta(days=k)):
            if o["id"] not in ventanas:
                continue
            a = o["open_ms"] - ajustes.PAUSA_ANTES_MIN * MIN
            b = o["open_ms"] + ajustes.PAUSA_DESPUES_MIN * MIN
            if b > desde_ms and a < hasta_ms:
                tramos.append({"desde": a, "hasta": b, "ventanas": [o["id"]], "aperturas": [o["open_ms"]]})
    return _juntar(tramos)


def prudente(desde_ms: int, dias: int = ajustes.AGENDA_DIAS) -> list[dict]:
    d0 = datetime.fromtimestamp(desde_ms / 1000, ajustes.MAD).date()
    out = []
    for k in range(-1, dias + 1):
        d = d0 + timedelta(days=k)
        if d.weekday() >= 5:
            continue
        for (h1, m1), (h2, m2) in PRUDENTE:
            a = int(datetime(d.year, d.month, d.day, h1, m1, tzinfo=ajustes.MAD).timestamp() * 1000)
            b = int(datetime(d.year, d.month, d.day, h2, m2, tzinfo=ajustes.MAD).timestamp() * 1000)
            out.append({"desde": a, "hasta": b, "ventanas": ["prudente"], "aperturas": [], "ventana": "prudente",
                        "nombre": "modo prudente (sin agenda de aperturas)"})
    return out


def guardar(tramos: list[dict], ruta=None) -> None:
    ruta = ruta or ajustes.AGENDA
    tmp = str(ruta) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"formato": 1, "ts": ajustes.ahora_ms(), "valida_hasta": ajustes.ahora_ms() + ajustes.AGENDA_DIAS * 86_400_000,
                   "tramos": tramos}, f, ensure_ascii=False)
    os.replace(tmp, ruta)


def leer(ruta=None) -> dict | None:
    try:
        with open(ruta or ajustes.AGENDA, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def obtener(ahora_ms: int, openings_on=None) -> tuple[list[dict], str]:
    """(tramos, origen): «calendario» (apertura.openings_on), «guardada» (agenda.json de < 7 días) o «prudente»."""
    try:
        tr = calcular(ahora_ms, openings_on=openings_on)
        try:
            guardar(tr)
        except OSError:
            pass
        return tr, "calendario"
    except Exception as e:   # apertura no se puede importar ahora
        log.warning("Agenda: no se puede calcular con apertura.openings_on (%s)", e)
    g = leer()
    if g and g.get("valida_hasta", 0) > ahora_ms and g.get("tramos"):
        return g["tramos"], "guardada"
    return prudente(ahora_ms), "prudente"


def en_pausa(t: int, tramos: list[dict]) -> dict | None:
    for x in tramos:
        if x["desde"] <= t < x["hasta"]:
            return x
    return None


def proxima(t: int, tramos: list[dict]) -> dict | None:
    fut = [x for x in tramos if x["hasta"] > t]
    return min(fut, key=lambda x: x["desde"]) if fut else None


def puede_empezar(t: int, tramos: list[dict]) -> tuple[bool, str | None]:
    """No se empieza ningún trabajo en una pausa ni a menos de 3 min de una."""
    x = en_pausa(t, tramos)
    if x:
        return False, f"Pausa por la apertura de {x['nombre']} hasta las {ajustes.hora_mad(x['hasta'])}"
    p = proxima(t, tramos)
    if p and p["desde"] - t < ajustes.NO_EMPEZAR_MIN * MIN:
        return False, f"La apertura de {p['nombre']} empieza en menos de 3 min: no se empieza nada"
    return True, None
