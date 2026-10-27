"""Pruebas de bot/historia_extra.py, sin red, con muestras reales recortadas."""
import asyncio
import io
import json
import zipfile
from datetime import date, datetime, timezone

import pytest

from bot import backtest_aperturas as bt
from bot import historia_extra as hx

MIN = 60_000

# Respuesta real de https://api.alternative.me/fng/?limit=3&format=json (26-10-2026)
FNG_RAW = {"name": "Fear and Greed Index", "data": [
    {"value": "50", "value_classification": "Neutral", "timestamp": "1792972800", "time_until_update": "51892"},
    {"value": "54", "value_classification": "Neutral", "timestamp": "1792886400"},
    {"value": "51", "value_classification": "Neutral", "timestamp": "1792800000"}], "metadata": {"error": None}}
# Respuesta real de /fapi/v1/fundingRate?symbol=BTCUSDT&startTime=1609459200000&limit=3
FUND_RAW = [{"symbol": "BTCUSDT", "fundingTime": 1609459200002, "fundingRate": "0.00022753", "markPrice": "",
             "rateType": "Regular"},
            {"symbol": "BTCUSDT", "fundingTime": 1609488000006, "fundingRate": "0.00026336", "markPrice": "",
             "rateType": "Regular"},
            {"symbol": "BTCUSDT", "fundingTime": 1609516800003, "fundingRate": "0.00034457", "markPrice": "",
             "rateType": "Regular"}]
# Recortes reales de GDELT DOC 2.0 (timelinetone de 7 días: horas; timelinevolraw de 2 días: 15 min)
TONE_RAW = {"query_details": {"title": "bitcoin"}, "timeline": [{"series": "Average Tone", "data": [
    {"date": "20210101T000000Z", "value": 0.2627}, {"date": "20210101T010000Z", "value": 0.0149}]}]}
VOL_RAW = {"query_details": {"title": "bitcoin"}, "timeline": [{"series": "Article Count", "data": [
    {"date": "20210101T000000Z", "value": 0, "norm": 647}, {"date": "20210101T020000Z", "value": 1, "norm": 438}]}]}


def utc(*a) -> int:
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------- velas de 1 min

def test_meses_desde_2021_con_las_funciones_de_backtest_aperturas():
    months, cur = hx.months_since("2021-01", date(2026, 10, 26))
    assert months[0] == "2021-01" and months[-1] == "2026-09" and len(months) == 69 and cur == "2026-10"
    assert hx.months_since("2026-10", date(2026, 10, 26)) == ([], "2026-10")


