"""Datos del laboratorio vivo (docs/07, 4 y 6.2): el Mercado que ve una familia y la caché por época.

- Velas de 1 min en bruto (solo los minutos con vela, como A[c] de medir_selectiva.py): las lee el libro, para
  reproducir las cifras verificadas. Se cargan con backtest_aperturas.load_series (zip mensual si existe; si no, la
  parte de la API que mantiene al día la vigilancia del bot), que guarda también la compra del tomador (columna 9).
- Rejilla densa de 1 min (como laboratorio.dense_from_rows): los minutos sin vela repiten el último cierre con volumen 0
  y valid = False. Las familias usan la densa y sus barras agregadas (alineadas a UTC y solo las CERRADAS).
- Funding real de Binance (laboratorio.load_funding), miedo y codicia, cierres de las bolsas (backtest_aperturas.
  index_data, conocidos 15 min después del cierre: backtest.CLOSE_MARGIN_MS), calendario macro y aperturas reales
  (backtest_aperturas.build_openings con LAB_WINDOWS y sin las sesiones especiales).

Sin mirar al futuro: Mercado.recortado(T) es la misma vista sin nada posterior a T (velas que cierran después de T,
barras sin cerrar, funding, miedo y codicia y cierres de bolsa publicados después). Deja el calendario de aperturas y
el de datos macro (sus horas se saben de antemano). La prueba del recorte de cada familia se apoya en esto.

Caché: lab_vivo\\cache\\<época>-<firma>\\<MONEDA>\\*.npy (float64 / int64), que las tareas abren con mmap_mode="r": varios
subprocesos comparten las páginas del sistema y no copian nada."""
import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .. import config
from . import ajustes

log = logging.getLogger("lab_vivo.datos")

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
MAD = ajustes.MAD
TF_CACHE = (5, 15, 60, 240, 1440)   # barras guardadas en la caché (las demás se agregan al pedirlas)
FORMATO_CACHE = 1
COLS = ("o", "h", "l", "c", "v", "tb")
SYM = {c: f"{c}USDT" for c in config.COINS}


def dia_mad(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).date().isoformat()


def medianoche_mad(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=MAD).timestamp() * 1000)


def _mes_mas(y: int, m: int, k: int) -> tuple[int, int]:
    z = y * 12 + (m - 1) + k
    return z // 12, z % 12 + 1


# ---------------------------------------------------------------- época y periodo (4.2)

def epoca_de(ms: int | None = None) -> str:
    """Época = el mes en curso (de Madrid)."""
    d = datetime.fromtimestamp((ms if ms is not None else ajustes.ahora_ms()) / 1000, MAD).date()
    return f"{d.year:04d}-{d.month:02d}"


def periodo_epoca(epoca: str) -> dict:
    """Periodo de una época: los 3 años hasta el último día del mes anterior (regla de los 3 años como mucho). Corte
    fijo el 16-08-2025. Todo en ms de Madrid; fin es exclusivo (medianoche del día 1 del mes de la época)."""
    y, m = int(epoca[:4]), int(epoca[5:7])
    y0, m0 = _mes_mas(y, m, -12 * ajustes.ANOS)
    ini = medianoche_mad(date(y0, m0, 1))
    fin = medianoche_mad(date(y, m, 1))
    return {"epoca": epoca, "inicio": ini, "fin": fin, "corte": ajustes.CORTE_MS,
            "desde": date(y0, m0, 1).isoformat(), "hasta": (date(y, m, 1) - timedelta(days=1)).isoformat(),
            "corte_dia": ajustes.CORTE_DIA}


def meses_entre(desde_ms: int, hasta_ms: int) -> list[str]:
    a = datetime.fromtimestamp(desde_ms / 1000, timezone.utc).date()
    b = datetime.fromtimestamp((hasta_ms - 1) / 1000, timezone.utc).date()
    y, m, out = a.year, a.month, []
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = _mes_mas(y, m, 1)
    return out


# ---------------------------------------------------------------- archivos de velas

def dir_1m() -> Path:
    return config.HIST_DIR / "1m"


def archivos_moneda(moneda: str, meses: list[str]) -> list[Path]:
    """El zip mensual de Binance manda sobre la parte de la API (como laboratorio.month_files y medir_selectiva.py)."""
    out = []
    for ym in meses:
        z = dir_1m() / f"{SYM[moneda]}-1m-{ym}.zip"
        p = dir_1m() / f"{SYM[moneda]}-1m-{ym}-api.json"
        if z.exists():
            out.append(z)
        elif p.exists():
            out.append(p)
    return out


