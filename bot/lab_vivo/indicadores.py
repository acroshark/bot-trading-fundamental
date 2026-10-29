"""Indicadores causales con numpy (docs/07, 6.3): cada valor i se conoce al cierre de la barra i y solo usa barras
≤ i (las funciones «previo» usan solo las ANTERIORES, sin la actual). Los primeros valores sin historia suficiente son
NaN. Portados de medir_selectiva.py (f_ema, f_wilder, f_atr_adx, r_adx, m_pivotes) y del laboratorio (ATR de 14 h en
percentil de 30 días).

Las ventanas largas (medianas y percentiles de 720 barras) se calculan por trozos para no gastar memoria: el PC del
usuario va justo."""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as _ventanas

TROZO = 2048


def anterior(x: np.ndarray, k: int = 1, relleno=np.nan) -> np.ndarray:
    """x desplazado k barras hacia delante (el valor de hace k barras)."""
    x = np.asarray(x, float)
    out = np.full(len(x), relleno, float)
    if k < len(x):
        out[k:] = x[:len(x) - k]
    return out


def flancos(cond: np.ndarray) -> np.ndarray:
    """True solo donde la condición EMPIEZA a cumplirse (una señal por flanco, no en cada barra que dura)."""
    c = np.asarray(cond, bool)
    if not len(c):
        return c
    return c & ~np.r_[False, c[:-1]]