def _zip(path, lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(path.stem + ".csv", "\n".join(lines) + "\n")
    path.write_bytes(buf.getvalue())


def test_comprobacion_de_un_mes_en_microsegundos_con_hueco(tmp_path, monkeypatch):
    monkeypatch.setattr(hx.config, "HIST_DIR", tmp_path)
    a, _ = bt.month_bounds("2025-03")
    ts = [a + k * MIN for k in range(10)] + [a + 20 * MIN]          # faltan 10 minutos
    lines = [f"{t * 1000},1,2,0.5,1.5,10,{t * 1000 + 59_999_999},15,3,4,6,0" for t in ts]   # µs, como desde 2025
    p = bt.zip_path("BTCUSDT", "2025-03")
    p.parent.mkdir(parents=True)
    _zip(p, lines)
    c = hx.check_month(p, "2025-03")
    assert c["units"] == "us" and c["us_rows"] == 11 and c["first"] == a and c["last"] == a + 20 * MIN
    assert c["outside"] == 0 and c["dups"] == 0 and not c["unsorted"]
    assert c["gaps"] == [[a + 9 * MIN, a + 20 * MIN, 10]]
    # el mismo archivo lo lee bt.load_series en milisegundos
    s = bt.load_series([p])
    assert list(s.t) == ts


def test_comprobacion_detecta_filas_fuera_del_mes_y_repetidas(tmp_path):
    a, b = bt.month_bounds("2024-02")
    ts = [a - MIN, a, a, a + MIN]
    p = tmp_path / "ETHUSDT-1m-2024-02.zip"
    _zip(p, ["open_time,open"] + [f"{t},1,2,0.5,1.5,10,{t + 59_999},15,3,4,6,0" for t in ts])
    c = hx.check_month(p, "2024-02")
    assert c["units"] == "ms" and c["outside"] == 1 and c["dups"] == 1 and c["unsorted"]


def test_cobertura_1m_desde_archivos_y_parte_api(tmp_path, monkeypatch):
    monkeypatch.setattr(hx.config, "HIST_DIR", tmp_path)
    a, b = bt.month_bounds("2026-09")
    c0, _ = bt.month_bounds("2026-10")
    for sym in hx.SYMS.values():
        p = bt.zip_path(sym, "2026-09")
        p.parent.mkdir(parents=True, exist_ok=True)
        _zip(p, [f"{t},1,2,0.5,1.5,10,{t + 59_999},15,3,4,6,0" for t in range(a, b, MIN)])
        rows = [[t, 1.0, 2.0, 0.5, 1.5, 10.0, 15.0, 4.0] for t in range(c0, c0 + 30 * MIN, MIN)]
        hx._write_json(bt.part_path(sym, "2026-10"), {"symbol": sym, "month": "2026-10", "rows": rows})
    cov = hx.coverage_1m("2026-09", date(2026, 10, 26))
    btc = cov["BTCUSDT"]
    assert btc["from"] == "2026-09-01" and btc["months"] == 2 and btc["missing_months"] == []
    assert btc["bars"] == 30 * 1440 + 30 and btc["missing_min"] == 0 and btc["us_months"] == []


# ---------------------------------------------------------------- miedo y codicia

def test_fng_real_ordenado_y_sin_repetidos():
    rows = hx.parse_fng(FNG_RAW)
    assert rows == [["2026-10-24", 51], ["2026-10-25", 54], ["2026-10-26", 50]]
    raw = {"data": FNG_RAW["data"] + [{"value": "120", "timestamp": "1792713600"}, {"value": "x", "timestamp": "1"}],
           "metadata": {"error": None}}
    assert hx.parse_fng(raw) == rows                                 # fuera de 0-100 o roto: fuera
    with pytest.raises(ValueError):
        hx.parse_fng({"data": [], "metadata": {"error": "fallo"}})


def test_fng_guardado_cargado_y_sin_mirar_el_futuro(tmp_path):
    hx._write_json(hx.fng_path(tmp_path), [["2026-10-25", 54], ["2026-10-24", 51]])
    rows = hx.load_fng(tmp_path)
    assert rows == [["2026-10-24", 51], ["2026-10-25", 54]]
    assert hx.load_fng(tmp_path / "no") == []
    assert hx.fng_known_at(rows, utc(2026, 10, 25, 0, 30)) == ["2026-10-24", 51]   # el de hoy aún no (1 h)
    assert hx.fng_known_at(rows, utc(2026, 10, 25, 1, 0)) == ["2026-10-25", 54]
    assert hx.fng_known_at(rows, utc(2026, 10, 24, 0, 59)) is None


# ---------------------------------------------------------------- funding

def test_funding_real_y_carga(tmp_path):
    rows = hx.parse_funding(FUND_RAW + [{"fundingTime": "x"}])
    assert rows == [[1609459200002, 0.00022753], [1609488000006, 0.00026336], [1609516800003, 0.00034457]]
    hx._write_json(hx.funding_path("BTC", tmp_path), rows[::-1])
    assert hx.funding_path("BTC", tmp_path).name == "BTCUSDT.json"
    got = hx.load_funding("btcusdt", tmp_path)
    assert got == rows
    assert hx.funding_known_at(got, 1609488000006) == [1609488000006, 0.00026336]
    assert hx.funding_known_at(got, 1609488000005) == [1609459200002, 0.00022753]
    assert hx.funding_known_at(got, 1609459200001) is None


async def test_funding_paginado_e_incremental(tmp_path, monkeypatch):
    calls = []
    t0 = utc(2021, 1, 1)

    async def fake(session, url, params=None, headers=None, tries=4):
        calls.append(dict(params))
        st = params["startTime"]
        n = 1000 if st == t0 else 3
        first = (st - t0 + 8 * 3600_000 - 1) // (8 * 3600_000)
        return [{"symbol": params["symbol"], "fundingTime": t0 + (first + k) * 8 * 3600_000,
                 "fundingRate": "0.0001"} for k in range(n)]

    monkeypatch.setattr(hx.history, "_get_json", fake)
    n = await hx.fetch_funding(None, "BTC", tmp_path)
    assert n == 1003 and len(calls) == 2
    assert calls[0] == {"symbol": "BTCUSDT", "startTime": t0, "limit": 1000}
    assert calls[1]["startTime"] == t0 + 999 * 8 * 3600_000 + 1
    rows = hx.load_funding("BTC", tmp_path)
    assert [r[0] for r in rows] == sorted({r[0] for r in rows}) and len(rows) == 1003
    calls.clear()
    await hx.fetch_funding(None, "BTC", tmp_path)                    # segunda vez: solo lo que falta
    assert calls[0]["startTime"] == rows[-1][0] + 1


# ---------------------------------------------------------------- calendario macro

def test_hora_de_nueva_york_con_cambio_de_hora():
    assert hx.et_ms(date(2021, 1, 13), "08:30") == utc(2021, 1, 13, 13, 30)      # EST
    assert hx.et_ms(date(2021, 3, 10), "08:30") == utc(2021, 3, 10, 13, 30)      # aún EST (cambia el 14-3)
    assert hx.et_ms(date(2021, 4, 13), "08:30") == utc(2021, 4, 13, 12, 30)      # EDT
    assert hx.et_ms(date(2021, 11, 3), "14:00") == utc(2021, 11, 3, 18, 0)       # EDT (cambia el 7-11)
    assert hx.et_ms(date(2021, 12, 15), "14:00") == utc(2021, 12, 15, 19, 0)     # EST


def test_calendario_macro_fechas_oficiales():
    now = utc(2026, 10, 26, 10)
    ev = hx.macro_events(now)
    by = {(e["name"], e["date"]): e for e in ev}
    assert [e["t_ms"] for e in ev] == sorted(e["t_ms"] for e in ev)
    count = lambda n, y=None: sum(1 for e in ev if e["name"] == n and (y is None or e["date"][:4] == str(y)))
    assert count("FOMC") == 48 and all(count("FOMC", y) == 8 for y in range(2021, 2027))
    assert count("CPI") == 71 and count("NFP") == 71
    # comprobadas a mano en bls.gov y federalreserve.gov (incluidos los retrasos del cierre del Gobierno de 2025)
    assert by[("CPI", "2021-01-13")]["t_ms"] == utc(2021, 1, 13, 13, 30) and by[("CPI", "2021-01-13")]["ref"] == "2020-12"
    assert by[("CPI", "2025-10-24")]["ref"] == "2025-09" and by[("CPI", "2025-10-24")]["t_ms"] == utc(2025, 10, 24, 12, 30)
    assert by[("CPI", "2025-12-18")]["ref"] == "2025-11"
    assert by[("NFP", "2025-11-20")]["ref"] == "2025-09" and by[("NFP", "2025-11-20")]["t_ms"] == utc(2025, 11, 20, 13, 30)
    assert by[("NFP", "2025-12-16")]["ref"] == "2025-11"
    assert by[("NFP", "2026-02-11")]["t_ms"] == utc(2026, 2, 11, 13, 30)
    assert by[("NFP", "2021-07-02")]["t_ms"] == utc(2021, 7, 2, 12, 30)
    assert by[("FOMC", "2024-11-07")]["t_ms"] == utc(2024, 11, 7, 19, 0)
    assert by[("FOMC", "2022-03-16")]["t_ms"] == utc(2022, 3, 16, 18, 0)
    assert ("FOMC", "2025-08-22") not in by                              # votación por escrito sin decisión de tipos
    assert hx.missing_refs(ev, "CPI") == ["2025-10"] and hx.missing_refs(ev, "NFP") == ["2025-10"]
    sched = sorted((e["name"], e["date"]) for e in ev if e["scheduled"])
    assert sched == [("CPI", "2026-11-10"), ("CPI", "2026-12-10"), ("FOMC", "2026-10-28"), ("FOMC", "2026-12-09"),
                     ("NFP", "2026-11-06"), ("NFP", "2026-12-04")]
    for e in ev:   # hora oficial en Nueva York
        loc = datetime.fromtimestamp(e["t_ms"] / 1000, hx.ET).strftime("%H:%M")
        assert loc == ("14:00" if e["name"] == "FOMC" else "08:30")
        assert datetime.fromtimestamp(e["t_ms"] / 1000, hx.ET).date().isoformat() == e["date"]


def test_macro_guardado_y_cargado(tmp_path):
    assert hx.build_macro(tmp_path, utc(2026, 10, 26)) == 190
    ev = hx.load_macro(tmp_path)
    assert len(ev) == 190 and set(ev[0]) >= {"name", "date", "t_ms"}
    fomc = hx.load_macro(tmp_path, ("FOMC",))
    assert len(fomc) == 48 and fomc[0]["date"] == "2021-01-27"
    win = hx.macro_between(ev, utc(2026, 10, 14, 12), utc(2026, 10, 14, 13))
    assert [(e["name"], e["date"]) for e in win] == [("CPI", "2026-10-14")]
    assert hx.load_macro(tmp_path / "no") == []


# ---------------------------------------------------------------- GDELT

def test_gdelt_lectura_y_mezcla():
    t = hx.parse_gdelt(TONE_RAW)
    v = hx.parse_gdelt(VOL_RAW)
    a = utc(2021, 1, 1)
    assert t == {a: 0.2627, a + 3600_000: 0.0149} and v == {a: 0.0, a + 7200_000: 1.0}
    assert hx.merge_gdelt(t, v) == [[a, 0.2627, 0.0], [a + 3600_000, 0.0149, None], [a + 7200_000, None, 1.0]]
    assert hx.parse_gdelt({}) == {} and hx.parse_gdelt({"timeline": []}) == {}
    assert hx.gdelt_dt(a) == "20210101000000" and hx.gdelt_ms("20210101T010000Z") == a + 3600_000
    msg = "Please limit requests to one every 5 seconds or contact kalev.leetaru5@gmail.com for larger queries."
    assert hx.gdelt_text_is_limit(msg) and not hx.gdelt_text_is_limit(json.dumps(TONE_RAW))


def test_gdelt_tramos():
    a = utc(2021, 1, 1)
    ch = hx.gdelt_chunks(a, a + 200 * hx.DAY, 90 * hx.DAY)
    assert ch == [(a, a + 90 * hx.DAY), (a + 90 * hx.DAY, a + 180 * hx.DAY), (a + 180 * hx.DAY, a + 200 * hx.DAY)]


async def test_gdelt_descarga_por_tramos_reanudable(tmp_path, monkeypatch):
    calls, failed = [], []
    a0 = utc(2021, 1, 1)

    async def fake(session, pacer, query, mode, a, b):
        calls.append((mode, a, b))
        if a == a0 + 90 * hx.DAY and mode == "timelinevolraw" and not failed:   # falla una sola vez
            failed.append(1)
            raise IOError("límite de GDELT (429)")
        days = range(a, b + hx.DAY, hx.DAY)                          # GDELT incluye el punto final
        key = "Average Tone" if mode == "timelinetone" else "Article Count"
        return {"timeline": [{"series": key, "data": [
            {"date": datetime.fromtimestamp(d / 1000, timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
             "value": -1.5 if mode == "timelinetone" else 7} for d in days]}]}

    monkeypatch.setattr(hx, "_gdelt_get", fake)
    end = a0 + 200 * hx.DAY
    r = await hx.fetch_gdelt(None, None, "cripto", "(bitcoin OR crypto)", "1d", tmp_path, "2021-01-01", end)
    assert len(r["errors"]) == 1 and r["points"] == 200 and r["pending"] == 1   # al tramo 2 le falta el volumen
    g = hx.load_gdelt("cripto", tmp_path)
    assert g["query"] == "(bitcoin OR crypto)" and g["resolution"] == "1d" and g["step_ms"] == hx.DAY
    assert g["series"][0] == [a0, -1.5, 7.0] and all(a0 <= p[0] < end for p in g["series"])
    assert g["series"][100] == [a0 + 100 * hx.DAY, -1.5, None]
    calls.clear()
    r = await hx.fetch_gdelt(None, None, "cripto", "(bitcoin OR crypto)", "1d", tmp_path, "2021-01-01", end)
    assert r["errors"] == [] and r["points"] == 200 and r["pending"] == 0
    # solo lo que faltaba: el volumen del tramo 2 y el último tramo entero
    assert calls == [("timelinevolraw", a0 + 90 * hx.DAY, a0 + 180 * hx.DAY),
                     ("timelinetone", a0 + 180 * hx.DAY, end), ("timelinevolraw", a0 + 180 * hx.DAY, end)]
    g = hx.load_gdelt("cripto", tmp_path)
    # sin mirar el futuro: el día D solo se conoce cuando acaba y se publica (1 h después)
    assert hx.gdelt_known_at(g, a0 + hx.DAY - 1) is None
    assert hx.gdelt_known_at(g, a0 + hx.DAY) is None
    assert hx.gdelt_known_at(g, a0 + hx.DAY + hx.H)[0] == a0
    assert hx.gdelt_known_at(g, a0 + 5 * hx.DAY + 13 * 3600_000)[0] == a0 + 4 * hx.DAY
    assert hx.gdelt_lag(15 * hx.MIN) == 30 * hx.MIN and hx.gdelt_lag(hx.H) == hx.H
    assert hx.load_gdelt("otra", tmp_path) == {}


# ---------------------------------------------------------------- cobertura

def test_cobertura_y_texto(tmp_path):
    hx._write_json(hx.fng_path(tmp_path), [["2018-02-01", 30], ["2018-02-02", 15], ["2018-02-05", 40]])
    t0 = utc(2021, 1, 1)
    hx._write_json(hx.funding_path("SOL", tmp_path), [[t0, 0.0001], [t0 + 8 * 3600_000, 0.0001],
                                                     [t0 + 12 * 3600_000, 0.0002], [t0 + 36 * 3600_000, 0.0]])
    hx.build_macro(tmp_path, utc(2026, 10, 26))
    a0 = utc(2021, 1, 1)
    hx._write_json(hx.gdelt_path("cripto", tmp_path), {
        "query": "q", "resolution": "1d", "step_ms": hx.DAY, "chunks_done": [[a0, a0 + 90 * hx.DAY]],
        "series": [[a0 + k * hx.DAY, -0.5, 10.0 if k < 90 else None] for k in range(100)] + [[a0 + 103 * hx.DAY, -1, None]]})
    cov = hx.coverage(tmp_path)
    g = cov["gdelt"]["cripto"]
    assert g["from"] == "2021-01-01" and g["tone_to"] == "2021-04-14" and g["vol_to"] == "2021-03-31"
    assert g["chunks_done"] == 1 and g["chunks_pending"] >= 20 and g["vol_missing"] == 11
    assert g["gaps"] == [["2021-04-10", "2021-04-14", 3]]
    assert "1m" not in cov
    assert cov["fng"] == {"from": "2018-02-01", "to": "2018-02-05", "n": 3, "gaps": [["2018-02-02", "2018-02-05", 2]]}
    sol = cov["funding"]["SOLUSDT"]
    assert sol["n"] == 4 and sol["short_periods"] == 1 and len(sol["gaps"]) == 1
    assert cov["funding"]["BTCUSDT"]["n"] == 0
    assert cov["macro"]["by_name"] == {"CPI": 71, "NFP": 71, "FOMC": 48}
    assert cov["macro"]["missing"] == ["CPI 2025-10", "NFP 2025-10"]
    txt = "\n".join(hx.coverage_text(cov))
    assert "Miedo y codicia: 2018-02-01 → 2018-02-05" in txt and "Funding SOLUSDT" in txt
    assert set(cov["timing_sources"]) >= {"fng", "funding", "macro", "gdelt"}   # validación externa de horarios


async def test_gdelt_se_para_si_corta_y_no_deja_archivo_vacio(tmp_path, monkeypatch):
    calls = []

    async def fake(session, pacer, query, mode, a, b):
        calls.append((mode, a))
        raise IOError("HTTP 429: Please limit requests to one every 5 seconds")

    monkeypatch.setattr(hx, "_gdelt_get", fake)
    a0 = utc(2021, 1, 1)
    r = await hx.fetch_gdelt(None, None, "fed", "x", "1d", tmp_path, "2021-01-01", a0 + 400 * hx.DAY)
    assert r["blocked"] and r["points"] == 0 and r["pending"] == 5
    assert len(calls) == hx.GDELT_MAX_FAILS and len(r["errors"]) == hx.GDELT_MAX_FAILS
    assert "429" in r["errors"][0]
    assert not hx.gdelt_path("fed", tmp_path).exists()
