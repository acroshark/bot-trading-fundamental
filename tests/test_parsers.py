from bot.agenda import parse_ics
from bot.hunters import parse_binance, parse_edgar, parse_okx, parse_rss, parse_status, parse_tree, parse_upbit
from bot.market import bias_of, flow_from

# Muestras recortadas de respuestas reales del 23-10-2026.


def test_tree_reenvia_tuits_como_t2_y_saca_monedas():
    data = [{"_id": "211", "source": "Twitter", "title": "Elon Musk (@elonmusk): Bitcoin", "url": "https://x.com/1",
             "time": 1792718779211, "symbols": ["BTC_USDT", "DOGE_USDT"]},
            {"_id": "212", "source": "Upbit", "title": "Upbit lists XYZ", "time": 1792718790922, "symbols": []}]
    a, b = parse_tree(data, "tree")
    assert (a.tier, a.via, a.coins_hint, a.symbols) == ("T2", "X", ["BTC"], ["BTC", "DOGE"])
    assert b.tier == "T1" and b.published == 1792718790922


def test_okx():
    data = {"code": "0", "data": [{"details": [{"title": "OKX lists X", "url": "https://my.okx.com/a", "pTime": "1792582618984"}]}]}
    [it] = parse_okx(data, "okx")
    assert it.uid == "https://my.okx.com/a" and it.published == 1792582618984


def test_binance_solo_listados_noticias_y_exclusiones():
    data = {"data": {"catalogs": [
        {"catalogId": 48, "articles": [{"code": "abc", "title": "Binance Will List X", "releaseDate": 1792553471286}]},
        {"catalogId": 51, "articles": [{"code": "api", "title": "API update", "releaseDate": 1}]}]}}
    [it] = parse_binance(data, "binance")
    assert it.uid == "abc" and it.url.endswith("/abc")


def test_upbit_hora_coreana_y_ticker():
    data = {"data": {"notices": [{"id": 6694, "title": "리플(XRP) 입출금 일시 중단 안내", "listed_at": "2026-10-22T23:25:04+09:00"}]}}
    [it] = parse_upbit(data, "upbit")
    assert it.coins_hint == ["XRP"] and it.published == 1792679104000


def test_estado_de_exchange():
    data = {"incidents": [{"id": "x1", "name": "Delayed XRP withdrawals", "impact": "minor",
                           "created_at": "2026-10-22T16:20:53.123-07:00", "shortlink": "https://stspg.io/x"}]}
    [it] = parse_status(data, "cb_status", "Coinbase")
    assert it.title == "Coinbase: Delayed XRP withdrawals (minor)" and it.published == 1792711253123


def test_edgar():
    data = {"hits": {"hits": [{"_id": "0001213900-26-111688:ea0309236-8k_ark21shares.htm", "_source": {
        "ciks": ["0001869699"], "display_names": ["Ark 21Shares Bitcoin ETF  (ARKB)  (CIK 0001869699)"],
        "form": "8-K", "adsh": "0001213900-26-111688", "file_description": "CURRENT REPORT"}}]}}
    [it] = parse_edgar(data, "edgar", "BTC")
    assert it.title == "Ark 21Shares Bitcoin ETF  (ARKB) presenta 8-K en la SEC (current report)"
    assert it.url == "https://www.sec.gov/Archives/edgar/data/1869699/000121390026111688/ea0309236-8k_ark21shares.htm"


def test_rss():
    raw = b"""<?xml version="1.0"?><rss version="2.0"><channel><item><title>Fed &lt;b&gt;holds&lt;/b&gt; rates</title>
    <link>https://fed.gov/1</link><guid>g1</guid><pubDate>Thu, 22 Oct 2026 18:00:00 GMT</pubDate></item></channel></rss>"""
    [it] = parse_rss(raw, "fed")
    assert it.title == "Fed holds rates" and it.uid == "g1" and it.published == 1792692000000


def test_calendario_bls_solo_datos_que_mueven():
    ics = ("BEGIN:VEVENT\nDTSTART;TZID=US-Eastern:20261112T083000\nSUMMARY:Consumer Price Index for October 2026\nEND:VEVENT\n"
           "BEGIN:VEVENT\nDTSTART;TZID=US-Eastern:20261112T100000\nSUMMARY:County Employment and Wages\nEND:VEVENT\n")
    [ev] = parse_ics("BEGIN:VCALENDAR\n" + ics)
    assert ev["impact"] == "alto" and ev["t"] == 1794490200000   # 08:30 hora de Nueva York = 13:30 UTC


def test_flujo_minorista():
    f = flow_from({"longShortRatio": "2.1095"}, {"lastFundingRate": "0.00006299"},
                  [{"sumOpenInterestValue": "100"}, {"sumOpenInterestValue": "103"}], {"buySellRatio": "1.4603"})
    assert round(f["ls"], 2) == 2.11 and round(f["funding"], 4) == 0.0063 and round(f["oi"], 1) == 3.0
    assert round(f["taker"], 3) == 0.594
    assert bias_of(2.11, 0.006)["label"] == "largo"
    assert bias_of(2.8, 0.006)["label"] == "muy largo"
    assert bias_of(1.2, 0.006)["label"] == "neutral"
    # Con historial: un 3,3 en XRP es lo normal si está en mitad de su rango de 7 días.
    assert bias_of(3.3, 0.005, pctl=55)["label"] == "largo"
    assert bias_of(3.3, 0.005, pctl=95)["label"] == "muy largo"
