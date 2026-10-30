"""Histórico de noticias de 3 años (bot/noticias_historia.py): el filtro es el del bot, el sello no mira al futuro, la
deduplicación es la de la mesa, la petición a la IA local es la del triaje en vivo, el triaje solo usa los huecos del
servidor y el archivo del laboratorio solo se escribe con todo triado. Sin red: las descargas y el servidor son falsos."""
import gzip
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

import numpy as np
import pytest

from bot import config, filters, local_ai
from bot import noticias_historia as nh
from bot.lab_vivo import noticias as nl

MIN, H = 60_000, 3_600_000
T0 = int(datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)

TITULOS = [
    "Bitcoin jumps 5% as spot ETF inflows hit a record", "Ethereum devs schedule the Fusaka upgrade",
    "XRP rallies after Ripple wins appeal", "Solana network halts for five hours", "SOL tops $250 as memecoins boom",
    "Coinbase shares slide after SEC lawsuit", "Stablecoin bill clears the Senate", "Binance hit by $40m hack",
    "Fed holds rates steady, signals two cuts in 2026", "US CPI rises 0.4% in September, above forecasts",
    "Nonfarm payrolls beat expectations as jobless rate falls", "Trump announces 25% tariffs on EU cars",
    "Russia launches missile strike on Kyiv", "EU adopts new sanctions package against Iran", "ECB cuts rates by 25bp",
    "Bank of Japan raises rates for the first time since 2007", "Oil spikes 8% after attack on Saudi facilities",
    "Recession fears grow as GDP contracts", "Treasury yields surge after hot jobs data",
    "Workers go on strike at Boeing plants", "Lakers beat Celtics 112-108", "Weather: heat wave grips Spain",
    "Price prediction: will Bitcoin hit $200k?", "Apple unveils new iPhone with AI features",
    "Taxes indexed to inflation for 2027", "Star Wars sequel tops box office", "Election results: incumbent wins",
    "Nvidia earnings beat estimates", "Tether mints another $1bn USDT", "MiCA licence granted to OKX in Malta",
]


# ---------------------------------------------------------------- filtro

@pytest.mark.parametrize("titulo", TITULOS)
def test_la_puerta_no_pierde_nada_que_classify_daria_cripto_o_macro(titulo):
    d = filters.classify(titulo, "gdelt:ejemplo.com", None, lang="en")
    if d.relevant and d.clase in nh.CLASES:
        assert nh.puerta(titulo), (titulo, d.reason)
        assert nh.clasificar(titulo, "gdelt:ejemplo.com").clase == d.clase
    else:
        assert nh.clasificar(titulo, "gdelt:ejemplo.com") is None


def test_la_puerta_deja_fuera_lo_que_no_es_cripto_ni_macro():
    for t in ("Lakers beat Celtics 112-108", "Weather: heat wave grips Spain", "Star Wars sequel tops box office"):
        assert not nh.puerta(t)
    assert nh.puerta("Bitcoin jumps 5%") and nh.puerta("Fed holds rates steady")


def test_un_dominio_que_empieza_por_fed_no_es_la_fed():
    t = "Inflation cools to 2.1% in September"
    assert filters.classify(t, "fedscoop.com", None, lang="en").relevant is False     # lo tomaría por la Fed
    assert nh.clasificar(t, "gdelt:fedscoop.com").clase == "macro"


def test_limpiar_titulo_quita_la_coletilla_del_medio_y_las_entidades():
    assert nh.limpiar_titulo("Bitcoin jumps 5% after ETF approval | Reuters", "reuters.com") == \
        "Bitcoin jumps 5% after ETF approval"
    assert nh.limpiar_titulo("Stocks slide as yields rise - Yahoo Finance", "finance.yahoo.com") == \
        "Stocks slide as yields rise"
    assert nh.limpiar_titulo("Powell speaks today - what to expect", "cnbc.com") == "Powell speaks today - what to expect"
    assert nh.limpiar_titulo("S&amp;P 500   hits &quot;record&quot;", "x.com") == 'S&P 500 hits "record"'


# ---------------------------------------------------------------- GDELT

def fila_gkg(rid, fecha, dom, url, temas, tono, extras) -> bytes:
    p = [""] * 27
    p[0], p[1], p[2], p[3], p[4], p[7], p[15], p[26] = rid, fecha, "1", dom, url, temas, tono, extras
    return "\t".join(p).encode()


