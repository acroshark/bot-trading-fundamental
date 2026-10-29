"""El corredor del laboratorio vivo (docs/07, apartado 11): un supervisor ligero, 24/7, en un proceso aparte y con
prioridad baja. Es el único que escribe en lab_vivo.db.

Bucle (cada 5 s): mandos (parar, pausa_manual, decisiones del usuario) → pausa por las aperturas y recursos → vigila
las tareas (tiempos, pausas, memoria) → ingiere los resultados → admite las hipótesis nuevas del buzón → planifica y
lanza trabajos en los huecos libres (cada uno en su subproceso) → tareas con hora (avance, consolidación, diario,
cambio de época y canario) → estado.json.

Nada de esto opera: lo que aguanta todas las etapas pasa a una prueba en avance VIRTUAL y, si la supera, se propone al
usuario por la pestaña Evolución (propuestas\\). Nada pasa solo a la cartera paper."""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .. import config
from . import NOMBRE, agenda, ajustes, almacen, criterios, previas, recursos, salida_web

log = logging.getLogger("lab_vivo.corredor")
MIN, H, DAY = 60_000, 3_600_000, 86_400_000
RAIZ = Path(__file__).resolve().parents[2]
PRIO = {"datos": 1, "canario": 1, "admision": 2, "avance": 3, "robustez": 4, "completa": 5, "rapida": 6}
MAX_S = {"datos": 1800, "canario": 1800, "admision": 1200, "rapida": 900, "completa": 1800, "robustez": 1800,
         "avance": 900}
MEDIDA = ("rapida", "completa", "robustez", "avance")
EN_MARCHA = ("admitida", "en_admision", "en_rapida", "en_completa", "en_robustez")   # hipótesis con etapas por hacer
MODULOS_BOT = ("apertura", "paper", "comisiones", "config", "laboratorio", "backtest_aperturas", "historia_extra",
               "evolucion", "vigilancia", "regions", "backtest", "history")
ORDEN = "bot.lab_vivo correr"      # lo que busca el vigilante en la línea de órdenes del PID del lock (16)
PESADO_MAX_S = 600                 # las tarjetas de estado.json se recalculan al cambiar algo o cada 10 min (nunca en pausa)


def firma_codigo() -> str:
    """Sha de bot/lab_vivo/** y de los módulos del bot que usa (11.8): si cambia, se vuelve a pasar el canario."""
    h = hashlib.sha1()
    base = RAIZ / "bot"
    files = sorted((base / "lab_vivo").rglob("*.py")) + [base / f"{m}.py" for m in MODULOS_BOT]
    for p in files:
        try:
            h.update(p.relative_to(RAIZ).as_posix().encode())
            h.update(p.read_bytes())
        except OSError:
            h.update(b"falta")
    return h.hexdigest()[:12]


