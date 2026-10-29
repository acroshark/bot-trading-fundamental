"""Modo real con OKX EU: preparado y APAGADO hasta que el usuario meta sus claves y lo active él (docs/06-modo-real.md).

Modos (evaluar_modo; se vuelven a mirar cada pocos segundos):
- «apagado»: por defecto, y siempre que falte el archivo de claves o cualquiera de las tres claves. No hace peticiones
  privadas.
- «solo_lectura»: claves completas pero sin todo lo que pide el modo real. Lee saldo, comisiones, posiciones y órdenes.
  No abre nada, no mueve stops ni cierra por tiempo. Lo único que manda (y solo si el bot ya tiene posiciones suyas en esa
  misma cuenta) son órdenes que reducen el riesgo: poner o reponer el stop de lo suyo, cancelar entradas suyas y, si se
  pide la parada con cierre, cerrar lo suyo. El cliente rechaza antes de firmar cualquier otra orden (reduce_riesgo).
- «demo»: OKX_DEMO=1. Opera en la demo de OKX (cabecera x-simulated-trading: 1), con dinero ficticio.
- «real»: solo con el archivo de claves completo, OKX_DEMO=0, la variable BOT_MODO_REAL=1 y la confirmación escrita que
  guarda la web (secretos/real_confirmado, con la fecha y la huella de ESAS claves: con otras claves no vale), y sin
  parada de emergencia. Si falta algo, se queda en solo lectura y dice por qué.

Claves: secretos/okx.env, FUERA del repositorio (M:\\BotFundamental-datos\\secretos). Se leen al arrancar; el código
nunca escribe ese archivo (solo crea la plantilla okx.env.ejemplo si falta) y nunca enseña un valor: ni en el registro,
ni en los eventos del bus, ni en la base, ni en la web (todo lo que sale pasa por okx_firma.limpiar). El bot NO puede
retirar fondos ni mover dinero entre cuentas: lista blanca de rutas (RUTAS_PERMITIDAS) comprobada antes de firmar, sin
ninguna ruta de retiradas, transferencias, subcuentas, conversiones ni préstamos.

RealBroker tiene la interfaz pública de PaperBroker (submit, portfolio, decide, close, cancel, set_mode, halt, resume,
results, run, announce, publish_portfolio), así el motor de aperturas no nota la diferencia. Además: parada_emergencia,
set_limites, confirmar y vista_real (panel «Cuenta real»). Las órdenes salen en el bucle asíncrono (run → ronda): los
métodos que llama el motor o la web solo deciden y ponen en cola.
- La entrada es a mercado (con clOrdId y la cabecera expTime) y, al llenarse, el stop y el objetivo quedan EN EL
  EXCHANGE como órdenes algo (OCO para la parte con objetivo, condicional para la que solo lleva stop): si el bot cae,
  siguen vivos en OKX. Si no se pueden poner, la posición se cierra al momento; si tampoco se puede cerrar, la parte
  queda marcada «SIN STOP» (aviso fuerte en el panel) y cada pocos segundos se vuelve a intentar poner el stop o cerrar.
- Una orden en duda (corte de red, OKX ocupado) nunca se repite a ciegas ni se da por perdida: se consulta por su
  clOrdId hasta pasado su expTime y, mientras no se sepa, el registro se queda «enviando» (cuenta en los límites).
- Salida A (parcial + cerrojo + seguimiento): la parcial es la OCO de la primera mitad; al llegar al cerrojo, el stop
  de lo que queda sube al suelo y desde ahí sigue al mejor precio (paper.lock_update) con amend-algos: el stop solo
  sube (en un corto, solo baja) y nunca se mueve antes del cerrojo. Si el precio ya ha cruzado el nuevo stop, se cierra
  a mercado (como el paper). Sin break-even ni stop dinámico.
- Cierre por tiempo y cierre manual: el bot comprueba que la posición está en OKX, cierra a mercado y cancela las algo
  (al contado, primero las algo, y solo vende si OKX confirma que ya no están vivas).
- Reconciliación al arrancar y cada 5 min: manda lo que diga OKX (posiciones y órdenes abiertas).
- Cada registro lleva la cuenta y el entorno (demo o real) en que se abrió: lo de otra cuenta o de la demo se aparta y
  nunca se gestiona ni se cierra con otras claves. Al cambiar de claves o de entorno, lo que esperaba aprobación o
  salir se anula: una orden pedida en la demo nunca sale en la cuenta real.
- Una algo que OKX ya no encuentra (ni viva ni en su historial) deja su parte SIN STOP: se vuelve a proteger o se cierra.
- Cada 5 min se vuelven a mirar los permisos de la clave: con permiso de retirada, el bot deja de operar.
- Límites duros que la web puede bajar y nunca subir (Limites): importe por operación (10 USDC), pérdida del día (3 %),
  4 posiciones, una por moneda, 6 entradas al día, reglas del usuario SIEMPRE (también con BOT_USER_RULES=0), y los
  controles del paper (diferencial, pausa macro, exposición ≤ 100 % y perpetuos ≤ 2 × capital).
- Parada de emergencia (archivo secretos/PARAR_REAL o parada_emergencia()): cancela las entradas pendientes, protege lo
  que esté a medias, NO cierra posiciones salvo que se pida (cerrar_posiciones=True o la palabra CERRAR en el archivo,
  también después de una primera parada y también en solo lectura) y deja el modo en solo lectura. La parada queda en
  memoria y en la base: solo la quita una confirmación nueva desde la web.
- Sin cortos si la cuenta no tiene X-Perps (lo detecta al leer la cuenta y lo dice).
Nada de esto se ha probado contra OKX: las pruebas usan un HTTP simulado. Lo «sin confirmar» está en docs/06."""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

import aiohttp
from yarl import URL

from . import config, paper
from .models import now_ms
from .okx_firma import Claves, enmascarar, hora_iso, limpiar, proteger_registro
from .paper import (MAD, STOP_NAMES, _ago, _f, _hm, _n, _px, _qty, _sg, _verb, day_start_ms, instrument_of, market_of,
                    next_midnight_ms)

log = proteger_registro(logging.getLogger("okx_real"))

# ---------------------------------------------------------------- archivos y modos

APAGADO, SOLO_LECTURA, DEMO, REAL = "apagado", "solo_lectura", "demo", "real"
OPERAN = (DEMO, REAL)
ARCHIVO_CLAVES = "okx.env"
ARCHIVO_EJEMPLO = "okx.env.ejemplo"
ARCHIVO_CONFIRMADO = "real_confirmado"
ARCHIVO_PARAR = "PARAR_REAL"
VAR_MODO_REAL = "BOT_MODO_REAL"
FRASE_CONFIRMACION = "ACTIVO EL MODO REAL"
MAX_ARCHIVO = 16_384   # bytes: un archivo de claves o de parada más grande no se lee

PLANTILLA = """\
# Claves de la API de OKX EU para el modo real del bot (docs/06-modo-real.md, «Cómo activarlo»).
#
# 1. Copia este archivo como «okx.env» en esta misma carpeta (M:\\BotFundamental-datos\\secretos\\okx.env) y rellena
#    las cuatro líneas de abajo. Este archivo de ejemplo no se lee nunca.
# 2. Crea la clave en https://my.okx.com -> Perfil -> API (API keys):
#    - Permisos: para empezar, SOLO «Lectura» (Read): basta para que el bot lea tu saldo y tus comisiones. «Operar»
#      (Trade) solo el día que decidas activar el modo real. NUNCA «Retirar» (Withdraw): con ese permiso el bot se
#      niega a operar.
#    - Restricción de IP: pon la IP pública de tu PC. Sin IP, OKX caduca la clave tras 14 días sin uso.
#    - La frase de paso (passphrase) la eliges tú al crear la clave. OKX no la guarda: si la pierdes, crea otra clave.
# 3. Las claves de tu cuenta van con OKX_DEMO=0 (así viene). Con ellas el paper pasa a cobrar la comisión real de tu
#    cuenta y, si no tiene X-Perps, deja de abrir cortos. La DEMO de OKX es aparte: sus claves se crean en Operar ->
#    Trading demo -> Centro personal -> API de demo, van con OKX_DEMO=1 y con ellas el bot puede mandar órdenes de
#    dinero ficticio a la demo (solo si además pones BOT_BROKER=real).
# 4. Con OKX_DEMO=0 el bot solo LEE la cuenta (saldo, comisiones, posiciones y órdenes) hasta que además:
#    - arranques el bot con la variable de entorno BOT_MODO_REAL=1, y
#    - escribas la confirmación en la web del bot (panel «Cuenta real»), ya con las claves reales puestas aquí.
#    La confirmación vale solo para ESTAS claves: si las cambias, hay que confirmar otra vez.
#
# Reglas de este archivo:
# - Una línea por valor: NOMBRE=valor, sin espacios alrededor del «=» (si pones comillas, se quitan).
# - No lo subas a ningún sitio, no lo copies a OneDrive ni lo pegues en un chat. El bot nunca lo escribe y nunca
#   enseña su contenido: ni en la web, ni en los registros, ni en la base de datos.
# - Parada de emergencia: crea en esta carpeta un archivo llamado PARAR_REAL (puede estar vacío). El bot deja de abrir
#   operaciones, cancela las entradas pendientes y pasa a solo lectura; los stops siguen puestos en OKX. Si el archivo
#   contiene la palabra CERRAR, además cierra a mercado las posiciones del bot (también si ya estaba parado). La parada
#   solo se quita escribiendo otra vez la confirmación en la web: borrar el archivo no basta.

OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
OKX_DEMO=0
"""


def carpeta_secretos() -> Path:
    """M:\\BotFundamental-datos\\secretos (o DATA_DIR/secretos): fuera del repositorio y fuera de OneDrive."""
    return config.DATA_DIR / "secretos"


def crear_plantilla(carpeta: Path | None = None) -> Path | None:
    """Crea la carpeta y okx.env.ejemplo si no existen. Nunca crea ni toca okx.env. Devuelve la ruta si la ha creado."""
    carpeta = Path(carpeta or carpeta_secretos())
    ruta = carpeta / ARCHIVO_EJEMPLO
    if ruta.exists():
        return None
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta.write_text(PLANTILLA, encoding="utf-8")
    log.info("Modo real: plantilla de claves creada en %s (cópiala como %s y rellénala)", ruta, ARCHIVO_CLAVES)
    return ruta


def _leer_texto(ruta: Path) -> str | None:
    """Texto de un archivo pequeño o None si no existe, no se puede leer o es demasiado grande."""
    try:
        if not ruta.is_file() or ruta.stat().st_size > MAX_ARCHIVO:
            return None
        return ruta.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def leer_env(texto: str) -> dict[str, str]:
    """NOMBRE=valor por línea; ignora comentarios (#) y líneas vacías; quita «export » y las comillas de los extremos.
    No quita «#» dentro de un valor (una frase de paso puede llevarlo)."""
    out = {}
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        if linea.lower().startswith("export "):
            linea = linea[7:].strip()
        k, sep, v = linea.partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def leer_confirmacion(carpeta: Path, huella: str | None = None) -> dict | None:
    """La confirmación escrita que guarda la web (JSON con la frase, la fecha y la huella de las claves) o None si falta
    o no vale. Con huella, solo vale si es la de esas claves (una confirmación dada con otras claves no sirve)."""
    txt = _leer_texto(Path(carpeta) / ARCHIVO_CONFIRMADO)
    try:
        d = json.loads(txt) if txt else None
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("frase") != FRASE_CONFIRMACION or not _f(d.get("ms")):
        return None
    if huella is not None and d.get("huella") != huella:
        return None
    return {"ms": int(_f(d["ms"])), "fecha": str(d.get("fecha") or "")}


def leer_claves(carpeta: Path) -> tuple[Claves | None, str | None]:
    """(claves, OKX_DEMO) del archivo de claves, o (None, None) si no se puede leer. Nunca devuelve el texto."""
    txt = _leer_texto(Path(carpeta) / ARCHIVO_CLAVES)
    if txt is None:
        return None, None
    vals = leer_env(txt)
    return (Claves(vals.get("OKX_API_KEY"), vals.get("OKX_API_SECRET"), vals.get("OKX_API_PASSPHRASE")),
            vals.get("OKX_DEMO", "").strip())


def leer_parada(carpeta: Path) -> dict | None:
    """La parada de emergencia (secretos/PARAR_REAL) o None. Vale con el archivo vacío; con la palabra CERRAR (o
    «cerrar»: true en JSON) también pide cerrar las posiciones."""
    ruta = Path(carpeta) / ARCHIVO_PARAR
    if not ruta.exists():
        return None
    txt = _leer_texto(ruta) or ""
    try:
        d = json.loads(txt) if txt.strip().startswith("{") else None
    except ValueError:
        d = None
    try:
        ms = int(ruta.stat().st_mtime * 1000)
    except OSError:
        ms = None
    if isinstance(d, dict):
        return {"motivo": str(d.get("motivo") or "Archivo PARAR_REAL")[:200], "cerrar": d.get("cerrar") is True,
                "ms": int(_f(d.get("ms"), ms or 0)), "fecha": str(d.get("fecha") or "")}
    return {"motivo": "Archivo PARAR_REAL", "cerrar": "CERRAR" in txt.upper(), "ms": ms, "fecha": ""}


def _fecha(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).strftime("%d-%m-%Y %H:%M:%S")


def frase_ok(frase: str) -> bool:
    return " ".join(str(frase or "").split()).upper() == FRASE_CONFIRMACION