def csv_muestra() -> bytes:
    return b"\n".join([
        fila_gkg("1", "20261020120000", "reuters.com", "https://reuters.com/a", "ECON_BITCOIN;ECON_STOCKMARKET",
                 "-2.5,1,3,4", "<PAGE_TITLE>Bitcoin slides 4% as ETF outflows mount | Reuters</PAGE_TITLE>"),
        fila_gkg("2", "20261020120000", "espn.com", "https://espn.com/b", "SPORTS", "1.0,1,1,1",
                 "<PAGE_TITLE>Lakers beat Celtics 112-108</PAGE_TITLE>"),
        fila_gkg("3", "20261020120000", "bbc.co.uk", "https://bbc.co.uk/c", "ECON_BITCOIN", "0", ""),
        fila_gkg("4", "20261020120000", "cnbc.com", "https://cnbc.com/d", "", "0.5",
                 "<PAGE_TITLE>Fed holds rates steady, signals two cuts</PAGE_TITLE>"),
    ]) + b"\n"


def test_filas_gkg_guarda_cripto_y_macro_con_su_sello():
    c, dom = __import__("collections").Counter(), {}
    t_arch = nh.ms_de("20261020120000")
    filas = nh.filas_gkg(csv_muestra(), "20261020120000", t_arch + 5 * MIN, c, dom)
    assert [r["id"] for r in filas] == ["1", "4"]
    r = filas[0]
    assert r["tit"] == "Bitcoin slides 4% as ETF outflows mount" and r["clase"] == "cripto" and r["mon"] == ["BTC"]
    assert r["t"] == t_arch and r["s"] == t_arch + 5 * MIN and r["dom"] == "reuters.com"
    assert r["tono"] == -2.5 and r["gdelt"] == ["ECON_BITCOIN"]
    assert filas[1]["clase"] == "macro" and filas[1]["mon"] == []
    assert c["filas"] == 4 and c["sin_titular"] == 1 and c["tema_cripto_gdelt"] == 2 and c["con_titular"] == 3
    assert dom == {"cripto": {"reuters.com": 1}, "macro": {"cnbc.com": 1}}


