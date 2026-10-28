"""Evolución de la estrategia: historial de todo lo que cambia el sistema, campeón frente a aspirantes, propuestas de
ajuste dentro de las reglas del usuario e informe semanal. SOLO PAPER: nada de esto opera con dinero real.

- Historial (tabla evolution_log; solo se añade: dos disparadores de SQLite impiden borrar o modificar filas). Cada
  cambio lleva ts, tipo (TIPOS), sujeto, antes, después, motivo en español, evidencia (cifras y tamaño de muestra),
  origen y, si deshace otro, revierte (id del deshecho). Lo escriben este módulo y los demás con registrar() y se lee
  con leer(). Un cambio de estrategia guarda la estrategia entera antes y después: se puede revertir (revertir).
- Campeón: la estrategia que opera el motor de aperturas (paper). Sus operaciones son las «impulso-…» de la tabla trades
  con su versión (tags.estrategia_firma = firma_motor de los parámetros con que el motor la abrió; sin etiqueta, las de
  antes de la integración, que solo cuentan para el preset). Hasta que el motor confirma que cargó la versión del
  archivo (estado «openings», estrategia_cargada) queda «pendiente» (el motor lo recarga solo en ≤ 1 min, cuando no
  hay ninguna apertura en curso; no hace falta reiniciar): no se evalúan su degradación ni sus ajustes. Su referencia
  para comparar es su GEMELO VIRTUAL de la vigilancia (misma salida de 7 días, mismas reglas y costes que los
  aspirantes; con el preset, la variante «…|7d» de LAB_REF que la vigilancia marca en_paper). El paper
  solo sirve de segunda comprobación (y para ver la brecha paper–virtual).
- Aspirantes: las demás estrategias de la vigilancia en vivo (tabla vigilancia y estado «vigilancia», 2.8). Solo pueden
  sustituir al campeón las que el motor sabe operar con SU salida (7 días de seguridad; «sig» y «24h» no: cuenta su
  variante «-7d») y que no son el propio campeón con otro nombre. Cada semana (y a petición) se comparan con datos
  NUEVOS y COMPLETOS (solo operaciones cuya entrada es de hace más de su horizonte, 7 días, cerradas o no: así las
  abiertas no sesgan nada; las «en curso» se muestran aparte). Un aspirante sustituye al campeón con ≥ 40 operaciones
  nuevas, R medio positivo y mayor que el del gemelo con p < 0,05 corregida por Holm entre los k aspirantes
  comparables, sin quedar por debajo del paper, cumpliendo las reglas del usuario y CONFIRMADO en una evaluación
  posterior con datos que no se solapan (≥ 10 operaciones nuevas de cada lado con ventaja). Sin gemelo con ≥ 20
  operaciones comparables no hay promoción (nunca se compara con 0 R). El campeón se propone pausar si en ≥ 30
  operaciones nuevas su R contraído baja de −0,3 y la evidencia es clara (IC 95 % por debajo de 0 y p < 0,05 de que
  pierde). Cada p es el más prudente de dos pruebas: remuestreo por bloques de 7 días y t con errores robustos por
  bloques (el remuestreo solo se queda corto en las colas con pocas operaciones).
- Modo «proponer» (por defecto): la propuesta se guarda (tabla evolution_proposals, tampoco se borra) y se muestra;
  aplicarla o descartarla es cosa del usuario. Modo «automatico» (BOT_EVOLUCION_MODO o desde la web): promociones y
  pausas se aplican solas (una promoción como mucho cada 14 días desde el último cambio o reversión; una pausa solo si
  sigue propuesta en la evaluación de la semana siguiente). Los ajustes de parámetros son SIEMPRE propuestas. Lo que el
  usuario descarta o revierte queda bloqueado: solo se vuelve a proponer con datos posteriores a su decisión (los
  mínimos de siempre) y el modo automático nunca lo aplica solo.
- Lo aplicado se escribe en DATA_DIR/estrategia_activa.json (con su firma); el motor lo lee al arrancar y lo recarga
  en caliente (apertura.ACTIVA_CHECK_S, entre aperturas; leer_estrategia_activa y params_motor). Reglas fijas que nada
  de esto puede romper, aunque BOT_USER_RULES=0: stop ≥ 1,5 %, objetivo ≥ 2R y ≥ 3 % neto de costes, sin
  break-even, stop dinámico ni parciales (cumple_reglas).
- Honestidad estadística: medias contraídas hacia 0 R (como 20 operaciones neutras), peso que se reduce a la mitad
  cada 90 días, mínimos de casos, corrección por número de pruebas (Holm en los aspirantes, Bonferroni en los ajustes)
  y confirmación con datos nuevos. Con poca muestra no se concluye nada.
- Cálculo en un hilo con su propia conexión de lectura; ningún fallo para el bucle (se ve en el mensaje «evolucion»).

Interfaz para otros módulos (fuentes_vivas, aprendizaje, apertura…):
    from bot import evolucion
    evolucion.registrar(store, "fuente_silenciada", "ejemplo.com", antes={"estado": "activa", "peso": 1.0},
                        despues={"estado": "silenciada", "peso": 0.0}, motivo="Acierta 9 de 42 (21 %)…",
                        evidencia={"casos": 42, "aciertos": 9}, origen="fuentes_vivas")
    evolucion.leer(store, tipo="peso_cambiado", desde=ms, limite=100)
  - store: el Store (desde el bucle) o una sqlite3.Connection propia (desde un hilo). Devuelve la entrada guardada.
  - peso_cambiado: antes y después como número o como {"peso": número, …}; el informe lo usa para «suben y bajan».
  - Deshacer desde la web un cambio que gestiona otro módulo: Evolucion(…, reversores={origen (o tipo): fn});
    fn(entrada) → {"ok": bool, "why": texto} (puede ser async) y registra su propia entrada con revierte=entrada["id"].
  - FuentesVivas (bot/fuentes_vivas.py): evo.conectar_fuentes(fv) le pone registrar=evo.registrar_fuente (traduce su
    {accion, id, nombre, antes, despues, motivo, cifras, deshacer…}) y el reversor que deshace con su orden inversa
    (fv.accion) solo si la fuente sigue como la dejó ese cambio y la orden la devuelve a como estaba.
  - Aprendizaje (bot/aprendizaje.py): evo.conectar_aprendizaje(apr) copia cada cambio de peso de medios_peso_hist al
    historial («peso_cambiado», origen «aprendizaje», evidencia.cambio_id y sus cifras; en cada ronda, en lotes), lo
    deshace con apr.revertir(cambio_id) y da al informe sus medios (apr.medios()).
  - Motor de aperturas (bot/apertura.py): firma_motor(params completos) → la versión que copia en tags.estrategia_firma
    de cada ticket y publica en «openings».estrategia_cargada = {firma, ts, pausada, fuente}.
  - Medios para el informe: Evolucion(…, leer_medios=fn); fn() → [{"medio", "peso", "casos", "acierto" (0 a 1),
    "veredicto", …}] (formato de apr.medios(); también vale el antiguo con "peso_antes" y "aciertos"; en un hilo).
"""
import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import config
from .models import now_ms

log = logging.getLogger("evolucion")
MAD = ZoneInfo("Europe/Madrid")
H, DAY = 3_600_000, 86_400_000

ENABLED = os.environ.get("BOT_EVOLUCION", "1") != "0"
MODOS = ("proponer", "automatico")
MODO_DEFECTO = os.environ.get("BOT_EVOLUCION_MODO", "proponer").strip().lower()
ARCHIVO = "estrategia_activa.json"
TICK_S = 300                 # cada cuánto se mira si toca informe (y se copian los cambios de peso del aprendizaje)
REFRESH_S = 600              # cada cuánto se recalculan las cifras de la web
INFORME_H = 7                # el informe de la semana sale el lunes a las 7:00 de Madrid
VIG_ESPERA_S = 900           # al arrancar, lo que se espera a que la vigilancia publique sus estrategias para evaluar

PROMO_MIN_OPS = 40           # operaciones nuevas del aspirante para poder sustituir al campeón
P_MAX = 0.05
CONFIRMA_MIN_OPS = 10        # operaciones posteriores (de cada lado) para confirmar la ventaja en otra evaluación
DEGRADA_R = -0.3             # R contraído del campeón por debajo del cual se propone pausar...
DEGRADA_MIN_OPS = 30         # ... con al menos tantas operaciones nuevas (y evidencia clara de que pierde)
MIN_REF = 20                 # operaciones del gemelo (o del paper) para usarlas de referencia
SEG_MIN = 25                 # casos mínimos de un grupo (y del resto) para proponer un ajuste
PRIOR_K = 20.0               # contracción hacia 0 R: como 20 operaciones neutras
VIDA_MEDIA_D = 90            # decaimiento temporal: una operación de hace 90 días pesa la mitad
BLOQUE_D = 7                 # bloques de días del remuestreo (las operaciones de 7 días se solapan)
N_BOOT = 2000
SEED = 20261027
ENFRIAR_D = 14               # modo automático: días mínimos entre dos promociones (o desde la última reversión)
PAUSA_ESPERA_D = 6           # modo automático: la pausa se aplica sola si sigue propuesta en la evaluación siguiente
MIN_NET_PCT = 3.0            # regla del usuario: el objetivo deja ≥ 3 % neto de costes
COST_SLIP_BPS = 2.0          # deslizamiento por lado del coste de ida y vuelta (como paper.cost_rt_pct)
CHAMP_PREFIX = "impulso-"    # estrategias del motor de aperturas en el paper (apertura.impulse_strategy)
APR_LOTE = 1000              # cambios de peso del aprendizaje que se copian al historial por ronda
DUR_MOTOR = "7d"             # la salida del motor: sin tope de tiempo, solo 7 días de seguridad
HORIZONTE_H = {"7d": 168, "24h": 24, "sig": 168}   # horas tras la entrada en que una operación ya ha terminado seguro
DUR_TXT = {"7d": "sin tope de tiempo, 7 días de seguridad", "24h": "24 horas",
           "sig": "hasta la misma apertura del día siguiente"}
# Parámetros de operativa que forman la versión (firma_motor): con todos iguales, es la misma estrategia
FIRMA_KEYS = ("measure_min", "min_move_pct", "entry", "pb_frac", "entry_end_min", "rvol_min", "vol_filter",
              "btc_filter", "news_mode", "windows", "coins", "sl_pct", "target_r", "max_hold_h", "min_net_pct")

TIPOS = {"fuente_añadida": "Medio añadido", "fuente_silenciada": "Medio silenciado",
         "fuente_retirada": "Medio retirado", "fuente_restaurada": "Medio restaurado",
         "peso_cambiado": "Peso cambiado", "estrategia_promovida": "Estrategia promovida",
         "estrategia_degradada": "Estrategia degradada", "parametro_cambiado": "Parámetro cambiado",
         "informe_semanal": "Informe semanal", "veredicto_medio": "Veredicto de un medio",
         "propuesta_descartada": "Propuesta descartada", "filtro_cambiado": "Filtro de noticias cambiado"}
PROPIOS = ("estrategia_promovida", "estrategia_degradada", "parametro_cambiado")   # los revierte este módulo
NO_REVERSIBLES = ("informe_semanal", "veredicto_medio", "propuesta_descartada",
                  "filtro_cambiado")   # apuntes, no cambios que deshacer (el del código se deshace con el código)
# Cambios del código que alteran la operativa en paper sin pasar por la evolución: entran con un reinicio. Se apuntan una
# sola vez (id fijo) al arrancar, para que el historial y el informe de la cartera de prueba digan desde cuándo cambió.
CAMBIOS_CODIGO = [{
    "id": "codigo-noticias-2026-10-28", "tipo": "filtro_cambiado",
    "sujeto": "Señal de noticias del motor de aperturas (filtro news_mode de la estrategia en paper)",
    "antes": {"macro": "contaban todas las noticias macro y de temas de mercado con dirección, con las cuatro monedas",
              "contexto": "política, empresas y tecnología también iban a la IA local y contaban",
              "predicciones": "60 % la de la ventana cripto y 40 % las de los índices de la ventana"},
    "despues": {"macro": "una noticia macro o geopolítica solo cuenta si la cripto la confirma (reflejo: a su favor al "
                         "menos el mayor de 0,5 % y 2σ de 60 min, en los 60 min siguientes); confirmada, vale para las "
                         "cuatro monedas",
                "contexto": "política, empresas, tecnología y sucesos se captan y se miden, pero no se trían ni cuentan",
                "predicciones": "solo la de la ventana cripto de la moneda (las de los índices ya no cuentan)"},
    "motivo": ("Lo pidió el usuario: solo le interesan las noticias de cripto y, de las demás (guerras, bancos centrales, "
               "aranceles…), las que se ven reflejadas en la cripto; el índice de cada bolsa no le importa. Cambia lo "
               "que filtra la estrategia en paper (news_mode «no_contra»): la cartera de prueba de 100 € (desde el "
               "28-10-2026) mezcla dos regímenes desde este reinicio. Para medirla limpia, empezar otra con "
               "scripts/nueva_cartera_paper.py."),
    "evidencia": {"cambio_codigo": "noticias-2026-10-28",
                  "triadas_con_direccion_24h": 3379, "contexto_que_sale_de_la_senal_24h": 1091,
                  "macro_con_direccion_24h": 1555,
                  "azar_confirmada_pct": {"umbral_fijo_0_5": 18.7, "umbral_2sigma": 5.0},
                  "macro_reales_5_dias": {"n": 1892, "confirmada_pct": 2.9, "contraria_pct": 5.1,
                                          "misma_hora_direccion_al_azar_pct": 3.5},
                  "nota": "La dirección del triaje no se cumple más que al azar: una «confirmada» no prueba causa."}}]
# Acciones de fuentes_vivas → tipos del historial («activar» = una fuente en prueba pasa a activa: sube su peso)
FUENTE_TIPO = {"anadir": "fuente_añadida", "silenciar": "fuente_silenciada", "retirar": "fuente_retirada",
               "restaurar": "fuente_restaurada", "activar": "peso_cambiado", "a_prueba": "peso_cambiado"}
PESO_ESTADO = {"activa": 1.0, "prueba": 0.5, "silenciada": 0.0, "retirada": 0.0}   # como feeds.PESO_ESTADO
# Estado al que lleva cada orden de fuentes_vivas (fuentes_vivas.MANUAL; «restaurar» vuelve a su «previo»)
ACCION_ESTADO = {"silenciar": "silenciada", "retirar": "retirada", "activar": "activa", "a_prueba": "prueba"}
ESTADOS = ("pendiente", "aplicada", "descartada", "caducada", "revertida")
ACCIONES = ("aplicar", "descartar", "revertir", "evaluar", "modo")
AVISO = ("Solo paper: nada de esto opera con dinero real. Al principio hay pocas operaciones y con poca muestra no se "
         "concluye nada: las medias se contraen hacia 0 R, lo antiguo pesa menos y ningún cambio se propone sin los "
         "mínimos de casos, una prueba estadística corregida por el número de pruebas y su confirmación con datos "
         "nuevos.")
REGLAS = {"stop_min_pct": config.RULE_MIN_STOP_PCT, "rr_min": config.RULE_MIN_RR, "objetivo_neto_min_pct": MIN_NET_PCT,
          "break_even": False, "stop_dinamico": False, "parciales": False, "solo_paper": True,
          "texto": "Stop ≥ 1,5 %, objetivo ≥ 2R y ≥ 3 % neto de costes, sin break-even ni stop dinámico; solo paper. "
                   "La evolución nunca las rompe."}
CRITERIOS = {"promocion_min_operaciones": PROMO_MIN_OPS, "p_max": P_MAX, "correccion": "Holm",
             "confirmacion_min_operaciones": CONFIRMA_MIN_OPS, "degradacion_R_contraido": DEGRADA_R,
             "degradacion_min_operaciones": DEGRADA_MIN_OPS, "referencia_min_operaciones": MIN_REF,
             "ajuste_min_casos": SEG_MIN, "contraccion_operaciones": PRIOR_K, "vida_media_dias": VIDA_MEDIA_D,
             "bloque_dias": BLOQUE_D, "enfriamiento_dias": ENFRIAR_D, "pausa_espera_dias": PAUSA_ESPERA_D,
             "horizonte_horas": HORIZONTE_H[DUR_MOTOR],
             "prueba": "la más prudente de remuestreo por bloques de 7 días y t con errores robustos por bloques",
             "texto": f"Un aspirante sustituye al campeón con ≥ {PROMO_MIN_OPS} operaciones nuevas completas, R medio "
                      "positivo y mayor que el del gemelo virtual del campeón con p < 0,05 corregida por Holm entre "
                      f"los aspirantes comparables, y confirmado después con ≥ {CONFIRMA_MIN_OPS} operaciones nuevas; "
                      f"el campeón se propone pausar con R contraído < −0,3 en ≥ {DEGRADA_MIN_OPS} operaciones nuevas "
                      "y evidencia clara de que pierde. Solo cuentan operaciones de hace más de 7 días (cerradas o "
                      "no). Los ajustes de parámetros siempre los aplica el usuario; lo que descartas o reviertes no "
                      "vuelve sin datos posteriores ni se aplica solo."}

# Como laboratorio.WIN_SETS / COIN_SETS (los grupos de los identificadores de estrategia)
WIN_IDS = ("tokio-seul", "china", "europa", "saopaulo", "nuevayork")
WIN_NAME = {"tokio-seul": "Tokio y Seúl", "china": "Shanghái y Hong Kong", "europa": "Europa", "saopaulo": "São Paulo",
            "nuevayork": "Nueva York"}
WIN_SETS = {"todas": WIN_IDS, "nuevayork": ("nuevayork",), "europa": ("europa",), "asia": ("tokio-seul", "china"),
            "america": ("saopaulo", "nuevayork")}
COIN_SETS = {"todas": tuple(config.COINS), "BTC+ETH": ("BTC", "ETH"), "SOL+XRP": ("SOL", "XRP"),
             **{c: (c,) for c in config.COINS}}
