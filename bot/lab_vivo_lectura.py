"""Lectura del laboratorio vivo para el servidor (docs/07, apartado 14; contrato en docs/04, 2.19 y 3).

El laboratorio vivo (bot/lab_vivo/) va en un proceso aparte. Este módulo NO lo importa ni abre lab_vivo.db: solo lee
los JSON que escribe en DATA_DIR/lab_vivo (o BOT_LAB_VIVO_DIR), con caché por mtime, y tolera que falten:
- estado.json (13.1), mejores.json, diario/AAAA-MM-DD.json (13.2), cola.json e hipotesis.json (13.4),
  hipotesis/<id>.json (detalle con la tarjeta de cada variante, 13.3) y generador/*.json (notas del agente generador).
Su único mando (14.3) crea o borra el archivo de control «pausa_manual»; el servidor solo lo deja pasar con el origen
estricto de la propia web (server.origen_web).

- vista(): GET /api/lab_vivo. Sin estado.json, {"estado": "apagado", "nota": …}; con el latido de hace más de 3 min,
  estado «parado» y el resto, lo último que hubo.
- resumen(): el mensaje del bus «lab_vivo» (14.2): listas recortadas a 5 y tarjetas sin «curva» ni «checks».
- run(): cada 30 s mira estado.json; si cambió (o pasa a parado, o cambia la pausa manual), publica el resumen. Sin
  archivo publica {"type": "lab_vivo", "estado": "apagado"} una sola vez.
- tarjetas(): todas las tarjetas que hay en los archivos (las usa el ranking de estrategias, bot/ranking.py).
"""
import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .models import now_ms

log = logging.getLogger("lab_vivo_lectura")
MAD = ZoneInfo("Europe/Madrid")
ID_RE = re.compile(r"^h-\d{8}-\d{4}-[a-z0-9_-]{3,60}$")   # id de una hipótesis (nada de rutas)
DIA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LATIDO_PARADO_S = 180       # un latido de hace más de 3 min: el corredor está parado (como el vigilante)
CADA_S = 30                 # cada cuánto mira estado.json la tarea del servidor
LISTA_BUS = 5               # tarjetas por lista en el mensaje del bus
MEJORES_API = 20
HIPOTESIS_MAX = 1000
GENERADOR_MAX = 50
ACCIONES = ("pausar", "reanudar")
NOTA_APAGADO = "El laboratorio vivo no está en marcha (scripts/lab-vivo.ps1)"


def carpeta() -> Path:
    """La carpeta del laboratorio vivo: BOT_LAB_VIVO_DIR o DATA_DIR/lab_vivo (como bot/lab_vivo/ajustes.py)."""
    return Path(os.environ.get("BOT_LAB_VIVO_DIR", str(config.DATA_DIR / "lab_vivo")))


def dia_madrid(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).date().isoformat()


