import asyncio
import json
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import aiohttp
import pytest

from bot import trend as tr
from bot.regions import MARKETS
from bot.trend import (OrderFlowWindow, Trend, imbalance, last_session, market_state, parse_agg_trade, parse_chart,
                       parse_daily, trend_label)

UTC = timezone.utc
TOKIO, NY = MARKETS["tokio"], MARKETS["nuevayork"]


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def chart(meta: dict, ts: list | None, quote: dict) -> dict:
    """Forma exacta de /v8/finance/chart: sin velas Yahoo omite "timestamp" y deja quote como [{}]."""
    r = {"meta": meta, "indicators": {"quote": [quote]}}
    if ts is not None:
        r["timestamp"] = ts
    return {"chart": {"result": [r], "error": None}}


def period(tz: str, start: int, end: int, off: int) -> dict:
    return {"timezone": tz, "start": start, "end": end, "gmtoffset": off}


# ---- Muestras recortadas de respuestas reales de Yahoo (23-10-2026)

# Nikkei 225 a las 03:00 UTC (12:00 JST): Yahoo da velas nulas desde las 11:40 JST por la pausa de mediodía.
N225_CLOSE = [62139.12, 61514.33, 61956.63, 61871.04, 61683.96, 61912.11, 61903.15, 61830.15, 62100.46, 62294.33,
              62072.54, 62221.32, 62151.32, 61954.92, 61767.36, 61818.66, 61726.12, 61790.82, 61667.76, 61768.6,
              61933.36, 61921.2, 61859.84, 61785.06, 61646.18, 61674.66, 61647.3, 61577.03, 61715.41, 61767.16,
              61825.26, 61825.26] + [None] * 5
N225_TS = [1792713600 + 300 * k for k in range(37)]
N225_META = {"currency": "JPY", "symbol": "^N225", "exchangeName": "OSA", "instrumentType": "INDEX",
             "regularMarketTime": 1792722900, "gmtoffset": 32400, "timezone": "JST", "exchangeTimezoneName": "Asia/Tokyo",
             "regularMarketPrice": 61825.26, "regularMarketDayHigh": 62681.88, "regularMarketDayLow": 61462.61,
             "chartPreviousClose": 62907.84, "previousClose": 62907.84, "priceHint": 2,
             "currentTradingPeriod": {"regular": period("JST", 1792713600, 1792737000, 32400)},
             "dataGranularity": "5m", "range": "1d"}
N225_PAUSA = chart(N225_META, N225_TS, {
    "open": [62667.05, 62160.3, 61528.67, 61980.4, 61839.41, 61714.39, 61890.09, 61882.64, 61813.7, 62095.21,
             62290.14, 62069.64, 62228.28, 62142.7, 61971.41, 61758.33, 61795.45, 61728.35, 61773.85, 61690.4,
             61762.68, 61943.17, 61922.38, 61862.48, 61781.28, 61636.04, 61672.47, 61649.22, 61572.44, 61716.53,
             61769.2, 61825.26] + [None] * 5,
    "high": [62681.88, 62265.45, 61956.63, 62011.89, 62031.65, 61930.04, 61974.28, 61964.99, 62100.46, 62333.33,
             62298.14, 62230.91, 62242.82, 62142.7, 61978.27, 61818.66, 61887.57, 61906.26, 61773.85, 61806.77,
             62031.89, 62060.4, 61940.68, 61862.48, 61781.28, 61742.48, 61732.51, 61649.22, 61737.1, 61767.16,
             61825.26, 61825.26] + [None] * 5,
    "low": [62079.72, 61514.33, 61462.61, 61831.49, 61683.52, 61671.81, 61837.65, 61778.55, 61803.9, 61990.99,
            62072.54, 62040.45, 62010.72, 61921.82, 61766.2, 61546.98, 61726.12, 61696.27, 61607.22, 61607.47,
            61710.91, 61914.8, 61843.37, 61721.12, 61641.93, 61636.04, 61644.72, 61555.2, 61561.25, 61655.96,
            61769.2, 61825.26] + [None] * 5,
    "close": N225_CLOSE, "volume": [0] * 32 + [None] * 5})

# Nikkei después del cierre (pedido a las 16:00 UTC): últimas 6 velas; regularMarketTime 15:45:03 JST (cierre 15:30).
N225_CIERRE_META = dict(N225_META, regularMarketTime=1792737903, regularMarketPrice=62693.16,
                        regularMarketDayHigh=62786.89)
N225_CIERRE_TS = [1792735500, 1792735800, 1792736100, 1792736400, 1792736700, 1792737000]
N225_CIERRE_Q = {"open": [62578.80078125, 62591.8515625, 62647.8984375, 62759.05078125, 62726.828125, 62693.16015625],
                 "high": [62608.12890625, 62670.05859375, 62769.37109375, 62786.890625, 62726.828125, 62693.16015625],
                 "low": [62563.05078125, 62569.71875, 62647.75, 62698.94921875, 62726.828125, 62693.16015625],
                 "close": [62582.5390625, 62658.51953125, 62750.5, 62734.8984375, 62726.828125, 62693.16015625],
                 "volume": [0] * 6}