MARKET_WIN = {"tokio": "tokio-seul", "seul": "tokio-seul", "shanghai": "china", "hongkong": "china",
              "frankfurt": "europa", "londres": "europa", "madrid": "europa", "saopaulo": "saopaulo",
              "nuevayork": "nuevayork"}
VISTA_PARAMS = ("measure_min", "min_move_pct", "entry", "rvol_min", "vol_filter", "btc_filter", "news_mode", "windows",
                "coins", "sl_pct", "target_r", "max_hold_h", "min_net_pct")
# Por si bot/apertura.py no se puede importar (lo están editando): lo esencial del preset IMPULSO.
LAB_REF_FALLBACK = "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig"
PRESET_FALLBACK = {"estrategia": "impulso", "measure_min": 15, "min_move_pct": 0.2, "entry": "t:30", "pb_frac": 0.5,
                   "entry_end_min": 120, "rvol_min": 1.5, "vol_filter": "todos", "btc_filter": "a_favor",
                   "news_mode": "no_contra", "windows": list(WIN_IDS), "coins": list(config.COINS), "sl_pct": 3.0,
                   "target_r": 3.0, "max_hold_h": 168, "min_net_pct": 3.0}
GRUPO_TXT = {"noticia": {"a_favor": "Con noticia a favor", "en_contra": "Con noticia en contra",
                         "neutral": "Sin noticia clara"},
             "lado": {"largo": "Largos", "corto": "Cortos"},
             "apertura_anterior": {"a_favor": "Apertura anterior a favor", "en_contra": "Apertura anterior en contra",
                                   "sin_dato": "Apertura anterior sin dato"},
             "btc": {"a_favor": "BTC a favor", "en_contra": "BTC en contra", "neutral": "BTC neutral"},
             "volatilidad": {"baja": "Volatilidad baja", "media": "Volatilidad media", "alta": "Volatilidad alta"}}
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre")

SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_log(id TEXT PRIMARY KEY, ts INTEGER NOT NULL, tipo TEXT NOT NULL, sujeto TEXT,
  antes TEXT, despues TEXT, motivo TEXT NOT NULL, evidencia TEXT, origen TEXT, revierte TEXT);
CREATE INDEX IF NOT EXISTS evolution_log_ts ON evolution_log(ts);
CREATE INDEX IF NOT EXISTS evolution_log_tipo ON evolution_log(tipo, ts);
CREATE INDEX IF NOT EXISTS evolution_log_sujeto ON evolution_log(sujeto, ts);
CREATE INDEX IF NOT EXISTS evolution_log_revierte ON evolution_log(revierte);
CREATE TRIGGER IF NOT EXISTS evolution_log_sin_borrar BEFORE DELETE ON evolution_log
  BEGIN SELECT RAISE(ABORT, 'evolution_log: el historial no se borra'); END;
CREATE TRIGGER IF NOT EXISTS evolution_log_sin_cambios BEFORE UPDATE ON evolution_log
  BEGIN SELECT RAISE(ABORT, 'evolution_log: el historial no se modifica'); END;
CREATE TABLE IF NOT EXISTS evolution_proposals(id TEXT PRIMARY KEY, ts INTEGER NOT NULL, tipo TEXT NOT NULL,
  sujeto TEXT, clave TEXT NOT NULL, estado TEXT NOT NULL, cuerpo TEXT NOT NULL, actualizada INTEGER,
  resuelta_ts INTEGER, resuelta_por TEXT, nota TEXT);
CREATE INDEX IF NOT EXISTS evolution_proposals_estado ON evolution_proposals(estado, ts);
CREATE TRIGGER IF NOT EXISTS evolution_proposals_sin_borrar BEFORE DELETE ON evolution_proposals
  BEGIN SELECT RAISE(ABORT, 'evolution_proposals: las propuestas no se borran'); END;
CREATE TABLE IF NOT EXISTS evolution_state(clave TEXT PRIMARY KEY, valor TEXT);
"""
LOG_COLS = ("id", "ts", "tipo", "sujeto", "antes", "despues", "motivo", "evidencia", "origen", "revierte")
PROP_COLS = ("id", "ts", "tipo", "sujeto", "clave", "estado", "cuerpo", "actualizada", "resuelta_ts", "resuelta_por",
             "nota")


# ---------------------------------------------------------------- utilidades

def _f(x, default=None):
    try:
        v = float(x)
        return v if v == v else default   # NaN = sin dato
    except (TypeError, ValueError):
        return default


def _r(x, d: int = 4):
    return None if x is None else round(float(x), d)


def _n(x, d: int = 2) -> str:
    """Número para los textos, con coma decimal."""
    if x is None:
        return "–"
    return f"{round(float(x), d) + 0.0:.{d}f}".replace(".", ",")


def _sg(x, d: int = 2) -> str:
    return "–" if x is None else ("+" if round(float(x), d) > 0 else "") + _n(x, d)


def _fecha(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).strftime("%d-%m-%Y") if ms else "–"


def _media(ops: list[dict]) -> float | None:
    return sum(o["R"] for o in ops) / len(ops) if ops else None


def _jd(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def _dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=_jd)


def _loads(s, default=None):
    if s is None:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _nuevo_id(pref: str, ts: int) -> str:
    return f"{pref}{ts}-{uuid.uuid4().hex[:8]}"


def _semilla(s: str) -> int:
    return int(hashlib.sha1(str(s).encode()).hexdigest()[:8], 16) ^ SEED


def coste_rt_pct() -> float:
    """Coste de ida y vuelta en % del nominal (como paper.cost_rt_pct: 0,24 % con 0,10 % de comisión y 2 pb)."""
    return 2 * (config.FEE_PCT + COST_SLIP_BPS / 100)


# ---------------------------------------------------------------- semanas (lunes a domingo, hora de Madrid)

def _lunes(d: date) -> int:
    m = d - timedelta(days=d.weekday())
    return int(datetime.combine(m, dtime(0), tzinfo=MAD).timestamp() * 1000)


def lunes_de(ms: int) -> int:
    """Lunes a las 0:00 de Madrid de la semana de ms."""
    return _lunes(datetime.fromtimestamp(ms / 1000, MAD).date())


def semana_de(ms: int) -> str:
    y, w, _ = datetime.fromtimestamp(ms / 1000, MAD).date().isocalendar()
    return f"{y}-W{w:02d}"


def semana_limites(semana: str) -> tuple[int, int]:
    """(lunes 0:00, lunes siguiente 0:00) en ms de una semana «AAAA-Wss»."""
    y, w = str(semana).split("-W")
    d = date.fromisocalendar(int(y), int(w), 1)
    return _lunes(d), _lunes(d + timedelta(days=7))


def semana_objetivo(ahora: int) -> str:
    """La última semana completa cuyo informe ya toca (el lunes a las INFORME_H de Madrid)."""
    d = datetime.fromtimestamp(ahora / 1000, MAD).date()
    lun = d - timedelta(days=d.weekday())        # lunes de esta semana
    if ahora < _lunes(d) + INFORME_H * H:        # antes de las 7:00 del lunes, aún no toca la semana pasada
        lun -= timedelta(days=7)
    return semana_de(_lunes(lun - timedelta(days=7)))


def proxima_evaluacion(ahora: int) -> int:
    t = lunes_de(ahora) + INFORME_H * H
    if ahora >= t:
        t = _lunes(datetime.fromtimestamp(ahora / 1000, MAD).date() + timedelta(days=7)) + INFORME_H * H
    return t


def _texto_semana(semana: str) -> str:
    a, b = semana_limites(semana)
    d1 = datetime.fromtimestamp(a / 1000, MAD).date()
    d2 = d1 + timedelta(days=6)
    if d1.year != d2.year:
        return f"del {d1.day} de {MESES[d1.month - 1]} de {d1.year} al {d2.day} de {MESES[d2.month - 1]} de {d2.year}"
    if d1.month != d2.month:
        return f"del {d1.day} de {MESES[d1.month - 1]} al {d2.day} de {MESES[d2.month - 1]} de {d2.year}"
    return f"del {d1.day} al {d2.day} de {MESES[d2.month - 1]} de {d2.year}"


# ---------------------------------------------------------------- historial (evolution_log)

def _db(destino) -> sqlite3.Connection:
    return destino.db if hasattr(destino, "db") else destino


def asegurar_tablas(destino) -> None:
    """Crea las tablas de la evolución si faltan (sin tocar la transacción del que llama si ya están)."""
    db = _db(destino)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evolution_state'").fetchone() is None:
        db.executescript(SCHEMA)


def _entrada(row) -> dict:
    e = dict(zip(LOG_COLS, row))
    for k in ("antes", "despues", "evidencia"):
        e[k] = _loads(e[k])
    e["etiqueta"] = TIPOS.get(e["tipo"], e["tipo"].replace("_", " ").capitalize())
    return e


def _nueva_entrada(tipo: str, sujeto, antes, despues, motivo: str, evidencia, origen: str, revierte, ts: int,
                   id_: str | None = None) -> dict:
    tipo = str(tipo or "").strip()[:40]
    if not tipo:
        raise ValueError("registrar: falta el tipo del cambio")
    if tipo not in TIPOS:
        log.warning("Evolución: tipo de cambio nuevo «%s» (se guarda igual)", tipo)
    motivo = str(motivo or "").strip()
    if not motivo:
        log.warning("Evolución: cambio «%s» de %s sin motivo", tipo, sujeto)
        motivo = "(sin motivo)"
    if evidencia is not None and not isinstance(evidencia, dict):
        evidencia = {"valor": evidencia}
    return {"id": id_ or _nuevo_id("v", ts), "ts": int(ts), "tipo": tipo,
            "sujeto": None if sujeto is None else str(sujeto)[:300], "antes": antes, "despues": despues,
            "motivo": motivo[:2000], "evidencia": evidencia or {}, "origen": str(origen or "")[:60] or None,
            "revierte": revierte, "etiqueta": TIPOS.get(tipo, tipo.replace("_", " ").capitalize())}


def _insertar(db, entradas: list[dict]) -> None:
    asegurar_tablas(db)
    db.executemany("INSERT INTO evolution_log VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [(e["id"], e["ts"], e["tipo"], e["sujeto"], _dumps(e["antes"]), _dumps(e["despues"]), e["motivo"],
                     _dumps(e["evidencia"]), e["origen"], e["revierte"]) for e in entradas])
    db.commit()


def registrar(destino, tipo: str, sujeto, antes=None, despues=None, motivo: str = "", evidencia=None,
              origen: str = "", revierte: str | None = None, ts: int | None = None, id_: str | None = None) -> dict:
    """Añade un cambio al historial y lo devuelve. destino: Store (desde el bucle) o sqlite3.Connection propia (desde
    un hilo). Un tipo nuevo se admite (con aviso en el registro); sin tipo, ValueError. Nada se borra ni se modifica."""
    e = _nueva_entrada(tipo, sujeto, antes, despues, motivo, evidencia, origen, revierte,
                       now_ms() if ts is None else int(ts), id_)
    _insertar(_db(destino), [e])
    return e


def apuntar_cambios_codigo(destino, ts: int | None = None, cambios=None) -> list[dict]:
    """Apunta en el historial los cambios del código (CAMBIOS_CODIGO) que aún no estén (por su id fijo). Devuelve los
    apuntados ahora. Nunca dos veces el mismo, aunque el bot se reinicie."""
    out = []
    for c in (CAMBIOS_CODIGO if cambios is None else cambios):
        if leer(destino, id_=c["id"], limite=1):
            continue
        out.append(registrar(destino, c["tipo"], c["sujeto"], c.get("antes"), c.get("despues"), c["motivo"],
                             c.get("evidencia"), "codigo", ts=ts, id_=c["id"]))
    return out


def desde_fuentes(entrada: dict) -> dict:
    """Entrada de fuentes_vivas ({ts, area, accion, id, nombre, antes, despues, tipo, motivo, cifras, auto, por,
    deshacer}) → argumentos de registrar(). Los estados van con su peso para que el informe vea qué sube y qué baja."""
    acc = str(entrada.get("accion") or "")
    est = lambda s: None if s is None else {"estado": s, "peso": PESO_ESTADO.get(s)}
    cifras = entrada.get("cifras") if isinstance(entrada.get("cifras"), dict) else {}
    return {"tipo": FUENTE_TIPO.get(acc, f"fuente_{acc}" if acc else "fuente_cambio"),
            "sujeto": entrada.get("nombre") or entrada.get("id"), "antes": est(entrada.get("antes")),
            "despues": est(entrada.get("despues")), "motivo": entrada.get("motivo") or "",
            "evidencia": {**cifras, "fuente_id": entrada.get("id"), "regla": entrada.get("tipo"),
                          "accion": acc or None, "auto": entrada.get("auto"), "por": entrada.get("por"),
                          "deshacer": entrada.get("deshacer")}}


def leer(destino, tipo=None, sujeto: str | None = None, origen: str | None = None, desde: int | None = None,
         hasta: int | None = None, limite: int = 200, id_: str | None = None) -> list[dict]:
    """Cambios del historial, del más reciente al más antiguo. tipo: uno, varios (lista) o «a,b». Cada entrada lleva
    revertida_por (id de la entrada que la deshizo, o None). Sin tabla todavía, lista vacía."""
    where, args = [], []
    if isinstance(tipo, str):
        tipo = [t.strip() for t in tipo.split(",") if t.strip()]
    if tipo:
        where.append(f"tipo IN ({','.join('?' * len(tipo))})")
        args += list(tipo)
    for col, val in (("sujeto", sujeto), ("origen", origen), ("id", id_)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if desde is not None:
        where.append("ts >= ?")
        args.append(int(desde))
    if hasta is not None:
        where.append("ts < ?")
        args.append(int(hasta))
    lim = max(1, min(int(limite or 200), 5000))
    sql = (f"SELECT {', '.join(LOG_COLS)} FROM evolution_log {'WHERE ' + ' AND '.join(where) if where else ''} "
           "ORDER BY ts DESC, rowid DESC LIMIT ?")
    db = _db(destino)
    try:
        out = [_entrada(r) for r in db.execute(sql, (*args, lim))]
        ids = [e["id"] for e in out]
        rev = {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            rev.update(dict(db.execute(f"SELECT revierte, id FROM evolution_log WHERE revierte IN "
                                       f"({','.join('?' * len(part))})", part).fetchall()))
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise
    for e in out:
        e["revertida_por"] = rev.get(e["id"])
    return out


# ---------------------------------------------------------------- estrategia activa y reglas del usuario

def _preset() -> tuple[dict, str]:
    """(preset IMPULSO del motor, estrategia del laboratorio que reproduce su entrada)."""
    try:
        from . import apertura
        return dict(apertura.IMPULSO), apertura.LAB_REF
    except Exception:   # el motor no se puede importar ahora mismo: lo esencial del preset
        return dict(PRESET_FALLBACK), LAB_REF_FALLBACK


def gemelo_preset() -> str | None:
    """Estrategia de la vigilancia que reproduce el motor con el preset: LAB_REF con sus 7 días de seguridad (como
    vigilancia.paper_id()); None si el preset no lleva 7 días de seguridad."""
    pre, lab = _preset()
    if not isinstance(lab, str) or _f(pre.get("max_hold_h")) != 168:
        return None
    return lab.rsplit("|", 1)[0] + "|" + DUR_MOTOR


def cumple_reglas(params: dict | None, base: dict | None = None) -> tuple[bool, list[str]]:
    """Reglas fijas del usuario sobre los parámetros del motor (encima del preset IMPULSO o de base). No dependen de
    BOT_USER_RULES: stop ≥ 1,5 %, objetivo ≥ 2R y ≥ 3 % neto de costes, horquilla pura y solo modo impulso."""
    over = dict(params or {})
    p = {**(_preset()[0] if base is None else base), **over}
    fallos = []
    sl, r = _f(p.get("sl_pct")), _f(p.get("target_r"))
    if sl is None or sl < config.RULE_MIN_STOP_PCT - 1e-9:
        fallos.append(f"stop {_n(sl)} % (mínimo {_n(config.RULE_MIN_STOP_PCT)} %)")
    if r is None or r < config.RULE_MIN_RR - 1e-9:
        fallos.append(f"objetivo {_n(r)}R (mínimo {_n(config.RULE_MIN_RR)}R)")
    if sl is not None and r is not None:
        net = sl * r - coste_rt_pct()
        if net < MIN_NET_PCT - 1e-9:
            fallos.append(f"el objetivo deja {_n(net)} % neto de costes (mínimo {_n(MIN_NET_PCT)} %)")
    mn = p.get("min_net_pct")
    if mn is not None and (_f(mn) is None or _f(mn) < MIN_NET_PCT - 1e-9):
        fallos.append(f"min_net_pct = {mn!r} (mínimo {_n(MIN_NET_PCT)} %)")
    if over.get("be_r") not in (None, 0, 0.0):
        fallos.append("break-even (prohibido)")
    if (_f(over.get("trail_atr"), 0.0) or 0.0) > 0 or over.get("trail_start_r") is not None \
            or over.get("trailPct") is not None:
        fallos.append("stop dinámico (prohibido)")
    if (_f(over.get("tp1_frac"), 0.0) or 0.0) > 0:
        fallos.append("salida parcial (prohibida: horquilla pura)")
    if over.get("target_pct") is not None:
        fallos.append("objetivo en % (se usa target_r)")
    if p.get("estrategia", "impulso") != "impulso":
        fallos.append("solo el modo impulso del motor de aperturas")
    return not fallos, fallos


def activa_por_defecto() -> dict:
    """Sin archivo: el preset IMPULSO tal cual (params vacíos = el motor usa su preset). lab_id: la estrategia del
    laboratorio que reproduce su entrada; lab_twin: la de la vigilancia que reproduce también su salida (su gemelo)."""
    _, lab = _preset()
    return {"version": 2, "estrategia": "impulso", "fuente": "preset", "params": {}, "lab_id": lab,
            "lab_twin": gemelo_preset(), "twin_filtro": None, "codigo": None,
            "nombre": "Preset IMPULSO (movimiento de los primeros minutos, 3 % y 3R)", "pausada": False,
            "desde": None, "actualizado": None, "motivo": "Sin cambios de la evolución: el preset del motor",
            "solo_paper": True}


def es_preset(activa: dict) -> bool:
    return activa.get("fuente") == "preset" and not activa.get("params")


def validar_activa(d) -> tuple[bool, str | None]:
    if not isinstance(d, dict) or d.get("estrategia") != "impulso" or not isinstance(d.get("params"), dict):
        return False, "formato no válido (hace falta estrategia «impulso» y params)"
    if d.get("solo_paper") is False:
        return False, "solo se admite paper"
    ok, fallos = cumple_reglas(d["params"])
    return (True, None) if ok else (False, "rompe las reglas del usuario: " + "; ".join(fallos))


def leer_estrategia_activa(path: Path | str | None = None) -> dict | None:
    """Lo que debe operar el motor de aperturas (paper): estrategia_activa.json validado, o None si no existe o no vale
    (entonces, el preset IMPULSO de siempre). Nunca devuelve algo que rompa las reglas del usuario."""
    p = Path(path) if path else config.DATA_DIR / ARCHIVO
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.error("Evolución: no pude leer %s (%s): el motor usará el preset", p, e)
        return None
    ok, why = validar_activa(d)
    if not ok:
        log.error("Evolución: %s no vale (%s): el motor usará el preset", p, why)
        return None
    return d


def params_motor(activa: dict | None) -> dict | None:
    """Parámetros para OpeningEngine(params=…): None = el preset IMPULSO de siempre (sin archivo o sin cambios)."""
    if not activa or (activa.get("fuente") == "preset" and not activa.get("params")):
        return None
    return {"estrategia": "impulso", **activa["params"]}


def escribir_activa(path: Path, d: dict) -> None:
    """Escritura atómica (archivo temporal y reemplazo): el motor nunca lee un archivo a medias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=_jd), encoding="utf-8")
    os.replace(tmp, path)