def _hora(ms) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, MAD).strftime("%d-%m %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


class Lector:
    """JSON con caché por (mtime, tamaño): un archivo que no cambia no se vuelve a leer. Tolera que falte o que no se
    pueda leer (devuelve None). Se puede usar desde varios hilos."""

    def __init__(self):
        self._c: dict[str, tuple] = {}
        self._lock = threading.Lock()

    def leer(self, ruta: Path, extraer=None, cache: bool = True):
        """El JSON de ruta (o lo que saque de él extraer(d): así se guarda en la caché solo lo que hace falta y no el
        archivo entero). Con cache=False no se guarda nada (el detalle de una hipótesis, que pesa)."""
        k = str(ruta) + ("|x" if extraer else "")
        try:
            st = os.stat(ruta)
        except OSError:
            with self._lock:
                self._c.pop(k, None)
            return None
        clave = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._c.get(k)
        if hit and hit[0] == clave:
            return hit[1]
        try:
            d = json.loads(Path(ruta).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = None   # a medias o roto: se vuelve a intentar cuando cambie
        if extraer is not None:
            try:
                d = extraer(d)
            except Exception:
                d = None
        if cache:
            with self._lock:
                self._c[k] = (clave, d)
        return d

    def mtime(self, ruta: Path) -> int | None:
        try:
            return os.stat(ruta).st_mtime_ns
        except OSError:
            return None

    def olvidar_salvo(self, prefijo: str, vivos: set[str]) -> None:
        """Quita de la caché los archivos de una carpeta que ya no existen."""
        with self._lock:
            for k in [k for k in self._c if k.startswith(prefijo) and k.split("|")[0] not in vivos]:
                del self._c[k]


def _sin_curva(t) -> dict | None:
    """Tarjeta (13.3) sin «checks» y con el avance sin «curva» (el mensaje del bus va ligero)."""
    if not isinstance(t, dict):
        return None
    out = {k: v for k, v in t.items() if k != "checks"}
    if isinstance(out.get("avance"), dict):
        out["avance"] = {k: v for k, v in out["avance"].items() if k != "curva"}
    return out


def _tarjetas_del_detalle(d) -> tuple | None:
    """(ts, [tarjetas sin curva ni checks]) de un hipotesis/<id>.json: lo único que necesita el ranking."""
    if not isinstance(d, dict):
        return None
    tjs = [_sin_curva(v.get("tarjeta")) for v in d.get("variantes") or [] if isinstance(v, dict)]
    return d.get("ts"), [t for t in tjs if t]


class LabVivoLectura:
    def __init__(self, bus=None, dir: Path | None = None, clock=now_ms):
        self.bus = bus
        self._dir = Path(dir) if dir is not None else None
        self.clock = clock
        self.lector = Lector()
        self._clave_bus = None

    @property
    def dir(self) -> Path:
        return self._dir if self._dir is not None else carpeta()

    # ---------------------------------------------------------------- GET /api/lab_vivo y subrutas

    def _estado(self) -> dict | None:
        d = self.lector.leer(self.dir / "estado.json")
        return d if isinstance(d, dict) else None

    def parado(self, est: dict, now: int | None = None) -> bool:
        lat = est.get("latido", est.get("ts"))
        now = self.clock() if now is None else now
        return not isinstance(lat, (int, float)) or isinstance(lat, bool) or now - lat > LATIDO_PARADO_S * 1000

    def pausa_manual(self) -> bool:
        return (self.dir / "pausa_manual").exists()

    def vista(self) -> dict:
        """estado.json más «mejores» (de mejores.json, hasta 20) y «diario» (el de hoy, o null). Sin estado.json,
        «apagado»; con el latido de hace más de 3 min, «parado» y lo último que hubo."""
        est = self._estado()
        if est is None:
            return {"estado": "apagado", "nota": NOTA_APAGADO, "pausa_manual": self.pausa_manual()}
        out = dict(est)
        now = self.clock()
        if self.parado(est, now):
            out["estado_corredor"] = est.get("estado")
            out["estado"] = "parado"
            out["nota"] = (f"El laboratorio vivo no da señales desde las {_hora(est.get('latido', est.get('ts')))} "
                           "(más de 3 min): lo de abajo es lo último que escribió (scripts/lab-vivo.ps1 lo relanza)")
        mj = self.lector.leer(self.dir / "mejores.json")
        mejores = mj.get("mejores") if isinstance(mj, dict) else None
        if not isinstance(mejores, list):   # sin mejores.json (o roto), las de estado.json, si son una lista
            mejores = est.get("mejores") if isinstance(est.get("mejores"), list) else []
        out["mejores"] = [t for t in mejores[:MEJORES_API] if isinstance(t, dict)]
        dia = self.lector.leer(self.dir / "diario" / f"{dia_madrid(now)}.json")
        out["diario"] = dia if isinstance(dia, dict) else None
        out["pausa_manual"] = self.pausa_manual()
        return out

    def hipotesis(self, estado: str | None = None, familia: str | None = None, limite: int = 100) -> dict:
        """{hipotesis} de hipotesis.json, filtradas por estado y familia, como mucho «limite» (1 a 1.000)."""
        d = self.lector.leer(self.dir / "hipotesis.json")
        filas = d.get("hipotesis") if isinstance(d, dict) else None
        filas = [h for h in (filas if isinstance(filas, list) else []) if isinstance(h, dict)]
        if estado:
            filas = [h for h in filas if h.get("estado") == estado]
        if familia:
            filas = [h for h in filas if h.get("familia") == familia]
        limite = max(1, min(HIPOTESIS_MAX, int(limite)))
        out = {"hipotesis": filas[:limite], "n": len(filas)}
        if not isinstance(d, dict):
            out["nota"] = NOTA_APAGADO if self._estado() is None else "Aún no hay índice de hipótesis (hipotesis.json)"
        else:
            out["ts"] = d.get("ts")
        return out

    def hipotesis_detalle(self, hid: str) -> dict | None:
        """hipotesis/<id>.json, o None si no existe. El id lo valida el servidor con ID_RE (nada de rutas)."""
        if not ID_RE.fullmatch(hid or ""):
            return None
        d = self.lector.leer(self.dir / "hipotesis" / f"{hid}.json", cache=False)   # pesa: no se guarda en memoria
        return d if isinstance(d, dict) else None

    def diario(self, dia: str | None = None) -> dict | None:
        """diario/<dia>.json (por defecto, hoy en Madrid), o None si no hay."""
        dia = dia or dia_madrid(self.clock())
        if not DIA_RE.fullmatch(dia):
            return None
        d = self.lector.leer(self.dir / "diario" / f"{dia}.json")
        return d if isinstance(d, dict) else None

    def cola(self) -> dict:
        d = self.lector.leer(self.dir / "cola.json")
        if isinstance(d, dict):
            return d
        return {"estado": "apagado" if self._estado() is None else "sin_cola", "pendientes": 0, "por_tipo": {},
                "siguientes": [], "nota": NOTA_APAGADO if self._estado() is None else "Aún no hay cola.json"}

    def generador(self, limite: int = 10) -> dict:
        """Las últimas notas del agente generador (generador/AAAA-MM-DD-HHMM.json), de la más reciente a la más vieja."""
        car = self.dir / "generador"
        limite = max(1, min(GENERADOR_MAX, int(limite)))
        try:
            nombres = sorted((e.name for e in os.scandir(car) if e.is_file() and e.name.endswith(".json")
                              and not e.name.startswith((".", "_"))), reverse=True)
        except OSError:
            nombres = []
        notas = []
        for n in nombres[:limite]:
            d = self.lector.leer(car / n)
            if isinstance(d, dict):
                notas.append({"archivo": n, **d})
        return {"notas": notas, "n": len(nombres)}

    # ---------------------------------------------------------------- mensaje del bus (14.2)

    def resumen(self) -> dict:
        v = self.vista()
        if v.get("estado") == "apagado":
            return {"type": "lab_vivo", "estado": "apagado", "nota": NOTA_APAGADO, "pausa_manual": v.get("pausa_manual")}
        rec = v.get("recursos") if isinstance(v.get("recursos"), dict) else {}
        canario = ((v.get("codigo") or {}).get("canario") or {}) if isinstance(v.get("codigo"), dict) else {}
        out = {"type": "lab_vivo", "ts": v.get("ts"), "estado": v.get("estado"), "motivo": v.get("motivo"),
               "latido": v.get("latido"), "aviso": v.get("aviso"), "proxima_pausa": v.get("proxima_pausa") or {},
               "contadores": v.get("contadores") or {}, "embudo": v.get("embudo") or [], "meta": v.get("meta") or {},
               "canario": canario.get("estado") if isinstance(canario, dict) else None,
               "recursos": {"procesos": rec.get("procesos"), "memoria_libre_mb": rec.get("memoria_libre_mb")},
               "pausa_manual": v.get("pausa_manual")}
        for k in ("mejores", "en_avance", "listas"):
            lst = v.get(k) if isinstance(v.get(k), list) else []
            out[k] = [t for t in (_sin_curva(x) for x in lst[:LISTA_BUS]) if t]
        if v.get("nota"):
            out["nota"] = v["nota"]
        return out

    def _paso(self) -> tuple:
        """(clave, mensaje): la clave cambia si cambia estado.json, si pasa a parado o si cambia la pausa manual."""
        ruta = self.dir / "estado.json"
        mt = self.lector.mtime(ruta)
        pausa = self.pausa_manual()
        if mt is None:
            return ("apagado", pausa), None
        est = self._estado()
        clave = (mt, self.parado(est) if est else True, pausa)
        if clave == self._clave_bus:
            return clave, None
        return clave, self.resumen()

    def publicar(self, forzar: bool = False) -> bool:
        """Publica el resumen si cambió algo (o siempre con forzar). Sin bus, no hace nada."""
        if self.bus is None:
            return False
        clave, msg = self._paso()
        if forzar or clave != self._clave_bus:
            self._clave_bus = clave
            self.bus.publish(msg or self.resumen())
            return True
        return False

    async def run(self) -> None:
        while True:
            try:
                clave, msg = await asyncio.to_thread(self._paso)
                if clave != self._clave_bus and self.bus is not None:
                    self._clave_bus = clave
                    self.bus.publish(msg or await asyncio.to_thread(self.resumen))
            except Exception:
                log.exception("Laboratorio vivo: no pude leer o publicar su estado")
            await asyncio.sleep(CADA_S)

    # ---------------------------------------------------------------- mando (14.3)

    def mando(self, accion: str) -> dict:
        """pausar: crea «pausa_manual» (el corredor no empieza nada mientras exista); reanudar: lo borra. No hay más
        mandos (ni criterios, ni cupos, ni aprobar nada desde aquí)."""
        p = self.dir / "pausa_manual"
        if accion == "pausar":
            if not self.dir.is_dir():
                return {"ok": False, "why": "El laboratorio vivo no está instalado: no existe su carpeta"}
            if p.exists():
                return {"ok": True, "why": "Ya estaba en pausa"}
            try:
                p.write_text(f"Pausa pedida desde la web el {_hora(self.clock())}\n", encoding="utf-8")
            except OSError as e:
                return {"ok": False, "why": f"No pude crear pausa_manual ({str(e)[:120]})"}
            return {"ok": True, "why": None}
        if accion == "reanudar":
            if not p.exists():
                return {"ok": True, "why": "No estaba en pausa"}
            try:
                p.unlink()
            except OSError as e:
                return {"ok": False, "why": f"No pude borrar pausa_manual ({str(e)[:120]})"}
            return {"ok": True, "why": None}
        return {"ok": False, "why": "Acción del laboratorio vivo desconocida (pausar o reanudar)"}

    # ---------------------------------------------------------------- tarjetas para el ranking

    def tarjetas(self) -> dict:
        """Todas las tarjetas de variante (13.3) que hay en mejores.json, estado.json (mejores, en_avance y listas) e
        hipotesis/<id>.json, una por id (la del archivo con el «ts» más reciente), con el contexto que necesita el
        ranking: periodo de la época (datos.periodo), pruebas acumuladas (contadores.n) y el estado."""
        cards: dict[str, tuple] = {}

        def add(t, ts, archivo: str) -> None:
            if not isinstance(t, dict) or not isinstance(t.get("id"), str):
                return
            ts = ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0
            prev = cards.get(t["id"])
            if prev is None or ts >= prev[0]:
                cards[t["id"]] = (ts, t, archivo)

        est = self._estado()
        lista = (lambda d, k: d.get(k) if isinstance(d, dict) and isinstance(d.get(k), list) else [])
        mj = self.lector.leer(self.dir / "mejores.json")
        for t in lista(mj, "mejores"):
            add(t, mj.get("ts"), "lab_vivo/mejores.json")
        for k in ("mejores", "en_avance", "listas"):
            for t in lista(est, k):
                add(t, est.get("ts"), "lab_vivo/estado.json")
        car = self.dir / "hipotesis"
        vivos = set()
        try:
            nombres = sorted(e.name for e in os.scandir(car) if e.is_file() and e.name.endswith(".json"))
        except OSError:
            nombres = []
        for n in nombres:
            ruta = car / n
            vivos.add(str(ruta))
            # del detalle (con hasta 200 operaciones por variante) solo se guardan las tarjetas, sin curva ni checks
            ts, tjs = self.lector.leer(ruta, extraer=_tarjetas_del_detalle) or (None, [])
            for t in tjs:
                add(t, ts, f"lab_vivo/hipotesis/{n}")
        self.lector.olvidar_salvo(str(car) + os.sep, vivos)   # con el separador: hipotesis.json no es de la carpeta
        datos = (est or {}).get("datos") if isinstance((est or {}).get("datos"), dict) else {}
        cont = (est or {}).get("contadores") if isinstance((est or {}).get("contadores"), dict) else {}
        n_cont = cont.get("n") if isinstance(cont.get("n"), dict) else {}
        return {"tarjetas": [(t, ts, a) for ts, t, a in cards.values()],
                "periodo": datos.get("periodo") if isinstance(datos.get("periodo"), dict) else None,
                "probadas": n_cont.get("total"),
                "estado": ("apagado" if est is None else "parado" if self.parado(est) else est.get("estado")),
                "ts": (est or {}).get("ts"), "carpeta": str(self.dir)}
