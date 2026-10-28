"""Salud del proceso para la prueba de 24-72 h sin nadie delante (hallazgo n.º 40): memoria, handles, retraso del bucle
de asyncio y, cada 5 min, una fila en DATA_DIR/logs/salud.csv con eso y lo principal de /health (tiempo en marcha, cola
de la IA local, base y WAL, fuentes, reloj). Solo mide y apunta: no toca nada del bot."""
import asyncio
import csv
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("salud")
MAD = ZoneInfo("Europe/Madrid")
CADA_S = 300
LAG_CADA_S = 1.0             # el retraso del bucle se mide con una espera de 1 s
COLUMNAS = ("hora_madrid", "en_marcha_s", "mem_mb", "privada_mb", "handles", "hilos", "retraso_bucle_ms",
            "retraso_bucle_max_ms", "ia_cola", "ia_hechas", "ia_sin_triar", "db_mb", "wal_mb", "feeds_ok", "feeds_total",
            "clientes_web", "reloj_desfase_ms")


def _windows(k32=None, psapi=None) -> dict | None:
    """Memoria (working set y privada) y handles del propio proceso con la API de Windows (ctypes, sin dependencias)."""
    import ctypes

    class Contadores(ctypes.Structure):   # PROCESS_MEMORY_COUNTERS_EX
        _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                                           "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]

    if k32 is None:
        from ctypes import wintypes
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32))
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_uint32)
    yo = k32.GetCurrentProcess()
    c = Contadores()
    c.cb = ctypes.sizeof(c)
    if not psapi.GetProcessMemoryInfo(yo, ctypes.byref(c), c.cb):
        return None
    n = ctypes.c_uint32()
    handles = n.value if k32.GetProcessHandleCount(yo, ctypes.byref(n)) else None
    return {"mem_mb": round(c.WorkingSetSize / 1e6, 1), "privada_mb": round(c.PrivateUsage / 1e6, 1), "handles": handles}


def _linux(base: Path = Path("/proc/self")) -> dict | None:
    """Lo mismo en Linux (Claude en la nube): /proc/self/status y los descriptores abiertos."""
    try:
        campos = dict(line.split(":", 1) for line in (base / "status").read_text().splitlines() if ":" in line)
    except OSError:
        return None
    kb = lambda k: int(campos[k].split()[0]) if k in campos else None
    rss, priv = kb("VmRSS"), kb("RssAnon")
    try:
        handles = len(os.listdir(base / "fd"))
    except OSError:
        handles = None
    return {"mem_mb": None if rss is None else round(rss * 1024 / 1e6, 1),
            "privada_mb": None if priv is None else round(priv * 1024 / 1e6, 1), "handles": handles}


def proceso() -> dict:
    """{mem_mb, privada_mb, handles, hilos} del proceso del bot; None en lo que no se pueda leer."""
    try:
        out = _windows() if os.name == "nt" else _linux()
    except Exception:
        log.debug("No se pudo leer la memoria del proceso", exc_info=True)
        out = None
    import threading
    return {**(out or {"mem_mb": None, "privada_mb": None, "handles": None}), "hilos": threading.active_count()}


def fila(h: dict, retraso: dict, ahora: float | None = None) -> dict:
    """Fila de salud.csv a partir de /health (con «proceso») y del retraso del bucle."""
    p = h.get("proceso") or {}
    ia, db, fe = h.get("local_ai") or {}, h.get("db") or {}, (h.get("feeds") or {}).get("totals") or {}
    t = datetime.fromtimestamp(time.time() if ahora is None else ahora, MAD)
    return {"hora_madrid": t.strftime("%Y-%m-%d %H:%M:%S"), "en_marcha_s": h.get("en_marcha_s"),
            "mem_mb": p.get("mem_mb"), "privada_mb": p.get("privada_mb"), "handles": p.get("handles"),
            "hilos": p.get("hilos"), "retraso_bucle_ms": retraso.get("ms"), "retraso_bucle_max_ms": retraso.get("max_ms"),
            "ia_cola": ia.get("queue"), "ia_hechas": ia.get("done"), "ia_sin_triar": ia.get("dropped"),
            "db_mb": db.get("mb"), "wal_mb": db.get("wal_mb"), "feeds_ok": fe.get("ok"), "feeds_total": fe.get("feeds"),
            "clientes_web": h.get("clients"), "reloj_desfase_ms": (h.get("reloj") or {}).get("desfase_ms")}


def apuntar(path: Path, row: dict) -> None:
    """Añade la fila (con cabecera si el archivo es nuevo). ; como separador y coma decimal: se abre tal cual en Excel."""
    nuevo = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS, delimiter=";")
        if nuevo:
            w.writeheader()
        w.writerow({k: (str(v).replace(".", ",") if isinstance(v, float) else ("" if v is None else v))
                    for k, v in row.items()})


class Salud:
    """Mide el retraso del bucle (cuánto se pasa una espera de 1 s: si algo bloquea el bucle, crece) y cada 5 min
    apunta una fila en salud.csv con health() (la función de /health)."""

    def __init__(self, health, path: Path, clock=time.monotonic, sleep=asyncio.sleep):
        self.health, self.path, self.clock, self.sleep = health, Path(path), clock, sleep
        self.retraso = {"ms": None, "max_ms": None}
        self._max = 0.0

    async def run(self) -> None:
        prox = self.clock() + CADA_S
        while True:
            t0 = self.clock()
            await self.sleep(LAG_CADA_S)
            lag = max(0.0, (self.clock() - t0 - LAG_CADA_S) * 1000)
            self._max = max(self._max, lag)
            self.retraso = {"ms": round(lag, 1), "max_ms": round(self._max, 1)}
            if self.clock() >= prox:
                prox = self.clock() + CADA_S
                try:
                    await asyncio.to_thread(apuntar, self.path, fila(self.health(), self.retraso))
                except Exception:
                    log.exception("No se pudo apuntar la salud en %s", self.path)
                self._max = 0.0   # el máximo es el de cada tramo de 5 min