N225_CIERRE = chart(N225_CIERRE_META, N225_CIERRE_TS, N225_CIERRE_Q)

# Muestra sintética: no hay respuesta real de un festivo. Es la cabecera real del 23-10 sin velas y con
# chartPreviousClose igual al último precio (rango vacío). En fin de semana Yahoo devuelve las velas de la sesión
# anterior (comprobado el sábado 24-10 a la 01:00 JST), caso que cubre test_festivo_con_velas_del_dia_anterior.
N225_FESTIVO = chart({
    "currency": "JPY", "symbol": "^N225", "exchangeName": "OSA", "instrumentType": "INDEX",
    "regularMarketTime": 1792737903, "gmtoffset": 32400, "timezone": "JST", "exchangeTimezoneName": "Asia/Tokyo",
    "regularMarketPrice": 62693.16, "regularMarketDayHigh": 62786.89, "regularMarketDayLow": 61462.61,
    "chartPreviousClose": 62693.16, "previousClose": 62907.84, "priceHint": 2,
    "currentTradingPeriod": {"regular": period("JST", 1792713600, 1792737000, 32400)},
    "dataGranularity": "5m", "range": "1d"}, None, {})

# Hang Seng después del cierre: durante la subasta de cierre (16:00-16:08 HKT) el índice se queda congelado en
# 23116,04 (vela plana de las 16:00) y el cierre real, 23093,37, llega a las 16:08:30 (regularMarketTime).
HSI_META = {"currency": "HKD", "symbol": "^HSI", "exchangeName": "HKG", "instrumentType": "INDEX",
            "regularMarketTime": 1792742910, "gmtoffset": 28800, "timezone": "HKT", "exchangeTimezoneName": "Asia/Hong_Kong",
            "regularMarketPrice": 23093.37, "regularMarketDayHigh": 23231.63, "regularMarketDayLow": 22803.77,
            "chartPreviousClose": 22934.68, "previousClose": 22934.68, "priceHint": 2,
            "currentTradingPeriod": {"regular": period("HKT", 1792719000, 1792743000, 28800)},
            "dataGranularity": "5m", "range": "1d"}
HSI_TS = [1792741200, 1792741500, 1792741800, 1792742100, 1792742400, 1792742700]
HSI_Q = {"open": [23121.9296875, 23119.330078125, 23123.8203125, 23120.2890625, 23116.0390625, 23116.0390625],
         "high": [23125.16015625, 23126.9296875, 23126.7109375, 23128.44921875, 23116.0390625, 23116.0390625],
         "low": [23113.369140625, 23102.140625, 23115.48046875, 23108.76953125, 23116.0390625, 23093.369140625],
         "close": [23119.689453125, 23124.380859375, 23121.099609375, 23115.509765625, 23116.0390625, 23093.369140625],
         "volume": [0] * 6}
HSI_CIERRE = chart(HSI_META, HSI_TS, HSI_Q)
# Reconstrucción de la misma respuesta pedida a las 16:00:30 HKT, en plena subasta (sin la vela de las 16:05).
HSI_SUBASTA = chart(dict(HSI_META, regularMarketTime=1792742430, regularMarketPrice=23116.04), HSI_TS[:5],
                    {k: v[:5] for k, v in HSI_Q.items()})

# Diario de 5 días del Nikkei: Yahoo deja a null el cierre de hoy aunque la sesión ya terminó.
N225_DIARIO = chart({"symbol": "^N225", "exchangeTimezoneName": "Asia/Tokyo", "regularMarketTime": 1792737903,
                     "regularMarketPrice": 62693.16, "chartPreviousClose": 64916.7, "priceHint": 2,
                     "dataGranularity": "1d", "range": "5d"},
                    [1792368000, 1792454400, 1792540800, 1792627200, 1792713600],
                    {"close": [64916.69921875, 64075.421875, 64621.23828125, 62907.83984375, None],
                     "open": [None] * 5, "high": [None] * 5, "low": [None] * 5, "volume": [0] * 5})
# El mismo diario pedido a mediodía: la cabecera es la de N225_PAUSA (12:15 JST), que no es el cierre.
N225_DIARIO_MEDIODIA = json.loads(json.dumps(N225_DIARIO))
N225_DIARIO_MEDIODIA["chart"]["result"][0]["meta"].update(regularMarketTime=1792722900, regularMarketPrice=61825.26)

# Diario de 5 días del S&P 500 pedido en plena sesión (16:08 UTC): Yahoo pone en el cierre de hoy el precio del momento.
GSPC_DIARIO = chart({"currency": "USD", "symbol": "^GSPC", "instrumentType": "INDEX", "regularMarketTime": 1792771715,
                     "exchangeTimezoneName": "America/New_York", "regularMarketPrice": 7653.76,
                     "chartPreviousClose": 7659.44, "priceHint": 2, "dataGranularity": "1d", "range": "5d"},
                    [1792416600, 1792503000, 1792589400, 1792675800, 1792762200],
                    {"open": [7702.77001953125, 7679.009765625, 7736.43017578125, 7694.6201171875, 7637.18017578125],
                     "high": [7731.18017578125, 7748.419921875, 7770.39990234375, 7702.93994140625, 7660.14990234375],
                     "low": [7684.7900390625, 7668.02978515625, 7709.33984375, 7637.35986328125, 7612.06982421875],
                     "close": [7690.72998046875, 7712.56982421875, 7757.169921875, 7663.43017578125, 7653.759765625],
                     "volume": [4587260000, 5004770000, 4921480000, 5972620000, 1043894000]})