def _zip(csv: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.gkg.csv", csv)
    return buf.getvalue()


def test_procesar_dia_comprueba_el_md5_y_usa_el_sello_mas_tardio(tmp_path, monkeypatch):
    zb = _zip(csv_muestra())
    t_arch = nh.ms_de("20261020120000")
    lm = {"bueno": t_arch - 10 * MIN, "malo": t_arch - 10 * MIN, "tarde": t_arch + 7 * MIN + 30_000}
    monkeypatch.setattr(nh, "bajar", lambda url, ritmo=None, timeout=120: (zb, lm[url.split("/")[-1]], ""))
    archivos = [["20261020120000", len(zb), hashlib.md5(zb).hexdigest(), "http://x/bueno"],
                ["20261020120000", len(zb), "md5-falso", "http://x/malo"]]
    r = nh.procesar_dia("2026-10-20", archivos, tmp_path, hilos=2)
    assert not r["completo"] and r["faltan"] == [{"archivo": "20261020120000", "error": "md5 distinto"}]
    assert r["guardadas"] == 2 and r["filas"]["cripto"] == 1 and r["filas"]["macro"] == 1
    assert not nh._dia_hecho(tmp_path, "2026-10-20")              # un día incompleto se repite al relanzar
    archivos[1] = ["20261020120000", len(zb), hashlib.md5(zb).hexdigest(), "http://x/tarde"]
    r = nh.procesar_dia("2026-10-20", archivos, tmp_path, hilos=2)
    assert r["completo"] and nh._dia_hecho(tmp_path, "2026-10-20") and r["filas"]["publicados_tarde"] == 1
    filas = nh.leer_jsonl_gz(tmp_path / "2026-10-20.jsonl.gz")
    # el archivo publicado 7,5 min tarde: sello al minuto siguiente de su Last-Modified
    assert sorted(f["s"] for f in filas) == [t_arch, t_arch, t_arch + 8 * MIN, t_arch + 8 * MIN]


# ---------------------------------------------------------------- WordPress

def test_posts_wp_filtra_por_date_gmt_y_limpia_el_titular():
    a, b = nh._gmt_ms("2026-09-01T00:00:00"), nh._gmt_ms("2026-10-01T00:00:00")
    posts = [{"id": 7, "date_gmt": "2026-09-03T10:00:05", "link": "https://bitcoinist.com/x",
              "title": {"rendered": "Bitcoin <em>Breaks</em> $120K &#8211; New High"}, "categories": [3, 4]},
             {"id": 8, "date_gmt": "2026-08-31T23:59:59", "title": {"rendered": "fuera"}},
             {"id": 9, "date_gmt": None, "title": {"rendered": "sin fecha"}}, "basura"]
    filas = nh.posts_wp(posts, "www.bitcoinist.com", a, b)
    assert len(filas) == 1
    r = filas[0]
    assert r["id"] == "bitcoinist.com:7" and r["dom"] == "bitcoinist.com" and r["cat"] == [3, 4]
    assert r["tit"] == "Bitcoin Breaks $120K – New High" and r["t"] == r["s"] == nh._gmt_ms("2026-09-03T10:00:05")


# ---------------------------------------------------------------- historias

def pieza(i, s, dom, tit, clase="cripto", fuente="wp"):
    return {"id": str(i), "t": s, "s": s, "dom": dom, "tit": tit, "clase": clase, "mon": ["BTC"], "temas": [],
            "macro": False, "fuente": fuente}


def test_la_mesa_junta_la_misma_historia_de_varios_medios_como_desk():
    mesa = nh.Mesa()
    piezas = [pieza(1, T0, "a.com", "Bitcoin ETF inflows hit record $1.2 billion"),
              pieza(2, T0 + 5 * MIN, "b.com", "Bitcoin ETF inflows hit a record $1.2 billion on Monday"),
              pieza(3, T0 + 6 * MIN, "b.com", "Bitcoin ETF inflows hit record $1.2 billion"),       # mismo medio
              pieza(4, T0 + 9 * MIN, "c.com", "Ethereum staking yields fall to new low"),
              pieza(5, T0 + 4 * H, "d.com", "Bitcoin ETF inflows hit record $1.2 billion")]          # pasadas 3 h
    cerradas = []
    for p in piezas:
        cerradas += mesa.pieza(p)
    cerradas += mesa.cerrar_todo()
    hs = sorted((nh.historia_limpia(h) for h in cerradas), key=lambda h: h["s0"])
    assert [h["tit"][:20] for h in hs] == ["Bitcoin ETF inflows ", "Ethereum staking yie", "Bitcoin ETF inflows "]
    assert [m[0] for m in hs[0]["medios"]] == ["a.com", "b.com"] and hs[0]["n_piezas"] == 3
    assert hs[0]["n_medios"] == 2 and hs[0]["n30"] == 2 and hs[2]["n_medios"] == 1
    assert len({h["id"] for h in hs}) == 3


def test_la_basura_no_va_al_triaje():
    assert nh.basura({"tit": "Bybit Kicks Off a 150,000 USDC Prize Pool", "url": "https://x.com/a", "clase": "cripto"})
    assert nh.basura({"tit": "Token X lists on Y", "url": "https://chainwire.org/2026/x", "clase": "cripto"})
    assert nh.basura({"tit": "Houthi attacks wound 73 civilians", "clase": "macro", "temas": ["guerra", "geopolitica"]})
    assert not nh.basura({"tit": "Houthi attacks push Brent above $90", "clase": "macro", "temas": ["guerra"]})
    assert not nh.basura({"tit": "Nonfarm payrolls beat expectations", "clase": "macro", "temas": []})
    assert not nh.basura({"tit": "Bybit hacked: $1.4bn in ETH stolen", "url": "https://x.com/b", "clase": "cripto"})


def test_racimo_y_seleccion_con_el_sello_del_medio_que_lo_completa():
    h = {"s0": T0, "clase": "macro",
         "medios": [["a", T0, "wp"], ["b", T0 + 5 * MIN, "wp"], ["c", T0 + 20 * MIN + 30_000, "gdelt"],
                    ["d", T0 + 40 * MIN, "wp"]]}
    assert nh.racimo(h, 3) == T0 + 20 * MIN + 30_000 and nh.racimo(h, 4) is None
    assert nh.RACIMO_N == {"cripto": 2, "macro": 3}
    assert nh.seleccion(h) == T0 + 22 * MIN            # macro: el sello del tercero + 1 min, al minuto siguiente
    assert nh.seleccion({**h, "medios": h["medios"][:2]}) is None
    assert nh.seleccion({**h, "tit": "Russia launches missile strike on Kyiv", "temas": ["guerra", "geopolitica"]}) is None
    assert nh.seleccion({**h, "tit": "Missile strike on Kyiv sends oil prices higher", "temas": ["guerra"]}) is not None
    hc = {**h, "clase": "cripto"}
    assert nh.seleccion(hc) == T0 + 6 * MIN            # cripto: el segundo medio basta
    assert nh.seleccion({**hc, "medios": h["medios"][:1]}) is None


def test_historias_del_mes_solo_con_los_medios_que_sigue_el_bot(tmp_path):
    (tmp_path / "medios_vivos.json").write_text(json.dumps({"medios": ["reuters.com", "cnbc.com", "bitcoinist.com"]}),
                                                "utf-8")
    gd = tmp_path / "gdelt" / "dias"
    gd.mkdir(parents=True)
    filas = [{"id": "g1", "t": T0, "s": T0, "dom": "reuters.com", "tit": "Fed holds rates steady, signals two cuts",
              "clase": "macro", "mon": []},
             {"id": "g2", "t": T0, "s": T0 + 15 * MIN, "dom": "granjadeclics.com",       # no lo sigue el bot: fuera
              "tit": "Fed holds rates steady, signals two cuts", "clase": "macro", "mon": []},
             {"id": "g3", "t": T0, "s": T0 + 15 * MIN, "dom": "cnbc.com",
              "tit": "Fed holds rates steady and signals two cuts this year", "clase": "macro", "mon": []}]
    for d in nh.dias_entre("2026-10-01", "2026-10-28"):
        rows = filas if d == "2026-10-20" else []
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as g:
            for r in rows:
                g.write((json.dumps(r) + "\n").encode())
        (gd / f"{d}.jsonl.gz").write_bytes(buf.getvalue())
        (gd / f"{d}.json").write_text(json.dumps({"completo": True}), "utf-8")
    for s in nh.WP:
        car = tmp_path / "wp" / nh.medio_de(s)
        car.mkdir(parents=True)
        rows = [{"id": "bitcoinist.com:1", "t": T0 + 20 * MIN, "s": T0 + 20 * MIN, "dom": "bitcoinist.com",
                 "tit": "Fed holds rates steady, signals two cuts", "clase": "macro", "mon": []},
                {"id": "bitcoinist.com:2", "t": T0 + 9 * 86_400_000, "s": T0 + 9 * 86_400_000, "dom": "bitcoinist.com",
                 "tit": "Bitcoin hits a new high", "clase": "cripto", "mon": ["BTC"]}] if s == "bitcoinist.com" else []
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as g:
            for r in rows:
                g.write((json.dumps(r) + "\n").encode())
        (car / "2026-10.jsonl.gz").write_bytes(buf.getvalue())
        (car / "2026-10.json").write_text("{}", "utf-8")
    r = nh.construir_historias("2026-10-01", "2026-10-28", tmp_path)
    assert r["2026-10"]["historias"] == 1                 # la del 29-10 (después de «hasta») no entra
    [h] = nh.leer_jsonl_gz(tmp_path / "historias" / "2026-10.jsonl.gz")
    # bitcoinist repite el titular de Reuters: es una copia y no suma medio
    assert [m[0] for m in h["medios"]] == ["reuters.com", "cnbc.com"] and h["n30"] == 2 and h["n_copias"] == 1
    assert h["fuente"] == "gdelt" and h["clase"] == "macro" and nh.seleccion(h) is None   # 2 medios < 3


def test_seguir_construye_el_tramo_de_meses_completos_que_llega_al_final():
    meses = ["2026-07", "2026-08", "2026-09", "2026-10"]
    assert nh._bloque_final(["2026-09", "2026-10"], meses) == "2026-09"
    assert nh._bloque_final(["2026-07", "2026-09", "2026-10"], meses) == "2026-09"   # agosto aún falta
    assert nh._bloque_final(["2026-07", "2026-08", "2026-09"], meses) is None         # sin el último, nada
    assert nh._bloque_final(meses, meses) == "2026-07"


def test_meses_completos_mira_gdelt_y_los_24_medios(tmp_path):
    lista = [[f"202610{d:02d}000000", 1, "x", "u"] for d in (1, 2)] + [["20260930000000", 1, "x", "u"]]
    (tmp_path / "gdelt" / "lista").mkdir(parents=True)
    (tmp_path / "gdelt" / "lista" / "gkg-20260930-20261002.json").write_text(json.dumps(lista), "utf-8")
    gd = tmp_path / "gdelt" / "dias"
    gd.mkdir(parents=True)
    for d in ("2026-10-01", "2026-10-02", "2026-09-30"):
        (gd / f"{d}.json").write_text(json.dumps({"completo": True}), "utf-8")
    for s in nh.WP:
        (tmp_path / "wp" / nh.medio_de(s)).mkdir(parents=True)
        (tmp_path / "wp" / nh.medio_de(s) / "2026-10.json").write_text("{}", "utf-8")
    assert nh.meses_completos("2026-09-30", "2026-10-02", tmp_path) == ["2026-10"]   # septiembre: faltan los medios


# ---------------------------------------------------------------- triaje

UPBIT = {"resumen": "Hackeo en Upbit", "tipo": "seguridad", "temas": ["cripto"], "credibilidad": 80,
         "alcance": {"nivel": "regional", "puntuacion": 40}, "temporalidad": {"horizonte": "horas", "urgencia": 70},
         "apetito_riesgo": "baja", "repercusion": {"direccion": -60, "magnitud": 60, "activos": ["ETH"],
                                                     "bolsas": ["seul"]},
         "novedad": 90, "motivo": "robo grande", "importancia": 8}


async def test_la_peticion_es_la_del_triaje_en_vivo():
    from test_local_ai import FakeSession, mem_bus
    s = FakeSession(json.dumps(UPBIT, ensure_ascii=False))
    await local_ai.LocalAI(mem_bus(), s).classify("Upbit hacked: ETH stolen", "Bitcoinist", "T2", ["ETH"])
    [(_, body)] = s.calls
    assert nh.cuerpo_triaje("Upbit hacked: ETH stolen", "Bitcoinist", "T2", ["ETH"]) == body


def test_leer_triaje_normaliza_como_el_worker():
    data = {"choices": [{"message": {"content": json.dumps(UPBIT, ensure_ascii=False)}}]}
    from bot.regions import REGIONS
    tr = nh.leer_triaje(data, "Upbit hacked", ["ETH"], "jp")
    # 8 → 7: la magnitud 60 no deja más (local_ai.importance_cap), como en vivo
    assert tr["sentimiento"] == "bajista" and tr["monedas"] == ["ETH"] and tr["importancia"] == 7
    assert tr["repercusion"]["bolsas"] == ["seul", REGIONS["jp"][1]]   # la bolsa de la región, como LocalAI._worker
    with pytest.raises(ValueError):
        nh.leer_triaje({"choices": [{"message": {"content": "{}"}}]}, "t", [], None)


class _Reloj:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _historias(base, filas):
    car = base / "historias"
    car.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as g:
        for h in filas:
            g.write((json.dumps(h) + "\n").encode())
    (car / "2026-10.jsonl.gz").write_bytes(buf.getvalue())


def hist(i, s0, n=3, clase="cripto", tit="Bitcoin ETF inflows hit record", dom="bitcoinist.com", fuente="wp"):
    medios = [[f"m{k}.com", s0 + k * MIN, "wp"] for k in range(n)]
    return {"id": f"h{i}", "t0": s0, "s0": s0, "tit": tit, "dom": dom, "fuente": fuente, "clase": clase,
            "mon": ["BTC"], "medios": medios, "n_medios": n, "n30": n, "n_piezas": n}


def test_el_triaje_solo_pide_con_el_hueco_libre_y_se_puede_relanzar(tmp_path, monkeypatch):
    _historias(tmp_path, [hist(1, T0), hist(2, T0 + H), hist(3, T0 + 2 * H, n=1)])   # la 3 no es racimo
    reloj = _Reloj()
    monkeypatch.setattr(nh.time, "monotonic", reloj.monotonic)
    monkeypatch.setattr(nh.time, "sleep", reloj.sleep)
    monkeypatch.setattr(config, "GPU_LOCK", tmp_path / "no-hay-gpu.lock")
    estados = iter([False, False, True, True, True, True, True, False, True, True, True, True, True] + [True] * 50)
    log_ = []

    def libre(url):
        x = next(estados)
        log_.append(("sondeo", x))
        return x

    class R:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.data

    def urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        log_.append(("pide", body["messages"][1]["content"].splitlines()[0]))
        return R(json.dumps({"choices": [{"message": {"content": json.dumps(UPBIT)}}]}).encode())

    monkeypatch.setattr(nh, "_hueco_libre", libre)
    monkeypatch.setattr(nh.urllib.request, "urlopen", urlopen)
    r = nh.triar(tmp_path, idle_s=3.0)
    assert r["hechas"] == 2 and r["pendientes"] == 0
    # antes de cada petición, el hueco llevaba ≥ 3 s libre seguido (4 sondeos libres de 1 s)
    pedidas = [i for i, x in enumerate(log_) if x[0] == "pide"]
    for i in pedidas:
        assert [x[1] for x in log_[i - 4:i]] == [True] * 4
    hechas = nh.triadas(tmp_path)
    assert set(hechas) == {"h1", "h2"} and hechas["h1"]["uso"] == T0 + 2 * MIN
    assert hechas["h1"]["triage"]["sentimiento"] == "bajista" and hechas["h1"]["tier"] in ("T0", "T1", "T2", "T3")
    assert nh.triar(tmp_path, idle_s=3.0)["hechas"] == 0           # relanzar no repite nada


def test_con_cuota_pide_sin_esperar_al_hueco_y_descansa_lo_suyo(tmp_path, monkeypatch):
    _historias(tmp_path, [hist(1, T0), hist(2, T0 + H)])
    reloj = _Reloj()
    monkeypatch.setattr(nh.time, "monotonic", reloj.monotonic)
    monkeypatch.setattr(nh.time, "sleep", reloj.sleep)
    monkeypatch.setattr(nh.time, "time", lambda: reloj.t)
    monkeypatch.setattr(nh.time, "perf_counter", lambda: reloj.t)
    monkeypatch.setattr(config, "GPU_LOCK", tmp_path / "no-hay-gpu.lock")
    monkeypatch.setattr(nh, "_hueco_libre", lambda url: False)          # siempre ocupado: con cuota pide igual
    monkeypatch.setattr(nh, "vigilar_vivo", lambda: {"lat_p90_s": 12.0, "n": 30, "sin_triar": 0, "tiradas_30min": 4})
    monkeypatch.setitem(nh._VIGILA, "huecos_hasta", 0.0)
    monkeypatch.setitem(nh._VIGILA, "ultima", 0.0)
    pedidas = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            reloj.t += 7.0                                               # cada petición tarda 7 s
            return json.dumps({"choices": [{"message": {"content": json.dumps(UPBIT)}}]}).encode()

    def urlopen(req, timeout=None):
        pedidas.append(reloj.t)
        return R()

    monkeypatch.setattr(nh.urllib.request, "urlopen", urlopen)
    r = nh.triar(tmp_path, cuota=0.2)
    assert r["hechas"] == 2 and len(pedidas) == 2
    assert pedidas[1] - pedidas[0] == pytest.approx(7 + 28, abs=1.01)   # 7 s pidiendo y 4 × 7 s descansando: 20 %
    assert nh._triar_una_vez(tmp_path, None, None, 3.0, None, cuota=0.9)["hechas"] == 0   # tope del 50 %, nada pendiente


def test_la_cuota_vuelve_a_los_huecos_si_el_vivo_no_aguanta(tmp_path, monkeypatch):
    import sqlite3
    now = T0 + 2 * H
    db = tmp_path / "bot.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, ts INTEGER, story TEXT, agent TEXT, kind TEXT, body TEXT)")
    cap = lambda i, ts, tier="T1": (ts, f"s{i}", "hunter", "captured", json.dumps(   # noqa: E731
        {"patch": {"tier": tier, "fresh": True, "clase": "cripto", "srcState": "activa"}}))
    filas = [cap(1, now - 20 * MIN), (now - 20 * MIN + 9000, "s1", "local", "triaged", "{}"),
             cap(2, now - 10 * MIN), (now - 10 * MIN + 400_000, "s2", "local", "triaged", "{}"),   # 400 s
             cap(3, now - 5 * MIN, tier="T2")]                                                      # T2: no cuenta
    con.executemany("INSERT INTO events(ts, story, agent, kind, body) VALUES (?, ?, ?, ?, ?)", filas)
    con.commit()
    con.close()
    hora = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")   # noqa: E731
    lg = tmp_path / "bot.log"
    lg.write_text(f"{hora(now - 40 * MIN)},1 INFO local_ai: Cola de la IA local llena: 100 noticias de menor\n"
                  f"{hora(now - 5 * MIN)},1 INFO local_ai: Cola de la IA local llena: 130 noticias de menor\n", "utf-8")
    v = nh.vigilar_vivo(db, lg, now)
    assert v["n"] == 2 and v["lat_p90_s"] == 9.0 and v["sin_triar"] == 0 and v["tiradas_30min"] == 30
    assert nh.vivo_aguanta(v) == (True, "")
    assert not nh.vivo_aguanta({**v, "lat_p90_s": 400.0})[0]
    assert not nh.vivo_aguanta({**v, "sin_triar": 3})[0] and not nh.vivo_aguanta({**v, "tiradas_30min": 61})[0]
    assert not nh.vivo_aguanta({"error": "sin base"})[0]
    # con la vigilancia mala, la cuota se queda en 0 durante HUECOS_S
    monkeypatch.setattr(nh, "vigilar_vivo", lambda: {"lat_p90_s": 900.0, "n": 5, "sin_triar": 0})
    monkeypatch.setitem(nh._VIGILA, "huecos_hasta", 0.0)
    monkeypatch.setitem(nh._VIGILA, "ultima", 0.0)
    assert nh.cuota_ahora(0.2, tmp_path) == 0.0 and nh.cuota_ahora(0.2, tmp_path) == 0.0
    assert json.loads((tmp_path / "vigilancia_vivo.json").read_text("utf-8"))["ok"] is False
    monkeypatch.setitem(nh._VIGILA, "huecos_hasta", 0.0)
    monkeypatch.setattr(nh, "vigilar_vivo", lambda: {"lat_p90_s": 10.0, "n": 5, "sin_triar": 0})
    monkeypatch.setitem(nh._VIGILA, "ultima", 0.0)
    assert nh.cuota_ahora(0.2, tmp_path) == 0.2 and nh.cuota_ahora(0.0, tmp_path) == 0.0


