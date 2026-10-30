"""Histórico de noticias de 3 años con su hora, para que el laboratorio vivo pruebe estrategias con noticias (docs/07,
4.1, clave «noticias_3a»). SOLO PAPER: solo mide; no opera ni da consejo.

Lo pidió el usuario el 29-10-2026 («¿el agente tiene en cuenta las noticias?»): el laboratorio solo tenía precio,
volumen y funding de 3 años; las noticias del bot empiezan el 23-10-2026 y los titulares públicos bajados, el 29-07-2026.

Fuentes (públicas, sin clave ni cuenta; lo bajado son DATOS: solo se lee como texto, nunca se ejecuta):
1. GDELT 2.0 GKG en inglés (data.gdeltproject.org/gdeltv2/AAAAMMDDHHMMSS.gkg.csv.zip, un archivo cada 15 min desde
   2015): cada fila es un artículo con su dominio, URL, temas, tono y el titular en <PAGE_TITLE> de Extras. Se baja en
   memoria (el zip no se guarda), se comprueba su md5 con la lista maestra y solo se guarda lo filtrado.
   Sello (cuándo se pudo conocer): la hora del nombre del archivo, que GDELT publica unos 10 min ANTES de esa hora
   (Last-Modified de seis archivos de 2023 a 2026, comprobado el 29-10-2026), o su Last-Modified si es posterior.
2. API REST de WordPress de los 24 medios de cripto que la tienen abierta (los de controles/fuentes y controles/bruscos):
   titular y hora de publicación al segundo (date_gmt). GDELT solo ve ~250 titulares de cripto al día.
Filtro: el del bot en vivo, filters.classify con idioma «en»: solo las clases «cripto» y «macro», las que en vivo van a la
IA local. `puerta` usa las MISMAS expresiones que classify para esas dos clases y evita llamarla en las ~110.000 filas
diarias que no lo son: no cambia el resultado (tests/test_noticias_historia.py).

Historias: la deduplicación de la mesa (desk.NewsDesk) con los medios que sigue el bot (medios_vivos.json). Van a la IA
local las que llegan a racimo (RACIMO_N medios distintos en 30 min), con la petición del triaje en vivo y solo en los
huecos de su servidor (docs/07, apartado 21).

Carpeta: DATA_DIR/noticias_historia/ (nunca en C:). Cada paso se puede relanzar: sigue donde lo dejó. El archivo
«parar» en esa carpeta para las descargas (y «seguir») al acabar lo que tengan en curso; «parar_triaje», el triaje.
    gdelt/lista/gkg-<desde>-<hasta>.json   la lista maestra del periodo: [[AAAAMMDDHHMMSS, bytes, md5, url]]
    gdelt/dias/AAAA-MM-DD.jsonl.gz          filas filtradas del día (una por artículo)
    gdelt/dias/AAAA-MM-DD.json              su resumen; existe solo si el día está hecho
    wp/<medio>/AAAA-MM.jsonl.gz y .json     lo mismo de cada medio de cripto y mes
    medios_vivos.json                       los medios que ve la mesa (congelados el primer día)
    historias/AAAA-MM.jsonl.gz y .json      historias por el mes de su primera pieza, con sus medios y sellos
    triaje/AAAA-MM.jsonl                    el triaje de cada historia seleccionada (una línea por historia)
    lab/noticias.npz y meta.json            lo que lee el laboratorio (bot/lab_vivo/noticias.py)
    informe.json e informe.md               cobertura y calidad (bot/noticias_informe.py)

Uso: python -X utf8 -m bot.noticias_historia gdelt|wp|historias|triaje|lab|seguir [opciones] (--help en cada una)"""
import argparse
import gzip
import hashlib
import html
import io
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np

from . import config, filters
from .topics import _BY_LANG, _FUERTE_BY_LANG, _MASKS, _clean