def configurar_registro() -> None:
    ajustes.REGISTRO.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(hh, RotatingFileHandler) for hh in root.handlers):
        return
    fh = RotatingFileHandler(ajustes.REGISTRO / "corredor.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    root.setLevel(logging.INFO)


class Corredor:
    def __init__(self, procesos: int | None = None, sin_pausas: bool = False, reloj=None, lanzador=None,
                 openings_on=None, memoria=None, cpu=None, una_pasada: bool = False, max_horas: float = 24.0):
        ajustes.preparar_carpetas()
        self.una_pasada = una_pasada      # correr --una-pasada: todo lo que haya en la cola, una vez, y se para
        self.max_horas = max_horas
        self.con = almacen.conectar()
        self.reloj = reloj or ajustes.ahora_ms
        self.procesos_max = min(3, procesos or ajustes.PROCESOS)
        self.sin_pausas = sin_pausas
        self.lanzador = lanzador or self._lanzar_subproceso
        self.openings_on = openings_on
        self.memoria = memoria or recursos.memoria_libre_mb
        self.cpu = cpu or recursos.Cpu().pct
        self.procs: dict[str, dict] = {}
        self.arranque = self.reloj()
        self.estado = "arrancando"
        self.motivo = None
        self.errores: list[dict] = []
        self.diario_lineas: list[str] = []
        self.tramos, self.agenda_origen, self.agenda_ts = [], None, 0
        self.ultimo_estado = 0
        self.cpu_alta_desde = None
        self.espera_codigo_hasta = 0
        self.firma_cod = firma_codigo()
        self.recursos_info: dict = {}
        self.cambios = True
        self._repetidos: dict[str, int] = {}
        self._pesado: dict | None = None      # tarjetas, embudo, contadores y meta de estado.json (salida_web.pesado)
        self._pesado_ts = 0
        self._cola: dict | None = None
        self.bloqueo_n = False                # la base tiene menos pruebas que n_acumulado.jsonl: no se mide nada

    # ---------------------------------------------------------------- arranque y parada
    def tomar_lock(self) -> bool:
        """corredor.lock con el PID, la hora y la hora de creación del proceso. Un lock viejo (el corredor murió sin
        soltarlo: apagón, reinicio, taskkill) cuyo PID ha reutilizado Windows para otro proceso NO es un corredor vivo:
        se reconoce por la hora de creación (recursos.mismo_proceso) y se reemplaza."""
        p = ajustes.LOCK
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if (d.get("pid") and d["pid"] != os.getpid()
                    and recursos.mismo_proceso(d["pid"], d.get("creado"), d.get("ts"))):
                log.warning("Ya hay un corredor vivo (PID %s): salgo", d["pid"])
                return False
            if d.get("pid") and d["pid"] != os.getpid() and recursos.pid_vivo(d["pid"]):
                log.warning("corredor.lock viejo: el PID %s es ahora de otro proceso (no se toca); lo reemplazo", d["pid"])
        except (OSError, ValueError):
            pass
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "ts": self.reloj(), "creado": recursos.inicio_proceso(os.getpid()),
                                   "orden": ORDEN}), encoding="utf-8")
        os.replace(tmp, p)
        return True

    def soltar_lock(self) -> None:
        try:
            d = json.loads(ajustes.LOCK.read_text(encoding="utf-8"))
            if d.get("pid") == os.getpid():
                ajustes.LOCK.unlink()
        except (OSError, ValueError):
            pass

    def arrancar(self) -> bool:
        if not config.USER_RULES or abs(float(config.RISK_PER_TRADE_PCT) - 0.5) > 1e-9:
            self.estado = "error"
            self.motivo = ("Las reglas del usuario tienen que estar activas (BOT_USER_RULES) y el riesgo por operación en "
                           "0,5 %: así no se mide nada")
            log.error(self.motivo)
            return False
        with self.con:
            previas.sembrar(self.con)
            self._registrar_familias()
            # Reanudación: los trabajos en curso vuelven a la cola desde su punto (sus procesos murieron con el Job Object)
            for r in self.con.execute("SELECT id FROM trabajos WHERE estado = 'en_curso'").fetchall():
                self.con.execute("UPDATE trabajos SET estado = 'pendiente', intentos = intentos + 1, pid = NULL WHERE id = ?",
                                 (r[0],))
                almacen.evento(self.con, "reanudacion", f"Trabajo {r[0]} reanudado tras una caída", r[0])
        for p in ajustes.TRABAJOS.glob("*.tmp"):
            try:
                p.unlink()
            except OSError:
                pass
        self.estado = "activo"
        self._comprobar_n("arranque")
        return True

    # ---------------------------------------------------------------- el N acumulado fuera de la base (registro_n.py)
    def _comprobar_n(self, motivo: str) -> bool:
        """Con la base por debajo de n_acumulado.jsonl no se mide nada (estado «error», pero el corredor sigue vivo
        para que el vigilante no lo relance sin parar y para atender la orden heredar-n). Si cuadra, se apunta."""
        from . import registro_n
        with self.con:
            mot = registro_n.comprobar(self.con)
            if mot:
                if not self.bloqueo_n:
                    almacen.evento(self.con, "error", mot)
                    log.error(mot)
                self.bloqueo_n, self.estado, self.motivo = True, "error", mot
                self.cambios = True
                return False
            registro_n.apuntar(self.con, motivo)
        if self.bloqueo_n:
            self.bloqueo_n, self.estado, self.motivo = False, "activo", None
            with self.con:
                self.nota("El número de pruebas vuelve a cuadrar con n_acumulado.jsonl: se reanuda la medida", "herencia_n")
        return True

    def aplicar_herencia(self, nota: str, por: str = "usuario") -> dict | None:
        """La orden heredar-n del usuario (registro_n.heredar), con el N efectivo que ya cuenta esta base."""
        from . import registro_n
        st = self.estadistica_global()
        with self.con:
            r = registro_n.heredar(self.con, nota, por, n_lab_actual=st["n_efectivo"])
        self._comprobar_n("herencia")
        return r

    def _registrar_familias(self) -> None:
        from .familias import cargar, sha_archivo
        for n, f in cargar(forzar=True).items():
            sha = sha_archivo(n)
            r = self.con.execute("SELECT sha FROM familias WHERE nombre = ? AND version = ?", (n, f.version)).fetchone()
            if r is None:
                self.con.execute("INSERT INTO familias(nombre, version, sha, alta) VALUES (?, ?, ?, ?)",
                                 (n, f.version, sha, self.reloj()))
            elif r[0] != sha:
                self._nota_rep(f"fam:{n}", f"Familia {n} v{f.version} cambiada sin subir versión: no se usa hasta que "
                                           "suba la versión (sus resultados no serían comparables)")

    def familias_db(self) -> dict:
        out = {}
        for n, v, s in self.con.execute("SELECT nombre, version, sha FROM familias"):
            out.setdefault(n, {})[v] = s
        return out

    # ---------------------------------------------------------------- utilidades
    def _nota_rep(self, clave: str, texto: str) -> None:
        """Una traza que se repite sale como mucho una vez cada 5 min."""
        now = self.reloj()
        if now - self._repetidos.get(clave, 0) >= 5 * MIN:
            self._repetidos[clave] = now
            log.warning(texto)

    def nota(self, texto: str, tipo: str = "nota", sujeto: str | None = None, guardar: bool = True) -> None:
        log.info(texto)
        self.diario_lineas.append(f"{ajustes.hora_mad(self.reloj())} {texto}")
        self.diario_lineas = self.diario_lineas[-200:]
        if guardar:
            almacen.evento(self.con, tipo, texto, sujeto)
        self.cambios = True

    def error(self, texto: str, sujeto: str | None = None) -> None:
        log.error(texto)
        self.errores.append({"ts": self.reloj(), "texto": texto})
        self.errores = self.errores[-20:]
        try:
            with self.con:
                almacen.evento(self.con, "error", texto, sujeto)
        except Exception:
            pass
        self.cambios = True

    def epoca(self) -> str:
        from . import datos
        return datos.epoca_de(self.reloj())

    def cache_epoca(self) -> Path | None:
        from . import datos
        return datos.cache_existente(self.epoca())

    def canario_ok(self) -> bool:
        c = almacen.kv_get(self.con, "canario") or {}
        return c.get("estado") == "ok" and c.get("firma_codigo") == self.firma_cod

    # ---------------------------------------------------------------- pausas y recursos
    def actualizar_agenda(self, now: int) -> None:
        if now - self.agenda_ts > H or not self.tramos:
            self.tramos, self.agenda_origen = agenda.obtener(now, self.openings_on)
            self.agenda_ts = now

    def en_pausa(self, now: int) -> dict | None:
        if self.sin_pausas:
            return None
        return agenda.en_pausa(now, self.tramos)

    def gestionar_pausa(self, now: int) -> None:
        """T − 5 min: pausa.ahora (las tareas salen solas con el código 75); T − 3 min: la que quede se mata y vuelve a
        la cola desde su punto, sin gastar intento. Tras la pausa, se borra pausa.ahora."""
        x = self.en_pausa(now)
        if x:
            if not ajustes.PAUSA_AHORA.exists():
                ajustes.PAUSA_AHORA.write_text(json.dumps({"desde": x["desde"], "hasta": x["hasta"]}), encoding="utf-8")
                self.nota(f"Pausa por la apertura de {x['nombre']} hasta las {ajustes.hora_mad(x['hasta'])}",
                          "pausa", guardar=False)
            if now >= x["desde"] + (ajustes.PAUSA_ANTES_MIN - ajustes.MATAR_ANTES_MIN) * MIN:
                for tid in list(self.procs):
                    self._matar(tid, "pausa: se mata en T − 3 min y vuelve a la cola desde su punto", gastar=False)
        elif ajustes.PAUSA_AHORA.exists():
            try:
                ajustes.PAUSA_AHORA.unlink()
            except OSError:
                pass

    def puede_empezar(self, now: int) -> tuple[bool, str | None]:
        if ajustes.PARAR.exists():
            return False, "Parada pedida (archivo parar)"
        if ajustes.PAUSA_MANUAL.exists():
            return False, "Pausa manual (archivo pausa_manual)"
        if not self.sin_pausas:
            ok, why = agenda.puede_empezar(now, self.tramos)
            if not ok:
                return False, why
        libre = self.memoria()
        if libre is not None and libre < ajustes.MIN_LIBRE_MB:
            return False, f"Memoria libre {libre:.0f} MB < {ajustes.MIN_LIBRE_MB} MB: no se empieza nada"
        if self.cpu_alta_desde and now - self.cpu_alta_desde >= ajustes.CPU_FRENO_S * 1000:
            return False, f"CPU del PC por encima del {ajustes.CPU_FRENO_PCT:.0f} % desde hace más de 1 min"
        return True, None

    def huecos(self) -> int:
        n = len(self.procs)
        if n == 0:
            return 1
        libre = self.memoria()
        if n < self.procesos_max and libre is not None and libre >= ajustes.SEGUNDO_LIBRE_MB:
            return self.procesos_max - n
        return 0

    def vigilar_recursos(self, now: int) -> None:
        pct = self.cpu()
        if pct is not None and pct > ajustes.CPU_FRENO_PCT:
            self.cpu_alta_desde = self.cpu_alta_desde or now
        else:
            self.cpu_alta_desde = None
        libre = self.memoria()
        if libre is not None and libre < ajustes.MATAR_LIBRE_MB and self.procs:
            tid = max(self.procs, key=lambda k: self.procs[k]["t0"])
            self._matar(tid, f"memoria libre {libre:.0f} MB < {ajustes.MATAR_LIBRE_MB} MB: el bot y la IA local van "
                             "primero", gastar=False)
        mem = 0.0
        for tid, x in self.procs.items():
            mm = recursos.memoria_proceso_mb(x["pid"]) if x.get("pid") else None
            mem += mm or 0.0
        self.recursos_info = {"procesos": len(self.procs), "procesos_max": self.procesos_max, "cpus": list(ajustes.CPUS),
                              "prioridad": "por debajo de lo normal", "memoria_libre_mb": round(libre) if libre else None,
                              "memoria_limite_mb": ajustes.MEMORIA_MB, "memoria_usada_mb": round(mem),
                              "cpu_pc_pct": pct,
                              "limitado_por": ("pausa" if self.en_pausa(now) else "memoria" if libre is not None and
                                               libre < ajustes.MIN_LIBRE_MB else "cpu" if self.cpu_alta_desde else None)}

    # ---------------------------------------------------------------- trabajos
    def encolar(self, tipo: str, objetivo: str | None, payload: dict, prioridad_hip: int = 3) -> str | None:
        """Crea el trabajo si no existe ya (id determinista: objetivo + tipo + firmas). Devuelve su id."""
        key = json.dumps({"t": tipo, "o": objetivo, "fd": payload.get("firma_datos"), "fc": self.firma_cod,
                          "x": payload.get("clave_extra")}, sort_keys=True)
        tid = f"{tipo}-{hashlib.sha1(key.encode()).hexdigest()[:14]}"
        r = self.con.execute("SELECT estado FROM trabajos WHERE id = ?", (tid,)).fetchone()
        if r is not None and r[0] in ("pendiente", "en_curso", "hecho"):
            return tid
        tr = {"id": tid, "tipo": tipo, "objetivo": objetivo, "epoca": self.epoca(), "firma_codigo": self.firma_cod,
              "criterios": criterios.VERSION, "criterios_sha": criterios.SHA, "creado": self.reloj(), **payload}
        p = ajustes.TRABAJOS / f"{tid}.trabajo.json"
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(tr, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, p)
        prio = PRIO.get(tipo, 7) * 10 + max(1, min(5, int(prioridad_hip)))
        self.con.execute("INSERT OR REPLACE INTO trabajos(id, tipo, objetivo, prioridad, estado, intentos, creado) "
                         "VALUES (?, ?, ?, ?, 'pendiente', COALESCE((SELECT intentos FROM trabajos WHERE id = ?), 0), ?)",
                         (tid, tipo, objetivo, prio, tid, self.reloj()))
        self.cambios = True
        return tid

    def _lanzar_subproceso(self, tid: str) -> dict:
        logf = open(ajustes.TRABAJOS / f"{tid}.log", "a", encoding="utf-8")
        flags = 0
        if os.name == "nt":
            flags = 0x08000000 | recursos.BELOW_NORMAL   # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
        p = subprocess.Popen([recursos.python(), "-X", "utf8", "-m", "bot.lab_vivo", "tarea", tid], cwd=str(RAIZ),
                             env=ajustes.entorno_tareas(), stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)
        return {"popen": p, "pid": p.pid, "log": logf}

    def siguientes(self) -> list[dict]:
        """Por prioridad del tipo, prioridad de la hipótesis, turno entre familias y orden de llegada."""
        filas = [dict(r) for r in self.con.execute("SELECT * FROM trabajos WHERE estado = 'pendiente' ORDER BY prioridad, creado")]
        hoy = ajustes.dia_mad(self.reloj())
        usadas = Counter()
        for r in self.con.execute("SELECT objetivo, empezado FROM trabajos WHERE empezado IS NOT NULL"):
            if r[1] and ajustes.dia_mad(r[1]) == hoy:
                usadas[self._familia_de(r[0])] += 1
        bloqueo_medida = not self.canario_ok() or self.cache_epoca() is None or self.reloj() < self.espera_codigo_hasta
        out = []
        for f in filas:
            if f["tipo"] in MEDIDA and bloqueo_medida:
                continue
            if f["tipo"] == "admision" and self.cache_epoca() is None:
                continue
            f["_fam"] = self._familia_de(f["objetivo"])
            out.append(f)
        out.sort(key=lambda f: (f["prioridad"] // 10, f["prioridad"] % 10, usadas[f["_fam"]], f["creado"] or 0))
        return out

    def _familia_de(self, objetivo: str | None) -> str:
        if not objetivo:
            return ""
        hid = objetivo.split(":")[0]
        r = self.con.execute("SELECT familia FROM hipotesis WHERE id = ?", (hid,)).fetchone()
        return r[0] if r else ""

    def lanzar(self, now: int) -> None:
        ok, why = self.puede_empezar(now)
        if not ok:
            self.motivo = why
            return
        if self.estado == "activo":
            self.motivo = None
        for _ in range(self.huecos()):
            sig = [f for f in self.siguientes() if f["id"] not in self.procs]
            if not sig:
                return
            f = sig[0]
            try:
                x = self.lanzador(f["id"])
            except Exception as e:
                self.error(f"No pude lanzar el trabajo {f['id']}: {e}", f["id"])
                return
            self.procs[f["id"]] = {**x, "t0": now, "tipo": f["tipo"], "objetivo": f["objetivo"],
                                   "max_s": MAX_S.get(f["tipo"], 1800)}
            with self.con:
                self.con.execute("UPDATE trabajos SET estado = 'en_curso', empezado = ?, pid = ? WHERE id = ?",
                                 (now, x.get("pid"), f["id"]))
            log.info("Trabajo %s (%s) lanzado: PID %s", f["id"], f["tipo"], x.get("pid"))
            self.cambios = True

    def _matar(self, tid: str, motivo: str, gastar: bool = True) -> None:
        x = self.procs.pop(tid, None)
        if not x:
            return
        try:
            if x.get("popen") is not None:
                x["popen"].kill()
            elif x.get("pid"):
                recursos.terminar(x["pid"])
        except Exception:
            pass
        try:
            if x.get("log"):
                x["log"].close()
        except Exception:
            pass
        with self.con:
            self.con.execute("UPDATE trabajos SET estado = 'pendiente', pid = NULL, intentos = intentos + ?, error = ? "
                             "WHERE id = ?", (1 if gastar else 0, motivo, tid))
        self.nota(f"Trabajo {tid} detenido: {motivo}", "tarea", tid, guardar=gastar)

    def vigilar_procesos(self, now: int) -> None:
        for tid in list(self.procs):
            x = self.procs[tid]
            code = x["popen"].poll() if x.get("popen") is not None else x.get("code")
            if code is None:
                if now - x["t0"] > x["max_s"] * 1000:
                    self._matar(tid, f"demasiado lento (más de {x['max_s'] // 60} min)")
                    self._fallo(tid, "demasiado lento")
                continue
            self.procs.pop(tid)
            try:
                if x.get("log"):
                    x["log"].close()
            except Exception:
                pass
            seg = (now - x["t0"]) / 1000
            if code == 0 and (ajustes.TRABAJOS / f"{tid}.resultado.json").exists():
                with self.con:
                    self.con.execute("UPDATE trabajos SET segundos = ? WHERE id = ?", (seg, tid))
                continue   # lo ingiere ingerir()
            if code == ajustes.CODIGO_PAUSADO:
                with self.con:
                    self.con.execute("UPDATE trabajos SET estado = 'pendiente', pid = NULL WHERE id = ?", (tid,))
                log.info("Trabajo %s pausado: vuelve a la cola desde su punto", tid)
                continue
            texto = self._cola_log(tid)
            if code == ajustes.CODIGO_NO_IMPORTABLE:
                self.espera_codigo_hasta = now + ajustes.ESPERA_CODIGO_S * 1000
                with self.con:
                    self.con.execute("UPDATE trabajos SET estado = 'pendiente', pid = NULL, error = ? WHERE id = ?",
                                     ("código del bot no importable: se espera 10 min sin gastar intentos", tid))
                self.error(f"Código del bot no importable (¿otro flujo lo está editando?): se espera 10 min · {tid}", tid)
                continue
            self._fallo(tid, f"salió con el código {code}: {texto[-400:]}")

    def _cola_log(self, tid: str) -> str:
        try:
            return (ajustes.TRABAJOS / f"{tid}.log").read_text(encoding="utf-8", errors="replace")[-3000:]
        except OSError:
            return ""

    def _fallo(self, tid: str, motivo: str) -> None:
        r = self.con.execute("SELECT intentos, tipo, objetivo FROM trabajos WHERE id = ?", (tid,)).fetchone()
        if r is None:
            return
        intentos = (r[0] or 0) + 1
        with self.con:
            if intentos >= ajustes.MAX_INTENTOS:
                self.con.execute("UPDATE trabajos SET estado = 'fallido', intentos = ?, error = ?, terminado = ? "
                                 "WHERE id = ?", (intentos, motivo[:2000], self.reloj(), tid))
                if r[2]:
                    hid = r[2].split(":")[0]
                    almacen.actualizar_hipotesis(self.con, hid, estado="error")
                    if r[1] == "robustez":
                        almacen.actualizar_variante(self.con, r[2], clase="error", motivos=[f"error: {motivo[:300]}"])
                almacen.evento(self.con, "error", f"Trabajo {tid} fallido tras {intentos} intentos: {motivo[:300]}", tid)
                if r[1] == "canario":
                    almacen.kv_set(self.con, "canario", {"estado": "fallido", "ts": self.reloj(), "motivo": motivo[:500],
                                                         "firma_codigo": self.firma_cod})
            else:
                self.con.execute("UPDATE trabajos SET estado = 'pendiente', intentos = ?, pid = NULL, error = ? WHERE id = ?",
                                 (intentos, motivo[:2000], tid))
        self.error(f"Trabajo {tid} ({r[1]}) falló ({intentos} de {ajustes.MAX_INTENTOS}): {motivo[:200]}", tid)

    # ---------------------------------------------------------------- ingesta (idempotente, una transacción por resultado)
    def ingerir(self) -> int:
        n = 0
        for p in sorted(ajustes.TRABAJOS.glob("*.resultado.json")):
            tid = p.name[:-len(".resultado.json")]
            if tid in self.procs:
                continue
            r = self.con.execute("SELECT * FROM trabajos WHERE id = ?", (tid,)).fetchone()
            if r is None or r["estado"] == "hecho":
                continue
            try:
                res = json.loads(p.read_text(encoding="utf-8"))
                tr = json.loads((ajustes.TRABAJOS / f"{tid}.trabajo.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                self._fallo(tid, f"resultado ilegible: {e}")
                continue
            try:
                with self.con:
                    getattr(self, f"_ingerir_{tr['tipo']}")(tr, res)
                    self.con.execute("UPDATE trabajos SET estado = 'hecho', terminado = ?, pid = NULL WHERE id = ?",
                                     (self.reloj(), tid))
                n += 1
                self.cambios = True
            except Exception as e:   # un resultado que no se puede ingerir no tumba el corredor
                log.exception("No pude ingerir %s", tid)
                self._fallo(tid, f"ingesta: {type(e).__name__}: {e}")
        if n:
            from . import registro_n
            try:
                with self.con:
                    registro_n.apuntar(self.con, "medida")   # solo si creció el N (registro de solo añadir)
            except OSError as e:
                self._nota_rep("registro_n", f"No pude apuntar el N en n_acumulado.jsonl: {e}")
        return n

    def _variante(self, vid: str) -> dict:
        return almacen.fila_dict(self.con.execute("SELECT * FROM variantes WHERE id = ?", (vid,)).fetchone())

    def _hipotesis(self, hid: str) -> dict:
        return almacen.fila_dict(self.con.execute("SELECT * FROM hipotesis WHERE id = ?", (hid,)).fetchone())

    def _ingerir_datos(self, tr: dict, res: dict) -> None:
        almacen.kv_set(self.con, "epoca", {"epoca": tr["epoca"], "carpeta": res.get("carpeta"), "firma": res.get("firma"),
                                           "periodo": res.get("periodo"), "ts": self.reloj()})
        almacen.evento(self.con, "época", f"Caché de la época {tr['epoca']} lista (firma {res.get('firma')})")

    def _ingerir_canario(self, tr: dict, res: dict) -> None:
        res = dict(res, firma_codigo=tr.get("firma_codigo"))
        almacen.kv_set(self.con, "canario", res)
        almacen.insertar_prueba(self.con, {"variante": "canario:libro", "etapa": "canario", "cuenta_en_n": 0,
                                           "firma_codigo": tr.get("firma_codigo"), "criterios": criterios.VERSION,
                                           "perfil": "backtest:0.10", "n_dentro": (res.get("libro") or {}).get("dentro_n"),
                                           "n_fuera": (res.get("libro") or {}).get("fuera_n"),
                                           "R_dentro": (res.get("libro") or {}).get("dentro_R"),
                                           "R_fuera": (res.get("libro") or {}).get("fuera_R"),
                                           "pasa": res.get("estado") == "ok", "metricas": res})
        almacen.evento(self.con, "canario", res.get("texto") or f"Canario: {res.get('estado')}")

    def _ingerir_admision(self, tr: dict, res: dict) -> None:
        hid = tr["objetivo"]
        h = self._hipotesis(hid)
        vres = res.get("variantes") or {}
        a8 = [m for x in vres.values() for m in x.get("motivos", []) if m.startswith("A8")]
        adm = dict(h.get("admision") or {})
        adm["datos"] = vres
        if a8:
            for v in self.con.execute("SELECT id FROM variantes WHERE hipotesis = ?", (hid,)).fetchall():
                almacen.actualizar_variante(self.con, v[0], clase="rechazada", motivos=a8[:5])
            adm["motivos"] = a8
            almacen.actualizar_hipotesis(self.con, hid, estado="rechazada", admision=adm)
            almacen.evento(self.con, "rechazo", f"{hid}: {a8[0]}", hid)
            return
        vivas = 0
        for vid, x in vres.items():
            v = self._variante(vid)
            if v and not x.get("ok"):
                almacen.actualizar_variante(self.con, vid, clase="rechazada", motivos=x.get("motivos"))
            elif v and v["clase"] in ("pendiente", "fuera_de_reglas"):
                vivas += 1
        if not vivas:
            adm["motivos"] = ["A9: ninguna variante da señales suficientes"]
            almacen.actualizar_hipotesis(self.con, hid, estado="rechazada", admision=adm)
            return
        estado = "en_avance_exploratoria" if h.get("exploratoria") else "lista_para_rapida"
        almacen.actualizar_hipotesis(self.con, hid, estado=estado, admision=adm)
        if h.get("exploratoria"):
            for v in self.con.execute("SELECT id, clase FROM variantes WHERE hipotesis = ?", (hid,)).fetchall():
                if v[1] == "pendiente":
                    almacen.actualizar_variante(self.con, v[0], clase="en_avance", avance_desde=self._medianoche_siguiente(),
                                                etapa="avance")

    def _payload_variante(self, v: dict, h: dict) -> dict:
        from .familias import sha_archivo
        return {"vid": v["id"], "familia": h["familia"], "params": v["params"], "salida": v["salida"],
                "fuera_de_reglas": v["clase"] == "fuera_de_reglas" or not v["cumple_reglas"],
                "familia_sha": sha_archivo(h["familia"]), "exploratoria": bool(h.get("exploratoria")),
                "avance_desde": v.get("avance_desde")}

    def _ingerir_rapida(self, tr: dict, res: dict) -> None:
        import numpy as np
        hid = tr["objetivo"]
        h = self._hipotesis(hid)
        ref = bool((h.get("cuerpo") or {}).get("referencia"))
        sigue = []
        for vid, x in (res.get("variantes") or {}).items():
            v = self._variante(vid)
            almacen.insertar_prueba(self.con, {
                "variante": vid, "etapa": "referencia" if ref else "rapida", "epoca": tr["epoca"],
                "firma_datos": tr.get("firma_datos"), "firma_codigo": tr["firma_codigo"], "criterios": criterios.VERSION,
                "perfil": "okx_eu_con_xperps", "cuenta_en_n": 0 if ref else 1, "n_dentro": x["n_dentro"],
                "R_dentro": x["R_dentro"], "pasa": x["pasa"], "motivos": x["motivos"], "metricas": x["metricas"]},
                (x["serie"]["desde"], np.asarray(x["serie"]["r"])))
            if x["pasa"] or v["clase"] == "fuera_de_reglas":
                sigue.append(vid)
                almacen.actualizar_variante(self.con, vid, etapa="rapida", motivos=x["motivos"] if not x["pasa"] else [])
            else:
                almacen.actualizar_variante(self.con, vid, clase="descartada_rapida", etapa="rapida", motivos=x["motivos"])
        if sigue:
            vs = [self._payload_variante(self._variante(vid), h) for vid in sigue]
            self.encolar("completa", hid, {"variantes": vs, "cache": tr["cache"], "firma_datos": tr.get("firma_datos")},
                         h.get("prioridad") or 3)
            almacen.actualizar_hipotesis(self.con, hid, estado="en_completa")
        else:
            almacen.actualizar_hipotesis(self.con, hid, estado="terminada")
        self._leccion(h["familia"])

    def _ingerir_completa(self, tr: dict, res: dict) -> None:
        import numpy as np
        hid = tr["objetivo"]
        h = self._hipotesis(hid)
        ref = bool((h.get("cuerpo") or {}).get("referencia"))
        robustas = []
        for vid, x in (res.get("variantes") or {}).items():
            v = self._variante(vid)
            met = dict(x["metricas"], k4=x.get("k4"))
            almacen.insertar_prueba(self.con, {
                "variante": vid, "etapa": "referencia" if ref else "completa", "epoca": tr["epoca"],
                "firma_datos": tr.get("firma_datos"), "firma_codigo": tr["firma_codigo"], "criterios": criterios.VERSION,
                "perfil": "okx_eu_con_xperps", "cuenta_en_n": 0, "n_dentro": x["n_dentro"], "n_fuera": x["n_fuera"],
                "R_dentro": x["R_dentro"], "R_fuera": x["R_fuera"], "p_fuera": x["p_fuera"], "sr_fuera": x["sr_fuera"],
                "dias_fuera": x["dias_fuera"], "asimetria": x["asimetria"], "curtosis": x["curtosis"], "pasa": x["pasa"],
                "motivos": x["motivos"], "metricas": met}, (x["serie"]["desde"], np.asarray(x["serie"]["r"])))
            fdr = v["clase"] == "fuera_de_reglas"
            if x["pasa"] and not fdr and not ref:
                robustas.append(vid)
                almacen.actualizar_variante(self.con, vid, etapa="completa", frecuencia=x.get("frecuencia"), motivos=[])
            elif not fdr:
                almacen.actualizar_variante(self.con, vid, clase="descartada_completa", etapa="completa",
                                            frecuencia=x.get("frecuencia"), motivos=x["motivos"])
            else:
                almacen.actualizar_variante(self.con, vid, etapa="completa", frecuencia=x.get("frecuencia"),
                                            motivos=["fuera de reglas: informativa (nunca puede ser candidata)"] + x["motivos"])
        if robustas:
            con_fuera = {r[0] for r in self.con.execute(
                "SELECT DISTINCT variante FROM pruebas WHERE etapa IN ('completa', 'robustez') AND variante LIKE ?",
                (hid + ":%",))}
            todas = [self._variante(r[0]) for r in self.con.execute("SELECT id FROM variantes WHERE hipotesis = ?", (hid,))]
            hermanas = [self._payload_variante(v, h) for v in todas
                        if v["id"] not in con_fuera and v["clase"] not in ("duplicada", "rechazada")]
            conocidas = {r[0]: r[1] for r in self.con.execute(
                "SELECT variante, R_fuera FROM pruebas WHERE etapa = 'completa' AND variante LIKE ?", (hid + ":%",))}
            fdr_ids = [v["id"] for v in todas if v["clase"] == "fuera_de_reglas" or not v["cumple_reglas"]]
            for vid in robustas:
                self.encolar("robustez", vid, {"variante": self._payload_variante(self._variante(vid), h),
                                               "hermanas": hermanas, "fuera_conocidas": conocidas,
                                               "fuera_de_reglas": fdr_ids, "cache": tr["cache"],
                                               "firma_datos": tr.get("firma_datos")}, h.get("prioridad") or 3)
            almacen.actualizar_hipotesis(self.con, hid, estado="en_robustez")
        else:
            almacen.actualizar_hipotesis(self.con, hid, estado="terminada")
        self._leccion(h["familia"])

    def _ingerir_robustez(self, tr: dict, res: dict) -> None:
        import numpy as np
        vid = tr["objetivo"]
        v = self._variante(vid)
        hid = v["hipotesis"]
        h = self._hipotesis(hid)
        for hv, x in (res.get("hermanas") or {}).items():
            almacen.insertar_prueba(self.con, {
                "variante": hv, "etapa": "completa", "epoca": tr["epoca"], "firma_datos": tr.get("firma_datos"),
                "firma_codigo": tr["firma_codigo"], "criterios": criterios.VERSION, "perfil": "okx_eu_con_xperps",
                "cuenta_en_n": 0, "n_dentro": x["n_dentro"], "n_fuera": x["n_fuera"], "R_dentro": x["R_dentro"],
                "R_fuera": x["R_fuera"], "p_fuera": x["p_fuera"], "sr_fuera": x["sr_fuera"], "dias_fuera": x["dias_fuera"],
                "asimetria": x["asimetria"], "curtosis": x["curtosis"], "pasa": None,
                "motivos": ["solo para B6 (estabilidad de la hipótesis): no pasó la etapa anterior"],
                "metricas": x["metricas"]}, (x["serie"]["desde"], np.asarray(x["serie"]["r"])))
        com = almacen.fila_dict(self.con.execute("SELECT * FROM pruebas WHERE variante = ? AND etapa = 'completa' "
                                                 "ORDER BY id DESC LIMIT 1", (vid,)).fetchone()) or {}
        mot = list(res.get("motivos") or [])
        st = self.estadistica_global(extra={vid: com})
        q, dsr = st["q"].get(vid), st["dsr"].get(vid)
        cr = criterios.CRITERIOS["robustez"]
        if q is None or q > cr["fdr_q"]:
            mot.append(f"B7: Benjamini-Hochberg q = {q} sobre {st['bh_m']} pruebas acumuladas (hace falta ≤ 0,10)")
        if dsr is None or dsr < cr["dsr_min"]:
            mot.append(f"B8: Sharpe deflactado {None if dsr is None else round(dsr, 4)} con N = {st['n_dsr']:.0f} "
                       "(hace falta ≥ 0,95)")
        met = {k: res.get(k) for k in ("B1", "B2", "B3", "estres", "estabilidad", "info")}
        met.update(q=q, dsr=dsr, n_dsr=st["n_dsr"])
        almacen.insertar_prueba(self.con, {
            "variante": vid, "etapa": "robustez", "epoca": tr["epoca"], "firma_datos": tr.get("firma_datos"),
            "firma_codigo": tr["firma_codigo"], "criterios": criterios.VERSION, "perfil": "okx_eu_con_xperps",
            "cuenta_en_n": 0, "n_fuera": com.get("n_fuera"), "R_fuera": com.get("R_fuera"), "p_fuera": com.get("p_fuera"),
            "pasa": not mot, "motivos": mot, "metricas": met})
        if mot:
            almacen.actualizar_variante(self.con, vid, clase="descartada_robustez", etapa="robustez", motivos=mot)
        else:
            self._guardar_operaciones(vid, res.get("operaciones") or {})
            hermanas_en = self.con.execute("SELECT id FROM variantes WHERE hipotesis = ? AND clase = 'en_avance'",
                                           (hid,)).fetchone()
            if hermanas_en:
                almacen.actualizar_variante(self.con, vid, clase="candidata_hermana", etapa="robustez", motivos=[])
            else:
                almacen.actualizar_variante(self.con, vid, clase="en_avance", etapa="avance", motivos=[],
                                            avance_desde=self._medianoche_siguiente())
                almacen.evento(self.con, "candidata", f"{vid} pasa todas las pruebas: entra en la prueba en avance virtual "
                                                      f"desde la próxima medianoche de Madrid", vid)
        pend = self.con.execute("SELECT COUNT(*) FROM trabajos WHERE tipo = 'robustez' AND estado IN ('pendiente', "
                                "'en_curso') AND objetivo LIKE ? AND id != ?", (hid + ":%", tr["id"])).fetchone()[0]
        if not pend:
            almacen.actualizar_hipotesis(self.con, hid, estado="terminada")
        self._leccion(h["familia"])

    def _ingerir_avance(self, tr: dict, res: dict) -> None:
        """Cada trabajo de avance recalcula TODAS las operaciones de cada variante desde 8 días antes de su inicio: su
        resultado sustituye al anterior. Las filas que ya no salen (una operación abierta que el recálculo ya no da,
        por un hueco de datos que se rellenó) se borran en vez de quedarse como operaciones fantasma; si alguna era
        completa, se apunta en eventos con su contenido. Un resultado más viejo que el último ingerido no cambia nada."""
        ahora = int(res.get("ahora") or self.reloj())
        ult = almacen.kv_get(self.con, "avance_ts")
        if ult and ahora < int(ult):
            log.info("Avance %s (de %s) más viejo que el último ingerido: no cambia nada", tr.get("id"), ahora)
            return
        for vid, filas in (res.get("variantes") or {}).items():
            nuevos = {f["id"] for f in filas}
            viejas = [dict(r) for r in self.con.execute("SELECT * FROM avance WHERE variante = ?", (vid,))]
            fantasmas = [r for r in viejas if r["id"] not in nuevos]
            if fantasmas:
                comp = [r for r in fantasmas if r["completa"] and not r["previa"]]
                self.con.executemany("DELETE FROM avance WHERE id = ?", [(r["id"],) for r in fantasmas])
                almacen.evento(self.con, "avance_recalculo",
                               f"{vid}: {len(fantasmas)} operaciones del avance ya no salen al recalcular "
                               f"({len(comp)} completas); se quitan", vid,
                               {"quitadas": [{k: r[k] for k in ("id", "t_entrada", "t_salida", "R_f", "completa", "previa")}
                                             for r in fantasmas]})
            for f in filas:
                self.con.execute("INSERT OR REPLACE INTO avance(id, variante, moneda, lado, t_senal, t_entrada, t_salida, R, "
                                 "R_f, pct, motivo, completa, previa, cuerpo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (f["id"], f["variante"], f["moneda"], f["lado"], f["t_senal"], f["t_entrada"],
                                  f["t_salida"], f["R"], f["R_f"], f["pct"], f["motivo"], f["completa"], f["previa"],
                                  almacen.dumps(f.get("cuerpo"))))
        almacen.kv_set(self.con, "avance_ts", ahora)

    def _guardar_operaciones(self, vid: str, ops: dict) -> None:
        for tramo, lista in ops.items():
            for k, o in enumerate(lista):
                self.con.execute("INSERT OR REPLACE INTO operaciones(variante, tramo, k, cuerpo) VALUES (?, ?, ?, ?)",
                                 (vid, tramo, k, almacen.dumps(o)))

    def _medianoche_siguiente(self) -> int:
        d = datetime.fromtimestamp(self.reloj() / 1000, ajustes.MAD).date() + timedelta(days=1)
        return int(datetime(d.year, d.month, d.day, tzinfo=ajustes.MAD).timestamp() * 1000)

    def _leccion(self, fam: str) -> None:
        """Lecciones por familia, calculadas (sin IA): cuántas variantes pasan cada etapa y el fallo más común."""
        filas = self.con.execute("SELECT v.clase, v.motivos FROM variantes v JOIN hipotesis h ON v.hipotesis = h.id "
                                 "WHERE h.familia = ?", (fam,)).fetchall()
        por = Counter()
        fallo = Counter()
        for cl, mo in filas:
            por[cl] += 1
            try:
                for m in json.loads(mo or "[]")[:1]:
                    fallo[str(m).split(":")[0]] += 1
            except ValueError:
                pass
        rap = self.con.execute("SELECT COUNT(DISTINCT p.variante) FROM pruebas p JOIN variantes v ON p.variante = v.id JOIN "
                               "hipotesis h ON v.hipotesis = h.id WHERE h.familia = ? AND p.etapa = 'rapida' AND p.pasa = 1",
                               (fam,)).fetchone()[0]
        comp = self.con.execute("SELECT COUNT(DISTINCT p.variante) FROM pruebas p JOIN variantes v ON p.variante = v.id JOIN "
                                "hipotesis h ON v.hipotesis = h.id WHERE h.familia = ? AND p.etapa = 'completa' AND p.pasa = 1",
                                (fam,)).fetchone()[0]
        medidas_ = sum(n for c, n in por.items() if c not in ("pendiente", "duplicada", "rechazada"))
        comun = fallo.most_common(1)[0][0] if fallo else None
        lec = almacen.kv_get(self.con, "lecciones") or {}
        lec[fam] = {"probadas": medidas_, "por_etapa": {"rapida": rap, "completa": comp}, "fallo_mas_comun": comun,
                    "texto": f"{fam}: {medidas_} variantes, {rap} pasan la rápida y {comp} la completa"
                             + (f" (fallo más común: {comun})" if comun else "")}
        almacen.kv_set(self.con, "lecciones", lec)

    # ---------------------------------------------------------------- estadística acumulada (9.3 y 9.4)
    def estadistica_global(self, extra: dict | None = None) -> dict:
        """BH sobre todas las pruebas (vivo con ≥ 100 op fuera: su p HAC; el resto, p = 1; previas, también las
        heredadas de una base anterior: las suyas) y Sharpe deflactado con N_dsr = N efectivo del laboratorio vivo + 381
        + 1.917 + el N efectivo heredado (registro_n.py).

        N efectivo del laboratorio vivo = máx(Li y Ji sobre todas las series fuera; suma de Li y Ji por hipótesis + 1 por
        cada hipótesis medida sin fuera de muestra). Li y Ji sobre miles de series de ≈ 411 días SATURA (la matriz de
        correlación tiene rango ≤ días: con 3.200 series independientes da ≈ 620): sin el suelo por hipótesis, el listón
        de B8 dejaría de subir justo cuando se prueban muchas. Cuando Li y Ji se acerca a los días (≥ la mitad), se avisa."""
        import numpy as np
        from . import medidas, registro_n
        filas = {}
        for r in self.con.execute("SELECT * FROM pruebas WHERE etapa = 'completa' ORDER BY id"):
            filas[r["variante"]] = almacen.fila_dict(r)
        for k, x in (extra or {}).items():
            if x:
                filas[k] = x
        n_total = almacen.n_total(self.con)
        vivo_ids = list(filas)
        ps = [float(f["p_fuera"]) if (f.get("n_fuera") or 0) >= criterios.c("completa", "min_ops_fuera")
              and f.get("p_fuera") is not None else 1.0 for f in filas.values()]
        ps_vivo_sin_fuera = [1.0] * max(0, n_total - len(vivo_ids))
        ps_prev = previas.ps_previas(self.con)
        todos = ps + ps_vivo_sin_fuera + ps_prev
        qs = medidas.bh(todos) if todos else []
        q = {vid: qs[i] for i, vid in enumerate(vivo_ids)}
        series, por_hip = [], {}
        for vid in vivo_ids:
            pr = filas[vid]
            row = self.con.execute("SELECT r_dia FROM series WHERE prueba = ?", (pr.get("id"),)).fetchone() if pr.get("id") else None
            if row is not None:
                s = almacen.blob_a_serie(row[0])
                series.append(s)
                por_hip.setdefault(vid.split(":")[0], []).append(s)
        n_lij = medidas.n_efectivo(series) if series else 0.0
        medidas_hip = {r[0].split(":")[0] for r in self.con.execute(
            "SELECT DISTINCT variante FROM pruebas WHERE cuenta_en_n = 1")}
        n_bloques = medidas.n_efectivo_bloques(por_hip) + len(medidas_hip - set(por_hip))
        n_ef = max(n_lij, n_bloques)
        dias = min(len(s) for s in series) if series else 0
        aviso = None
        if series and n_lij >= 0.5 * dias:
            aviso = (f"El N efectivo de Li y Ji ({n_lij:.0f}) se acerca a los días del fuera de muestra ({dias}): con más "
                     f"series ya no puede crecer (satura). Manda el N por hipótesis ({n_bloques:.0f}).")
        her = registro_n.heredado(self.con)
        cp = criterios.CRITERIOS["previas"]
        n_dsr = n_ef + cp["n_lab_efectivo"] + cp["n_resto"] + her["n_efectivo"]
        dsr = {}
        for vid, f in filas.items():
            if f.get("sr_fuera") is not None and f.get("dias_fuera"):
                dsr[vid] = medidas.dsr(float(f["sr_fuera"]), int(f["dias_fuera"]), float(f.get("asimetria") or 0),
                                       float(f.get("curtosis") or 3), n_dsr)
        return {"q": q, "dsr": dsr, "n_efectivo": n_ef, "n_efectivo_lij": n_lij, "n_efectivo_hipotesis": n_bloques,
                "n_heredado": her["n_efectivo"], "dias_fuera": dias, "aviso_n": aviso, "n_dsr": n_dsr,
                "bh_m": len(todos), "umbral_z": medidas.z_umbral(n_dsr), "n_total": n_total, "n_fuera": len(vivo_ids)}

    # ---------------------------------------------------------------- buzón: admisión sin datos (A1-A7, A10, A11)
    def admitir_buzon(self) -> int:
        from .hipotesis import firma_hipotesis, validar
        n = 0
        for p in sorted(ajustes.ENTRADA.glob("*.json")):
            if p.name.endswith(".tmp"):
                continue
            try:
                h = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                self._rechazar_archivo(p, None, [f"A1: JSON ilegible: {e}"])
                continue
            hid = h.get("id") if isinstance(h, dict) else None
            if hid and self.con.execute("SELECT 1 FROM hipotesis WHERE id = ?", (hid,)).fetchone():
                self._rechazar_archivo(p, h, [f"A1: ya hay una hipótesis con el id {hid}"], registrar=False)
                continue
            firmas = {r[0]: r[1] for r in self.con.execute("SELECT firma, id FROM variantes")}
            r = validar(h, firmas, self.familias_db())
            mot = list(r["motivos"])
            if r["ok"] and h.get("autor") == "generador-claude":   # cupo de la pasada del generador
                pasada = hid[:16]
                ya = self.con.execute("SELECT COUNT(*) FROM hipotesis WHERE autor = 'generador-claude' AND substr(id, 1, 16) = ?",
                                      (pasada,)).fetchone()[0]
                if ya >= ajustes.CUPO_PASADA:
                    mot.append(f"A11: la pasada {pasada[2:]} del generador ya tiene {ya} hipótesis (máximo {ajustes.CUPO_PASADA})")
            now = self.reloj()
            with self.con:
                if mot:
                    self.con.execute("INSERT OR IGNORE INTO hipotesis(id, recibida, autor, familia, familia_version, titulo, "
                                     "firma, estado, prioridad, exploratoria, fuera_de_reglas, cuerpo, admision, actualizada) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?, 'rechazada', ?, ?, ?, ?, ?, ?)",
                                     (hid or p.stem, now, (h or {}).get("autor"), (h or {}).get("familia") or "?",
                                      (h or {}).get("familia_version"), (h or {}).get("titulo"),
                                      firma_hipotesis(h) if isinstance(h, dict) else "", 3, 0, 0, almacen.dumps(h),
                                      almacen.dumps({"motivos": mot}), now))
                    almacen.evento(self.con, "rechazo", f"{hid or p.name}: {mot[0]}", hid)
                else:
                    estado = "esperando_datos" if r.get("estado_inicial") == "esperando_datos" else "admitida"
                    self.con.execute("INSERT INTO hipotesis(id, recibida, autor, familia, familia_version, titulo, firma, "
                                     "estado, prioridad, exploratoria, fuera_de_reglas, cuerpo, admision, actualizada) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                     (hid, now, h["autor"], r["familia"], r["familia_version"], h["titulo"],
                                      firma_hipotesis(h), estado, r["prioridad"], int(r["exploratoria"]),
                                      int(r["fuera_de_reglas"]), almacen.dumps(h), almacen.dumps({"motivos": []}), now))
                    for v in r["variantes"]:
                        clase = ("duplicada" if v["duplicada_de"] else "fuera_de_reglas" if v["fuera_de_reglas"]
                                 else "pendiente")
                        firma = v["firma"] if not v["duplicada_de"] else f"dup:{hid}:{v['vid']}:{v['firma']}"
                        self.con.execute("INSERT INTO variantes(id, hipotesis, vid, firma, params, salida, cumple_reglas, clase, "
                                         "etapa, motivos, actualizada) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'admision', ?, ?)",
                                         (f"{hid}:{v['vid']}", hid, v["vid"], firma, almacen.dumps(v["params"]),
                                          almacen.dumps(v["salida"]), int(v["cumple_reglas"]), clase,
                                          almacen.dumps([f"duplicada de {v['duplicada_de']}"] if v["duplicada_de"] else []),
                                          now))
                    almacen.evento(self.con, "admision", f"Hipótesis {hid} admitida ({len(r['variantes'])} variantes)", hid)
            self._mover(p, ajustes.RECHAZADAS if mot else ajustes.ACEPTADAS, h, mot)
            n += 1
            self.cambios = True
        return n

    def _rechazar_archivo(self, p: Path, h, mot: list[str], registrar: bool = True) -> None:
        if registrar:
            with self.con:
                almacen.evento(self.con, "rechazo", f"{p.name}: {mot[0]}")
        self._mover(p, ajustes.RECHAZADAS, h, mot)

    def _mover(self, p: Path, dest: Path, h, mot: list[str]) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        q = dest / p.name
        try:
            if isinstance(h, dict):
                h = dict(h)
                if mot:
                    h["admision"] = {"motivos": mot}
                tmp = q.with_name(q.name + ".tmp")
                tmp.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, q)
                p.unlink()
            else:
                shutil.move(str(p), str(q))
        except OSError as e:
            log.warning("No pude mover %s: %s", p, e)

    # ---------------------------------------------------------------- planificación
    def planificar(self, now: int) -> None:
        ep = self.epoca()
        car = self.cache_epoca()
        if car is None:
            self.encolar("datos", f"epoca:{ep}", {"epoca": ep, "firma_datos": None})
            self.estado_datos = "sin_datos"
            return
        meta = json.loads((car / "meta.json").read_text(encoding="utf-8"))
        fd = meta.get("firma")
        if self.canario_ok() and self.estado == "canario_fallido":
            self.estado, self.motivo = "activo", None
            self.nota("El canario vuelve a reproducir seis2_resumen.json: se reanuda la medida", "canario")
        if not self.canario_ok():
            c = almacen.kv_get(self.con, "canario") or {}
            if c.get("estado") == "fallido" and c.get("firma_codigo") == self.firma_cod:
                self.estado = "canario_fallido"
                que = ("las comisiones reales no cuadran" if c.get("reproduce") and not (c.get("comisiones") or {}).get("ok")
                       else "el libro no reproduce seis2_resumen.json")
                self.motivo = f"Canario fallido ({que}): se para toda medida (solo sigue la admisión)"
            else:
                ronda = almacen.kv_get(self.con, "canario_ronda") or ""
                with self.con:
                    self.encolar("canario", "canario", {"firma_datos": fd, "clave_extra": f"{self.firma_cod}:{ronda}"})
        # admisión con datos (A8 y A9)
        for h in self.con.execute("SELECT * FROM hipotesis WHERE estado = 'admitida'").fetchall():
            h = almacen.fila_dict(h)
            vs = [almacen.fila_dict(v) for v in self.con.execute(
                "SELECT * FROM variantes WHERE hipotesis = ? AND clase IN ('pendiente', 'fuera_de_reglas')", (h["id"],))]
            if not vs:
                with self.con:
                    almacen.actualizar_hipotesis(self.con, h["id"], estado="rechazada",
                                                 admision={"motivos": ["A7: ya probada: no queda ninguna variante nueva"]})
                continue
            with self.con:
                self.encolar("admision", h["id"], {"variantes": [self._payload_variante(v, h) for v in vs],
                                                   "cache": str(car), "firma_datos": fd}, h.get("prioridad") or 3)
                almacen.actualizar_hipotesis(self.con, h["id"], estado="en_admision")
        # etapa rápida con el cupo del día
        if self.canario_ok():
            hoy = ajustes.dia_mad(now)
            cupo = almacen.kv_get(self.con, f"cupo:{hoy}") or {"hipotesis": 0, "variantes": 0, "familias": {}}
            for h in self.con.execute("SELECT * FROM hipotesis WHERE estado = 'lista_para_rapida' ORDER BY prioridad, "
                                      "recibida").fetchall():
                h = almacen.fila_dict(h)
                vs = [almacen.fila_dict(v) for v in self.con.execute(
                    "SELECT * FROM variantes WHERE hipotesis = ? AND clase IN ('pendiente', 'fuera_de_reglas')", (h["id"],))]
                if (cupo["hipotesis"] >= ajustes.CUPO_DIA or cupo["variantes"] + len(vs) > 6 * ajustes.CUPO_DIA
                        or cupo["familias"].get(h["familia"], 0) >= ajustes.CUPO_FAMILIA):
                    continue   # espera al día siguiente, por orden (no se rechaza)
                with self.con:
                    self.encolar("rapida", h["id"], {"variantes": [self._payload_variante(v, h) for v in vs],
                                                     "cache": str(car), "firma_datos": fd}, h.get("prioridad") or 3)
                    almacen.actualizar_hipotesis(self.con, h["id"], estado="en_rapida")
                    cupo["hipotesis"] += 1
                    cupo["variantes"] += len(vs)
                    cupo["familias"][h["familia"]] = cupo["familias"].get(h["familia"], 0) + 1
                    almacen.kv_set(self.con, f"cupo:{hoy}", cupo)
        self.estado_datos = "ok"
        self.datos_info = {"epoca": ep, "periodo": {"desde": meta["periodo"].get("desde"), "hasta": meta["periodo"].get("hasta"),
                                                    "corte": ajustes.CORTE_DIA},
                           "firma": fd, "datos_hasta": meta.get("hasta"), "atrasados": False}

    # ---------------------------------------------------------------- tareas con hora (11.1)
    def tareas_con_hora(self, now: int) -> None:
        if self.en_pausa(now):
            return   # si una tarea cae en una pausa, se hace al acabar
        dt = datetime.fromtimestamp(now / 1000, ajustes.MAD)
        hora = dt.strftime("%Y-%m-%d %H")
        ult = almacen.kv_get(self.con, "hechas") or {}
        def toca(clave: str, marca: str, cond: bool) -> bool:
            return cond and ult.get(clave) != marca
        car = self.cache_epoca()
        cambios = False
        if toca("avance", hora, dt.minute >= 20 and car is not None):
            vs = [almacen.fila_dict(v) for v in self.con.execute(
                "SELECT * FROM variantes WHERE avance_desde IS NOT NULL AND clase IN ('en_avance', 'revocada', 'lista', "
                "'propuesta')")]
            if vs and self.canario_ok():
                with self.con:
                    pl = [self._payload_variante(v, self._hipotesis(v["hipotesis"])) for v in vs]
                    self.encolar("avance", "avance", {"variantes": pl, "clave_extra": hora, "firma_datos": None})
            ult["avance"] = hora
            cambios = True
        dia = dt.date().isoformat()
        if toca("consolidacion", dia, (dt.hour, dt.minute) >= (4, 30)):
            self.consolidar()
            ult["consolidacion"] = dia
            cambios = True
        if toca("diario", dia, (dt.hour, dt.minute) >= (0, 5)):
            ayer = (dt.date() - timedelta(days=1)).isoformat()
            with self.con:
                salida_web.escribir_diario(self.con, ayer)
            ult["diario"] = dia
            cambios = True
        if toca("domingo", dia, dt.weekday() == 6 and (dt.hour, dt.minute) >= (5, 30)):
            with self.con:
                almacen.kv_set(self.con, "canario", {"estado": "pendiente", "ts": now, "motivo": "canario de los domingos"})
                almacen.kv_set(self.con, "canario_ronda", dia)
            ult["domingo"] = dia
            cambios = True
        if cambios:
            with self.con:
                almacen.kv_set(self.con, "hechas", ult)

    # ---------------------------------------------------------------- consolidación (11.9)
    def consolidar(self) -> dict:
        from . import medidas
        now = self.reloj()
        with self.con:
            st = self.estadistica_global()
            cr = criterios.CRITERIOS
            # 9.7 revocación: B7 y B8 con el N de hoy
            for v in self.con.execute("SELECT id, clase FROM variantes WHERE clase IN ('candidata', 'en_avance', "
                                      "'candidata_hermana', 'lista', 'propuesta', 'revocada')").fetchall():
                vid, cl = v[0], v[1]
                ok = (st["q"].get(vid, 1.0) <= cr["robustez"]["fdr_q"]
                      and st["dsr"].get(vid, 0.0) >= cr["robustez"]["dsr_min"])
                if not ok and cl != "revocada":
                    motivo = (f"V1: con el N de hoy deja de pasar B7/B8 (q {st['q'].get(vid)}, "
                              f"DSR {st['dsr'].get(vid)})")
                    almacen.actualizar_variante(self.con, vid, clase="revocada", motivos=[motivo])
                    almacen.evento(self.con, "revocada", f"{vid} revocada: con el N de hoy ya no pasa B7 y B8", vid)
                    if cl == "propuesta":   # su propuesta deja de valer: no se puede aceptar sin respaldo estadístico
                        salida_web.retirar_propuestas(self.con, vid, f"revocada: {motivo}")
                elif ok and cl == "revocada":
                    almacen.actualizar_variante(self.con, vid, clase="en_avance", motivos=[])
                    almacen.evento(self.con, "candidata", f"{vid} vuelve a pasar B7 y B8 con el N de hoy", vid)
            # 9.5 y 10.2: prueba en avance con Holm acumulado
            p_holm = self._avance_juicio(now)
            # 9.6 cuenta honesta
            pv = {}
            for r in self.con.execute("SELECT variante, metricas FROM pruebas WHERE etapa = 'completa'"):
                try:
                    pv[r[0]] = json.loads(r[1] or "{}").get("por_trimestre") or {}
                except ValueError:
                    pass
            ep = almacen.kv_get(self.con, "epoca") or {}
            per = ep.get("periodo") or {}
            honesta = medidas.cuenta_honesta(pv, per.get("corte", ajustes.CORTE_MS), per.get("fin", now)) if pv else \
                {"trimestres": [], "R_medio": None, "texto": "Sin variantes con fuera de muestra aún."}
            cons = {"ts": now, "q": st["q"], "dsr": st["dsr"], "n_efectivo": st["n_efectivo"], "n_dsr": st["n_dsr"],
                    "n_efectivo_lij": st["n_efectivo_lij"], "n_efectivo_hipotesis": st["n_efectivo_hipotesis"],
                    "n_heredado": st["n_heredado"], "aviso_n": st["aviso_n"],
                    "bh_m": st["bh_m"], "umbral_z": st["umbral_z"], "cuenta_honesta": honesta, "p_holm": p_holm}
            almacen.kv_set(self.con, "consolidacion", cons)
            self.con.execute("INSERT OR REPLACE INTO consolidaciones(ts, n_total, n_fuera, n_efectivo, n_previo, n_dsr, "
                             "bh_m, cuerpo) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (now, st["n_total"], st["n_fuera"], st["n_efectivo"], almacen.n_previo(self.con), st["n_dsr"],
                              st["bh_m"], almacen.dumps({"cuenta_honesta": honesta, "n_efectivo_lij": st["n_efectivo_lij"],
                                                         "n_efectivo_hipotesis": st["n_efectivo_hipotesis"],
                                                         "aviso_n": st["aviso_n"]})))
            for p in self.con.execute("SELECT id FROM variantes WHERE clase = 'lista'").fetchall():
                prop = salida_web.escribir_propuesta(self.con, p[0])
                almacen.actualizar_variante(self.con, p[0], clase="propuesta")
                almacen.evento(self.con, "lista", f"{p[0]} está lista: se propone al usuario ({prop['id']})", p[0])
            from . import registro_n
            try:
                registro_n.apuntar(self.con, "consolidación", {"n_lab": round(st["n_efectivo"], 3), "bh_m": st["bh_m"],
                                                               "n_dsr": round(st["n_dsr"], 3)}, forzar=True)
            except OSError as e:
                self.error(f"No pude apuntar el N en n_acumulado.jsonl: {e}")
        self.escribir_salidas(completo=True)
        self._limpiar_trabajos(now)
        return cons

    def _avance_juicio(self, now: int) -> dict:
        from .. import evolucion
        cr = criterios.CRITERIOS["avance"]
        vs = [almacen.fila_dict(v) for v in self.con.execute(
            "SELECT * FROM variantes WHERE avance_desde IS NOT NULL AND clase IN ('en_avance', 'revocada', 'lista', "
            "'propuesta', 'descartada_avance')")]
        p_raw = {}
        datos_v = {}
        for v in vs:
            rows = [dict(r) for r in self.con.execute("SELECT * FROM avance WHERE variante = ? AND previa = 0 AND completa = 1",
                                                      (v["id"],))]
            ops = [{"t": r["t_entrada"], "R": r["R_f"], "t_out": r["t_salida"], "gana": r["R_f"] > 0} for r in rows]
            bm = evolucion.bootstrap_mejora(ops, None, 0.0) if len(ops) >= 5 else {"p": None, "p_menor": None}
            p_raw[v["id"]] = bm.get("p") if bm.get("p") is not None else 1.0
            datos_v[v["id"]] = (v, rows, ops, bm)
        expl = {k for k, (v, *_rest) in datos_v.items() if (self._hipotesis(v["hipotesis"]) or {}).get("exploratoria")}
        from . import medidas
        p_holm = {}
        for grupo in (set(p_raw) - expl, expl):
            ks = sorted(grupo)
            for k, adj in zip(ks, medidas.holm([p_raw[k] for k in ks])):
                p_holm[k] = round(adj, 5)
        for vid, (v, rows, ops, bm) in datos_v.items():
            if v["clase"] == "descartada_avance":
                continue
            ex = vid in expl
            dias = (now - int(v["avance_desde"])) / DAY
            n = len(ops)
            dias_dist = len({ajustes.dia_mad(r["t_entrada"]) for r in rows})
            e, pk, dd = 100.0, 100.0, 0.0
            for r in sorted(rows, key=lambda r: r["t_salida"] or 0):
                e += r["pct"] or 0.0
                pk = max(pk, e)
                dd = max(dd, (pk - e) / pk * 100)
            Rm = sum(o["R"] for o in ops) / n if n else None
            freq = n / max(1.0, dias)
            clase_f = v.get("frecuencia")
            min_ops = cr["explo_min_ops"] if ex else cr["min_ops"]
            min_d = cr["explo_min_dias"] if ex else cr["min_dias"]
            p_max = cr["explo_p_max"] if ex else cr["p_max"]
            if n >= cr["descarte_min_ops"]:
                rc, _ = evolucion.contraido(ops, now)
                if rc < 0 and (bm.get("p_menor") or 1.0) < cr["descarte_p"]:
                    almacen.actualizar_variante(self.con, vid, clase="descartada_avance",
                                                motivos=["en avance pierde: R contraído < 0 con p < 0,05"])
                    almacen.evento(self.con, "descartada_avance", f"{vid} descartada en avance: pierde", vid)
                    if v["clase"] == "propuesta":   # mientras espera al usuario, la prueba en avance sigue juzgando
                        salida_web.retirar_propuestas(self.con, vid, "en avance pierde: R contraído < 0 con p < 0,05")
                    continue
            if n >= cr["no_azar_ops"] and not (Rm and Rm > 0 and p_holm.get(vid, 1) < p_max):
                almacen.actualizar_variante(self.con, vid, clase="descartada_avance",
                                            motivos=["en avance, con ≥ 200 operaciones, no se distingue del azar"])
                if v["clase"] == "propuesta":
                    salida_web.retirar_propuestas(self.con, vid, "en avance, con ≥ 200 operaciones, no se distingue "
                                                                 "del azar")
                continue
            if v["clase"] == "propuesta":
                continue
            v_ok = (v["clase"] == "en_avance" and n >= min_ops and dias_dist >= cr["min_dias_distintos"] and dias >= min_d
                    and Rm is not None and Rm > 0 and p_holm.get(vid, 1) < p_max and dd < cr["caida_max_pct"]
                    and freq >= (1.0 if clase_f == "frecuente" else 0.25))
            if v_ok:
                almacen.actualizar_variante(self.con, vid, clase="lista")
        return p_holm

    def _limpiar_trabajos(self, now: int) -> None:
        lim = now - 7 * DAY
        viejos = [r[0] for r in self.con.execute("SELECT id FROM trabajos WHERE estado IN ('hecho', 'fallido') AND "
                                                 "COALESCE(terminado, creado) < ?", (lim,))]
        for tid in viejos:
            for ext in ("trabajo.json", "resultado.json", "punto.json"):
                try:
                    (ajustes.TRABAJOS / f"{tid}.{ext}").unlink()
                except OSError:
                    pass
        lim_log = now - 30 * DAY
        for p in ajustes.TRABAJOS.glob("*.log"):
            try:
                if p.stat().st_mtime * 1000 < lim_log:
                    p.unlink()
            except OSError:
                pass

    # ---------------------------------------------------------------- decisiones del usuario (13.6)
    def leer_decisiones(self) -> None:
        """Las decisiones del usuario (13.6). Una aceptación solo vale si la variante SIGUE propuesta (no revocada ni
        descartada en avance desde entonces), si es la última propuesta de esa variante y, si la salida cierra por
        tiempo antes de la ganancia mínima, si trae acepta_excepcion_tiempo: true (la excepción de «6 diarias»). Si no,
        la propuesta se retira con el motivo, o espera a que llegue la aceptación de la excepción. Un descarte vale
        siempre."""
        for d in salida_web.leer_decisiones():
            pid = d["propuesta"]
            ruta = ajustes.PROPUESTAS / f"{pid}.json"
            p = salida_web.leer(ruta)
            if not p or p.get("estado") != "pendiente":
                continue
            vid = p.get("variante")
            limpia = {k: v for k, v in p.items() if k not in ("formato", "ts")}
            dts = d.get("ts")
            if isinstance(dts, (int, float)) and dts >= 1e12 and p.get("creada") and dts < int(p["creada"]):
                continue   # decisión (en ms) anterior a la propuesta: es de otra, no de esta
            with self.con:
                if d["decision"] == "aceptada":
                    v = self._variante(vid) or {}
                    ultima = salida_web.ultima_propuesta(vid)
                    problema = None
                    if v.get("clase") != "propuesta":     # revocada o descartada en avance: ninguna suya vale
                        problema = f"la variante ya no está propuesta (ahora: {v.get('clase') or 'no existe'})"
                        salida_web.retirar_propuestas(self.con, vid, f"aceptación no válida: {problema}")
                    elif ultima != pid:                   # vale la más reciente; esta, no
                        problema = f"hay una propuesta más reciente de la misma variante ({ultima})"
                        salida_web.escribir(ruta, dict(limpia, estado="retirada", retirada=self.reloj(),
                                                       motivo_retirada=f"aceptación no válida: {problema}"))
                    if problema:
                        almacen.evento(self.con, "decision_no_valida", f"Propuesta {pid}: aceptación no válida: {problema}",
                                       vid, d)
                        continue
                    if (p.get("reglas") or {}).get("excepcion_tiempo") and d.get("acepta_excepcion_tiempo") is not True:
                        marca = f"{d.get('ts')}|{d.get('_archivo')}"   # la misma decisión no se apunta dos veces
                        if limpia.get("decision_pendiente") != marca:
                            salida_web.escribir(ruta, dict(limpia, decision_pendiente=marca,
                                                           falta=("aceptar la excepción del cierre por tiempo "
                                                                  "(acepta_excepcion_tiempo: true)")))
                            almacen.evento(self.con, "decision_incompleta",
                                           f"Propuesta {pid}: para aceptarla falta aceptar la excepción del cierre por "
                                           "tiempo (como en «6 diarias»)", vid, d)
                        continue
                    almacen.actualizar_variante(self.con, vid, clase="aceptada")
                else:
                    almacen.actualizar_variante(self.con, vid, clase="descartada_usuario",
                                                motivos=[f"descartada por el usuario: {d.get('nota') or ''}"])
                almacen.evento(self.con, "decision", f"Propuesta {pid}: {d['decision']} por el usuario", vid, d)
            limpia.pop("falta", None)
            limpia["estado"] = d["decision"]
            if d.get("acepta_excepcion_tiempo") is True:
                limpia["excepcion_tiempo_aceptada"] = True
            salida_web.escribir(ruta, limpia)

    # ---------------------------------------------------------------- salidas
    def runtime(self, now: int, ligero: bool = False) -> dict:
        """Lo que cambia a cada vuelta (latido, estado, recursos, en curso). ligero (en las pausas): sin volver a
        escribir cola.json, con el último resumen de la cola."""
        x = agenda.proxima(now, self.tramos) if self.tramos else None
        en = self.en_pausa(now)
        estado = self.estado
        motivo = self.motivo
        if estado == "activo":
            if en:
                estado, motivo = "pausa", f"Pausa por la apertura de {en['nombre']} hasta las {ajustes.hora_mad(en['hasta'])}"
            elif getattr(self, "estado_datos", None) == "sin_datos":
                estado, motivo = "sin_datos", "Preparando la caché de velas de la época"
            elif not self.procs:
                estado = "en_espera" if not motivo else estado
        if ligero and self._cola is not None:
            cola = self._cola
        else:
            cola = salida_web.escribir_cola(self.con, self._en_curso(now))
            cola = {k: cola.get(k) for k in ("pendientes", "por_tipo", "siguientes")}
            cola["siguientes"] = (cola.get("siguientes") or [])[:10]
            self._cola = cola
        return {"version": NOMBRE, "pid": os.getpid(), "arranque": self.arranque, "estado": estado, "motivo": motivo,
                "proxima_pausa": ({"ventana": x["ventana"], "nombre": x["nombre"], "desde": x["desde"],
                                   "hasta": x["hasta"]} if x else None),
                "agenda_origen": self.agenda_origen, "recursos": self.recursos_info,
                "datos": getattr(self, "datos_info", {}), "firma_codigo": self.firma_cod,
                "en_curso": self._en_curso(now), "cola": cola, "diario_lineas": self.diario_lineas,
                "errores": self.errores}

    def _en_curso(self, now: int) -> list[dict]:
        out = []
        for tid, x in self.procs.items():
            pt = None
            try:
                pt = json.loads((ajustes.TRABAJOS / f"{tid}.punto.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            hecho = len((pt or {}).get("variantes") or {})
            out.append({"trabajo": tid, "tipo": x["tipo"], "hipotesis": (x["objetivo"] or "").split(":")[0],
                        "desde": x["t0"], "hechas": hecho})
        return out

    def escribir_salidas(self, completo: bool = False) -> None:
        """estado.json a cada latido; las tarjetas, mejores.json y el diario solo cuando ha cambiado algo (o cada 10
        min) y NUNCA en una pausa de las aperturas: en la pausa, el latido con las últimas tarjetas (11.5)."""
        now = self.reloj()
        pausa = self.en_pausa(now) is not None
        recalcula = completo or self._pesado is None or (
            not pausa and (self.cambios or now - self._pesado_ts >= PESADO_MAX_S * 1000))
        try:
            with self.con:
                if recalcula:
                    self._pesado = salida_web.pesado(self.con)
                    self._pesado_ts = now
                salida_web.escribir_estado(self.con, self.runtime(now, ligero=pausa and not completo), self._pesado)
                if recalcula:
                    salida_web.escribir(ajustes.DIR / "mejores.json", {"mejores": self._pesado["mejores_20"]})
                    salida_web.escribir_diario(self.con, ajustes.dia_mad(now),
                                               {"lecciones": [x.get("texto") for x in (almacen.kv_get(self.con, "lecciones")
                                                                                     or {}).values()]})
                if completo:
                    salida_web.escribir_hipotesis(self.con)
                    salida_web.escribir_generador(self.con, self.estado)
        except Exception:
            log.exception("No pude escribir las salidas")
        self.ultimo_estado = now
        if recalcula:
            self.cambios = False

    # ---------------------------------------------------------------- el bucle
    def paso(self) -> None:
        now = self.reloj()
        self.actualizar_agenda(now)
        self.gestionar_pausa(now)
        self.vigilar_recursos(now)
        self.vigilar_procesos(now)
        self.ingerir()
        if not ajustes.PARAR.exists():
            self.ordenes(now)
            try:
                self.leer_decisiones()
            except Exception:
                log.exception("Decisiones del usuario ilegibles")
            if self.estado not in ("error",):
                self.admitir_buzon()
                self.planificar(now)
                self.lanzar(now)
                if not self.una_pasada:   # en una pasada no hay avance por horas ni consolidación de las 04:30
                    self.tareas_con_hora(now)
        if (self.cambios and not self.en_pausa(now)) or now - self.ultimo_estado >= ajustes.ESTADO_S * 1000:
            self.escribir_salidas()

    def ordenes(self, now: int) -> None:
        """Las órdenes de __main__ con el corredor en marcha (él es el único que escribe en lab_vivo.db):
        heredar_n.json (la herencia del N que acepta el usuario) y consolidar.ahora (la consolidación ahora, fuera de
        las pausas)."""
        if ajustes.HEREDAR_N.exists():
            try:
                o = json.loads(ajustes.HEREDAR_N.read_text(encoding="utf-8"))
                r = self.aplicar_herencia(str(o.get("nota") or ""), str(o.get("por") or "usuario"))
                log.warning("Orden heredar-n aplicada: %s", r)
            except Exception as e:
                self.error(f"Orden heredar-n ilegible o fallida: {e}")
            try:
                ajustes.HEREDAR_N.unlink()
            except OSError:
                pass
        if ajustes.CONSOLIDAR_AHORA.exists() and not self.en_pausa(now) and not self.bloqueo_n:
            try:
                ajustes.CONSOLIDAR_AHORA.unlink()
            except OSError:
                pass
            self.nota("Consolidación pedida con la orden «consolidar»", "consolidacion", guardar=False)
            self.consolidar()

    def parar(self, forzar_tras_s: int = 120) -> None:
        """Parada cooperativa: pausa.ahora; las tareas guardan su punto y salen; a los 2 min se matan las que queden."""
        if self.procs:
            ajustes.PAUSA_AHORA.write_text(json.dumps({"parada": True}), encoding="utf-8")
            t0 = time.time()
            while self.procs and time.time() - t0 < forzar_tras_s:
                self.vigilar_procesos(self.reloj())
                time.sleep(1)
            for tid in list(self.procs):
                self._matar(tid, "parada del corredor", gastar=False)
        self.ingerir()
        self.estado = "parado"
        self.motivo = "Parada pedida (archivo parar)" if ajustes.PARAR.exists() else "Parado"
        self.escribir_salidas()
        try:
            ajustes.PAUSA_AHORA.unlink()
        except OSError:
            pass
        self.soltar_lock()

    # ---------------------------------------------------------------- una pasada (correr --una-pasada)
    def lo_que_queda(self) -> dict:
        """Lo que le queda a una pasada: trabajos pendientes o en curso, hipótesis con etapas por hacer y el buzón. Las
        que esperan al cupo de otro día no la frenan: se cuentan aparte («esperan_cupo»)."""
        trab = Counter(r[0] for r in self.con.execute("SELECT tipo FROM trabajos WHERE estado IN ('pendiente', 'en_curso')"))
        hip = Counter(r[0] for r in self.con.execute("SELECT estado FROM hipotesis"))
        buzon = sum(1 for _ in ajustes.ENTRADA.glob("*.json")) if ajustes.ENTRADA.exists() else 0
        en_marcha = sum(hip[e] for e in EN_MARCHA)
        return {"trabajos": dict(trab), "en_marcha": en_marcha, "esperan_cupo": hip["lista_para_rapida"],
                "buzon": buzon, "procesos": len(self.procs),
                "terminada": not trab and not en_marcha and not buzon and not self.procs}

    def fin_de_pasada(self) -> bool:
        """En una pasada: ¿se acabó? Cuando no queda nada (o el canario falla, o pasa de max_horas) se consolida
        (BH, N efectivo y Sharpe deflactado con el N acumulado, mejores, hipótesis y diario) y se para. Nunca en una
        pausa de las aperturas: la consolidación espera a que acabe."""
        now = self.reloj()
        if self.estado == "error":   # sin reglas o con la base por debajo de n_acumulado.jsonl: ni se mide ni se consolida
            self.error(f"Pasada detenida sin consolidar: {self.motivo}")
            with self.con:
                almacen.kv_set(self.con, "pasada", {"ts": now, "desde": self.arranque, "motivo": self.motivo,
                                                    "queda": self.lo_que_queda()})
            self.escribir_salidas()
            return True
        if self.en_pausa(now):
            return False
        q = self.lo_que_queda()
        if self.estado == "canario_fallido":
            motivo = "el canario no reproduce seis2_resumen.json: no se mide nada"
        elif now - self.arranque > self.max_horas * H:
            motivo = f"más de {self.max_horas:g} h sin terminar (queda: {q['trabajos']})"
        elif q["terminada"]:
            motivo = None
        else:
            return False
        if motivo:
            self.error(f"Pasada detenida: {motivo}")
        cons = self.consolidar()
        with self.con:
            self.nota(f"Pasada terminada{' (' + motivo + ')' if motivo else ''}: N total {cons.get('n_total', almacen.n_total(self.con))}, "
                      f"m de BH {cons['bh_m']}, N_dsr {cons['n_dsr']:.0f}"
                      + (f"; {q['esperan_cupo']} hipótesis esperan al cupo de otro día" if q["esperan_cupo"] else ""),
                      "pasada")
            almacen.kv_set(self.con, "pasada", {"ts": now, "desde": self.arranque, "motivo": motivo, "queda": q})
        self.escribir_salidas(completo=True)
        return True

    def correr(self, una_vez: bool = False) -> int:
        configurar_registro()
        if not ajustes.ACTIVO:
            log.warning("BOT_LAB_VIVO=0: el corredor no arranca")
            return 0
        if not self.tomar_lock():
            return 1
        info = recursos.proceso_bajo(ajustes.CPUS, ajustes.MEMORIA_MB)
        log.info("Corredor %s en marcha (PID %s%s): %s", NOMBRE, os.getpid(), ", una pasada" if self.una_pasada else "",
                 info)
        if not self.arrancar():
            self.escribir_salidas()
            self.soltar_lock()
            return 2
        try:
            while True:
                self.paso()
                if una_vez or ajustes.PARAR.exists():
                    break
                if self.una_pasada and self.fin_de_pasada():
                    break
                time.sleep(ajustes.BUCLE_S)
        except KeyboardInterrupt:
            pass
        finally:
            self.parar()
        return 0
