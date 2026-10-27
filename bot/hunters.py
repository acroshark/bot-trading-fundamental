"""Cazadores de noticias: un lector por fuente gratuita. Cada parse_* es una función pura para poder probarla."""
import asyncio
import calendar
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import feedparser

from . import config
from .desk import NewsDesk
from .models import Item, Source, now_ms

log = logging.getLogger("hunters")
TAG_RX = re.compile(r"<[^>]+>")


# ---------- Lectores (funciones puras) ----------
def parse_rss(raw: bytes, source: str) -> list[Item]:
    feed = feedparser.parse(raw)
    out = []
    for e in feed.entries:
        title = TAG_RX.sub("", e.get("title", "")).strip()
        st = e.get("published_parsed") or e.get("updated_parsed")
        out.append(Item(source, e.get("id") or e.get("link") or title, title, e.get("link", ""),
                        calendar.timegm(st) * 1000 if st else None))
    return out


def parse_tree(data: list, source: str) -> list[Item]:
    out = []
    for n in data:
        title = (n.get("title") or n.get("en") or "").strip()
        syms = [s.split("_")[0].replace("USDT", "") for s in (n.get("symbols") or [])]
        via = n.get("source") or "Tree"
        tier = "T2" if via in ("Twitter", "Blogs", "Reddit") else "T1"
        if via == "Twitter":
            via = "X"
        out.append(Item(source, str(n.get("_id") or n.get("time")), title, n.get("url") or "", n.get("time"),
                        coins_hint=[s for s in syms if s in config.COINS], symbols=syms, tier=tier, via=via))
    return out


def parse_okx(data: dict, source: str) -> list[Item]:
    out = []
    for block in data.get("data", []):
        for a in block.get("details", []):
            out.append(Item(source, a["url"], a["title"], a["url"], int(a["pTime"]) if a.get("pTime") else None))
    return out


def parse_binance(data: dict, source: str) -> list[Item]:
    out = []
    for cat in data.get("data", {}).get("catalogs", []):
        if cat.get("catalogId") not in (48, 49, 161):   # nuevos listados, noticias y exclusiones
            continue
        for a in cat.get("articles", []):
            url = f"https://www.binance.com/en/support/announcement/detail/{a['code']}"
            out.append(Item(source, a["code"], a["title"], url, a.get("releaseDate")))
    return out


def parse_upbit(data: dict, source: str) -> list[Item]:
    out = []
    for n in data.get("data", {}).get("notices", []):
        ts = datetime.fromisoformat(n["listed_at"]).timestamp() * 1000 if n.get("listed_at") else None
        syms = re.findall(r"\(([A-Z0-9]{2,10})\)", n["title"])
        out.append(Item(source, str(n["id"]), n["title"], f"https://upbit.com/service_center/notice?id={n['id']}",
                        int(ts) if ts else None, coins_hint=[s for s in syms if s in config.COINS], symbols=syms))
    return out


def parse_status(data: dict, source: str, exchange: str) -> list[Item]:
    out = []
    for inc in data.get("incidents", [])[:20]:
        ts = datetime.fromisoformat(inc["created_at"].replace("Z", "+00:00")).timestamp() * 1000
        title = f"{exchange}: {inc['name']} ({inc.get('impact', 'sin impacto')})"
        out.append(Item(source, inc["id"], title, inc.get("shortlink", ""), int(ts)))
    return out


def parse_edgar(data: dict, source: str, coin: str) -> list[Item]:
    out = []
    for h in data.get("hits", {}).get("hits", []):
        s = h["_source"]
        adsh = s["adsh"]
        fname = h["_id"].split(":", 1)[1] if ":" in h["_id"] else ""
        cik = int(s["ciks"][0]) if s.get("ciks") else 0
        name = re.sub(r"\s*\(CIK \d+\)", "", (s.get("display_names") or ["?"])[0]).strip()
        desc = (s.get("file_description") or "").strip().lower()
        title = f"{name} presenta {s.get('form', '?')} en la SEC" + (f" ({desc})" if desc else "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fname}"
        out.append(Item(source, adsh, title, url, None, coins_hint=[coin]))
    return out


# ---------- Descargas ----------
async def get_bytes(s: aiohttp.ClientSession, url: str, headers: dict | None = None) -> bytes:
    async with s.get(url, headers=headers) as r:
        r.raise_for_status()
        return await r.read()


async def get_json(s: aiohttp.ClientSession, url: str, headers: dict | None = None):
    async with s.get(url, headers=headers) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


def rss(id_, name, tier, interval, url, headers=None):
    return Source(id_, name, tier, interval, lambda s: _rss(s, url, id_, headers))


async def _rss(s, url, id_, headers):
    return parse_rss(await get_bytes(s, url, headers), id_)


async def _edgar(s):
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=1)).isoformat()
    items = []
    for coin, q in (("BTC", "bitcoin"), ("ETH", "ethereum"), ("SOL", "solana"), ("XRP", "xrp")):
        url = (f"https://efts.sec.gov/LATEST/search-index?q=%22{q}%22&forms=8-K,S-1,S-1/A,19b-4,S-3"
               f"&dateRange=custom&startdt={start}&enddt={today.isoformat()}")
        items += parse_edgar(await get_json(s, url, {"User-Agent": config.SEC_USER_AGENT}), "edgar", coin)
    return items