# S&P 500 a las 14:10 UTC y su futuro ES=F.
GSPC = chart({"currency": "USD", "symbol": "^GSPC", "instrumentType": "INDEX", "regularMarketTime": 1792764600,
              "exchangeTimezoneName": "America/New_York", "regularMarketPrice": 7630.83, "regularMarketDayHigh": 7637.48,
              "regularMarketDayLow": 7615.69, "chartPreviousClose": 7663.43, "previousClose": 7663.43, "priceHint": 2,
              "dataGranularity": "5m", "range": "1d"},
             [1792762200 + 300 * k for k in range(8)],
             {"open": [7637.18, 7623.59, 7617.35, 7630.6, 7631.51, 7628.81, 7633.84, 7631.24],
              "high": [7637.18, 7623.68, 7631.81, 7633.39, 7636.34, 7635.06, 7637.48, 7632.96],
              "low": [7621.38, 7615.92, 7615.69, 7626.7, 7627.12, 7625.57, 7628.46, 7626.61],
              "close": [7623.12, 7617.01, 7630.29, 7631.24, 7628.68, 7634.84, 7631.21, 7630.83],
              "volume": [49087352, 51564326, 42885445, 44441101, 35509159, 34973089, 39195707, 34502786]})
ESF = chart({"currency": "USD", "symbol": "ES=F", "instrumentType": "FUTURE", "regularMarketTime": 1792767883,
             "exchangeTimezoneName": "America/New_York", "regularMarketPrice": 7687.75, "chartPreviousClose": 7704.5,
             "previousClose": 7704.5, "priceHint": 2, "dataGranularity": "5m", "range": "1d"},
            [1792728300 + 300 * k for k in range(4)],
            {"open": [7702.0, 7701.5, 7698.5, 7697.0], "high": [7702.25, 7703.25, 7699.25, 7698.25],
             "low": [7701.0, 7698.25, 7695.5, 7695.0], "close": [7701.5, 7698.75, 7697.25, 7697.0],
             "volume": [0, 747, 945, 441]})

# ---- Muestras reales de OKX EU y Binance

BOOKS5_BTC = {"arg": {"channel": "books5", "instId": "BTC-USDC"}, "data": [{
    "asks": [["81473", "0.0016", "0", "1"], ["81476.2", "0.03137567", "0", "1"], ["81476.3", "0.00030683", "0", "1"],
             ["81476.8", "0.03198861", "0", "1"], ["81477.1", "0.02455095", "0", "1"]],
    "bids": [["81472.9", "0.06601437", "0", "3"], ["81471.7", "0.0319878", "0", "1"], ["81471.6", "0.01872336", "0", "1"],
             ["81470.2", "0.03241867", "0", "1"], ["81470.1", "0.04859893", "0", "1"]],
    "instId": "BTC-USDC", "ts": "1792768358108", "seqId": 5242169904}]}
AGG_BUY = {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "E": 1792768357102, "s": "BTCUSDT", "a": 4096442831,
           "p": "81550.01000000", "q": "0.01000000", "f": 6787112044, "l": 6787112045, "T": 1792768357101,
           "m": False, "M": True}}
AGG_SELL = {"stream": "xrpusdt@aggTrade", "data": {"e": "aggTrade", "E": 1792768357300, "s": "XRPUSDT", "a": 912345678,
            "p": "1.30740000", "q": "500.00000000", "f": 1, "l": 2, "T": 1792768357299, "m": True, "M": True}}
KLINES_BTC = [[1792768260000, "81512.00000000", "81579.36000000", "81512.00000000", "81574.00000000", "12.51558000",
               1792768319999, "1020555.29647170", 2218, "8.64161000", "704651.83497270", "0"],
              [1792768320000, "81574.01000000", "81574.01000000", "81550.00000000", "81550.01000000", "4.45453000",
               1792768379999, "363337.22521100", 1237, "2.67057000", "217827.02530730", "0"]]

TREND_KEYS = {"name", "index", "symbol", "state", "price", "prev_close", "change_pct", "day_high", "day_low",
              "mom_30m_pct", "last_session", "futures", "next_open_utc", "close_utc", "spark", "updated", "error"}
FLOW_KEYS = {"imbalance", "bid_vol", "ask_vol", "spread_bps", "cvd_1m", "cvd_5m", "cvd_15m", "buy_ratio_5m",
             "vol_5m_usd", "vol_ratio", "ret_5m_pct", "ret_15m_pct", "ret_60m_pct", "trend", "venue"}


class FakeBus:
    def __init__(self):
        self.sent = []

    def publish(self, msg):
        json.dumps(msg)   # como el bus de verdad: tiene que ser serializable
        self.sent.append(msg)
        return msg


