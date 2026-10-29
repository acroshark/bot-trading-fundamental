"""Ruptura V3 («rango de apertura con tendencia, volumen y régimen», 29-10-2026): las reglas del diseño
BACKTEST_DIR/controles/selectiva/diseno_ruptura.md (variante V3, «RP-15 tarde») que no dependen del motor. SOLO PAPER.

Lo usan el motor de aperturas (apertura.RupturaRun y OpeningEngine con el preset «ruptura3») y su réplica de 3 años
(scripts/replica_ruptura3.py), así que en vivo y en el backtest se decide con las mismas funciones:
- Régimen cripto: el de la skill crypto-regime-analyzer (MIT, TraderMonty; .claude/skills/crypto-regime-analyzer),
  portado aquí tal cual con las dos adaptaciones declaradas en el diseño (sección 5): la amplitud de las alts con 3
  (ETH, SOL y XRP; la skill pide 5) y el momento con las 4 monedas (la skill pide 5). La dominancia de BTC no tiene
  histórico: queda sin dato y su peso se reparte, como manda la skill. Largos solo en RISK_ON (≥ 80) y cortos solo en
  RISK_OFF (< 40); en NEUTRAL o sin datos (UNKNOWN) no se opera. El régimen solo restringe: nunca dispara una entrada.
- Tendencia de la moneda: diaria (cierre y SMA20 frente a SMA50) y de 4 h (cierre > EMA20 > EMA50, o al revés) con el
  ADX(14) de Wilder de 4 h ≥ 25 y el DI a favor. Solo velas cerradas antes de la apertura.
- Rango, margen y stop estructural de la ruptura (sección 7): margen máx(2 pb del máximo, 5 % de la altura), stop
  máx(2 %, otro lado del rango + 0,10 %) y, si pasa del 3 %, no se entra.
Las cuentas van con numpy en el mismo orden que BACKTEST_DIR/controles/selectiva/medir_selectiva.py (medias,
sumas y suavizados): así la réplica da las mismas operaciones, bit a bit, que la medida que se prerregistró.
HONESTO: con la salida A, fuera de muestra +0,43 R en 118 operaciones, pero dentro de muestra −0,03 R en 159; el p
frente a su base al azar es 0,09 y la mejora frente a «6 diarias v2» sale de la selección de entradas. No es una
ventaja demostrada: se mide en paper."""
import bisect
import math

import numpy as np

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
M5, H4 = 5 * MIN, 4 * H

# ---------------------------------------------------------------- reglas fijadas en el diseño (umbrales redondos)
ADX_MIN = 25.0            # ADX(14) de 4 h mínimo (Wilder)
VOL_X = 1.5               # volumen de la vela de 5 min de la ruptura ≥ 1,5 × la media de las del rango
TR_X = 1.5                # rango verdadero de esa vela ≥ 1,5 × ATR(14) de 5 min (expansión)
ATR5_N = 14
STOP_HOLGURA_PCT = 0.10   # el stop estructural queda 0,10 % más allá del otro lado del rango
BRK_BPS, BRK_FRAC = 2.0, 0.05   # margen de ruptura: el mayor de 2 pb del máximo o el 5 % de la altura del rango
PRE5 = 16                 # velas de 5 min antes de la apertura (80 min) para el ATR de 5 min
MIN_4H = 150              # velas de 4 h cerradas que hacen falta antes de la última (EMA50 y ADX asentados)
MIN_1M_4H = 200           # solo en la réplica: una vela de 4 h hecha con menos de 200 de 1 min no vale
DIAS_REGIMEN = 420        # cierres diarios que lee el régimen (SMA200 + pendiente de 20 días y la caída de 365)
MIN_DIARIA = 50

ZONAS = ("RISK_ON", "NEUTRAL", "RISK_OFF", "UNKNOWN")
ZONA_TXT = {"RISK_ON": "favorable (RISK_ON)", "NEUTRAL": "neutral", "RISK_OFF": "defensivo (RISK_OFF)",
            "UNKNOWN": "sin datos"}


