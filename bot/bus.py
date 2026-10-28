"""Bus de eventos: guarda, recuerda el último estado y reparte cada mensaje a las webs conectadas."""
import asyncio
import json

from . import config
from .models import now_ms
from .store import Store

# Tipos que solo salen en el cable: no se guardan como último estado ni van en la foto inicial de una web que se conecta
# (las decisiones de las aperturas las pide la web a GET /api/decisiones; docs/04, 2.17).
NO_STATE = frozenset({"decision_apertura"})


class Bus:
    def __init__(self, store: Store):
        self.store = store
        self.clients: set[asyncio.Queue] = set()
        self.state: dict[str, dict] = {}   # último mensaje de cada tipo (fuentes, mercado, agenda, agentes)
        self.started = now_ms()
        self._seq = 0

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
        out = [{"type": "reset", "phase": config.PHASE, "started": self.started}]
        out += list(self.state.values())
        # Los descartes son la gran mayoría con miles de feeds: van aparte para no tapar el resto del historial.
        evs = self.store.recent_events(config.HISTORY_EVENTS, exclude=("filtered",))
        evs += self.store.recent_events(config.HISTORY_FILTERED, only=("filtered",))
        out += sorted(evs, key=lambda e: e["ts"])
        return out