# ---------------------------------------------------------------- índices

def test_tokio_en_pausa_de_mediodia():
    c = parse_chart(N225_PAUSA, TOKIO)
    assert c["price"] == 61825.26 and c["prev_close"] == 62907.84 and c["change_pct"] == -1.721
    assert len(c["spark"]) == 32 and None not in c["spark"] and c["spark"][-1] == 61825.26
    assert c["mom_30m_pct"] == round((61825.26 / 61674.66 - 1) * 100, 3)   # últimas 6 velas válidas antes de la pausa
    assert c["day_high"] == 62681.88 and c["day_low"] == 61462.61
    assert c["session_date"] == "2026-10-23" and c["last_candle"] == (1792713600 + 31 * 300) * 1000
    st = market_state(TOKIO, utc(2026, 10, 23, 3, 0), c["last_candle"], checked=True)
    assert st["state"] == "pausa" and not st["holiday"] and st["close_utc"] == ms(utc(2026, 10, 23, 6, 30))


def test_festivo_sin_velas():
    c = parse_chart(N225_FESTIVO, TOKIO)
    assert c["spark"] == [] and c["mom_30m_pct"] is None and c["session_date"] is None
    # Sin velas, chartPreviousClose es de un periodo vacío: se usa previousClose.
    assert c["price"] == 62693.16 and c["prev_close"] == 62907.84 and c["change_pct"] == -0.341
    lunes = utc(2026, 10, 12, 2, 0)   # 11:00 JST, Tokio debería estar abierta según el horario
    st = market_state(TOKIO, lunes, c["last_candle"], checked=True)
    assert st["state"] == "cerrada" and st["holiday"] and st["close_utc"] is None
    assert st["next_open_utc"] == ms(utc(2026, 10, 13, 0, 0))
    assert market_state(TOKIO, lunes, None, checked=False)["state"] == "abierta"   # sin datos frescos no se corrige
    assert market_state(TOKIO, utc(2026, 10, 12, 0, 20), None, checked=True)["state"] == "abierta"   # margen por retraso


def test_festivo_con_velas_del_dia_anterior():
    c = parse_chart(N225_PAUSA, TOKIO)   # el lunes Yahoo sigue dando las velas del viernes
    st = market_state(TOKIO, utc(2026, 10, 26, 2, 0), c["last_candle"], checked=True)
    assert st["state"] == "cerrada" and st["holiday"]


def test_spark_como_mucho_120():
    n = 130
    data = chart(dict(N225_META), [1792713600 + 300 * k for k in range(n)],
                 {"open": [100.0 + k for k in range(n)], "close": [100.5 + k for k in range(n)]})
    c = parse_chart(data, TOKIO)
    assert len(c["spark"]) == 120 and c["spark"][-1] == 100.5 + n - 1 and c["spark"][0] == 100.5 + 10


def test_primeras_velas_usan_la_apertura():
    data = chart(dict(N225_META, regularMarketPrice=62100.0), N225_TS[:2],
                 {"open": [62000.0, 62050.0], "close": [62050.0, 62100.0]})
    assert parse_chart(data, TOKIO)["mom_30m_pct"] == round((62100 / 62000 - 1) * 100, 3)


def test_error_de_yahoo():
    with pytest.raises(ValueError, match="No data found"):
        parse_chart({"chart": {"result": None, "error": {"code": "Not Found",
                                                         "description": "No data found, symbol may be delisted"}}}, TOKIO)


def test_ultima_sesion_para_replicar():
    days = parse_daily(N225_DIARIO, TOKIO)
    assert [d["date"] for d in days] == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    assert days[-1]["close"] == 62693.16 and days[-1]["final"]   # el null de hoy se rellena: el dato es del cierre
    # Tras el cierre: la sesión de hoy.
    s = last_session(days, TOKIO, utc(2026, 10, 23, 15, 0))
    assert s == {"date": "2026-10-23", "change_pct": -0.341, "close": 62693.16}
    assert s["change_pct"] == round((62693.16 / 62907.83984375 - 1) * 100, 3)
    # En la pausa de mediodía: la anterior completa.
    s = last_session(days, TOKIO, utc(2026, 10, 23, 3, 0), parse_chart(N225_PAUSA, TOKIO))
    assert s["date"] == "2026-10-22" and s["change_pct"] == round((62907.83984375 / 64621.23828125 - 1) * 100, 3)
    # Cerrada hoy con el gráfico de 5 min de después del cierre: manda el gráfico (se refresca cada minuto).
    s = last_session(days[:-1], TOKIO, utc(2026, 10, 23, 7, 0), parse_chart(N225_CIERRE, TOKIO))
    assert s == {"date": "2026-10-23", "change_pct": -0.341, "close": 62693.16}
    # Cerrada hoy pero con el gráfico del mediodía (lleva horas fallando): no es el cierre, sigue la anterior.
    s = last_session(days[:-1], TOKIO, utc(2026, 10, 23, 14, 15), parse_chart(N225_PAUSA, TOKIO))
    assert s["date"] == "2026-10-22"
    # El lunes antes de abrir: la del viernes, del diario o del gráfico de 5 min (Yahoo sigue dando el viernes).
    assert last_session(days, TOKIO, utc(2026, 10, 25, 23, 30))["date"] == "2026-10-23"
    assert last_session(days[:-1], TOKIO, utc(2026, 10, 25, 23, 30), parse_chart(N225_CIERRE, TOKIO))["close"] == 62693.16