def _n(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


# ---------------------------------------------------------------- régimen cripto (skill crypto-regime-analyzer)
# Port de .claude/skills/crypto-regime-analyzer/scripts (scorer.py, numeric_utils.py y calculators/*.py) sin cambiar
# ninguna cuenta. tests/test_ruptura3.py lo compara con la skill.

PESOS = {"btc_trend": 0.25, "alt_breadth": 0.20, "dominance": 0.15, "funding": 0.15, "drawdown_vol": 0.15,
         "momentum_thrust": 0.10}
MIN_COMPONENTES, MIN_PESO = 4, 0.65
ZONAS_UMBRAL = ((80, "RISK_ON"), (40, "NEUTRAL"), (0, "RISK_OFF"))
MAX_ABS_FUNDING = 1.0
ALT_MIN_UNIVERSO = 3      # adaptación declarada (la skill: 5)
MOM_MIN_UNIVERSO = 4      # adaptación declarada (la skill: 5)


def _media(vals: list[float]) -> float:
    """scaled_mean de la skill: media finita sin desbordar."""
    if not vals:
        raise ValueError("hace falta al menos un valor")
    s = max(abs(v) for v in vals)
    if s == 0:
        return 0.0
    return s * (math.fsum(v / s for v in vals) / len(vals))


def btc_trend(closes: list) -> dict:
    """C1 (25 %): estructura de BTC frente a SMA50 y SMA200 y pendiente de la SMA200 a 20 días (≥ 220 cierres)."""
    if not closes or len(closes) < 220:
        return {"score": 50, "signal": "sin datos (hacen falta 220 cierres diarios de BTC)", "data_available": False}
    price, ma50, ma200 = closes[-1], _media(closes[-50:]), _media(closes[-200:])
    prev = _media(closes[-220:-20])
    if math.isclose(ma200, prev, rel_tol=1e-12, abs_tol=0.0):
        di = "flat"
    elif ma200 > prev:
        di = "rising"
    else:
        di = "falling"
    flat = all(math.isclose(v, ma200, rel_tol=1e-12, abs_tol=0.0) for v in (price, ma50))
    if flat:
        base, est = 50, "plana"
    elif price > ma50 > ma200:
        base, est = 90, "alcista (precio > SMA50 > SMA200)"
    elif price > ma200 and price <= ma50 and ma50 > ma200:
        base, est = 65, "retroceso alcista (entre SMA200 y SMA50)"
    elif price <= ma200 and price <= ma50 and ma50 > ma200:
        base, est = 55, "medias alcistas con el precio debajo"
    elif price > ma50 and ma50 <= ma200:
        base, est = 45, "intento de recuperación (por encima de SMA50, medias bajistas)"
    else:
        base, est = 15, "bajista (precio < SMA50 < SMA200)"
    score = max(0, min(100, base + (10 if di == "rising" else -10 if di == "falling" else 0)))
    sube = {"rising": "sube", "falling": "baja", "flat": "plana"}[di]
    return {"score": score, "signal": f"BTC {est}; SMA200 {sube}", "data_available": True}


def _pct_encima(series: dict, n: int) -> tuple[float, int]:
    above = counted = 0
    for cl in series.values():
        if not cl or len(cl) < n:
            continue
        counted += 1
        if cl[-1] > _media(cl[-n:]):
            above += 1
    return (above / counted * 100) if counted else 0.0, counted


def alt_breadth(alts: dict) -> dict:
    """C2 (20 %): parte de las alts por encima de su SMA200, con el retoque de la SMA50."""
    elig = {k: v for k, v in (alts or {}).items() if v and len(v) >= 200}
    p200, n = _pct_encima(elig, 200)
    if n < ALT_MIN_UNIVERSO:
        return {"score": 50, "signal": f"sin datos ({n} alts con 200 días)", "data_available": False}
    p50, _ = _pct_encima(elig, 50)
    score = (95 if p200 >= 80 else 80 if p200 >= 65 else 65 if p200 >= 50 else 45 if p200 >= 35 else
             25 if p200 >= 20 else 10)
    if p50 >= p200 + 15:
        score += 5
    elif p50 <= p200 - 15:
        score -= 5
    return {"score": max(0, min(100, score)),
            "signal": f"{p200:.0f} % de {n} alts por encima de la SMA200 ({p50:.0f} % de la SMA50)",
            "data_available": True}


def funding_regime(rates: dict) -> dict:
    """C4 (15 %): funding medio de 8 h de los perpetuos (contrario en los extremos)."""
    raw = [r for r in (rates or {}).values() if r is not None]
    if any(isinstance(r, bool) or not isinstance(r, (int, float)) or not math.isfinite(r) or abs(r) > MAX_ABS_FUNDING
           for r in raw):
        return {"score": 50, "signal": "funding no válido", "data_available": False}
    if len(raw) < 2:
        return {"score": 50, "signal": "sin datos de funding (hacen falta 2 perpetuos)", "data_available": False}
    avg = _media(raw)
    score = (80 if avg <= -0.00010 else 65 if avg < 0.0 else 75 if avg <= 0.00010 else 55 if avg <= 0.00030 else
             30 if avg <= 0.00060 else 10)
    return {"score": score, "signal": f"funding medio {avg * 100:.4f} % por 8 h en {len(raw)} perpetuos",
            "data_available": True}


def _vol_realizada(closes: list, n: int) -> float:
    rets = [math.log(closes[i]) - math.log(closes[i - 1]) for i in range(len(closes) - n + 1, len(closes))
            if closes[i - 1] > 0]
    if len(rets) < 2:
        return 0.0
    m = math.fsum(rets) / len(rets)
    var = math.fsum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(0.0, var)) * math.sqrt(365)


