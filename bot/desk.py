"""Mesa de noticias: recibe lo que traen los cazadores y los feeds, filtra, agrupa repetidas y lo publica en el bus.

Estado de la fuente (bot/fuentes_vivas.py, que feeds marca en cada entrada como Item.estado e Item.peso): va en el patch
de cada captada, descartada y confirmación (srcState, srcWeight). Las de fuentes silenciadas o retiradas se publican
igual (así se siguen midiendo y pueden volver), pero con peso 0 no cuentan en la señal ni como confirmación
independiente, y van al final de la cola de la IA local (fuentes_vivas.prioridad_triaje; una retirada no se tría).
Si la historia sube de estado (la confirma o la trae una fuente mejor), vuelve a la cola con la prioridad nueva cuando
es mayor que la que tuvo (o no entró): sin triaje, la subida no llegaría nunca a la señal."""
import logging
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import config
from .bus import Bus
from .filters import Decision, classify, similar, tokens
from .models import Item, Source, fmt_age, now_ms, story_id
from .store import Store

log = logging.getLogger("desk")
TITLE_MAX = 280          # algunos feeds meten el resumen entero en el título
HUNTER_NOTE_S = 2.0      # con miles de piezas por hora, el «último» del cazador se refresca como mucho cada 2 s
FUTURE_TOL_MS = 10 * 60_000
STALE_MS = 24 * 3600_000
# Segundo nivel de dominios como co.uk, com.br o go.jp: el medio es la etiqueta anterior.
SLD = {"co", "com", "ne", "or", "go", "ac", "gov", "gob", "org", "net", "edu", "gv"}
ESTADOS = ("activa", "prueba", "silenciada", "retirada")   # de mejor a peor (como feeds.ESTADOS)


@dataclass
class Recent:
    ts: int
    story: str
    source: str
    title: str
    coins: list[str]
    words: set[str]
    outlets: set[str] = field(default_factory=set)
    corrob: list[dict] = field(default_factory=list)
    seq: int = 0          # orden de llegada: ante varias parecidas gana la más antigua
    estado: str = "activa"   # el mejor estado de fuente con que ha llegado la historia (srcState)
    # Para volver a mandarla al triaje si sube de estado: lo que pide on_relevant y la prioridad con que se mandó
    name: str = ""
    tier: str = ""
    region: str | None = None
    prio_base: float | None = None   # priority() sin el ajuste por estado (None: no se tría)
    enviada: float | None = None     # prioridad con que se mandó a la IA local (None: nunca)


def outlet_of(item: Item, src: Source) -> str:
    """El medio que firma la pieza: varios feeds del mismo medio no son confirmaciones independientes."""
    if item.outlet:
        return item.outlet.strip().lower()   # lo calcula feeds (dominio del medio, también en Google News)
    if item.via:
        return item.via.strip().lower()   # Google News y relays: el medio original
    host = (urlsplit(item.url or "").hostname or "").lower()
    parts = [p for p in host.split(".") if p and p != "www"]
    if len(parts) >= 3 and parts[-2] in SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) or src.name.lower()


def priority(d: Decision, tier: str) -> float:
    """Orden en la cola de la IA local: lo fresco y fuerte primero; el contexto, cuando haya hueco."""
    return (3 if d.strong else 1) + {"T0": 1.5, "T1": 1.0}.get(tier, 0) + (3 if d.fresh else 0)


def src_state(item: Item) -> tuple[str, float]:
    """(estado, peso) de la fuente de la pieza, como los marca feeds; sin marca (cazadores), activa con peso 1."""
    estado = getattr(item, "estado", None)
    estado = estado if estado in ESTADOS else "activa"
    try:
        peso = float(getattr(item, "peso", 1.0))
    except (TypeError, ValueError):
        peso = 1.0
    return estado, (0.0 if estado in ("silenciada", "retirada") else max(0.0, min(1.0, peso)))


def triage_priority(prio: float, estado: str, story: str) -> float | None:
    """Prioridad en la cola de la IA local según el estado de la fuente (fuentes_vivas.prioridad_triaje); None: no se
    tría. Si fuentes_vivas no se puede importar, la de siempre."""
    if estado == "activa":
        return prio
    try:
        from .fuentes_vivas import prioridad_triaje   # perezoso: fuentes_vivas importa hunters, que importa la mesa
    except Exception:
        log.exception("No pude cargar fuentes_vivas.prioridad_triaje: prioridad normal")
        return prio
    return prioridad_triaje(prio, estado, story)