def test_cierre_con_retraso_o_subasta():
    # Índice con retraso: a las 15:40 JST el último dato aún es de las 15:25.
    tarde = parse_chart(chart(dict(N225_CIERRE_META, regularMarketTime=1792736700, regularMarketPrice=62726.83),
                              N225_CIERRE_TS[:5], {k: v[:5] for k, v in N225_CIERRE_Q.items()}), TOKIO)
    assert tarde["session_date"] == "2026-10-23"
    assert last_session([], TOKIO, utc(2026, 10, 23, 6, 40), tarde) is None
    # Hong Kong: con el índice congelado en la subasta no hay cierre; llega a las 16:08:30 HKT.
    HK = MARKETS["hongkong"]
    sub = parse_chart(HSI_SUBASTA, HK)
    assert sub["session_date"] == "2026-10-23" and sub["data_ts"] >= ms(utc(2026, 10, 23, 8, 0))
    assert last_session([], HK, utc(2026, 10, 23, 8, 5), sub) is None
    fin = parse_chart(HSI_CIERRE, HK)
    assert last_session([], HK, utc(2026, 10, 23, 8, 20), fin) == {"date": "2026-10-23", "change_pct": 0.692,
                                                                   "close": 23093.37}


def test_diario_pedido_en_mitad_de_la_sesion():
    # Nikkei: el null de hoy no se rellena con el precio del mediodía.
    days = parse_daily(N225_DIARIO_MEDIODIA, TOKIO)
    assert days[-1]["date"] == "2026-10-22"
    assert last_session(days, TOKIO, utc(2026, 10, 23, 14, 15))["date"] == "2026-10-22"
    # S&P 500: Yahoo da el cierre de hoy con el precio del momento; no es final y no vale tras el cierre.
    days = parse_daily(GSPC_DIARIO, NY)
    assert days[-1] == {"date": "2026-10-23", "close": 7653.76, "change_pct": days[-1]["change_pct"], "final": False}
    assert all(d["final"] for d in days[:-1])
    s = last_session(days, NY, utc(2026, 10, 23, 20, 10))
    assert s == {"date": "2026-10-22", "change_pct": round((7663.43017578125 / 7757.169921875 - 1) * 100, 3),
                 "close": 7663.43}


def test_price_hint_nulo():
    q = N225_PAUSA["chart"]["result"][0]["indicators"]["quote"][0]
    c = parse_chart(chart(dict(N225_META, priceHint=None), N225_TS, q), TOKIO)
    assert c["price"] == 61825.26 and c["prev_close"] == 62907.84 and c["spark"][-1] == 61825.26


async def test_ronda_de_indices_con_fallos(monkeypatch):
    monkeypatch.setattr(tr, "REQ_GAP_S", 0)
    t = Trend(FakeBus(), None)
    calls = []
    answers = {("^N225", "1d"): N225_PAUSA, ("^N225", "5d"): N225_DIARIO, ("^GSPC", "1d"): GSPC, ("ES=F", "1d"): ESF}

    async def fake_yahoo(symbol, rng, interval):
        calls.append((symbol, rng, interval))
        if (symbol, rng) not in answers:
            raise tr.RateLimited("HTTP 429")
        return answers[(symbol, rng)]

    monkeypatch.setattr(t, "_yahoo", fake_yahoo)
    fails, total, limited = await t.refresh_indices()
    assert ("^N225", "1d", "5m") in calls and ("^N225", "5d", "1d") in calls and ("ES=F", "1d", "5m") in calls
    assert not any(s is None for s, _, _ in calls)   # Dubái no tiene índice en Yahoo y no se consulta
    assert total == 9 * 2 + 1 and fails == total - 4 and limited == fails
    msg = t.build_trend(utc(2026, 10, 23, 14, 15))
    m = msg["markets"]
    assert msg["type"] == "trend" and set(m) == {k for k, v in MARKETS.items() if v.index}
    assert all(TREND_KEYS <= set(e) for e in m.values())
    ny = m["nuevayork"]
    assert ny["state"] == "abierta" and ny["change_pct"] == round((7630.83 / 7663.43 - 1) * 100, 3)
    assert ny["futures"] == {"symbol": "ES=F", "change_pct": round((7687.75 / 7704.5 - 1) * 100, 3), "price": 7687.75}
    assert ny["error"] == "diario: HTTP 429" and ny["close_utc"] == ms(utc(2026, 10, 23, 20, 0))
    assert m["tokio"]["error"] is None and m["tokio"]["state"] == "cerrada"
    # El gráfico de 5 min de Tokio es del mediodía: el cierre real sale del diario.
    assert m["tokio"]["last_session"] == {"date": "2026-10-23", "change_pct": -0.341, "close": 62693.16}
    assert m["madrid"]["futures"] is None
    assert m["madrid"]["price"] is None and m["madrid"]["error"].startswith("gráfico de 5 min: HTTP 429")

    # Segunda ronda: falla el S&P 500 y se conservan sus últimos datos; el diario ya no se pide hasta dentro de 1 h.
    del answers[("^GSPC", "1d")]
    calls.clear()
    await t.refresh_indices()
    assert ("^N225", "5d", "1d") not in calls
    ny = t.build_trend(utc(2026, 10, 23, 14, 16))["markets"]["nuevayork"]
    assert ny["price"] == 7630.83 and ny["error"].startswith("gráfico de 5 min: HTTP 429")