def drawdown_vol(closes: list) -> dict:
    """C5 (15 %): caída de BTC desde el máximo de 365 días y percentil de su volatilidad de 30 días."""
    if not closes or len(closes) < 365:
        return {"score": 50, "signal": "sin datos (hacen falta 365 cierres de BTC)", "data_available": False}
    win = closes[-365:]
    high, price = max(win), closes[-1]
    dd = 1 - (price / high) if high > 0 else 0.0
    score = 90 if dd <= 0.10 else 75 if dd <= 0.20 else 55 if dd <= 0.35 else 35 if dd <= 0.50 else \
        20 if dd <= 0.65 else 10
    vols = [_vol_realizada(win[: i + 1], 30) for i in range(30, len(win), 7)]
    cur = _vol_realizada(win, 30)
    pct = sum(1 for v in vols if v <= cur) / len(vols) if vols else 0.5
    if pct <= 1 / 3:
        score += 10
    elif pct >= 2 / 3:
        score -= 10
    if dd > 0.65 and pct >= 2 / 3:
        score = max(score, 15)
    return {"score": max(0, min(100, score)), "signal": f"BTC a {dd * 100:.1f} % de su máximo de un año",
            "data_available": True}


def momentum_thrust(series: dict) -> dict:
    """C6 (10 %): parte de las monedas con retorno de 30 días positivo."""
    pos = counted = 0
    for cl in (series or {}).values():
        if not cl or len(cl) < 31:
            continue
        counted += 1
        if cl[-1] > cl[-31]:
            pos += 1
    if counted < MOM_MIN_UNIVERSO:
        return {"score": 50, "signal": f"sin datos ({counted} monedas con 31 días)", "data_available": False}
    pct = pos / counted * 100
    score = 90 if pct >= 85 else 75 if pct >= 65 else 55 if pct >= 45 else 35 if pct >= 25 else 20 if pct >= 10 else 35
    return {"score": score, "signal": f"{pct:.0f} % de {counted} monedas suben en 30 días", "data_available": True}


def compuesto(comps: dict) -> dict:
    """calculate_composite_score de la skill: media ponderada de los componentes con dato (≥ 4 y ≥ 65 % del peso)."""
    ok = {k: c for k, c in comps.items() if k in PESOS and c.get("data_available", False)}
    tot = sum(PESOS[k] for k in ok)
    if len(ok) < MIN_COMPONENTES or tot < MIN_PESO:
        return {"score": None, "zone": "UNKNOWN", "available_weight": round(tot, 4)}
    eff = {k: PESOS[k] / tot for k in ok}
    score = round(max(0.0, min(100.0, sum(ok[k]["score"] * w for k, w in eff.items()))), 1)
    zone = ZONAS_UMBRAL[-1][1]
    for th, z in ZONAS_UMBRAL:
        if score >= th:
            zone = z
            break
    return {"score": score, "zone": zone, "available_weight": round(tot, 4),
            "effective_weights": {k: round(v, 4) for k, v in eff.items()}}


def regimen(cierres: dict, funding: dict) -> dict:
    """Régimen cripto con las 4 monedas: cierres = {moneda: [cierres diarios cerrados, del más viejo al último]} (se
    usan los DIAS_REGIMEN últimos) y funding = {moneda: tasa de 8 h en tanto por uno, o None}. {zona, puntos,
    componentes{id: {puntos, texto, dato}}, peso_disponible}."""
    cl = {c: list(v or [])[-DIAS_REGIMEN:] for c, v in (cierres or {}).items()}
    btc = cl.get("BTC") or []
    comps = {"btc_trend": btc_trend(btc),
             "alt_breadth": alt_breadth({c: cl.get(c) or [] for c in ("ETH", "SOL", "XRP")}),
             "funding": funding_regime({f"{c}USDT": v for c, v in (funding or {}).items()}),
             "drawdown_vol": drawdown_vol(btc),
             "momentum_thrust": momentum_thrust(cl)}
    comp = compuesto(comps)
    return {"zona": comp["zone"], "puntos": comp["score"], "peso_disponible": comp["available_weight"],
            "componentes": {k: {"puntos": v["score"], "texto": v["signal"], "dato": bool(v["data_available"])}
                            for k, v in comps.items()}}