def test_compilar_lab_exige_todo_triado(tmp_path):
    _historias(tmp_path, [hist(1, T0), hist(2, T0 + H, n=4, clase="macro", tit="Fed holds rates", dom="reuters.com",
                                            fuente="gdelt")])
    (tmp_path / "triaje").mkdir()
    linea = lambda i, tr: json.dumps({"id": f"h{i}", "uso": 0, "triage": tr}) + "\n"   # noqa: E731
    tr1 = local_ai.normalize_triage(UPBIT, "t", ["ETH"])
    (tmp_path / "triaje" / "2026-10.jsonl").write_text(linea(1, tr1), "utf-8")
    r = nh.compilar_lab(tmp_path)
    assert not r["ok"] and r["faltan"] == 1 and not (tmp_path / "lab" / "noticias.npz").exists()
    tr2 = local_ai.normalize_triage({**UPBIT, "tipo": "macro", "apetito_riesgo": "sube",
                                     "repercusion": {"direccion": 40, "magnitud": 30, "activos": [], "bolsas": []}},
                                    "t", [])
    with open(tmp_path / "triaje" / "2026-10.jsonl", "a", encoding="utf-8") as f:
        f.write(linea(2, tr2))
    r = nh.compilar_lab(tmp_path)
    assert r["ok"] and r["n"] == 2
    n = nl.cargar(tmp_path / "lab")
    assert list(n.t) == [T0 + 2 * MIN, T0 + H + 3 * MIN] and list(n.mon) == [1 << config.COINS.index("ETH"), 0]
    assert list(n.sentido()) == [-1, 1] and n.tipos[n.tipo[1]] == "macro" and list(n.clase) == [0, 1]
    assert list(n.fuente) == [1, 0] and n.medios[n.medio[1]] == "reuters.com" and nl.firma(tmp_path / "lab")
    assert r["firma"] == nh.compilar_lab(tmp_path)["firma"]          # mismo contenido, misma firma