async def test_yahoo_429():
    class Resp:
        status = 429

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    t = Trend(FakeBus(), SimpleNamespace(get=lambda url, **kw: Resp()))
    with pytest.raises(tr.RateLimited, match="HTTP 429"):
        await t._yahoo("^N225", "1d", "5m")


async def test_diario_se_repite_tras_el_cierre(monkeypatch):
    """El gráfico de 5 min de Tokio falla desde mediodía: el cierre sale del diario, que se vuelve a pedir media hora
    después del cierre (el de mediodía no lo tenía)."""
    monkeypatch.setattr(tr, "REQ_GAP_S", 0)
    t = Trend(FakeBus(), None)
    calls, answers = [], {("^N225", "1d"): N225_PAUSA, ("^N225", "5d"): N225_DIARIO_MEDIODIA}

    async def fake_yahoo(symbol, rng, interval):
        calls.append((symbol, rng))
        if (symbol, rng) not in answers:
            raise tr.RateLimited("HTTP 429")
        return answers[(symbol, rng)]

    monkeypatch.setattr(t, "_yahoo", fake_yahoo)
    await t.refresh_indices(utc(2026, 10, 23, 3, 15))
    del answers[("^N225", "1d")]
    answers[("^N225", "5d")] = N225_DIARIO
    calls.clear()
    await t.refresh_indices(utc(2026, 10, 23, 6, 50))   # 20 min después del cierre: aún no toca
    assert ("^N225", "5d") not in calls
    tokio = t.build_trend(utc(2026, 10, 23, 6, 50))["markets"]["tokio"]
    assert tokio["price"] == 61825.26 and tokio["last_session"]["date"] == "2026-10-22"   # el del mediodía no vale
    await t.refresh_indices(utc(2026, 10, 23, 7, 0))
    assert ("^N225", "5d") in calls
    calls.clear()
    await t.refresh_indices(utc(2026, 10, 23, 7, 1))
    assert ("^N225", "5d") not in calls   # ya tiene el de después del cierre
    tokio = t.build_trend(utc(2026, 10, 23, 7, 1))["markets"]["tokio"]
    assert tokio["last_session"] == {"date": "2026-10-23", "change_pct": -0.341, "close": 62693.16}