def lado_regimen(zona: str | None) -> int:
    """+1 en RISK_ON (solo largos), −1 en RISK_OFF (solo cortos), 0 en NEUTRAL o sin datos."""
    return 1 if zona == "RISK_ON" else -1 if zona == "RISK_OFF" else 0


# ---------------------------------------------------------------- datos de entrada (velas y funding)

def cierres_cerrados(barras_1d: list, open_ms: int) -> list[float]:
    """Cierres de los días UTC ya cerrados en open_ms (vela [t, o, h, l, c, …] con t + 1 día ≤ open_ms), en orden."""
    return [float(b[4]) for b in sorted(barras_1d or [], key=lambda b: b[0]) if b[0] + DAY <= open_ms]


def funding_8h(filas: list, t_ms: int) -> float | None:
    """Última tasa de funding liquidada en t_ms o antes ([[ms, tasa por periodo], …], en tanto por uno), escalada a 8 h
    si su periodo (la distancia a la anterior) es de 1, 2 o 4 h. None sin dos liquidaciones."""
    ts = [int(r[0]) for r in filas or []]
    i = bisect.bisect_right(ts, t_ms) - 1
    if i < 1:
        return None
    per = round((ts[i] - ts[i - 1]) / H)
    return float(filas[i][1]) * (8 / per if per in (1, 2, 4) else 1.0)


# ---------------------------------------------------------------- tendencia de la moneda

def tendencia_diaria(cierres: list) -> int:
    """+1 si el último cierre y la SMA20 están por encima de la SMA50, −1 si los dos por debajo, 0 si no (≥ 50 días)."""
    x = np.asarray(cierres if cierres is not None else [], dtype=float)
    if len(x) < MIN_DIARIA:
        return 0
    cd, m20, m50 = x[-1], x[-20:].mean(), x[-50:].mean()
    return 1 if cd > m50 and m20 > m50 else -1 if cd < m50 and m20 < m50 else 0


