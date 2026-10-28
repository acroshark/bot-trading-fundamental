"""Agenda macro: calendario oficial del BLS y reuniones de la Fed. La usan el paper (pausa de 10 min antes de un dato
de alto impacto) y el briefing de la IA local antes de cada apertura; el estratega de la fase 2 todavía no (n.º 25).
Las fechas del FOMC están escritas a mano: si la última está a menos de FOMC_AVISO_D días, lo dice el registro y el
mensaje «calendar» (aviso), porque sin ellas no habría pausa por la Fed (hallazgo n.º 33)."""
import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from .bus import Bus
from .models import now_ms

log = logging.getLogger("agenda")
NY = ZoneInfo("America/New_York")
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
HIGH = re.compile(r"consumer price index|employment situation|producer price index", re.I)
MEDIUM = re.compile(r"job openings|employment cost index|real earnings|personal income", re.I)
# Fuente: federalreserve.gov/monetarypolicy/fomccalendars.htm (consultada el 23-10-2026). Las de 2027 son provisionales.
FOMC_DAYS = ["2026-10-28", "2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
             "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08"]
FOMC_AVISO_D = 90   # con la última fecha del FOMC a menos de esto, se avisa: hay que añadir las del año siguiente


def fomc_aviso(now: int) -> str | None:
    """Aviso si el calendario FOMC escrito a mano se acaba en menos de FOMC_AVISO_D días (o ya se acabó); si no, None."""
    last = datetime.fromisoformat(FOMC_DAYS[-1] + "T14:00:00").replace(tzinfo=NY)
    dias = (last.timestamp() * 1000 - now) / 86_400_000
    if dias >= FOMC_AVISO_D:
        return None
    cuando = f"se acaba el {last:%d-%m-%Y}" if dias >= 0 else f"se acabó el {last:%d-%m-%Y}"
    return (f"El calendario de la Fed (FOMC) escrito a mano en bot/agenda.py {cuando}: añade las fechas del año "
            "siguiente (federalreserve.gov/monetarypolicy/fomccalendars.htm); sin ellas no hay pausa macro por la Fed.")


def parse_ics(text: str) -> list[dict]:
    out = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        m = re.search(r"DTSTART(?:;TZID=[^:]+)?:(\d{8}T\d{6})", block)
        s = re.search(r"SUMMARY:(.+)", block)
        if not (m and s):
            continue
        name = s.group(1).strip()
        impact = "alto" if HIGH.search(name) else "medio" if MEDIUM.search(name) else None
        if not impact:
            continue
        local = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=NY)
        out.append({"t": int(local.timestamp() * 1000), "name": name, "src": "BLS", "impact": impact})
    return out


def fomc_events() -> list[dict]:
    out = []
    for d in FOMC_DAYS:
        local = datetime.fromisoformat(d + "T14:00:00").replace(tzinfo=NY)
        out.append({"t": int(local.timestamp() * 1000), "name": "Decisión de tipos de la Fed (FOMC)", "src": "Fed", "impact": "alto"})
    return out


async def run(bus: Bus, session: aiohttp.ClientSession) -> None:
    while True:
        events = fomc_events()
        try:
            async with session.get(BLS_ICS) as r:
                r.raise_for_status()
                events += parse_ics(await r.text())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Calendario BLS no disponible: %s", e)
        now = now_ms()
        upcoming = sorted((e for e in events if now - 3600_000 < e["t"] < now + 21 * 86400_000), key=lambda e: e["t"])
        aviso = fomc_aviso(now)
        if aviso:
            log.warning("Agenda: %s", aviso)
        bus.publish({"type": "calendar", "events": upcoming, "updated": now, "aviso": aviso})
        await asyncio.sleep(6 * 3600)