async def test_espera_creciente_y_publicacion_cada_minuto(monkeypatch):
    """Con 429 o más de la mitad de fallos la ronda se espacia, pero «trend» se sigue publicando cada 60 s."""
    clock, slept = [1000.0], []

    async def fake_sleep(s):
        slept.append(s)
        clock[0] += s
        if len(slept) == 8:
            raise asyncio.CancelledError

    monkeypatch.setattr(tr, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(tr, "asyncio", SimpleNamespace(sleep=fake_sleep, CancelledError=asyncio.CancelledError))
    bus = FakeBus()
    t = Trend(bus, None)
    results, rounds = [(18, 19, 1), (12, 19, 0), (2, 19, 0), (0, 19, 0)], []

    async def fake_refresh(now=None):
        rounds.append(clock[0])
        return results[len(rounds) - 1]

    monkeypatch.setattr(t, "refresh_indices", fake_refresh)
    with pytest.raises(asyncio.CancelledError):
        await t._indices_loop()
    assert rounds == [1000, 1120, 1360, 1420]   # 120 s, 240 s y vuelta a 60 s
    assert slept == [60] * 8 and len(bus.sent) == 8 and all(m["type"] == "trend" for m in bus.sent)
    assert t.ok["indices"] and t.err["indices"] == ""


# ---------------------------------------------------------------- libro y operaciones

def test_imbalance_books5_real():
    b = imbalance(BOOKS5_BTC["data"][0])
    bv, av = 0.19774313, 0.08982206
    assert b["bid_vol"] == pytest.approx(bv) and b["ask_vol"] == pytest.approx(av)
    assert b["imbalance"] == round((bv - av) / (bv + av), 4) and 0 < b["imbalance"] < 1
    assert b["spread_bps"] == pytest.approx(0.1 / 81472.95 * 10000, abs=1e-3)
    assert imbalance(BOOKS5_BTC) == b   # también acepta el mensaje entero
    assert imbalance({"asks": [], "bids": []})["imbalance"] is None


def test_agg_trade_comprador_maker_es_venta():
    assert parse_agg_trade(AGG_BUY) == ("BTC", 1792768357101, 81550.01, 0.01, False)
    assert parse_agg_trade(AGG_SELL) == ("XRP", 1792768357299, 1.3074, 500.0, True)
    assert parse_agg_trade({"result": None, "id": 1}) is None


def test_etiqueta_de_tendencia():
    assert trend_label(0.2, 5) == "alcista" and trend_label(-0.2, -5) == "bajista"
    assert trend_label(0.15, 5) == "lateral" and trend_label(0.3, -1) == "lateral" and trend_label(None, 3) == "lateral"


T0 = 1_792_760_000_000


def fill(w: OrderFlowWindow, seconds: int, move_pct: float, buy_q: float, sell_q: float) -> int:
    for i in range(seconds):
        px = 100 * (1 + move_pct / 100 * i / seconds)
        w.add(T0 + i * 1000, px, buy_q, False)
        w.add(T0 + i * 1000 + 500, px, sell_q, True)
    return T0 + (seconds - 1) * 1000 + 600


def test_ventana_alcista():
    w = OrderFlowWindow()
    now = fill(w, 1200, 1.0, 1.0, 0.5)
    m = w.metrics(now)
    assert (m["cvd_1m"], m["cvd_5m"], m["cvd_15m"], m["cvd_60m"]) == (30.0, 150.0, 450.0, 600.0)
    assert m["buy_ratio_5m"] == pytest.approx(2 / 3, abs=1e-4)
    p = lambda i: 100 * (1 + 0.01 * i / 1200)
    assert m["ret_15m_pct"] == round((p(1199) / p(299) - 1) * 100, 3) and m["ret_5m_pct"] > 0
    assert m["ret_60m_pct"] is None   # aún no hay una hora de historia
    assert m["vol_ratio"] == pytest.approx(1.0, abs=0.02) and m["vol_5m_usd"] == pytest.approx(300 * 1.5 * p(1050), rel=0.01)
    assert m["trend"] == "alcista"


def test_ventana_bajista_y_lateral():
    w = OrderFlowWindow()
    assert w.metrics(T0)["trend"] is None   # sin datos
    m = w.metrics(fill(w, 1200, -1.0, 0.5, 1.0))
    assert m["cvd_15m"] < 0 and m["ret_15m_pct"] < -0.15 and m["trend"] == "bajista"
    w2 = OrderFlowWindow()
    m2 = w2.metrics(fill(w2, 1200, 1.0, 0.5, 1.0))   # sube con ventas agresivas: no hay confirmación
    assert m2["ret_15m_pct"] > 0.15 and m2["cvd_15m"] < 0 and m2["trend"] == "lateral"


def test_memoria_acotada_y_retorno_de_una_hora():
    w = OrderFlowWindow()
    for i in range(2 * 3600 * 4):   # 2 h con 4 operaciones por segundo
        w.add(T0 + i * 250, 100 + i * 1e-4, 1.0, i % 2 == 1)
    assert len(w.b) <= OrderFlowWindow.KEEP_S + 1
    now = T0 + (2 * 3600 * 4 - 1) * 250
    m = w.metrics(now)
    assert m["ret_60m_pct"] > 0 and m["vol_ratio"] == pytest.approx(1.0, abs=0.02)
    assert w.metrics(now + 200_000)["cvd_5m"] is None   # sin operaciones recientes las métricas no valen


def test_relleno_con_velas_de_1_min():
    w = OrderFlowWindow()
    w.add(T0, 50.0, 3.0, False)
    now = 1792768357102
    w.seed_klines(KLINES_BTC, now)   # la segunda vela está en curso y se descarta
    assert len(w.b) == 1 and w.b[0][5] == 81574.0 and w.last_ms == 1792768319999
    w.add(*parse_agg_trade(AGG_BUY)[1:])
    m = w.metrics(now)
    assert m["cvd_1m"] == pytest.approx(8.64161 - (12.51558 - 8.64161) + 0.01, abs=1e-3)
    assert m["price"] == 81550.01


def test_relleno_de_una_hora_da_retornos():
    """Con una cubeta por minuto (velas de 1 min, formato de KLINES_BTC) los retornos y vol_ratio valen."""
    base = 1792764720000
    rows = [[base + k * 60000, "100.0", "100.5", "99.5", f"{100 + k * 0.01:.2f}", "2.0", base + k * 60000 + 59999,
             "200.0", 10, "1.2", "120.0", "0"] for k in range(61)]
    now = base + 60 * 60000 + 30000   # la última vela está en curso y se descarta
    w = OrderFlowWindow()
    w.seed_klines(rows, now)
    m = w.metrics(now)
    assert m["ret_5m_pct"] == round((100.59 / 100.54 - 1) * 100, 3) and m["ret_15m_pct"] == round((100.59 / 100.44 - 1) * 100, 3)
    assert m["vol_ratio"] == pytest.approx(1.0, abs=0.05) and m["cvd_5m"] == pytest.approx(5 * 0.4)


def test_ventana_con_hueco():
    """10 min a 100, 45 min sin datos (corte sin relleno) y 2 min a 102: no hay precio de hace 5 ni 15 min, así que
    no hay retornos ni tendencia, y vol_ratio no se calcula con solo 2 min continuos."""
    w = OrderFlowWindow()
    for i in list(range(600)) + list(range(3300, 3420)):
        w.add(T0 + i * 1000, 100.0 if i < 600 else 102.0, 1.0, False)
    m = w.metrics(T0 + 3419_600)
    assert m["ret_5m_pct"] is None and m["ret_15m_pct"] is None and m["ret_60m_pct"] is None
    assert m["trend"] == "lateral" and m["vol_ratio"] is None and m["cvd_1m"] == 60.0
    # 10 min, 40 min de hueco y 15 min al mismo ritmo: la media de volumen solo cuenta el tramo continuo.
    w2 = OrderFlowWindow()
    for i in list(range(600)) + list(range(3000, 3900)):
        w2.add(T0 + i * 1000, 100.0, 1.0, i % 2 == 1)
    m2 = w2.metrics(T0 + 3899_600)
    assert m2["vol_ratio"] == pytest.approx(1.0, abs=0.01)
    assert m2["ret_5m_pct"] == 0.0 and m2["ret_15m_pct"] is None


def test_mensaje_orderflow():
    t = Trend(FakeBus(), None)
    now = 1792768360000
    t.books["BTC"] = {**imbalance(BOOKS5_BTC), "rx": now - 1000}
    t.books["ETH"] = {**imbalance(BOOKS5_BTC), "rx": now - 120_000}   # libro viejo: no vale
    t.windows["BTC"].seed_klines(KLINES_BTC, now)
    msg = t.build_orderflow(now)
    assert msg["type"] == "orderflow" and set(msg["coins"]) == {"BTC", "ETH", "SOL", "XRP"}
    assert all(FLOW_KEYS <= set(c) for c in msg["coins"].values())
    btc, eth = msg["coins"]["BTC"], msg["coins"]["ETH"]
    assert btc["imbalance"] > 0 and btc["cvd_1m"] is not None and btc["venue"] == "OKX EU (libro) + Binance (operaciones)"
    assert eth["imbalance"] is None and eth["trend"] is None
    json.dumps(msg)


# ---------------------------------------------------------------- WebSockets con conexiones falsas

class FakeWS:
    def __init__(self, msgs):
        self.msgs, self.sent = list(msgs), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send_json(self, d):
        self.sent.append(d)

    async def send_str(self, s):
        self.sent.append(s)

    async def receive(self, timeout=None):
        if not self.msgs:
            raise asyncio.CancelledError   # fin de la prueba
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=self.msgs.pop(0))


