"""Ajustes del laboratorio vivo: variables BOT_LAB_VIVO_*, rutas y límites (docs/07, 11.10). config.py no se toca.

Todo lo que escribe el laboratorio va a M:\\BotFundamental-datos\\lab_vivo (DATA_DIR\\lab_vivo), nunca a C:. Este
módulo no importa numpy: lo usan también el corredor antes de fijar los hilos de BLAS y las órdenes ligeras."""
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config

MAD = ZoneInfo("Europe/Madrid")
MIN, H, DAY = 60_000, 3_600_000, 86_400_000


def _int(nombre: str, defecto: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        v = int(os.environ.get(nombre, defecto))
    except (TypeError, ValueError):
        v = defecto
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


ACTIVO = os.environ.get("BOT_LAB_VIVO", "1") != "0"
DIR = Path(os.environ.get("BOT_LAB_VIVO_DIR", str(config.DATA_DIR / "lab_vivo")))
PROCESOS = _int("BOT_LAB_VIVO_PROCESOS", 2, 1, 3)          # el segundo y el tercero, solo con ≥ 4 GB libres
CPUS = tuple(int(x) for x in os.environ.get("BOT_LAB_VIVO_CPUS", "3,4,5").replace(";", ",").split(",")
             if x.strip().isdigit()) or (3, 4, 5)
MEMORIA_MB = _int("BOT_LAB_VIVO_MEMORIA_MB", 2500, 300)
MIN_LIBRE_MB = _int("BOT_LAB_VIVO_MIN_LIBRE_MB", 1500, 0)
MATAR_LIBRE_MB = 800                                        # por debajo se mata la tarea más reciente
SEGUNDO_LIBRE_MB = 4096                                     # el segundo proceso solo con esta memoria libre
CPU_FRENO_PCT, CPU_FRENO_S = 90.0, 60                       # CPU de todo el PC: con más de 90 % durante 60 s, no se empieza nada
CUPO_DIA = _int("BOT_LAB_VIVO_CUPO_DIA", 24, 0)             # hipótesis que empiezan la etapa rápida en un día de Madrid
CUPO_PASADA = _int("BOT_LAB_VIVO_CUPO_PASADA", 6, 0)        # hipótesis por pasada del generador
CUPO_FAMILIA = 8                                            # hipótesis por familia y día
API = os.environ.get("BOT_LAB_VIVO_API", "http://127.0.0.1:8765")   # fase 2: noticias en vivo

# Pausas en las aperturas: las pidió el usuario (no se pueden cambiar con variables)
PAUSA_ANTES_MIN, PAUSA_DESPUES_MIN = 5, 30
NO_EMPEZAR_MIN = 3        # a menos de esto de una pausa no se empieza ningún trabajo
MATAR_ANTES_MIN = 3       # en T − 3 min la tarea que quede se mata y vuelve a la cola desde su último punto
AGENDA_DIAS = 7

BUCLE_S = 5               # bucle del supervisor
ESTADO_S = 30             # estado.json cada 30 s o al cambiar algo
LATIDO_PARADO_S = 180     # el servidor y el vigilante dan por parado un latido de hace más de 3 min
MAX_INTENTOS = 3
ESPERA_CODIGO_S = 600     # código del bot que no se importa: 10 min sin gastar intentos
CODIGO_PAUSADO = 75       # código de salida de una tarea que se pausa de forma cooperativa
CODIGO_NO_IMPORTABLE = 76  # código de salida de una tarea si el código del bot no se importa (otro flujo lo edita)

# Periodo y corte (4.2): época = mes; 3 años hasta el último día del mes anterior; corte fijo el 16-08-2025 (Madrid)
CORTE_DIA = "2025-08-16"
CORTE_MS = int(datetime(2025, 8, 16, tzinfo=MAD).timestamp() * 1000)
ANOS = 3
COLA_DATOS_D = 15         # días de velas tras el fin del periodo para que acaben las operaciones de los últimos días
CALENTAMIENTO_MAX_D = 430  # calentamiento máximo que puede pedir una familia (el régimen cripto pide 420 días)

# Rutas (todas dentro de DIR)
DB = DIR / "lab_vivo.db"
COLA = DIR / "cola"
ENTRADA, ACEPTADAS, RECHAZADAS = COLA / "entrada", COLA / "aceptadas", COLA / "rechazadas"
TRABAJOS = DIR / "trabajos"
CACHE = DIR / "cache"
CACHE_VIVO = CACHE / "vivo"
REGISTRO = DIR / "registro"
TMP = DIR / "tmp"
DIARIO = DIR / "diario"
GENERADOR = DIR / "generador"
PROPUESTAS = DIR / "propuestas"
DECISIONES = DIR / "decisiones"
HIPOTESIS_DIR = DIR / "hipotesis"
PARAR = DIR / "parar"
PAUSA_MANUAL = DIR / "pausa_manual"
PAUSA_AHORA = DIR / "pausa.ahora"
LOCK = DIR / "corredor.lock"
ESTADO = DIR / "estado.json"
AGENDA = DIR / "agenda.json"
CONSOLIDAR_AHORA = DIR / "consolidar.ahora"   # orden «consolidar» con el corredor en marcha: la hace él (único escritor)
HEREDAR_N = DIR / "heredar_n.json"            # orden «heredar-n» con el corredor en marcha (registro_n.py)

AVISO = ("Laboratorio de investigación: nada de esto opera. Lo que aguanta pasa a una prueba en avance VIRTUAL y a la "
         "cartera paper solo si tú lo decides.")

CARPETAS = (ENTRADA, ACEPTADAS, RECHAZADAS, TRABAJOS, CACHE, CACHE_VIVO, REGISTRO, TMP, DIARIO, GENERADOR, PROPUESTAS,
            DECISIONES, HIPOTESIS_DIR)


def preparar_carpetas() -> None:
    for p in CARPETAS:
        p.mkdir(parents=True, exist_ok=True)


def recargar() -> None:
    """Vuelve a leer las variables de entorno (las pruebas cambian BOT_LAB_VIVO_DIR con monkeypatch)."""
    g = globals()
    g["ACTIVO"] = os.environ.get("BOT_LAB_VIVO", "1") != "0"
    d = Path(os.environ.get("BOT_LAB_VIVO_DIR", str(config.DATA_DIR / "lab_vivo")))
    g["DIR"] = d
    g["PROCESOS"] = _int("BOT_LAB_VIVO_PROCESOS", 2, 1, 3)
    g["MEMORIA_MB"] = _int("BOT_LAB_VIVO_MEMORIA_MB", 2500, 300)
    g["MIN_LIBRE_MB"] = _int("BOT_LAB_VIVO_MIN_LIBRE_MB", 1500, 0)
    g["CUPO_DIA"] = _int("BOT_LAB_VIVO_CUPO_DIA", 24, 0)
    g["CUPO_PASADA"] = _int("BOT_LAB_VIVO_CUPO_PASADA", 6, 0)
    g["DB"] = d / "lab_vivo.db"
    g["COLA"] = d / "cola"
    g["ENTRADA"], g["ACEPTADAS"], g["RECHAZADAS"] = d / "cola" / "entrada", d / "cola" / "aceptadas", d / "cola" / "rechazadas"
    for k, sub in (("TRABAJOS", "trabajos"), ("CACHE", "cache"), ("REGISTRO", "registro"), ("TMP", "tmp"),
                   ("DIARIO", "diario"), ("GENERADOR", "generador"), ("PROPUESTAS", "propuestas"),
                   ("DECISIONES", "decisiones"), ("HIPOTESIS_DIR", "hipotesis")):
        g[k] = d / sub
    g["CACHE_VIVO"] = d / "cache" / "vivo"
    g["PARAR"], g["PAUSA_MANUAL"], g["PAUSA_AHORA"] = d / "parar", d / "pausa_manual", d / "pausa.ahora"
    g["LOCK"], g["ESTADO"], g["AGENDA"] = d / "corredor.lock", d / "estado.json", d / "agenda.json"
    g["CONSOLIDAR_AHORA"], g["HEREDAR_N"] = d / "consolidar.ahora", d / "heredar_n.json"
    g["CARPETAS"] = (g["ENTRADA"], g["ACEPTADAS"], g["RECHAZADAS"], g["TRABAJOS"], g["CACHE"], g["CACHE_VIVO"],
                     g["REGISTRO"], g["TMP"], g["DIARIO"], g["GENERADOR"], g["PROPUESTAS"], g["DECISIONES"],
                     g["HIPOTESIS_DIR"])


def entorno_tareas() -> dict:
    """Variables de entorno de cada tarea: hilos de BLAS en 1 y temporales en lab_vivo\\tmp (en M:, nunca en C:)."""
    env = dict(os.environ)
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[k] = "1"
    env["TMP"] = env["TEMP"] = env["TMPDIR"] = str(TMP)
    env["PYTHONIOENCODING"] = "utf-8"
    env["BOT_LAB_VIVO_DIR"] = str(DIR)
    return env


def ahora_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def dia_mad(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).date().isoformat()


def hora_mad(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).strftime("%H:%M")


def fecha_mad(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).strftime("%d-%m-%Y %H:%M")
