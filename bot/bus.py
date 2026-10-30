"""Bus de eventos: guarda, recuerda el último estado y reparte cada mensaje a las webs conectadas."""
import asyncio
import json
import math
from pathlib import Path

from . import config
from .models import now_ms
from .store import Store

# Tipos que solo salen en el cable: no se guardan como último estado ni van en la foto inicial de una web que se conecta
# (las decisiones de las aperturas las pide la web a GET /api/decisiones; docs/04, 2.17).
NO_STATE = frozenset({"decision_apertura"})


def _sin_nan(o):
    """o con NaN e ±infinito pasados a None (en listas, tuplas y diccionarios, a cualquier profundidad)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _sin_nan(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sin_nan(v) for v in o]
    return o


def dumps_web(o) -> str:
    """JSON que el navegador sí lee: sin NaN ni Infinity (JSON.parse no los admite y, en la foto inicial del
    WebSocket, un solo NaN le haría perder a la web todo su estado). Lo normal sale tal cual, sin coste extra; solo si
    algo trae un número no finito se limpia (pasa a null) y se vuelve a escribir."""
    try:
        return json.dumps(o, ensure_ascii=False, allow_nan=False)
    except ValueError as e:
        if "Out of range float" not in str(e):   # otro fallo (una referencia circular…): como antes, sale el error
            raise
        return json.dumps(_sin_nan(o), ensure_ascii=False, allow_nan=False)


CONTEO_KINDS = ("captured", "filtered", "corroborated", "triaged", "lowprio")   # los contadores del cazador y la IA
HUECO_MIN_MS = 5 * 60_000    # con miles de feeds llegan eventos cada pocos segundos: 5 min sin ninguno es estar parado


class Bus:
    def __init__(self, store: Store, latido: Path | str | None = None):
        self.store = store
        self.latido = Path(latido) if latido else None   # archivo que bot/salud.py toca cada minuto
        self.clients: set[asyncio.Queue] = set()
        self.state: dict[str, dict] = {}   # último mensaje de cada tipo (fuentes, mercado, agenda, agentes)
        self.started = now_ms()
        self._seq = 0
        self.hueco = self._hueco()   # cuánto estuvo parado antes de este arranque (hallazgo n.º 0)

    def _hueco(self) -> dict | None:
        """{desde, hasta, min}: de la última señal de vida (el último evento guardado o el latido, lo más reciente) a
        este arranque, si pasan de HUECO_MIN_MS; si no, None. Con el PC sin red no se guardan eventos, pero el latido
        sigue: así un rato sin red con el bot vivo no cuenta como parado."""
        try:
            last = self.store.ultima_actividad(self.started)
        except Exception:
            return None
        try:
            lat = int(self.latido.stat().st_mtime * 1000) if self.latido else None
        except OSError:
            lat = None
        if lat is not None and lat < self.started:
            last = max(last or 0, lat)
        if last is None or self.started - last < HUECO_MIN_MS:
            return None
        return {"desde": last, "hasta": self.started, "min": round((self.started - last) / 60_000)}

    def publish(self, msg: dict) -> dict:
        kind = msg["type"]
        if kind == "event":
            msg.setdefault("ts", now_ms())
            self._seq += 1
            msg.setdefault("id", f"e{msg['ts']}-{self._seq}")
            self.store.add_event(msg)
        elif kind == "agent":
            key = "agent:" + msg["agent"]
            prev = self.state.get(key, {"type": "agent", "agent": msg["agent"], "patch": {}})
            prev["patch"] = {**prev["patch"], **msg.get("patch", {})}
            self.state[key] = prev
        elif kind not in NO_STATE:
            self.state[kind] = msg
        data = dumps_web(msg)
        for q in list(self.clients):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:   # una web que no da abasto sale del bus y se le cierra la conexión
                self.clients.discard(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)   # aviso al servidor: cierra el WebSocket y la web vuelve con una foto nueva
        return msg

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def snapshot(self) -> list[dict]:
        """Lo que recibe una web al conectarse: reinicio, estado actual e historial reciente."""
        out = [{"type": "reset", "phase": config.PHASE, "started": self.started, "hueco": self.hueco}]
        out += list(self.state.values())
        # Los descartes son la gran mayoría con miles de feeds: van aparte para no tapar el resto del historial.
        evs = self.store.recent_events(config.HISTORY_EVENTS, exclude=("filtered",))
        evs += self.store.recent_events(config.HISTORY_FILTERED, only=("filtered",))
        out += sorted(evs, key=lambda e: e["ts"])
        out.append(self.conteo())   # al final: la web cambia lo contado con la foto por los totales reales
        return out

    def conteo(self, now: int | None = None) -> dict:
        """Totales de eventos por tipo desde el arranque (24 h como mucho: los descartes se borran a las 48 h). La foto
        solo trae los últimos 1.500 eventos y 300 descartes: con ella la web daba que pasaba un 79 % cuando pasaba un
        37 % (hallazgo n.º 8)."""
        now = now_ms() if now is None else now
        desde = max(self.started, now - 24 * 3600_000)
        kinds = {}
        for k in CONTEO_KINDS:
            try:
                kinds[k] = self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind = ? AND ts >= ?",
                                                 (k, desde)).fetchone()[0]
            except Exception:
                return {"type": "conteo", "desde": desde, "kinds": {}}
        return {"type": "conteo", "desde": desde, "kinds": kinds}
