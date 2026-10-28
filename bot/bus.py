"""Bus de eventos: guarda, recuerda el último estado y reparte cada mensaje a las webs conectadas."""
import asyncio
import json

from . import config
from .models import now_ms
from .store import Store


CONTEO_KINDS = ("captured", "filtered", "corroborated", "triaged", "lowprio")   # los contadores del cazador y la IA
HUECO_MIN_MS = 5 * 60_000    # con miles de feeds llegan eventos cada pocos segundos: 5 min sin ninguno es estar parado


class Bus:
    def __init__(self, store: Store):
        self.store = store
        self.clients: set[asyncio.Queue] = set()
        self.state: dict[str, dict] = {}   # último mensaje de cada tipo (fuentes, mercado, agenda, agentes)
        self.started = now_ms()
        self._seq = 0
        self.hueco = self._hueco()   # cuánto estuvo parado antes de este arranque (hallazgo n.º 0)

    def _hueco(self) -> dict | None:
        """{desde, hasta, min}: del último evento guardado a este arranque, si pasan de HUECO_MIN_MS; si no, None."""
        try:
            last = self.store.ultima_actividad(self.started)
        except Exception:
            return None
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
        else:
            self.state[kind] = msg
        data = json.dumps(msg, ensure_ascii=False)
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