def confirmar_real(carpeta: Path, frase: str, ahora: int) -> tuple[bool, str]:
    """Confirmación escrita desde la web: guarda secretos/real_confirmado con la fecha y la huella de las claves de
    okx.env, y quita una parada de emergencia anterior. Solo se acepta con las tres claves y OKX_DEMO=0 (una dada en demo
    o con otras claves no vale para el modo real). Por sí sola no activa nada: el modo real pide además
    BOT_MODO_REAL=1."""
    if not frase_ok(frase):
        return False, f"La frase no coincide: escribe exactamente «{FRASE_CONFIRMACION}»"
    carpeta = Path(carpeta)
    claves, demo = leer_claves(carpeta)
    if claves is None or not claves.completas():
        return False, "No se guarda: primero pon tus claves reales completas en okx.env"
    if demo != "0":
        return False, "No se guarda: la confirmación del modo real solo vale con OKX_DEMO=0 (claves reales)"
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / ARCHIVO_CONFIRMADO).write_text(
            json.dumps({"frase": FRASE_CONFIRMACION, "ms": int(ahora), "fecha": _fecha(ahora), "por": "web",
                        "huella": claves.huella()}, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.error("Modo real: no pude guardar la confirmación: %s", e)
        return False, "No se pudo guardar la confirmación en la carpeta de secretos"
    try:
        (carpeta / ARCHIVO_PARAR).unlink(missing_ok=True)
    except OSError as e:
        log.error("Modo real: no pude borrar %s: %s", ARCHIVO_PARAR, e)
    log.warning("Modo real: confirmación escrita guardada (%s)", _fecha(ahora))
    return True, f"Confirmación guardada el {_fecha(ahora)} (Madrid), solo para las claves que hay ahora en okx.env"


def parar_real(carpeta: Path, motivo: str, cerrar: bool, ahora: int) -> tuple[dict, bool, bool]:
    """Escribe secretos/PARAR_REAL y borra la confirmación: el modo real queda apagado hasta confirmar otra vez. Cada
    paso va aparte (si no se puede escribir PARAR_REAL, la confirmación se borra igual). Devuelve (parada, escrito,
    confirmación borrada)."""
    carpeta = Path(carpeta)
    d = {"motivo": str(motivo or "Parada de emergencia")[:200], "cerrar": bool(cerrar), "ms": int(ahora),
         "fecha": _fecha(ahora)}
    escrito = borrada = False
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / ARCHIVO_PARAR).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        escrito = True
    except OSError as e:   # la parada vale igual en memoria y en la base
        log.error("Modo real: no pude escribir %s: %s", ARCHIVO_PARAR, e)
    try:
        (carpeta / ARCHIVO_CONFIRMADO).unlink(missing_ok=True)
        borrada = True
    except OSError as e:
        log.error("Modo real: no pude borrar la confirmación: %s", e)
    return d, escrito, borrada


def _texto_parada(p: dict) -> str:
    return (f"Parada de emergencia activa ({p.get('motivo') or 'PARAR_REAL'}"
            + (f", {p['fecha']}" if p.get("fecha") else "") + "): para quitarla, confirma otra vez en la web")


def _clave_parada(p: dict) -> list:
    """Lo que distingue una parada por archivo de otra (para atender cada una una sola vez)."""
    return [p.get("ms"), bool(p.get("cerrar"))]


@dataclass(repr=False)
class EstadoModo:
    modo: str
    motivos: list[str]
    demo: bool = False
    claves: Claves | None = None

    def opera(self) -> bool:
        return self.modo in OPERAN

    def vista(self) -> dict:
        return {"modo": self.modo, "motivos": list(self.motivos), "demo": self.demo, "opera": self.opera()}

    def __repr__(self) -> str:
        return f"EstadoModo({self.modo}, demo={self.demo}, motivos={self.motivos!r})"


def evaluar_modo(carpeta: Path | None = None, env=None) -> EstadoModo:
    """Modo que toca con el archivo de claves, OKX_DEMO, BOT_MODO_REAL, la confirmación y la parada. Los motivos nombran
    lo que falta, nunca un valor."""
    env = os.environ if env is None else env
    carpeta = Path(carpeta or carpeta_secretos())
    ruta = carpeta / ARCHIVO_CLAVES
    if not ruta.exists():
        return EstadoModo(APAGADO, [f"Apagado: no hay archivo de claves ({ruta})"])
    txt = _leer_texto(ruta)
    if txt is None:
        return EstadoModo(APAGADO, [f"Apagado: no se puede leer el archivo de claves ({ruta})"])
    vals = leer_env(txt)
    claves = Claves(vals.get("OKX_API_KEY"), vals.get("OKX_API_SECRET"), vals.get("OKX_API_PASSPHRASE"))
    if not claves.completas():
        return EstadoModo(APAGADO, ["Apagado: faltan en okx.env " + ", ".join(claves.faltan())])
    demo = vals.get("OKX_DEMO", "").strip()
    parada = leer_parada(carpeta)
    if demo == "1":
        if parada:
            return EstadoModo(SOLO_LECTURA, [_texto_parada(parada)], True, claves)
        return EstadoModo(DEMO, ["Demo de OKX: órdenes con dinero ficticio (cabecera x-simulated-trading)"], True, claves)
    if demo != "0":
        return EstadoModo(SOLO_LECTURA, ["Solo lectura: OKX_DEMO no es 0 (real) ni 1 (demo)"], False, claves)
    faltan = []
    if str(env.get(VAR_MODO_REAL, "")).strip() != "1":
        faltan.append(f"Falta arrancar el bot con {VAR_MODO_REAL}=1")
    if not leer_confirmacion(carpeta, claves.huella()):
        faltan.append("Falta tu confirmación escrita en la web para estas claves (panel «Cuenta real»)")
    if parada:
        faltan.append(_texto_parada(parada))
    if faltan:
        return EstadoModo(SOLO_LECTURA, ["Solo lectura (ninguna orden)", *faltan], False, claves)
    return EstadoModo(REAL, ["Modo real ACTIVO: opera con tu dinero en OKX EU"], False, claves)


# ---------------------------------------------------------------- rutas: lista blanca y lo prohibido

BASE_REST = "https://eea.okx.com"
RUTAS_PUBLICAS = frozenset({
    ("GET", "/api/v5/public/time"), ("GET", "/api/v5/public/instruments"), ("GET", "/api/v5/market/ticker")})
RUTAS_LECTURA = frozenset({
    ("GET", "/api/v5/account/config"), ("GET", "/api/v5/account/balance"), ("GET", "/api/v5/account/trade-fee"),
    ("GET", "/api/v5/account/positions"), ("GET", "/api/v5/account/instruments"), ("GET", "/api/v5/trade/order"),
    ("GET", "/api/v5/trade/orders-pending"), ("GET", "/api/v5/trade/order-algo"),
    ("GET", "/api/v5/trade/orders-algo-pending")})
RUTAS_ORDENES = frozenset({
    ("POST", "/api/v5/trade/order"), ("POST", "/api/v5/trade/cancel-order"), ("POST", "/api/v5/trade/order-algo"),
    ("POST", "/api/v5/trade/amend-algos"), ("POST", "/api/v5/trade/cancel-algos"),
    ("POST", "/api/v5/account/set-leverage")})
RUTAS_PERMITIDAS = RUTAS_PUBLICAS | RUTAS_LECTURA | RUTAS_ORDENES
# Lo que mueve dinero fuera del trading (docs/06, apartado 5). Solo aparece aquí, para rechazarlo.
PREFIJOS_PROHIBIDOS = ("/api/v5/asset/", "/api/v5/users/", "/api/v5/fiat/", "/api/v5/finance/",
                       "/api/v5/account/subaccount", "/api/v5/broker/")
PALABRAS_PROHIBIDAS = ("withdraw", "transfer", "subaccount", "borrow", "repay", "convert", "margin-balance",
                       "deposit", "demo-adjust", "savings", "staking", "lending")
PERMISO_RETIRADA = "withdraw"   # en account/config, «perm»: con él, el bot se niega a operar

LIMITES_RUTA = {   # (peticiones, segundos) de la documentación; el cliente usa la mitad
    ("POST", "/api/v5/trade/order"): (60, 2.0), ("GET", "/api/v5/trade/order"): (60, 2.0),
    ("POST", "/api/v5/trade/cancel-order"): (60, 2.0), ("POST", "/api/v5/trade/order-algo"): (20, 2.0),
    ("POST", "/api/v5/trade/amend-algos"): (20, 2.0), ("POST", "/api/v5/trade/cancel-algos"): (20, 2.0),
    ("GET", "/api/v5/trade/orders-pending"): (60, 2.0), ("GET", "/api/v5/trade/order-algo"): (20, 2.0),
    ("GET", "/api/v5/trade/orders-algo-pending"): (20, 2.0), ("GET", "/api/v5/account/positions"): (10, 2.0),
    ("GET", "/api/v5/account/balance"): (10, 2.0), ("GET", "/api/v5/account/trade-fee"): (5, 2.0),
    ("GET", "/api/v5/account/config"): (5, 2.0), ("GET", "/api/v5/account/instruments"): (20, 2.0),
    ("POST", "/api/v5/account/set-leverage"): (20, 2.0), ("GET", "/api/v5/public/time"): (10, 2.0),
    ("GET", "/api/v5/public/instruments"): (20, 2.0), ("GET", "/api/v5/market/ticker"): (20, 2.0)}
CODIGOS_CLAVE = frozenset({"50100", "50101", "50103", "50104", "50105", "50106", "50107", "50110", "50111", "50112",
                           "50113", "50114", "50119", "50120"})   # clave, firma, frase, IP o permiso: no se reintenta
CODIGOS_LIMITE = frozenset({"50011", "50061"})   # límite de peticiones: rechazada seguro, se repite con espera
CODIGOS_OCUPADO = frozenset({"50013", "50001"})  # OKX ocupado o no disponible: un GET se repite; un POST queda en duda
CODIGOS_REINTENTO = CODIGOS_LIMITE | CODIGOS_OCUPADO
CODIGOS_DUDA = frozenset({"50004", "50026"})     # una orden en duda: se comprueba con clOrdId
CODIGO_HORA = "50102"
NO_EXISTE = frozenset({"51603", "51400", "51401", "51402", "51503", "51509"})   # la orden o la algo ya no está
STOP_CRUZADO = frozenset({"51278", "51280"})     # stop del lado equivocado del último precio (sin confirmar en demo)
REINTENTOS = 3
ESPERA_S = 0.5
ESPERA_MAX_S = 4.0
TIMEOUT = aiohttp.ClientTimeout(total=10)
EXP_ORDEN_MS = 10_000      # expTime de las órdenes a mercado: si OKX la recibe más tarde, la descarta
MARGEN_EXP_MS = 5_000      # pasado expTime + este margen, una orden que OKX no encuentra no llegó nunca


class ErrorOKX(Exception):
    """Fallo de una petición a OKX. reintentable: se puede repetir; ambiguo: una orden que quizá llegó."""

    def __init__(self, codigo, texto, reintentable: bool = False, ambiguo: bool = False):
        self.codigo, self.texto = str(codigo), enmascarar(str(texto or ""))[:300]
        self.reintentable, self.ambiguo = reintentable, ambiguo
        super().__init__(f"OKX {self.codigo}: {self.texto}")


class ErrorClave(ErrorOKX):
    """Clave, firma, frase de paso, IP o permiso rechazados: no se reintenta y el bot deja de operar."""


class RutaProhibida(ErrorOKX):
    """Ruta fuera de la lista blanca, o una orden en solo lectura: se rechaza antes de firmar."""


def ruta_prohibida(ruta: str) -> bool:
    r = str(ruta).lower()
    return r.startswith(PREFIJOS_PROHIBIDOS) or any(p in r for p in PALABRAS_PROHIBIDAS)


def comprobar_ruta(metodo: str, ruta: str) -> None:
    """Solo pasa una ruta exacta de la lista blanca (sin consulta, sin «..» ni barras dobles)."""
    if (not isinstance(ruta, str) or not ruta.startswith("/api/v5/") or ".." in ruta or "//" in ruta
            or any(c in ruta for c in "?#\\ %")):
        raise RutaProhibida("ruta", f"Ruta no válida: {str(ruta)[:80]}")
    if ruta_prohibida(ruta):
        raise RutaProhibida("prohibida", f"{metodo} {ruta}: el bot nunca retira ni mueve dinero entre cuentas")
    if (metodo, ruta) not in RUTAS_PERMITIDAS:
        raise RutaProhibida("fuera_de_lista", f"{metodo} {ruta} no está en la lista blanca del modo real")


def reduce_riesgo(metodo: str, ruta: str, cuerpo) -> bool:
    """¿Es una orden que solo puede reducir el riesgo? Cancelar órdenes o algo; vender al contado (en cash no se puede
    vender lo que no se tiene); en X-Perps, solo con reduceOnly. Lo que se admite en solo lectura y en la parada."""
    if metodo != "POST":
        return False
    if ruta in ("/api/v5/trade/cancel-order", "/api/v5/trade/cancel-algos"):
        return True
    if ruta in ("/api/v5/trade/order", "/api/v5/trade/order-algo") and isinstance(cuerpo, dict):
        return cuerpo.get("reduceOnly") is True or (cuerpo.get("tdMode") == "cash" and cuerpo.get("side") == "sell")
    return False


_SEGURO = re.compile(r"[A-Za-z0-9_.,:\-]*")


def consulta(params: dict | None) -> str:
    """Cadena de consulta tal cual se firma y se envía (como el SDK oficial: sin codificar). Solo admite letras,
    números y _ . , : - (instId, clOrdId, ordType…): cualquier otro carácter se rechaza antes de firmar."""
    partes = []
    for k, v in (params or {}).items():
        if v is None:
            continue
        k, v = str(k), str(v)
        if not k or not _SEGURO.fullmatch(k) or not _SEGURO.fullmatch(v):
            raise RutaProhibida("consulta", "Parámetro de consulta con caracteres no admitidos")
        partes.append(f"{k}={v}")
    return "&".join(partes)


# ---------------------------------------------------------------- cliente REST

class ClienteOKX:
    """REST de OKX EU (eea.okx.com) con firma, lista blanca, límite de peticiones y reintentos. escritura=False (por
    defecto) rechaza cualquier orden antes de firmarla; solo el broker lo enciende, en demo o en real. reducir=True
    deja pasar, sin escritura, solo las órdenes que reducen el riesgo (reduce_riesgo)."""

    def __init__(self, session, claves: Claves | None, demo: bool, clock=now_ms, dormir=asyncio.sleep,
                 mono=time.monotonic, reintentos: int = REINTENTOS):
        self.session = session
        self._claves = claves
        self.demo = bool(demo)
        self.clock, self._dormir, self._mono = clock, dormir, mono
        self.reintentos = reintentos
        self.escritura = False
        self.reducir = False
        self.desfase_ms = 0
        self.peticiones = 0
        self._turnos: dict[tuple, deque] = {}

    def __repr__(self) -> str:
        return (f"ClienteOKX({'demo' if self.demo else 'real'}, escritura={self.escritura}, reducir={self.reducir}, "
                f"{self._claves!r})")

    def hora_okx(self) -> int:
        """Hora del servidor de OKX estimada (reloj local + desfase medido)."""
        return int(self.clock() + self.desfase_ms)

    async def publico(self, ruta: str, params: dict | None = None) -> list:
        """GET público sin firmar (instrumentos, hora, precio)."""
        if ("GET", ruta) not in RUTAS_PUBLICAS:
            raise RutaProhibida("no_publica", f"{ruta} no es una ruta pública")
        return await self._pedir("GET", ruta, params, None, False)

    async def privado(self, metodo: str, ruta: str, params: dict | None = None, cuerpo=None,
                      exp_ms: int | None = None) -> list:
        """Petición firmada. Las de RUTAS_ORDENES solo con escritura (o, con reducir, las que reducen el riesgo).
        exp_ms: cabecera expTime (hora de OKX en ms tras la que la orden se descarta), solo en POST /trade/order."""
        metodo = metodo.upper()
        if (metodo, ruta) in RUTAS_PUBLICAS:
            raise RutaProhibida("publica", f"{ruta} es pública: no se firma")
        return await self._pedir(metodo, ruta, params, cuerpo, True, exp_ms)

    async def sincronizar_hora(self) -> int:
        """Desfase con el reloj de OKX (GET /api/v5/public/time): las firmas con más de 30 s de desfase no valen."""
        antes = self.clock()
        d = await self.publico("/api/v5/public/time")
        despues = self.clock()
        ts = _f((d[0] if d else {}).get("ts"))
        if ts:
            self.desfase_ms = int(ts - (antes + despues) / 2)
        return self.desfase_ms

    async def _turno(self, metodo: str, ruta: str) -> None:
        n, ventana = LIMITES_RUTA.get((metodo, ruta), (10, 2.0))
        n = max(1, n // 2)
        dq = self._turnos.setdefault((metodo, ruta), deque())
        while True:
            t = self._mono()
            while dq and t - dq[0] >= ventana:
                dq.popleft()
            if len(dq) < n:
                dq.append(t)
                return
            await self._dormir(ventana - (t - dq[0]) + 0.01)

    async def _pedir(self, metodo: str, ruta: str, params: dict | None, cuerpo, firmada: bool,
                     exp_ms: int | None = None) -> list:
        comprobar_ruta(metodo, ruta)
        if (metodo, ruta) in RUTAS_ORDENES and not self.escritura and not (
                self.reducir and reduce_riesgo(metodo, ruta, cuerpo)):
            raise RutaProhibida("solo_lectura", f"{metodo} {ruta}: el modo real no está activo (solo lectura)")
        if firmada and (self._claves is None or not self._claves.completas()):
            raise ErrorClave("sin_claves", "Faltan claves en okx.env")
        q = consulta(params)
        camino = ruta + ("?" + q if q else "")
        texto_cuerpo = json.dumps(cuerpo, separators=(",", ":"), ensure_ascii=False) if cuerpo is not None else ""
        post = metodo == "POST"
        ultimo = None
        for intento in range(self.reintentos + 1):
            if intento:
                await self._dormir(min(ESPERA_MAX_S, ESPERA_S * 2 ** (intento - 1)))
            await self._turno(metodo, ruta)
            cab = {"Content-Type": "application/json", "Accept": "application/json"}
            if firmada:
                cab.update(self._claves.cabeceras(hora_iso(self.hora_okx()), metodo, camino, texto_cuerpo))
            if self.demo:
                cab["x-simulated-trading"] = "1"
            if exp_ms is not None and post and ruta == "/api/v5/trade/order":
                cab["expTime"] = str(int(exp_ms))
            self.peticiones += 1
            try:   # la URL va tal cual se ha firmado (encoded=True) y sin seguir redirecciones (no se reenvían cabeceras)
                async with self.session.request(metodo, URL(BASE_REST + camino, encoded=True), headers=cab,
                                                data=texto_cuerpo.encode("utf-8") if texto_cuerpo else None,
                                                timeout=TIMEOUT, allow_redirects=False) as r:
                    estado, texto = r.status, await r.text()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                ultimo = ErrorOKX("red", f"{type(e).__name__}: {e}", reintentable=True, ambiguo=post)
                if post:   # no se sabe si llegó: quien llama lo comprueba (clOrdId) antes de repetir
                    raise ultimo
                continue
            try:
                j = json.loads(texto)
            except ValueError:
                j = None
            if not isinstance(j, dict) or "code" not in j:   # no es una respuesta de OKX (proxy, página de error…)
                ultimo = ErrorOKX(f"http{estado}", str(texto)[:200], reintentable=estado == 429 or estado >= 500,
                                  ambiguo=post and (estado >= 500 or 200 <= estado < 300))
                if estado == 429 or (estado >= 500 and not post):
                    continue
                raise ultimo
            codigo = str(j.get("code", ""))
            datos = j.get("data") if isinstance(j.get("data"), list) else []
            if codigo == "0":
                return datos
            fila = datos[0] if datos and isinstance(datos[0], dict) else {}
            scode = str(fila.get("sCode") or "")
            cod, msg = (scode, fila.get("sMsg") or j.get("msg")) if scode and scode != "0" else (codigo, j.get("msg"))
            if cod == CODIGO_HORA and intento < self.reintentos:   # rechazada por la hora: se puede repetir
                await self.sincronizar_hora()
                ultimo = ErrorOKX(cod, msg, reintentable=True)
                continue
            if cod in CODIGOS_CLAVE:
                raise ErrorClave(cod, msg)
            if cod in CODIGOS_LIMITE or estado == 429:   # límite de peticiones: rechazada seguro
                ultimo = ErrorOKX(cod, msg, reintentable=True)
                continue
            if post and (cod in CODIGOS_OCUPADO or cod in CODIGOS_DUDA or estado >= 500):
                raise ErrorOKX(cod, msg, ambiguo=True)   # quizá se procesó: nunca se repite a ciegas
            if cod in CODIGOS_OCUPADO:
                ultimo = ErrorOKX(cod, msg, reintentable=True)
                continue
            raise ErrorOKX(cod, msg)
        raise ultimo or ErrorOKX("reintentos", "Sin respuesta válida de OKX")


# ---------------------------------------------------------------- instrumentos y redondeos

def _d(x) -> Decimal:
    try:
        return Decimal(str(x).strip()) if x not in (None, "") else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _s(x) -> str:
    """Decimal a texto para OKX, sin exponente ni ceros de sobra."""
    d = _d(x)
    s = format(d.normalize(), "f") if d else "0"
    return s.rstrip("0").rstrip(".") if "." in s else s


def redondear_abajo(x, paso) -> Decimal:
    """Múltiplo de paso más cercano por debajo (cantidades: nunca se pasa del riesgo)."""
    x, paso = _d(x), _d(paso)
    if paso <= 0:
        return x
    return ((x / paso).to_integral_value(rounding=ROUND_FLOOR) * paso).quantize(paso)


def redondear_arriba(x, paso) -> Decimal:
    x, paso = _d(x), _d(paso)
    if paso <= 0:
        return x
    return ((x / paso).to_integral_value(rounding=ROUND_CEILING) * paso).quantize(paso)


def px_tick(px, tick, arriba: bool) -> Decimal:
    """Precio en múltiplo de tickSz, hacia arriba o hacia abajo."""
    return redondear_arriba(px, tick) if arriba else redondear_abajo(px, tick)


@dataclass
class Instrumento:
    coin: str
    inst: str          # spot | perp
    inst_id: str
    inst_type: str     # SPOT | FUTURES
    lot: Decimal
    min_sz: Decimal
    tick: Decimal
    ct_val: Decimal = field(default_factory=lambda: Decimal(1))

    def sz_de_qty(self, qty) -> Decimal:
        """Cantidad en monedas → sz de la orden (monedas al contado, contratos en X-Perps), hacia abajo."""
        if self.ct_val <= 0:
            return Decimal(0)
        return redondear_abajo(_d(qty) / self.ct_val, self.lot)

    def qty_de_sz(self, sz) -> Decimal:
        return _d(sz) * self.ct_val

    def px(self, x, arriba: bool) -> Decimal:
        return px_tick(x, self.tick, arriba)

    def unidad(self) -> str:
        return self.coin if self.inst == "spot" else "contratos"

    def vista(self) -> dict:
        return {"coin": self.coin, "inst": self.inst, "instId": self.inst_id, "lotSz": _s(self.lot),
                "minSz": _s(self.min_sz), "tickSz": _s(self.tick), "ctVal": _s(self.ct_val)}


def instrumento_de(fila: dict, coin: str, inst: str) -> Instrumento | None:
    """Instrumento a partir de una fila de /public/instruments (None si falta algo o no está «live»)."""
    if not isinstance(fila, dict) or fila.get("state") != "live":
        return None
    lot, mn, tick = _d(fila.get("lotSz")), _d(fila.get("minSz")), _d(fila.get("tickSz"))
    ct = _d(fila.get("ctVal")) if inst == "perp" else Decimal(1)
    if lot <= 0 or mn <= 0 or tick <= 0 or ct <= 0:
        return None
    return Instrumento(coin, inst, str(fila.get("instId")), str(fila.get("instType")), lot, mn, tick, ct)


def spot_id(coin: str) -> str:
    return f"{coin}-{config.QUOTE}"


def xperp_familia(coin: str) -> str:
    return f"{coin}-USD_UM_XPERP"


def comision_pct(fila: dict | None, lado: str = "taker") -> float | None:
    """Comisión de tomador (lado «taker») o de creador («maker») en % de una fila de account/trade-fee (un número
    negativo es comisión; positivo, reembolso). Se toma la mayor de las que trae (taker, takerUSDC, takerU), por
    prudencia."""
    lado = "maker" if lado == "maker" else "taker"
    vals = [_f((fila or {}).get(k)) for k in (lado, f"{lado}USDC", f"{lado}U")]
    vals = [max(0.0, -v) * 100 for v in vals if v is not None]
    return max(vals) if vals else None


# ---------------------------------------------------------------- límites duros

TECHO_PERDIDA_DIA_PCT = 3.0
TECHO_POSICIONES = 4
TECHO_ENTRADAS_DIA = 6
TECHO_USDC_DEFECTO = 10.0
ENTEROS = ("max_posiciones", "max_entradas_dia")
NOMBRES_LIMITE = {"max_usdc": "importe máximo por operación (USDC)", "perdida_dia_pct": "pérdida máxima del día (%)",
                  "max_posiciones": "posiciones abiertas", "max_entradas_dia": "entradas al día"}


def techos() -> dict:
    """Los límites más altos posibles. La web solo puede bajar desde aquí. El del importe se cambia en el PC del
    usuario (BOT_REAL_MAX_USDC, por defecto 10); el resto, nunca por encima de 3 %, 4 y 6."""
    usdc = _f(os.environ.get("BOT_REAL_MAX_USDC"), TECHO_USDC_DEFECTO)
    return {"max_usdc": usdc if usdc and usdc > 0 else TECHO_USDC_DEFECTO,
            "perdida_dia_pct": max(0.0, min(TECHO_PERDIDA_DIA_PCT, config.MAX_DAILY_LOSS_PCT)),
            "max_posiciones": max(0, min(TECHO_POSICIONES, config.MAX_POSITIONS)),
            "max_entradas_dia": TECHO_ENTRADAS_DIA}


def aplicar_limites(actuales: dict, cambios: dict, techo: dict) -> tuple[dict, list[str]]:
    """Límites nuevos: cada uno > 0 y ≤ su techo (bajar sí, subir por encima del techo nunca). Devuelve (límites,
    rechazos)."""
    out, malos = {k: min(_f(actuales.get(k), techo[k]), techo[k]) for k in techo}, []
    for k, v in (cambios or {}).items():
        if k not in techo:
            malos.append(f"Límite desconocido: {k}")
            continue
        x = _f(v)
        if x is None or x <= 0:
            malos.append(f"{NOMBRES_LIMITE[k]}: valor no válido")
            continue
        if k in ENTEROS:
            x = float(int(x))
        if x > techo[k] + 1e-9:
            malos.append(f"{NOMBRES_LIMITE[k]}: no se puede subir por encima de {_n(techo[k], 2 if k == 'max_usdc' else 0)} "
                         "desde la web")
            continue
        out[k] = x
    for k in ENTEROS:
        out[k] = int(out[k])
    return out, malos


# ---------------------------------------------------------------- salidas con las reglas del usuario (siempre)

def coste_rt_pct(fee_pct: float) -> float:
    """Coste de ida y vuelta en % con la comisión real de tomador y el deslizamiento supuesto del paper."""
    return 2 * (fee_pct + paper.COST_SLIP_BPS / 100)


def regla_ok(r, stop_pct, coste: float) -> bool:
    """«O gano más de 2 o pierdo 1»: r ≥ 2 y r × stop − coste ≥ 3 % neto (con el coste real)."""
    r, sp = _f(r), _f(stop_pct)
    return bool(r and sp and r >= config.RULE_MIN_RR - 1e-9 and r * sp - coste >= config.RULE_MIN_NET_PCT - 1e-9)


def salidas_reales(t: dict, coste: float) -> dict:
    """Como paper.exit_rules con las reglas del usuario SIEMPRE activas (también con BOT_USER_RULES=0): sin break-even
    ni stop dinámico; parcial y cerrojo solo si su nivel cumple regla_ok con el coste real. Una parcial del 100 % es un
    objetivo fijo."""
    stop = _f(t.get("stopPct"))
    tp1R, tp2R = _f(t.get("tp1R")), _f(t.get("tp2R"))
    tp1R = tp1R if tp1R and tp1R > 0 else None
    tp2R = tp2R if tp2R and tp2R > 0 else None
    frac = min(1.0, max(0.0, _f(t.get("tp1Frac"), 0.0))) if tp1R else 0.0
    lock, floor, trail = _f(t.get("lockR")), _f(t.get("lockFloorR")), _f(t.get("lockTrailR"))
    if lock and lock > 0 and floor and floor > 0:
        lock = max(lock, floor)
    else:
        lock = floor = None
    trail = trail if lock and trail and trail > 0 else None
    dropped = []
    if frac and not regla_ok(tp1R, stop, coste):
        dropped.append(f"salida parcial en {_n(tp1R, 1)} R descartada por tu regla con la comisión real")
        frac = 0.0
    if frac >= 1 - 1e-9:
        tp2R, frac = tp2R or tp1R, 0.0
    if lock and not regla_ok(floor, stop, coste):
        dropped.append(f"cerrojo con suelo en {_n(floor, 1)} R descartado por tu regla con la comisión real")
        lock = floor = trail = None
    if any(_f(t.get(k)) for k in ("beR", "trailPct", "trailStartR")):
        dropped.append("break-even y stop dinámico no se usan (tu regla: nada mueve el stop antes del cerrojo)")
    return {"tp1R": tp1R if frac else None, "tp1_frac": frac, "tp2R": tp2R, "lock_r": lock, "floor_r": floor,
            "lock_trail_r": trail, "dropped": dropped}


def motivo_real(why: str, kind: str = "riskko") -> str:
    """Código de decisiones_apertura (docs/04, 2.17) para un veto o un bloqueo del modo real."""
    return "tope_dia" if str(why).startswith("Tope diario") else paper.motivo_codigo(why, kind)


# ---------------------------------------------------------------- broker

PREFIJO_CL = "bfr"                 # clOrdId y algoClOrdId del bot: bfr + 14 car. + sufijo (≤ 32, alfanumérico)
TD_PERP = "isolated"               # margen aislado en X-Perps (sin confirmar: docs/06, apartado 3)
APALANCAMIENTO = 2
RESERVA_MERCADO = 0.05             # OKX congela un 5 % de más en las compras a mercado
MAX_NOTIONAL_PCT = paper.MAX_NOTIONAL_PCT
COMISION_PRUDENTE_PCT = 0.35       # sin la comisión real, la regla del 3 % neto se mira con la peor del contado
RONDA_S = 1.0
SONDEO_MS = 5_000                  # estado de las algo de cada posición
SALDO_MS = 30_000
RECONCILIAR_MS = 5 * 60_000
VIGILAR_MS = 5_000                 # archivos de claves, confirmación y parada
ARRANQUE_REINTENTO_MS = 60_000
PASO_STOP_R = 0.1                  # el seguimiento mueve el stop en OKX cuando sube al menos esto…
MOVER_CADA_MS = 5_000              # …y como mucho cada 5 s (amend-algos: 20 / 2 s)
LLENADO_INTENTOS = 6
LLENADO_ESPERA_S = 0.5
DUDA_ESPERA_S = 1.0                # consultas por clOrdId tras una orden en duda
DUDA_EXTRA_S = 10.0                # sin respuesta de OKX tras expTime + margen + esto: se deja para más tarde
ENVIANDO_CADA_MS = 2_000           # una entrada «enviando» se consulta como mucho cada 2 s
ENTRADA_VIVA_MAX_MS = 20_000       # una entrada a mercado que sigue viva sin llenarse se cancela
ALGO_DUDA_MS = 10_000              # una algo en duda: se busca por su algoClOrdId antes de poner otra
ALGO_OLVIDO_MS = 60_000            # …y pasado esto sin aparecer, se da por no creada
ALGO_NO_ESTA_MS = 10_000           # la algo de una parte «viva» que OKX no encuentra durante esto: la parte está sin stop
REINTENTO_MIN_MS = 5_000           # espera entre intentos de proteger o cerrar (crece hasta REINTENTO_MAX_MS)
REINTENTO_MAX_MS = 60_000
AMEND_FALLOS_REEMPLAZO = 2         # amend-algos que falla N veces seguidas: se cancela y se pone otra con el stop nuevo
CLOSED_EN_CARTERA = 50
ERR_LOG_EVERY_S = 300
CAPS = ("halt", "cancel", "parada", "limites", "confirmar")
MODOS_APROBACION = ("auto", "veto", "manual")


def _cl_base(story: str, submitted: int) -> str:
    return PREFIJO_CL + hashlib.sha1(f"{story}|{submitted}".encode()).hexdigest()[:14]


def _espera(n: int) -> int:
    """Espera creciente entre reintentos (5 s, 10 s, 20 s… hasta 60 s)."""
    return int(min(REINTENTO_MAX_MS, REINTENTO_MIN_MS * 2 ** max(0, n)))


class RealBroker:
    """Broker de OKX EU con la interfaz de PaperBroker. Ver el docstring del módulo y docs/06."""

    def __init__(self, bus, store, get_market, session=None, clock=now_ms, carpeta: Path | None = None, env=None,
                 dormir=asyncio.sleep, mono=time.monotonic, get_orderflow=None):
        self.bus, self.store, self.get_market, self.session = bus, store, get_market, session
        self.get_orderflow = get_orderflow
        self.clock, self._dormir, self._mono = clock, dormir, mono
        self.carpeta = Path(carpeta or carpeta_secretos())
        self.env = env
        try:
            crear_plantilla(self.carpeta)
        except OSError as e:
            log.warning("Modo real: no pude crear la plantilla de claves: %s", e)
        self.cliente: ClienteOKX | None = None
        self.listo = False                  # comprobaciones de arranque hechas (cuenta, instrumentos, reconciliación)
        self.bloqueo: str | None = None     # lo que impide operar aunque el modo lo permita (permiso withdraw, clave)
        self._bloqueo_retirada = False      # el bloqueo es por el permiso de retirada (se quita si OKX ya no lo da)
        self.avisos: list[str] = []         # avisos de la cuenta (IP sin atar, modo de posición…)
        self.cuenta: dict = {}
        self.cuenta_id: str | None = None   # entorno + cuenta de OKX (resumen): los registros de otra se apartan
        self.instrumentos: dict[tuple[str, str], Instrumento] = {}
        self.cortos_ok, self.cortos_por_que = False, "Aún no se ha leído la cuenta"
        # monedas con X-Perps en la cuenta (aunque el bot no pueda usarlos por acctLv o posMode): solo dice si la cuenta
        # tiene X-Perps, para los cortos del perfil «cuenta» del paper (bot/comisiones.py); None = sin leer
        self.xperps: list[str] | None = None
        self.comisiones: dict[str, float | None] = {"spot": None, "perp": None}
        # comisión de creador (orden límite) de la cuenta: solo para el perfil «cuenta» del paper (bot/comisiones.py)
        self.comisiones_creador: dict[str, float | None] = {"spot": None, "perp": None}
        self.positions: dict[str, dict] = {}  # abiertas (o «enviando») de la cuenta actual
        self.closed: list[dict] = []          # cerradas de la cuenta actual
        self.apartadas: dict[str, dict] = {}  # abiertas de otra cuenta o entorno (por id): nunca se tocan
        self._cerradas_otras: list[dict] = []
        self.pending: dict[str, dict] = {}
        self.en_curso: dict[str, dict] = {}   # aceptadas por el riesgo y aún sin posición (aprobación o en cola)
        self.externas: list[dict] = []        # posiciones de OKX que el bot no conoce (cuentan en los límites)
        self.reconciliacion: dict | None = None
        self.ultimo_error: dict | None = None
        self.halted: dict | None = None
        self.halt_until = 0
        self.parada: dict | None = None     # parada de emergencia: pegajosa (memoria y base); solo la quita confirmar
        self._parada_vista = None           # la última parada por archivo ya atendida (ms, cerrar)
        self.day_base: dict = {}
        self.inicio: float | None = None
        self.aprobacion = "veto"
        self.limites = techos()
        self._cola: list[dict] = []
        self._parando = False
        self._lever: set[str] = set()
        self._t = {"vigilar": 0, "arranque": 0, "saldo": 0, "reconciliar": 0}
        self._err_at: dict[str, float] = {}
        self._pub_key = None
        store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS real_trades(id TEXT PRIMARY KEY, story TEXT, opened INTEGER, closed INTEGER,
                                                   body TEXT);
            CREATE INDEX IF NOT EXISTS real_trades_closed ON real_trades(closed);
            CREATE TABLE IF NOT EXISTS real_state(key TEXT PRIMARY KEY, body TEXT);
            """
        )
        self._load()
        self.estado = self._evaluar()
        self._preparar_cliente()
        self._encolar_parada_pendiente()

    def __repr__(self) -> str:
        return (f"RealBroker({self.estado.modo}, listo={self.listo}, posiciones={len(self.positions)}, "
                f"cliente={self.cliente!r})")

    # ---- persistencia

    def _load(self) -> None:
        """Lee los registros guardados. Todos quedan apartados hasta saber de qué cuenta son las claves (arrancar)."""
        for tid, body, closed in self.store.db.execute("SELECT id, body, closed FROM real_trades ORDER BY opened"):
            try:
                d = json.loads(body)
                if not isinstance(d, dict) or "story" not in d:
                    raise ValueError("sin story")
            except (ValueError, TypeError) as e:
                log.error("Modo real: operación guardada %s ilegible, se ignora: %s", tid, e)
                continue
            if closed is None:
                self.apartadas[d.get("id") or tid] = d
            else:
                self._cerradas_otras.append(d)
        lim = self._get_state("limites")
        self.limites, _ = aplicar_limites(lim if isinstance(lim, dict) else {}, {}, techos())
        ap = self._get_state("aprobacion")
        self.aprobacion = ap if ap in MODOS_APROBACION else "veto"
        h = self._get_state("manual_halt")
        self.halted = h if isinstance(h, dict) and _f(h.get("at")) else None
        self.halt_until = int(_f(self._get_state("halt"), 0))
        p = self._get_state("parada")
        if isinstance(p, dict) and p.get("motivo"):
            self.parada = p
            self._parada_vista = p.get("archivo")
        if self.apartadas:
            log.info("Modo real: %d posiciones guardadas; se reconcilian con OKX al arrancar", len(self.apartadas))

    def _adoptar(self) -> None:
        """Reparte los registros por cuenta: los de la cuenta actual se gestionan; los de otra cuenta, de la demo (o de
        la real estando en demo) o sin cuenta se apartan y nunca se tocan. Carga el capital de partida de la cuenta."""
        todas = {**self.apartadas, **{r.get("id") or s: r for s, r in self.positions.items()}}
        self.positions, self.apartadas = {}, {}
        for tid, rec in todas.items():
            if self.cuenta_id and rec.get("cuenta") == self.cuenta_id:
                self.positions[rec["story"]] = rec
            else:
                self.apartadas[tid] = rec
        cerradas = sorted([*self._cerradas_otras, *self.closed], key=lambda t: t.get("closed") or 0)
        self.closed = [t for t in cerradas if self.cuenta_id and t.get("cuenta") == self.cuenta_id]
        self._cerradas_otras = [t for t in cerradas if not (self.cuenta_id and t.get("cuenta") == self.cuenta_id)]
        base = self._get_state(self._clave_cuenta("day_start")) if self.cuenta_id else None
        self.day_base = base if isinstance(base, dict) and _f(base.get("equity")) is not None else {}
        self.inicio = _f(self._get_state(self._clave_cuenta("inicio"))) if self.cuenta_id else None
        if self.inicio is not None and self.inicio <= 0:
            self.inicio = None

    def _clave_cuenta(self, key: str) -> str:
        return f"{key}:{self.cuenta_id}"

    def _get_state(self, key: str):
        row = self.store.db.execute("SELECT body FROM real_state WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def _set_state(self, key: str, value) -> None:
        self.store.db.execute("INSERT OR REPLACE INTO real_state VALUES (?,?)",
                              (key, json.dumps(limpiar(value), ensure_ascii=False)))
        self.store.db.commit()

    def _guardar(self, body: dict, closed: int | None = None) -> None:
        self.store.db.execute("INSERT OR REPLACE INTO real_trades VALUES (?,?,?,?,?)",
                              (body["id"], body["story"], body.get("opened") or 0, closed,
                               json.dumps(limpiar(body), ensure_ascii=False)))
        self.store.db.commit()

    def _borrar(self, body: dict) -> None:
        """Una entrada que nunca llegó a OKX no deja rastro como operación."""
        self.store.db.execute("DELETE FROM real_trades WHERE id=? AND closed IS NULL", (body["id"],))
        self.store.db.commit()

    # ---- modo y cliente

    def _evaluar(self) -> EstadoModo:
        """evaluar_modo con la parada pegajosa: mientras haya parada, nunca demo ni real (aunque falte PARAR_REAL)."""
        est = evaluar_modo(self.carpeta, self.env)
        if self.parada and est.modo in OPERAN:
            return EstadoModo(SOLO_LECTURA, [_texto_parada(self.parada), "Solo lectura: los stops siguen puestos en OKX"],
                              est.demo, est.claves)
        return est

    def _preparar_cliente(self) -> None:
        if self.estado.modo == APAGADO:
            self.cliente = None
        else:
            self.cliente = ClienteOKX(self.session, self.estado.claves, self.estado.demo, clock=self.clock,
                                      dormir=self._dormir, mono=self._mono)
        self._ajustar_escritura()

    def puede_escribir(self) -> bool:
        """Mandar cualquier orden: entradas, mover stops, cerrar por tiempo (demo o real, con la cuenta comprobada, sin
        bloqueo y sin parada)."""
        return (self.estado.opera() and self.listo and not self.bloqueo and self.cliente is not None
                and not self.parada and self.cuenta_id is not None)

    def puede_reducir(self) -> bool:
        """Mandar solo órdenes que reducen el riesgo de lo que el bot tiene en ESTA cuenta (poner o reponer stops,
        cancelar entradas suyas, cerrar lo suyo): también en solo lectura y durante la parada, siempre que haya claves,
        la cuenta esté leída y no haya bloqueo."""
        return self.cliente is not None and self.listo and not self.bloqueo and self.cuenta_id is not None

    def puede_abrir(self) -> bool:
        """Abrir operaciones nuevas: además, sin parada de emergencia, sin parada manual y sin pausa por pérdida."""
        return (self.puede_escribir() and not self.parada and not self._parando and not self.halted
                and not self.halt_until)

    def _ajustar_escritura(self) -> None:
        if self.cliente is not None:
            self.cliente.escritura = self.puede_escribir()
            self.cliente.reducir = self.puede_reducir()

    def _por_que_no(self) -> str | None:
        """Por qué no se abre nada ahora (None si se puede)."""
        if self.parada or self._parando:
            return _texto_parada(self.parada or {})
        if not self.estado.opera():
            return "Modo real no activo: " + "; ".join(self.estado.motivos)
        if self.bloqueo:
            return self.bloqueo
        if not self.listo:
            return "Modo real: aún no se ha podido leer la cuenta de OKX" + (
                f" ({self.ultimo_error['texto']})" if self.ultimo_error else "")
        if self.halted:
            return "Modo real detenido por ti: no entra nada nuevo hasta reanudar"
        if self.halt_until:
            return "Operativa detenida por la pérdida máxima del día hasta la medianoche de Madrid"
        return None

    def _etq(self) -> str:
        return {REAL: "dinero real", DEMO: "demo de OKX", APAGADO: "apagado"}.get(self.estado.modo, "solo lectura")

    def opera(self) -> bool:
        return self.puede_abrir()

    # ---- mercado

    def _market(self) -> dict:
        try:
            return self.get_market() or {}
        except Exception as e:
            log.warning("Modo real: sin datos de mercado: %s", e)
            return {}

    def _prices(self, now: int | None = None) -> dict:
        """Precios de OKX EU con menos de 15 s (como el paper)."""
        m = self._market()
        now = self.clock() if now is None else now
        upd = m.get("updated")
        out = {}
        for c, p in (m.get("prices") or {}).items():
            p, t = _f(p), _f((upd or {}).get(c))
            if p and (upd is None or (t is not None and now - t <= paper.PRICE_MAX_AGE_MS)):
                out[c] = p
        return out

    def _spread(self, coin: str) -> float | None:
        """Diferencial en pb, como el paper: el del ticker de OKX EU y, si falta, el del libro (orderflow)."""
        sp = (self._market().get("spread") or {}).get(coin)
        if sp is None and self.get_orderflow is not None:
            try:
                sp = (((self.get_orderflow() or {}).get("coins") or {}).get(coin) or {}).get("spread_bps")
            except Exception:
                sp = None
        return _f(sp)

    def _macro_soon(self, now: int) -> dict | None:
        """Dato macro de alto impacto en los próximos 10 min (la agenda del bus), como el paper."""
        for e in ((getattr(self.bus, "state", {}) or {}).get("calendar") or {}).get("events") or []:
            if e.get("impact") == "alto" and now <= (e.get("t") or 0) <= now + paper.MACRO_PAUSE_MS:
                return e
        return None

    def _expo(self, prices: dict, excluir: str | None = None) -> tuple[float, float]:
        """(exposición, nominal en perpetuos) abiertos y comprometidos, como el paper (los perpetuos cuentan en la
        exposición por su margen, la mitad del nominal): posiciones del bot (lo que queda abierto), entradas enviando o
        en curso (su plan) y posiciones de OKX fuera del bot."""
        expo = perp = 0.0
        w = 1 / paper.PERP_MAX_LEVERAGE
        for s, r in self.positions.items():
            if s == excluir:
                continue
            if r.get("estado") == "abierta":
                n = (r.get("notional") or 0.0) * (r.get("qty") or 0.0) / (r.get("qty0") or r.get("qty") or 1.0)
            else:
                n = _f((r.get("plan") or {}).get("notional"), 0.0) or 0.0
            es_perp = r.get("instrument") == "perp"
            expo += n * (w if es_perp else 1.0)
            perp += n if es_perp else 0.0
        for s, it in self.en_curso.items():
            if s == excluir or s in self.positions:
                continue
            n, es_perp = _f(it["plan"].get("notional"), 0.0) or 0.0, it["plan"].get("inst") == "perp"
            expo += n * (w if es_perp else 1.0)
            perp += n if es_perp else 0.0
        for x in self.externas:
            ins = self.instrumentos.get((x["coin"], "perp"))
            px = prices.get(x["coin"]) or x.get("avgPx") or 0.0
            n = abs(_f(x.get("pos"), 0.0) or 0.0) * float(ins.ct_val if ins else 1) * px
            expo += n * w
            perp += n
        return expo, perp

    # ---- cuenta

    async def arrancar(self, now: int | None = None) -> bool:
        """Comprobaciones de solo lectura (docs/06, apartado 9) y reconciliación. Sin ellas no se opera."""
        now = self.clock() if now is None else now
        self._t["arranque"] = now
        if self.cliente is None:
            self.listo = False
            return False
        try:
            await self._leer_cuenta(now)
            self.listo = True
            self._ajustar_escritura()
            await self.reconciliar(now)
            self.ultimo_error = None
        except ErrorClave as e:
            self.listo = False
            self._bloqueo_clave(e)
            self._error("arranque", e, now)
        except Exception as e:
            self.listo = False
            self._error("arranque", e, now)
        self._ajustar_escritura()
        self._status()
        self.publish_cuenta(force=True)
        return self.listo

    async def _leer_cuenta(self, now: int) -> None:
        cli = self.cliente
        await cli.sincronizar_hora()
        cfg = (await cli.privado("GET", "/api/v5/account/config") or [{}])[0]
        perm = str(cfg.get("perm") or "")
        self.avisos = []
        self.bloqueo, self._bloqueo_retirada = None, False
        if PERMISO_RETIRADA in perm.lower():
            self.bloqueo = ("La clave de API tiene permiso de retirada: el bot se niega a operar. Quítale ese permiso en "
                            "OKX o crea otra clave solo con Lectura + Operar")
            self._bloqueo_retirada = True
        if not str(cfg.get("ip") or "").strip():
            self.avisos.append("La clave no está atada a la IP de tu PC (y OKX la caduca tras 14 días sin uso)")
        acct, pos_mode = str(cfg.get("acctLv") or ""), str(cfg.get("posMode") or "")
        self.cuenta.update(perm=perm, ip=bool(str(cfg.get("ip") or "").strip()), acctLv=acct, posMode=pos_mode,
                           leida=now)
        # Qué cuenta es (entorno + uid de OKX, o la huella de las claves si OKX no da el uid), solo como resumen: los
        # registros de otra cuenta o de la demo no se gestionan nunca con estas claves.
        ident = str(cfg.get("uid") or "").strip() or ("claves:" + self.estado.claves.huella())
        cid = hashlib.sha256(f"okx|{'demo' if self.estado.demo else 'real'}|{ident}".encode()).hexdigest()[:12]
        if cid != self.cuenta_id:
            self.cuenta_id = cid
            self._adoptar()
            if self.apartadas:
                self._aviso(f"{len(self.apartadas)} posiciones guardadas son de otra cuenta o de otro entorno (demo o "
                            "real): el bot no las gestiona ni las cierra con estas claves. Revísalas tú en OKX", now,
                            apartadas=[self._vista_apartada(r) for r in self.apartadas.values()])
        await self._leer_instrumentos(acct, pos_mode)
        await self._leer_comisiones()
        await self._leer_saldo(now)

    def _bloqueo_clave(self, e: ErrorOKX) -> None:
        """OKX rechaza la clave, la firma, la frase, la IP o el permiso: el bot deja de operar hasta que cambien las
        claves (o se reinicie)."""
        self.bloqueo = f"OKX rechaza la clave ({e.codigo}: {e.texto}): revisa okx.env, la IP y los permisos"
        self._bloqueo_retirada = False

    async def _revisar_permisos(self, now: int) -> None:
        """Cada 5 min (con la reconciliación): si a la clave le han dado permiso de retirada, el bot deja de operar."""
        cfg = (await self.cliente.privado("GET", "/api/v5/account/config") or [{}])[0]
        perm = str(cfg.get("perm") or "")
        self.cuenta["perm"] = perm
        retirada = PERMISO_RETIRADA in perm.lower()
        if retirada and not self.bloqueo:
            self.bloqueo = ("La clave de API tiene ahora permiso de retirada: el bot deja de operar. Quítale ese permiso "
                            "en OKX o crea otra clave solo con Lectura + Operar (los stops que ya están en OKX siguen puestos)")
            self._bloqueo_retirada = True
            log.error("Modo real: la clave tiene permiso de retirada; el bot deja de operar")
            self._aviso(self.bloqueo, now)
        elif not retirada and self._bloqueo_retirada:   # se lo has quitado: vuelve a operar
            self.bloqueo, self._bloqueo_retirada = None, False
            log.warning("Modo real: la clave ya no tiene permiso de retirada")
            self._aviso("La clave ya no tiene permiso de retirada: el bot vuelve a operar", now)
        else:
            return
        self._ajustar_escritura()
        self._status()

    async def _leer_instrumentos(self, acct: str, pos_mode: str) -> None:
        cli = self.cliente
        spot = {f.get("instId"): f for f in await cli.publico("/api/v5/public/instruments", {"instType": "SPOT"})
                if isinstance(f, dict)}
        cuenta_spot = {f.get("instId") for f in await cli.privado("GET", "/api/v5/account/instruments",
                                                                 {"instType": "SPOT"}) if isinstance(f, dict)}
        out: dict[tuple[str, str], Instrumento] = {}
        for coin in config.COINS:
            ins = instrumento_de(spot.get(spot_id(coin)), coin, "spot")
            if ins and ins.inst_id in cuenta_spot:
                out[(coin, "spot")] = ins
        porque = []
        try:
            cuenta_fut = {f.get("instId") for f in await cli.privado("GET", "/api/v5/account/instruments",
                                                                    {"instType": "FUTURES"}) if isinstance(f, dict)}
        except ErrorClave:
            raise
        except ErrorOKX as e:
            cuenta_fut = set()
            porque.append(f"OKX no da los futuros de tu cuenta ({e.codigo}: {e.texto})")
        for coin in config.COINS:
            filas = await cli.publico("/api/v5/public/instruments", {"instType": "FUTURES",
                                                                     "instFamily": xperp_familia(coin)})
            vivas = sorted((f for f in filas if isinstance(f, dict) and f.get("state") == "live"
                            and f.get("ruleType") == "xperp"), key=lambda f: _f(f.get("expTime"), 0))
            ins = instrumento_de(vivas[-1], coin, "perp") if vivas else None
            if ins and ins.inst_id in cuenta_fut:
                out[(coin, "perp")] = ins
        if acct == "1":
            porque.append("la cuenta está en modo contado (sin derivados)")
        if pos_mode and pos_mode != "net_mode":
            porque.append(f"el modo de posición es {pos_mode} (el bot necesita net_mode; cámbialo tú en la web de OKX)")
        perps = [c for c in config.COINS if (c, "perp") in out]
        self.xperps = list(perps)
        if not perps:
            porque.append("tu cuenta no tiene X-Perps disponibles (test de idoneidad y cuenta de X-Perps en OKX)")
        self.cortos_ok = bool(perps) and acct != "1" and (not pos_mode or pos_mode == "net_mode")
        if not self.cortos_ok:
            out = {k: v for k, v in out.items() if k[1] != "perp"}
        self.cortos_por_que = ("Cortos en X-Perps: " + ", ".join(perps) if self.cortos_ok
                               else "Sin cortos: " + "; ".join(porque))
        self.instrumentos = out
        if not self.cortos_ok:
            log.warning("Modo real: %s", self.cortos_por_que)

    async def _leer_comisiones(self) -> None:
        cli = self.cliente
        spot = next((c for c in config.COINS if (c, "spot") in self.instrumentos), None)
        if spot:
            filas = await cli.privado("GET", "/api/v5/account/trade-fee", {"instType": "SPOT", "instId": spot_id(spot)})
            self.comisiones["spot"] = comision_pct(filas[0] if filas else None)
            self.comisiones_creador["spot"] = comision_pct(filas[0] if filas else None, "maker")
        perp = next((c for c in config.COINS if (c, "perp") in self.instrumentos), None)
        if perp:
            filas = await cli.privado("GET", "/api/v5/account/trade-fee",
                                      {"instType": "FUTURES", "instFamily": xperp_familia(perp)})
            self.comisiones["perp"] = comision_pct(filas[0] if filas else None)
            self.comisiones_creador["perp"] = comision_pct(filas[0] if filas else None, "maker")

    async def _leer_saldo(self, now: int) -> None:
        self._t["saldo"] = now
        b = (await self.cliente.privado("GET", "/api/v5/account/balance") or [{}])[0]
        det = {d.get("ccy"): d for d in b.get("details") or [] if isinstance(d, dict)}
        usdc = det.get(config.QUOTE) or {}
        eq = _f(b.get("totalEq"))
        self.cuenta.update(equity=eq, usdc_disponible=_f(usdc.get("availBal"), 0.0) or 0.0,
                           monedas={c: round((_f((det.get(c) or {}).get("availBal"), 0.0) or 0.0)
                                             + (_f((det.get(c) or {}).get("frozenBal"), 0.0) or 0.0), 10)
                                    for c in config.COINS}, saldo_at=now)
        if eq is not None and eq > 0 and self.cuenta_id:   # con la cuenta a 0 no hay capital de partida que fijar
            if self.inicio is None:
                self.inicio = eq
                self._set_state(self._clave_cuenta("inicio"), eq)
            self._roll_day(now, eq)

    def _roll_day(self, now: int, eq: float) -> None:
        """Capital con que empieza el día de Madrid: el primero leído tras la medianoche (o el primero mayor que 0, si la
        cuenta estaba vacía), menos lo cerrado hoy antes."""
        hoy = paper._day(now).isoformat()
        if self.day_base.get("date") != hoy or not (_f(self.day_base.get("equity")) or 0) > 0:
            hecho = sum(t.get("pnl") or 0.0 for t in self.closed if (t.get("closed") or 0) >= day_start_ms(now))
            self.day_base = {"date": hoy, "equity": round(eq - hecho, 6)}
            self._set_state(self._clave_cuenta("day_start"), self.day_base)

    def equity(self, prices: dict | None = None) -> float:
        return float(_f(self.cuenta.get("equity"), 0.0) or 0.0)

    def _upnl(self, rec: dict, px: float | None) -> float:
        """Resultado de una posición abierta: lo latente de lo que queda, lo ya cerrado de sus partes y la comisión de
        entrada (0 si aún está «enviando»)."""
        if rec.get("estado") != "abierta":
            return 0.0
        d = 1 if rec["side"] == "long" else -1
        px = px or rec.get("last") or rec["entry"]
        partes = rec.get("partes") or []
        realized = sum(x.get("pnl") or 0.0 for x in partes if x.get("estado") == "cerrada")
        abierto = sum(x.get("qty") or 0.0 for x in partes if x.get("estado") != "cerrada")
        share = rec["fee_open"] * abierto / rec["qty0"] if rec.get("qty0") else 0.0
        return d * (px - rec["entry"]) * abierto - share + realized

    def pnl_bot_hoy(self, now: int, prices: dict | None = None) -> float:
        """Lo que han hecho hoy las operaciones del bot: cerradas de hoy más el resultado de las abiertas."""
        prices = self._prices(now) if prices is None else prices
        hecho = sum(t.get("pnl") or 0.0 for t in self.closed if (t.get("closed") or 0) >= day_start_ms(now))
        return hecho + sum(self._upnl(r, prices.get(r["coin"])) for r in self.positions.values())

    def day_pnl(self, now: int | None = None, prices: dict | None = None) -> tuple[float, float]:
        """Resultado del día de Madrid: el PEOR de dos medidas, la variación del saldo de OKX desde el inicio del día y
        lo que han hecho las operaciones del bot. Así un ingreso de dinero no tapa una pérdida (y sacar dinero cuenta
        como pérdida, por el lado seguro)."""
        now = self.clock() if now is None else now
        eq = _f(self.cuenta.get("equity"))
        base = _f(self.day_base.get("equity")) if self.day_base.get("date") == paper._day(now).isoformat() else None
        bot = self.pnl_bot_hoy(now, prices)
        if eq is None or not base or base <= 0:
            pnl, base = bot, (base if base and base > 0 else (eq or 0.0))
        else:
            pnl = min(eq - base, bot)
        return pnl, (pnl / base * 100 if base else 0.0)

    def _check_halt(self, now: int) -> None:
        if self.halt_until and now >= self.halt_until:
            self.halt_until = 0
            self._set_state("halt", 0)
            self._status("Nuevo día en Madrid: acaba la pausa por pérdida diaria")
        if self.halt_until:
            return
        loss = max(0.0, -self.day_pnl(now)[1])
        if loss >= self.limites["perdida_dia_pct"]:
            self.halt_until = next_midnight_ms(now)
            self._set_state("halt", self.halt_until)
            log.warning("Modo real: pérdida del día %.2f %% ≥ %.2f %%: sin operaciones nuevas hasta la medianoche",
                        loss, self.limites["perdida_dia_pct"])
            self._status(f"Pérdida del día {_n(loss)} % ≥ {_n(self.limites['perdida_dia_pct'])} %: sin operaciones "
                         "nuevas hasta la medianoche de Madrid (las abiertas siguen con su stop en OKX)")

    # ---- publicación

    def _event(self, kind: str, agent: str, story: str, coin: str | None, title: str, text: str, patch: dict,
               now: int, **extra) -> dict:
        patch = {**patch, "real": {"modo": self.estado.modo, "demo": self.estado.demo}}
        return self.bus.publish(limpiar({"type": "event", "ts": now, "kind": kind, "agent": agent, "story": story,
                                         "title": title, "text": text, "coins": [coin] if coin else [],
                                         "patch": patch, **extra}))

    def _aviso(self, text: str, now: int, story: str = "real", coin: str | None = None, **datos) -> None:
        """Evento «real_aviso»: cambios de modo, parada, reconciliación, posiciones externas, fallos de OKX."""
        self._event("real_aviso", "real", story, coin, "Cuenta real", text, {"aviso": datos}, now)

    def _status(self, text: str | None = None) -> None:
        self.bus.publish(limpiar({"type": "agent", "agent": "real",
                                  "patch": {"off": not self.estado.opera(), "now": text or self._texto_estado()}}))

    def _texto_estado(self) -> str:
        n = len(self.positions)
        why = self._por_que_no()
        base = f"{n} {'posición abierta' if n == 1 else 'posiciones abiertas'} en OKX ({self._etq()})"
        sin = [p["coin"] for p in self.positions.values() if p.get("estado") == "abierta" and self._sin_stop(p)]
        alerta = f"¡SIN STOP EN OKX: {', '.join(sin)}! · " if sin else ""
        return alerta + (f"{why} · {base}" if why else f"Operando en {self._etq()} · {base}")

    def announce(self) -> None:
        self._status()

    def _error(self, donde: str, e: Exception, now: int) -> None:
        texto = enmascarar(f"{type(e).__name__}: {e}")[:300]
        self.ultimo_error = {"at": now, "donde": donde, "texto": texto}
        self._log_err(donde, f"Modo real: fallo en {donde}: {texto}", exc=not isinstance(e, ErrorOKX))

    def _log_err(self, key: str, msg: str, exc: bool = False) -> None:
        t = time.monotonic()
        if t - self._err_at.get(key, float("-inf")) >= ERR_LOG_EVERY_S:
            self._err_at[key] = t
            (log.exception if exc else log.warning)("%s", msg)

    # ---- mandos (los mismos que el paper, más la parada, los límites y la confirmación)

    def set_mode(self, mode: str) -> bool:
        """Aprobación: veto (por defecto) o manual; «auto» solo en demo (con dinero real, nunca sin tu ventana de veto)."""
        if mode not in MODOS_APROBACION:
            return False
        if mode == "auto" and self.estado.modo != DEMO:
            self._status("La aprobación automática solo se permite en la demo de OKX: con dinero real, veto o a mano")
            return False
        self.aprobacion = mode
        self._set_state("aprobacion", mode)
        self._status()
        self.publish_cuenta(force=True)
        return True

    def aprobacion_efectiva(self) -> str:
        """La aprobación que de verdad se aplica: «auto» (elegida en la demo) es «veto» fuera de la demo."""
        return "veto" if self.aprobacion == "auto" and self.estado.modo != DEMO else self.aprobacion

    def halt(self, why: str = "Detenido por ti") -> bool:
        done = not self.halted
        if done:
            self.halted = {"at": self.clock(), "why": why}
            self._set_state("manual_halt", self.halted)
            log.warning("Modo real: detenido por ti (%s)", why)
        self._status()
        self.publish_cuenta(force=True)
        return done

    def resume(self, why: str = "Reanudado por ti") -> bool:
        done = bool(self.halted)
        if done:
            self.halted = None
            self._set_state("manual_halt", None)
            log.warning("Modo real: reanudado (%s)", why)
        self._status()
        self.publish_cuenta(force=True)
        return done

    def set_limites(self, cambios: dict) -> dict:
        """Cambia los límites desde la web: solo hacia abajo (o de vuelta hasta el techo, nunca por encima)."""
        nuevos, malos = aplicar_limites(self.limites, cambios if isinstance(cambios, dict) else {}, techos())
        if nuevos != self.limites:
            self.limites = nuevos
            self._set_state("limites", nuevos)
            log.warning("Modo real: límites cambiados desde la web: %s", nuevos)
        self.publish_cuenta(force=True)
        return {"ok": not malos, "why": "; ".join(malos) or None, "limites": dict(self.limites), "techos": techos()}

    def confirmar(self, frase: str) -> dict:
        """Confirmación escrita desde la web. Con claves reales (OKX_DEMO=0) guarda la fecha y la huella de esas claves
        y quita la parada; en demo solo quita la parada (la confirmación del modo real no se guarda con claves de demo).
        Fuera de esos dos casos no hace nada. El modo cambia en la siguiente vigilancia."""
        now = self.clock()
        claves, demo = leer_claves(self.carpeta)
        if demo == "1" and claves is not None and claves.completas():
            if not frase_ok(frase):
                ok, why = False, f"La frase no coincide: escribe exactamente «{FRASE_CONFIRMACION}»"
            else:
                try:
                    (self.carpeta / ARCHIVO_PARAR).unlink(missing_ok=True)
                except OSError as e:
                    log.error("Modo real: no pude borrar %s: %s", ARCHIVO_PARAR, e)
                ok = True
                why = ("Demo de OKX: parada quitada" if self.parada else "Demo de OKX: no hace falta confirmación")
        else:
            ok, why = confirmar_real(self.carpeta, frase, now)
        if ok:
            if self.parada:
                log.warning("Modo real: parada de emergencia quitada por tu confirmación")
            self.parada = None
            self._set_state("parada", None)
            pa = leer_parada(self.carpeta)   # si PARAR_REAL no se pudo borrar, sigue mandando (evaluar_modo)
            self._parada_vista = _clave_parada(pa) if pa else None
            self._t["vigilar"] = 0
        self.publish_cuenta(force=True)
        return {"ok": ok, "why": why}

    def parada_emergencia(self, motivo: str = "Parada de emergencia desde la web", cerrar_posiciones: bool = False) -> bool:
        """Parada de emergencia: no entra nada más, cancela las entradas pendientes (aprobación, cola y órdenes de
        entrada vivas en OKX), protege lo que esté a medias, NO cierra posiciones salvo cerrar_posiciones=True (también
        si ya había una parada sin cierre), y deja el modo en solo lectura (escribe PARAR_REAL y borra la confirmación;
        la parada queda además en memoria y en la base). Lo que toca a OKX se hace en la ronda siguiente. Devuelve True
        si ha hecho algo nuevo (la primera parada o un cierre que no se había pedido)."""
        return self._iniciar_parada(motivo, bool(cerrar_posiciones), escribir=True)

    def _iniciar_parada(self, motivo: str, cerrar: bool, escribir: bool, archivo=None) -> bool:
        now = self.clock()
        previa = self.parada
        nueva = previa is None
        cierre_nuevo = bool(cerrar) and not (previa or {}).get("cerrar")
        if escribir:
            p, escrito, borrada = parar_real(self.carpeta, motivo, cerrar or bool((previa or {}).get("cerrar")), now)
            if escrito:   # el archivo que acabamos de escribir no es una parada nueva
                archivo = _clave_parada(p)
        else:   # la ha pedido el archivo PARAR_REAL: también caduca la confirmación (para volver, confirmar otra vez)
            p = {"motivo": str(motivo)[:200], "cerrar": bool(cerrar), "ms": now, "fecha": _fecha(now)}
            escrito, borrada = True, True
            try:
                (self.carpeta / ARCHIVO_CONFIRMADO).unlink(missing_ok=True)
            except OSError as e:
                borrada = False
                log.error("Modo real: no pude borrar la confirmación tras la parada: %s", e)
        if not nueva:   # la primera se conserva (motivo y fecha); solo se suma el cierre
            p = {**previa, "cerrar": bool(previa.get("cerrar")) or bool(cerrar)}
        if archivo is not None:
            p["archivo"] = archivo
            self._parada_vista = archivo
        hecho = nueva or cierre_nuevo
        if hecho:   # hasta que se haga en OKX (si el bot cae antes, se repite al volver)
            p.pop("hecha", None)
        self.parada = p
        try:
            self._set_state("parada", p)
        except Exception as e:   # la base tampoco: queda en memoria y se avisa
            log.error("Modo real: no pude guardar la parada en la base: %s", e)
        if not escrito or not borrada:
            self._aviso("Parada de emergencia: no se pudo " + " ni ".join(
                x for x, falla in (("escribir PARAR_REAL", not escrito), ("borrar la confirmación", not borrada)) if falla)
                + ". La parada sigue igual (en memoria y en la base): el modo real no vuelve hasta que confirmes otra vez",
                now, parada=p)
        log.warning("Modo real: PARADA DE EMERGENCIA (%s)%s", motivo, " y cierre de posiciones" if cerrar else "")
        self._anular_entradas(f"Parada de emergencia: {motivo}", now)
        if hecho and self.cliente is not None:
            self._parando = True
            acc = next((a for a in self._cola if a["accion"] == "parada"), None)
            if acc:
                acc["cerrar"] = acc["cerrar"] or bool(cerrar)
            else:
                self._cola.insert(0, {"accion": "parada", "cerrar": bool(cerrar), "motivo": motivo})
        elif nueva:   # sin claves no hay nada que hacer en OKX: solo lectura ya
            self._fin_parada(motivo, now, {})
        if self.estado.opera():
            self.estado = self._evaluar()
        self._ajustar_escritura()
        self._status()
        self.publish_cuenta(force=True)
        return hecho

    def _encolar_parada_pendiente(self) -> None:
        """Una parada que aún no se ha hecho en OKX (el bot cayó antes, o no había claves ni cuenta leída) se vuelve a
        poner en cola: se hace en cuanto se pueda leer la cuenta. Así un cierre pedido no se pierde con un reinicio."""
        p = self.parada
        if (p and not p.get("hecha") and self.cliente is not None
                and not any(a["accion"] == "parada" for a in self._cola)):
            self._parando = True
            self._cola.insert(0, {"accion": "parada", "cerrar": bool(p.get("cerrar")),
                                  "motivo": p.get("motivo") or "Parada de emergencia"})

    def _anular_entradas(self, why: str, now: int) -> None:
        """Las entradas que aún no han salido (esperando aprobación o en cola) se anulan: ni la parada ni un cambio de
        claves o de entorno las deja pasar."""
        for story in list(self.pending):
            self._resolve(story, False, why, now)
        for acc in [a for a in self._cola if a["accion"] == "abrir"]:
            self._cola.remove(acc)
            self._bloquear(acc["item"], why, now)

    def _fin_parada(self, motivo: str, now: int, res: dict) -> None:
        self._parando = False
        if self.estado.modo != APAGADO:
            self.estado = EstadoModo(SOLO_LECTURA, [_texto_parada(self.parada or {"motivo": motivo}),
                                                    "Solo lectura: los stops siguen puestos en OKX"],
                                     self.estado.demo, self.estado.claves)
        self._ajustar_escritura()
        txt = f"Parada de emergencia hecha ({motivo}): solo lectura"
        if res.get("canceladas"):
            txt += f" · {res['canceladas']} entradas canceladas en OKX"
        if res.get("cerradas"):
            txt += f" · {res['cerradas']} posiciones cerradas a mercado"
        if res.get("fallidas"):
            txt += (f" · NO se pudieron cerrar todavía: {', '.join(res['fallidas'])} (el cierre se reintenta hasta que "
                    "salga; míralo también en la web de OKX)")
        if res.get("errores"):
            txt += f" · fallos con OKX: {'; '.join(res['errores'])}"
        txt += (" · Lo que siga abierto conserva su stop en OKX (si a alguna parte le falta, el panel la marca SIN STOP "
                "y se repone); el bot ya no mueve stops ni cierra por tiempo")
        self._aviso(txt, now, parada=self.parada, resultado=res)
        self._status()

    async def _ejecutar_parada(self, acc: dict) -> None:
        """Con la cuenta leída: cancela las entradas del bot vivas en OKX, resuelve las que están a medias (si se
        llenaron, les pone el stop), repone los stops que falten y, si se pide, cierra lo del bot. Todo son órdenes que
        reducen el riesgo (permitidas en solo lectura). Cuenta lo que de verdad ha salido bien."""
        now = self.clock()
        res = {"canceladas": 0, "cerradas": 0, "fallidas": [], "errores": []}
        if self.puede_reducir():
            try:
                for o in await self.cliente.privado("GET", "/api/v5/trade/orders-pending"):
                    cl = str(o.get("clOrdId") or "")
                    if cl.startswith(PREFIJO_CL) and cl.endswith("e") and o.get("state") in ("live", "partially_filled"):
                        try:
                            await self.cliente.privado("POST", "/api/v5/trade/cancel-order",
                                                       cuerpo={"instId": o.get("instId"), "ordId": o.get("ordId")})
                            res["canceladas"] += 1
                        except ErrorOKX as e:
                            res["errores"].append(f"cancelar {o.get('instId')}: {e.codigo}")
            except ErrorOKX as e:
                res["errores"].append(f"órdenes pendientes: {e.codigo}")
                self._error("parada", e, now)
            for rec in [r for r in self.positions.values() if r.get("estado") == "enviando"]:
                await self._guard(rec["story"], self._resolver_enviando, rec, now)
            for rec in [r for r in self.positions.values() if r.get("estado") == "abierta" and self._sin_stop(r)]:
                await self._guard(rec["story"], self._reproteger, rec, now)
            if acc.get("cerrar"):
                why = "Parada de emergencia: cerrada a mercado por petición tuya"
                for rec in [r for r in self.positions.values() if r.get("estado") == "abierta"]:
                    rec.update(cierre_pedido=why, cierre_at=0, cierre_n=0)   # si no sale, se reintenta (solo reduce)
                    await self._guard(rec["story"], self._intentar_cierre, rec, why, now)
                    if rec["story"] in self.positions:
                        res["fallidas"].append(rec["coin"])
                    else:
                        res["cerradas"] += 1
                for rec in [r for r in self.positions.values() if r.get("estado") == "enviando"]:
                    rec.update(cierre_pedido=why, cierre_at=0, cierre_n=0)   # en cuanto conste llena: stop y cierre
                    self._guardar(rec)
                    res["fallidas"].append(f"{rec['coin']} (entrada sin confirmar)")
        else:
            res["errores"].append(self.bloqueo or "sin poder escribir en OKX")
        log.warning("Modo real: parada hecha: %d entradas canceladas, %d posiciones cerradas, %d sin cerrar",
                    res["canceladas"], res["cerradas"], len(res["fallidas"]))
        if self.parada is not None and self.puede_reducir() and (acc.get("cerrar") or not self.parada.get("cerrar")):
            self.parada["hecha"] = now   # los cierres que no salieron siguen pedidos en su posición (cierre_pedido)
            try:
                self._set_state("parada", self.parada)
            except Exception as e:
                log.error("Modo real: no pude guardar la parada en la base: %s", e)
        self._fin_parada(acc.get("motivo") or "Parada de emergencia", self.clock(), res)

    # ---- entrada: riesgo y aprobación

    def _coste(self, inst: str) -> float:
        fee = self.comisiones.get(inst)
        return coste_rt_pct(fee if fee is not None else COMISION_PRUDENTE_PCT)

    def entradas_hoy(self, now: int, excluir: str | None = None) -> int:
        start = day_start_ms(now)
        n = sum(1 for p in self.positions.values() if (p.get("opened") or 0) >= start and p["story"] != excluir)
        n += sum(1 for t in self.closed if (t.get("opened") or 0) >= start and t.get("story") != excluir)
        return n + sum(1 for s in self.en_curso if s != excluir and s not in self.positions)

    def _ocupadas(self, excluir: str | None = None) -> int:
        return (len([s for s in self.positions if s != excluir]) + len(self.externas)
                + len([s for s in self.en_curso if s != excluir and s not in self.positions]))

    def _moneda_ocupada(self, coin: str, excluir: str | None = None) -> str | None:
        for s, p in self.positions.items():
            if s != excluir and p.get("coin") == coin:
                return s
        for s, it in self.en_curso.items():
            if s != excluir and it["ticket"].get("coin") == coin:
                return s
        for x in self.externas:
            if x.get("coin") == coin:
                return f"posición en OKX fuera del bot ({x.get('instId')})"
        return None

    def _invalido(self, t: dict, story: str, risk: float, stop: float, propio: bool) -> tuple[str | None, dict]:
        inst = instrument_of(t)
        sal = salidas_reales(t, self._coste(inst))
        if t.get("none"):
            return "El estratega no propone operación", sal
        if t.get("coin") not in config.COINS:
            return f"Moneda no operable: {t.get('coin')}", sal
        if t.get("side") not in ("long", "short"):
            return f"Lado desconocido: {t.get('side')}", sal
        if t.get("instrument") not in (None, *paper.INSTRUMENTS):
            return f"Instrumento desconocido: {t.get('instrument')} (spot o perp)", sal
        if t.get("side") == "short" and inst == "spot":
            return "Corto al contado: no se puede en OKX EU (USDC); los cortos van en X-Perps", sal
        if not 0 < stop <= 20:
            return f"Stop no válido: {_n(stop)} %", sal
        if risk <= 0:
            return "Riesgo por operación no válido", sal
        if stop < config.RULE_MIN_STOP_PCT - 1e-9:
            return f"Regla del usuario: stop de {_n(stop)} % por debajo del mínimo de {_n(config.RULE_MIN_STOP_PCT)} %", sal
        rr, coste = sal["tp2R"], self._coste(inst)
        if rr and rr < config.RULE_MIN_RR - 1e-9:
            return f"Regla del usuario: objetivo de {_n(rr)} R por debajo del mínimo de {_n(config.RULE_MIN_RR)} R", sal
        if rr and not regla_ok(rr, stop, coste):
            return (f"Regla del usuario: el objetivo de {_n(rr)} R con stop de {_n(stop)} % deja {_n(rr * stop - coste)} % "
                    f"neto con la comisión real (mínimo {_n(config.RULE_MIN_NET_PCT, 0)} %)"), sal
        if not rr and not sal["lock_r"]:
            return (f"Regla del usuario: sin objetivo fijo ni cerrojo válido (hace falta al menos "
                    f"{_n(config.RULE_MIN_RR, 0)}R y {_n(config.RULE_MIN_NET_PCT, 0)} % neto)"), sal
        if t.get("timing") != "now":
            return "Modo real: solo entradas inmediatas (las programadas siguen en paper)", sal
        if not propio and (story in self.positions or story in self.en_curso):
            return "Ya hay una operación en curso para esta noticia", sal
        return None, sal

    def _dimensionar(self, t: dict, ins: Instrumento, px: float, fee_pct: float) -> tuple[Decimal | None, float, str | None]:
        """sz de la orden: tamaño por riesgo con la comisión real, con el tope de importe, el 40 % del capital y el saldo
        disponible (al contado, con la reserva del 5 % de OKX; en X-Perps, el margen a 2x). Hacia abajo."""
        eq = self.equity()
        disp = float(_f(self.cuenta.get("usdc_disponible"), 0.0) or 0.0)
        if eq <= 0:
            return None, 0.0, "Saldo 0 en la cuenta de trading de OKX: no hay con qué operar"
        d = 1 if t.get("side") == "long" else -1
        stop, risk = _f(t.get("stopPct"), 0.0), _f(t.get("risk"), 0.0)
        k = (1 - d * stop / 100) * (1 - d * 2 * paper.COST_SLIP_BPS / 10000)
        perdida = abs(1 - k) + fee_pct / 100 * (1 + k)
        nominal = min(eq * risk / 100 / perdida if perdida > 0 else 0.0, self.limites["max_usdc"],
                      eq * MAX_NOTIONAL_PCT / 100)
        if ins.inst == "spot":
            nominal = min(nominal, disp / (1 + RESERVA_MERCADO + fee_pct / 100))
        else:
            nominal = min(nominal, disp * APALANCAMIENTO * 0.9)
        sz = ins.sz_de_qty(nominal / px) if px > 0 else Decimal(0)
        # al contado, la comisión de compra sale de lo comprado: hace falta un poco más del mínimo
        minimo = ins.min_sz * (Decimal("1.01") if ins.inst == "spot" else Decimal(1))
        if sz <= 0 or sz < minimo:
            return None, nominal, (f"Por debajo del mínimo de OKX en {ins.inst_id}: {_n(nominal)} {config.QUOTE} dan "
                                   f"{_s(sz)} {ins.unidad()} y el mínimo es {_s(ins.min_sz)} (≈ "
                                   f"{_n(float(ins.qty_de_sz(ins.min_sz)) * px)} {config.QUOTE})")
        return sz, float(ins.qty_de_sz(sz)) * px, None

    def _controles(self, t: dict, story: str, now: int, propio: bool = False) -> tuple[list[dict], dict | None]:
        coin, inst = t.get("coin"), instrument_of(t)
        risk, stop = _f(t.get("risk"), 0.0), _f(t.get("stopPct"), 0.0)
        bad, sal = self._invalido(t, story, risk, stop, propio)
        lim = self.limites
        checks = []
        estado = self._por_que_no()
        if estado:
            checks.append({"ok": False, "t": estado})
        lado = "largo" if t.get("side") == "long" else "corto"
        checks.append({"ok": not bad, "t": bad or f"Ticket válido: {coin} en {lado} con stop de {_n(stop)} % "
                                                  "(reglas del usuario, siempre en el modo real)"})
        max_risk = config.RISK_PER_TRADE_PCT
        checks.append({"ok": risk <= max_risk, "t": f"Riesgo por operación {_n(risk)} % "
                       + (f"≤ {_n(max_risk)} % del capital" if risk <= max_risk else f"> {_n(max_risk)} % permitido")})
        loss = max(0.0, -self.day_pnl(now)[1])
        loss_ok = not self.halt_until and loss < lim["perdida_dia_pct"]
        checks.append({"ok": loss_ok, "t": f"Pérdida del día {_n(loss)} % {'<' if loss_ok else '≥'} "
                                           f"{_n(lim['perdida_dia_pct'])} % como máximo (saldo de OKX)"})
        n = self._ocupadas(story if propio else None)
        checks.append({"ok": n < lim["max_posiciones"], "t": f"Posiciones abiertas o en curso {n} de {lim['max_posiciones']}"
                       + (" como máximo" if n < lim["max_posiciones"] else ": no caben más")})
        e = self.entradas_hoy(now, story if propio else None)
        checks.append({"ok": e < lim["max_entradas_dia"],
                       "t": f"Tope diario de entradas: {e} de {lim['max_entradas_dia']} hoy"
                            + ("" if e < lim["max_entradas_dia"] else ": no entra ninguna más hasta mañana")})
        same = self._moneda_ocupada(coin, story if propio else None)
        checks.append({"ok": same is None, "t": f"Sin otra operación abierta o en curso en {coin}" if same is None
                       else f"Ya hay una operación en {coin} ({same}): una sola por moneda"})
        ins = self.instrumentos.get((coin, inst))
        if inst == "perp" and not self.cortos_ok:
            checks.append({"ok": False, "t": self.cortos_por_que})
        elif ins is None:
            checks.append({"ok": False, "t": f"{coin} {'al contado' if inst == 'spot' else 'en X-Perps'} no está "
                                             "disponible para tu cuenta de OKX"})
        prices = self._prices(now)
        px = prices.get(coin)
        checks.append({"ok": px is not None, "t": f"Precio de {coin} en OKX EU: {_px(px)}" if px
                       else f"Sin precio actualizado de {coin} en OKX EU"})
        sp = self._spread(coin)   # los mismos controles que el paper: diferencial y pausa macro
        if sp is None:
            checks.append({"ok": True, "t": f"Diferencial de {coin}: sin dato ahora"})
        else:
            ok = sp <= paper.MAX_SPREAD_BPS
            checks.append({"ok": ok, "t": f"Diferencial de {coin}: {_n(sp, 1)} pb {'≤' if ok else '>'} "
                                          f"{_n(paper.MAX_SPREAD_BPS, 0)} pb"})
        macro = self._macro_soon(now)
        checks.append({"ok": macro is None, "t": "Sin dato macro de alto impacto en los próximos 10 min" if macro is None
                       else f"Dato macro de alto impacto en {max(0, round((macro['t'] - now) / 60_000))} min: "
                            f"{macro.get('name', '')}"})
        fee = self.comisiones.get(inst)
        checks.append({"ok": fee is not None, "t": f"Comisión real de tu cuenta: {_n(fee, 3)} % por lado" if fee is not None
                       else "Sin la comisión real de tu cuenta (account/trade-fee): no se opera a ciegas"})
        plan = None
        if not bad and ins is not None and px and fee is not None and (inst != "perp" or self.cortos_ok):
            sz, nominal, why = self._dimensionar(t, ins, px, fee)
            checks.append({"ok": why is None, "t": why or (f"Importe {_n(nominal)} {config.QUOTE} ≤ {_n(lim['max_usdc'])} "
                                                           f"{config.QUOTE} por operación ({_s(sz)} {ins.unidad()})")})
            if why is None:
                plan = {"sz": _s(sz), "notional": round(nominal, 4), "ref": px, "inst": inst, "inst_id": ins.inst_id,
                        "salidas": sal, "fee_pct": fee}
                checks += self._checks_exposicion(inst, nominal, prices, story if propio else None)
        return checks, plan

    def _checks_exposicion(self, inst: str, nominal: float, prices: dict, excluir: str | None) -> list[dict]:
        """Exposición total ≤ 100 % del capital y nominal en perpetuos ≤ 2 × capital, con los textos del paper (el
        motivo_codigo sale «exposicion»)."""
        eq = self.equity()
        expo, perp = self._expo(prices, excluir)
        es_perp = inst == "perp"
        expo += nominal * (1 / paper.PERP_MAX_LEVERAGE if es_perp else 1.0)
        expo_pct = expo / eq * 100 if eq else 0.0
        ok = eq > 0 and expo <= eq * paper.MAX_EXPOSURE_PCT / 100 + 1e-6
        dec = 1 if round(expo_pct) == round(paper.MAX_EXPOSURE_PCT) else 0
        out = [{"ok": ok, "t": f"Exposición total {_n(expo_pct, dec)} % {'≤' if ok else '>'} "
                               f"{_n(paper.MAX_EXPOSURE_PCT, 0)} % del capital"
                               + (" (los perpetuos, por su margen: la mitad del nominal)" if es_perp or perp else "")}]
        if es_perp:
            perp += nominal
            ok = eq > 0 and perp <= eq * paper.PERP_MAX_LEVERAGE + 1e-6
            out.append({"ok": ok, "t": f"Nominal en perpetuos {_n(perp / eq * 100 if eq else 0.0, 0)} % "
                                       f"{'≤' if ok else '>'} {_n(paper.PERP_MAX_LEVERAGE * 100, 0)} % del capital (2:1, "
                                       "máximo para particulares en España)"})
        return out

    def submit(self, ticket: dict, story: str, headline: str = "", strategy: str = "", window: str = "") -> bool:
        """Recibe un ticket (formato patch.strat). True si pasa los controles; la orden sale en la ronda siguiente,
        tras la aprobación (veto o a mano; automática solo en demo)."""
        now = self.clock()
        t = dict(ticket or {})
        coin, side = t.get("coin"), t.get("side")
        window = window or t.get("win") or ""
        strategy = strategy or t.get("mode") or ""
        if side == "short" and t.get("instrument") is None:
            t["instrument"] = "perp"   # los cortos van en X-Perps
        checks, plan = self._controles(t, story, now)
        title = f"{_verb(side)} {coin} · {t.get('mode') or strategy} ({self._etq()})"
        bad = next((c for c in checks if not c["ok"]), None)
        if bad or plan is None:
            why = bad["t"] if bad else "Sin plan de orden"
            self._event("riskko", "risk", story, coin, title, f"Vetada: {why}",
                        {"risk": {"checks": checks, "ok": False, "why": why, "motivo_codigo": motivo_real(why)}}, now)
            log.info("Modo real: %s vetada: %s", story, why)
            return False
        self._event("riskok", "risk", story, coin, title, f"{len(checks)} de {len(checks)} controles superados",
                    {"risk": {"checks": checks, "ok": True}}, now)
        item = {"story": story, "ticket": t, "headline": headline, "strategy": strategy, "window": window,
                "market": t.get("market") or market_of(window), "submitted": now, "plan": plan,
                "cuenta": self.cuenta_id}   # aprobada para ESTA cuenta y entorno: en otra no sale nunca
        self.en_curso[story] = item
        if self.aprobacion == "auto" and self.estado.modo == DEMO:
            self._event("humanok", "human", story, coin, title, "Aprobación automática (solo en la demo de OKX)",
                        {"human": {"mode": "auto", "by": "Automático (demo)", "at": now}}, now)
            self._cola.append({"accion": "abrir", "item": item})
        else:
            mode = "manual" if self.aprobacion == "manual" else "veto"
            wait = config.VETO_SECONDS * 1000 if mode == "veto" else paper.MANUAL_MS
            item.update(mode=mode, deadline=now + wait)
            self.pending[story] = item
            text = (f"Orden con {self._etq()}: se aprobará sola en {config.VETO_SECONDS} s si no la vetas" if mode == "veto"
                    else f"Orden con {self._etq()}: esperando tu decisión (caduca a las {_hm(now + wait)})")
            self._event("humanwait", "human", story, coin, title, text, {"human": {"mode": mode, "deadline": now + wait}},
                        now)
        self.publish_cuenta(force=True)
        return True

    def decide(self, story: str, ok: bool, by: str | None = None) -> bool:
        done = self._resolve(story, bool(ok), by or ("Aprobada por ti" if ok else "Rechazada por ti"), self.clock())
        if done:
            self.publish_cuenta(force=True)
        return done

    def _resolve(self, story: str, ok: bool, by: str, now: int) -> bool:
        item = self.pending.pop(story, None)
        if item is None:
            return False
        t = item["ticket"]
        title = f"{_verb(t['side'])} {t['coin']} · {t.get('mode') or item['strategy']} ({self._etq()})"
        human = {"mode": item.get("mode"), "by": by, "at": now}
        if ok:
            self._event("humanok", "human", story, t["coin"], title, by, {"human": human}, now)
            self._cola.append({"accion": "abrir", "item": item})
        else:
            self.en_curso.pop(story, None)
            self._event("humanko", "human", story, t["coin"], title, by,
                        {"human": {**human, "no": True, "motivo_codigo": "aprobacion"}}, now)
        return True

    def cancel(self, story: str, why: str = "Cancelada por ti") -> bool:
        now = self.clock()
        if story in self.pending:
            done = self._resolve(story, False, why, now)
        else:
            acc = next((a for a in self._cola if a["accion"] == "abrir" and a["item"]["story"] == story), None)
            if acc:
                self._cola.remove(acc)
                self._bloquear(acc["item"], why, now)
            done = acc is not None
        self.publish_cuenta(force=True)
        return done

    def close(self, story: str, why: str = "Cierre manual") -> bool:
        """Cierra a mercado una posición abierta (desde la ronda siguiente y, si no sale, se reintenta con espera
        creciente hasta que salga). False si no está abierta o no se puede escribir en OKX."""
        p = self.positions.get(story)
        if not p or p.get("estado") != "abierta" or not self.puede_escribir():
            return False
        if not p.get("cierre_pedido"):
            p.update(cierre_pedido=str(why or "Cierre manual")[:200], cierre_at=0, cierre_n=0)
            self._guardar(p)
        return True

    def _bloquear(self, item: dict, why: str, now: int) -> None:
        self.en_curso.pop(item["story"], None)
        t = item["ticket"]
        self._event("blocked", "exec", item["story"], t["coin"], f"{_verb(t['side'])} {t['coin']} ({self._etq()})", why,
                    {"exec": {"blocked": True, "why": why, "motivo_codigo": motivo_real(why, "blocked")}}, now)
        log.info("Modo real: %s bloqueada: %s", item["story"], why)

    def _step_pending(self, now: int) -> None:
        for story, it in list(self.pending.items()):
            if now < it["deadline"]:
                continue
            if it["mode"] == "veto" and self.aprobacion != "manual" and self.puede_abrir():
                self._resolve(story, True, f"Sin veto en {config.VETO_SECONDS} s: aprobada", now)
            elif it["mode"] == "veto" and not self.puede_abrir():
                self._resolve(story, False, f"No se aprueba sola: {self._por_que_no()}", now)
            else:
                self._resolve(story, False, "Caducada sin respuesta", now)

    # ---- ejecución

    def _ins(self, rec: dict) -> Instrumento | None:
        ins = self.instrumentos.get((rec["coin"], rec["instrument"]))
        if ins and ins.inst_id == rec.get("inst_id"):
            return ins
        f = rec.get("ins")   # el de cuando se abrió (el instId de un X-Perp lleva su vencimiento)
        if isinstance(f, dict):
            return Instrumento(rec["coin"], rec["instrument"], f["instId"], "SPOT" if rec["instrument"] == "spot"
                               else "FUTURES", _d(f["lotSz"]), _d(f["minSz"]), _d(f["tickSz"]), _d(f["ctVal"]))
        return ins

    async def _abrir(self, item: dict) -> None:
        now = self.clock()
        story, t = item["story"], item["ticket"]
        coin, side = t["coin"], t["side"]
        age, max_age = now - int(item["submitted"]), _f(t.get("maxAgeMs"))
        if max_age and age > max_age:
            return self._bloquear(item, f"Aprobada {_ago(age)} después de enviarla (máximo {_ago(max_age)}): la señal "
                                        "y el plan de salidas ya son viejos", now)
        if not item.get("cuenta") or item.get("cuenta") != self.cuenta_id:
            return self._bloquear(item, "La orden se pidió con otras claves o en otro entorno (demo o real): no se "
                                        "manda en esta cuenta", now)
        checks, plan = self._controles(t, story, now, propio=True)
        bad = next((c for c in checks if not c["ok"]), None)
        if bad or plan is None:
            return self._bloquear(item, bad["t"] if bad else "Sin plan de orden", now)
        ins = self.instrumentos[(coin, plan["inst"])]
        d = 1 if side == "long" else -1
        base = _cl_base(story, item["submitted"])
        rec = {"id": "r" + hashlib.sha1(f"{story}|{item['submitted']}".encode()).hexdigest()[:12], "story": story,
               "coin": coin, "side": side, "instrument": ins.inst, "inst_id": ins.inst_id, "ins": ins.vista(),
               "estado": "enviando", "cl": base, "clOrdId": base + "e", "submitted": item["submitted"], "opened": now,
               "exp_ms": self.cliente.hora_okx() + EXP_ORDEN_MS,
               "stop_pct": _f(t["stopPct"]), "salidas": plan["salidas"], "headline": item["headline"],
               "strategy": item["strategy"], "window": item["window"], "market": item["market"],
               "hold": self._hold(t, now), "hold_active_t": _f(t.get("winEndActive")),
               "hold_active_label_t": t.get("holdActiveLabel"), "tags": t.get("tags") if isinstance(t.get("tags"), dict)
               else None, "modo": self.estado.modo, "entorno": "demo" if self.estado.demo else "real",
               "cuenta": self.cuenta_id, "plan": {"sz": plan["sz"], "ref": plan["ref"], "notional": plan["notional"]}}
        self.en_curso.pop(story, None)
        self.positions[story] = rec
        self._guardar(rec)
        self._event("order", "exec", story, coin, f"{_verb(side)} {plan['sz']} {ins.unidad()} de {ins.inst_id} "
                    f"({self._etq()})", f"A mercado · referencia {_px(plan['ref'])} {config.QUOTE} · importe ≈ "
                    f"{_n(plan['notional'])} {config.QUOTE} · stop y objetivo se ponen en OKX al llenarse",
                    {"exec": {"sent": now, "notional": plan["notional"], "sz": plan["sz"], "instId": ins.inst_id,
                              "clOrdId": rec["clOrdId"]}}, now)
        try:
            if ins.inst == "perp":
                await self._apalancamiento(ins)
                rec["exp_ms"] = self.cliente.hora_okx() + EXP_ORDEN_MS   # expTime cuenta desde el envío, no antes
                self._guardar(rec)
            rec["ordId"] = await self._enviar_entrada(rec, ins, d, plan["sz"])
        except ErrorOKX as e:   # OKX la rechazó o seguro que no llegó: no hay nada que proteger
            self.positions.pop(story, None)
            self._borrar(rec)
            if isinstance(e, ErrorClave):
                self._bloqueo_clave(e)
                self._ajustar_escritura()
            return self._bloquear(item, f"OKX rechazó la entrada ({e.codigo}: {e.texto})", now)
        self._guardar(rec)
        if not rec.get("ordId"):   # en duda: ni se borra ni se repite; se consulta cada ronda hasta saberlo
            self._aviso(f"{coin}: no se sabe aún si la entrada llegó a OKX (corte o OKX ocupado). No se repite: se "
                        f"consulta por su clOrdId en cada ronda y, si se llenó, se le pone el stop en cuanto conste",
                        now, story, coin)
            return
        try:
            fila = await self._esperar_llenado(ins, rec["ordId"], rec["clOrdId"])
            if fila and fila.get("state") in ("live", "partially_filled"):   # no se llena del todo: se cancela el resto
                await self._cancelar_entrada(ins, fila)
                fila = await self._orden(ins, ord_id=rec["ordId"])
        except ErrorOKX as e:   # la orden está en OKX y no se sabe cómo quedó: el registro se queda y se resuelve ya
            fila = None
            self._error("llenado", e, now)
        if fila is None or fila.get("state") not in ("filled", "canceled", "mmp_canceled"):
            self._aviso(f"{coin}: la entrada está en OKX pero aún no se sabe cómo ha quedado; se consulta en cada ronda "
                        "(se le pondrá el stop en cuanto conste)", now, story, coin)
            return
        if _d(fila.get("accFillSz")) <= 0:
            self.positions.pop(story, None)
            self._borrar(rec)
            return self._bloquear(item, "OKX canceló la orden de entrada a mercado sin llenarla", now)
        await self._proteger(rec, fila, self.clock())

    async def _apalancamiento(self, ins: Instrumento) -> None:
        if ins.inst_id in self._lever:
            return
        await self.cliente.privado("POST", "/api/v5/account/set-leverage",
                                   cuerpo={"instId": ins.inst_id, "lever": str(APALANCAMIENTO), "mgnMode": TD_PERP})
        self._lever.add(ins.inst_id)

    async def _enviar_entrada(self, rec: dict, ins: Instrumento, d: int, sz: str) -> str | None:
        """Orden de entrada a mercado con clOrdId y expTime. Devuelve su ordId, o None si queda en duda. Tras un fallo
        de red o con OKX ocupado no se repite a ciegas: se consulta por clOrdId hasta pasado expTime; solo si entonces
        OKX no la tiene se da por no enviada (ErrorOKX). Si tampoco se puede consultar, queda en duda («enviando»)."""
        cuerpo = {"instId": ins.inst_id, "side": "buy" if d > 0 else "sell", "ordType": "market", "sz": sz,
                  "clOrdId": rec["clOrdId"]}
        if ins.inst == "spot":
            cuerpo.update(tdMode="cash", tgtCcy="base_ccy", tradeQuoteCcy=config.QUOTE, banAmend=True)
        else:
            cuerpo.update(tdMode=TD_PERP)
        try:
            res = await self.cliente.privado("POST", "/api/v5/trade/order", cuerpo=cuerpo, exp_ms=rec["exp_ms"])
            return (res[0] if res else {}).get("ordId")
        except ErrorOKX as e:
            if not e.ambiguo:
                raise
            fila, seguro = await self._consultar_tras_duda(ins, rec["clOrdId"], rec["exp_ms"])
            if fila is not None:
                return fila.get("ordId")
            if seguro:
                raise ErrorOKX(e.codigo, f"no llegó a OKX ({e.texto})")
            return None

    async def _consultar_tras_duda(self, ins: Instrumento, cl: str, exp_ms: int) -> tuple[dict | None, bool]:
        """Tras una orden en duda: (fila, seguro). Consulta por clOrdId cada segundo hasta pasado expTime + margen. Si
        aparece, (fila, True); si OKX dice que no existe pasado ese plazo, (None, True): no llegó y ya no llegará. Si no
        se puede consultar, (None, False): sigue en duda."""
        espera_s = max(0.0, (exp_ms + MARGEN_EXP_MS - self.cliente.hora_okx()) / 1000)
        limite = self._mono() + espera_s
        while True:
            try:
                fila = await self._orden(ins, cl=cl)
                if fila is not None:
                    return fila, True
                if self._mono() >= limite:
                    return None, True
            except ErrorOKX as e:
                self._error("consulta", e, self.clock())
                if self._mono() >= limite + DUDA_EXTRA_S:
                    return None, False
            await self._dormir(DUDA_ESPERA_S)

    async def _cancelar_entrada(self, ins: Instrumento, fila: dict) -> None:
        try:
            await self.cliente.privado("POST", "/api/v5/trade/cancel-order",
                                       cuerpo={"instId": ins.inst_id, "ordId": fila.get("ordId")})
        except ErrorOKX as e:
            if e.codigo not in NO_EXISTE:
                raise

    async def _orden(self, ins: Instrumento, ord_id: str | None = None, cl: str | None = None) -> dict | None:
        params = {"instId": ins.inst_id, **({"ordId": ord_id} if ord_id else {"clOrdId": cl})}
        try:
            d = await self.cliente.privado("GET", "/api/v5/trade/order", params)
        except ErrorOKX as e:
            if e.codigo in NO_EXISTE:
                return None
            raise
        return d[0] if d else None

    async def _esperar_llenado(self, ins: Instrumento, ord_id: str | None, cl: str) -> dict | None:
        fila = None
        for _ in range(LLENADO_INTENTOS):
            fila = await self._orden(ins, ord_id=ord_id, cl=None if ord_id else cl)
            if fila and fila.get("state") in ("filled", "canceled", "mmp_canceled"):
                return fila
            await self._dormir(LLENADO_ESPERA_S)
        return fila

    def _descartar(self, rec: dict, why: str, now: int) -> None:
        """Una entrada que nunca llegó a llenarse: no deja rastro como operación (evento «blocked», como el paper)."""
        self.positions.pop(rec["story"], None)
        self._borrar(rec)
        self._event("blocked", "exec", rec["story"], rec["coin"], f"{_verb(rec['side'])} {rec['coin']} ({self._etq()})",
                    why, {"exec": {"blocked": True, "why": why, "motivo_codigo": motivo_real(why, "blocked")}}, now)
        log.info("Modo real: %s descartada: %s", rec["story"], why)

    async def _resolver_enviando(self, rec: dict, now: int) -> None:
        """Una entrada «enviando» (en duda, sin llenar a la vista o a medias tras una caída): se consulta en OKX y, si
        está llena (o parcial), se le ponen stop y objetivo; si sigue viva demasiado tiempo, se cancela el resto; solo se
        descarta si OKX la canceló sin llenar o si, pasado su expTime, OKX no la tiene y nunca dijo que la aceptaba."""
        if not self.puede_reducir():
            return
        ins = self._ins(rec)
        if ins is None:
            return
        try:
            o = await self._orden(ins, ord_id=rec.get("ordId"), cl=rec["clOrdId"])
        except ErrorOKX as e:
            self._error(rec["story"], e, now)
            return
        if o is None:
            if rec.get("ordId"):   # OKX la aceptó: no se olvida aunque ahora no la encuentre
                if not rec.get("aviso_no_encuentra"):
                    rec["aviso_no_encuentra"] = now
                    self._guardar(rec)
                    self._aviso(f"{rec['coin']}: OKX aceptó la entrada {rec['ordId']} y ahora no la encuentra; sigue "
                                "contando en los límites. Revísala en la web de OKX", now, rec["story"], rec["coin"])
            elif self.cliente.hora_okx() > _f(rec.get("exp_ms"), 0) + MARGEN_EXP_MS:
                self._descartar(rec, "La entrada no llegó a OKX (comprobado por su clOrdId pasado su expTime)", now)
            return
        rec["ordId"] = o.get("ordId") or rec.get("ordId")
        st = o.get("state")
        if st in ("live", "partially_filled"):
            if _d(o.get("accFillSz")) <= 0 and now - (rec.get("opened") or now) < ENTRADA_VIVA_MAX_MS and not self._parando:
                return self._guardar(rec)
            try:
                await self._cancelar_entrada(ins, o)
                o = await self._orden(ins, ord_id=rec["ordId"]) or o
            except ErrorOKX as e:
                self._error(rec["story"], e, now)
                return
            st = o.get("state")
            if st in ("live", "partially_filled"):
                return self._guardar(rec)
        if _d(o.get("accFillSz")) > 0:
            self._aviso(f"{rec['coin']}: la entrada se llenó sin que el bot lo viera a tiempo; se le ponen stop y "
                        "objetivo en OKX", now, rec["story"], rec["coin"])
            await self._proteger(rec, o, now)
        elif st in ("canceled", "mmp_canceled"):
            self._descartar(rec, "OKX canceló la orden de entrada a mercado sin llenarla", now)

    def _hold(self, t: dict, now: int) -> dict:
        end = _f(t.get("winEnd"))
        if end and end > now:
            return {"until": int(end), "label": str(t.get("holdLabel") or "fin de la ventana")}
        mins = _f(t.get("holdMin"))
        if mins and mins > 0:
            return {"until": now + int(mins * 60_000), "label": f"{_n(mins, 0)} min"}
        return {"until": now + paper.HOLD_MS, "label": f"{paper.HOLD_MS // 3600_000} h"}

    def _partes(self, ins: Instrumento, sz_total: Decimal, sal: dict) -> tuple[list[dict], str | None]:
        """Partes de la posición, cada una con su algo en OKX: la parcial (OCO stop + tp1) y el resto (OCO con tp2 o
        solo stop si sigue al cerrojo). Si alguna mitad no llega al mínimo de OKX, va entera."""
        resto_tp = "tp2" if sal["tp2R"] else None
        if sal["tp1_frac"]:
            sz1 = redondear_abajo(sz_total * _d(sal["tp1_frac"]), ins.lot)
            sz2 = sz_total - sz1
            if sz1 >= ins.min_sz and sz2 >= ins.min_sz:
                return [{"n": 1, "sz": _s(sz1), "tp_kind": "tp1"}, {"n": 2, "sz": _s(sz2), "tp_kind": resto_tp}], None
            nota = (f"sin salida parcial: {_s(sz_total)} {ins.unidad()} no se pueden partir en dos de al menos "
                    f"{_s(ins.min_sz)} (mínimo de OKX); toda la posición "
                    + ("va al objetivo" if resto_tp else "sigue al cerrojo"))
            return [{"n": 1, "sz": _s(sz_total), "tp_kind": resto_tp}], nota
        return [{"n": 1, "sz": _s(sz_total), "tp_kind": resto_tp}], None

    async def _proteger(self, rec: dict, fila: dict, now: int) -> None:
        """Con la entrada llena: niveles desde el precio medio real, partes y sus algo en OKX. Si no se pueden poner,
        la posición se cierra al momento; si tampoco se puede cerrar, queda marcada SIN STOP (aviso fuerte) y se
        reintenta poner el stop o cerrar con espera creciente."""
        ins = self._ins(rec)
        d = 1 if rec["side"] == "long" else -1
        px = float(_d(fila.get("avgPx")) or _d(fila.get("fillPx")) or _d(rec["plan"]["ref"]))
        llenado, fee, fee_ccy = _d(fila.get("accFillSz")), _d(fila.get("fee")), str(fila.get("feeCcy") or "")
        if ins.inst == "spot":
            neto = llenado + fee if fee_ccy == rec["coin"] else llenado   # la comisión de compra sale de lo comprado
            sz_total = redondear_abajo(neto, ins.lot)
            fee_open = float(-fee) * px if fee_ccy == rec["coin"] else float(-fee)
        else:
            sz_total, fee_open = llenado, float(-fee)
        qty = float(ins.qty_de_sz(sz_total))
        stop, sal = rec["stop_pct"], rec["salidas"]
        r = px * stop / 100
        sl = float(ins.px(px - d * r, arriba=d < 0))   # el stop, hacia fuera: nunca más cerca que el plan
        tp1 = float(ins.px(px + d * r * sal["tp1R"], arriba=d > 0)) if sal["tp1R"] else None
        tp2 = float(ins.px(px + d * r * sal["tp2R"], arriba=d > 0)) if sal["tp2R"] else None
        floor = float(ins.px(px + d * r * sal["floor_r"], arriba=d > 0)) if sal["lock_r"] else None
        notional = qty * px
        hold = rec["hold"]
        rec.update(estado="abierta", entry=px, qty=qty, qty0=qty, sz=_s(sz_total), sl=sl, sl_initial=sl,
                   sl_kind="inicial", high_water=px, last=px, riskUsd=notional * stop / 100, notional=notional,
                   fee_open=fee_open, tp1=tp1, tp1R=sal["tp1R"], tp1_frac=sal["tp1_frac"], tp2=tp2, tp2R=sal["tp2R"],
                   lock_r=sal["lock_r"], floor_r=sal["floor_r"], lock_trail_r=sal["lock_trail_r"], floor=floor,
                   locked=False, locked_at=None, hold_until=hold["until"], hold_label=hold["label"],
                   hold_active=None, hold_active_label=None, opened=now, ordId=fila.get("ordId") or rec.get("ordId"),
                   partials=[], amend_at=0, sondeo=now)
        if sal["lock_r"] and rec.get("hold_active_t") and rec["hold_active_t"] > hold["until"]:
            rec["hold_active"] = int(rec["hold_active_t"])
            rec["hold_active_label"] = str(rec.get("hold_active_label_t") or "cerrojo activado")
        if sz_total < ins.min_sz:
            rec["partes"] = []
            self._guardar(rec)
            self._aviso(f"{rec['coin']}: lo comprado ({_s(sz_total)}) queda por debajo del mínimo de OKX tras la "
                        "comisión: no se puede poner stop. Revísalo en la web de OKX", now, rec["story"], rec["coin"])
            return
        partes, nota = self._partes(ins, sz_total, sal)
        for p in partes:
            p.update(qty=float(ins.qty_de_sz(p["sz"])), sl=sl, tp=tp1 if p["tp_kind"] == "tp1" else tp2 if p["tp_kind"]
                     else None, estado="pendiente", algoClOrdId=f"{rec['cl']}p{p['n']}", reps=0)
        rec["partes"] = partes
        if nota:
            rec["nota"] = nota
        self._guardar(rec)
        fallos, dudas = [], []
        for p in partes:   # todas: una en duda no impide poner la de la otra parte
            ok, err, duda = await self._poner_algo(rec, ins, p)
            if not ok:
                (dudas if duda else fallos).append(err)
                if not duda:
                    break
        self._guardar(rec)
        ex = {"px": px, "qty": qty, "sz": rec["sz"], "notional": round(notional, 2), "sl": sl, "tp1": tp1, "tp2": tp2,
              "tp1Frac": sal["tp1_frac"], "floor": floor, "lockR": sal["lock_r"], "floorR": sal["floor_r"],
              "lockTrailR": sal["lock_trail_r"], "holdUntil": rec["hold_until"], "holdActive": rec["hold_active"],
              "instrument": ins.inst, "instId": ins.inst_id, "ordId": rec["ordId"], "fee": round(fee_open, 6),
              "partes": [{k: p.get(k) for k in ("n", "sz", "sl", "tp", "algoId", "estado")} for p in partes],
              **({"descartado": list(sal["dropped"])} if sal["dropped"] else {}), **({"nota": nota} if nota else {})}
        rec["exec"] = ex
        plan = ""
        if len(partes) > 1:
            plan += f" · parcial del {_n(sal['tp1_frac'] * 100, 0)} % en {_px(tp1)} (OCO en OKX)"
        if tp2:
            plan += f" · objetivo {_px(tp2)}"
        if sal["lock_r"]:
            plan += (f" · cerrojo: a +{_n(sal['lock_r'], 1)} R el stop sube a {_px(floor)} (+{_n(sal['floor_r'], 1)} R)"
                     + (f" y sigue al precio a {_n(sal['lock_trail_r'], 1)} R" if sal["lock_trail_r"] else ""))
        for txt in [*sal["dropped"], *([nota] if nota else [])]:
            plan += f" · {txt}"
        stop_txt = (f"stop {_px(sl)} en OKX" if not (fallos or dudas) else
                    f"SIN STOP CONFIRMADO EN OKX ({(fallos or dudas)[0]})")
        self._event("filled", "exec", rec["story"], rec["coin"],
                    f"{'Comprado' if d > 0 else 'Vendido'} {_qty(rec['coin'], qty)} {rec['coin']} a {_px(px)} "
                    f"({self._etq()})",
                    f"{ins.inst_id} · importe {_n(notional)} {config.QUOTE} · riesgo {_n(rec['riskUsd'])} {config.QUOTE}"
                    f" · comisión {_n(fee_open, 4)} {config.QUOTE} · {stop_txt}" + plan,
                    {"exec": dict(ex), "trade_id": rec["id"]}, now)
        log.info("Modo real: %s %s %s a %s (%s)", rec["side"], rec["sz"], ins.inst_id, px, rec["story"])
        if fallos:   # OKX rechaza el stop: la posición se cierra al momento
            self._aviso(f"{rec['coin']}: no se pudo poner el stop en OKX ({fallos[0]}): se cierra la posición al "
                        "momento", now, rec["story"], rec["coin"])
            await self._cerrar_posicion(rec, "Sin stop en OKX: cerrada al momento por seguridad")
            if rec["story"] in self.positions and self._sin_stop(rec):
                rec["reproteger_at"] = now + _espera(0)
                self._alerta_sin_stop(rec, now, "tampoco se pudo cerrar; se reintenta poner el stop o cerrar cada pocos "
                                                "segundos")
        elif dudas:   # quizá OKX sí lo tiene: no se pone otro ni se cierra a ciegas; se resuelve en unos segundos
            rec["reproteger_at"] = now + ALGO_DUDA_MS
            self._alerta_sin_stop(rec, now, f"no se sabe si OKX aceptó el stop ({dudas[0]}); se comprueba en unos "
                                            "segundos y, si no está, se pone otra vez o se cierra")
        if rec["story"] in self.positions:
            self._guardar(rec)
        self._status()

    def _cuerpo_algo(self, rec: dict, ins: Instrumento, p: dict, cl: str) -> dict:
        d = 1 if rec["side"] == "long" else -1
        b = {"instId": ins.inst_id, "side": "sell" if d > 0 else "buy", "ordType": "oco" if p.get("tp") else "conditional",
             "sz": p["sz"], "algoClOrdId": cl, "slTriggerPx": _s(p["sl"]), "slOrdPx": "-1", "slTriggerPxType": "last"}
        if p.get("tp"):
            b.update(tpTriggerPx=_s(p["tp"]), tpOrdPx="-1", tpTriggerPxType="last")
        if ins.inst == "spot":
            b.update(tdMode="cash", tradeQuoteCcy=config.QUOTE)
        else:
            b.update(tdMode=TD_PERP, reduceOnly=True)
        return b

    async def _poner_algo(self, rec: dict, ins: Instrumento, p: dict) -> tuple[bool, str | None, bool]:
        """Pone la algo de una parte (OCO o condicional) con reintentos, cada intento con un algoClOrdId nuevo. Antes de
        cada uno busca las anteriores que quedaron en duda (corte, OKX ocupado): si una está viva, la enlaza; si aún no
        se sabe, no pone otra (nunca dos stops por la misma parte). Devuelve (puesta, error, en_duda)."""
        err = None
        for _ in range(3):
            b = await self._buscar_dudosas(rec, ins, p)
            if b in ("viva", "cerrada"):
                return True, None, False
            if b == "duda":
                return False, err or "una orden de stop anterior sigue en duda", True
            cls = p.get("cls") or []
            cl = p["algoClOrdId"] + (f"r{len(cls)}" if cls else "")
            p["cls"] = [*cls, cl]
            self._guardar(rec)   # si el bot cae ahora, la reconciliación casa la algo con la parte por su algoClOrdId
            try:
                res = await self.cliente.privado("POST", "/api/v5/trade/order-algo",
                                                 cuerpo=self._cuerpo_algo(rec, ins, p, cl))
                algo_id = (res[0] if res else {}).get("algoId")
            except ErrorOKX as e:
                err = f"{e.codigo}: {e.texto}"
                if e.ambiguo:   # quizá la tiene: se busca antes de poner otra
                    p["dudosas"] = [*(p.get("dudosas") or []), {"cl": cl, "at": self.clock()}]
                    continue
                if isinstance(e, (ErrorClave, RutaProhibida)):
                    break
                continue
            if algo_id:
                p.update(algoId=str(algo_id), estado="viva", algoClOrdIdActual=cl, dudosas=[])
                return True, None, False
            err = "OKX no devolvió el algoId"
            p["dudosas"] = [*(p.get("dudosas") or []), {"cl": cl, "at": self.clock()}]
        duda = bool(p.get("dudosas")) and await self._buscar_dudosas(rec, ins, p) == "duda"
        if p.get("estado") in ("viva", "cerrada"):
            return True, None, False
        return False, err or "sin algoId", duda

    async def _buscar_dudosas(self, rec: dict, ins: Instrumento, p: dict) -> str | None:
        """Busca por su algoClOrdId las algo de la parte que quedaron en duda. «viva»: la enlaza; «cerrada»: ya saltó y
        cierra la parte; «duda»: alguna es aún muy reciente o no se pudo consultar; None: ninguna está (se puede poner
        otra). Una que no aparece en ALGO_OLVIDO_MS se da por no creada (si aparece después, la reconciliación la
        cancela por duplicada)."""
        quedan, ahora, duda = [], self.clock(), False
        for x in p.get("dudosas") or []:
            try:
                a = await self._algo(None, x["cl"])
            except ErrorOKX as e:
                self._error("buscar_algo", e, ahora)
                return "duda"
            st = (a or {}).get("state")
            if st in ("live", "pause", "partially_effective"):
                p.update(algoId=str(a.get("algoId")), estado="viva", algoClOrdIdActual=x["cl"], dudosas=[])
                sx = _f(a.get("slTriggerPx"))
                if sx:
                    p["sl"] = sx
                return "viva"
            if st == "effective":
                p.update(algoId=str(a.get("algoId")), estado="viva", dudosas=[])
                await self._parte_cerrada(rec, ins, p, a, ahora)
                return "cerrada"
            if a is None and ahora - _f(x.get("at"), 0) < ALGO_OLVIDO_MS:
                quedan.append(x)
                duda = duda or ahora - _f(x.get("at"), 0) < ALGO_DUDA_MS
        p["dudosas"] = quedan
        return "duda" if duda else None

    async def _algo(self, algo_id: str | None, cl: str | None = None) -> dict | None:
        try:
            d = await self.cliente.privado("GET", "/api/v5/trade/order-algo",
                                           {"algoId": algo_id} if algo_id else {"algoClOrdId": cl})
        except ErrorOKX as e:
            if e.codigo in NO_EXISTE:
                return None
            raise
        return d[0] if d else None

    # ---- gestión de las posiciones

    @staticmethod
    def _vivas(rec: dict) -> list[dict]:
        return [p for p in rec.get("partes") or [] if p.get("estado") == "viva"]

    @staticmethod
    def _sin_stop(rec: dict) -> list[dict]:
        """Partes que siguen en la cuenta sin una algo válida en OKX («pendiente», «cancelada» a mitad de un cierre, o
        «viva» con un tamaño que ya no vale tras un cierre parcial en X-Perps: rehacer)."""
        return [p for p in rec.get("partes") or [] if (p.get("estado") not in ("viva", "cerrada") or p.get("rehacer"))
                and p.get("estado") != "cerrada" and _d(p.get("sz")) > 0]

    def _alerta_sin_stop(self, rec: dict, now: int, detalle: str) -> None:
        """Aviso fuerte (una vez por episodio, y el panel lo enseña mientras dure) de una posición sin stop en OKX."""
        if not rec.get("sin_stop_desde"):
            rec["sin_stop_desde"] = now
            self._guardar(rec)
            self._aviso(f"¡{rec['coin']}: POSICIÓN SIN STOP EN OKX! {detalle}. Si no se arregla sola, pon el stop o "
                        "ciérrala tú en la web de OKX", now, rec["story"], rec["coin"], sin_stop=True)
            log.error("Modo real: %s SIN STOP en OKX (%s): %s", rec["coin"], rec["story"], detalle)
            self._status()

    async def _gestionar(self, rec: dict, now: int, prices: dict) -> None:
        if rec.get("estado") == "enviando":   # en duda o a medias: se consulta hasta saber cómo quedó
            if now - (rec.get("consulta_at") or 0) >= ENVIANDO_CADA_MS:
                rec["consulta_at"] = now
                await self._resolver_enviando(rec, now)
            return
        if rec.get("estado") != "abierta":
            return
        d = 1 if rec["side"] == "long" else -1
        px = prices.get(rec["coin"])
        if px:
            rec["last"] = px
            if d * (px - rec["high_water"]) > 0:
                rec["high_water"] = px
        if ((self._sin_stop(rec) or rec.get("cierres_dudosos")) and self.puede_reducir()
                and now >= (rec.get("reproteger_at") or 0)):
            await self._reproteger(rec, now)
            if rec["story"] not in self.positions:
                return
        if now - (rec.get("sondeo") or 0) >= SONDEO_MS:
            rec["sondeo"] = now
            await self._sondear(rec, now)
            if rec["story"] not in self.positions:
                return
        if rec.get("cierre_pedido") and self.puede_reducir():   # cierre manual o de la parada: hasta que salga
            if now >= (rec.get("cierre_at") or 0):
                await self._intentar_cierre(rec, rec["cierre_pedido"], now)
            return
        if not self.puede_escribir():   # solo lectura o parada: OKX mantiene los stops; el bot no los mueve
            return
        if px and rec.get("lock_r"):
            await self._cerrojo(rec, now)
        if (rec["story"] in self.positions and now >= rec["hold_until"]
                and now >= (rec.get("cierre_at") or 0)):
            await self._intentar_cierre(rec, f"Cierre por tiempo ({rec['hold_label']})", now)

    async def _intentar_cierre(self, rec: dict, why: str, now: int) -> None:
        n = rec.get("cierre_n") or 0
        await self._cerrar_posicion(rec, why)
        if rec["story"] in self.positions:   # no salió (o del todo): se reintenta con espera creciente
            rec["cierre_n"], rec["cierre_at"] = n + 1, now + _espera(n)
            self._guardar(rec)

    async def _reproteger(self, rec: dict, now: int) -> None:
        """Partes sin stop en OKX: primero se resuelve un cierre en duda; después, para cada parte, se busca su algo (por
        su algoClOrdId) y, si no está, se pone otra; si no se puede, se cierra a mercado. Con espera creciente entre
        intentos y, mientras quede alguna sin stop, aviso fuerte."""
        if not self.puede_reducir():
            return
        ins = self._ins(rec)
        if ins is None:
            return
        await self._resolver_cierres(rec, ins, now)
        if rec["story"] not in self.positions:
            return
        cerrar = []
        for p in [p for p in self._sin_stop(rec) if not p.get("cierre_dudoso")]:
            if p.get("rehacer"):   # algo viva con un tamaño que ya no vale: fuera y otra con el bueno
                await self._cancelar_algos(rec, ins, [p])
                if p.get("estado") != "cancelada":
                    continue
                p.pop("rehacer", None)
                p.pop("algoId", None)
                p["estado"] = "pendiente"
            ok, err, duda = await self._poner_algo(rec, ins, p)
            if ok and p.get("estado") == "viva":
                self._aviso(f"{rec['coin']}: la parte {p['n']} vuelve a tener su stop en OKX ({_px(p['sl'])})", now,
                            rec["story"], rec["coin"])
            elif not ok and not duda:
                cerrar.append(p)
        if cerrar:
            await self._cerrar_partes(rec, ins, cerrar, "Sin stop en OKX: cerrada al momento por seguridad")
        if rec["story"] not in self.positions:
            return
        if self._sin_stop(rec) or rec.get("cierres_dudosos"):
            n = rec.get("reproteger_n") or 0
            rec["reproteger_n"], rec["reproteger_at"] = n + 1, now + _espera(n)
            self._alerta_sin_stop(rec, now, "no se pudo poner el stop ni cerrar; se reintenta cada pocos segundos")
        elif rec.get("sin_stop_desde") or rec.get("reproteger_n"):
            if rec.get("sin_stop_desde"):
                self._aviso(f"{rec['coin']}: vuelve a tener stop en OKX en toda la posición", now, rec["story"],
                            rec["coin"], sin_stop=False)
            for k in ("sin_stop_desde", "reproteger_n", "reproteger_at"):
                rec.pop(k, None)
            self._status()
        self._guardar(rec)

    async def _cerrojo(self, rec: dict, now: int) -> None:
        """Cerrojo y seguimiento con paper.lock_update: antes de +lock_r R no mueve nada; activado, el stop de lo que
        queda sube al suelo y después sigue al mejor precio a lock_trail_r R. En OKX el stop solo sube (en un corto,
        solo baja), con amend-algos, y como mucho cada MOVER_CADA_MS salvo al activar el cerrojo. Si el precio ya ha
        cruzado el stop nuevo (o OKX lo rechaza por eso), se cierra a mercado, como el paper cierra en ese stop. Si OKX
        no deja modificar la algo, se cambia por otra con el stop nuevo."""
        ins = self._ins(rec)
        d = 1 if rec["side"] == "long" else -1
        was = bool(rec.get("locked"))
        nuevo, locked, kind = paper.lock_update(d, rec["entry"], rec["stop_pct"], rec["high_water"], rec["sl"], was,
                                                rec["lock_r"], rec.get("floor_r"), rec.get("lock_trail_r"))
        lock_now = locked and not was
        if lock_now:
            rec.update(locked=True, locked_at=now)
            if rec.get("hold_active") and rec["hold_active"] > rec["hold_until"]:
                rec["hold_until"], rec["hold_label"] = rec["hold_active"], rec.get("hold_active_label") or "cerrojo activado"
            self._guardar(rec)
        if not rec.get("locked"):   # nunca antes del cerrojo
            return
        floor = rec.get("floor")
        if floor is not None and d * (floor - nuevo) > 0:   # el suelo, aunque un intento anterior no llegara a OKX
            nuevo, kind = floor, "cerrojo"
        if kind is None:
            return
        objetivo = float(ins.px(nuevo, arriba=d > 0))
        r = rec["entry"] * rec["stop_pct"] / 100
        if d * (objetivo - rec["sl"]) <= 0:
            return
        primero = rec.get("sl_kind") not in ("cerrojo", "seguimiento")   # subir al suelo no espera
        if not primero and (d * (objetivo - rec["sl"]) < PASO_STOP_R * r
                            or now - (rec.get("amend_at") or 0) < MOVER_CADA_MS):
            return
        last = rec.get("last")
        if last and d * (last - objetivo) <= 0:   # el precio ya está al otro lado del stop nuevo
            rec["sl_kind"] = kind
            self._guardar(rec)
            await self._cerrar_partes(rec, ins, [p for p in rec.get("partes") or [] if p.get("estado") != "cerrada"],
                                      STOP_NAMES[kind])
            return
        cruzado, fallos = False, []
        for p in self._vivas(rec):
            res = await self._mover_stop(rec, ins, p, objetivo)
            if res == "cruzado":
                cruzado = True
            elif res == "fallo":
                fallos.append(p)
        rec["amend_at"] = now
        if cruzado:
            rec["sl_kind"] = kind
            self._guardar(rec)
            await self._cerrar_partes(rec, ins, [p for p in rec.get("partes") or [] if p.get("estado") != "cerrada"],
                                      STOP_NAMES[kind])
            return
        for p in fallos:
            if (p.get("amend_fallos") or 0) >= AMEND_FALLOS_REEMPLAZO and rec["story"] in self.positions:
                await self._reemplazar_stop(rec, ins, p, objetivo, kind)
        if rec["story"] not in self.positions:
            return
        vivas = self._vivas(rec)
        if not vivas or any(d * (objetivo - p["sl"]) > float(ins.tick) / 2 for p in vivas):
            self._guardar(rec)   # alguna no se ha movido: se reintenta en la ronda siguiente
            if fallos and not rec.get("aviso_amend"):
                rec["aviso_amend"] = now
                self._aviso(f"{rec['coin']}: OKX no deja mover el stop a {_px(objetivo)} ({kind}); se reintenta y, si "
                            "sigue fallando, se cambia la orden de stop por otra", now, rec["story"], rec["coin"])
            return
        rec.pop("aviso_amend", None)
        rec["sl"], rec["sl_kind"] = objetivo, kind
        lock = d * (objetivo - rec["entry"]) / r
        if primero:
            title = f"{rec['coin']}: cerrojo a +{_n(rec['floor_r'], 1)} R en OKX"
            tr = rec.get("lock_trail_r")
            text = (f"+{_n(rec['lock_r'], 1)} R alcanzado: el stop de lo que queda sube a {_px(objetivo)} en OKX "
                    f"(asegura {_sg(lock)} R)" + (f" y desde ahí sigue al precio a {_n(tr, 1)} R, sin bajar nunca" if tr
                                                  else "")
                    + f" · cierre por tiempo: {rec['hold_label']}")
            extra = {"lock": {"at": now, "lock_r": rec["lock_r"], "floor_r": rec.get("floor_r"),
                              "trail_r": rec.get("lock_trail_r"), "floor": rec.get("floor"), "hold_until": rec["hold_until"]}}
        else:
            title = f"{rec['coin']}: stop de seguimiento a {_px(objetivo)} en OKX"
            text = (f"A {_n(rec.get('lock_trail_r') or 0.0, 1)} R del {'máximo' if d > 0 else 'mínimo'} de "
                    f"{_px(rec['high_water'])} · asegura {_sg(lock)} R")
            extra = {}
        rec["exec"] = {**(rec.get("exec") or {}), "sl": objetivo, "high_water": rec["high_water"], "locked": True,
                       "holdUntil": rec["hold_until"]}
        self._event("manage", "exec", rec["story"], rec["coin"], title, text, {"exec": dict(rec["exec"]), **extra}, now)
        self._guardar(rec)

    async def _mover_stop(self, rec: dict, ins: Instrumento, p: dict, nuevo: float) -> str:
        """amend-algos del stop de una parte. Solo con el cerrojo activado y solo a favor (nunca baja). Devuelve «ok»,
        «no» (nada que mover), «duda» (lo aclara el sondeo), «cruzado» (stop al otro lado del último precio) o
        «fallo» (OKX lo rechaza: se cuenta para cambiar la algo)."""
        d = 1 if rec["side"] == "long" else -1
        if not rec.get("locked") or d * (nuevo - p["sl"]) <= 0:
            return "no"
        try:
            await self.cliente.privado("POST", "/api/v5/trade/amend-algos",
                                       cuerpo={"instId": ins.inst_id, "algoId": p["algoId"], "newSlTriggerPx": _s(nuevo),
                                               "newSlOrdPx": "-1", "newSlTriggerPxType": "last"})
        except ErrorOKX as e:
            if e.codigo in NO_EXISTE or e.ambiguo or e.reintentable:   # ya saltó, o no se sabe: lo verá el sondeo
                rec["sondeo"] = 0
                return "duda"
            if e.codigo in STOP_CRUZADO:
                return "cruzado"
            p["amend_fallos"] = (p.get("amend_fallos") or 0) + 1
            self._error("mover_stop", e, self.clock())
            return "fallo"
        p["sl"], p["amend_fallos"] = nuevo, 0
        return "ok"

    async def _reemplazar_stop(self, rec: dict, ins: Instrumento, p: dict, nuevo: float, kind: str) -> None:
        """OKX no deja modificar la algo: se cancela (comprobando que ya no está viva) y se pone otra con el stop nuevo;
        si no se puede, esa parte se cierra a mercado."""
        now = self.clock()
        await self._cancelar_algos(rec, ins, [p])
        if p.get("estado") != "cancelada":   # saltó entre tanto, o no se pudo cancelar: nada más que hacer ahora
            return
        p.update(sl=nuevo, estado="pendiente", amend_fallos=0)
        p.pop("algoId", None)
        ok, err, duda = await self._poner_algo(rec, ins, p)
        if ok:
            self._aviso(f"{rec['coin']}: OKX no dejaba mover el stop; se ha cambiado la orden de stop por otra en "
                        f"{_px(nuevo)}", now, rec["story"], rec["coin"])
        elif not duda:
            await self._cerrar_partes(rec, ins, [p], STOP_NAMES.get(kind, "Stop"))
        self._guardar(rec)

    async def _sondear(self, rec: dict, now: int) -> None:
        """Estado de las algo de la posición en OKX: la que ha saltado cierra su parte; manda lo que diga OKX. Una algo
        que OKX no encuentra durante ALGO_NO_ESTA_MS deja su parte sin stop: se trata como una cancelada (si la parte
        sigue en la cuenta, se vuelve a proteger o se cierra)."""
        ins = self._ins(rec)
        for p in self._vivas(rec):
            a = await self._algo(p["algoId"])
            if a is None:
                desde = p.setdefault("no_esta_desde", now)
                if now - desde >= ALGO_NO_ESTA_MS:
                    p.pop("no_esta_desde", None)
                    await self._parte_sin_stop(rec, ins, p, {"state": "no encontrada"}, now)
                continue
            p.pop("no_esta_desde", None)
            st = a.get("state")
            if st in ("live", "pause", "partially_effective"):
                sx = _f(a.get("slTriggerPx"))
                if sx and abs(sx - p["sl"]) > float(ins.tick) / 2:   # manda OKX (p. ej. lo cambiaste en su web)
                    p["sl"] = rec["sl"] = sx
                continue
            if st == "effective":
                await self._parte_cerrada(rec, ins, p, a, now)
            elif st in ("canceled", "order_failed"):
                await self._parte_sin_stop(rec, ins, p, a, now)
        if rec["story"] in self.positions and rec.get("partes") and all(
                p.get("estado") == "cerrada" for p in rec["partes"]):
            self._cerrar_registro(rec, now)
        elif rec["story"] in self.positions:
            self._guardar(rec)

    async def _parte_cerrada(self, rec: dict, ins: Instrumento, p: dict, a: dict, now: int, why: str | None = None) -> None:
        d = 1 if rec["side"] == "long" else -1
        ord_id = a.get("ordId") or next(iter(a.get("ordIdList") or []), None)
        o = await self._orden(ins, ord_id=ord_id) if ord_id else None
        lado = a.get("actualSide") or ("tp" if p.get("tp") and _f(a.get("actualPx")) and
                                       d * (_f(a.get("actualPx")) - p["tp"]) >= 0 else "sl")
        px = _f((o or {}).get("avgPx")) or _f(a.get("actualPx")) or (p["tp"] if lado == "tp" else p["sl"])
        sz = _d((o or {}).get("accFillSz")) or _d(p["sz"])
        fee = -(_f((o or {}).get("fee"), 0.0) or 0.0)
        qty = float(ins.qty_de_sz(sz))
        if lado == "tp":
            why = why or (f"Objetivo parcial ({_n(rec.get('tp1R') or 0.0, 1)} R)" if p["tp_kind"] == "tp1"
                          else f"Objetivo ({_n(rec.get('tp2R') or 0.0, 1)} R)")
            toque = p["tp"] if p.get("tp") is not None and d * (p["tp"] - px) > 0 else px   # el disparo, no la ejecución
            if d * (toque - rec["high_water"]) > 0:   # el precio llegó ahí aunque nuestro precio no lo viera
                rec["high_water"] = toque
        else:
            why = why or STOP_NAMES.get(rec.get("sl_kind"), "Stop")
        share = rec["fee_open"] * qty / rec["qty0"] if rec.get("qty0") else 0.0
        pnl = d * (px - rec["entry"]) * qty - fee - share
        p.update(estado="cerrada", exit=px, qty=qty, fee_exit=fee, why=why, at=now, pnl=round(pnl, 6),
                 sin_detalle=o is None)
        rec["qty"] = max(0.0, rec["qty"] - qty)
        if p["tp_kind"] == "tp1" and lado == "tp" and self._vivas(rec):
            part = {"qty": qty, "px": px, "at": now, "pnl": round(pnl, 4), "fee": round(fee, 6), "why": why,
                    "R": round(pnl / rec["riskUsd"], 4) if rec.get("riskUsd") else None, "tp1R": rec.get("tp1R")}
            rec["partials"] = [*rec.get("partials", []), part]
            rec["tp1_done"] = True
            rec["exec"] = {**(rec.get("exec") or {}), "qty": rec["qty"], "tp1_done": True, "partial": part}
            self._event("manage", "exec", rec["story"], rec["coin"], f"{rec['coin']}: salida parcial en {_px(px)} (OKX)",
                        f"{'Vendidos' if d > 0 else 'Comprados'} {_qty(rec['coin'], qty)} {rec['coin']} a {_px(px)}: "
                        f"{_sg(pnl)} {config.QUOTE} con comisión · quedan {_qty(rec['coin'], rec['qty'])} {rec['coin']}"
                        + (f" (el cerrojo se activa a +{_n(rec['lock_r'], 1)} R)" if rec.get("lock_r") and
                           not rec.get("locked") else ""),
                        {"exec": dict(rec["exec"]), "partial": {**part, "frac": rec.get("tp1_frac"),
                                                                "qty_left": rec["qty"], "locked": bool(rec.get("locked")),
                                                                "sl": rec["sl"], "floor": rec.get("floor"),
                                                                "hold_until": rec["hold_until"]}}, now)
            if rec.get("lock_r") and self.puede_escribir():   # el cerrojo, ya (no espera al siguiente precio)
                await self._cerrojo(rec, now)

    async def _parte_sin_stop(self, rec: dict, ins: Instrumento, p: dict, a: dict, now: int) -> None:
        """Una algo cancelada o fallida sin que el bot lo pidiera: si la parte sigue en la cuenta, se vuelve a proteger
        (o se cierra si no se puede, también en solo lectura); si ya no está, se da por cerrada fuera del bot."""
        tiene = await self._tiene(rec, ins, _d(p["sz"]))
        if not tiene:
            px = rec.get("last") or rec["entry"]
            p.update(estado="cerrada", exit=px, fee_exit=0.0, why="Cerrada fuera del bot (en OKX)", at=now,
                     estimada=True)
            rec["qty"] = max(0.0, rec["qty"] - p["qty"])
            self._aviso(f"{rec['coin']}: la orden de stop de OKX ya no está y la posición tampoco; se da por cerrada "
                        "fuera del bot (precio estimado)", now, rec["story"], rec["coin"])
            return
        p["estado"] = "pendiente"
        p.pop("algoId", None)
        self._aviso(f"{rec['coin']}: la orden de stop de OKX ya no está viva ({a.get('state')}); se vuelve a poner", now,
                    rec["story"], rec["coin"])
        if not self.puede_reducir():
            self._alerta_sin_stop(rec, now, f"el bot no puede escribir en OKX ({self.bloqueo or 'sin la cuenta leída'})")
            return
        rec["reproteger_at"] = 0
        await self._reproteger(rec, now)

    async def _tiene(self, rec: dict, ins: Instrumento, sz: Decimal) -> bool:
        """¿Sigue en la cuenta esa cantidad, en el sentido de la posición? Al contado, el saldo de la moneda; en
        X-Perps, la posición (con signo en net_mode)."""
        if ins.inst == "spot":
            await self._leer_saldo(self.clock())
            return _d((self.cuenta.get("monedas") or {}).get(rec["coin"])) >= sz * Decimal("0.99")
        d = 1 if rec["side"] == "long" else -1
        for f in await self.cliente.privado("GET", "/api/v5/account/positions", {"instType": "FUTURES"}):
            if f.get("instId") == ins.inst_id and d * _d(f.get("pos")) >= sz:
                return True
        return False

    async def _cerrar_posicion(self, rec: dict, why: str) -> None:
        ins = self._ins(rec)
        abiertas = [p for p in rec.get("partes") or [] if p.get("estado") != "cerrada"]
        if not abiertas:   # sin partes (por debajo del mínimo), o todas cerradas ya en OKX: manda el motivo de cada una
            if rec["story"] in self.positions:
                self._cerrar_registro(rec, self.clock(), None if rec.get("partes") else why)
            return
        await self._cerrar_partes(rec, ins, abiertas, why)

    async def _cerrar_partes(self, rec: dict, ins: Instrumento, partes: list[dict], why: str) -> bool:
        """Cierre a mercado de unas partes (True si OKX ha llenado algo). Antes comprueba en OKX que la posición está:
        nunca vende ni compra a ciegas. Al contado, primero cancela sus algo y solo vende lo que OKX confirma sin algo
        viva (si no, una algo olvidada podría vender después monedas que ya no son de la posición); si la venta no sale,
        el stop vuelve a OKX. En X-Perps las algo son reduceOnly: primero se cierra y después se cancelan. Una orden en
        duda no se repite ni se vuelve a proteger encima: se resuelve por su clOrdId en las rondas siguientes."""
        now = self.clock()
        d = 1 if rec["side"] == "long" else -1
        partes = [p for p in partes if p.get("estado") != "cerrada" and not p.get("cierre_dudoso")]
        if not partes:
            return False
        sz = sum((_d(p["sz"]) for p in partes), Decimal(0))
        try:
            if not await self._tiene(rec, ins, sz):
                rec["sondeo"] = 0
                if not rec.get("aviso_no_consta"):
                    rec["aviso_no_consta"] = now
                    self._aviso(f"{rec['coin']}: no consta en OKX la cantidad a cerrar ({_s(sz)} {ins.unidad()}): no se "
                                "manda ninguna orden; se revisan sus órdenes de stop", now, rec["story"], rec["coin"])
                self._guardar(rec)
                return False
        except ErrorOKX as e:
            self._error("cierre", e, now)
            return False
        rec.pop("aviso_no_consta", None)
        if ins.inst == "spot":
            for p in partes:
                if p.get("dudosas") and await self._buscar_dudosas(rec, ins, p) == "duda":
                    self._guardar(rec)
                    return False   # un stop en duda podría vender lo mismo: se espera a saberlo
            if rec["story"] not in self.positions:
                return True
            await self._cancelar_algos(rec, ins, [p for p in partes if p.get("estado") == "viva"])
            if all(p.get("estado") == "cerrada" for p in rec.get("partes") or []):   # saltaron entre tanto
                self._cerrar_registro(rec, now)
                return True
            if any(p.get("estado") == "viva" for p in partes):
                self._aviso(f"{rec['coin']}: OKX no confirma que se hayan cancelado sus órdenes de stop; no se vende lo "
                            "que aún tiene stop (lo protege) y se reintenta", now, rec["story"], rec["coin"])
            partes = [p for p in partes if p.get("estado") in ("pendiente", "cancelada")]
            if not partes:
                self._guardar(rec)
                return False
            sz = sum((_d(p["sz"]) for p in partes), Decimal(0))
        n = len(rec.get("cierres") or []) + 1
        cl, exp = f"{rec['cl']}c{n}", self.cliente.hora_okx() + EXP_ORDEN_MS
        cuerpo = {"instId": ins.inst_id, "side": "sell" if d > 0 else "buy", "ordType": "market", "sz": _s(sz),
                  "clOrdId": cl}
        if ins.inst == "spot":
            cuerpo.update(tdMode="cash", tgtCcy="base_ccy", tradeQuoteCcy=config.QUOTE)
        else:
            cuerpo.update(tdMode=TD_PERP, reduceOnly=True)
        rec["cierres"] = [*(rec.get("cierres") or []), cl]
        self._guardar(rec)
        fila, ord_id, dudoso = None, None, False
        try:
            try:
                res = await self.cliente.privado("POST", "/api/v5/trade/order", cuerpo=cuerpo, exp_ms=exp)
                ord_id = (res[0] if res else {}).get("ordId")
                dudoso = not ord_id
            except ErrorOKX as e:
                if not e.ambiguo:
                    raise
                o, seguro = await self._consultar_tras_duda(ins, cl, exp)
                if o is None and seguro:
                    raise
                ord_id, dudoso = (o or {}).get("ordId"), o is None
            if ord_id:
                fila = await self._esperar_llenado(ins, ord_id, cl)
                if fila and fila.get("state") in ("live", "partially_filled"):   # no se llena del todo: fuera el resto
                    await self._cancelar_entrada(ins, fila)
                    fila = await self._orden(ins, ord_id=ord_id) or fila
        except ErrorOKX as e:
            self._error("cierre", e, now)
        if dudoso or (ord_id and (fila is None or fila.get("state") in ("live", "partially_filled"))):
            for p in partes:
                p["cierre_dudoso"] = cl
                if p.get("estado") != "viva":
                    p["estado"] = "pendiente"
            rec["cierres_dudosos"] = [*(rec.get("cierres_dudosos") or []),
                                      {"cl": cl, "exp": exp, "why": why, "ordId": ord_id}]
            rec["reproteger_at"] = now
            self._guardar(rec)
            self._aviso(f"{rec['coin']}: no se sabe aún si OKX ha ejecutado el cierre ({why}); no se repite ni se pone "
                        "otro stop encima: se comprueba por su clOrdId en cada ronda", now, rec["story"], rec["coin"])
            return False
        if not fila or _d(fila.get("accFillSz")) <= 0:
            if ins.inst == "spot":   # la venta no salió: el stop vuelve a OKX
                for p in partes:
                    p["estado"] = "pendiente"
                    p.pop("algoId", None)
                    await self._poner_algo(rec, ins, p)
            self._guardar(rec)
            con_stop = all(p.get("estado") == "viva" for p in partes)
            self._aviso(f"{rec['coin']}: no se pudo cerrar a mercado ({why}); "
                        + ("sigue con su stop en OKX y se reintenta" if con_stop else
                           "QUEDA SIN STOP EN OKX: se reintenta poner el stop o cerrar cada pocos segundos"),
                        now, rec["story"], rec["coin"])
            if not con_stop:
                rec["reproteger_at"] = now + _espera(rec.get("reproteger_n") or 0)
                self._alerta_sin_stop(rec, now, f"no se pudo cerrar ({why}) ni volver a poner el stop")
            return False
        await self._aplicar_cierre(rec, ins, partes, fila, why, now)
        return True

    async def _aplicar_cierre(self, rec: dict, ins: Instrumento, partes: list[dict], fila: dict, why: str,
                              now: int) -> None:
        """Apunta un cierre que OKX ha llenado (del todo o en parte). Con un llenado parcial solo se cierra lo llenado:
        la parte que se queda a medias se parte en dos y lo que sigue abierto vuelve a llevar su stop en OKX (al contado
        ya no tenía; en X-Perps, la algo de la parte partida tenía un tamaño que ya no vale y se cambia)."""
        d = 1 if rec["side"] == "long" else -1
        px = _f(fila.get("avgPx")) or rec.get("last") or rec["entry"]
        fee_total = -(_f(fila.get("fee"), 0.0) or 0.0)
        llenado = _d(fila.get("accFillSz"))
        sz = sum((_d(p["sz"]) for p in partes), Decimal(0))
        restante, cerradas, partidas, intactas = llenado, [], [], []
        for p in partes:
            p.pop("cierre_dudoso", None)
            s = _d(p["sz"])
            if restante >= s:
                cerradas.append((p, s))
                restante -= s
            elif restante > 0:
                pieza = {k: v for k, v in p.items() if k not in ("algoId", "dudosas", "cls", "algoClOrdIdActual")}
                pieza.update(n=f"{p['n']}c{len(rec.get('cierres') or [])}", sz=_s(restante))
                rec["partes"].append(pieza)
                cerradas.append((pieza, restante))
                p["sz"] = _s(s - restante)
                p["qty"] = float(ins.qty_de_sz(p["sz"]))
                partidas.append(p)
                restante = Decimal(0)
            else:
                intactas.append(p)
        for p, s in cerradas:
            q = float(ins.qty_de_sz(s))
            share = rec["fee_open"] * q / rec["qty0"] if rec.get("qty0") else 0.0
            fee = fee_total * float(s / llenado) if llenado else 0.0
            pnl = d * (px - rec["entry"]) * q - fee - share
            p.update(estado="cerrada", exit=px, qty=q, fee_exit=fee, why=why, at=now, pnl=round(pnl, 6))
            rec["qty"] = max(0.0, rec["qty"] - q)
        if ins.inst == "perp":   # reduceOnly: lo cerrado y lo partido dejan de tener algo válida
            await self._cancelar_algos(rec, ins, [p for p, _ in cerradas if p.get("algoId")] + partidas)
        resto = [p for p in partidas + intactas if p.get("estado") != "cerrada"]
        ids_partidas = {id(p) for p in partidas}
        for p in resto:
            if ins.inst == "spot" or id(p) in ids_partidas:
                if p.get("estado") == "viva":   # X-Perps: su algo (de más tamaño) no se pudo cancelar: se rehace luego
                    p["rehacer"] = True
                    continue
                p["estado"] = "pendiente"
                p.pop("algoId", None)
                if _d(p["sz"]) < ins.min_sz:   # resto por debajo del mínimo de OKX: ni stop ni venta posibles
                    q = float(ins.qty_de_sz(p["sz"]))
                    p.update(estado="cerrada", exit=px, qty=q, fee_exit=0.0, at=now, estimada=True, pnl=round(
                        d * (px - rec["entry"]) * q, 6), why="Resto por debajo del mínimo de OKX (queda en tu cuenta)")
                    rec["qty"] = max(0.0, rec["qty"] - q)
                    continue
                await self._poner_algo(rec, ins, p)
        if llenado < sz:
            self._aviso(f"{rec['coin']}: OKX solo cerró {_s(llenado)} de {_s(sz)} ({why}); lo que queda sigue abierto "
                        "con su stop en OKX y se vuelve a intentar", now, rec["story"], rec["coin"])
        if not [p for p in rec["partes"] if p.get("estado") != "cerrada"]:
            self._cerrar_registro(rec, now, why)
        else:
            if self._sin_stop(rec):
                rec["reproteger_at"] = 0
            self._guardar(rec)

    async def _resolver_cierres(self, rec: dict, ins: Instrumento, now: int) -> None:
        """Cierres en duda: si OKX los llenó, se apuntan; si pasado su expTime OKX no los tiene, no salieron y esas
        partes vuelven a necesitar su stop; si no se puede saber, se espera."""
        quedan = []
        for x in rec.get("cierres_dudosos") or []:
            partes = [p for p in rec.get("partes") or [] if p.get("cierre_dudoso") == x["cl"]]
            try:
                o = await self._orden(ins, ord_id=x.get("ordId"), cl=x["cl"])
            except ErrorOKX as e:
                self._error(rec["story"], e, now)
                quedan.append(x)
                continue
            if o is not None and o.get("state") in ("live", "partially_filled"):
                try:
                    await self._cancelar_entrada(ins, o)
                except ErrorOKX as e:
                    self._error(rec["story"], e, now)
                quedan.append(x)
                continue
            if o is not None and _d(o.get("accFillSz")) > 0:
                await self._aplicar_cierre(rec, ins, partes, o, x.get("why") or "Cierre", now)
                if rec["story"] not in self.positions:
                    return
                continue
            if o is None and not x.get("ordId") and self.cliente.hora_okx() <= _f(x.get("exp"), 0) + MARGEN_EXP_MS:
                quedan.append(x)   # aún podría llegar
                continue
            for p in partes:   # no salió (OKX la canceló sin llenar o nunca la tuvo): esas partes siguen abiertas
                p.pop("cierre_dudoso", None)
        rec["cierres_dudosos"] = quedan
        if not quedan:
            rec.pop("cierres_dudosos", None)
        self._guardar(rec)

    async def _cancelar_algos(self, rec: dict, ins: Instrumento, partes: list[dict]) -> None:
        """Cancela las algo de esas partes y comprueba en OKX que ya no están vivas: solo entonces la parte pasa a
        «cancelada». Si una saltó entre tanto, su parte se cierra con lo que diga OKX; si sigue viva, se queda «viva»."""
        con = [p for p in partes if p.get("algoId")]
        ids = [{"instId": ins.inst_id, "algoId": p["algoId"]} for p in con]
        for i in range(0, len(ids), 10):
            try:
                await self.cliente.privado("POST", "/api/v5/trade/cancel-algos", cuerpo=ids[i:i + 10])
            except ErrorOKX as e:
                if e.codigo not in NO_EXISTE:
                    self._error("cancelar_algos", e, self.clock())
        for p in con:
            if p.get("estado") != "viva":
                continue
            try:
                a = await self._algo(p["algoId"])
            except ErrorOKX as e:
                self._error("cancelar_algos", e, self.clock())
                continue
            st = (a or {}).get("state")
            if a is None or st in ("canceled", "order_failed"):
                p["estado"] = "cancelada"
            elif st == "effective":
                await self._parte_cerrada(rec, ins, p, a, self.clock())

    def _cerrar_registro(self, rec: dict, now: int, why: str | None = None) -> dict:
        """Operación cerrada (formato 3.2 del paper): pnl, comisiones y R con todas las partes."""
        d = 1 if rec["side"] == "long" else -1
        partes = [p for p in rec.get("partes") or [] if p.get("exit") is not None]
        q_all = sum(p["qty"] for p in partes)
        avg = sum(p["exit"] * p["qty"] for p in partes) / q_all if q_all else rec.get("last") or rec["entry"]
        fees = rec.get("fee_open", 0.0) + sum(p.get("fee_exit") or 0.0 for p in partes)
        pnl = d * (avg - rec["entry"]) * q_all - fees
        risk = rec.get("riskUsd") or 0.0
        R = pnl / risk if risk else 0.0
        why = why or (partes[-1]["why"] if partes else "Cerrada")
        if len(partes) > 1 and any(p.get("tp_kind") == "tp1" and str(p.get("why", "")).startswith("Objetivo parcial")
                                   for p in partes) and "tras salida parcial" not in why:
            why += " tras salida parcial"
        trade = {"id": rec["id"], "story": rec["story"], "strategy": rec.get("strategy"), "window": rec.get("window"),
                 "market": rec.get("market"), "coin": rec["coin"], "side": rec["side"], "qty": round(q_all, 10),
                 "entry": round(rec["entry"], 8), "exit": round(avg, 8), "sl_initial": round(rec.get("sl_initial") or 0, 8),
                 "trail_pct": None, "opened": rec["opened"], "closed": now, "reason": why, "pnl": round(pnl, 4),
                 "pnl_pct": round(pnl / rec["notional"] * 100, 4) if rec.get("notional") else 0.0, "R": round(R, 4),
                 "fees": round(fees, 4), "headline": rec.get("headline"), "news_score": None,
                 "riskUsd": round(risk, 4), "notional": round(rec.get("notional") or 0.0, 2),
                 "equity_pct": round(pnl / self.equity() * 100, 4) if self.equity() else None,
                 "sl_final": round(rec.get("sl") or 0, 8), "high_water": round(rec.get("high_water") or 0, 8),
                 "instrument": rec.get("instrument"), "instId": rec.get("inst_id"), "funding": None,
                 "commissions": round(fees, 4), "modo": rec.get("modo"), "real": True, "cuenta": rec.get("cuenta"),
                 "entorno": rec.get("entorno"), "cl": rec.get("cl"),
                 "partes": [{k: p.get(k) for k in ("n", "sz", "exit", "why", "pnl", "fee_exit", "estimada")} for p in partes]}
        if rec.get("partials"):
            trade["partials"] = rec["partials"]
        if rec.get("lock_r"):
            trade.update(lock_r=rec["lock_r"], floor_r=rec.get("floor_r"), lock_trail_r=rec.get("lock_trail_r"),
                         locked=bool(rec.get("locked")), locked_at=rec.get("locked_at"))
        if rec.get("tags"):
            trade["tags"] = rec["tags"]
        self.positions.pop(rec["story"], None)
        self.closed.append(trade)
        self._guardar(trade, now)
        result = {k: trade[k] for k in ("coin", "side", "qty", "entry", "exit", "pnl", "pnl_pct", "R", "fees", "opened",
                                        "riskUsd", "notional", "headline", "strategy", "window")}
        result.update(why=why, closedAt=now)
        self._event("closed", "exec", rec["story"], rec["coin"],
                    f"{rec['coin']} cerrada: {_sg(pnl)} {config.QUOTE} ({self._etq()})",
                    f"{why} · {_sg(R)} R · comisiones {_n(fees, 4)} {config.QUOTE} (sin funding: lo liquida OKX en el saldo)",
                    {"result": result, "trade_id": trade["id"]}, now, tone="good" if pnl >= 0 else "bad")
        log.info("Modo real: cerrada %s %s (%s): %.4f %s", rec["coin"], rec["story"], why, pnl, config.QUOTE)
        self._status()
        return trade

    # ---- reconciliación

    async def reconciliar(self, now: int | None = None) -> dict:
        """Al arrancar y cada 5 min: manda OKX. Las algo que saltaron con el bot parado cierran su parte; el stop de OKX
        sustituye al guardado; una entrada a medias se resuelve (se protege, se cancela el resto o se descarta); las
        partes sin stop se vuelven a proteger; una algo del bot sin enlazar se casa con su parte por su algoClOrdId y,
        si es de una posición ya cerrada, duplicada o de un X-Perp sin posición, se cancela (las que no se sabe de qué
        son se dejan y se avisan); las órdenes de entrada viejas del bot se cancelan; las posiciones que el bot no conoce
        se avisan y cuentan en los límites."""
        now = self.clock() if now is None else now
        self._t["reconciliar"] = now
        cli = self.cliente
        pend = await cli.privado("GET", "/api/v5/trade/orders-pending")
        algos = []
        for tipo in ("conditional", "oco"):
            algos += await cli.privado("GET", "/api/v5/trade/orders-algo-pending", {"ordType": tipo})
        poss = await cli.privado("GET", "/api/v5/account/positions", {"instType": "FUTURES"})
        por_algo = {str(a.get("algoId")): a for a in algos if isinstance(a, dict)}
        cambios = []
        conocidas_algo = {str(p.get("algoId")) for rec in self.positions.values() for p in rec.get("partes") or []
                          if p.get("algoId")}
        # algo del bot sin enlazar que son de una parte sin stop (el bot cayó entre ponerla y guardarla): se enlazan
        por_cl = {}
        for rec in self.positions.values():
            for p in rec.get("partes") or []:
                if p.get("estado") not in ("viva", "cerrada"):
                    for c in [*(p.get("cls") or []), *(x.get("cl") for x in p.get("dudosas") or [])]:
                        por_cl[c] = (rec, p)
        for a in algos:
            m = por_cl.get(str(a.get("algoClOrdId") or ""))
            if m and str(a.get("algoId")) not in conocidas_algo and m[1].get("estado") not in ("viva", "cerrada"):
                rec, p = m
                p.update(algoId=str(a.get("algoId")), estado="viva", algoClOrdIdActual=a.get("algoClOrdId"), dudosas=[])
                p["sl"] = _f(a.get("slTriggerPx")) or p["sl"]
                conocidas_algo.add(str(a.get("algoId")))
                cambios.append(f"{rec['coin']}: su orden de stop {a.get('algoClOrdId')} estaba en OKX; enlazada")
                self._guardar(rec)
        for rec in list(self.positions.values()):
            ins = self._ins(rec)
            if ins is None:
                cambios.append(f"{rec['coin']}: instrumento {rec.get('inst_id')} desconocido; se deja como está")
                continue
            if rec.get("estado") == "enviando":
                antes = rec.get("estado")
                await self._resolver_enviando(rec, now)
                if rec["story"] not in self.positions:
                    cambios.append(f"{rec['coin']}: la entrada no llegó a llenarse; se descarta")
                elif rec.get("estado") != antes:
                    cambios.append(f"{rec['coin']}: la entrada se llenó sin que el bot lo viera; se le ponen stop y "
                                   "objetivo")
                else:
                    cambios.append(f"{rec['coin']}: la entrada sigue sin confirmar en OKX; se vuelve a consultar")
                continue
            for p in self._vivas(rec):
                a = por_algo.get(str(p.get("algoId")))
                if a:
                    sx = _f(a.get("slTriggerPx"))
                    if sx and abs(sx - p["sl"]) > float(ins.tick) / 2:
                        cambios.append(f"{rec['coin']}: el stop en OKX es {_px(sx)} (el bot tenía {_px(p['sl'])}); manda OKX")
                        p["sl"] = sx
                        rec["sl"] = sx
            antes = len(self._vivas(rec))
            if any(str(p.get("algoId")) not in por_algo for p in self._vivas(rec)):
                await self._sondear(rec, now)
                if rec["story"] not in self.positions or len(self._vivas(rec)) != antes:
                    cambios.append(f"{rec['coin']}: OKX ejecutó o canceló órdenes con el bot parado; actualizado")
            if rec["story"] in self.positions and (self._sin_stop(rec) or rec.get("cierres_dudosos")):
                await self._reproteger(rec, now)
                if rec["story"] in self.positions and self._sin_stop(rec):
                    cambios.append(f"{rec['coin']}: SIN STOP en OKX; se sigue intentando poner el stop o cerrar")
            if rec["story"] in self.positions:
                self._guardar(rec)
        conocidas_cl = {rec.get("clOrdId") for rec in self.positions.values()}
        ajenas = 0
        for o in pend:
            cl = str(o.get("clOrdId") or "")
            if cl.startswith(PREFIJO_CL):
                if cl not in conocidas_cl and cl.endswith("e") and self.puede_reducir():
                    try:
                        await cli.privado("POST", "/api/v5/trade/cancel-order",
                                          cuerpo={"instId": o.get("instId"), "ordId": o.get("ordId")})
                        cambios.append(f"Orden de entrada vieja del bot en {o.get('instId')}: cancelada")
                    except ErrorOKX as e:
                        self._error("reconciliar", e, now)
            else:
                ajenas += 1
        conocidas_inst = {rec.get("inst_id") for rec in self.positions.values() if rec.get("instrument") == "perp"}
        self.externas = [{"coin": str(f.get("instId", "")).split("-")[0], "instId": f.get("instId"), "pos": f.get("pos"),
                          "avgPx": _f(f.get("avgPx"))}
                         for f in poss if isinstance(f, dict) and _d(f.get("pos")) != 0
                         and f.get("instId") not in conocidas_inst]
        conocidas_algo = {str(p.get("algoId")) for rec in self.positions.values() for p in rec.get("partes") or []
                          if p.get("algoId") and p.get("estado") == "viva"}
        con_pos = {f.get("instId") for f in poss if isinstance(f, dict) and _d(f.get("pos")) != 0}
        bases_abiertas = {rec.get("cl") for rec in self.positions.values()}
        bases_cerradas = {t.get("cl") for t in self.closed if t.get("cl")}
        sobran, dudosas = [], []
        for a in algos:
            acl = str(a.get("algoClOrdId") or "")
            if not acl.startswith(PREFIJO_CL) or str(a.get("algoId")) in conocidas_algo:
                continue
            base = acl[:len(PREFIJO_CL) + 14]
            perp_sin_pos = str(a.get("instId") or "") not in con_pos and not str(a.get("instId") or "").endswith(
                f"-{config.QUOTE}")
            if base in bases_cerradas or base in bases_abiertas or perp_sin_pos:
                sobran.append(a)   # de una posición ya cerrada, duplicada, o reduceOnly sin posición: no protege nada
            else:
                dudosas.append(a)  # no se sabe de qué es (¿base de datos perdida?): no se toca
        if sobran and self.puede_reducir():
            ids = [{"instId": a.get("instId"), "algoId": a.get("algoId")} for a in sobran]
            for i in range(0, len(ids), 10):
                try:
                    await cli.privado("POST", "/api/v5/trade/cancel-algos", cuerpo=ids[i:i + 10])
                except ErrorOKX as e:
                    self._error("reconciliar", e, now)
            cambios.append(f"{len(sobran)} órdenes de stop del bot que ya no protegían nada: canceladas")
        self.reconciliacion = {"at": now, "cambios": cambios, "externas": list(self.externas),
                               "ordenes_ajenas": ajenas, "algos_huerfanas": len(dudosas)}
        if cambios or self.externas or dudosas:
            txt = "; ".join(cambios) if cambios else "Sin cambios en las posiciones del bot"
            if self.externas:
                txt += (" · Posiciones en OKX fuera del bot (cuentan en los límites; el bot no las toca): "
                        + ", ".join(f"{x['instId']} {x['pos']}" for x in self.externas))
            if dudosas:
                txt += (f" · {len(dudosas)} órdenes de stop del bot sin registro (¿base de datos perdida?): el bot no "
                        "las toca; revísalas en OKX")
            self._aviso("Reconciliación con OKX: " + txt, now, reconciliacion=self.reconciliacion)
        return self.reconciliacion

    # ---- ronda

    async def _vigilar_modo(self, now: int) -> None:
        if now - self._t["vigilar"] < VIGILAR_MS:
            return
        self._t["vigilar"] = now
        pa = leer_parada(self.carpeta)
        if pa and _clave_parada(pa) != self._parada_vista:   # cada PARAR_REAL se atiende una vez (también en solo
            if not self.parada or (pa["cerrar"] and not self.parada.get("cerrar")):   # lectura y tras otra parada)
                self._iniciar_parada(pa["motivo"], pa["cerrar"], escribir=False, archivo=_clave_parada(pa))
            else:
                self._parada_vista = _clave_parada(pa)
        nuevo = self._evaluar()
        viejo = self.estado
        if self._parando and nuevo.claves == viejo.claves and nuevo.demo == viejo.demo:
            return   # la parada en curso termina en solo lectura (_fin_parada)
        if nuevo.modo == viejo.modo and nuevo.demo == viejo.demo and nuevo.claves == viejo.claves:
            if nuevo.motivos != viejo.motivos:
                self.estado = nuevo
                self.publish_cuenta(force=True)
            return
        otra_cuenta = nuevo.claves != viejo.claves or nuevo.demo != viejo.demo or self.cliente is None
        if otra_cuenta:   # lo que esperaba aprobación o salir se pidió para la otra cuenta: no pasa a esta
            self._anular_entradas("Cambio de claves o de entorno en okx.env: la orden era para la otra cuenta y no se "
                                  "manda", now)
        self.estado = nuevo
        if otra_cuenta:
            # otras claves u otro entorno: hay que volver a leer la cuenta y nada de lo guardado se gestiona hasta saber
            # que es de esa cuenta (los registros de la demo nunca se tocan con las claves reales, ni al revés)
            self.listo, self.bloqueo, self._bloqueo_retirada, self.cuenta_id = False, None, False, None
            self.cuenta, self.instrumentos, self.externas = {}, {}, []
            self.comisiones = {"spot": None, "perp": None}
            self.comisiones_creador = {"spot": None, "perp": None}
            self.xperps = None
            self._lever = set()
            self._adoptar()
            self._t["arranque"] = 0
            self._preparar_cliente()
            self._encolar_parada_pendiente()
            if self._parando and self.cliente is None:   # sin claves ya no hay nada que hacer en OKX
                self._cola = [a for a in self._cola if a["accion"] != "parada"]
                self._fin_parada((self.parada or {}).get("motivo") or "Parada de emergencia", now,
                                 {"errores": ["sin claves en okx.env"]})
        elif nuevo.opera() and not viejo.opera():
            self.listo = False   # vuelve a comprobar la cuenta antes de operar
            self._t["arranque"] = 0
        self._ajustar_escritura()
        log.warning("Modo real: %s → %s (%s)", viejo.modo, nuevo.modo, "; ".join(nuevo.motivos))
        self._aviso(f"Modo {viejo.modo} → {nuevo.modo}: " + "; ".join(nuevo.motivos), now, de=viejo.modo, a=nuevo.modo)
        self._status()

    async def ronda(self, now: int | None = None) -> None:
        """Una ronda: modo, arranque, aprobaciones, cola de órdenes, posiciones, saldo y reconciliación. Cada parte va
        con su guarda: un fallo se registra y el resto sigue."""
        now = self.clock() if now is None else now
        await self._guard("vigilar", self._vigilar_modo, now)
        self._step_pending(now)   # también apagado: lo que esperaba aprobación no se aprueba solo
        if self.cliente is None:
            self.publish_cuenta()
            return
        if not self.listo and now - self._t["arranque"] >= ARRANQUE_REINTENTO_MS:
            await self.arrancar(now)
        espera = []
        while self._cola:
            acc = self._cola.pop(0)
            if acc["accion"] == "abrir":
                await self._guard(acc["item"]["story"], self._abrir, acc["item"])
            elif acc["accion"] == "cerrar":
                rec = self.positions.get(acc["story"])
                if rec and rec.get("estado") == "abierta":
                    await self._guard(acc["story"], self._cerrar_posicion, rec, acc.get("why") or "Cierre manual")
            elif acc["accion"] == "parada":
                if not self.listo:   # sin la cuenta leída no se sabe qué es del bot: espera a poder leerla
                    espera.append(acc)
                    continue
                await self._guard("parada", self._ejecutar_parada, acc)
        self._cola[:0] = espera
        if self.listo:
            prices = self._prices(now)
            for story, rec in list(self.positions.items()):
                await self._guard(story, self._gestionar, rec, now, prices)
            if now - self._t["saldo"] >= SALDO_MS:
                await self._guard("saldo", self._leer_saldo, now)
            if now - self._t["reconciliar"] >= RECONCILIAR_MS:
                await self._guard("permisos", self._revisar_permisos, now)
                await self._guard("reconciliar", self.reconciliar, now)
            self._check_halt(now)
        self.publish_cuenta()

    async def _guard(self, key: str, fn, *args) -> None:
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except ErrorClave as e:
            self._bloqueo_clave(e)
            self._ajustar_escritura()
            self._error(key, e, self.clock())
        except Exception as e:
            self._error(key, e, self.clock())

    async def run(self) -> None:
        """Bucle del modo real. Ninguna excepción lo tumba (salvo la cancelación al apagar el bot)."""
        self.announce()
        await self._guard("arranque", self.arrancar)
        while True:
            try:
                await self.ronda()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._error("ronda", e, self.clock())
            await self._dormir(RONDA_S)

    # ---- vistas

    @staticmethod
    def _vista_apartada(r: dict) -> dict:
        return {"story": r.get("story"), "coin": r.get("coin"), "side": r.get("side"), "estado": r.get("estado"),
                "opened": r.get("opened"), "modo": r.get("modo"), "entorno": r.get("entorno"), "instId": r.get("inst_id"),
                "sz": r.get("sz") or (r.get("plan") or {}).get("sz")}

    def _vista_pos(self, p: dict, prices: dict) -> dict:
        px = prices.get(p["coin"]) or p.get("last") or p.get("entry") or 0.0
        if p.get("estado") != "abierta":
            return {"story": p["story"], "coin": p["coin"], "side": p["side"], "estado": p.get("estado"),
                    "opened": p.get("opened"), "riskUsd": None, "instrument": p.get("instrument"), "real": True,
                    "sin_stop": p.get("estado") == "enviando"}
        realized = sum(x.get("pnl") or 0.0 for x in p.get("partes") or [] if x.get("estado") == "cerrada")
        u = self._upnl(p, px)
        return {"story": p["story"], "coin": p["coin"], "side": p["side"], "qty": round(p["qty"], 10),
                "entry": round(p["entry"], 8), "sl": round(p["sl"], 8), "trail_pct": None,
                "high_water": round(p["high_water"], 8), "opened": p["opened"], "riskUsd": round(p["riskUsd"], 4),
                "notional": round(p["notional"], 2), "headline": p.get("headline"), "strategy": p.get("strategy"),
                "window": p.get("window"), "id": p["id"], "market": p.get("market"), "price": px, "upnl": round(u, 4),
                "R": round(u / p["riskUsd"], 3) if p.get("riskUsd") else 0.0, "sl_initial": round(p["sl_initial"], 8),
                "tp2": p.get("tp2"), "be": False, "hold_until": p["hold_until"], "tp1": p.get("tp1"),
                "tp1_frac": p.get("tp1_frac", 0.0), "tp1_done": bool(p.get("tp1_done")), "realized": round(realized, 4),
                "be_r": None, "trail_start_r": None, "instrument": p.get("instrument"), "funding": None,
                "tags": p.get("tags"), "hold_label": p.get("hold_label"), "qty0": round(p.get("qty0") or 0.0, 10),
                "lock_r": p.get("lock_r"), "floor_r": p.get("floor_r"), "lock_trail_r": p.get("lock_trail_r"),
                "locked": bool(p.get("locked")), "floor": p.get("floor"), "hold_active": p.get("hold_active"),
                "sl_kind": p.get("sl_kind"), "instId": p.get("inst_id"), "real": True, "estado": "abierta",
                "sin_stop": bool(self._sin_stop(p)), "sin_stop_desde": p.get("sin_stop_desde"),
                "partes": [{k: x.get(k) for k in ("n", "sz", "sl", "tp", "estado", "algoId")} for x in p.get("partes") or []]}

    def portfolio(self, now: int | None = None) -> dict:
        """Cartera con el formato del paper (contrato 2.5) para el motor: «halted» es True mientras no se puede abrir
        nada (así el motor no envía). Las posiciones de OKX fuera del bot van en positions (ocupan su moneda)."""
        now = self.clock() if now is None else now
        prices = self._prices(now)
        pnl, pct = self.day_pnl(now)
        positions = [self._vista_pos(p, prices) for p in self.positions.values()]
        positions += [{"story": f"okx-{x['instId']}", "coin": x["coin"], "side": "long" if _d(x["pos"]) > 0 else "short",
                       "qty": _f(x["pos"]), "entry": x.get("avgPx"), "externa": True, "real": True}
                      for x in self.externas]
        pending = [{"story": i["story"], "deadline": i.get("deadline"), "mode": i.get("mode") or "cola",
                    "coin": i["ticket"]["coin"], "side": i["ticket"]["side"]}
                   for s, i in self.en_curso.items() if s not in self.positions]
        lim = self.limites
        return {"type": "portfolio", "ts": now, "equity": round(self.equity(), 2), "start_equity": self.inicio,
                "day_pnl": round(pnl, 2), "day_pnl_pct": round(pct, 3), "positions": positions, "pending": pending,
                "scheduled": [], "closed": list(reversed(self.closed[-CLOSED_EN_CARTERA:])),
                "limits": {"risk_per_trade_pct": config.RISK_PER_TRADE_PCT, "max_daily_loss_pct": lim["perdida_dia_pct"],
                           "max_positions": lim["max_posiciones"], "max_day_entries": lim["max_entradas_dia"],
                           "max_usdc": lim["max_usdc"], "day_loss_pct": round(max(0.0, -pct), 3),
                           "halted": bool(self.halt_until), "halt_until": self.halt_until or None},
                "mode": self.aprobacion_efectiva(), "halted": not self.puede_abrir(), "caps": list(CAPS), "cuenta": None,
                "real": {"modo": self.estado.modo, "demo": self.estado.demo, "opera": self.puede_abrir(),
                         "por_que": self._por_que_no()}}

    def vista_real(self, now: int | None = None) -> dict:
        """Lo que enseña el panel «Cuenta real» (mensaje cuenta_real y GET /api/real). Nunca lleva un valor de las
        claves: solo si el archivo existe y el modo."""
        now = self.clock() if now is None else now
        pnl, pct = self.day_pnl(now)
        # solo la confirmación que vale para las claves de ahora (una dada con otras claves no cuenta)
        conf = leer_confirmacion(self.carpeta, self.estado.claves.huella()) if self.estado.claves else None
        prices = self._prices(now)
        return limpiar({
            **self.estado.vista(), "etiqueta": self._etq(), "listo": self.listo, "puede_abrir": self.puede_abrir(),
            "por_que": self._por_que_no(), "bloqueo": self.bloqueo, "avisos": list(self.avisos),
            "parada": self.parada, "parando": self._parando, "halted": bool(self.halted), "halt_until": self.halt_until or None,
            "aprobacion": self.aprobacion_efectiva(),
            "archivos": {"carpeta": str(self.carpeta), "claves": (self.carpeta / ARCHIVO_CLAVES).exists(),
                         "confirmado": conf["fecha"] if conf else None, "parar": (self.carpeta / ARCHIVO_PARAR).exists()},
            "frase_confirmacion": FRASE_CONFIRMACION,
            "cuenta": {"equity": _f(self.cuenta.get("equity")), "usdc_disponible": _f(self.cuenta.get("usdc_disponible")),
                       "monedas": self.cuenta.get("monedas"), "acctLv": self.cuenta.get("acctLv"),
                       "posMode": self.cuenta.get("posMode"), "ip_atada": self.cuenta.get("ip"),
                       "permisos": self.cuenta.get("perm"), "saldo_at": self.cuenta.get("saldo_at"),
                       "day_pnl": round(pnl, 4), "day_pnl_pct": round(pct, 3), "inicio": self.inicio},
            "comisiones": dict(self.comisiones), "comisiones_creador": dict(self.comisiones_creador),
            "cortos": {"ok": self.cortos_ok, "por_que": self.cortos_por_que},
            "xperps": list(self.xperps) if self.xperps is not None else None,
            "broker": "real" if config.BROKER == "real" else "paper", "resumen": self.resumen(),
            "instrumentos": [i.vista() for i in self.instrumentos.values()],
            "limites": dict(self.limites), "techos": techos(), "entradas_hoy": self.entradas_hoy(now),
            "posiciones": [self._vista_pos(p, prices) for p in self.positions.values()],
            "sin_stop": [p["coin"] for p in self.positions.values()
                         if p.get("estado") == "enviando" or (p.get("estado") == "abierta" and self._sin_stop(p))],
            "apartadas": [self._vista_apartada(r) for r in self.apartadas.values()],
            "externas": list(self.externas), "pendientes": [{"story": i["story"], "coin": i["ticket"]["coin"],
                                                             "side": i["ticket"]["side"], "deadline": i.get("deadline"),
                                                             "mode": i.get("mode")} for i in self.pending.values()],
            "cerradas": list(reversed(self.closed[-20:])), "reconciliacion": self.reconciliacion,
            "ultimo_error": self.ultimo_error, "peticiones": self.cliente.peticiones if self.cliente else 0})

    def resumen(self) -> str:
        """Una línea para el panel: a quién van los tickets (BOT_BROKER) y si el modo real opera."""
        modo = self.estado.modo
        if config.BROKER != "real":
            if modo == APAGADO:
                return "Modo real apagado: el bot opera en paper"
            if modo == SOLO_LECTURA:
                return "Modo real en solo lectura: el bot opera en paper (solo lee tu cuenta de OKX)"
            return (f"Modo {'demo de OKX' if modo == DEMO else 'real'} listo, pero el bot opera en paper "
                    "(BOT_BROKER=paper: los tickets van al paper)")
        if self.puede_abrir():
            return "Operando en " + self._etq()
        return "Modo real sin operar: " + (self._por_que_no() or modo)

    def publish_cuenta(self, now: int | None = None, force: bool = False) -> bool:
        """Publica «cuenta_real» (no «portfolio»: ese es del paper) solo si ha cambiado algo."""
        v = self.vista_real(now)
        key = json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
        if key == self._pub_key and not force:
            return False
        self._pub_key = key
        self.bus.publish({"type": "cuenta_real", "ts": self.clock() if now is None else now, **v})
        return True

    publish_portfolio = publish_cuenta

    def results(self) -> dict:
        """Operaciones cerradas en OKX y agregados (formato de GET /api/resultados, contrato 3.2)."""
        start = self.inicio or self.equity()
        trades = sorted(self.closed, key=lambda t: t.get("closed") or 0)

        def agg(key: str) -> list[dict]:
            groups: dict[str, dict] = {}
            for t in trades:
                g = groups.setdefault(t.get(key) or "–", {"n": 0, "w": 0, "pnl": 0.0, "R": 0.0})
                g["n"] += 1
                g["w"] += (t.get("pnl") or 0.0) > 0
                g["pnl"] += t.get("pnl") or 0.0
                g["R"] += t.get("R") or 0.0
            rows = [{key: k, "n": g["n"], "win_rate": round(g["w"] / g["n"], 4), "pnl": round(g["pnl"], 4),
                     "avg_R": round(g["R"] / g["n"], 3)} for k, g in groups.items()]
            return sorted(rows, key=lambda r: -r["pnl"])

        eq, curve = start, [[trades[0]["opened"] if trades else self.clock(), round(start or 0.0, 4)]]
        for t in trades:
            eq = (eq or 0.0) + (t.get("pnl") or 0.0)
            curve.append([t["closed"], round(eq, 4)])
        days: dict[str, dict] = {}
        for t in trades:
            x = days.setdefault(paper._day(t["closed"]).isoformat(), {"pnl": 0.0, "n": 0})
            x["pnl"] += t.get("pnl") or 0.0
            x["n"] += 1
        daily, base = [], start or 0.0
        for day in sorted(days):
            x = days[day]
            daily.append({"date": day, "pnl": round(x["pnl"], 4), "pnl_pct": round(x["pnl"] / base * 100, 3) if base else 0.0,
                          "n": x["n"]})
            base += x["pnl"]
        return {"start_equity": start, "cuenta": None, "equity": round(self.equity(), 4), "trades": list(reversed(trades)),
                "by_strategy": agg("strategy"), "by_window": agg("window"), "by_coin": agg("coin"),
                "equity_curve": curve, "daily": daily, "real": True, "modo": self.estado.modo}