def ema_sembrada(x, n: int) -> np.ndarray:
    """EMA con α = 2 / (n + 1) sembrada con la SMA de los n primeros valores (NaN antes)."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a, v = 2 / (n + 1), x[:n].mean()
    out[n - 1] = v
    for i in range(n, len(x)):
        v = a * x[i] + (1 - a) * v
        out[i] = v
    return out


def adx_wilder(h, l, c, n: int = 14):
    """(ADX, +DI, −DI) de Wilder: TR, +DM y −DM con su suavizado (α = 1/n) y el ADX sembrado con la media de los n
    primeros DX. NaN donde aún no hay bastante historia."""
    h, l, c = (np.asarray(v, dtype=float) for v in (h, l, c))
    m = len(c)
    pdi, mdi, adx = np.full(m, np.nan), np.full(m, np.nan), np.full(m, np.nan)
    if m < 2 * n + 1:
        return adx, pdi, mdi
    tr, pdm, mdm = np.zeros(m), np.zeros(m), np.zeros(m)
    for i in range(1, m):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if up > dn and up > 0 else 0.0
        mdm[i] = dn if dn > up and dn > 0 else 0.0
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
    adx[first] = np.nanmean(dx[n:first + 1])
    for i in range(first + 1, m):
        adx[i] = (adx[i - 1] * (n - 1) + dx[i]) / n
    return adx, pdi, mdi


def series_4h(h, l, c) -> dict:
    """Indicadores de una serie de velas de 4 h (máximos, mínimos y cierres en orden): EMA20, EMA50, ADX, +DI y −DI."""
    adx, pdi, mdi = adx_wilder(h, l, c)
    cc = np.asarray(c, dtype=float)
    return {"c": cc, "e20": ema_sembrada(cc, 20), "e50": ema_sembrada(cc, 50), "adx": adx, "pdi": pdi, "mdi": mdi}


def tendencia_4h_en(s: dict, i: int) -> dict | None:
    """Tendencia de 4 h en la vela i de series_4h: d (+1 cierre > EMA20 > EMA50, −1 al revés, 0), ADX, DI (+1 si +DI >
    −DI) y ext = |cierre / EMA50 − 1| (desempate de la lista de vigilancia). None con menos de MIN_4H velas antes o sin
    ADX."""
    if i < MIN_4H or i >= len(s["c"]) or not np.isfinite(s["adx"][i]):
        return None
    cc, e20, e50 = s["c"][i], s["e20"][i], s["e50"][i]
    d = 1 if cc > e20 > e50 else -1 if cc < e20 < e50 else 0
    di = 1 if s["pdi"][i] > s["mdi"][i] else -1 if s["mdi"][i] > s["pdi"][i] else 0
    return {"d": d, "adx": float(s["adx"][i]), "di": di, "ext": float(abs(cc / e50 - 1)), "cierre": float(cc),
            "ema20": float(e20), "ema50": float(e50)}


def tendencia_4h(barras_4h: list, open_ms: int) -> dict | None:
    """Tendencia de 4 h al abrir con las velas de 4 h ([t, o, h, l, c, …]) cerradas en open_ms (t + 4 h ≤ open_ms):
    tendencia_4h_en en la última. En vivo se piden 1.000 (unos 166 días): la EMA50 y el ADX ya no dependen de dónde
    empiezan (la diferencia con la serie entera es menor que 1e-15)."""
    bs = sorted((b for b in barras_4h or [] if b[0] + H4 <= open_ms), key=lambda b: b[0])
    if len(bs) <= MIN_4H:
        return None
    s = series_4h([b[2] for b in bs], [b[3] for b in bs], [b[4] for b in bs])
    return tendencia_4h_en(s, len(bs) - 1)


def sentido(zona: str | None, d_dia: int, t4: dict | None, adx_min: float = ADX_MIN) -> tuple[int, str, str]:
    """Sentido permitido de la moneda en esta apertura (sección 6): (d, código, motivo). d = +1 con RISK_ON, diaria y 4 h
    alcistas y ADX ≥ 25 con +DI > −DI; −1 con RISK_OFF y todo bajista; 0 si no, con el código de decisiones_apertura
    (regimen_neutral, sin_tendencia o adx_bajo)."""
    lado = lado_regimen(zona)
    if not lado:
        return 0, "regimen_neutral", (f"Régimen cripto {ZONA_TXT.get(zona or 'UNKNOWN', zona)}: ni largos ni cortos "
                                      "(largos solo en RISK_ON y cortos solo en RISK_OFF)")
    sl = "alcista" if lado > 0 else "bajista"
    if d_dia != lado:
        return 0, "sin_tendencia", f"Régimen {ZONA_TXT[zona]}, pero la tendencia diaria de la moneda no es {sl}"
    if not t4 or t4.get("d") != lado:
        return 0, "sin_tendencia", (f"Régimen {ZONA_TXT[zona]} y diaria {sl}, pero la tendencia de 4 h no es {sl}"
                                    + ("" if t4 else " (sin velas de 4 h suficientes)"))
    if t4["adx"] < adx_min or t4["di"] != lado:
        why = (f"ADX(14) de 4 h {_n(t4['adx'], 1)} < {_n(adx_min, 0)}" if t4["adx"] < adx_min
               else f"el DI va en contra ({'−DI > +DI' if lado > 0 else '+DI > −DI'})")
        return 0, "adx_bajo", f"Régimen {ZONA_TXT[zona]}, diaria y 4 h {'alcistas' if lado > 0 else 'bajistas'}, pero {why}"
    return lado, "entra", (f"Régimen {ZONA_TXT[zona]}, tendencia diaria y de 4 h {'alcistas' if lado > 0 else 'bajistas'}"
                           f" y ADX(14) de 4 h {_n(t4['adx'], 1)}: solo {'largos' if lado > 0 else 'cortos'}")


# ---------------------------------------------------------------- rango, ruptura y stop

def margen(orh: float, orl: float) -> float:
    """Margen de ruptura: el mayor de 2 pb del máximo del rango o el 5 % de su altura (como el laboratorio)."""
    return max(BRK_BPS / 1e4 * orh, BRK_FRAC * (orh - orl))


def stop_estructural(d: int, c: float, orh: float, orl: float) -> float:
    """Stop estructural en % desde el cierre que dispara: hasta el otro lado del rango más 0,10 %."""
    return 100 * ((c - orl) / c if d > 0 else (orh - c) / c) + STOP_HOLGURA_PCT


def orden(act: dict) -> tuple:
    """Orden de los disparos de una apertura: el que dispara antes y, en la misma vela, el de mayor ADX de 4 h (y,
    si empatan, el más alejado de su EMA50)."""
    return int(act.get("t") or 0), -float(act.get("adx") or 0.0), -float(act.get("ext") or 0.0)