SEC = {"User-Agent": config.SEC_USER_AGENT}
SOURCES = [
    Source("tree", "Tree News", "T1", 15, lambda s: _json(s, "https://news.treeofalpha.com/api/news?limit=40", parse_tree, "tree")),
    Source("okx", "Anuncios OKX", "T0", 30,
           lambda s: _json(s, "https://eea.okx.com/api/v5/support/announcements", parse_okx, "okx")),
    Source("binance", "Anuncios Binance", "T0", 30, lambda s: _json(
        s, "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=10",
        parse_binance, "binance")),
    Source("upbit", "Avisos Upbit", "T0", 30, lambda s: _json(
        s, "https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page=20&category=all", parse_upbit, "upbit")),
    rss("fed", "Reserva Federal", "T0", 60, "https://www.federalreserve.gov/feeds/press_all.xml"),
    rss("bls", "BLS (empleo e IPC)", "T0", 60, "https://www.bls.gov/feed/bls_latest.rss"),
    rss("sec_press", "SEC · prensa", "T0", 60, "https://www.sec.gov/news/pressreleases.rss", SEC),
    rss("ecb", "BCE", "T0", 120, "https://www.ecb.europa.eu/rss/press.html"),
    Source("edgar", "SEC EDGAR", "T0", 180, _edgar),
    Source("cb_status", "Estado Coinbase", "T0", 120, lambda s: _json(
        s, "https://status.coinbase.com/api/v2/incidents.json", lambda d, i: parse_status(d, i, "Coinbase"), "cb_status")),
    Source("kr_status", "Estado Kraken", "T0", 120, lambda s: _json(
        s, "https://status.kraken.com/api/v2/incidents.json", lambda d, i: parse_status(d, i, "Kraken"), "kr_status")),
    rss("coindesk", "CoinDesk", "T2", 60, "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    rss("cointelegraph", "Cointelegraph", "T2", 60, "https://cointelegraph.com/rss"),
    rss("theblock", "The Block", "T2", 60, "https://www.theblock.co/rss.xml"),
    rss("decrypt", "Decrypt", "T2", 90, "https://decrypt.co/feed"),
    rss("btcmag", "Bitcoin Magazine", "T2", 120, "https://bitcoinmagazine.com/.rss/full/"),
]


async def _json(s, url, parser, id_):
    return parser(await get_json(s, url), id_)


# ---------- Bucle de cada fuente ----------
class SourceState:
    def __init__(self, src: Source):
        self.src = src
        self.n = 0
        self.ok: bool | None = None
        self.err = ""
        self.last = 0
        self.lat: int | None = None
        self.rtt: int | None = None

    def view(self) -> dict:
        return {"id": self.src.id, "name": self.src.name, "tier": self.src.tier, "n": self.n, "ok": self.ok,
                "err": self.err, "last": self.last, "lat": self.lat, "rtt": self.rtt, "every": self.src.interval}


async def run_source(st: SourceState, session: aiohttp.ClientSession, desk: NewsDesk) -> None:
    src, warm, wait = st.src, True, st.src.interval
    await asyncio.sleep(random.uniform(0, 3))   # que no salgan todas a la vez
    while True:
        t = time.perf_counter()
        try:
            items = await src.fetch(session)
            st.rtt = int((time.perf_counter() - t) * 1000)
            for it in sorted(items, key=lambda i: i.published or 0):
                if desk.handle(it, src, warm):
                    st.n += 1
                    if it.published:
                        st.lat = max(0, now_ms() - it.published)
            st.ok, st.err, st.last = True, "", now_ms()
            warm, wait = False, src.interval
        except asyncio.CancelledError:
            raise
        except Exception as e:  # una fuente caída no debe tumbar al resto
            st.ok, st.err = False, (str(e) or type(e).__name__)[:160]
            wait = min(wait * 2, 600)
            log.warning("Fuente %s falla: %s (reintento en %.0f s)", src.id, st.err, wait)
        await asyncio.sleep(wait * random.uniform(0.9, 1.1))
