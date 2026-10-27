"""Bolsas de referencia y regiones de noticias: horarios, índices y coordenadas. Lo comparten tendencia, backtest,
predicciones, fuentes y la web."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Market:
    key: str
    name: str            # nombre corto para la web
    exchange: str
    tz: str
    open: tuple[int, int]
    close: tuple[int, int]
    lunch: tuple[tuple[int, int], tuple[int, int]] | None
    index: str | None    # símbolo en el gráfico público de Yahoo Finance
    index_name: str
    futures: str | None  # futuro que cotiza fuera de hora (indica cómo abrirá)
    lonlat: tuple[float, float]
    region: str          # región de noticias asociada


MARKETS: dict[str, Market] = {m.key: m for m in [
    Market("tokio", "Tokio", "Bolsa de Tokio (JPX)", "Asia/Tokyo", (9, 0), (15, 30), ((11, 30), (12, 30)), "^N225", "Nikkei 225", None, (139.69, 35.69), "jp"),
    Market("seul", "Seúl", "Bolsa de Corea (KRX)", "Asia/Seoul", (9, 0), (15, 30), None, "^KS11", "KOSPI", None, (126.98, 37.57), "kr"),
    Market("shanghai", "Shanghái", "Bolsa de Shanghái (SSE)", "Asia/Shanghai", (9, 30), (15, 0), ((11, 30), (13, 0)), "000001.SS", "SSE Composite", None, (121.47, 31.23), "cn"),
    Market("hongkong", "Hong Kong", "Bolsa de Hong Kong (HKEX)", "Asia/Hong_Kong", (9, 30), (16, 0), ((12, 0), (13, 0)), "^HSI", "Hang Seng", None, (114.17, 22.32), "hk"),
    Market("dubai", "Dubái", "Bolsa de Dubái (DFM)", "Asia/Dubai", (10, 0), (15, 0), None, None, "DFM General", None, (55.27, 25.20), "me"),
    Market("frankfurt", "Fráncfort", "Bolsa de Fráncfort (Xetra)", "Europe/Berlin", (9, 0), (17, 30), None, "^GDAXI", "DAX 40", None, (8.68, 50.11), "de"),
    Market("londres", "Londres", "Bolsa de Londres (LSE)", "Europe/London", (8, 0), (16, 30), None, "^FTSE", "FTSE 100", None, (-0.13, 51.51), "uk"),
    Market("madrid", "Madrid", "Bolsa de Madrid (BME)", "Europe/Madrid", (9, 0), (17, 30), None, "^IBEX", "IBEX 35", None, (-3.70, 40.42), "es"),
    Market("nuevayork", "Nueva York", "Bolsa de Nueva York (NYSE)", "America/New_York", (9, 30), (16, 0), None, "^GSPC", "S&P 500", "ES=F", (-74.01, 40.71), "us"),
    Market("saopaulo", "São Paulo", "Bolsa de Brasil (B3)", "America/Sao_Paulo", (10, 0), (17, 0), None, "^BVSP", "Ibovespa", None, (-46.63, -23.55), "latam"),
]}

# Región de noticias → bolsa a la que afectan primero ("por cercanía de bolsa").
REGIONS = {
    "jp": ("Japón", "tokio"), "kr": ("Corea del Sur", "seul"), "cn": ("China continental", "shanghai"),
    "hk": ("Hong Kong y Taiwán", "hongkong"), "me": ("Oriente Medio, Ucrania y Rusia", "dubai"),
    "de": ("Alemania y zona euro", "frankfurt"), "uk": ("Reino Unido e Irlanda", "londres"),
    "es": ("España y Portugal", "madrid"), "us": ("Estados Unidos y Canadá", "nuevayork"),
    "latam": ("Latinoamérica", "saopaulo"), "global": ("Cripto y global", None),
}


def _local(m: Market, now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(m.tz))


def _at(m: Market, day, hm: tuple[int, int]) -> datetime:
    return datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=ZoneInfo(m.tz))


def is_trading_day(m: Market, day) -> bool:
    """Lunes a viernes. Los festivos de cada bolsa no se modelan: el gráfico de Yahoo los delata (no hay velas)."""
    return day.weekday() < 5


def session(m: Market, now: datetime | None = None) -> dict:
    """Estado de la sesión: 'abierta', 'pausa' (mediodía), 'preapertura' (menos de 1 h), 'cerrada'.
    Incluye la próxima apertura y el cierre de hoy en UTC."""
    now = now or datetime.now(timezone.utc)
    loc = _local(m, now)
    today = loc.date()
    o, c = _at(m, today, m.open), _at(m, today, m.close)
    trading = is_trading_day(m, today)
    nxt = None
    for k in range(0, 8):
        d = today + timedelta(days=k)
        t = _at(m, d, m.open)
        if is_trading_day(m, d) and t > now:
            nxt = t
            break
    state = "cerrada"
    if trading and o <= now < c:
        state = "abierta"
        if m.lunch and _at(m, today, m.lunch[0]) <= now < _at(m, today, m.lunch[1]):
            state = "pausa"
    elif nxt and (nxt - now) <= timedelta(hours=1):
        state = "preapertura"
    return {"state": state, "open_utc": o.astimezone(timezone.utc) if trading else None,
            "close_utc": c.astimezone(timezone.utc) if trading else None,
            "next_open_utc": nxt.astimezone(timezone.utc) if nxt else None}


def close_utc_on(m: Market, day) -> datetime:
    return _at(m, day, m.close).astimezone(timezone.utc)


def open_utc_on(m: Market, day) -> datetime:
    return _at(m, day, m.open).astimezone(timezone.utc)


def closes_before_open(a: Market, b: Market, day) -> bool:
    """¿La bolsa A ya ha cerrado cuando abre la B ese mismo día (UTC)? Solo entonces A sirve para anticipar a B."""
    return close_utc_on(a, day) <= open_utc_on(b, day)


def ordered_by_close(day) -> list[Market]:
    return sorted(MARKETS.values(), key=lambda m: close_utc_on(m, day))