def firma(activa: dict) -> str:
    """Huella del archivo de estrategia (parámetros, estrategia del laboratorio y pausa): base de las propuestas."""
    key = {"params": activa.get("params") or {}, "lab_id": activa.get("lab_id"), "pausada": bool(activa.get("pausada"))}
    return hashlib.sha1(json.dumps(key, sort_keys=True, default=_jd).encode()).hexdigest()[:12]


def _norm(v):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float, np.number)):
        return round(float(v), 6)
    if isinstance(v, (list, tuple, set)):
        return sorted(str(x).strip().lower() for x in v)
    return str(v).strip().lower()


def firma_motor(p: dict | None) -> str:
    """Versión de lo que opera el motor: huella de sus parámetros de operativa (FIRMA_KEYS, normalizados: 168 = 168,0,
    listas sin orden). El motor la calcula con sus parámetros completos (OpeningEngine.p) y la copia en las etiquetas de
    cada ticket (tags.estrategia_firma): así cada operación paper dice qué versión la abrió. La pausa no cuenta."""
    key = {k: _norm((p or {}).get(k)) for k in FIRMA_KEYS}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def efectivos(activa: dict) -> dict:
    return {**_preset()[0], **(activa.get("params") or {})}


def firma_estrategia(activa: dict) -> str:
    """firma_motor de los parámetros efectivos de una estrategia activa (preset + sus cambios)."""
    return firma_motor(efectivos(activa))


def _num(s: str) -> float:
    return float(str(s).replace(",", "."))


def lab_a_params(sid: str, base: dict | None = None) -> tuple[dict | None, str | None]:
    """Identificador del laboratorio (vigilancia) → parámetros del motor en modo impulso, o (None, por qué no). El motor
    solo sabe operar la familia «Movimiento de los primeros minutos» (ap_mom) con entrada t:N o pb, confirmación de
    volumen relativo o ninguna, todos los días y SU salida (sin tope de tiempo, 7 días de seguridad: lo medido con otra
    salida no es lo que se aplicaría). Las noticias siguen como en el campeón (base)."""
    p = str(sid or "").split("|")
    if len(p) != 11:
        return None, "identificador de estrategia no válido"
    src, conf, vol, btc, win, coins, dia, entry, sl, r, dur = p
    s = src.split(":")
    if s[0] != "ap_mom" or len(s) != 3:
        return None, "el motor de aperturas solo sabe operar la familia «Movimiento de los primeros minutos»"
    try:
        measure, move = int(s[1]), _num(s[2])
        c = conf.split(":")
        if c[0] == "rvol" and len(c) == 2:
            rvol = _num(c[1])
        elif c == ["ninguna"]:
            rvol = None
        else:
            return None, f"confirmación «{conf}» que el motor no tiene"
        e = entry.split(":")
        if e[0] == "t" and len(e) == 2:   # antes de acabar la medida se entra al medir (como el motor)
            entry_s = f"t:{max(int(e[1]), measure)}"
        elif e[0] == "pb" and len(e) == 2 and int(e[1]) == measure:
            entry_s = "pb"
        else:
            return None, f"entrada «{entry}» que el motor no tiene"
        slv, rv = _num(sl), _num(r)
    except ValueError:
        return None, "identificador de estrategia no válido"
    if dia != "todos":
        return None, "filtro de días de la semana que el motor no tiene"
    if vol not in ("todos", "no_baja", "alta", "baja") or btc not in ("ninguno", "no_contra", "a_favor") \
            or win not in WIN_SETS or coins not in COIN_SETS or dur not in HORIZONTE_H:
        return None, "filtros que el motor no tiene"
    if dur != DUR_MOTOR:
        return None, (f"su salida ({DUR_TXT[dur]}) no es la del motor ({DUR_TXT[DUR_MOTOR]}): lo medido no sería lo "
                      "que se aplica; cuenta su variante de 7 días")
    out = {"measure_min": measure, "min_move_pct": move, "entry": entry_s, "rvol_min": rvol, "vol_filter": vol,
           "btc_filter": btc, "windows": list(WIN_SETS[win]), "coins": list(COIN_SETS[coins]), "sl_pct": slv,
           "target_r": rv, "max_hold_h": HORIZONTE_H[dur],
           "news_mode": (base or {}).get("news_mode") or PRESET_FALLBACK["news_mode"]}
    return out, None


# ---------------------------------------------------------------- datos (en un hilo, conexión propia)

def _ventana(market, window, story) -> str | None:
    if market in MARKET_WIN:
        return MARKET_WIN[market]
    for k, name in WIN_NAME.items():
        if window and name in str(window):
            return k
    s = str(story or "")
    if s.startswith("a-"):
        for k in sorted(WIN_IDS, key=len, reverse=True):
            if s[2:].startswith(k + "-"):
                return k
    return None


def op_paper(t: dict) -> dict | None:
    """Operación paper (3.2), cerrada o abierta → forma común de las cuentas (abierta: R y t_out None); None si no
    sirve. firma: la versión del motor que la abrió (tags.estrategia_firma; None en las de antes de la integración)."""
    opened = _f(t.get("opened"))
    if opened is None:
        return None
    R, closed = _f(t.get("R")), _f(t.get("closed"))
    cerrada = R is not None and closed is not None
    tags = t.get("tags") if isinstance(t.get("tags"), dict) else {}
    pnl = _f(t.get("pnl"), R)
    return {"t": int(opened), "t_out": int(closed) if cerrada else None, "R": R if cerrada else None,
            "gana": (pnl or 0) > 0 if cerrada else None, "moneda": t.get("coin"),
            "lado": "largo" if t.get("side") == "long" else "corto",
            "ventana": _ventana(t.get("market"), t.get("window"), t.get("story")),
            "noticia": tags.get("noticia"), "vol_pct": _f(tags.get("volatilidad_pct")),
            "apertura_anterior": tags.get("apertura_anterior"), "btc": tags.get("btc"),
            "firma": tags.get("estrategia_firma") or None,
            "estrategia": t.get("strategy"), "story": t.get("story"), "titular": t.get("headline")}


def op_virtual(r: dict) -> dict | None:
    """Fila de la tabla vigilancia (2.8), cerrada o abierta → forma común; None si es de calentamiento o «sin_datos»
    (abierta que ya no se puede resolver tras un apagón: no cuenta)."""
    cuerpo = r.get("cuerpo")
    if isinstance(cuerpo, str):
        cuerpo = _loads(cuerpo, {})
    cuerpo = cuerpo if isinstance(cuerpo, dict) else {}
    R, t, t_out = _f(r.get("R")), _f(r.get("entrada_t")), _f(r.get("salida_t"))
    if cuerpo.get("previa") or r.get("motivo") == "sin_datos" or t is None:
        return None
    cerrada = R is not None and t_out is not None
    ap = str(r.get("apertura") or "")
    return {"t": int(t), "t_out": int(t_out) if cerrada else None, "R": R if cerrada else None,
            "gana": (_f(r.get("pct"), R) or 0) > 0 if cerrada else None, "moneda": r.get("moneda"),
            "lado": r.get("lado"), "ventana": cuerpo.get("ventana") or (ap[:-11] if len(ap) > 11 else None),
            "estrategia": r.get("estrategia")}


def leer_paper(db) -> list[dict]:
    """Operaciones paper del motor de aperturas (estrategia «impulso-…»), cerradas y abiertas."""
    out = []
    try:
        rows = db.execute("SELECT body FROM trades").fetchall()
    except sqlite3.OperationalError:   # sin tabla trades todavía
        return out
    for (body,) in rows:
        t = _loads(body)
        if isinstance(t, dict) and str(t.get("strategy") or "").startswith(CHAMP_PREFIX):
            o = op_paper(t)
            if o:
                out.append(o)
    return out


def leer_filas_vigilancia(db) -> list[dict]:
    """Filas de la tabla vigilancia (contrato 4), cerradas y abiertas. Sin tabla, lista vacía."""
    cols = ("estrategia", "apertura", "moneda", "lado", "entrada_t", "salida_t", "R", "pct", "motivo", "cuerpo")
    try:
        rows = db.execute(f"SELECT {', '.join(cols)} FROM vigilancia")
        return [dict(zip(cols, r)) for r in rows]
    except sqlite3.OperationalError:
        return []


def completas(ops: list[dict], ahora: int, horizonte_h: float) -> tuple[list[dict], int, int]:
    """(operaciones que cuentan, en curso, sin resolver). Solo cuentan las de horizonte COMPLETO: entrada de hace más
    de horizonte_h, cerradas o no (con su resultado). Contar solo las cerradas sesgaría: de las recientes, las que ya
    cerraron son sobre todo stops (el stop está más cerca que el objetivo). Las recientes van aparte («en curso») y una
    de horizonte completo aún abierta (dato raro) no cuenta («sin resolver»)."""
    corte = ahora - int(horizonte_h * H)
    cuentan = sorted((o for o in ops if o["t"] <= corte and o["R"] is not None), key=lambda o: (o["t_out"], o["t"]))
    en_curso = sum(1 for o in ops if o["t"] > corte)
    sin_resolver = sum(1 for o in ops if o["t"] <= corte and o["R"] is None)
    return cuentan, en_curso, sin_resolver


def _dur(sid: str) -> str:
    return str(sid or "").rsplit("|", 1)[-1]


def _estrategias(meta: list[dict], filas: list[dict], ahora: int) -> list[dict]:
    """Estrategias vigiladas con sus operaciones nuevas (desde su «desde») de horizonte completo. Con la lista de la
    vigilancia, solo las de la lista; sin ella (aún no ha publicado), las de la tabla."""
    by: dict[str, list] = {}
    for r in filas:
        o = op_virtual(r)
        if o and o["estrategia"]:
            by.setdefault(o["estrategia"], []).append(o)

    def item(sid, m, desde):
        dur = _dur(sid)
        hz = HORIZONTE_H.get(dur, HORIZONTE_H[DUR_MOTOR])
        ops, en_curso, sin_res = completas([o for o in by.get(sid, []) if o["t"] >= desde], ahora, hz)
        return {"id": sid, "codigo": m.get("codigo"), "nombre": m.get("nombre") or sid, "clase": m.get("clase"),
                "origen": m.get("origen"), "desde": desde, "backtest": m.get("backtest"), "dur": dur,
                "horizonte_h": hz, "en_paper": bool(m.get("en_paper")), "variante_de": m.get("variante_de"),
                "ops": ops, "en_curso": en_curso, "sin_resolver": sin_res}

    out, seen = [], set()
    for m in meta or []:
        sid = m.get("id") if isinstance(m, dict) else None
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(item(sid, m, int(_f(m.get("desde"), 0) or 0)))
    if not meta:
        for sid, ops in sorted(by.items()):
            out.append(item(sid, {}, min(o["t"] for o in ops)))
    return out


def _filtrar(ops: list[dict], filtro: dict | None) -> list[dict]:
    """Operaciones del gemelo con el filtro de ventanas y monedas de un ajuste (gemelo aproximado)."""
    if not filtro:
        return ops
    w, c = set(filtro.get("windows") or []), set(filtro.get("coins") or [])
    return [o for o in ops if (not w or o.get("ventana") in w) and (not c or o.get("moneda") in c)]


# ---------------------------------------------------------------- estadística