def firma_archivos(paths) -> str:
    h = hashlib.sha1()
    for p in sorted(Path(x) for x in paths):
        try:
            st = p.stat()
            h.update(f"{p.name}|{st.st_size}|{int(st.st_mtime)}\n".encode())
        except OSError:
            h.update(f"{p.name}|falta\n".encode())
    return h.hexdigest()[:12]


# ---------------------------------------------------------------- estructuras

@dataclass
class Velas:
    """Velas de 1 min en bruto (solo los minutos con vela), ordenadas: t (apertura, ms) y o, h, l, c, v, tb."""
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    tb: np.ndarray

    def __len__(self) -> int:
        return len(self.t)

    def tramo(self, desde: int | None, hasta: int | None) -> "Velas":
        """Velas que abren en [desde, …) y ya cerradas en hasta (t + 1 min ≤ hasta)."""
        i = 0 if desde is None else int(np.searchsorted(self.t, desde, "left"))
        j = len(self.t) if hasta is None else int(np.searchsorted(self.t, hasta - MIN, "right"))
        j = max(i, j)
        return Velas(*(getattr(self, k)[i:j] for k in ("t", "o", "h", "l", "c", "v", "tb")))


@dataclass
class Denso:
    """Rejilla de 1 min sin huecos desde t0: los minutos sin vela repiten el último cierre (volumen 0, valid False)."""
    t0: int
    n: int
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    tb: np.ndarray
    valid: np.ndarray

    @property
    def t(self) -> np.ndarray:
        return self.t0 + np.arange(self.n, dtype=np.int64) * MIN

    def tramo(self, desde: int | None, hasta: int | None) -> "Denso":
        i0 = 0 if desde is None else max(0, min(self.n, -(-(desde - self.t0) // MIN)))
        i1 = self.n if hasta is None else max(i0, min(self.n, (hasta - self.t0) // MIN))
        return Denso(self.t0 + i0 * MIN, i1 - i0, *(getattr(self, k)[i0:i1] for k in (*COLS, "valid")))


@dataclass
class Barras:
    """Barras de tf_min minutos alineadas a UTC y CERRADAS: t (apertura), t_cierre (= t + tf: el instante en que se
    conocen), o, h, l, c, v, tb y n_validas (minutos con vela)."""
    tf: int
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    tb: np.ndarray
    n_validas: np.ndarray

    @property
    def t_cierre(self) -> np.ndarray:
        return self.t + self.tf * MIN

    def __len__(self) -> int:
        return len(self.t)

    def tramo(self, desde: int | None, hasta: int | None) -> "Barras":
        """Barras que abren en [desde, …) y cerradas en hasta (t_cierre ≤ hasta)."""
        i = 0 if desde is None else int(np.searchsorted(self.t, desde, "left"))
        j = len(self.t) if hasta is None else int(np.searchsorted(self.t, hasta - self.tf * MIN, "right"))
        j = max(i, j)
        return Barras(self.tf, *(getattr(self, k)[i:j] for k in ("t", *COLS, "n_validas")))

    def cerrada_en(self, t_ms) -> np.ndarray | int:
        """Índice de la última barra cerrada en t_ms (t_cierre ≤ t_ms), −1 si ninguna."""
        return np.searchsorted(self.t + self.tf * MIN, t_ms, "right") - 1


def _vacias(tf: int) -> Barras:
    z = np.zeros(0)
    return Barras(tf, np.zeros(0, np.int64), z, z, z, z, z, z, np.zeros(0, np.int32))


def denso_de(v: Velas) -> Denso | None:
    """Velas en bruto → rejilla densa (laboratorio.dense_from_rows, con la compra del tomador)."""
    if not len(v):
        return None
    t = (np.asarray(v.t, np.int64) // MIN) * MIN
    keep = np.concatenate(([True], np.diff(t) > 0))
    t = t[keep]
    t0 = int(t[0])
    n = int((t[-1] - t0) // MIN) + 1
    k = (t - t0) // MIN
    valid = np.zeros(n, bool)
    valid[k] = True
    last = np.maximum.accumulate(np.where(valid, np.arange(n), 0))
    c = np.zeros(n)
    c[k] = np.asarray(v.c)[keep]
    c = c[last]
    o, h, l = c.copy(), c.copy(), c.copy()
    o[k], h[k], l[k] = np.asarray(v.o)[keep], np.asarray(v.h)[keep], np.asarray(v.l)[keep]
    vv, tb = np.zeros(n), np.zeros(n)
    vv[k], tb[k] = np.asarray(v.v)[keep], np.asarray(v.tb)[keep]
    return Denso(t0, n, o, h, l, c, vv, tb, valid)


def agregar(den: Denso | None, tf: int) -> Barras:
    """Barras de tf minutos desde la rejilla densa, alineadas a múltiplos de tf en UTC; solo las completas."""
    if den is None or den.n == 0:
        return _vacias(tf)
    if tf == 1:
        return Barras(1, den.t, den.o, den.h, den.l, den.c, den.v, den.tb, np.asarray(den.valid, np.int32))
    tfm = tf * MIN
    a = -(-den.t0 // tfm) * tfm
    i0 = (a - den.t0) // MIN
    nb = (den.n - i0) // tf if den.n > i0 else 0
    if nb <= 0:
        return _vacias(tf)
    s = slice(int(i0), int(i0 + nb * tf))
    sh = (nb, tf)
    return Barras(tf, a + np.arange(nb, dtype=np.int64) * tfm,
                  np.ascontiguousarray(np.asarray(den.o[s]).reshape(sh)[:, 0]),
                  np.asarray(den.h[s]).reshape(sh).max(1), np.asarray(den.l[s]).reshape(sh).min(1),
                  np.ascontiguousarray(np.asarray(den.c[s]).reshape(sh)[:, -1]),
                  np.asarray(den.v[s]).reshape(sh).sum(1), np.asarray(den.tb[s]).reshape(sh).sum(1),
                  np.asarray(den.valid[s]).reshape(sh).sum(1).astype(np.int32))


def cargar_velas(paths: list[Path], desde_ms: int | None = None, hasta_ms: int | None = None) -> Velas:
    """Velas de los archivos con backtest_aperturas.load_series (la misma lectura que medir_selectiva.py)."""
    from .. import backtest_aperturas as bt
    s = bt.load_series([p for p in paths if Path(p).exists()])
    t = np.frombuffer(s.t, dtype=np.int64).copy() if len(s) else np.zeros(0, np.int64)
    cols = [np.frombuffer(s.cols[k]).copy() if len(s) else np.zeros(0) for k in (0, 1, 2, 3, 4, 6)]
    del s
    v = Velas(t, *cols)
    return v.tramo(desde_ms, hasta_ms) if (desde_ms is not None or hasta_ms is not None) else v


# ---------------------------------------------------------------- fuentes no de velas

def cargar_funding(moneda: str) -> tuple[np.ndarray, np.ndarray]:
    from .. import laboratorio as lab
    a = lab.load_funding(moneda)
    if a is None or not len(a):
        return np.zeros(0, np.int64), np.zeros(0)
    return a[:, 0].astype(np.int64), a[:, 1].astype(float)


def cargar_fng() -> tuple[np.ndarray, np.ndarray]:
    """Miedo y codicia: el del día D se conoce a las 00:00 UTC de D + 1 h (historia_extra.fng_known_at)."""
    from .. import laboratorio as lab
    f = lab.load_fng() or {}
    ks = sorted(f)
    t = np.array([int(datetime.fromisoformat(k).replace(tzinfo=timezone.utc).timestamp() * 1000) + H for k in ks],
                 np.int64)
    return t, np.array([f[k] for k in ks], float)


def cargar_bolsas() -> dict:
    """Cierres diarios de las bolsas: {bolsa: (conocido_ms, día, cierre, retorno %)}; un cierre se conoce 15 min
    después (backtest.CLOSE_MARGIN_MS)."""
    from .. import backtest, regions
    from .. import backtest_aperturas as bt
    out = {}
    try:
        idx = bt.index_data()
    except Exception as e:   # sin índices: las familias que los piden no tienen datos
        log.warning("Sin cierres de bolsa: %s", e)
        return out
    for k, x in idx.items():
        dias = sorted(x["close"])
        kn, cl, rt, ds = [], [], [], []
        m = regions.MARKETS[k]
        for d in dias:
            c_, p_ = x["close"][d]
            if not p_:
                continue
            kn.append(int(regions.close_utc_on(m, date.fromisoformat(d)).timestamp() * 1000) + backtest.CLOSE_MARGIN_MS)
            ds.append(d)
            cl.append(float(c_))
            rt.append((float(c_) / float(p_) - 1) * 100)
        o = np.argsort(np.array(kn, np.int64), kind="stable")
        out[k] = (np.array(kn, np.int64)[o], [ds[i] for i in o], np.array(cl)[o], np.array(rt)[o])
    return out


def cargar_macro() -> tuple[np.ndarray, list[str]]:
    from .. import laboratorio as lab
    ts, names = lab.load_macro(with_names=True)
    if ts is None:
        return np.zeros(0, np.int64), []
    return np.asarray(ts, np.int64), [names.get(int(t), "?") for t in ts]


def cargar_aperturas(desde_ms: int, hasta_ms: int, idx: dict | None = None) -> list[dict]:
    """Aperturas reales de LAB_WINDOWS (festivos del histórico de índices) sin las sesiones especiales. idx = {} usa
    solo el calendario (sin festivos): lo que hacen las pruebas, que no leen disco."""
    from .. import apertura as ap
    from .. import backtest_aperturas as bt
    if idx is None:
        try:
            idx = bt.index_data()
        except Exception:
            idx = {}
    a = datetime.fromtimestamp(desde_ms / 1000, timezone.utc).date() - timedelta(days=1)
    b = datetime.fromtimestamp(hasta_ms / 1000, timezone.utc).date() + timedelta(days=1)
    ops = [o for o in bt.build_openings(a, b, idx) if o["id"] in ap.LAB_WINDOWS and not ap.special_session(o)]
    ops = [o for o in ops if desde_ms <= o["open_ms"] < hasta_ms]
    return sorted(ops, key=lambda o: (o["open_ms"], o["id"]))


# ---------------------------------------------------------------- la fuente (todo lo cargado) y la vista (Mercado)

@dataclass
class Fuente:
    monedas: tuple
    periodo: dict
    velas: dict                       # moneda → Velas
    densos: dict                      # moneda → Denso
    barras: dict                      # (moneda, tf) → Barras
    funding: dict                     # moneda → (t_ms, tasa en fracción)
    fng: tuple
    bolsas: dict
    macro: tuple
    aperturas: list
    firma: str = ""
    carpeta: Path | None = None
    meta: dict = field(default_factory=dict)

    def barras_de(self, moneda: str, tf: int) -> Barras:
        k = (moneda, int(tf))
        if k not in self.barras:
            self.barras[k] = agregar(self.densos.get(moneda), int(tf))
        return self.barras[k]


def construir_fuente(desde_ms: int, hasta_ms: int, periodo: dict, monedas=None, carpeta: Path | None = None,
                     aperturas: bool = True, tf_cache=TF_CACHE) -> Fuente:
    """Carga velas (moneda a moneda, para no tener las cuatro en memoria a la vez si hay carpeta), funding, miedo y
    codicia, bolsas, macro y aperturas de [desde_ms, hasta_ms). Con carpeta, lo guarda en .npy y lo vuelve a abrir con
    mmap."""
    monedas = tuple(monedas or config.COINS)
    meses = meses_entre(desde_ms, hasta_ms)
    usados = {c: archivos_moneda(c, meses) for c in monedas}
    firma = firma_archivos([p for v in usados.values() for p in v])
    if carpeta is not None:
        tmp = Path(str(carpeta) + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
    velas, densos, barras = {}, {}, {}
    for c in monedas:
        v = cargar_velas(usados[c], desde_ms, hasta_ms)
        d = denso_de(v)
        if carpeta is not None:
            sub = tmp / c
            sub.mkdir(parents=True, exist_ok=True)
            for k in ("t", *COLS):
                np.save(sub / f"raw_{k}.npy", np.ascontiguousarray(getattr(v, k)))
            if d is not None:
                for k in (*COLS, "valid"):
                    np.save(sub / f"den_{k}.npy", np.ascontiguousarray(getattr(d, k)))
                for tf in tf_cache:
                    b = agregar(d, tf)
                    for k in ("t", *COLS, "n_validas"):
                        np.save(sub / f"b{tf}_{k}.npy", np.ascontiguousarray(getattr(b, k)))
            (sub / "meta.json").write_text(json.dumps({"t0": d.t0 if d else None, "n": d.n if d else 0,
                                                       "velas": len(v)}), encoding="utf-8")
            del v, d
        else:
            velas[c], densos[c] = v, d
    fund = {c: cargar_funding(c) for c in monedas}
    fng, bol, mac = cargar_fng(), cargar_bolsas(), cargar_macro()
    aps = cargar_aperturas(desde_ms, hasta_ms) if aperturas else []
    meta = {"formato": FORMATO_CACHE, "desde": desde_ms, "hasta": hasta_ms, "periodo": periodo, "firma": firma,
            "monedas": list(monedas), "archivos": {c: [p.name for p in usados[c]] for c in monedas},
            "creado": ajustes.ahora_ms(), "tf_cache": list(tf_cache)}
    if carpeta is not None:
        np.save(tmp / "aux_fng_t.npy", fng[0])
        np.save(tmp / "aux_fng_v.npy", fng[1])
        np.save(tmp / "aux_macro_t.npy", mac[0])
        for c in monedas:
            np.save(tmp / f"aux_fund_{c}_t.npy", fund[c][0])
            np.save(tmp / f"aux_fund_{c}_r.npy", fund[c][1])
        (tmp / "extra.json").write_text(json.dumps({
            "macro_nombres": mac[1], "aperturas": aps,
            "bolsas": {k: {"t": v[0].tolist(), "dia": v[1], "cierre": v[2].tolist(), "ret": v[3].tolist()}
                       for k, v in bol.items()}}, ensure_ascii=False), encoding="utf-8")
        (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if Path(carpeta).exists():
            shutil.rmtree(carpeta, ignore_errors=True)
        os.replace(tmp, carpeta)
        return abrir_fuente(Path(carpeta))
    return Fuente(monedas, periodo, velas, densos, barras, fund, fng, bol, mac, aps, firma, None, meta)


def guardar_fuente(fu: Fuente, carpeta: Path, tf_cache=TF_CACHE) -> Fuente:
    """Guarda una Fuente en memoria con el mismo formato de la caché (las pruebas lo usan con datos sintéticos)."""
    carpeta = Path(carpeta)
    tmp = Path(str(carpeta) + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    for c in fu.monedas:
        sub = tmp / c
        sub.mkdir(parents=True, exist_ok=True)
        v, d = fu.velas[c], fu.densos.get(c)
        for k in ("t", *COLS):
            np.save(sub / f"raw_{k}.npy", np.ascontiguousarray(getattr(v, k)))
        if d is not None:
            for k in (*COLS, "valid"):
                np.save(sub / f"den_{k}.npy", np.ascontiguousarray(getattr(d, k)))
            for tf in tf_cache:
                b = agregar(d, tf)
                for k in ("t", *COLS, "n_validas"):
                    np.save(sub / f"b{tf}_{k}.npy", np.ascontiguousarray(getattr(b, k)))
        (sub / "meta.json").write_text(json.dumps({"t0": d.t0 if d else None, "n": d.n if d else 0, "velas": len(v)}),
                                       encoding="utf-8")
    np.save(tmp / "aux_fng_t.npy", fu.fng[0])
    np.save(tmp / "aux_fng_v.npy", fu.fng[1])
    np.save(tmp / "aux_macro_t.npy", np.asarray(fu.macro[0], np.int64))
    for c in fu.monedas:
        np.save(tmp / f"aux_fund_{c}_t.npy", fu.funding[c][0])
        np.save(tmp / f"aux_fund_{c}_r.npy", fu.funding[c][1])
    (tmp / "extra.json").write_text(json.dumps({
        "macro_nombres": list(fu.macro[1]), "aperturas": fu.aperturas,
        "bolsas": {k: {"t": np.asarray(v[0]).tolist(), "dia": list(v[1]), "cierre": np.asarray(v[2]).tolist(),
                       "ret": np.asarray(v[3]).tolist()} for k, v in fu.bolsas.items()}}, ensure_ascii=False),
        encoding="utf-8")
    meta = {"formato": FORMATO_CACHE, "desde": int(min(int(fu.velas[c].t[0]) for c in fu.monedas)),
            "hasta": int(max(int(fu.velas[c].t[-1]) for c in fu.monedas)) + MIN, "periodo": fu.periodo,
            "firma": fu.firma or "sintetica", "monedas": list(fu.monedas), "archivos": {}, "creado": ajustes.ahora_ms(),
            "tf_cache": list(tf_cache)}
    (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if carpeta.exists():
        shutil.rmtree(carpeta, ignore_errors=True)
    os.replace(tmp, carpeta)
    return abrir_fuente(carpeta)


def abrir_fuente(carpeta: Path, mmap: bool = True) -> Fuente:
    """Abre una caché guardada (mmap: varios procesos comparten las páginas y no se copia nada)."""
    carpeta = Path(carpeta)
    meta = json.loads((carpeta / "meta.json").read_text(encoding="utf-8"))
    aux = json.loads((carpeta / "extra.json").read_text(encoding="utf-8"))
    mm = "r" if mmap else None
    ld = lambda p: np.load(p, mmap_mode=mm)   # noqa: E731
    velas, densos, barras, fund = {}, {}, {}, {}
    for c in meta["monedas"]:
        sub = carpeta / c
        velas[c] = Velas(*(ld(sub / f"raw_{k}.npy") for k in ("t", *COLS)))
        m = json.loads((sub / "meta.json").read_text(encoding="utf-8"))
        if m.get("t0") is not None:
            densos[c] = Denso(int(m["t0"]), int(m["n"]), *(ld(sub / f"den_{k}.npy") for k in (*COLS, "valid")))
            for tf in meta.get("tf_cache") or TF_CACHE:
                if (sub / f"b{tf}_t.npy").exists():
                    barras[(c, int(tf))] = Barras(int(tf), *(ld(sub / f"b{tf}_{k}.npy")
                                                             for k in ("t", *COLS, "n_validas")))
        else:
            densos[c] = None
        fund[c] = (np.load(carpeta / f"aux_fund_{c}_t.npy"), np.load(carpeta / f"aux_fund_{c}_r.npy"))
    bol = {k: (np.array(v["t"], np.int64), list(v["dia"]), np.array(v["cierre"], float), np.array(v["ret"], float))
           for k, v in (aux.get("bolsas") or {}).items()}
    return Fuente(tuple(meta["monedas"]), meta["periodo"], velas, densos, barras, fund,
                  (np.load(carpeta / "aux_fng_t.npy"), np.load(carpeta / "aux_fng_v.npy")), bol,
                  (np.load(carpeta / "aux_macro_t.npy"), list(aux.get("macro_nombres") or [])),
                  list(aux.get("aperturas") or []), meta.get("firma", ""), carpeta, meta)


class Mercado:
    """Lo que ve una familia: una vista de la Fuente acotada a [desde, hasta) («recortado» = hasta). Todo lo que
    devuelve se conoce antes de hasta: velas cerradas, barras cerradas, funding liquidado, miedo y codicia y cierres
    de bolsa publicados. El calendario de aperturas y el macro (solo horas) se ven enteros."""

    def __init__(self, fuente: Fuente, hasta: int | None = None, desde: int | None = None):
        self.f = fuente
        self.T = hasta
        self.L = desde
        self.monedas = tuple(fuente.monedas)
        self.periodo = dict(fuente.periodo)
        self._cache: dict = {}

    # vistas
    def recortado(self, t_ms: int) -> "Mercado":
        T = t_ms if self.T is None else min(self.T, t_ms)
        return Mercado(self.f, T, self.L)

    def ventana(self, desde_ms: int | None, hasta_ms: int | None) -> "Mercado":
        L = desde_ms if self.L is None else (max(self.L, desde_ms) if desde_ms is not None else self.L)
        T = hasta_ms if self.T is None else (min(self.T, hasta_ms) if hasta_ms is not None else self.T)
        return Mercado(self.f, T, L)

    @property
    def hasta(self) -> int | None:
        return self.T

    def indice(self, moneda: str) -> int:
        return self.monedas.index(moneda)

    def velas(self, moneda: str) -> Velas:
        k = ("v", moneda)
        if k not in self._cache:
            v = self.f.velas[moneda]
            self._cache[k] = v.tramo(self.L, self.T) if (self.L is not None or self.T is not None) else v
        return self._cache[k]

    def denso(self, moneda: str) -> Denso | None:
        k = ("d", moneda)
        if k not in self._cache:
            d = self.f.densos.get(moneda)
            self._cache[k] = d.tramo(self.L, self.T) if d is not None and (self.L is not None or self.T is not None) else d
        return self._cache[k]

    def barras(self, moneda: str, tf_min: int) -> Barras:
        k = ("b", moneda, int(tf_min))
        if k not in self._cache:
            b = self.f.barras_de(moneda, int(tf_min))
            self._cache[k] = b.tramo(self.L, self.T) if (self.L is not None or self.T is not None) else b
        return self._cache[k]

    def funding(self, moneda: str) -> tuple[np.ndarray, np.ndarray]:
        """(t_ms de la liquidación, tasa por periodo en fracción) liquidados antes de hasta."""
        ft, fr = self.f.funding.get(moneda, (np.zeros(0, np.int64), np.zeros(0)))
        if self.T is None:
            return ft, fr
        j = int(np.searchsorted(ft, self.T, "left"))
        return ft[:j], fr[:j]

    def tasa_en(self, moneda: str, t_ms: int) -> float | None:
        """Última tasa publicada antes de t_ms, en % (como medir_selectiva.tasa_en)."""
        ft, fr = self.funding(moneda)
        j = int(np.searchsorted(ft, t_ms, "left")) - 1
        return float(fr[j] * 100) if j >= 0 else None

    def fng(self) -> tuple[np.ndarray, np.ndarray]:
        t, v = self.f.fng
        if self.T is None:
            return t, v
        j = int(np.searchsorted(t, self.T, "left"))
        return t[:j], v[:j]

    def fng_en(self, t_ms: int) -> float | None:
        t, v = self.fng()
        j = int(np.searchsorted(t, t_ms, "right")) - 1
        return float(v[j]) if j >= 0 else None

    def bolsa(self, clave: str) -> tuple[np.ndarray, list, np.ndarray, np.ndarray]:
        """(conocido_ms, día, cierre, retorno %) de una bolsa, solo lo publicado antes de hasta."""
        x = self.f.bolsas.get(clave)
        if x is None:
            return np.zeros(0, np.int64), [], np.zeros(0), np.zeros(0)
        if self.T is None:
            return x
        j = int(np.searchsorted(x[0], self.T, "left"))
        return x[0][:j], x[1][:j], x[2][:j], x[3][:j]

    def bolsas_conocidas(self, t_ms: int, desde_ms: int | None = None) -> list[tuple]:
        """(bolsa, día, retorno %) de las sesiones cerradas hace ≥ 15 min en t_ms (y desde desde_ms)."""
        out = []
        for k in self.f.bolsas:
            kt, ds, _, rt = self.bolsa(k)
            j = int(np.searchsorted(kt, t_ms, "right"))
            i = 0 if desde_ms is None else int(np.searchsorted(kt, desde_ms, "left"))
            out += [(k, ds[q], float(rt[q])) for q in range(i, j)]
        return out

    @property
    def macro(self) -> tuple[np.ndarray, list[str]]:
        return self.f.macro

    @property
    def aperturas(self) -> list[dict]:
        return self.f.aperturas

    noticias = None   # fase 2 (noticias_vivas)


# ---------------------------------------------------------------- caché de la época

def carpeta_epoca(epoca: str, firma: str) -> Path:
    return ajustes.CACHE / f"{epoca}-{firma}"


def rango_epoca(periodo: dict) -> tuple[int, int]:
    """Datos de la época: calentamiento antes del inicio y COLA_DATOS_D días tras el fin (para que acaben las
    operaciones de los últimos días; las que no acaban son «fin de datos» y no cuentan)."""
    return (periodo["inicio"] - ajustes.CALENTAMIENTO_MAX_D * DAY,
            min(periodo["fin"] + ajustes.COLA_DATOS_D * DAY, (ajustes.ahora_ms() // MIN) * MIN))


def firma_epoca(epoca: str) -> str:
    per = periodo_epoca(epoca)
    a, b = rango_epoca(per)
    meses = meses_entre(a, per["fin"])   # el mes de la cola (en curso) cambia cada hora: no entra en la firma
    return firma_archivos([p for c in config.COINS for p in archivos_moneda(c, meses)])


def cache_existente(epoca: str) -> Path | None:
    base = ajustes.CACHE
    if not base.exists():
        return None
    cands = sorted((p for p in base.iterdir() if p.is_dir() and p.name.startswith(epoca + "-")
                    and (p / "meta.json").exists()), key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def preparar_epoca(epoca: str | None = None, forzar: bool = False) -> Fuente:
    """Caché de la época (la construye si falta o si cambió la firma de sus archivos)."""
    epoca = epoca or epoca_de()
    per = periodo_epoca(epoca)
    fir = firma_epoca(epoca)
    car = carpeta_epoca(epoca, fir)
    if car.exists() and (car / "meta.json").exists() and not forzar:
        return abrir_fuente(car)
    a, b = rango_epoca(per)
    log.info("Construyendo la caché de la época %s (%s → %s)", epoca, dia_mad(a), dia_mad(b))
    fu = construir_fuente(a, b, per, carpeta=car)
    for p in ajustes.CACHE.iterdir():   # las cachés viejas de la misma época sobran
        if p.is_dir() and p.name.startswith(epoca + "-") and p != car:
            shutil.rmtree(p, ignore_errors=True)
    return fu


def mercado_epoca(epoca: str | None = None) -> Mercado:
    epoca = epoca or epoca_de()
    car = cache_existente(epoca)
    fu = abrir_fuente(car) if car else preparar_epoca(epoca)
    return Mercado(fu)


def mercado_vivo(desde_ms: int, hasta_ms: int | None = None, periodo: dict | None = None) -> Mercado:
    """Mercado en memoria con lo último (prueba en avance): las mismas lecturas que la caché, sin guardar nada."""
    hasta_ms = hasta_ms or (ajustes.ahora_ms() // MIN) * MIN
    per = periodo or {"epoca": "vivo", "inicio": desde_ms, "fin": hasta_ms, "corte": ajustes.CORTE_MS}
    return Mercado(construir_fuente(desde_ms, hasta_ms, per, aperturas=True))


# ---------------------------------------------------------------- datos sintéticos (pruebas y conformidad)

def fuente_sintetica(dias: int = 90, monedas=None, semilla: int = 7, t0: int | None = None, huecos: bool = True,
                     periodo: dict | None = None) -> Fuente:
    """Velas de 1 min sintéticas (paseo aleatorio con rachas de tendencia y volatilidad cambiante), funding cada 8 h,
    miedo y codicia, cierres de bolsa y aperturas reales del calendario. Sin red ni disco: para la conformidad de las
    familias y las pruebas."""
    monedas = tuple(monedas or config.COINS)
    rng = np.random.default_rng(semilla)
    t0 = t0 if t0 is not None else int(datetime(2025, 3, 3, tzinfo=timezone.utc).timestamp() * 1000)
    n = dias * 1440
    velas, densos = {}, {}
    precio0 = {"BTC": 60000.0, "ETH": 3000.0, "SOL": 150.0, "XRP": 0.6}
    base_ret = rng.standard_normal(n)
    # episodios de todo el mercado (unos 2 al día): volumen × 3-8, volatilidad × 2,5 y empuje en un sentido; BTC va
    # primero y las alts le siguen con 15-30 min de retraso (para que las familias de flujo y de adelanto tengan señal)
    epi = np.zeros(n)
    sentido = np.zeros(n)
    for a in np.flatnonzero(rng.random(n) < 2 / 1440):
        dur = int(rng.integers(20, 180))
        epi[a:a + dur] = rng.uniform(3, 8)
        sentido[a:a + dur] = rng.choice([-1.0, 1.0])
    for i, c in enumerate(monedas):
        retraso = 0 if c == "BTC" else int(rng.integers(15, 31))
        ep = np.r_[np.zeros(retraso), epi[:n - retraso]] if retraso else epi
        se = np.r_[np.zeros(retraso), sentido[:n - retraso]] if retraso else sentido
        vol = 0.0008 * (1 + 0.8 * np.sin(np.arange(n) / (1440 * 3.0) + i)) ** 2 + 0.0002
        vol = vol * np.where(ep > 0, 2.5, 1.0)
        deriva = 0.00015 * np.sign(np.sin(np.arange(n) / (1440 * 4.5) + 2 * i))
        r = (0.6 * base_ret + 0.8 * rng.standard_normal(n)) * vol + deriva * vol * 3 + se * vol * 0.6
        # saltos ocasionales (cascadas y subidas)
        saltos = rng.random(n) < 0.0004
        r[saltos] += rng.choice([-1, 1], saltos.sum()) * rng.uniform(0.01, 0.04, saltos.sum())
        c_ = precio0.get(c, 100.0) * np.exp(np.cumsum(r))
        o_ = np.r_[c_[0], c_[:-1]]
        sp = np.abs(rng.standard_normal(n)) * vol * c_ * 0.6
        h_ = np.maximum(o_, c_) + sp
        l_ = np.minimum(o_, c_) - sp
        v_ = rng.gamma(2.0, 50.0, n) * (1 + 5 * saltos) * np.where(ep > 0, ep, 1.0)
        tb_ = v_ * np.clip(rng.uniform(0.3, 0.7, n) + 0.25 * se, 0.02, 0.98)
        t = t0 + np.arange(n, dtype=np.int64) * MIN
        keep = np.ones(n, bool)
        if huecos:
            for _ in range(max(1, dias // 15)):
                a = int(rng.integers(0, n - 30))
                keep[a:a + int(rng.integers(1, 20))] = False
        vv = Velas(t[keep], o_[keep], h_[keep], l_[keep], c_[keep], v_[keep], tb_[keep])
        velas[c], densos[c] = vv, denso_de(vv)
    fund = {}
    for i, c in enumerate(monedas):
        ft = (t0 // (8 * H) + 1 + np.arange(dias * 3, dtype=np.int64)) * 8 * H
        fr = 0.0001 + 0.0003 * np.sin(np.arange(len(ft)) / 9.0 + i) + 0.0002 * rng.standard_normal(len(ft))
        fund[c] = (ft, fr)
    dd = [datetime.fromtimestamp((t0 + k * DAY) / 1000, timezone.utc).date() for k in range(dias)]
    fng = (np.array([int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000) + H for d in dd],
                    np.int64), np.clip(50 + 30 * np.sin(np.arange(dias) / 7.0), 1, 99))
    from .. import regions
    bol = {}
    for k in ("nuevayork", "tokio", "londres"):
        m = regions.MARKETS[k]
        ds = [d for d in dd if d.weekday() < 5]
        kn = np.array([int(regions.close_utc_on(m, d).timestamp() * 1000) + 15 * MIN for d in ds], np.int64)
        rt = rng.standard_normal(len(ds)) * 1.1
        cl = 4000 * np.exp(np.cumsum(rt / 100))
        o = np.argsort(kn, kind="stable")
        bol[k] = (kn[o], [ds[i].isoformat() for i in o], cl[o], rt[o])
    mt = np.array([t0 + int(x) * DAY + int(13.5 * H) for x in range(5, dias, 9)], np.int64)
    mn = [("CPI", "NFP", "FOMC")[i % 3] for i in range(len(mt))]
    try:
        aps = cargar_aperturas(t0, t0 + n * MIN, idx={})
    except Exception:   # sin calendario: sin aperturas
        aps = []
    per = periodo or {"epoca": "sintetica", "inicio": t0, "fin": t0 + n * MIN, "corte": t0 + int(n * 0.6) * MIN}
    return Fuente(monedas, per, velas, densos, {}, fund, fng, bol, (mt, mn), aps, f"sint{semilla}", None, {})