log = logging.getLogger("noticias_historia")

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
BASE = Path(os.environ.get("BOT_NOTICIAS_HISTORIA_DIR", str(config.DATA_DIR / "noticias_historia")))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) estudio-noticias-paper/1.0"
GDELT_LISTA = "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
DESDE, HASTA = "2023-07-01", "2026-10-28"   # 3 años del laboratorio (épocas 2026-10 y 2026-11) y 3 meses de margen
CLASES = ("cripto", "macro")                # las que en vivo van a la IA local (filters.py)
TEMAS_CRIPTO_RX = re.compile(rb"[A-Z_]*(?:BITCOIN|CRYPTOCURRENC|BLOCKCHAIN)[A-Z_]*")
TITULO_RX = re.compile(rb"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
TITULO_MAX = 280                            # como desk.TITLE_MAX
INTENTOS = 4
IDLE_PRIORITY_CLASS = 0x40

# ---------------------------------------------------------------- filtro (el del bot)

_T_EN, _F_EN = _BY_LANG["en"], _FUERTE_BY_LANG["en"]
_TEMAS_PUERTA = ("cripto",) + tuple(sorted(filters.MACRO_TOPICS))


def puerta(titulo: str) -> bool:
    """¿Podría classify(titulo, lang="en") dar «cripto» o «macro»? Las mismas expresiones y máscaras que classify para
    esas dos clases (monedas, cripto en general, los temas macro, las clases fuertes y la macro en inglés), sin los
    otros 14 temas ni el ruido. Si dice que no, classify tampoco las da (lo comprueban las pruebas)."""
    if filters.detect_coins(titulo) or filters.CRYPTO_RX.search(titulo):
        return True
    text = _clean(titulo)
    if not text:
        return False
    for t in _TEMAS_PUERTA:
        rx = _T_EN.get(t)
        mask = _MASKS.get(t)
        if rx is not None and rx.search(mask.sub(" ", text) if mask else text):
            return True
    if any(rx.search(text) for rx in _F_EN.values()):
        return True
    plain = filters.INFLACION_MASK.sub(" ", text)
    return bool(filters.MACRO_RX.search(plain) or filters.CPI_RX.search(plain))


def clasificar(titulo: str, fuente: str) -> filters.Decision | None:
    """La decisión del bot si es cripto o macro (None si no). `fuente` lleva prefijo («gdelt:», «wp:»): classify
    trata como oficiales las fuentes cuyo id empieza por fed, bls, ecb o sec (fedscoop.com no es la Fed)."""
    if not puerta(titulo):
        return None
    d = filters.classify(titulo, fuente, None, lang="en")
    return d if d.relevant and d.clase in CLASES else None


def limpiar_titulo(raw: str, dominio: str = "") -> str:
    """Entidades HTML fuera, espacios unificados y sin la coletilla del medio («… | Reuters», «… - CNBC») cuando la
    coletilla es el propio medio: el feed RSS de ese medio no la lleva."""
    t = " ".join(html.unescape(raw or "").split())
    m = re.match(r"^(.{15,}?)\s+[|\-–—]\s+([^|\-–—]{2,60})$", t)
    if m and dominio:
        cola = re.sub(r"[^a-z0-9]", "", m.group(2).lower())
        dom = re.sub(r"[^a-z0-9]", "", dominio.lower())
        label = re.sub(r"[^a-z0-9]", "", (dominio.lower().split(".")[-2] if "." in dominio else dominio.lower()))
        if cola and (cola in dom or (len(label) >= 3 and label in cola)):
            t = m.group(1).strip()
    return t[:TITULO_MAX]


# ---------------------------------------------------------------- utilidades

def ms_de(ts14: str) -> int:
    """AAAAMMDDHHMMSS (UTC) → ms."""
    return int(datetime.strptime(ts14, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp() * 1000)


def dias_entre(desde: str, hasta: str) -> list[str]:
    a, b = date.fromisoformat(desde), date.fromisoformat(hasta)
    return [(a + timedelta(days=k)).isoformat() for k in range((b - a).days + 1)]


def escribir_atomico(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def leer_jsonl_gz(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def prioridad_baja() -> None:
    """Prioridad de reposo (Windows: IDLE_PRIORITY_CLASS; la heredan los procesos hijos): solo usa la CPU que no quiere
    nadie, para no frenar al bot ni al laboratorio."""
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetPriorityClass(k32.GetCurrentProcess(), IDLE_PRIORITY_CLASS)
    else:
        try:
            os.nice(15)
        except OSError:
            pass


class Ritmo:
    """Tope de bajada en MB/s entre todos los hilos de un proceso (la wifi la usa también el bot)."""

    def __init__(self, mbs: float | None):
        self.bps = (mbs or 0) * 1e6
        self.lock = threading.Lock()
        self.t = time.monotonic()

    def gastar(self, n: int) -> None:
        if self.bps <= 0:
            return
        with self.lock:
            self.t = max(self.t, time.monotonic()) + n / self.bps
            espera = self.t - time.monotonic()
        if espera > 0:
            time.sleep(espera)


def bajar(url: str, ritmo: Ritmo | None = None, timeout: int = 120) -> tuple[bytes | None, int | None, str]:
    """(contenido, Last-Modified en ms, error). Reintenta con esperas crecientes; 404 no se reintenta."""
    err = ""
    for k in range(INTENTOS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                lm = r.headers.get("Last-Modified")
                trozos = []
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    if ritmo:
                        ritmo.gastar(len(b))
                    trozos.append(b)
            try:
                lm_ms = int(parsedate_to_datetime(lm).timestamp() * 1000) if lm else None
            except (TypeError, ValueError):
                lm_ms = None
            return b"".join(trozos), lm_ms, ""
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:   # red, tiempo, conexión cortada
            err = str(e)[:120]
        time.sleep(5 * (k + 1))
    return None, None, err


# ---------------------------------------------------------------- GDELT

def lista_gdelt(desde: str, hasta: str, base: Path = BASE) -> list[list]:
    """La lista maestra del periodo (solo los GKG en inglés), guardada en gdelt/lista/. Se baja en streaming (~130 MB)."""
    path = base / "gdelt" / "lista" / f"gkg-{desde.replace('-', '')}-{hasta.replace('-', '')}.json"
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    a, b = desde.replace("-", ""), hasta.replace("-", "")
    filas = []
    with urllib.request.urlopen(urllib.request.Request(GDELT_LISTA, headers={"User-Agent": UA}), timeout=300) as r:
        for linea in r:
            p = linea.decode("ascii", "replace").split()
            if len(p) == 3 and p[2].endswith(".gkg.csv.zip"):
                ts = p[2].rsplit("/", 1)[1][:14]
                if a <= ts[:8] <= b:
                    filas.append([ts, int(p[0]), p[1], p[2]])
    escribir_atomico(path, json.dumps(filas).encode())
    return filas


def filas_gkg(csv: bytes, ts14: str, sello_ms: int, cuentas: Counter, dominios: dict) -> list[dict]:
    """Filas cripto y macro de un GKG descomprimido. cuentas y dominios ({clase: Counter}) se actualizan."""
    t_archivo = ms_de(ts14)
    out = []
    for line in csv.split(b"\n"):
        if not line:
            continue
        cuentas["filas"] += 1
        # El titular va en el último campo (Extras): se busca antes de partir la fila, que casi nunca hace falta.
        k = line.rfind(b"\t")
        m = TITULO_RX.search(line, k + 1) if k >= 0 else None
        p = None
        if b"ECON_BITCOIN" in line or b"CRYPTOCURRENC" in line:
            p = line.split(b"\t")
            if len(p) >= 27 and TEMAS_CRIPTO_RX.search(p[7]):
                cuentas["tema_cripto_gdelt"] += 1
        if not m:
            cuentas["sin_titular"] += 1
            continue
        crudo = m.group(1).decode("utf-8", "replace")
        if p is None:
            p = line.split(b"\t")
        if len(p) < 27:
            cuentas["filas_cortas"] += 1
            continue
        dom = p[3].decode("utf-8", "replace").strip().lower()
        tit = limpiar_titulo(crudo, dom)
        if not tit:
            cuentas["sin_titular"] += 1
            continue
        cuentas["con_titular"] += 1
        d = clasificar(tit, "gdelt:" + dom)
        if d is None:
            continue
        cuentas[d.clase] += 1
        dominios.setdefault(d.clase, Counter())[dom] += 1
        try:
            t = ms_de(p[1].decode()[:14])
        except ValueError:
            t = t_archivo
        tono = p[15].split(b",")
        try:
            tono0 = round(float(tono[0]), 3) if tono and tono[0] else None
        except ValueError:
            tono0 = None
        out.append({"id": p[0].decode("ascii", "replace"), "t": t, "s": max(sello_ms, t), "dom": dom,
                    "url": p[4].decode("utf-8", "replace")[:500], "tit": tit, "clase": d.clase, "mon": d.coins,
                    "temas": d.topics, "macro": bool(d.macro), "tono": tono0,
                    "gdelt": sorted({x.decode() for x in TEMAS_CRIPTO_RX.findall(p[7])})})
    return out


def procesar_dia(dia: str, archivos: list[list], carpeta: Path, hilos: int = 4, mbs: float | None = None) -> dict:
    """Un día de GKG: baja sus archivos en memoria (hilos a la vez, con el tope de MB/s), comprueba el md5, filtra y
    guarda AAAA-MM-DD.jsonl.gz y su resumen AAAA-MM-DD.json (este al final: marca el día como hecho)."""
    t0 = time.time()
    ritmo = Ritmo(mbs)
    cuentas, dominios, filas, faltan = Counter(), {}, [], []
    mb = 0.0

    def uno(a):
        zb, lm, err = bajar(a[3], ritmo)
        return a, zb, lm, err

    with ThreadPoolExecutor(max(1, hilos)) as ex:
        pend = list(archivos)
        vivos = set()
        while pend or vivos:
            while pend and len(vivos) < 2 * max(1, hilos):   # pocos zips en memoria a la vez
                vivos.add(ex.submit(uno, pend.pop(0)))
            hecho = next(as_completed(vivos))
            vivos.discard(hecho)
            a, zb, lm, err = hecho.result()
            if zb is None or hashlib.md5(zb).hexdigest() != a[2]:
                faltan.append({"archivo": a[0], "error": err or "md5 distinto"})
                continue
            mb += len(zb) / 1e6
            try:
                with zipfile.ZipFile(io.BytesIO(zb)) as z:
                    csv = z.read(z.namelist()[0])
            except (zipfile.BadZipFile, IndexError, OSError) as e:
                faltan.append({"archivo": a[0], "error": f"zip: {e}"[:120]})
                continue
            cuentas["archivos_ok"] += 1
            sello = max(ms_de(a[0]), (lm // MIN + 1) * MIN if lm else 0)
            if lm and lm > ms_de(a[0]):
                cuentas["publicados_tarde"] += 1
            filas += filas_gkg(csv, a[0], sello, cuentas, dominios)
    filas.sort(key=lambda r: (r["s"], r["t"], r["id"]))
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as g:
        for r in filas:
            g.write((json.dumps(r, ensure_ascii=False) + "\n").encode())
    escribir_atomico(carpeta / f"{dia}.jsonl.gz", buf.getvalue())
    res = {"dia": dia, "archivos": len(archivos), "archivos_ok": cuentas.pop("archivos_ok", 0), "faltan": faltan,
           "completo": not faltan, "mb_zip": round(mb, 1), "filas": dict(cuentas), "guardadas": len(filas),
           "dominios": {c: dict(v.most_common(200)) for c, v in dominios.items()},
           "segundos": round(time.time() - t0, 1), "hecho": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    escribir_atomico(carpeta / f"{dia}.json", json.dumps(res, ensure_ascii=False, indent=1).encode())
    return res


def _dia_hecho(carpeta: Path, dia: str) -> bool:
    p = carpeta / f"{dia}.json"
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text("utf-8")).get("completo"))
    except (OSError, ValueError):
        return False


def _trabajo_dia(args) -> dict:
    dia, archivos, carpeta, hilos, mbs = args
    try:
        return procesar_dia(dia, archivos, Path(carpeta), hilos, mbs)
    except Exception as e:   # un día que falla se repite al relanzar
        return {"dia": dia, "error": f"{type(e).__name__}: {e}"[:300]}


def descargar_gdelt(desde: str = DESDE, hasta: str = HASTA, procesos: int = 2, hilos: int = 4, mbs: float = 10.0,
                    base: Path = BASE) -> dict:
    """Todos los días del periodo que falten, de los más recientes a los más antiguos (si se para a medias, lo que más
    falta es lo más viejo, que solo es calentamiento)."""
    from multiprocessing import get_context
    prioridad_baja()
    lista = lista_gdelt(desde, hasta, base)
    por_dia: dict[str, list] = {}
    for f in lista:
        por_dia.setdefault(f"{f[0][:4]}-{f[0][4:6]}-{f[0][6:8]}", []).append(f)
    carpeta = base / "gdelt" / "dias"
    carpeta.mkdir(parents=True, exist_ok=True)
    dias = [d for d in sorted(por_dia, reverse=True) if not _dia_hecho(carpeta, d)]
    log.info("GDELT: %d días por hacer de %d (%d archivos en la lista)", len(dias), len(por_dia), len(lista))
    hechos, errores, t0 = 0, [], time.time()
    parar = base / "parar"
    trabajos = [(d, por_dia[d], str(carpeta), hilos, mbs / max(1, procesos)) for d in dias]
    with get_context("spawn").Pool(max(1, procesos), maxtasksperchild=20) as pool:
        for r in pool.imap_unordered(_trabajo_dia, trabajos):
            if r.get("error"):
                errores.append(r)
                log.warning("GDELT %s: %s", r["dia"], r["error"])
            else:
                hechos += 1
                log.info("GDELT %s: %d de %d archivos, %d guardadas (%s), %.0f s · %d/%d días · %.1f h",
                         r["dia"], r["archivos_ok"], r["archivos"], r["guardadas"],
                         ", ".join(f"{k} {v}" for k, v in r["filas"].items() if k in CLASES), r["segundos"],
                         hechos, len(dias), (time.time() - t0) / 3600)
            if parar.exists():
                log.info("GDELT: «parar» encontrado; se para al acabar los días en curso")
                pool.terminate()
                break
    return {"hechos": hechos, "errores": errores, "pendientes": len(dias) - hechos, "horas": (time.time() - t0) / 3600}


# ---------------------------------------------------------------- WordPress (24 medios de cripto)

WP = ["bitcoinist.com", "www.newsbtc.com", "cryptopotato.com", "ambcrypto.com", "dailyhodl.com", "cryptobriefing.com",
      "crypto.news", "zycrypto.com", "thecryptobasic.com", "www.coinspeaker.com", "blockonomi.com", "coincentral.com",
      "coinjournal.net", "finbold.com", "www.cryptopolitan.com", "coinedition.com", "timestabloid.com",
      "thenewscrypto.com", "www.cryptotimes.io", "bitcoinmagazine.com", "coinchapter.com",
      "www.crypto-news-flash.com", "coinfomania.com", "www.tronweekly.com"]   # controles/bruscos/descarga_bruscos.py
WP_PAUSA_S = 1.5
WP_PAUSA_SITIO = {"cryptobriefing.com": 10.0}   # corta con 429 a 1,5 s
WP_CAMPOS = "id,date,date_gmt,link,title,categories"
WP_MARGEN_H = 15   # after/before van en la hora LOCAL del sitio (de −12 a +14 h): se pide de más y se filtra por date_gmt


def medio_de(sitio: str) -> str:
    """bitcoinist.com, newsbtc.com… (sin «www.»), como desk.outlet_of."""
    host = sitio.lower().split("/")[0]
    return host[4:] if host.startswith("www.") else host


def meses_entre(desde: str, hasta: str) -> list[str]:
    a, b = date.fromisoformat(desde), date.fromisoformat(hasta)
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _gmt_ms(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(re.sub(r"(Z|[+-]\d\d:\d\d)$", "", s)).replace(tzinfo=timezone.utc)
                   .timestamp() * 1000)
    except ValueError:
        return None


def posts_wp(posts: list, sitio: str, a_ms: int, b_ms: int) -> list[dict]:
    """Posts de la API de WordPress → filas {id, t, s, dom, url, tit} con date_gmt en [a, b). Sin filtrar aún."""
    out, dom = [], medio_de(sitio)
    for p in posts if isinstance(posts, list) else []:
        if not isinstance(p, dict):
            continue
        t = _gmt_ms(p.get("date_gmt"))
        tit = p.get("title")
        tit = tit.get("rendered") if isinstance(tit, dict) else tit
        if t is None or not (a_ms <= t < b_ms) or not isinstance(tit, str):
            continue
        tit = limpiar_titulo(re.sub(r"<[^>]+>", " ", tit), dom)
        if tit:
            out.append({"id": f"{dom}:{p.get('id')}", "t": t, "s": t, "dom": dom, "url": str(p.get("link") or "")[:500],
                        "tit": tit, "cat": [c for c in (p.get("categories") or []) if isinstance(c, int)][:12]})
    return out


def mes_wp(sitio: str, mes: str, ritmo_s: float, carpeta: Path) -> dict:
    """Un mes de un sitio: todas las páginas, filtrado por date_gmt al mes UTC, clasificado como el bot y guardado
    en wp/<medio>/AAAA-MM.jsonl.gz con su resumen AAAA-MM.json (al final: marca el mes como hecho)."""
    y, m = int(mes[:4]), int(mes[5:])
    a = datetime(y, m, 1, tzinfo=timezone.utc)
    b = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    a_ms, b_ms = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
    after = (a - timedelta(hours=WP_MARGEN_H)).strftime("%Y-%m-%dT%H:%M:%S")
    before = (b + timedelta(hours=WP_MARGEN_H)).strftime("%Y-%m-%dT%H:%M:%S")
    filas, pagina, paginas, peticiones, err = [], 1, 1, 0, ""
    while pagina <= paginas:
        q = urllib.parse.urlencode({"per_page": 100, "page": pagina, "after": after, "before": before,
                                    "orderby": "date", "order": "asc", "_fields": WP_CAMPOS})
        url = f"https://{sitio}/wp-json/wp/v2/posts?{q}"
        st, hd, body = _get_wp(url)
        peticiones += 1
        time.sleep(ritmo_s)
        if st == 400 and pagina > 1:   # página fuera de rango
            break
        if st != 200:
            err = f"HTTP {st}"
            break
        try:
            js = json.loads(body.decode("utf-8-sig", "replace"))
            assert isinstance(js, list)
        except (ValueError, AssertionError):
            err = "respuesta que no es una lista JSON"
            break
        filas += posts_wp(js, sitio, a_ms, b_ms)
        paginas = int(hd.get("x-wp-totalpages") or 1)
        pagina += 1
    if err:
        return {"sitio": sitio, "mes": mes, "error": err, "peticiones": peticiones}
    vistos, out, cuentas = set(), [], Counter()
    for r in sorted(filas, key=lambda r: (r["t"], r["id"])):
        if r["id"] in vistos:
            continue
        vistos.add(r["id"])
        cuentas["posts"] += 1
        d = clasificar(r["tit"], "wp:" + r["dom"])
        if d is None:
            continue
        cuentas[d.clase] += 1
        out.append({**r, "clase": d.clase, "mon": d.coins, "temas": d.topics, "macro": bool(d.macro)})
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as g:
        for r in out:
            g.write((json.dumps(r, ensure_ascii=False) + "\n").encode())
    escribir_atomico(carpeta / f"{mes}.jsonl.gz", buf.getvalue())
    res = {"sitio": sitio, "mes": mes, "peticiones": peticiones, "cuentas": dict(cuentas), "guardadas": len(out),
           "hecho": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    escribir_atomico(carpeta / f"{mes}.json", json.dumps(res, ensure_ascii=False).encode())
    return res


def _get_wp(url: str, tries: int = 5) -> tuple[int, dict, bytes]:
    """(estado, cabeceras en minúsculas, cuerpo). 400/401/403/404 no se reintentan (no se salta ningún bloqueo)."""
    espera = 10.0
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, {k2.lower(): v for k2, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                return e.code, {}, b""
            if e.code == 429:
                ra = (e.headers or {}).get("Retry-After")
                if ra and ra.isdigit():
                    espera = max(espera, min(float(ra), 300.0))
        except Exception:
            pass
        if k < tries - 1:
            time.sleep(espera)
            espera = min(espera * 2, 300.0)
    return 0, {}, b""


def sitio_wp(sitio: str, meses: list[str], base: Path = BASE, parar: Path | None = None) -> dict:
    """Los meses que falten de un sitio, del más reciente al más antiguo. Tres meses fallidos seguidos: se deja."""
    carpeta = base / "wp" / medio_de(sitio)
    carpeta.mkdir(parents=True, exist_ok=True)
    ritmo = WP_PAUSA_SITIO.get(sitio, WP_PAUSA_S)
    res = {"sitio": sitio, "hechos": 0, "fallidos": [], "guardadas": 0, "posts": 0}
    seguidos = 0
    for mes in sorted(meses, reverse=True):
        if parar is not None and parar.exists():
            res["parado"] = True
            break
        if (carpeta / f"{mes}.json").exists():
            continue
        r = mes_wp(sitio, mes, ritmo, carpeta)
        if r.get("error"):
            res["fallidos"].append(r)
            seguidos += 1
            log.warning("WP %s %s: %s", sitio, mes, r["error"])
            if seguidos >= 3:
                res["dejado"] = "3 meses fallidos seguidos"
                break
            continue
        seguidos = 0
        res["hechos"] += 1
        res["guardadas"] += r["guardadas"]
        res["posts"] += r["cuentas"].get("posts", 0)
        log.info("WP %s %s: %d posts, %d guardadas (%s)", sitio, mes, r["cuentas"].get("posts", 0), r["guardadas"],
                 ", ".join(f"{k} {v}" for k, v in r["cuentas"].items() if k in CLASES))
    return res


def descargar_wp(desde: str = DESDE, hasta: str = HASTA, base: Path = BASE, sitios=None) -> dict:
    prioridad_baja()
    meses = meses_entre(desde, hasta)
    sitios = list(sitios or WP)
    with ThreadPoolExecutor(len(sitios)) as ex:
        res = list(ex.map(lambda s: sitio_wp(s, meses, base, base / "parar"), sitios))
    escribir_atomico(base / "wp" / "resumen.json", json.dumps(
        {"hecho": datetime.now(timezone.utc).isoformat(timespec="seconds"), "desde": desde, "hasta": hasta,
         "sitios": res}, ensure_ascii=False, indent=1).encode())
    return {"sitios": len(res), "fallidos": sum(len(r["fallidos"]) for r in res)}


# ---------------------------------------------------------------- historias (deduplicadas como la mesa)

RACIMO_MIN = 30           # «racimo»: medios distintos en los 30 min desde la primera publicación (H34 del catálogo)
# Medios distintos (de los que sigue el bot) para ir a la IA local. Fijado el 29-10-2026 solo por volumen, antes de
# mirar ningún precio: en octubre de 2026 salen 3,4 historias de cripto al día con ≥ 2 y 3,5 macro con ≥ 4 (con ≥ 3
# serían 5,8), unas 8.400 en 3 años, lo que cabe en los huecos de la GPU (~1.500 al día).
RACIMO_N = {"cripto": 2, "macro": 3}
# 30-10-2026, con la criba de basura (las copias del mismo titular ya no suman medio): de agosto a octubre de 2026 quedan
# 2,62 historias de cripto al día con ≥ 2 medios y 0,58 macro con ≥ 3 (con ≥ 2 serían 5,98 y con ≥ 4, 0,10). Antes de la
# criba, la macro con ≥ 4 era 3,5 al día: casi todo, copias.


# Criba de basura antes del triaje (30-10-2026, la pidió el usuario; fijada por el contenido, sin mirar precios):
# - copias: una pieza con el MISMO titular (sin mayúsculas ni signos) que otra de la historia no suma medio: notas de
#   prensa sindicadas (Travala, Bybit, MEXC…) y prensa local o cadenas que repiten el texto (Newsquest, Reach, NBC…);
# - notas de prensa por su URL (distribuidoras y secciones de pago) o por frases que solo usan ellas;
# - partes de guerra, terrorismo o sucesos sin lectura de mercado: macro cuyos temas son solo guerra, geopolítica,
#   política o catástrofe (o solo «geopolítica fuerte») sin ninguna palabra de mercado en el titular.
PR_URL_RX = re.compile(r"prnewswire|globenewswire|chainwire|businesswire|accesswire|accessnewswire|newsfilecorp|"
                       r"einpresswire|newswire\.ca|/press-?releases?/|/pr/|/sponsored|/partner-?content|/pressrelease|"
                       r"/paid-?content|/brand-?story", re.I)
PR_TIT_RX = re.compile(r"\b(?:press release|sponsored|partner content|prize pool|airdrop campaign|giveaway|"
                       r"trading competition|trading contest|loyalty program)\b", re.I)
MERCADO_RX = re.compile(r"\b(?:oil|crude|brent|wti|gas|lng|opec|gold|stocks?|shares|markets?|wall street|dow|nasdaq|"
                        r"s&p|ftse|nikkei|dax|bitcoin|crypto\w*|dollar|euro|yen|yuan|currenc\w+|yields?|bonds?|"
                        r"treasur\w+|rates?|inflation|econom\w+|gdp|sanctions?|tariffs?|trade|exports?|imports?|"
                        r"shipping|strait|hormuz|suez|tankers?|refiner\w*|pipelines?|prices?|investors?|banks?|fed|ecb|"
                        r"imf|budget|debt|default|ceasefire|truce|peace)\b", re.I)
TEMAS_SIN_MERCADO = {"guerra", "geopolitica", "politica", "elecciones", "catastrofe"}


def titulo_norma(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def basura(h: dict) -> str | None:
    """Por qué una historia es basura para el triaje (None si no lo es). Solo mira su titular, su URL y sus temas."""
    if PR_URL_RX.search(h.get("url") or "") or PR_TIT_RX.search(h.get("tit") or ""):
        return "nota de prensa"
    if h.get("clase") == "macro" and not MERCADO_RX.search(h.get("tit") or ""):
        tm = set(h.get("temas") or [])
        if tm and tm <= TEMAS_SIN_MERCADO and tm & {"guerra", "geopolitica"}:
            return "parte de guerra o sucesos sin lectura de mercado"
        if not tm:
            from .topics import strong_kinds
            if strong_kinds(h.get("tit") or "", "en") == ["geopolitica_fuerte"]:
                return "parte de guerra o sucesos sin lectura de mercado"
    return None


def medios_vivos(base: Path = BASE) -> set[str]:
    """Los medios que sigue el bot en vivo (los dominios de bot/fuentes/*.json, sin relays) y los 24 de cripto: la mesa
    del histórico solo ve estos, como la de vivo. La lista se congela en medios_vivos.json la primera vez (la de
    bot/fuentes cambia con el tiempo y el histórico tiene que salir igual al rehacerlo)."""
    p = base / "medios_vivos.json"
    if p.exists():
        return set(json.loads(p.read_text("utf-8"))["medios"])
    vivos = sorted(set(fuentes_vivas_por_medio()) | {medio_de(s) for s in WP})
    escribir_atomico(p, json.dumps({"creado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "nota": "dominios de bot/fuentes/*.json (sin Google News ni feedburner) y los 24 "
                                            "medios de cripto de WordPress", "medios": vivos},
                                   ensure_ascii=False, indent=0).encode())
    return set(vivos)


def filas_mes(mes: str, base: Path = BASE, desde: str = DESDE, hasta: str = HASTA) -> tuple[list[dict], dict]:
    """Las filas de GDELT (los días del mes dentro del periodo) y de los 24 medios (sus archivos del mes), juntas, del
    periodo [desde, hasta] y por sello, con lo que falta (días de GDELT o meses de WordPress sin hacer)."""
    y, m = int(mes[:4]), int(mes[5:])
    a = max(date(y, m, 1), date.fromisoformat(desde))
    b = min(date(y + (m == 12), m % 12 + 1, 1), date.fromisoformat(hasta) + timedelta(days=1))
    a_ms = int(datetime(a.year, a.month, a.day, tzinfo=timezone.utc).timestamp() * 1000)
    b_ms = int(datetime(b.year, b.month, b.day, tzinfo=timezone.utc).timestamp() * 1000)
    filas, faltan = [], {"gdelt": [], "wp": []}
    car = base / "gdelt" / "dias"
    vivos = medios_vivos(base)
    for k in range((b - a).days):
        d = (a + timedelta(days=k)).isoformat()
        if not _dia_hecho(car, d):
            faltan["gdelt"].append(d)
            continue
        for r in leer_jsonl_gz(car / f"{d}.jsonl.gz"):
            if medio_corto(r["dom"]) in vivos:   # de GDELT, solo los medios que sigue el bot
                r["fuente"] = "gdelt"
                filas.append(r)
    for sitio in WP:
        car = base / "wp" / medio_de(sitio)
        if not (car / f"{mes}.json").exists():
            faltan["wp"].append(medio_de(sitio))
            continue
        for r in leer_jsonl_gz(car / f"{mes}.jsonl.gz"):
            if a_ms <= r["t"] < b_ms:
                r["fuente"] = "wp"
                filas.append(r)
    filas.sort(key=lambda r: (r["s"], r["t"], r["id"]))
    return filas, faltan


class Mesa:
    """La deduplicación de desk.NewsDesk: una pieza es de una historia reciente (de las últimas
    config.DEDUPE_WINDOW_S) si filters.similar con el titular de su PRIMERA pieza; si no, abre historia. Las demás
    piezas la confirman (medios distintos, con su sello)."""

    def __init__(self, ventana_ms: int = config.DEDUPE_WINDOW_S * 1000):
        self.ventana = ventana_ms
        self.recientes: list[dict] = []   # historias abiertas, por orden de llegada
        self.idx: dict[str, list[dict]] = {}
        self.seq = 0

    def _expirar(self, s: int) -> list[dict]:
        out = []
        while self.recientes and s - self.recientes[0]["s0"] > self.ventana:
            h = self.recientes.pop(0)
            for w in h["_w"]:
                lst = self.idx.get(w)
                if lst:
                    if lst[0] is h:
                        lst.pop(0)
                    else:
                        try:
                            lst.remove(h)
                        except ValueError:
                            pass
                    if not lst:
                        del self.idx[w]
            out.append(h)
        return out

    def pieza(self, r: dict) -> list[dict]:
        """Añade una pieza (en orden de sello). Devuelve las historias que se cierran (ya no pueden crecer)."""
        cerradas = self._expirar(r["s"])
        words = filters.tokens(r["tit"])
        hits: Counter = Counter()
        por_id = {}
        for w in words:
            for h in self.idx.get(w, ()):
                hits[id(h)] += 1
                por_id[id(h)] = h
        dup = None
        for hid in sorted((k for k, n in hits.items() if n >= 2), key=lambda k: por_id[k]["_seq"]):
            if filters.similar(words, por_id[hid]["_w"]):
                dup = por_id[hid]
                break
        if dup is not None:
            dup["n_piezas"] += 1
            tn = titulo_norma(r["tit"])
            if tn in dup["_tits"]:   # el mismo texto en otro medio: copia (nota de prensa o sindicación), no suma
                if r["dom"] not in dup["_doms"]:
                    dup["n_copias"] += 1
                return cerradas
            dup["_tits"].add(tn)
            if r["dom"] not in dup["_doms"]:
                dup["_doms"].add(r["dom"])
                dup["medios"].append([r["dom"], r["s"], r["fuente"]])
            return cerradas
        h = {"id": "h" + hashlib.sha1(f"{r['fuente']}|{r['id']}".encode()).hexdigest()[:12], "t0": r["t"], "s0": r["s"],
             "tit": r["tit"], "dom": r["dom"], "fuente": r["fuente"], "url": r.get("url", ""), "clase": r["clase"],
             "mon": r.get("mon") or [], "temas": r.get("temas") or [], "macro": r.get("macro", False),
             "medios": [[r["dom"], r["s"], r["fuente"]]], "n_piezas": 1, "n_copias": 0,
             "_w": words, "_doms": {r["dom"]}, "_tits": {titulo_norma(r["tit"])}, "_seq": self.seq}
        self.seq += 1
        self.recientes.append(h)
        for w in words:
            self.idx.setdefault(w, []).append(h)
        return cerradas

    def cerrar_todo(self) -> list[dict]:
        out, self.recientes, self.idx = self.recientes, [], {}
        return out


def racimo(h: dict, n: int, minutos: int = RACIMO_MIN) -> int | None:
    """Sello del n-ésimo medio distinto si llega en los `minutos` desde la primera publicación; None si no."""
    lim = h["s0"] + minutos * MIN
    ss = sorted(s for _, s, _ in h["medios"] if s <= lim)
    return ss[n - 1] if len(ss) >= n else None


def historia_limpia(h: dict) -> dict:
    out = {k: v for k, v in h.items() if not k.startswith("_")}
    out["basura"] = basura(out)
    out["n_medios"] = len(h["medios"])
    out["n30"] = sum(1 for _, s, _ in h["medios"] if s <= h["s0"] + RACIMO_MIN * MIN)
    return out


def construir_historias(desde: str = DESDE, hasta: str = HASTA, base: Path = BASE) -> dict:
    """historias/AAAA-MM.jsonl.gz (por el mes de su primera pieza) con el resumen historias/AAAA-MM.json. Solo los
    meses con GDELT y los 24 medios completos; la ventana de 3 h pasa de un mes al siguiente."""
    car = base / "historias"
    car.mkdir(parents=True, exist_ok=True)
    mesa, res = Mesa(), {}
    pend: dict[str, list] = {}
    meses = meses_entre(desde, hasta)
    for mes in meses:
        filas, faltan = filas_mes(mes, base, desde, hasta)
        if faltan["gdelt"] or faltan["wp"]:
            res[mes] = {"incompleto": {k: len(v) for k, v in faltan.items()}}
            mesa = Mesa()   # sin el mes anterior completo, la ventana empieza de cero
            pend = {}
            continue
        for r in filas:
            for h in mesa.pieza(r):
                pend.setdefault(datetime.fromtimestamp(h["s0"] / 1000, timezone.utc).strftime("%Y-%m"), []).append(h)
        # lo del mes anterior ya no puede crecer: se escribe
        for m2 in [k for k in pend if k < mes]:
            _escribir_historias(car, m2, pend.pop(m2), res)
    for h in mesa.cerrar_todo():
        pend.setdefault(datetime.fromtimestamp(h["s0"] / 1000, timezone.utc).strftime("%Y-%m"), []).append(h)
    for m2 in sorted(pend):
        if m2 in meses and "incompleto" not in res.get(m2, {}):
            _escribir_historias(car, m2, pend[m2], res)
    escribir_atomico(car / "resumen.json", json.dumps(res, ensure_ascii=False, indent=1).encode())
    return res


def _escribir_historias(car: Path, mes: str, hs: list[dict], res: dict) -> None:
    hs = sorted((historia_limpia(h) for h in hs), key=lambda h: (h["s0"], h["id"]))
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as g:
        for h in hs:
            g.write((json.dumps(h, ensure_ascii=False) + "\n").encode())
    escribir_atomico(car / f"{mes}.jsonl.gz", buf.getvalue())
    c = Counter()
    for h in hs:
        c[h["clase"]] += 1
        for n in (2, 3, 5, 10):
            if h["n30"] >= n:
                c[f"{h['clase']}_racimo{n}"] += 1
    res[mes] = {"historias": len(hs), **dict(c)}


# ---------------------------------------------------------------- triaje (la IA local del bot, en sus huecos)

TRIAJE_IDLE_S = 3.0        # el hueco del servidor tiene que llevar libre tanto antes de pedir (el bot no espera)
TRIAJE_SONDEO_S = 1.0
TRIAJE_RETRASO_MS = MIN    # el triaje se usa desde el minuto siguiente al sello del racimo (en vivo tarda ~7 s)


def fuentes_vivas_por_medio() -> dict[str, tuple[str, str, str | None]]:
    """medio → (nombre, nivel, región) del feed en vivo de ese medio (bot/fuentes/*.json), el de mejor nivel; sin
    Google News ni relays. Lo que no tenga feed va como en vivo un medio sin historial: su dominio y T2."""
    out = {}
    orden = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
    for f in sorted((Path(__file__).parent / "fuentes").glob("*.json")):
        try:
            d = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        items = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
        for it in items:
            if not isinstance(it, dict) or not it.get("url"):
                continue
            host = (urllib.parse.urlsplit(it["url"]).hostname or "").lower()
            if not host or "google." in host or "feedburner" in host:
                continue
            parts = [p for p in host.split(".") if p and p != "www"]
            med = ".".join(parts[-3:]) if len(parts) >= 3 and parts[-2] in ("co", "com", "ne", "or", "go", "ac", "gov",
                                                                         "gob", "org", "net", "edu", "gv") \
                else ".".join(parts[-2:])
            prev = out.get(med)
            tier = it.get("tier") or "T2"
            if prev is None or orden.get(tier, 9) < orden.get(prev[1], 9):
                out[med] = (str(it.get("name") or med), tier, it.get("region"))
    return out


def medio_corto(dom: str) -> str:
    parts = [p for p in dom.lower().split(".") if p and p != "www"]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "ne", "or", "go", "ac", "gov", "gob", "org", "net", "edu", "gv"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def cuerpo_triaje(titulo: str, src: str, tier: str, coins: list[str]) -> dict:
    """El cuerpo de la petición de LocalAI.classify, tal cual (lo comprueba la prueba)."""
    from . import local_ai
    return {
        "model": config.LOCAL_AI_ALIAS, "temperature": 0, "max_tokens": 700,
        "messages": [{"role": "system", "content": local_ai.SYSTEM},
                     {"role": "user", "content": f"Titular: {titulo}\nFuente: {src} (nivel {tier})\n"
                                                 f"Monedas detectadas por reglas: {', '.join(coins) or 'ninguna'}"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "triaje", "schema": local_ai.SCHEMA,
                                                                   "strict": True}},
        "chat_template_kwargs": {"enable_thinking": False},
    }


def leer_triaje(data: dict, titulo: str, coins: list[str], region: str | None) -> dict:
    """La respuesta del servidor → el triaje del contrato 2.7, como LocalAI.classify y LocalAI._worker."""
    from . import local_ai
    from .regions import REGIONS
    text = data["choices"][0]["message"]["content"]
    raw = json.loads(text[text.find("{"):text.rfind("}") + 1])
    if not isinstance(raw, dict) or local_ai._num(raw.get("importancia"), 0, 10, None) is None:
        raise ValueError("respuesta sin importancia")
    tr = local_ai.normalize_triage(raw, titulo, coins)
    home = REGIONS.get(region, (None, None))[1] if region else None
    rep = tr.get("repercusion") or {}
    if home and tr["tipo"] != "otro" and rep.get("bolsas") and home not in rep["bolsas"]:
        rep["bolsas"] = [*rep["bolsas"], home]
    return tr


def seleccion(h: dict) -> int | None:
    """¿Va la historia a la IA local? Sí si es un racimo (RACIMO_N[clase] medios distintos en RACIMO_MIN): devuelve
    desde cuándo se puede usar su triaje (sello del n-ésimo medio + TRIAJE_RETRASO_MS, al minuto)."""
    if (h["basura"] if "basura" in h else basura(h)):
        return None
    s = racimo(h, RACIMO_N.get(h.get("clase"), 99))
    return None if s is None else -(-(s + TRIAJE_RETRASO_MS) // MIN) * MIN


# Orden del triaje (no cambia QUÉ se tría, solo cuándo): primero los 91 días de controles/fuentes y las ventanas de
# controles/bruscos (para comprobar pronto que el histórico repite lo ya medido), luego el resto, de lo más nuevo a lo
# más viejo.
VENTANA_91 = ("2026-07-29", "2026-10-28")


def _ventanas_bruscos() -> list[tuple[int, int, int]]:
    """(desde, hasta, prioridad) de las ventanas «antes» (1) y de control (2) de controles/bruscos/episodios.json."""
    try:
        d = json.loads((config.BACKTEST_DIR / "controles" / "bruscos" / "episodios.json").read_text("utf-8"))
        return [(int(v["desde_ms"]), int(v["hasta_ms"]), 1 if v.get("tipo") == "episodio" else 2)
                for v in d.get("ventanas") or []]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def prioridad_triaje(uso: int, ventanas: list[tuple[int, int, int]]) -> int:
    a = int(datetime.fromisoformat(VENTANA_91[0]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    b = int(datetime.fromisoformat(VENTANA_91[1]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    if a <= uso < b:
        return 0
    return min((p for x, y, p in ventanas if x <= uso < y), default=3)


def pendientes_triaje(base: Path = BASE, meses: list[str] | None = None) -> list[tuple[str, dict]]:
    """(mes, historia) seleccionadas y sin triaje, en el orden de prioridad_triaje y, dentro, de lo más nuevo a lo más
    viejo."""
    car = base / "historias"
    hechas = triadas(base)
    ventanas = _ventanas_bruscos()
    out = []
    for p in sorted(car.glob("*.jsonl.gz"), reverse=True):
        mes = p.name[:7]
        if meses and mes not in meses:
            continue
        for h in leer_jsonl_gz(p):
            u = seleccion(h)
            if h["id"] not in hechas and u is not None:
                out.append((prioridad_triaje(u, ventanas), -u, mes, h))
    out.sort(key=lambda x: (x[0], x[1], x[3]["id"]))
    return [(mes, h) for _, _, mes, h in out]


def triadas(base: Path = BASE) -> dict[str, dict]:
    out = {}
    for p in sorted((base / "triaje").glob("*.jsonl")):
        with open(p, encoding="utf-8") as f:
            for x in f:
                try:
                    r = json.loads(x)
                    out[r["id"]] = r
                except (ValueError, KeyError):
                    continue   # una línea a medias de un corte
    return out


def _hueco_libre(url: str) -> bool | None:
    """¿El servidor de la IA local está libre? None si no responde (apagado o cargando)."""
    try:
        with urllib.request.urlopen(url + "/slots", timeout=3) as r:
            slots = json.loads(r.read().decode("utf-8", "replace"))
        return not any(s.get("is_processing") for s in slots if isinstance(s, dict))
    except Exception:
        return None


def triar(base: Path = BASE, max_n: int | None = None, horas: float | None = None, idle_s: float = TRIAJE_IDLE_S,
          url: str | None = None, esperar_min: float | None = None, cuota: float = 0.0) -> dict:
    """Tría las historias seleccionadas con la IA local del bot, de una en una y solo cuando su hueco lleva idle_s
    libre (el bot no tiene nada esperando) y no hay gpu.lock de la fábrica: nunca le quita el turno al triaje en
    vivo, como mucho le retrasa una noticia que llegue mientras (~7 s). Guarda triaje/AAAA-MM.jsonl línea a línea;
    «parar_triaje» en la carpeta lo para. Con esperar_min, al acabar lo pendiente vuelve a mirar cada esperar_min
    minutos si hay historias nuevas (meses que se van construyendo) hasta que se le pare."""
    tot = {"hechas": 0, "fallos": 0, "pendientes": 0, "horas": 0.0}
    t0 = time.time()
    while True:
        r = _triar_una_vez(base, None if not max_n else max_n - tot["hechas"],
                           None if not horas else horas - (time.time() - t0) / 3600, idle_s, url, cuota)
        tot["hechas"] += r["hechas"]
        tot["fallos"] += r["fallos"]
        tot["pendientes"] = r["pendientes"]
        if not esperar_min or (base / "parar_triaje").exists() or (max_n and tot["hechas"] >= max_n) \
                or (horas and time.time() - t0 > horas * 3600) or r.get("roto"):
            break
        if r["pendientes"] == 0:
            log.info("Triaje: nada pendiente; vuelvo a mirar en %.0f min", esperar_min)
            fin = time.time() + esperar_min * 60
            while time.time() < fin and not (base / "parar_triaje").exists():
                time.sleep(5)
    tot["horas"] = (time.time() - t0) / 3600
    return tot


CUOTA_MAX = 0.5   # nunca más de la mitad del tiempo de la GPU para el histórico
# Con cuota, cada 10 min se mira el triaje en vivo (solo lectura de bot.db y bot.log). Si lo importante se retrasa o la
# cola tira de más, 60 min solo en los huecos (lo pidió la sesión principal el 30-10-2026 al aprobar la cuota del 20 %).
VIGILA_S, HUECOS_S = 600, 3600
VIGILA = {"ventana_min": 30, "p90_max_s": 300, "sin_triar_max": 3, "sin_triar_tras_min": 15, "tiradas_max_30min": 60}
_VIGILA: dict = {"huecos_hasta": 0.0, "ultima": 0.0}


def vigilar_vivo(db: Path | None = None, log_path: Path | None = None, ahora_ms: int | None = None) -> dict:
    """Cómo va el triaje en vivo: retraso de captura a triaje de las noticias T0 y T1 frescas de cripto y macro (las
    que van primero en la cola), las que llevan > 15 min sin triar, y cuántas ha tirado la cola llena en 30 min."""
    import sqlite3
    db = Path(db or config.DB_PATH)
    log_path = Path(log_path or (config.LOG_DIR / "bot.log"))
    now = int(ahora_ms or time.time() * 1000)
    a = now - VIGILA["ventana_min"] * MIN
    out = {"ts": now, "lat_p90_s": None, "n": 0, "sin_triar": 0, "tiradas_30min": None}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        cap = {}
        for ts, story, body in con.execute("SELECT ts, story, body FROM events WHERE kind = 'captured' AND ts > ?",
                                           (a,)):
            p = (json.loads(body or "{}").get("patch") or {})
            if p.get("tier") in ("T0", "T1") and p.get("fresh") and p.get("clase") in CLASES \
                    and p.get("srcState", "activa") == "activa":
                cap[story] = ts
        tri = {s: ts for ts, s in con.execute("SELECT ts, story FROM events WHERE kind IN ('triaged', 'lowprio') "
                                              "AND ts > ?", (a,))}
        con.close()
    except Exception as e:   # sin base no se puede vigilar: se dice y se tría solo en los huecos
        return {**out, "error": str(e)[:120]}
    lat = sorted((tri[s] - t) / 1000 for s, t in cap.items() if s in tri and tri[s] >= t)
    out["n"] = len(lat)
    out["lat_p90_s"] = round(lat[int(0.9 * (len(lat) - 1))], 1) if lat else None
    out["sin_triar"] = sum(1 for s, t in cap.items() if s not in tri and t < now - VIGILA["sin_triar_tras_min"] * MIN)
    try:   # «Cola de la IA local llena: N noticias …»: N es lo tirado desde que arrancó el bot
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            lineas = f.read().decode("utf-8", "replace").splitlines()
        marcas = []
        for x in lineas:
            m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*Cola de la IA local llena: (\d+) noticias", x)
            if m:
                t = int(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                marcas.append((t, int(m.group(2))))
        dentro = [n for t, n in marcas if t >= a]
        antes = [n for t, n in marcas if t < a]
        if dentro:
            base = antes[-1] if antes and antes[-1] <= dentro[-1] else min(dentro)
            out["tiradas_30min"] = dentro[-1] - base
        else:
            out["tiradas_30min"] = 0
    except OSError:
        pass
    return out


def vivo_aguanta(v: dict) -> tuple[bool, str]:
    """¿Se puede seguir con la cuota? (no, con el motivo, si lo importante se retrasa o la cola tira de más)."""
    if v.get("error"):
        return False, f"no se puede leer el triaje en vivo ({v['error']})"
    if v.get("lat_p90_s") is not None and v["lat_p90_s"] > VIGILA["p90_max_s"]:
        return False, f"las T0 y T1 tardan {v['lat_p90_s']:.0f} s en triarse (p90 > {VIGILA['p90_max_s']} s)"
    if v.get("sin_triar", 0) >= VIGILA["sin_triar_max"]:
        return False, f"{v['sin_triar']} noticias T0 o T1 llevan más de {VIGILA['sin_triar_tras_min']} min sin triar"
    if (v.get("tiradas_30min") or 0) > VIGILA["tiradas_max_30min"]:
        return False, f"la cola en vivo ha tirado {v['tiradas_30min']} noticias en 30 min"
    return True, ""


def cuota_ahora(cuota: float, base: Path) -> float:
    """La cuota que toca ahora: la pedida, o 0 (solo huecos) durante HUECOS_S tras una vigilancia mala."""
    if not cuota:
        return 0.0
    t = time.time()
    if t - _VIGILA["ultima"] >= VIGILA_S:
        _VIGILA["ultima"] = t
        v = vigilar_vivo()
        ok, why = vivo_aguanta(v)
        v.update({"ok": ok, "motivo": why, "cuota": cuota})
        try:
            escribir_atomico(base / "vigilancia_vivo.json", json.dumps(v, ensure_ascii=False).encode())
        except OSError:
            pass
        if not ok and t >= _VIGILA["huecos_hasta"]:
            log.info("Triaje: vuelvo a los huecos durante %d min: %s", HUECOS_S // 60, why)
        if not ok:
            _VIGILA["huecos_hasta"] = t + HUECOS_S
    return 0.0 if t < _VIGILA["huecos_hasta"] else cuota


def _triar_una_vez(base: Path, max_n: int | None, horas: float | None, idle_s: float, url: str | None,
                   cuota: float = 0.0) -> dict:
    """Una pasada por lo pendiente. cuota = 0: solo en los huecos (idle_s libre). cuota > 0 (lo decide el usuario):
    pide sin esperar al hueco (la petición espera en la cola del servidor detrás de la que esté en curso) y luego
    descansa lo justo para no usar más de esa fracción del tiempo; el triaje en vivo, con su cola por prioridad, pierde
    lo menos prioritario de su cola en esa misma proporción."""
    url = url or f"http://127.0.0.1:{config.LOCAL_AI_PORT}"
    cuota = max(0.0, min(CUOTA_MAX, float(cuota or 0.0)))
    fv = fuentes_vivas_por_medio()
    pend = pendientes_triaje(base)
    (base / "triaje").mkdir(parents=True, exist_ok=True)
    parar = base / "parar_triaje"
    t0, n, fallos, esperas = time.time(), 0, 0, 0.0
    log.info("Triaje: %d historias pendientes", len(pend))
    for mes, h in pend:
        if (max_n and n >= max_n) or (horas and time.time() - t0 > horas * 3600) or parar.exists():
            break
        libre_desde = None
        sin_tiempo = False
        q = cuota_ahora(cuota, base)   # 0 si el triaje en vivo no aguanta: solo huecos
        while not q:   # espera al hueco libre (con cuota no espera: ya descansó tras la anterior)
            if parar.exists():
                break
            if horas and time.time() - t0 > horas * 3600:
                sin_tiempo = True
                break
            ok = _hueco_libre(url) if not config.GPU_LOCK.exists() else False
            now = time.monotonic()
            if ok:
                libre_desde = libre_desde or now
                if now - libre_desde >= idle_s:
                    break
            else:
                libre_desde = None
                if ok is None:
                    time.sleep(10)   # apagado o cargando: no se arranca nada desde aquí
            time.sleep(TRIAJE_SONDEO_S)
            esperas += TRIAJE_SONDEO_S
        while q and (config.GPU_LOCK.exists() or _hueco_libre(url) is None):
            if parar.exists() or (horas and time.time() - t0 > horas * 3600):   # la fábrica tiene la GPU o el
                sin_tiempo = True                                                # servidor no está: no se pide nada
                break
            time.sleep(10)
        if parar.exists() or sin_tiempo:
            break
        med = medio_corto(h["dom"])
        name, tier, region = fv.get(med, (med, "T2", None))
        body = cuerpo_triaje(h["tit"], name, tier, h.get("mon") or [])
        a = time.perf_counter()
        try:
            req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8"))
            tr = leer_triaje(data, h["tit"], h.get("mon") or [], region)
        except Exception as e:
            fallos += 1
            log.warning("Triaje de %s falló: %s", h["id"], str(e)[:120])
            if fallos > 50 and fallos > n:
                return {"hechas": n, "fallos": fallos, "pendientes": len(pend) - n, "roto": True}
            continue
        tr["ms"] = int((time.perf_counter() - a) * 1000)
        tr["model"] = "Qwen3.5 9B (local)"
        fila = {"id": h["id"], "uso": seleccion(h), "src": name, "tier": tier, "region": region,
                "ts": int(time.time() * 1000), "triage": tr}
        with open(base / "triaje" / f"{mes}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(fila, ensure_ascii=False) + "\n")
        n += 1
        if n % 50 == 0:
            log.info("Triaje: %d hechas (%.1f h, %d fallos, quedan %d)", n, (time.time() - t0) / 3600, fallos,
                     len(pend) - n)
        if q:   # descanso: la petición (con su espera en la cola) cuenta entera, así que se usa algo menos
            fin = time.time() + tr["ms"] / 1000 * (1 - q) / q
            while time.time() < fin and not parar.exists():
                time.sleep(min(1.0, max(0.0, fin - time.time())))
    return {"hechas": n, "fallos": fallos, "pendientes": len(pend) - n, "horas": (time.time() - t0) / 3600}


# ---------------------------------------------------------------- lo que lee el laboratorio

def historias_seleccionadas(base: Path = BASE) -> list[dict]:
    """Las historias en racimo (seleccion ≠ None), de todos los meses construidos, por su t de uso."""
    out = []
    for p in sorted((base / "historias").glob("*.jsonl.gz")):
        for h in leer_jsonl_gz(p):
            u = seleccion(h)
            if u is not None:
                out.append({**h, "uso": u})
    out.sort(key=lambda h: (h["uso"], h["id"]))
    return out


def compilar_lab(base: Path = BASE, exigir_completo: bool = True) -> dict:
    """noticias_historia/lab/noticias.npz y meta.json (bot/lab_vivo/noticias.py): las historias seleccionadas con su
    triaje. Con exigir_completo no escribe nada si falta alguna por triar (el laboratorio mediría otra cosa cada vez)."""
    from . import local_ai
    from .lab_vivo import noticias as nl
    hs = historias_seleccionadas(base)
    tr = triadas(base)
    faltan = [h["id"] for h in hs if h["id"] not in tr]
    res = {"seleccionadas": len(hs), "triadas": len(hs) - len(faltan), "faltan": len(faltan)}
    if faltan and exigir_completo:
        return {**res, "ok": False, "motivo": "faltan historias por triar: no se escribe el archivo del laboratorio"}
    medios = sorted({medio_corto(h["dom"]) for h in hs})
    mi = {m: i for i, m in enumerate(medios)}
    filas, por_mes = [], Counter()
    for h in hs:
        x = tr.get(h["id"])
        if x is None:
            continue
        t = x["triage"]
        rep = t.get("repercusion") or {}
        bits = 0
        for c in t.get("monedas") or []:
            if c in config.COINS:
                bits |= 1 << config.COINS.index(c)
        filas.append({"t": h["uso"], "t0": h["t0"], "n30": h["n30"], "nmed": h["n_medios"],
                      "clase": nl.CLASES.index(h["clase"]), "mon": bits, "dir": int(rep.get("direccion") or 0),
                      "imp": int(t.get("importancia") or 0),
                      "tipo": local_ai.TIPOS.index(t["tipo"]) if t.get("tipo") in local_ai.TIPOS else len(local_ai.TIPOS) - 1,
                      "cred": int(t.get("credibilidad") or 0), "nov": int(t.get("novedad") or 0),
                      "mag": int(rep.get("magnitud") or 0), "fuente": nl.FUENTES.index(h["fuente"]),
                      "medio": mi[medio_corto(h["dom"])]})
        por_mes[datetime.fromtimestamp(h["uso"] / 1000, timezone.utc).strftime("%Y-%m")] += 1
    n = nl.Noticias.de_filas(filas, local_ai.TIPOS, medios)
    sha = hashlib.sha1()
    for k in nl.COLUMNAS:
        sha.update(np.ascontiguousarray(getattr(n, k)).tobytes())
    sha.update(json.dumps({"racimo": RACIMO_N, "min": RACIMO_MIN, "tipos": list(local_ai.TIPOS),
                           "medios": medios}).encode())
    meta = {"formato": 1, "firma": sha.hexdigest()[:12], "n": len(n), "por_mes": dict(sorted(por_mes.items())),
            "desde": int(n.t[0]) if len(n) else None, "hasta": int(n.t[-1]) if len(n) else None,
            "seleccion": {"racimo_n": RACIMO_N, "racimo_min": RACIMO_MIN, "retraso_triaje_ms": TRIAJE_RETRASO_MS,
                          "texto": "historias (deduplicadas como la mesa) que publican ≥ n medios distintos en 30 min; "
                                   "t = sello del medio n + 1 min"},
            "faltan": len(faltan), "creado": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    nl.guardar(n, base / "lab", meta)
    return {**res, "ok": True, "firma": meta["firma"], "n": len(n)}


# ---------------------------------------------------------------- seguir (lo encadena todo, solo)

def meses_completos(desde: str = DESDE, hasta: str = HASTA, base: Path = BASE) -> list[str]:
    """Los meses del periodo con todos sus días de GDELT hechos (los que GDELT tiene) y los 24 medios hechos."""
    lista = lista_gdelt(desde, hasta, base)
    con_archivos = {f"{f[0][:4]}-{f[0][4:6]}-{f[0][6:8]}" for f in lista}
    car = base / "gdelt" / "dias"
    out = []
    for mes in meses_entre(desde, hasta):
        dias = [d for d in dias_entre(desde, hasta) if d.startswith(mes)]
        ok_g = all(_dia_hecho(car, d) for d in dias if d in con_archivos)
        ok_w = all((base / "wp" / medio_de(s) / f"{mes}.json").exists() for s in WP)
        if ok_g and ok_w:
            out.append(mes)
    return out


def _bloque_final(completos: list[str], meses: list[str]) -> str | None:
    """El primer mes del tramo de meses completos que llega hasta el último del periodo (None si el último falta)."""
    k = len(meses) - 1
    if k < 0 or meses[k] not in completos:
        return None
    while k > 0 and meses[k - 1] in completos:
        k -= 1
    return meses[k]


def _tomar_lock(p: Path) -> bool:
    """Un solo «seguir» a la vez: el lock guarda el PID; uno cuyo proceso ya no vive se reemplaza."""
    if p.exists():
        try:
            pid = int(json.loads(p.read_text("utf-8"))["pid"])
            from .lab_vivo import recursos
            if recursos.pid_vivo(pid) and pid != os.getpid():
                return False
        except (OSError, ValueError, KeyError, ImportError):
            pass
    escribir_atomico(p, json.dumps({"pid": os.getpid(), "ts": int(time.time() * 1000)}).encode())
    return True


def seguir(desde: str = DESDE, hasta: str = HASTA, base: Path = BASE, esperar_min: float = 15.0,
           parada_h: float = 1.5, cuota: float = 0.0) -> dict:
    """Hasta acabar: (1) construye las historias del tramo de meses completos que llega hasta «hasta» (de lo más
    nuevo hacia atrás, en una sola pasada cronológica cada vez que el tramo crece); (2) tría lo seleccionado en los
    huecos de la GPU (por turnos de esperar_min); (3) si la descarga de GDELT o de WordPress lleva parada_h horas sin
    avanzar, la relanza aquí; (4) con todo bajado, construido y triado, compila el archivo del laboratorio y escribe el
    informe. «parar» o «parar_triaje» en la carpeta lo paran."""
    lock = base / "seguir.lock"
    if not _tomar_lock(lock):
        log.info("Seguir: ya hay otro en marcha (seguir.lock)")
        return {"estado": "ya_en_marcha"}
    prioridad_baja()
    meses = meses_entre(desde, hasta)
    construido_desde, ultimo_avance, ultimos_hechos = None, time.time(), -1
    try:
        while not ((base / "parar").exists() or (base / "parar_triaje").exists()):
            completos = meses_completos(desde, hasta, base)
            hechos = sum(1 for _ in (base / "gdelt" / "dias").glob("*.json")) + \
                sum(1 for _ in (base / "wp").glob("*/*.json"))
            if hechos != ultimos_hechos:
                ultimos_hechos, ultimo_avance = hechos, time.time()
            elif len(completos) < len(meses) and time.time() - ultimo_avance > parada_h * 3600:
                log.info("Seguir: la descarga lleva %.1f h sin avanzar: la relanzo aquí", parada_h)
                descargar_wp(desde, hasta, base)
                descargar_gdelt(desde, hasta, base=base)
                ultimo_avance = time.time()
                continue
            ini = _bloque_final(completos, meses)
            if ini is not None and ini != construido_desde:
                log.info("Seguir: historias de %s a %s", ini, meses[-1])
                construir_historias(f"{ini}-01", hasta, base)
                construido_desde = ini
            r = _triar_una_vez(base, None, esperar_min / 60, TRIAJE_IDLE_S, None, cuota)
            todo = construido_desde == meses[0] and len(completos) == len(meses)
            if todo and r["pendientes"] == 0 and not r.get("roto"):
                lab = compilar_lab(base)
                from . import noticias_informe
                inf = noticias_informe.informe(base, desde, hasta)
                escribir_atomico(base / "informe.json", json.dumps(inf, ensure_ascii=False, indent=1).encode())
                escribir_atomico(base / "informe.md", noticias_informe.a_markdown(inf).encode())
                res = {"estado": "hecho", "lab": lab, "hecho": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                escribir_atomico(base / "seguir_hecho.json", json.dumps(res, ensure_ascii=False, indent=1).encode())
                log.info("Seguir: todo hecho: %s", json.dumps(res, ensure_ascii=False))
                return res
            if r["hechas"] == 0:
                fin = time.time() + esperar_min * 60
                while time.time() < fin and not (base / "parar").exists() and not (base / "parar_triaje").exists():
                    time.sleep(5)
        return {"estado": "parado"}
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- órdenes

def _registro(base: Path, nombre: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(base / nombre, encoding="utf-8"), logging.StreamHandler()])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.noticias_historia")
    sub = ap.add_subparsers(dest="orden", required=True)
    g = sub.add_parser("gdelt", help="baja y filtra los GKG de GDELT")
    g.add_argument("--desde", default=DESDE)
    g.add_argument("--hasta", default=HASTA)
    g.add_argument("--procesos", type=int, default=2)
    g.add_argument("--hilos", type=int, default=4)
    g.add_argument("--mbs", type=float, default=10.0, help="tope de bajada en MB/s entre todos")
    w = sub.add_parser("wp", help="baja y filtra los 24 medios de cripto (API de WordPress)")
    w.add_argument("--desde", default=DESDE)
    w.add_argument("--hasta", default=HASTA)
    w.add_argument("--sitios", default="", help="solo estos (separados por comas)")
    hs = sub.add_parser("historias", help="deduplica por historia (como la mesa) los meses completos")
    hs.add_argument("--desde", default=DESDE)
    hs.add_argument("--hasta", default=HASTA)
    t = sub.add_parser("triaje", help="tría las historias seleccionadas con la IA local, en sus huecos")
    t.add_argument("--max", type=int, default=None)
    t.add_argument("--horas", type=float, default=None)
    t.add_argument("--idle", type=float, default=TRIAJE_IDLE_S, help="segundos de hueco libre antes de pedir")
    t.add_argument("--esperar", type=float, default=None, help="al acabar, vuelve a mirar cada tantos minutos")
    t.add_argument("--cuota", type=float, default=0.0, help="fracción de la GPU (0: solo los huecos; lo decide el usuario)")
    lb = sub.add_parser("lab", help="compila el archivo que lee el laboratorio (solo con todo triado)")
    lb.add_argument("--aunque-falten", action="store_true", help="escribe aunque falten historias por triar")
    sg = sub.add_parser("seguir", help="encadena lo que falta: historias, triaje, lab e informe (y relanza descargas)")
    sg.add_argument("--esperar", type=float, default=15.0, help="minutos de cada turno de triaje")
    sg.add_argument("--cuota", type=float, default=0.0, help="fracción de la GPU (0: solo los huecos; lo decide el usuario)")
    a = ap.parse_args(argv)
    if a.orden == "seguir":
        _registro(BASE, "seguir.log")
        r = seguir(esperar_min=a.esperar, cuota=a.cuota)
        log.info("Seguir: %s", json.dumps(r, ensure_ascii=False))
        return 0
    if a.orden == "lab":
        _registro(BASE, "lab.log")
        r = compilar_lab(exigir_completo=not a.aunque_falten)
        log.info("Laboratorio: %s", json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    if a.orden == "historias":
        _registro(BASE, "historias.log")
        r = construir_historias(a.desde, a.hasta)
        log.info("Historias: %s", json.dumps(r, ensure_ascii=False))
        return 0
    if a.orden == "triaje":
        _registro(BASE, "triaje.log")
        prioridad_baja()
        r = triar(max_n=a.max, horas=a.horas, idle_s=a.idle, esperar_min=a.esperar, cuota=a.cuota)
        log.info("Triaje terminado: %s", json.dumps(r))
        return 0
    if a.orden == "gdelt":
        _registro(BASE, "gdelt.log")
        r = descargar_gdelt(a.desde, a.hasta, a.procesos, a.hilos, a.mbs)
        log.info("GDELT terminado: %s", json.dumps({k: v for k, v in r.items() if k != "errores"}))
        return 0 if not r["errores"] else 1
    if a.orden == "wp":
        _registro(BASE, "wp.log")
        r = descargar_wp(a.desde, a.hasta, sitios=[s for s in a.sitios.split(",") if s] or None)
        log.info("WP terminado: %s", json.dumps(r))
        return 0 if not r["fallidos"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