def t_cola(t: float, gl: int) -> float:
    """P(T > t) de una t de Student con gl grados de libertad enteros (fórmulas cerradas de Abramowitz y Stegun
    26.7.3 y 26.7.4; sin scipy)."""
    th = math.atan(abs(t) / math.sqrt(gl))
    s, c = math.sin(th), math.cos(th)
    if gl == 1:
        A = 2 * th / math.pi
    elif gl % 2:
        a, acc = 1.0, c
        for j in range(1, (gl - 3) // 2 + 1):
            a *= 2 * j / (2 * j + 1)
            acc += a * c ** (2 * j + 1)
        A = 2 / math.pi * (th + s * acc)
    else:
        b, acc = 1.0, 1.0
        for j in range(1, (gl - 2) // 2 + 1):
            b *= (2 * j - 1) / (2 * j)
            acc += b * c ** (2 * j)
        A = s * acc
    A = min(1.0, max(0.0, A))   # A = P(|T| < |t|)
    return (1 - A) / 2 if t >= 0 else (1 + A) / 2


def t_robusta(a: list[dict], b: list[dict] | None = None, umbral: float = 0.0, bloque_d: int = BLOQUE_D) -> dict:
    """Diferencia de R medio (a − b, o a − umbral) con errores robustos por bloques de días (clúster: las operaciones
    del mismo bloque se parecen y se solapan) y t de Student con bloques − 1 grados de libertad: (t, gl, p de que a sea
    mejor, p de que sea peor). Con menos de 2 bloques, sin p."""
    out = {"t": None, "gl": None, "p": None, "p_menor": None}
    if not a or (b is not None and not b):
        return out
    t0 = min([o["t"] for o in a] + [o["t"] for o in (b or [])])

    def por_bloque(ops):
        d: dict[int, list] = {}
        for o in ops:
            x = d.setdefault(int((o["t"] - t0) // (bloque_d * DAY)), [0.0, 0])
            x[0] += o["R"]
            x[1] += 1
        return d

    A = por_bloque(a)
    Na = sum(n for _, n in A.values())
    ma = sum(s for s, _ in A.values()) / Na
    B = por_bloque(b) if b is not None else {}
    Nb = sum(n for _, n in B.values())
    mb = sum(s for s, _ in B.values()) / Nb if b is not None else float(umbral)
    claves = set(A) | set(B)
    G = len(claves)
    if G < 2:
        return out
    v = []
    for k in claves:
        sa, na = A.get(k, (0.0, 0))
        x = (sa - ma * na) / Na
        if b is not None:
            sb, nb = B.get(k, (0.0, 0))
            x -= (sb - mb * nb) / Nb
        v.append(x)
    var = G / (G - 1) * sum(x * x for x in v)
    d = ma - mb
    if var <= 1e-18:
        p = 0.0 if d > 0 else 1.0 if d < 0 else 0.5
        return {"t": None, "gl": G - 1, "p": p, "p_menor": 1.0 - p if d else 0.5}
    t = d / math.sqrt(var)
    p = t_cola(t, G - 1)
    return {"t": _r(t, 3), "gl": G - 1, "p": _r(p, 5), "p_menor": _r(1 - p, 5)}


def bootstrap_mejora(a: list[dict], b: list[dict] | None = None, umbral: float = 0.0, n_boot: int = N_BOOT,
                     bloque_d: int = BLOQUE_D, semilla: int = SEED) -> dict:
    """Diferencia de R medio (a − b, o a − umbral sin b), p de una cola de que a sea mejor (p) y de que sea peor
    (p_menor). Remuestreo por bloques de días seguidos con las operaciones de los dos lados juntas: las del mismo día
    se parecen (mismo mercado) y las de varios días se solapan, así que no se tratan como independientes. Con pocas
    operaciones y resultados asimétricos (−1R / +3R) el remuestreo solo se queda corto en las colas (bajo la hipótesis
    nula da p < 0,0025 unas tres veces más de lo debido): p y p_menor son el MAYOR (el más prudente) del remuestreo
    (p_boot) y de la t con errores robustos por bloques (p_t, t_robusta)."""
    out = {"dif": None, "p": None, "p_menor": None, "p_boot": None, "p_t": None, "ic95": None, "n_a": len(a),
           "n_b": len(b) if b is not None else None}
    if not a or (b is not None and not b):
        return out
    ts = [o["t"] for o in a] + [o["t"] for o in (b or [])]
    t0 = min(ts)
    D = int((max(ts) - t0) // DAY) + 1

    def por_dia(ops):
        s, n = np.zeros(D), np.zeros(D)
        for o in ops:
            k = int((o["t"] - t0) // DAY)
            s[k] += o["R"]
            n[k] += 1
        return s, n

    sa, na = por_dia(a)
    ma = sa.sum() / na.sum()
    if b is not None:
        sb, nb = por_dia(b)
        mb = sb.sum() / nb.sum()
    else:
        mb = float(umbral)
    obs = ma - mb
    out["dif"] = _r(obs)
    L = max(1, min(bloque_d, D // 3))
    rng = np.random.default_rng(semilla)
    starts = rng.integers(0, D, size=(n_boot, -(-D // L)))
    idx = ((starts[:, :, None] + np.arange(L)) % D).reshape(n_boot, -1)[:, :D]
    NA = na[idx].sum(1)
    ok = NA > 0
    with np.errstate(divide="ignore", invalid="ignore"):
        dif = sa[idx].sum(1) / NA
        if b is not None:
            NB = nb[idx].sum(1)
            ok &= NB > 0
            dif = dif - sb[idx].sum(1) / NB
        else:
            dif = dif - mb
    dif = dif[ok]
    if len(dif) < n_boot // 2:
        return out
    pb = (1 + int(np.sum(dif - obs >= obs))) / (1 + len(dif))
    pbm = (1 + int(np.sum(dif - obs <= obs))) / (1 + len(dif))
    tr = t_robusta(a, b, umbral, bloque_d)
    out["p_boot"], out["p_t"] = _r(pb, 5), tr["p"]
    out["p"] = _r(max(pb, tr["p"] if tr["p"] is not None else pb), 5)
    out["p_menor"] = _r(max(pbm, tr["p_menor"] if tr["p_menor"] is not None else pbm), 5)
    lo, hi = np.percentile(dif, [2.5, 97.5])
    out["ic95"] = [_r(lo), _r(hi)]
    return out


def contraido(ops: list[dict], ahora: int) -> tuple[float, float]:
    """(R medio contraído hacia 0, número efectivo de operaciones): cada operación pesa 0,5^(edad / 90 días) y se suman
    PRIOR_K operaciones neutras. Con pocas operaciones, el resultado se queda cerca de 0."""
    if not ops:
        return 0.0, 0.0
    w = np.array([0.5 ** (max(0, ahora - (o["t_out"] or o["t"])) / (VIDA_MEDIA_D * DAY)) for o in ops])
    R = np.array([o["R"] for o in ops], float)
    return float((w * R).sum() / (w.sum() + PRIOR_K)), float(w.sum())


def _muestra(n: int) -> str:
    if n == 0:
        return "sin datos"
    if n < 10:
        return "muy poca muestra: no dice nada"
    if n < PROMO_MIN_OPS:
        return "poca muestra: orientativo"
    return "muestra suficiente para comparar"


def resumen(ops: list[dict], ahora: int, n_boot: int = N_BOOT, semilla: int = SEED) -> dict:
    """Cifras de un conjunto de operaciones terminadas: número, R medio y total, ganadoras, R contraído e IC del 95 %
    (remuestreo por bloques, desde 5 operaciones)."""
    n = len(ops)
    rc, neff = contraido(ops, ahora)
    out = {"operaciones": n, "R_medio": None, "R_total": 0.0, "ganadoras": None, "R_contraido": _r(rc),
           "n_efectivo": _r(neff, 1), "ic95": None, "dias": len({o["t"] // DAY for o in ops}), "muestra": _muestra(n)}
    if n:
        R = [o["R"] for o in ops]
        out.update(R_medio=_r(sum(R) / n), R_total=_r(sum(R), 3), ganadoras=_r(sum(bool(o["gana"]) for o in ops) / n))
    if n >= 5:
        out["ic95"] = bootstrap_mejora(ops, None, 0.0, n_boot, semilla=semilla)["ic95"]
    return out


def curva_semanal(ops: list[dict], desde: int | None, ahora: int, semanas: int = 52) -> list[dict]:
    """R de cada semana (por la hora de salida) y acumulado, de lunes a domingo de Madrid, sin huecos."""
    if not ops and not desde:
        return []
    start = lunes_de(min([o["t_out"] for o in ops] + ([desde] if desde else [])))
    by: dict[int, list] = {}
    for o in ops:
        by.setdefault(lunes_de(o["t_out"]), []).append(o["R"])
    out, acc, lun = [], 0.0, start
    end = lunes_de(ahora)
    while lun <= end:
        Rs = by.get(lun, [])
        acc += sum(Rs)
        out.append({"semana": semana_de(lun + DAY), "lunes": lun, "operaciones": len(Rs), "R": _r(sum(Rs), 3),
                    "R_acum": _r(acc, 3)})
        lun = _lunes(datetime.fromtimestamp(lun / 1000, MAD).date() + timedelta(days=7))
    return out[-semanas:]


def _grupo(o: dict, dim: str):
    if dim == "volatilidad":
        v = o.get("vol_pct")
        return None if v is None else "baja" if v < 100 / 3 else "alta" if v >= 200 / 3 else "media"
    return o.get(dim)


def _nombre_grupo(dim: str, val) -> str:
    if dim == "ventana":
        return f"Apertura de {WIN_NAME.get(val, val)}"
    if dim == "moneda":
        return str(val)
    return GRUPO_TXT.get(dim, {}).get(val, f"{dim} {val}")


# ---------------------------------------------------------------- comparación y propuestas (funciones puras)

def _confirmacion_motor(activa: dict, f_act: str, motor: dict | None) -> dict:
    """¿El motor opera ya la versión del archivo? Lo dice su estado «openings».estrategia_cargada; sin ese dato solo se
    da por bueno el preset de siempre (antes de la integración)."""
    m = motor if isinstance(motor, dict) and motor.get("firma") else None
    pausa = bool(activa.get("pausada"))
    if m is not None:
        ok = str(m["firma"]) == f_act
        cargada = int(_f(m.get("ts"), 0) or 0) or None
        if ok:
            texto = f"El motor confirma que opera esta versión ({f_act})" + (
                f"; la cargó por última vez el {_fecha(cargada)}." if cargada else ".")
        else:
            texto = (f"Pendiente: el motor opera otra versión ({m['firma']}"
                     f"{', cargada el ' + _fecha(cargada) if cargada else ''}). Carga {ARCHIVO} solo en ≤ 1 min, "
                     "cuando no haya ninguna apertura en curso (no hace falta reiniciar; si no la carga, puede que use "
                     "otros parámetros, p. ej. BOT_IMPULSO_PARAMS). Hasta que cargue esta, sus operaciones no cuentan "
                     "para ella y no se evalúan ni su degradación ni sus ajustes.")
    else:
        ok, cargada = es_preset(activa), None
        texto = ("El motor aún no informa de qué versión opera: se supone el preset de siempre." if ok else
                 f"Pendiente: el motor aún no ha confirmado esta versión ({f_act}); carga {ARCHIVO} solo en ≤ 1 min, "
                 "cuando no haya ninguna apertura en curso. Hasta entonces sus operaciones no cuentan para ella y no se "
                 "evalúan ni su degradación ni sus ajustes.")
    pausa_ok = (not pausa) or (m is not None and ok and bool(m.get("pausada")))
    if pausa and not pausa_ok:
        texto += " La pausa se aplica cuando el motor la cargue (en ≤ 1 min, sin ninguna apertura en curso)."
    return {"confirmado": ok, "cargada": cargada if ok else None, "texto": texto,
            "firma_motor": m["firma"] if m else None, "pausa_confirmada": pausa_ok}


def _gemelo(est: list[dict], activa: dict, ef: dict, f_act: str) -> tuple[dict | None, set]:
    """(gemelo virtual del campeón, ids que son el propio campeón con otro nombre). El gemelo es el lab_twin del
    archivo; con el preset, el que la vigilancia marca en_paper; si no, cualquiera cuyos parámetros sean los del
    campeón."""
    noops = set()
    for e in est:
        p, _ = lab_a_params(e["id"], ef)
        if p is not None and firma_motor({**ef, **p}) == f_act:
            noops.add(e["id"])
    want = activa.get("lab_twin") or (gemelo_preset() if es_preset(activa) else None)
    twin = next((e for e in est if want and e["id"] == want), None)
    if twin is None and es_preset(activa):
        twin = next((e for e in est if e.get("en_paper")), None)
    if twin is None:
        twin = next((e for e in est if e["id"] in noops), None)
    return twin, noops


def _comparar(e: dict, twin_ops: list[dict] | None, twin_desde, paper: list[dict], ef: dict, ahora: int,
              n_boot: int, piso: int | None = None) -> dict:
    """Un aspirante frente al gemelo virtual del campeón (mismas reglas y costes) con datos nuevos y completos del
    mismo periodo; el paper del campeón, como segunda comprobación. El p se corrige después (Holm, en calcular)."""
    params, por_que = lab_a_params(e["id"], ef)
    reglas_ok, fallos = cumple_reglas(params) if params else (False, [])
    inicio = max(e["desde"] or 0, twin_desde or 0, piso or 0)
    a_ops = [o for o in e["ops"] if o["t"] >= inicio]
    ref = [o for o in twin_ops if o["t"] >= inicio] if twin_ops is not None else None
    n = len(a_ops)
    vacio = {"dif": None, "p": None, "ic95": None}
    bt = bootstrap_mejora(a_ops, ref, 0.0, n_boot, semilla=_semilla(e["id"])) \
        if ref is not None and len(ref) >= MIN_REF and a_ops else vacio
    m_a = _media(a_ops)
    rc_a, _ = contraido(a_ops, ahora)
    pap = [o for o in paper if o["t"] >= inicio]
    m_p = _media(pap) if len(pap) >= MIN_REF else None
    faltan = []
    if params is None:
        faltan.append(f"el motor no sabe operarla: {por_que}")
    elif not reglas_ok:
        faltan.append("rompe las reglas del usuario: " + "; ".join(fallos))
    if n < PROMO_MIN_OPS:
        faltan.append(f"lleva {n} operaciones nuevas completas comparables (hacen falta {PROMO_MIN_OPS})")
    if ref is None:
        faltan.append("el campeón no tiene gemelo en la vigilancia (mismas reglas y costes): no hay con qué compararlo")
    elif len(ref) < MIN_REF:
        faltan.append(f"el gemelo del campeón lleva {len(ref)} operaciones comparables (hacen falta {MIN_REF})")
    elif bt["dif"] is None or bt["dif"] <= 0:
        faltan.append("su R medio no supera al del gemelo del campeón")
    if n and (m_a <= 0 or rc_a <= 0):
        faltan.append(f"su R medio no es positivo ({_sg(m_a)}, contraído {_sg(rc_a)})")
    if m_p is not None and n and m_a <= m_p:
        faltan.append(f"no supera al campeón en paper (R medio {_sg(m_p)} en {len(pap)} operaciones)")
    piso_txt = f"; solo cuentan datos posteriores a tu decisión del {_fecha(piso)}" if piso else ""
    return {"id": e["id"], "codigo": e["codigo"], "nombre": e["nombre"], "clase": e["clase"], "origen": e["origen"],
            "desde": e["desde"], "backtest": e.get("backtest"), "dur": e.get("dur"),
            "variante_de": e.get("variante_de"),
            "compatible": params is not None, "por_que_no": por_que, "params": params,
            "reglas_ok": bool(params) and reglas_ok, "vivo": resumen(e["ops"], ahora, n_boot, _semilla(e["id"])),
            "en_curso": e.get("en_curso", 0), "sin_resolver": e.get("sin_resolver", 0),
            "curva": curva_semanal(e["ops"], e["desde"], ahora),
            "frente": {"referencia": "virtual" if ref is not None else None,
                       "texto": ("operaciones virtuales del gemelo del campeón en la vigilancia (mismas reglas y "
                                 "costes)" if ref is not None else "el campeón no tiene gemelo en la vigilancia")
                       + piso_txt,
                       "desde": inicio or None, "operaciones": n, "R_medio": _r(m_a), "R_contraido": _r(rc_a),
                       "ref_operaciones": len(ref) if ref is not None else 0, "ref_R_medio": _r(_media(ref or [])),
                       "paper_operaciones": len(pap), "paper_R_medio": _r(m_p), "dif_R": bt["dif"], "p": bt["p"],
                       "ic95": bt["ic95"], "pruebas": None, "p_max_corregido": None, "bloqueo_desde": piso,
                       "cumple": False, "faltan": faltan, "confirmacion": None},
            "_a": a_ops, "_r": ref or []}


def _holm(asp: list[dict]) -> int:
    """Corrección de Holm entre los k aspirantes comparables (el motor sabe operarlos y cumplen las reglas): el de p
    más bajo necesita p < 0,05/k, el siguiente < 0,05/(k − 1)… y al primero que no pasa, ninguno más. Devuelve k."""
    fam = [a for a in asp if a["compatible"] and a["reglas_ok"]]
    k = len(fam)
    sigue = True
    for i, a in enumerate(sorted(fam, key=lambda a: a["frente"]["p"] if a["frente"]["p"] is not None else 1.0)):
        fr = a["frente"]
        thr = P_MAX / (k - i)
        fr["pruebas"], fr["p_max_corregido"] = k, _r(thr, 5)
        ok = sigue and fr["p"] is not None and fr["p"] < thr
        sigue = ok
        if not ok and fr["dif_R"] is not None and fr["dif_R"] > 0:
            fr["faltan"].append(f"la ventaja puede ser suerte (p = {_n(fr['p'], 4)}; con {k} aspirante"
                                f"{'s' if k != 1 else ''} comparable{'s' if k != 1 else ''} hace falta < "
                                f"{_n(thr, 4)})")
    for a in asp:
        a["frente"]["cumple"] = not a["frente"]["faltan"]
    return k


def _confirmar(a: dict, ob: dict | None, ahora: int) -> tuple[dict | None, dict | None]:
    """Segunda mirada con datos que no se solapan: (nueva observación o None, confirmacion para la web). Cuando un
    aspirante cumple por primera vez queda «en observación»; se confirma si en las operaciones posteriores a esa fecha
    (≥ CONFIRMA_MIN_OPS de cada lado) sigue por encima del gemelo. Si no, vuelve a observación desde hoy."""
    fr = a["frente"]
    if not fr["cumple"]:
        return None, None
    if ob is None:
        return ({"ts": ahora, "operaciones": fr["operaciones"], "dif_R": fr["dif_R"], "p": fr["p"]},
                {"estado": "en_observacion", "desde": ahora,
                 "texto": f"Cumple por primera vez: se confirma en otra evaluación con {CONFIRMA_MIN_OPS} operaciones "
                          "posteriores de cada lado que mantengan la ventaja."})
    t0 = int(ob["ts"])
    an = [o for o in a["_a"] if o["t"] >= t0]
    rn = [o for o in a["_r"] if o["t"] >= t0]
    base = {"desde": t0, "operaciones_nuevas": len(an), "ref_operaciones_nuevas": len(rn)}
    if len(an) < CONFIRMA_MIN_OPS or len(rn) < CONFIRMA_MIN_OPS:
        return ob, {**base, "estado": "esperando",
                    "texto": f"Cumple desde el {_fecha(t0)}: falta confirmarlo con {CONFIRMA_MIN_OPS} operaciones "
                             f"posteriores de cada lado (lleva {len(an)}; el gemelo, {len(rn)})."}
    d = _media(an) - _media(rn)
    if d > 0:
        return ob, {**base, "estado": "confirmada", "dif_R_nuevas": _r(d),
                    "texto": f"Confirmado: en {len(an)} operaciones posteriores al {_fecha(t0)} mantiene la ventaja "
                             f"({_sg(d)} R frente a {len(rn)} del gemelo)."}
    return ({"ts": ahora, "operaciones": fr["operaciones"], "dif_R": fr["dif_R"], "p": fr["p"]},
            {**base, "estado": "no_confirmada", "dif_R_nuevas": _r(d),
             "texto": f"No se confirmó: en {len(an)} operaciones posteriores al {_fecha(t0)} no mantuvo la ventaja "
                      f"({_sg(d)} R); vuelve a observación desde hoy."})


def _bloqueo_txt(b: dict | None) -> str:
    if not b:
        return ""
    que = "la descartaste" if b.get("motivo") == "descartada" else "la revertiste"
    return (f" Ya {que} el {_fecha(b.get('ts'))}: vuelve solo con datos posteriores a esa fecha y, aunque el modo sea "
            "automático, no se aplica sola.")


def ajustes(ops: list[dict], activa: dict, ef: dict, ahora: int, n_boot: int = N_BOOT, bloqueos: dict | None = None,
            lab_twin: str | None = None) -> list[dict]:
    """Propuestas de ajuste de filtros del campeón con sus operaciones en vivo: dejar fuera un grupo (una apertura, una
    moneda, las operaciones sin noticia o con volatilidad baja) que va claramente peor que el resto. Exige SEG_MIN casos
    en el grupo y en el resto, R contraído del grupo < 0 y p < 0,05 corregido por el número de pruebas (Bonferroni).
    Un ajuste descartado o revertido solo cuenta con datos posteriores. Las salidas (stop y objetivo) no se tocan aquí:
    sus variantes se comparan como aspirantes de la vigilancia."""
    bloqueos = bloqueos or {}
    over = activa.get("params") or {}
    wins, coins = list(ef.get("windows") or []), list(ef.get("coins") or [])
    base = firma(activa)
    grupos = []
    for w in sorted({o["ventana"] for o in ops if o.get("ventana")}):
        if w in wins and len(wins) > 1:
            grupos.append(("ventana", w, {"windows": [x for x in wins if x != w]},
                           f"Quitar la apertura de {WIN_NAME.get(w, w)}"))
    for c in sorted({o["moneda"] for o in ops if o.get("moneda")}):
        if c in coins and len(coins) > 1:
            grupos.append(("moneda", c, {"coins": [x for x in coins if x != c]}, f"Dejar de operar {c}"))
    nm = ef.get("news_mode")
    if nm in ("no_contra", "etiqueta"):
        grupos.append(("noticia", "neutral", {"news_mode": "a_favor"}, "Operar solo con noticias a favor"))
    if nm == "etiqueta":
        grupos.append(("noticia", "en_contra", {"news_mode": "no_contra"}, "No operar con noticias en contra"))
    if ef.get("vol_filter") == "todos":
        grupos.append(("volatilidad", "baja", {"vol_filter": "no_baja"}, "No operar con volatilidad baja"))
    tests = []
    for dim, val, cambio, texto in grupos:
        clave = f"ajuste|{dim}|{val}|{base}"
        piso = (bloqueos.get(clave) or {}).get("ts")
        xs = [o for o in ops if piso is None or o["t"] >= piso]
        seg = [o for o in xs if _grupo(o, dim) == val]
        resto = [o for o in xs if _grupo(o, dim) != val]
        if len(seg) >= SEG_MIN and len(resto) >= SEG_MIN:
            tests.append((dim, val, cambio, texto, seg, resto, clave))
    out, m = [], len(tests)
    for dim, val, cambio, texto, seg, resto, clave in tests:
        rc, _ = contraido(seg, ahora)
        bt = bootstrap_mejora(resto, seg, 0.0, n_boot, semilla=_semilla(f"{dim}:{val}"))
        if rc >= 0 or bt["dif"] is None or bt["dif"] <= 0 or bt["p"] is None or bt["p"] >= P_MAX / m:
            continue
        ok, fallos = cumple_reglas({**over, **cambio})
        if not ok:   # no pasa con los filtros, pero por si acaso
            continue
        bl = bloqueos.get(clave)
        m_seg, m_rest = _media(seg), _media(resto)
        # El gemelo sigue valiendo si se quitan aperturas o monedas (filtrado) o se cambian las noticias (la vigilancia
        # no las mira: aspirantes y gemelo van igual sin ellas); con otra volatilidad, ya no
        twin = lab_twin if set(cambio) <= {"windows", "coins", "news_mode"} else None
        c = {"tipo": "parametro_cambiado", "sujeto": f"{'/'.join(cambio)}: {texto.lower()}", "clave": clave,
             "titulo": texto,
             "texto": f"{texto}: en {len(seg)} operaciones da R medio {_sg(m_seg)} frente a {_sg(m_rest)} del resto "
                      f"({len(resto)}); p = {_n(bt['p'], 4)} con corrección por {m} pruebas. Medido con los mismos "
                      "datos que lo sugieren: si se aplica, se sigue vigilando."
                      + ("" if twin else " Tras este ajuste el campeón no tendrá gemelo en la vigilancia: no se "
                                         "podrán comparar aspirantes con él hasta revertirlo.") + _bloqueo_txt(bl),
             "cambio": {"params": cambio, "lab_twin": twin}, "base_firma": base,
             "evidencia": {"grupo": dim, "valor": val, "operaciones_grupo": len(seg), "R_medio_grupo": _r(m_seg),
                           "R_contraido_grupo": _r(rc), "operaciones_resto": len(resto), "R_medio_resto": _r(m_rest),
                           "dif_R": bt["dif"], "p": bt["p"], "pruebas": m, "p_max_corregido": _r(P_MAX / m, 5),
                           "datos_desde": (bl or {}).get("ts")}}
        if bl:
            c["bloqueada"] = bl
        out.append(c)
    return out


def calcular(paper: list[dict], meta: list[dict], filas: list[dict], activa: dict, ahora: int,
             n_boot: int = N_BOOT, motor: dict | None = None, bloqueos: dict | None = None,
             observacion: dict | None = None) -> dict:
    """Panorama completo: campeón (paper de su versión y su gemelo virtual), aspirantes frente a él y propuestas
    candidatas. motor: «openings».estrategia_cargada; bloqueos: {clave: {ts, motivo…}} (descartadas y revertidas);
    observacion: {clave: {ts…}} (aspirantes que ya cumplieron una vez). Devuelve también la observación nueva."""
    bloqueos, observacion = dict(bloqueos or {}), dict(observacion or {})
    ef = efectivos(activa)
    f_act = firma_estrategia(activa)
    base = firma(activa)
    conf = _confirmacion_motor(activa, f_act, motor)
    hz = _f(ef.get("max_hold_h"), HORIZONTE_H[DUR_MOTOR]) or HORIZONTE_H[DUR_MOTOR]
    mias = [o for o in paper if o.get("firma") == f_act or (o.get("firma") is None and es_preset(activa))]
    champ, en_curso, sin_res = completas(mias, ahora, hz)
    cerradas = sorted((o for o in mias if o["R"] is not None), key=lambda o: o["t_out"])
    # «Desde»: cuando se aplicó esta versión o, con el preset, su primera operación (la carga del motor cambia en cada
    # arranque: solo confirma la versión)
    desde = activa.get("desde") or min((o["t"] for o in mias), default=None)
    est = _estrategias(meta, filas, ahora)
    twin, noops = _gemelo(est, activa, ef, f_act)
    filtro = activa.get("twin_filtro") if twin is not None and activa.get("lab_twin") == twin["id"] else None
    twin_ops = _filtrar(twin["ops"], filtro) if twin is not None else None
    ok, fallos = cumple_reglas(activa.get("params") or {})
    vivo = resumen(champ, ahora, n_boot, _semilla("campeon"))
    brecha = None
    if twin_ops and champ:
        ini = max(twin["desde"] or 0, champ[0]["t"])
        pp, vv = [o for o in champ if o["t"] >= ini], [o for o in twin_ops if o["t"] >= ini]
        if pp and vv:
            brecha = {"desde": ini, "paper_operaciones": len(pp), "virtual_operaciones": len(vv),
                      "R_paper": _r(_media(pp)), "R_virtual": _r(_media(vv)), "dif_R": _r(_media(pp) - _media(vv)),
                      "texto": f"Brecha paper–virtual: {_sg(_media(pp) - _media(vv))} R ({len(pp)} operaciones paper, "
                               f"{len(vv)} virtuales; el paper filtra por noticias y límites diarios y tiene costes "
                               "reales)."}
    # Degradación: solo con la versión confirmada, datos posteriores a un bloqueo y evidencia clara
    piso_p = (bloqueos.get(f"pausa|{base}") or {}).get("ts")
    deg = [o for o in champ if piso_p is None or o["t"] >= piso_p]
    rc_d, _ = contraido(deg, ahora)
    btd = bootstrap_mejora(deg, None, 0.0, n_boot, semilla=_semilla("degradacion")) if len(deg) >= 5 else {}
    evid = bool(btd.get("ic95") and btd["ic95"][1] < 0 and btd.get("p_menor") is not None and btd["p_menor"] < P_MAX)
    degradado = conf["confirmado"] and len(deg) >= DEGRADA_MIN_OPS and rc_d < DEGRADA_R and evid
    campeon = {"nombre": activa.get("nombre"), "codigo": activa.get("codigo") or (twin or {}).get("codigo"),
               "lab_id": activa.get("lab_id"), "fuente": activa.get("fuente"), "pausada": bool(activa.get("pausada")),
               "desde": desde, "firma": f_act, "confirmado": conf["confirmado"],
               "pendiente_reinicio": not conf["confirmado"] or not conf["pausa_confirmada"], "motor": conf,
               "params": activa.get("params") or {}, "efectivos": {k: ef.get(k) for k in VISTA_PARAMS},
               "reglas": {"ok": ok, "fallos": fallos}, "vivo": vivo, "en_curso": en_curso, "sin_resolver": sin_res,
               "curva": curva_semanal(champ, desde, ahora),
               "gemelo": {"id": twin["id"], "codigo": twin.get("codigo"), "filtro": filtro,
                          "texto": "aproximado: el gemelo sin lo que quitó el ajuste" if filtro else None}
               if twin is not None else None,
               "virtual": resumen(twin_ops, ahora, n_boot, _semilla(twin["id"])) if twin is not None else None,
               "virtual_en_curso": twin.get("en_curso") if twin is not None else None,
               "curva_virtual": curva_semanal(twin_ops, twin["desde"], ahora) if twin is not None else None,
               "brecha_paper_virtual": brecha, "degradado": degradado,
               "degradacion": {"evaluada": conf["confirmado"], "operaciones": len(deg), "R_medio": _r(_media(deg)),
                               "R_contraido": _r(rc_d), "ic95": btd.get("ic95"), "p_menor": btd.get("p_menor"),
                               "evidencia_clara": evid, "datos_desde": piso_p}}
    # Aspirantes (sin el gemelo ni las copias del campeón)
    asp = [_comparar(e, twin_ops, twin["desde"] if twin is not None else None, champ, ef, ahora, n_boot,
                     (bloqueos.get(f"promo|{e['id']}|{base}") or {}).get("ts"))
           for e in est if (twin is None or e["id"] != twin["id"]) and e["id"] not in noops]
    var7 = {e.get("variante_de"): e.get("codigo") or e["id"] for e in est if e.get("variante_de")}
    for a in asp:
        if a["por_que_no"] and "salida" in a["por_que_no"] and var7.get(a["id"]):
            a["por_que_no"] += f" ({var7[a['id']]})"
    k = _holm(asp)
    nueva_obs = {}
    for a in asp:
        clave = f"promo|{a['id']}|{base}"
        a["frente"]["clave"] = clave
        ob, a["frente"]["confirmacion"] = _confirmar(a, observacion.get(clave), ahora)
        if ob is not None:
            nueva_obs[clave] = ob
    asp.sort(key=lambda a: -(a["vivo"]["R_contraido"] or 0))
    cand = []
    buenos = sorted((a for a in asp if a["frente"]["cumple"]
                     and (a["frente"]["confirmacion"] or {}).get("estado") == "confirmada"),
                    key=lambda a: -(a["frente"]["R_contraido"] or 0))
    if buenos:
        a, fr = buenos[0], buenos[0]["frente"]
        bl = bloqueos.get(fr["clave"])
        net = a["params"]["sl_pct"] * a["params"]["target_r"] - coste_rt_pct()
        pap = (f" En paper el campeón lleva {fr['paper_operaciones']} operaciones con R medio "
               f"{_sg(fr['paper_R_medio'])}: también lo supera." if fr["paper_R_medio"] is not None else "")
        c = {"tipo": "estrategia_promovida", "sujeto": f"{a['codigo'] or a['id'][:40]} · {a['nombre'][:90]}",
             "clave": fr["clave"], "titulo": f"Sustituir el campeón por {a['codigo'] or 'aspirante'}",
             "texto": f"{a['codigo'] or 'El aspirante'} lleva {fr['operaciones']} operaciones nuevas completas con R "
                      f"medio {_sg(fr['R_medio'])} frente a {_sg(fr['ref_R_medio'])} del gemelo virtual del campeón "
                      f"({fr['ref_operaciones']}, mismas reglas y costes); diferencia {_sg(fr['dif_R'])} R con p = "
                      f"{_n(fr['p'], 4)} (corrección de Holm por {k} aspirante{'s' if k != 1 else ''}: hacía falta < "
                      f"{_n(fr['p_max_corregido'], 4)}; la más prudente de remuestreo por bloques de {BLOQUE_D} días y "
                      "t robusta). "
                      f"{fr['confirmacion']['texto']}{pap} Cumple las reglas del usuario: stop "
                      f"{_n(a['params']['sl_pct'])} %, {_n(a['params']['target_r'])}R y {_n(net)} % neto de costes. "
                      "Solo paper." + _bloqueo_txt(bl),
             "cambio": {"lab_id": a["id"], "lab_twin": a["id"], "codigo": a["codigo"], "nombre": a["nombre"],
                        "params": a["params"]},
             "base_firma": base,
             "evidencia": {"aspirante": a["id"], "operaciones": fr["operaciones"], "R_medio": fr["R_medio"],
                           "R_contraido": fr["R_contraido"], "referencia": fr["referencia"],
                           "gemelo": twin["id"] if twin is not None else None, "ref_operaciones": fr["ref_operaciones"],
                           "ref_R_medio": fr["ref_R_medio"], "paper_operaciones": fr["paper_operaciones"],
                           "paper_R_medio": fr["paper_R_medio"], "dif_R": fr["dif_R"], "p": fr["p"], "ic95": fr["ic95"],
                           "pruebas": k, "p_max_corregido": fr["p_max_corregido"], "desde": fr["desde"],
                           "confirmacion": fr["confirmacion"],
                           "tambien_cumplen": [x["codigo"] or x["id"] for x in buenos[1:]]}}
        if bl:
            c["bloqueada"] = bl
        cand.append(c)
    if degradado and not campeon["pausada"]:
        bl = bloqueos.get(f"pausa|{base}")
        ic = btd.get("ic95") or [None, None]
        c = {"tipo": "estrategia_degradada", "sujeto": activa.get("nombre") or "campeón", "clave": f"pausa|{base}",
             "titulo": "Pausar el campeón",
             "texto": f"El campeón lleva {len(deg)} operaciones nuevas completas con R medio {_sg(_media(deg))} "
                      f"(contraído hacia 0: {_sg(rc_d)}; IC 95 % de {_sg(ic[0])} a {_sg(ic[1])}; p = "
                      f"{_n(btd.get('p_menor'), 4)} de que sea solo una mala racha): por debajo de {_sg(DEGRADA_R, 1)} "
                      f"con al menos "
                      f"{DEGRADA_MIN_OPS} y con evidencia clara. Se propone pausarlo hasta revisarlo. Solo paper."
                      + _bloqueo_txt(bl),
             "cambio": {"pausada": True}, "base_firma": base,
             "evidencia": {"operaciones": len(deg), "R_medio": _r(_media(deg)), "R_contraido": _r(rc_d),
                           "ganadoras": _r(sum(bool(o["gana"]) for o in deg) / len(deg)), "ic95": btd.get("ic95"),
                           "p_menor": btd.get("p_menor"), "umbral_R": DEGRADA_R, "min_operaciones": DEGRADA_MIN_OPS,
                           "datos_desde": piso_p}}
        if bl:
            c["bloqueada"] = bl
        cand.append(c)
    if not campeon["pausada"] and conf["confirmado"]:
        cand += ajustes(champ, activa, ef, ahora, n_boot, bloqueos, twin["id"] if twin is not None else None)
    for a in asp:
        a.pop("_a", None)
        a.pop("_r", None)
    return {"ts": ahora, "campeon": campeon, "aspirantes": asp, "candidatas": cand, "observacion": nueva_obs,
            "pruebas": k, "_champ": champ, "_cerradas": cerradas}


def _por_grupos(ops: list[dict], ahora: int) -> list[str]:
    """Líneas «qué ha funcionado» por noticia, apertura, moneda, lado, apertura anterior y volatilidad."""
    lines = []
    for dim in ("noticia", "ventana", "moneda", "lado", "apertura_anterior", "volatilidad"):
        vals: dict = {}
        for o in ops:
            g = _grupo(o, dim)
            if g is not None:
                vals.setdefault(g, []).append(o)
        if len(vals) < 2:   # un solo grupo no dice qué funciona mejor
            continue
        parts = []
        for g, xs in sorted(vals.items(), key=lambda kv: -len(kv[1])):
            if len(xs) < 3:
                continue
            m = _media(xs)
            rc, _ = contraido(xs, ahora)
            flag = " (muy poca muestra)" if len(xs) < SEG_MIN else ""
            parts.append(f"{_nombre_grupo(dim, g)}: {len(xs)} op., R medio {_sg(m)} (contraído {_sg(rc)}){flag}")
        if parts:
            lines.append(" · ".join(parts))
    return lines


def _peso(x):
    return _f(x.get("peso")) if isinstance(x, dict) else _f(x)


def _medios_semana(cambios: list[dict], medios: list[dict] | None) -> tuple[list[dict], list[dict], list[str]]:
    """(suben, bajan, líneas del estado actual): cada medio con su peso al empezar y al acabar la semana (varios cambios
    se juntan), sus casos y su acierto (del aprendizaje o de la evidencia del cambio)."""
    info = {m["medio"]: m for m in medios or [] if isinstance(m, dict) and m.get("medio")}
    agg: dict[str, dict] = {}
    for e in sorted(cambios, key=lambda e: e["ts"]):
        if e["tipo"] != "peso_cambiado" or not e.get("sujeto"):
            continue
        p0, p1 = _peso(e.get("antes")), _peso(e.get("despues"))
        if p0 is None or p1 is None:
            continue
        x = agg.setdefault(e["sujeto"], {"medio": e["sujeto"], "antes": p0, "cambios": 0})
        x["despues"] = p1
        x["cambios"] += 1
        ev = e.get("evidencia") or {}
        for k_in, k_out in (("casos", "casos"), ("aciertos", "aciertos"), ("acierto_aj", "acierto"),
                            ("acierto", "acierto")):
            if ev.get(k_in) is not None and x.get(k_out) is None:
                x[k_out] = ev[k_in]
    for m in info.values():   # formato antiguo (peso_antes) sin cambio en el historial
        p0, p1 = _f(m.get("peso_antes")), _f(m.get("peso"))
        if p0 is not None and p1 is not None and m["medio"] not in agg:
            agg[m["medio"]] = {"medio": m["medio"], "antes": p0, "despues": p1, "cambios": 1,
                               "casos": m.get("casos"), "aciertos": m.get("aciertos")}
    suben, bajan = [], []
    for x in agg.values():
        m = info.get(x["medio"]) or {}
        if m.get("casos") is not None:
            x["casos"] = m["casos"]
        if m.get("acierto") is not None and x.get("aciertos") is None:
            x["acierto"] = m["acierto"]
        if m.get("veredicto"):
            x["veredicto"] = m["veredicto"]
        if x["despues"] > x["antes"]:
            suben.append(x)
        elif x["despues"] < x["antes"]:
            bajan.append(x)
    suben.sort(key=lambda x: x["antes"] - x["despues"])
    bajan.sort(key=lambda x: x["despues"] - x["antes"])
    ahora_lines = []
    con = [m for m in info.values() if _f(m.get("peso")) is not None and _f(m.get("peso")) != 1.0]
    mas = sorted((m for m in con if m["peso"] > 1), key=lambda m: -m["peso"])[:5]
    menos = sorted((m for m in con if m["peso"] < 1), key=lambda m: m["peso"])[:5]

    def corto(m):
        ac = _f(m.get("acierto"))
        return f"{m['medio']} ({_n(m['peso'])}" + (f", acierto {_n(ac * 100, 0)} %" if ac is not None else "") + ")"

    if mas:
        ahora_lines.append("Pesan más ahora: " + ", ".join(corto(m) for m in mas))
    if menos:
        ahora_lines.append("Pesan menos ahora: " + ", ".join(corto(m) for m in menos))
    return suben, bajan, ahora_lines


def construir_informe(semana: str, pan: dict, cambios: list[dict], propuestas: list[dict], medios: list[dict] | None,
                      ahora: int, nota_medios: str | None = None) -> dict:
    """Informe semanal en español: resumen, qué ha funcionado, medios que suben y bajan, qué estrategia lidera, qué
    cambió y por qué, propuestas pendientes y avisos. Las cifras van también como números en «cifras». Lo de la semana
    y «qué ha funcionado» son descriptivos (operaciones cerradas); las comparaciones usan solo operaciones completas."""
    a, b = semana_limites(semana)
    c = pan["campeon"]
    cerradas = pan.get("_cerradas") or []
    sem = [o for o in cerradas if a <= o["t_out"] < b]
    rs = resumen(sem, ahora, 200)
    tot = c["vivo"]
    titulo = f"Informe de la semana {_texto_semana(semana)}"
    sec = []
    # Resumen
    lines = [f"Campeón: {c['nombre']}{' (PAUSADO)' if c['pausada'] else ''}."]
    if sem:
        lines.append(f"Esta semana cerró {len(sem)} operaciones: {_sg(rs['R_total'])} R en total, "
                     f"{_n((rs['ganadoras'] or 0) * 100, 0)} % ganadoras.")
    else:
        lines.append("Esta semana no cerró ninguna operación.")
    desde = f" desde el {_fecha(c['desde'])}" if c["desde"] else ""
    if tot["operaciones"]:
        lines.append(f"En total{desde}: {tot['operaciones']} operaciones completas, R medio {_sg(tot['R_medio'])} "
                     f"(contraído hacia 0: {_sg(tot['R_contraido'])}); {tot['muestra']}"
                     + (f"; {c['en_curso']} en curso aún no cuentan." if c.get("en_curso") else "."))
    if c.get("pendiente_reinicio"):
        lines.append(c["motor"]["texto"])
    if c.get("virtual") and c["virtual"]["operaciones"]:
        v = c["virtual"]
        lines.append(f"Su gemelo en la vigilancia (virtual): {v['operaciones']} operaciones, R medio {_sg(v['R_medio'])}.")
    if c.get("brecha_paper_virtual"):
        lines.append(c["brecha_paper_virtual"]["texto"])
    sec.append({"titulo": "Resumen", "lineas": lines})
    # Qué ha funcionado
    lines = _por_grupos(cerradas, ahora) if cerradas else []
    if not lines:
        lines = ["Aún no hay operaciones suficientes del campeón para decir qué funciona."]
    for o in sorted(sem, key=lambda o: o["t_out"])[:12]:
        nt = GRUPO_TXT["noticia"].get(o.get("noticia"), "sin etiqueta de noticia").lower()
        lines.append(f"{_fecha(o['t_out'])} · {WIN_NAME.get(o.get('ventana'), o.get('ventana') or '–')} · "
                     f"{'compra' if o['lado'] == 'largo' else 'venta'} {o.get('moneda')} · {nt} → {_sg(o['R'])} R")
    sec.append({"titulo": "Qué ha funcionado", "lineas": lines})
    # Medios
    lines = []
    by_t: dict[str, list] = {}
    for e in cambios:
        if e["tipo"].startswith("fuente_"):
            by_t.setdefault(e["tipo"], []).append(e["sujeto"] or "–")
    for t, txt in (("fuente_añadida", "Medios nuevos"), ("fuente_restaurada", "Medios restaurados"),
                   ("fuente_silenciada", "Medios silenciados"), ("fuente_retirada", "Medios retirados")):
        if by_t.get(t):
            lines.append(f"{txt} ({len(by_t[t])}): {', '.join(by_t[t][:15])}")
    suben, bajan, estado_medios = _medios_semana(cambios, medios)

    def _m(x):
        if x.get("casos") and x.get("aciertos") is not None:
            extra = f", {x['aciertos']} de {x['casos']} aciertos"
        elif x.get("casos") and x.get("acierto") is not None:
            extra = f", acierto {_n(float(x['acierto']) * 100, 0)} % en {_n(x['casos'], 0)} casos"
        else:
            extra = ""
        if x.get("cambios", 1) > 1:
            extra += f", {x['cambios']} cambios"
        return f"{x['medio']} ({_n(x['antes'])} → {_n(x['despues'])}{extra})"

    if suben:
        lines.append("Suben: " + ", ".join(_m(x) for x in suben[:5]))
    if bajan:
        lines.append("Bajan: " + ", ".join(_m(x) for x in bajan[:5]))
    if not lines:
        lines.append("Sin cambios en los medios esta semana.")
    lines += estado_medios
    if nota_medios:
        lines.append(nota_medios)
    sec.append({"titulo": "Medios que suben y bajan", "lineas": lines})
    # Qué estrategia lidera
    asp = pan["aspirantes"]
    lines = []
    cand = [("campeón", tot, None)] + [(x["codigo"] or x["id"][:30], x["vivo"], x["frente"]) for x in asp]
    con = [x for x in cand if x[1]["operaciones"]]
    if con:
        lid = max(con, key=lambda x: x[1]["R_contraido"] or 0)
        extra = ""
        if lid[2] is not None and not lid[2]["cumple"]:
            extra = f" No puede sustituir al campeón: {lid[2]['faltan'][0]}."
        elif lid[2] is not None and lid[2].get("confirmacion"):
            extra = f" {lid[2]['confirmacion']['texto']}"
        lines.append(f"Lidera por R medio contraído: {lid[0]} ({lid[1]['operaciones']} op., R medio "
                     f"{_sg(lid[1]['R_medio'])}, contraído {_sg(lid[1]['R_contraido'])}).{extra}")
    for x in asp[:6]:
        fr, v = x["frente"], x["vivo"]
        if fr["cumple"]:
            estado = "cumple: " + ((fr.get("confirmacion") or {}).get("texto") or "")
        else:
            estado = "no cumple: " + "; ".join(fr["faltan"])
        lines.append(f"{x['codigo'] or x['id'][:30]} ({x['clase'] or 'vigilada'}): {v['operaciones']} op., R medio "
                     f"{_sg(v['R_medio'])} · frente al gemelo del campeón {_sg(fr['dif_R'])} R (p = {_n(fr['p'], 3)}) "
                     f"· {estado}")
    if not asp:
        lines.append("No hay aspirantes en la vigilancia todavía.")
    sec.append({"titulo": "Qué estrategia lidera", "lineas": lines})
    # Qué cambió y por qué
    lines = [f"{_fecha(e['ts'])} · {e['etiqueta']}: {e['sujeto'] or '–'} — {e['motivo']}"
             for e in sorted(cambios, key=lambda e: e["ts"]) if e["tipo"] != "informe_semanal"][:40]
    sec.append({"titulo": "Qué cambió y por qué", "lineas": lines or ["No hubo cambios esta semana."]})
    sec.append({"titulo": "Propuestas pendientes",
                "lineas": [f"{p['cuerpo'].get('titulo')}: {p['cuerpo'].get('texto')}" for p in propuestas]
                or ["Ninguna."]})
    sec.append({"titulo": "Avisos", "lineas": [AVISO]})
    texto = "\n\n".join(s["titulo"] + "\n" + "\n".join("· " + x for x in s["lineas"]) for s in sec)
    cifras = {"semana": {"operaciones": len(sem), "R_total": rs["R_total"], "ganadoras": rs["ganadoras"]},
              "campeon": {**{k: tot[k] for k in ("operaciones", "R_medio", "R_contraido", "ganadoras")},
                          "en_curso": c.get("en_curso"), "firma": c.get("firma"),
                          "pendiente_reinicio": c.get("pendiente_reinicio")},
              "aspirantes": [{"codigo": x["codigo"], "id": x["id"], "operaciones": x["vivo"]["operaciones"],
                              "R_medio": x["vivo"]["R_medio"], "R_contraido": x["vivo"]["R_contraido"],
                              "dif_R": x["frente"]["dif_R"], "p": x["frente"]["p"],
                              "p_max_corregido": x["frente"]["p_max_corregido"], "cumple": x["frente"]["cumple"],
                              "confirmacion": (x["frente"].get("confirmacion") or {}).get("estado")}
                             for x in asp],
              "pruebas": pan.get("pruebas"),
              "cambios": sum(e["tipo"] != "informe_semanal" for e in cambios), "propuestas": len(propuestas),
              "medios": {"suben": suben[:10], "bajan": bajan[:10]}}
    return {"semana": semana, "desde_ms": a, "hasta_ms": b, "titulo": titulo, "resumen": " ".join(sec[0]["lineas"][:2]),
            "secciones": sec, "texto": texto, "cifras": cifras, "generado": ahora}


# ---------------------------------------------------------------- servicio

class Evolucion:
    """Evolución de la estrategia (ver el docstring del módulo).
    - leer_vigilancia() → {"estrategias": [{id, codigo, nombre, clase, origen, desde, backtest, en_paper, variante_de}],
      "operaciones": [filas de la tabla vigilancia]}: sustituye a la lectura de la tabla y del estado del bus (en un
      hilo).
    - leer_medios() → medios con su peso para el informe (aprendizaje / fuentes_vivas; en un hilo).
    - leer_motor() → {"firma", "ts", "pausada"} de lo que cargó el motor (por defecto, «openings».estrategia_cargada).
    - reversores {origen (o tipo): fn(entrada) → {ok, why}}: deshacen cambios que gestionan otros módulos."""

    def __init__(self, bus, store, clock=now_ms, archivo: Path | str | None = None, modo: str | None = None,
                 leer_vigilancia=None, leer_medios=None, leer_motor=None, reversores: dict | None = None,
                 n_boot: int = N_BOOT):
        self.bus, self.store, self.clock = bus, store, clock
        self.archivo = Path(archivo) if archivo else config.DATA_DIR / ARCHIVO
        self.leer_vigilancia, self.leer_medios, self.leer_motor = leer_vigilancia, leer_medios, leer_motor
        self.reversores = dict(reversores or {})
        self.n_boot = n_boot
        asegurar_tablas(store)
        m = (modo or self._estado("modo") or MODO_DEFECTO or "").strip().lower()
        self.modo = m if m in MODOS else "proponer"
        self.panorama: dict | None = None
        self.err: str | None = None
        self.nota_archivo: str | None = None
        self.msg: dict | None = None
        self.ultima_orden: dict | None = None
        self.aprendizaje = None
        self._key = None
        self._lock = asyncio.Lock()
        self._refresco = None
        self._tasks: set = set()
        self._pronto = None                  # publicación agrupada tras varios registros seguidos
        self._revierte: dict[str, str] = {}  # id de fuente → id de la entrada que se está deshaciendo
        self._apr_revierte: dict[int, str] = {}   # cambio_id nuevo del aprendizaje → id de la entrada que deshace
        self._vig_espera: int | None = None       # desde cuándo se espera a la vigilancia para evaluar

    # ---- estado propio (evolution_state)

    def _estado(self, clave: str, default=None):
        row = self.store.db.execute("SELECT valor FROM evolution_state WHERE clave=?", (clave,)).fetchone()
        return _loads(row[0], default) if row else default

    def _set_estado(self, clave: str, valor) -> None:
        self.store.db.execute("INSERT OR REPLACE INTO evolution_state VALUES (?,?)", (clave, _dumps(valor)))
        self.store.db.commit()

    def _bloquear(self, clave: str, ts: int, motivo: str, por: str, pid: str | None) -> None:
        """Lo que el usuario descarta o revierte: solo vuelve con datos posteriores y nunca se aplica solo."""
        b = self._estado("bloqueos", {}) or {}
        b[clave] = {"ts": ts, "motivo": motivo, "por": por, "propuesta": pid}
        self._set_estado("bloqueos", b)

    def _desbloquear(self, clave: str) -> None:
        b = self._estado("bloqueos", {}) or {}
        if b.pop(clave, None) is not None:
            self._set_estado("bloqueos", b)

    # ---- historial

    def registrar(self, tipo: str, sujeto, antes=None, despues=None, motivo: str = "", evidencia=None,
                  origen: str = "", revierte: str | None = None) -> dict:
        """Como registrar() del módulo, con la hora del bot, y vuelve a publicar el mensaje «evolucion»."""
        e = registrar(self.store, tipo, sujeto, antes, despues, motivo, evidencia, origen, revierte, ts=self.clock())
        self._publicar_pronto()
        return e

    def apuntar_cambios_codigo(self) -> list[dict]:
        """Al arrancar: los cambios del código que alteran la operativa en paper, una sola vez (CAMBIOS_CODIGO)."""
        out = apuntar_cambios_codigo(self.store, ts=self.clock())
        for e in out:
            log.warning("Evolución: apuntado «%s» (%s)", e["etiqueta"], e["sujeto"])
        if out:
            self._publicar_pronto()
        return out

    def registrar_fuente(self, entrada: dict) -> None:
        """registrar= de FuentesVivas: apunta su cambio en el historial (se llama en el bucle; rápido)."""
        rv = self._revierte.pop(str(entrada.get("id")), None)
        registrar(self.store, **desde_fuentes(entrada), origen="fuentes_vivas", revierte=rv,
                  ts=int(_f(entrada.get("ts"), 0) or self.clock()))
        self._publicar_pronto()

    def conectar_fuentes(self, fv) -> None:
        """Enlaza FuentesVivas: sus cambios van al historial y se deshacen desde aquí con su orden inversa, solo si la
        fuente sigue como la dejó ese cambio (ninguno posterior) y la orden la devuelve a como estaba."""
        fv.registrar = self.registrar_fuente

        def deshacer(e: dict) -> dict:
            ev = e.get("evidencia") or {}
            d = ev.get("deshacer") or {}
            fid = d.get("id") or ev.get("fuente_id")
            if not d.get("accion") or not fid:
                return {"ok": False, "why": "Este cambio no dice cómo deshacerse"}
            est = lambda x: x.get("estado") if isinstance(x, dict) else x
            antes, despues = est(e.get("antes")), est(e.get("despues"))
            recientes = leer(self.store, origen="fuentes_vivas", desde=e["ts"], limite=5000)
            pos = next((i for i, x in enumerate(recientes) if x["id"] == e["id"]), len(recientes))
            despues_de = [x for x in recientes[:pos] if (x.get("evidencia") or {}).get("fuente_id") == fid]
            pares = {x["revierte"]: x["id"] for x in despues_de if x.get("revierte")}   # cambio posterior y reversión
            if any(x["id"] not in pares and x["id"] not in pares.values() for x in despues_de):
                return {"ok": False, "why": "Después hubo otros cambios de esta fuente: revierte antes el más reciente"}
            fila = (getattr(fv, "filas", None) or {}).get(fid)
            actual = getattr(fila, "estado", None)
            if actual is not None and despues is not None and actual != despues:
                return {"ok": False, "why": f"La fuente ya no está {despues} (ahora está {actual}): no se revierte"}
            acc = d["accion"]
            destino = ACCION_ESTADO.get(acc)
            if acc == "restaurar":   # vuelve al estado que tenía antes de silenciarse
                destino = getattr(fila, "previo", None) or "activa"
            if not (destino == antes or (antes is None and destino == "retirada")):
                return {"ok": False, "why": f"No hay acción que la devuelva a «{antes or 'sin dar de alta'}»: su orden "
                                            f"inversa ({acc}) la dejaría {destino or 'igual'}"}
            self._revierte[str(fid)] = e["id"]
            try:
                r = fv.accion({"accion": acc, "id": fid, "motivo": f"Deshace «{e['motivo'][:120]}»"})
            finally:
                self._revierte.pop(str(fid), None)
            r = r if isinstance(r, dict) else {}
            return {"ok": bool(r.get("ok")), "why": r.get("error") or r.get("why")}

        self.reversores["fuentes_vivas"] = deshacer

    def conectar_aprendizaje(self, apr) -> None:
        """Enlaza Aprendizaje: cada cambio de peso de medios_peso_hist entra en el historial como «peso_cambiado» (se
        copian en cada ronda, con su cambio_id y sus cifras), se deshace desde aquí con apr.revertir(cambio_id) y el
        informe lee sus medios (apr.medios())."""
        self.aprendizaje = apr
        if self.leer_medios is None:
            self.leer_medios = lambda: apr.medios(limit=500)

        def deshacer(e: dict) -> dict:
            ev = e.get("evidencia") or {}
            cid = _f(ev.get("cambio_id"))
            if cid is None:
                return {"ok": False, "why": "Este cambio de peso no dice qué cambio del aprendizaje es"}
            if e["tipo"] == "veredicto_medio" or ev.get("tipo_cambio") == "veredicto":
                return {"ok": False, "why": "Ese apunte solo cambió el veredicto del medio, no su peso: no hay nada que "
                                            "revertir"}
            cid = int(cid)
            self._apuntar_apr(self._filas_apr(self.store.db, self._cursor_apr(), APR_LOTE))
            if self.store.db.execute("SELECT 1 FROM medios_peso_hist WHERE outlet = ? AND id > ? AND tipo != 'veredicto' "
                                     "LIMIT 1", (e["sujeto"], cid)).fetchone():
                return {"ok": False, "why": "Después hubo otros cambios de peso de este medio: revierte antes el más "
                                            "reciente"}
            res = apr.revertir(cid, f"Revertido el cambio n.º {cid} desde la evolución")
            if not res or not res.get("ok", True):
                return {"ok": False, "why": (res or {}).get("why") or "El aprendizaje no encuentra ese cambio"}
            self._apr_revierte[int(res["id"])] = e["id"]
            self._apuntar_apr(self._filas_apr(self.store.db, self._cursor_apr(), APR_LOTE))
            return {"ok": True, "why": None}

        self.reversores["aprendizaje"] = deshacer

    def _cursor_apr(self) -> int:
        return int(_f(self._estado("aprendizaje_cursor"), 0) or 0)

    @staticmethod
    def _filas_apr(db, cursor: int, limite: int) -> list[tuple]:
        try:
            return db.execute("SELECT id, ts, outlet, antes, despues, tipo, motivo, cuerpo FROM medios_peso_hist "
                              "WHERE id > ? ORDER BY id LIMIT ?", (int(cursor), int(limite))).fetchall()
        except sqlite3.OperationalError:   # sin tabla del aprendizaje todavía
            return []

    def _entrada_apr(self, cid: int, outlet: str) -> str | None:
        for x in leer(self.store, sujeto=outlet, origen="aprendizaje", limite=2000):
            if (x.get("evidencia") or {}).get("cambio_id") == cid and not x["revertida_por"]:
                return x["id"]
        return None

    def _apuntar_apr(self, filas: list[tuple]) -> int:
        """Copia al historial los cambios de peso del aprendizaje posteriores al cursor (en el bucle; rápido). Las filas
        «veredicto» (el peso no cambia, solo el veredicto confirmado) van como «veredicto_medio», que no se revierte."""
        cur = self._cursor_apr()
        out, lote = [], {}   # lote: cambio_id → id de su entrada, para enlazar una reversión copiada en la misma ronda
        for cid, ts, outlet, antes, despues, tipo, motivo, cuerpo in filas:
            if cid <= cur:
                continue
            rv = self._apr_revierte.pop(int(cid), None)
            mm = re.search(r"n\.º (\d+)", motivo or "") if rv is None else None
            if mm:
                rv = lote.get((int(mm.group(1)), outlet)) or self._entrada_apr(int(mm.group(1)), outlet)
            cifras = _loads(cuerpo, {}) or {}
            ev = {**(cifras if isinstance(cifras, dict) else {}), "cambio_id": int(cid), "tipo_cambio": tipo}
            t = int(_f(ts, 0) or self.clock())
            if tipo == "veredicto":
                mv = re.search(r"veredicto: (\w+) → (\w+)", motivo or "")
                e = _nueva_entrada("veredicto_medio", outlet, {"veredicto": mv.group(1)} if mv else None,
                                   {"veredicto": mv.group(2) if mv else ev.get("veredicto"), "peso": _f(despues)},
                                   motivo or "Cambio de veredicto del aprendizaje", ev, "aprendizaje", None, t)
            else:
                e = _nueva_entrada("peso_cambiado", outlet, {"peso": _f(antes)}, {"peso": _f(despues)},
                                   motivo or "Cambio de peso del aprendizaje", ev, "aprendizaje", rv, t)
            out.append(e)
            lote[(int(cid), outlet)] = e["id"]
            cur = max(cur, int(cid))
        if out:
            _insertar(self.store.db, out)
        if cur != self._cursor_apr():
            self._set_estado("aprendizaje_cursor", cur)
        if out:
            self._publicar_pronto()
        return len(out)

    async def sincronizar_aprendizaje(self) -> int:
        """Una ronda de copia de medios_peso_hist → evolution_log (lectura en un hilo, escritura en el bucle)."""
        if self.aprendizaje is None:
            return 0

        def hilo(cursor):
            with self.store.reader() as db:
                return self._filas_apr(db, cursor, APR_LOTE)

        try:
            filas = await asyncio.to_thread(hilo, self._cursor_apr())
            return self._apuntar_apr(filas)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Evolución: no pude copiar los cambios de peso del aprendizaje; se reintenta")
            return 0

    def _publicar_pronto(self) -> None:
        """Publica en ~1 s (varios cambios seguidos salen en un solo mensaje); sin bucle, al momento."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._publicar(self.clock())
            return
        if self._pronto is None:
            def go():
                self._pronto = None
                self._publicar(self.clock())
            self._pronto = loop.call_later(1.0, go)

    # ---- estrategia activa

    def activa(self) -> dict:
        """Lo que opera el motor: el archivo validado o, si no existe o no vale, el preset (con nota para la web)."""
        try:
            d = json.loads(self.archivo.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.nota_archivo = None
            return activa_por_defecto()
        except (OSError, ValueError) as e:
            self.nota_archivo = f"No pude leer {self.archivo.name} ({e}): el motor usará el preset"
            return activa_por_defecto()
        ok, why = validar_activa(d)
        if not ok:
            self.nota_archivo = f"{self.archivo.name} no vale ({why}): el motor usará el preset"
            return activa_por_defecto()
        self.nota_archivo = None
        return d

    def _motor(self) -> dict | None:
        try:
            m = self.leer_motor() if self.leer_motor is not None else \
                (self.bus.state.get("openings") or {}).get("estrategia_cargada")
        except Exception:
            log.exception("Evolución: no pude leer qué estrategia cargó el motor")
            return None
        return dict(m) if isinstance(m, dict) else None

    # ---- cálculo

    def _calcular_hilo(self, meta, ahora: int, motor, bloqueos, observacion) -> dict:
        activa = self.activa()
        with self.store.reader() as db:
            paper = leer_paper(db)
            filas = leer_filas_vigilancia(db) if self.leer_vigilancia is None else None
        if self.leer_vigilancia is not None:
            v = self.leer_vigilancia() or {}
            meta, filas = v.get("estrategias") or [], v.get("operaciones") or []
        pan = calcular(paper, meta, filas, activa, ahora, self.n_boot, motor, bloqueos, observacion)
        pan["activa"] = activa
        return pan

    async def _calcular(self, ahora: int) -> dict | None:
        meta = None
        if self.leer_vigilancia is None:
            meta = [dict(x) for x in ((self.bus.state.get("vigilancia") or {}).get("estrategias") or [])
                    if isinstance(x, dict)]
        try:
            args = (self._motor(), self._estado("bloqueos", {}) or {}, self._estado("observacion", {}) or {})
            pan = await asyncio.to_thread(self._calcular_hilo, meta, ahora, *args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.err = str(e)[:200] or type(e).__name__
            log.exception("Evolución: fallo en el cálculo; se reintenta en la próxima ronda")
            return None
        self.err = None
        self.panorama, self._refresco = pan, ahora
        return pan

    async def refrescar(self, ahora: int | None = None) -> dict | None:
        """Recalcula las cifras de la web (sin tocar las propuestas ni la observación) y publica si algo cambió."""
        ahora = self.clock() if ahora is None else int(ahora)
        pan = await self._calcular(ahora)
        self._publicar(ahora)
        return pan

    async def evaluar(self, ahora: int | None = None) -> dict | None:
        """Comparación semanal (o a petición): recalcula, guarda la observación de los aspirantes, las propuestas
        nuevas, refresca las que siguen, caduca las que ya no respaldan los datos y, en modo automático, aplica
        promociones y pausas."""
        async with self._lock:
            ahora = self.clock() if ahora is None else int(ahora)
            pan = await self._calcular(ahora)
            if pan is not None:
                try:
                    self._set_estado("observacion", pan["observacion"])
                    self._sincronizar(pan["candidatas"], ahora)
                    self._set_estado("ultima_evaluacion", ahora)
                    if self.modo == "automatico":
                        await self._auto(ahora)
                except Exception as e:
                    self.err = str(e)[:200] or type(e).__name__
                    log.exception("Evolución: fallo al guardar las propuestas")
            self._publicar(ahora)
            return pan

    # ---- propuestas

    def _propuestas(self, estado: str | None = None, limite: int = 200, db=None) -> list[dict]:
        db = db or self.store.db
        sql = f"SELECT {', '.join(PROP_COLS)} FROM evolution_proposals"
        args: list = []
        if estado:
            sql += " WHERE estado = ?"
            args.append(estado)
        try:
            rows = db.execute(sql + " ORDER BY ts DESC, rowid DESC LIMIT ?", (*args, int(limite))).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            p = dict(zip(PROP_COLS, r))
            p["cuerpo"] = _loads(p["cuerpo"], {})
            out.append(p)
        return out

    def _propuesta(self, pid: str) -> dict | None:
        row = self.store.db.execute(f"SELECT {', '.join(PROP_COLS)} FROM evolution_proposals WHERE id=?",
                                    (pid,)).fetchone()
        if not row:
            return None
        p = dict(zip(PROP_COLS, row))
        p["cuerpo"] = _loads(p["cuerpo"], {})
        return p

    def _resolver(self, pid: str, estado: str, ahora: int, por: str, nota: str | None) -> None:
        self.store.db.execute("UPDATE evolution_proposals SET estado=?, resuelta_ts=?, resuelta_por=?, nota=? "
                              "WHERE id=? AND estado='pendiente'", (estado, ahora, por, nota, pid))
        self.store.db.commit()

    def _sincronizar(self, candidatas: list[dict], ahora: int) -> None:
        db = self.store.db
        pend = {p["clave"]: p for p in self._propuestas("pendiente", 1000)}
        claves = set()
        for c in candidatas:
            claves.add(c["clave"])
            old = pend.get(c["clave"])
            if old is not None:   # sigue respaldada: se refrescan sus cifras
                db.execute("UPDATE evolution_proposals SET cuerpo=?, actualizada=? WHERE id=?",
                           (_dumps(c), ahora, old["id"]))
            else:
                pid = _nuevo_id("p", ahora)
                db.execute("INSERT INTO evolution_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (pid, ahora, c["tipo"], c["sujeto"], c["clave"], "pendiente", _dumps(c), ahora, None, None,
                            None))
                log.info("Evolución: nueva propuesta %s (%s): %s", pid, c["tipo"], c["titulo"])
        for clave, p in pend.items():
            if clave not in claves:
                db.execute("UPDATE evolution_proposals SET estado='caducada', resuelta_ts=?, resuelta_por='evolucion', "
                           "nota=? WHERE id=?", (ahora, "La última evaluación ya no la respalda (o cambió la estrategia "
                                                        "activa)", p["id"]))
        db.commit()

    async def _auto(self, ahora: int) -> None:
        """Modo automático: aplica la promoción (con ENFRIAR_D días desde el último cambio o reversión) y la pausa (si
        sigue propuesta PAUSA_ESPERA_D días después: en la evaluación semanal siguiente). Nunca lo que el usuario
        descartó o revirtió."""
        ultimo = self._estado("ultimo_cambio")
        for tipo in ("estrategia_promovida", "estrategia_degradada"):
            for p in self._propuestas("pendiente", 1000):
                if p["tipo"] != tipo:
                    continue
                if p["cuerpo"].get("bloqueada"):
                    log.info("Evolución: %s no se aplica sola: ya la descartaste o la revertiste", p["id"])
                    continue
                if tipo == "estrategia_promovida" and ultimo and ahora - int(ultimo) < ENFRIAR_D * DAY:
                    log.info("Evolución: promoción %s en espera: el último cambio fue hace menos de %d días",
                             p["id"], ENFRIAR_D)
                    continue
                if tipo == "estrategia_degradada" and ahora - p["ts"] < PAUSA_ESPERA_D * DAY:
                    log.info("Evolución: pausa %s en espera: se aplica sola si se repite en la evaluación de la "
                             "semana que viene", p["id"])
                    continue
                res = await self._aplicar_cambio(p["cuerpo"], p["id"], "automático", ahora)
                log.info("Evolución (automático): %s → %s", p["id"], res)
                if res.get("ok"):
                    return   # un cambio por evaluación: lo demás se hizo sobre la estrategia de antes

    async def aplicar(self, pid: str, por: str = "usuario") -> dict:
        """Aplica una propuesta pendiente si los datos de hoy la siguen respaldando y cumple las reglas del usuario."""
        async with self._lock:
            ahora = self.clock()
            p = self._propuesta(str(pid))
            if p is None:
                res = {"ok": False, "why": "No existe esa propuesta"}
            elif p["estado"] != "pendiente":
                res = {"ok": False, "why": f"La propuesta ya está {p['estado']}"}
            else:
                pan = await self._calcular(ahora)
                vig = next((c for c in (pan or {}).get("candidatas", []) if c["clave"] == p["clave"]), None)
                if pan is None:
                    res = {"ok": False, "why": "No pude recalcular con los datos de hoy: inténtalo más tarde"}
                elif vig is None:
                    self._resolver(p["id"], "caducada", ahora, "evolucion",
                                   "Los datos de hoy ya no la respaldan (o cambió la estrategia activa)")
                    res = {"ok": False, "why": "Los datos de hoy ya no la respaldan: queda caducada y no se aplica"}
                else:
                    res = await self._aplicar_cambio(vig, p["id"], por, ahora)
            self._publicar(ahora)
            return res

    async def _aplicar_cambio(self, c: dict, pid: str, por: str, ahora: int) -> dict:
        antes = await asyncio.to_thread(self.activa)
        if firma(antes) != c.get("base_firma"):
            self._resolver(pid, "caducada", ahora, "evolucion", "La estrategia activa cambió desde la propuesta")
            return {"ok": False, "why": "La estrategia activa ha cambiado desde la propuesta: queda caducada"}
        base = {k: v for k, v in antes.items() if k not in ("registro", "propuesta", "revierte", "firma")}
        tipo, ch = c["tipo"], c.get("cambio") or {}
        if tipo == "estrategia_promovida":
            despues = {"version": 2, "estrategia": "impulso", "fuente": "evolucion", "params": dict(ch["params"]),
                       "lab_id": ch.get("lab_id"), "lab_twin": ch.get("lab_twin") or ch.get("lab_id"),
                       "twin_filtro": None, "codigo": ch.get("codigo"), "nombre": ch.get("nombre"), "pausada": False,
                       "desde": ahora}
        elif tipo == "estrategia_degradada":
            despues = {**base, "pausada": True}
        elif tipo == "parametro_cambiado":
            params = {**(base.get("params") or {}), **ch["params"]}
            tw = ch.get("lab_twin")
            efn = {**_preset()[0], **params}
            filtro = base.get("twin_filtro") if tw else None
            if tw and ({"windows", "coins"} & set(ch["params"])):   # gemelo aproximado: sin lo que se quita
                filtro = {"windows": list(efn.get("windows") or []), "coins": list(efn.get("coins") or [])}
            despues = {**base, "fuente": "evolucion", "params": params, "lab_id": None, "lab_twin": tw,
                       "twin_filtro": filtro, "nombre": f"{base.get('nombre')} · ajustada", "desde": ahora}
        else:
            return {"ok": False, "why": f"Tipo de propuesta desconocido: {tipo}"}
        eid = _nuevo_id("v", ahora)
        despues.update(actualizado=ahora, motivo=c["texto"], registro=eid, propuesta=pid, solo_paper=True)
        despues["firma"] = firma_estrategia(despues)
        ok, why = validar_activa(despues)
        if not ok:   # nunca debería pasar (las candidatas ya se filtran), pero la regla es la regla
            self._resolver(pid, "descartada", ahora, "evolucion", f"Rompería las reglas del usuario: {why}")
            return {"ok": False, "why": f"No se aplica: {why}"}
        try:
            await asyncio.to_thread(escribir_activa, self.archivo, despues)
        except Exception as e:
            log.exception("Evolución: no pude escribir %s", self.archivo)
            return {"ok": False, "why": f"No pude escribir {self.archivo.name}: {e}"}
        try:
            ev = {**(c.get("evidencia") or {}), "propuesta": pid, "por": por, "modo": self.modo,
                  "firma_antes": firma_estrategia(antes), "firma_despues": despues["firma"]}
            registrar(self.store, tipo, c["sujeto"], antes, despues, c["texto"], ev, "evolucion", ts=ahora, id_=eid)
        except Exception:
            log.exception("Evolución: no pude registrar el cambio; se deja la estrategia como estaba")
            try:
                await asyncio.to_thread(escribir_activa, self.archivo, antes)
            except Exception:
                log.exception("Evolución: tampoco pude restaurar %s", self.archivo)
            return {"ok": False, "why": "No pude registrar el cambio en el historial: se deja como estaba"}
        self._resolver(pid, "aplicada", ahora, por, None)
        if tipo != "estrategia_degradada":
            self._set_estado("ultimo_cambio", ahora)
        for o in self._propuestas("pendiente", 1000):   # las hechas sobre la estrategia de antes ya no valen
            if o["cuerpo"].get("base_firma") == c.get("base_firma"):
                self._resolver(o["id"], "caducada", ahora, "evolucion", "La estrategia activa ha cambiado")
        log.info("Evolución: aplicada %s (%s) por %s: %s", pid, tipo, por, c.get("titulo"))
        return {"ok": True, "why": None, "registro": eid}

    async def descartar(self, pid: str, motivo: str = "", por: str = "usuario") -> dict:
        async with self._lock:
            ahora = self.clock()
            p = self._propuesta(str(pid))
            if p is None:
                res = {"ok": False, "why": "No existe esa propuesta"}
            elif p["estado"] != "pendiente":
                res = {"ok": False, "why": f"La propuesta ya está {p['estado']}"}
            else:
                self._resolver(p["id"], "descartada", ahora, por, str(motivo or "Descartada por ti")[:300])
                self._bloquear(p["clave"], ahora, "descartada", por, p["id"])
                res = {"ok": True, "why": None}
            self._publicar(ahora)
            return res

    async def revertir(self, eid: str, por: str = "usuario") -> dict:
        """Deshace un cambio del historial: los de estrategia y el modo, aquí (si no hubo otros después); los demás, con
        el reversor de su módulo. La reversión es una entrada nueva con revierte = el id deshecho."""
        async with self._lock:
            ahora = self.clock()
            res = await self._revertir(str(eid), por, ahora)
            self._publicar(ahora)
            return res

    async def _revertir(self, eid: str, por: str, ahora: int) -> dict:
        found = leer(self.store, id_=eid, limite=1)
        if not found:
            return {"ok": False, "why": "No existe ese cambio"}
        e = found[0]
        if e["revertida_por"]:
            return {"ok": False, "why": "Ese cambio ya se revirtió"}
        if e["tipo"] in NO_REVERSIBLES or (e.get("evidencia") or {}).get("tipo_cambio") == "veredicto":
            return {"ok": False, "why": f"«{e['etiqueta']}» es un apunte del historial, no un cambio que se pueda revertir"}
        fn = self.reversores.get(e["origen"]) or self.reversores.get(e["tipo"])
        if fn is not None:
            try:
                r = fn(e)
                r = await r if inspect.isawaitable(r) else r
                return r if isinstance(r, dict) else {"ok": bool(r), "why": None}
            except Exception as ex:
                log.exception("Evolución: el reversor de %s falló", e["tipo"])
                return {"ok": False, "why": f"El módulo «{e['origen'] or e['tipo']}» no pudo revertirlo: {ex}"}
        if e["sujeto"] == "evolucion.modo" and isinstance(e["antes"], dict):
            return self._set_modo(e["antes"].get("modo"), por, ahora, revierte=e["id"])
        if e["tipo"] not in PROPIOS or not isinstance(e["antes"], dict) or not isinstance(e["despues"], dict):
            return {"ok": False, "why": f"Este cambio lo gestiona «{e['origen'] or 'otro módulo'}», que no está "
                                        "enlazado con la evolución: no se puede revertir desde aquí"}
        actual = await asyncio.to_thread(self.activa)
        if firma(actual) != firma(e["despues"]):
            return {"ok": False, "why": "Después de este cambio hubo otros: revierte antes el más reciente"}
        nuevo = {k: v for k, v in e["antes"].items() if k not in ("registro", "propuesta", "revierte")}
        rid = _nuevo_id("v", ahora)
        nuevo.update(desde=ahora, actualizado=ahora, motivo=f"Reversión de «{e['motivo'][:160]}»", registro=rid,
                     revierte=e["id"], solo_paper=True)
        nuevo["firma"] = firma_estrategia(nuevo)
        ok, why = validar_activa(nuevo)
        if not ok:
            return {"ok": False, "why": f"No se puede volver a lo de antes: {why}"}
        tipo = {"estrategia_promovida": "estrategia_degradada", "estrategia_degradada": "estrategia_promovida"} \
            .get(e["tipo"], "parametro_cambiado")
        try:
            await asyncio.to_thread(escribir_activa, self.archivo, nuevo)
            registrar(self.store, tipo, nuevo.get("nombre") or e["sujeto"], actual, nuevo,
                      f"Reversión pedida por {por}: se deshace «{e['etiqueta']}: {e['sujeto']}»",
                      {"revierte": e["id"], "por": por, "firma_antes": firma_estrategia(actual),
                       "firma_despues": nuevo["firma"]}, "evolucion", revierte=e["id"], ts=ahora, id_=rid)
        except Exception as ex:
            log.exception("Evolución: no pude revertir %s", eid)
            try:
                await asyncio.to_thread(escribir_activa, self.archivo, actual)
            except Exception:
                log.exception("Evolución: tampoco pude restaurar %s", self.archivo)
            return {"ok": False, "why": f"No pude revertirlo: {ex}"}
        # La decisión del usuario se mantiene: lo revertido queda bloqueado (y si deshace una reversión, se libera)
        pid = (e.get("evidencia") or {}).get("propuesta")
        p = self._propuesta(pid) if pid else None
        if p is not None:
            self._bloquear(p["clave"], ahora, "revertida", por, pid)
            self.store.db.execute("UPDATE evolution_proposals SET estado='revertida', nota=? WHERE id=? AND "
                                  "estado='aplicada'", (f"Revertida por {por} el {_fecha(ahora)}", pid))
            self.store.db.commit()
        if e.get("revierte"):
            x = leer(self.store, id_=e["revierte"], limite=1)
            xp = ((x[0].get("evidencia") or {}).get("propuesta")) if x else None
            p2 = self._propuesta(xp) if xp else None
            if p2 is not None:
                self._desbloquear(p2["clave"])
        self._set_estado("ultimo_cambio", ahora)   # toda reversión reinicia el enfriamiento del modo automático
        return {"ok": True, "why": None, "registro": rid}

    def _set_modo(self, modo, por: str, ahora: int, revierte: str | None = None) -> dict:
        modo = str(modo or "").strip().lower()
        if modo not in MODOS:
            return {"ok": False, "why": "Modo no válido (proponer o automatico)"}
        if modo == self.modo:
            return {"ok": True, "why": "Ya estaba en ese modo"}
        antes, self.modo = self.modo, modo
        self._set_estado("modo", modo)
        txt = {"proponer": "solo propone; aplicar es cosa tuya",
               "automatico": "aplica sola las promociones y las pausas (los ajustes de parámetros siguen siendo "
                             "propuestas, y lo que descartes o revientas nunca se aplica solo)"}[modo]
        registrar(self.store, "parametro_cambiado", "evolucion.modo", {"modo": antes}, {"modo": modo},
                  f"Modo de la evolución cambiado por {por}: {txt}", {"por": por}, "evolucion", revierte=revierte,
                  ts=ahora)
        return {"ok": True, "why": None}

    async def set_modo(self, modo: str, por: str = "usuario") -> dict:
        async with self._lock:
            ahora = self.clock()
            res = self._set_modo(modo, por, ahora)
            self._publicar(ahora)
            return res

    # ---- informe semanal

    def _informe_hecho(self, semana: str) -> dict | None:
        found = leer(self.store, tipo="informe_semanal", sujeto=semana, limite=1)
        return found[0] if found else None

    async def informe_semanal(self, semana: str | None = None, ahora: int | None = None) -> dict | None:
        """Informe de una semana ya terminada (sin semana, la última completa), guardado en evolution_log como
        «informe_semanal» con sujeto = la semana («AAAA-Wss»). Si ya existe, se devuelve sin repetirlo."""
        ahora = self.clock() if ahora is None else int(ahora)
        semana = semana or semana_objetivo(ahora)
        hecho = self._informe_hecho(semana)
        if hecho:
            return hecho
        await self.sincronizar_aprendizaje()   # que los cambios de peso de la semana estén en el historial
        pan = self.panorama if self.panorama and self.panorama.get("ts") == ahora else await self._calcular(ahora)
        if pan is None:
            return None
        a, b = semana_limites(semana)

        def hilo():
            with self.store.reader() as db:
                cambios = leer(db, desde=a, hasta=b, limite=5000)
            medios, nota = None, None
            if self.leer_medios is not None:
                try:
                    medios = list(self.leer_medios() or [])
                except Exception as e:
                    log.exception("Evolución: no pude leer los medios para el informe")
                    nota = f"No pude leer el aprendizaje de medios ({str(e)[:120]})."
            return cambios, medios, nota

        try:
            cambios, medios, nota = await asyncio.to_thread(hilo)
            inf = construir_informe(semana, pan, cambios, self._propuestas("pendiente"), medios, ahora, nota)
            e = registrar(self.store, "informe_semanal", semana, None, inf, f"Informe semanal automático: {inf['titulo']}",
                          inf["cifras"], "evolucion", ts=ahora)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            self.err = str(ex)[:200] or type(ex).__name__
            log.exception("Evolución: no pude generar el informe de %s", semana)
            return None
        log.info("Evolución: informe semanal %s guardado (%s)", semana, e["id"])
        self._publicar(ahora)
        return e

    # ---- bucle 24/7

    def _vigilancia_lista(self, ahora: int) -> bool:
        """¿Ya se puede evaluar? Al arrancar, la vigilancia aún no ha publicado sus estrategias: sin ellas la evaluación
        y el informe de la semana saldrían sin aspirantes ni gemelo (y no se rehacen). Se espera hasta VIG_ESPERA_S;
        apagada o sin lista, se sigue sin ella."""
        if self.leer_vigilancia is not None:
            return True
        v = self.bus.state.get("vigilancia") or {}
        if v.get("estado") in ("apagada", "sin_estrategias") or (v.get("estrategias") and v.get("estado") != "arrancando"):
            self._vig_espera = None
            return True
        if self._vig_espera is None:
            self._vig_espera = ahora
        if ahora - self._vig_espera >= VIG_ESPERA_S * 1000:
            log.warning("Evolución: la vigilancia no ha publicado sus estrategias en %d min; se evalúa sin ellas",
                        VIG_ESPERA_S // 60)
            return True
        return False

    async def paso(self, ahora: int | None = None) -> None:
        """Una ronda: copia los cambios de peso del aprendizaje; si toca (y la vigilancia ya publicó sus estrategias),
        evaluación e informe de la semana; si no, refresco de las cifras cada REFRESH_S."""
        ahora = self.clock() if ahora is None else int(ahora)
        await self.sincronizar_aprendizaje()
        semana = semana_objetivo(ahora)
        if not self._informe_hecho(semana) and self._vigilancia_lista(ahora):
            await self.evaluar(ahora)
            await self.informe_semanal(semana, ahora)
        elif self._refresco is None or ahora - self._refresco >= REFRESH_S * 1000:
            await self.refrescar(ahora)

    async def run(self) -> None:
        if not ENABLED:
            log.info("Evolución apagada (BOT_EVOLUCION=0)")
            return
        while True:
            try:
                await self.paso()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Evolución: fallo en la ronda; sigue en la próxima")
            await asyncio.sleep(TICK_S)

    # ---- web

    def reversible(self, e: dict) -> bool:
        """¿Se puede pedir su reversión? Lo que revierte este módulo (estrategia y modo) y lo de un origen con reversor
        (fuentes_vivas, aprendizaje); nunca el informe, un veredicto de medio, una propuesta descartada ni lo ya revertido."""
        if e.get("revertida_por") or e.get("tipo") in NO_REVERSIBLES:
            return False
        if (e.get("evidencia") or {}).get("tipo_cambio") == "veredicto":   # copiadas antes como peso_cambiado
            return False
        if e.get("origen") in self.reversores or e.get("tipo") in self.reversores:
            return True
        if e.get("sujeto") == "evolucion.modo":
            return isinstance(e.get("antes"), dict)
        return e.get("tipo") in PROPIOS and isinstance(e.get("antes"), dict) and isinstance(e.get("despues"), dict)

    def _ligera(self, e: dict) -> dict:
        """Entrada del historial para el mensaje y la API: con «reversible» (la web solo ofrece Revertir entonces); el
        informe va solo con su título y resumen."""
        e = {**e, "reversible": self.reversible(e)}
        if e["tipo"] == "informe_semanal" and isinstance(e.get("despues"), dict):
            d = e["despues"]
            e["despues"] = {"titulo": d.get("titulo"), "resumen": d.get("resumen")}
        return e

    def _mensaje(self, ahora: int) -> dict:
        pan = self.panorama or {}
        db = self.store.db
        cambios = [self._ligera(e) for e in leer(db, limite=30)]
        inf = leer(db, tipo="informe_semanal", limite=1)
        props = self._propuestas("pendiente", 50)
        recientes = [p for p in self._propuestas(None, 30) if p["estado"] != "pendiente"][:10]
        c = pan.get("campeon")
        if c is not None:
            c = {**c, "nota": self.nota_archivo}
        if self.err:
            estado, nota = "error", f"El último cálculo falló ({self.err}); se reintenta en la próxima ronda."
        elif not pan:
            estado, nota = "arrancando", "Calculando el campeón y los aspirantes…"
        elif not (c or {}).get("vivo", {}).get("operaciones") and not any(a["vivo"]["operaciones"]
                                                                          for a in pan.get("aspirantes", [])):
            estado, nota = "sin_datos", "Aún no hay operaciones terminadas del campeón ni de los aspirantes."
        else:
            estado, nota = "ok", None
        ultimo = self._estado("ultimo_cambio")
        bloqueos = self._estado("bloqueos", {}) or {}
        return {"type": "evolucion", "ts": ahora, "estado": estado, "nota": nota, "modo": self.modo, "aviso": AVISO,
                "reglas": REGLAS, "criterios": CRITERIOS, "campeon": c, "aspirantes": pan.get("aspirantes", []),
                "pruebas": pan.get("pruebas"),
                "propuestas": [{"id": p["id"], "ts": p["ts"], "tipo": p["tipo"], "sujeto": p["sujeto"],
                                "titulo": p["cuerpo"].get("titulo"), "texto": p["cuerpo"].get("texto"),
                                "cambio": p["cuerpo"].get("cambio"), "evidencia": p["cuerpo"].get("evidencia"),
                                "actualizada": p["actualizada"], "bloqueada": p["cuerpo"].get("bloqueada"),
                                "automatica": self.modo == "automatico" and p["tipo"] != "parametro_cambiado"
                                and not p["cuerpo"].get("bloqueada")}
                               for p in props],
                "recientes": [{"id": p["id"], "ts": p["ts"], "tipo": p["tipo"], "titulo": p["cuerpo"].get("titulo"),
                               "estado": p["estado"], "resuelta_ts": p["resuelta_ts"], "por": p["resuelta_por"],
                               "nota": p["nota"]} for p in recientes],
                "bloqueos": sorted(({"clave": k, **v} for k, v in bloqueos.items()),
                                   key=lambda b: -(b.get("ts") or 0))[:20],
                "cambios": cambios,
                "informe": {"id": inf[0]["id"], "ts": inf[0]["ts"], "semana": inf[0]["sujeto"],
                            "titulo": (inf[0]["despues"] or {}).get("titulo"),
                            "resumen": (inf[0]["despues"] or {}).get("resumen")} if inf else None,
                "ultima_evaluacion": self._estado("ultima_evaluacion"), "proxima_evaluacion": proxima_evaluacion(ahora),
                "ultimo_cambio": ultimo,
                "automatico_desde": int(ultimo) + ENFRIAR_D * DAY if ultimo and self.modo == "automatico" else None,
                "ultima_orden": self.ultima_orden}

    def _publicar(self, ahora: int) -> None:
        try:
            msg = json.loads(_dumps(self._mensaje(ahora)))   # solo tipos de JSON (nada de numpy)
            key = _dumps({k: v for k, v in msg.items() if k not in ("ts", "proxima_evaluacion")})
            self.msg = msg
            if key != self._key:
                self._key = key
                self.bus.publish(msg)
        except Exception:
            log.exception("Evolución: no pude publicar el mensaje «evolucion»")

    async def atender(self, m: dict) -> dict:
        """Orden de la web: {"type": "evolucion", "accion": "aplicar|descartar|revertir|evaluar|modo", "id": "p…|v…",
        "motivo": "…", "modo": "proponer|automatico"}. El resultado queda en ultima_orden del mensaje."""
        acc = m.get("accion")
        oid = m.get("id")
        if acc not in ACCIONES:
            res = {"ok": False, "why": "Acción desconocida"}
        elif acc in ("aplicar", "descartar", "revertir") and not (isinstance(oid, str) and 1 <= len(oid) <= 128):
            res = {"ok": False, "why": "Falta el id (texto de 1 a 128 caracteres)"}
        elif acc == "aplicar":
            res = await self.aplicar(oid, "usuario (web)")
        elif acc == "descartar":
            mot = m.get("motivo") if isinstance(m.get("motivo"), str) else ""
            res = await self.descartar(oid, mot[:300], "usuario (web)")
        elif acc == "revertir":
            res = await self.revertir(oid, "usuario (web)")
        elif acc == "evaluar":
            pan = await self.evaluar()
            res = {"ok": pan is not None, "why": None if pan is not None else "El cálculo falló: mira la nota"}
        else:
            res = await self.set_modo(m.get("modo"), "usuario (web)")
        self.ultima_orden = {"accion": acc, "id": oid if isinstance(oid, str) else None, **res, "ts": self.clock()}
        self._publicar(self.clock())
        return res

    def orden_web(self, m: dict) -> bool:
        """Para server.on_ws_message (síncrono): valida lo básico y lanza atender() en una tarea. False si no es una
        orden de la evolución."""
        if not isinstance(m, dict) or m.get("type") != "evolucion" or m.get("accion") not in ACCIONES:
            return False
        t = asyncio.get_running_loop().create_task(self.atender(m))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return True

    # ---- rutas de la API (consultas en un hilo con conexión propia)

    async def api_vista(self) -> dict:
        """GET /api/evolucion: el último mensaje «evolucion» (o uno nuevo)."""
        if self.msg is None:
            self._publicar(self.clock())
        return self.msg or {"type": "evolucion", "estado": "arrancando"}

    async def api_historial(self, tipo=None, sujeto: str | None = None, origen: str | None = None,
                            desde: int | None = None, hasta: int | None = None, limite: int = 200) -> dict:
        """GET /api/evolucion/historial: la línea de tiempo con filtros, del más reciente al más antiguo."""
        def hilo():
            with self.store.reader() as db:
                return leer(db, tipo, sujeto, origen, desde, hasta, limite)
        items = await asyncio.to_thread(hilo)
        return {"cambios": [self._ligera(e) for e in items], "tipos": TIPOS}

    async def api_informes(self, semana: str | None = None, limite: int = 12) -> dict:
        """GET /api/evolucion/informes: los informes semanales completos (o el de una semana)."""
        def hilo():
            with self.store.reader() as db:
                return leer(db, "informe_semanal", sujeto=semana, limite=limite)
        items = await asyncio.to_thread(hilo)
        return {"informes": [{"id": e["id"], "ts": e["ts"], **(e["despues"] or {})} for e in items]}

    async def api_propuestas(self, estado: str | None = None, limite: int = 100) -> dict:
        """GET /api/evolucion/propuestas: propuestas (todas o de un estado), de la más reciente a la más antigua."""
        estado = estado if estado in ESTADOS else None

        def hilo():
            with self.store.reader() as db:
                return self._propuestas(estado, max(1, min(int(limite or 100), 1000)), db)
        return {"propuestas": await asyncio.to_thread(hilo)}