class NewsDesk:
    def __init__(self, bus: Bus, store: Store, on_relevant=None):
        self.bus = bus
        self.store = store
        self.recent: deque[Recent] = deque()
        # Índice palabra → recientes que la tienen (en orden de llegada): con miles de recientes no se comparan todas.
        self._idx: dict[str, deque[Recent]] = defaultdict(deque)
        self._seq = 0
        self.on_relevant = on_relevant   # siguiente agente: (story, title, src, tier, coins, prio=, region=)
        self._last_note = 0.0

    def handle(self, item: Item, src: Source, warm: bool) -> str | None:
        """Procesa una pieza. Devuelve el evento generado ("captured", "filtered", "corroborated", "srcstate") o None."""
        key = f"{item.source}|{item.uid}"
        if self.store.is_seen(key):
            return None
        now = now_ms()
        self.store.mark_seen(key, now)
        # Una fecha en el futuro (zona horaria mal puesta en el feed) no dice nada de la frescura.
        age_ms = now - item.published if item.published and item.published <= now + FUTURE_TOL_MS else None
        age_ms = max(0, age_ms) if age_ms is not None else None
        if warm and (age_ms is None or age_ms > config.BACKFILL_MAX_S * 1000):
            return None   # histórico que ya existía antes de arrancar
        if age_ms is not None and age_ms > STALE_MS:
            return None   # pieza vieja que un feed vuelve a listar: ni es noticia ni merece un descarte visible
        tier = item.tier or src.tier
        name = f"{src.name} · {item.via}" if item.via else src.name
        title = " ".join(item.title.split())
        if not title:
            return None
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX - 1].rstrip() + "…"
        d = classify(title, src.id, age_ms / 1000 if age_ms is not None else None, item.coins_hint, item.symbols, item.lang)
        sid = story_id(item.source, item.uid)
        outlet = outlet_of(item, src)
        estado, peso = src_state(item)
        base = {"headline": title, "src": name, "srcId": item.source, "via": item.via, "outlet": outlet, "tier": tier,
                "t0": now, "lat": age_ms, "url": item.url, "coins": d.coins, "pub": item.published,
                "region": item.region, "lang": item.lang, "fresh": d.fresh, "topics": d.topics,
                "srcState": estado, "srcWeight": peso}
        self._note(f"Último: «{title[:70]}» · {name}")

        if not d.relevant:
            if warm:
                return "filtered"   # los descartes del primer sondeo no se publican: con miles de feeds serían decenas de miles
            self.bus.publish({"type": "event", "kind": "filtered", "agent": "hunter", "story": sid, "title": title,
                              "coins": d.coins, "text": f"{name} · {tier} → {d.reason}",
                              "patch": {**base, "filter": {"ok": False, "why": d.reason}}})
            return "filtered"

        if warm and not d.fresh and not d.strong:
            return "filtered"   # contexto flojo del primer sondeo (política local, empresas…): miles de piezas sin valor para abrir

        words = tokens(title)
        self._expire(now)
        dup = self._find_dup(words)
        if dup:
            if dup.source == item.source or outlet in dup.outlets:
                # La misma entrada por un feed con mejor estado (feeds la marca como mejora): la historia sube de estado.
                if getattr(item, "mejora", None) and ESTADOS.index(estado) < ESTADOS.index(dup.estado):
                    dup.estado = estado
                    self.bus.publish({"type": "event", "kind": "srcstate", "agent": "hunter", "story": dup.story,
                                      "title": dup.title, "coins": dup.coins,
                                      "text": f"Sube a {estado}: también la trae {name}",
                                      "patch": {"srcState": estado, "srcWeight": peso}})
                    self._retriage(dup)
                    return "srcstate"
                return None   # el mismo medio repite, corrige o publica en otro de sus feeds
            dup.outlets.add(outlet)
            dup.corrob.append({"src": name, "tier": tier, "lat": now - dup.ts, "title": title, "url": item.url,
                               "region": item.region, "outlet": outlet, "srcId": item.source, "lang": item.lang,
                               "pub": item.published, "srcState": estado, "srcWeight": peso})
            patch = {"corrob": list(dup.corrob)}
            sube = ESTADOS.index(estado) < ESTADOS.index(dup.estado)
            if sube:   # la confirma una fuente mejor: la historia sube
                dup.estado = estado
                patch.update(srcState=estado, srcWeight=peso)
            self.bus.publish({"type": "event", "kind": "corroborated", "agent": "hunter", "story": dup.story,
                              "title": dup.title, "coins": dup.coins,
                              "text": f"También lo publica {name} · {tier} · {fmt_age(now - dup.ts)} después",
                              "patch": patch})
            if sube:
                self._retriage(dup)
            return "corroborated"

        rec = Recent(now, sid, item.source, title, d.coins, words, {outlet}, estado=estado, name=name, tier=tier,
                     region=item.region, prio_base=priority(d, tier))
        self._remember(rec)
        if d.fresh:
            when = f"llegó {fmt_age(age_ms)} después de publicarse" if age_ms is not None else "sin hora de publicación"
            why = d.reason + ". Fresca y sin duplicados"
        else:
            ago = fmt_age(age_ms).lstrip("+")
            when = f"contexto: se publicó hace {ago}"
            why = d.reason + f". Contexto (hace {ago}): sirve para preparar aperturas, no para entrar ya"
        self.bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": sid, "title": title,
                          "coins": d.coins, "text": f"{name} · {tier} · {when}",
                          "patch": {**base, "filter": {"ok": True, "why": why}}})
        log.log(logging.INFO if d.fresh else logging.DEBUG, "Relevante%s [%s] %s", "" if d.fresh else " (contexto)", name, title)
        if self.on_relevant:
            prio = triage_priority(rec.prio_base, estado, sid)
            if prio is not None:   # una retirada se publica (para medirla) pero no gasta IA local
                rec.enviada = prio
                self.on_relevant(sid, title, name, tier, d.coins, prio=prio, region=item.region)
        return "captured"

    def _retriage(self, rec: Recent) -> None:
        """La historia subió de estado (de retirada o silenciada a prueba o activa): vuelve a la cola de la IA local con
        la prioridad de su estado nuevo si es mayor que la que tuvo o si no entró. LocalAI no repite una que ya espera
        en la cola (le sube la prioridad) ni una que ya trió."""
        if not self.on_relevant or rec.prio_base is None:
            return
        prio = triage_priority(rec.prio_base, rec.estado, rec.story)
        if prio is None or (rec.enviada is not None and prio <= rec.enviada):
            return
        rec.enviada = prio
        self.on_relevant(rec.story, rec.title, rec.name, rec.tier, rec.coins, prio=prio, region=rec.region)

    def _note(self, text: str) -> None:
        t = time.monotonic()
        if t - self._last_note >= HUNTER_NOTE_S:
            self._last_note = t
            self.bus.publish({"type": "agent", "agent": "hunter", "patch": {"now": text}})

    def _remember(self, rec: Recent) -> None:
        self._seq += 1
        rec.seq = self._seq
        self.recent.append(rec)
        for w in rec.words:
            self._idx[w].append(rec)

    def _find_dup(self, words: set[str]) -> Recent | None:
        """La reciente más antigua que cuenta la misma noticia. similar() exige al menos 2 palabras en común, así que
        solo se compara con las que comparten 2 o más (índice invertido) en vez de barrer toda la ventana."""
        hits: Counter[int] = Counter()
        by_seq: dict[int, Recent] = {}
        for w in words:
            for r in self._idx.get(w, ()):
                hits[r.seq] += 1
                by_seq[r.seq] = r
        for seq in sorted(s for s, n in hits.items() if n >= 2):
            if similar(words, by_seq[seq].words):
                return by_seq[seq]
        return None

    def _expire(self, now: int) -> None:
        while self.recent and now - self.recent[0].ts > config.DEDUPE_WINDOW_S * 1000:
            r = self.recent.popleft()
            for w in r.words:   # r es la más antigua de cada lista del índice
                q = self._idx.get(w)
                if not q:
                    continue
                if q[0] is r:
                    q.popleft()
                else:
                    q.remove(r)
                if not q:
                    del self._idx[w]