def sma(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    cs = np.cumsum(np.r_[0.0, x])
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """EMA con semilla en la media de las n primeras (r_ema de medir_selectiva.py)."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a, v = 2 / (n + 1), float(x[:n].mean())
    out[n - 1] = v
    for i in range(n, len(x)):
        v = a * x[i] + (1 - a) * v
        out[i] = v
    return out


def wilder(x: np.ndarray, n: int) -> np.ndarray:
    """Media de Wilder (f_wilder de medir_selectiva.py)."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    s = float(x[:n].mean())
    out[n - 1] = s
    for i in range(n, len(x)):
        s = s + (x[i] - s) / n
        out[i] = s
    return out


def rango_verdadero(h, l, c) -> np.ndarray:
    h, l, c = (np.asarray(a, float) for a in (h, l, c))
    pc = np.r_[c[0], c[:-1]] if len(c) else c
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def atr(h, l, c, n: int = 14) -> np.ndarray:
    """ATR de Wilder (en precio)."""
    return wilder(rango_verdadero(h, l, c), n)


def atr_pct(h, l, c, n: int = 14) -> np.ndarray:
    """ATR de Wilder en % del cierre."""
    c = np.asarray(c, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return atr(h, l, c, n) / c * 100


def rsi(c, n: int = 14) -> np.ndarray:
    """RSI de Wilder."""
    c = np.asarray(c, float)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    up, dn = np.maximum(d, 0.0), np.maximum(-d, 0.0)
    au, ad = float(up[:n].mean()), float(dn[:n].mean())
    for i in range(n, len(c)):
        if i > n:
            au = (au * (n - 1) + up[i - 1]) / n
            ad = (ad * (n - 1) + dn[i - 1]) / n
        out[i] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def adx(h, l, c, n: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ADX, +DI y −DI de Wilder (r_adx de medir_selectiva.py)."""
    h, l, c = (np.asarray(a, float) for a in (h, l, c))
    m = len(c)
    pdi, mdi, ax = np.full(m, np.nan), np.full(m, np.nan), np.full(m, np.nan)
    if m < 2 * n:
        return ax, pdi, mdi
    tr, pdm, mdm = np.zeros(m), np.zeros(m), np.zeros(m)
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    up, dn = h[1:] - h[:-1], l[:-1] - l[1:]
    pdm[1:] = np.where((up > dn) & (up > 0), up, 0.0)
    mdm[1:] = np.where((dn > up) & (dn > 0), dn, 0.0)
    s_tr, s_p, s_m = tr[1:n + 1].sum(), pdm[1:n + 1].sum(), mdm[1:n + 1].sum()
    dx = np.full(m, np.nan)
    for i in range(n, m):
        if i > n:
            s_tr, s_p, s_m = s_tr - s_tr / n + tr[i], s_p - s_p / n + pdm[i], s_m - s_m / n + mdm[i]
        if s_tr > 0:
            pdi[i], mdi[i] = 100 * s_p / s_tr, 100 * s_m / s_tr
            ss = pdi[i] + mdi[i]
            dx[i] = 100 * abs(pdi[i] - mdi[i]) / ss if ss > 0 else 0.0
    first = 2 * n - 1
    ax[first] = np.nanmean(dx[n:first + 1])
    for i in range(first + 1, m):
        ax[i] = (ax[i - 1] * (n - 1) + dx[i]) / n
    return ax, pdi, mdi


def bollinger(c, n: int = 20, k: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(media, banda alta, banda baja, anchura = (alta − baja) / media) con la desviación poblacional de n cierres."""
    c = np.asarray(c, float)
    mid = sma(c, n)
    sd = np.full(len(c), np.nan)
    if len(c) >= n:
        w = _ventanas(c, n)
        sd[n - 1:] = w.std(axis=1)
    up, lo = mid + k * sd, mid - k * sd
    with np.errstate(divide="ignore", invalid="ignore"):
        ancho = (up - lo) / mid
    return mid, up, lo, ancho


def _previo(x: np.ndarray, n: int, fn) -> np.ndarray:
    """fn sobre las n barras ANTERIORES a cada i (sin la i): NaN mientras no hay n."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) <= n:
        return out
    w = _ventanas(x[:-1], n)          # w[j] = x[j : j + n] → para i = j + n
    for a in range(0, len(w), TROZO):
        out[n + a:n + a + len(w[a:a + TROZO])] = fn(w[a:a + TROZO])
    return out


def max_previo(x, n: int) -> np.ndarray:
    """Máximo de las n barras anteriores (la actual no entra: rupturas)."""
    return _previo(x, n, lambda w: w.max(axis=1))


def min_previo(x, n: int) -> np.ndarray:
    return _previo(x, n, lambda w: w.min(axis=1))


def media_previa(x, n: int) -> np.ndarray:
    return _previo(x, n, lambda w: w.mean(axis=1))


def desv_previa(x, n: int) -> np.ndarray:
    return _previo(x, n, lambda w: w.std(axis=1, ddof=1) if w.shape[1] > 1 else np.zeros(len(w)))


def mediana_previa(x, n: int) -> np.ndarray:
    return _previo(x, n, lambda w: np.median(w, axis=1))


def percentil_previo(x, n: int) -> np.ndarray:
    """Percentil (0-1) de x[i] frente a las n barras anteriores: fracción de ellas por debajo (sin NaN)."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) <= n:
        return out
    w = _ventanas(x[:-1], n)
    for a in range(0, len(w), TROZO):
        ww = w[a:a + TROZO]
        cur = x[n + a:n + a + len(ww)]
        ok = np.isfinite(ww)
        cnt = ok.sum(axis=1)
        with np.errstate(invalid="ignore"):
            below = ((ww < cur[:, None]) & ok).sum(axis=1)
            res = np.where(cnt >= max(2, n // 2), below / np.maximum(cnt, 1), np.nan)
        res[~np.isfinite(cur)] = np.nan
        out[n + a:n + a + len(ww)] = res
    return out


def cuantil_previo(x, n: int, q: float) -> np.ndarray:
    """Cuantil q de las n barras anteriores (sin NaN)."""
    return _previo(x, n, lambda w: np.nanquantile(w, q, axis=1) if np.isfinite(w).any() else np.full(len(w), np.nan))


def mediana_previa_en(x, n: int, idx) -> np.ndarray:
    """Mediana de las n barras ANTERIORES solo en los índices idx (lo mismo que mediana_previa(x, n)[idx], sin calcularla
    en todas las barras: las familias la piden solo donde ya se cumple el resto de la regla)."""
    x = np.asarray(x, float)
    idx = np.asarray(idx, np.int64)
    out = np.full(len(idx), np.nan)
    ok = idx >= n
    for k in np.flatnonzero(ok):
        i = int(idx[k])
        out[k] = np.median(x[i - n:i])
    return out


def volumen_relativo_en(v, n: int, idx) -> np.ndarray:
    """volumen_relativo(v, n)[idx] calculado solo en esos índices."""
    v = np.asarray(v, float)
    idx = np.asarray(idx, np.int64)
    med = mediana_previa_en(v, n, idx)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(med > 0, v[idx] / med, np.nan)


def volumen_relativo(v, n: int) -> np.ndarray:
    """Volumen de la barra frente a la mediana de las n anteriores."""
    v = np.asarray(v, float)
    med = mediana_previa(v, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(med > 0, v / med, np.nan)


def retornos_log(c) -> np.ndarray:
    c = np.asarray(c, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.r_[np.nan, np.log(c[1:] / c[:-1])]


def pivotes(h, l, p: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """Fractales p-p (m_pivotes de medir_selectiva.py): índices de los máximos y mínimos. Ojo: un pivote en i solo se
    CONOCE en i + p (pivotes_confirmados)."""
    h, l = np.asarray(h, float), np.asarray(l, float)
    N = len(h)
    if N < 2 * p + 1:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    i = np.arange(p, N - p)
    hl, hr = _ventanas(h, p)[: N - 2 * p].max(1), _ventanas(h, p)[p + 1:].max(1)[: N - 2 * p]
    ll, lr = _ventanas(l, p)[: N - 2 * p].min(1), _ventanas(l, p)[p + 1:].min(1)[: N - 2 * p]
    return i[(h[i] > hl) & (h[i] >= hr)], i[(l[i] < ll) & (l[i] <= lr)]


def pivotes_confirmados(h, l, p: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """(índice del pivote, índice en que se conoce = pivote + p) de máximos y mínimos, como arrays (k, 2)."""
    ph, pl = pivotes(h, l, p)
    return np.c_[ph, ph + p], np.c_[pl, pl + p]


def vwap_desde(precio, volumen, ancla: np.ndarray) -> np.ndarray:
    """VWAP acumulado desde la última barra con ancla True (incluida)."""
    p, v = np.asarray(precio, float), np.asarray(volumen, float)
    out = np.full(len(p), np.nan)
    sp = sv = 0.0
    empezado = False
    for i in range(len(p)):
        if ancla[i]:
            sp = sv = 0.0
            empezado = True
        if not empezado:
            continue
        sp += p[i] * v[i]
        sv += v[i]
        out[i] = sp / sv if sv > 0 else p[i]
    return out


def ultimo_valido(x: np.ndarray) -> np.ndarray:
    """Rellena los NaN con el último valor válido anterior."""
    x = np.asarray(x, float)
    ok = np.isfinite(x)
    idx = np.where(ok, np.arange(len(x)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = x[idx]
    out[~ok & (np.cumsum(ok) == 0)] = np.nan
    return out


def indice_en(t_barras_cierre: np.ndarray, t_ms) -> np.ndarray:
    """Para cada t_ms, la última barra cerrada (t_cierre ≤ t_ms); −1 si ninguna."""
    return np.searchsorted(np.asarray(t_barras_cierre), t_ms, "right") - 1