class FakeSession:
    def __init__(self, ws):
        self.ws, self.urls = ws, []

    def ws_connect(self, url, **kw):
        self.urls.append(url)
        return self.ws

    def get(self, url, **kw):
        raise ConnectionError("sin red en las pruebas")


async def test_libro_okx_por_websocket():
    ws = FakeWS([json.dumps({"event": "subscribe", "arg": {"channel": "books5", "instId": "BTC-USDC"}}),
                 json.dumps(BOOKS5_BTC), "pong"])
    t = Trend(FakeBus(), FakeSession(ws))
    with pytest.raises(asyncio.CancelledError):
        await t._books_loop()
    assert ws.sent[0]["args"][0] == {"channel": "books5", "instId": "BTC-USDC"} and len(ws.sent[0]["args"]) == 4
    assert t.books["BTC"]["imbalance"] > 0 and t.ok["books"] is True


async def test_operaciones_binance_por_websocket():
    ws = FakeWS([json.dumps(AGG_BUY), json.dumps(AGG_SELL)])
    s = FakeSession(ws)
    t = Trend(FakeBus(), s)
    with pytest.raises(asyncio.CancelledError):
        await t._trades_loop()   # el relleno con velas falla (sin red) y sigue con el directo
    assert s.urls == ["wss://data-stream.binance.vision/stream?streams="
                      "btcusdt@aggTrade/ethusdt@aggTrade/solusdt@aggTrade/xrpusdt@aggTrade"]
    assert t.windows["BTC"].b[-1][1] == 0.01 and t.windows["XRP"].b[-1][2] == 500.0 and t.ok["trades"] is True


def test_desequilibrio_del_libro_es_una_media_de_un_minuto():
    """Hallazgo n.º 12: el libro de BTC en OKX EU es tan fino que el desequilibrio pasaba de +0,88 a −0,94 en 20 s; el
    que se publica (y usan las predicciones) es una media exponencial de ~1 min, y el del instante va aparte."""
    from bot.trend import IMB_TAU_S, smooth_imbalance
    assert smooth_imbalance(None, None, 0.88, 0) == 0.88
    v = smooth_imbalance(0.88, 0, -0.94, 20_000)
    assert v == pytest.approx(0.88 + (1 - math.exp(-20 / IMB_TAU_S)) * (-0.94 - 0.88)) and v > 0.2
    assert smooth_imbalance(0.5, 0, None, 1000) == 0.5
    assert smooth_imbalance(0.0, 0, 1.0, 600_000) == pytest.approx(1.0, abs=1e-4)   # tras 10 min, el de ahora
