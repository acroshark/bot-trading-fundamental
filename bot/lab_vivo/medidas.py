"""Estadística, robustez y meta del laboratorio vivo (docs/07, 8, 9 y 10.3). Reutiliza la del laboratorio de 2.000
(laboratorio.hac_t, t_sf, bh_qvalues, deflated_sharpe, expected_max_z, effective_trials y moments) y porta las medidas
de medir_selectiva.py (cluster_se, ic_dias, max_dd_R, dd_cuenta, base_azar).

Cómo se mide en todas las etapas: R = R_f (con funding: lo que cobra el paper); día = día de Madrid de la entrada;
serie diaria = suma de R por día natural, con ceros en los días sin operaciones."""
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import numpy as np

from .. import laboratorio as lab
from . import ajustes, criterios

MAD = ajustes.MAD
RISK = 0.5            # % de la cuenta por R (config.RISK_PER_TRADE_PCT; el corredor exige que sea 0,5)
META_PCT = 1.0        # lo que pidió el usuario: 1 % al día
META_R = META_PCT / RISK


def _num(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


# ---------------------------------------------------------------- días, semanas y trimestres de Madrid

def dia_de(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).date().isoformat()


def dias_entre(a: str, b: str) -> list[str]:
    """Días naturales de a a b, ambos incluidos."""
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    return [(d0 + timedelta(days=k)).isoformat() for k in range((d1 - d0).days + 1)] if d1 >= d0 else []


def semana_iso(dia: str) -> str:
    y, w, _ = date.fromisoformat(dia).isocalendar()
    return f"{y}-S{w:02d}"


def trimestre(ms: int) -> str:
    d_ = datetime.fromtimestamp(ms / 1000, MAD)
    return f"{d_.year}-T{(d_.month - 1) // 3 + 1}"


def inicio_trimestre(q: str) -> int:
    y, k = int(q[:4]), int(q[-1])
    return int(datetime(y, 3 * (k - 1) + 1, 1, tzinfo=MAD).timestamp() * 1000)


def trimestres_entre(a_ms: int, b_ms: int) -> list[tuple[str, int, int]]:
    """(trimestre, desde, hasta) que cubren [a_ms, b_ms): el primero empieza en a_ms (el corte)."""
    out, q = [], trimestre(a_ms)
    t = a_ms
    while t < b_ms:
        y, k = int(q[:4]), int(q[-1])
        y2, k2 = (y + 1, 1) if k == 4 else (y, k + 1)
        nq = f"{y2}-T{k2}"
        fin = min(b_ms, inicio_trimestre(nq))
        out.append((q, t, fin))
        q, t = nq, fin
    return out


# ---------------------------------------------------------------- series y estadística

def serie_diaria(ops: list[dict], desde_dia: str, hasta_dia: str, clave: str = "R_f") -> np.ndarray:
    dias = dias_entre(desde_dia, hasta_dia)
    pos = {d: i for i, d in enumerate(dias)}
    x = np.zeros(len(dias))
    for r in ops:
        i = pos.get(r["dia"])
        if i is not None:
            x[i] += r[clave]
    return x


def p_hac(serie: np.ndarray, retardo: int | None = None) -> tuple[float, float]:
    """(t de Newey-West, p de una cola) de la media de la serie diaria (laboratorio.hac_t y t_sf con días − 1)."""
    retardo = retardo or criterios.c("robustez", "hac_retardo_d")
    x = np.asarray(serie, float)
    if len(x) < 3 or not np.any(x):
        return 0.0, 1.0
    t = lab.hac_t(x, retardo)
    p = lab.t_sf(t, len(x) - 1)
    return float(t), float(p) if math.isfinite(p) else 1.0


def momentos(serie: np.ndarray) -> dict:
    m, sd, sk, ku = lab.moments(np.asarray(serie, float))
    return {"media": m, "sd": sd, "sr": (m / sd) if sd > 0 else 0.0, "asimetria": sk, "curtosis": ku, "T": len(serie)}


def dsr(sr: float, T: int, asimetria: float, curtosis: float, n: float) -> float:
    v = lab.deflated_sharpe(sr, T, asimetria, curtosis, None, n)
    return float(v) if v == v else 0.0


def z_umbral(n: float) -> float:
    return float(lab.expected_max_z(n))


def bh(ps: list[float]) -> list[float]:
    return lab.bh_qvalues(ps)


def n_efectivo(series: list[np.ndarray]) -> float:
    if len(series) < 2:
        return float(len(series))
    L = min(len(s) for s in series)
    X = np.array([np.asarray(s, float)[-L:] for s in series])
    return float(lab.effective_trials(X))


def n_efectivo_bloques(series_por_bloque: dict) -> float:
    """Suma del N efectivo de Li y Ji de cada bloque (las variantes de una hipótesis, ≤ 6 series: ahí Li y Ji no
    satura). Entre bloques se suman como independientes: lo prudente (un N mayor sube el listón del Sharpe deflactado).
    Es el suelo del N efectivo del laboratorio vivo (docs/07, 9.4)."""
    return float(sum(n_efectivo(s) for s in series_por_bloque.values() if len(s)))


def holm(ps: list[float]) -> list[float]:
    """p ajustados de Holm (en el mismo orden)."""
    m = len(ps)
    if not m:
        return []
    o = sorted(range(m), key=lambda i: ps[i])
    adj, run = [1.0] * m, 0.0
    for k, i in enumerate(o):
        run = max(run, min(1.0, (m - k) * ps[i]))
        adj[i] = run
    return adj


def cluster_se(ops: list[dict], clave: str = "R_f") -> float | None:
    """Error típico del R medio agrupando por día (medir_selectiva.cluster_se)."""
    if len(ops) < 2:
        return None
    s, n_ = defaultdict(float), Counter()
    for r in ops:
        s[r["dia"]] += r[clave]
        n_[r["dia"]] += 1
    N = len(ops)
    m = sum(s.values()) / N
    G = len(s)
    if G < 2:
        return None
    v = sum((s[d] - m * n_[d]) ** 2 for d in s) * G / (G - 1)
    return math.sqrt(v) / N


def ic_dias(ops: list[dict], B: int = 2000, semilla: int = 20261029, clave: str = "R_f") -> list[float] | None:
    """Intervalo del 95 % del R medio con remuestreo por días (medir_selectiva.ic_dias)."""
    if len(ops) < 5:
        return None
    s, n_ = defaultdict(float), Counter()
    for r in ops:
        s[r["dia"]] += r[clave]
        n_[r["dia"]] += 1
    ks = sorted(s)
    S, Nn = np.array([s[k] for k in ks]), np.array([n_[k] for k in ks], dtype=float)
    rng = np.random.default_rng(semilla)
    idx = rng.integers(0, len(ks), size=(B, len(ks)))
    m = S[idx].sum(1) / Nn[idx].sum(1)
    return [round(float(np.percentile(m, 2.5)), 4), round(float(np.percentile(m, 97.5)), 4)]


def max_dd_R(ops: list[dict]) -> float:
    acc = peak = dd = 0.0
    for r in sorted(ops, key=lambda r: (r["ext"], r["t"])):
        acc += r["R_f"]
        peak = max(peak, acc)
        dd = max(dd, peak - acc)
    return dd


def dd_cuenta(ops: list[dict], eq0: float = 100.0) -> float:
    e = peak = eq0
    dd = 0.0
    for r in sorted(ops, key=lambda r: (r["ext"], r["t"])):
        e += r["N"] * (r["ret"] + r["fund_pct"]) / 100
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak * 100)
    return dd


def capital_final(ops: list[dict], eq0: float = 100.0) -> float:
    return eq0 + sum(r["N"] * (r["ret"] + r["fund_pct"]) / 100 for r in ops)


def media(ops: list[dict], clave: str = "R_f") -> float | None:
    return float(np.mean([r[clave] for r in ops])) if ops else None


def sin_mayores(ops: list[dict], k: int = 5) -> float | None:
    """R medio sin las k mayores operaciones."""
    if len(ops) <= k:
        return None
    x = np.sort(np.array([r["R_f"] for r in ops]))
    return float(x[:-k].mean())


def sin_mejor_semana(ops: list[dict]) -> tuple[float | None, str | None]:
    """R medio sin la semana ISO de Madrid con más R."""
    if not ops:
        return None, None
    s = defaultdict(float)
    for r in ops:
        s[semana_iso(r["dia"])] += r["R_f"]
    best = max(s, key=lambda k: s[k])
    resto = [r["R_f"] for r in ops if semana_iso(r["dia"]) != best]
    return (float(np.mean(resto)) if resto else None), best


def semanas(ops: list[dict]) -> int:
    return len({semana_iso(r["dia"]) for r in ops})


def mitades(ops: list[dict], a_ms: int, b_ms: int) -> tuple[float | None, float | None]:
    mid = a_ms + (b_ms - a_ms) // 2
    h1 = [r["R_f"] for r in ops if r["t"] < mid]
    h2 = [r["R_f"] for r in ops if r["t"] >= mid]
    return (float(np.mean(h1)) if h1 else None), (float(np.mean(h2)) if h2 else None)


def por(ops: list[dict], key) -> dict:
    g = defaultdict(list)
    for r in ops:
        g[key(r)].append(r["R_f"])
    return {k: {"n": len(v), "R": round(float(np.mean(v)), 4), "suma_R": round(float(np.sum(v)), 2)}
            for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}


def cierres_por_tiempo(ops: list[dict]) -> dict:
    """Las operaciones que cierran por tiempo ANTES del cerrojo (why «tiempo» sin cerrojo activado): las que rompen
    «o gano más de 2 o pierdo 1» (docs/07, 6.5). n de las «de» (todas), su %, las que salen entre −1R y +2R (R_f) y las
    que ganan pero menos de 2R."""
    t = [r for r in ops if r.get("why") == "tiempo" and not r.get("locked")]
    n = len(ops)
    return {"n": len(t), "de": n, "pct": round(100.0 * len(t) / n, 1) if n else 0.0,
            "entre": sum(1 for r in t if -1.0 < r["R_f"] < 2.0),
            "ganan_menos_2R": sum(1 for r in t if 0.0 < r["R_f"] < 2.0)}


def colas(ops: list[dict]) -> dict:
    if not ops:
        return {}
    x = np.array([r["R_f"] for r in ops])
    return {k: round(float(np.percentile(x, q)), 3) for k, q in (("p1", 1), ("p5", 5), ("p50", 50), ("p95", 95),
                                                                 ("p99", 99))} | {
        "min": round(float(x.min()), 3), "max": round(float(x.max()), 3), "n_ge_3R": int((x >= 3).sum()),
        "n_ge_5R": int((x >= 5).sum())}


# ---------------------------------------------------------------- clases, meta y cuenta

def clase_frecuencia(op_dia: float) -> str | None:
    if op_dia >= criterios.c("completa", "freq_frecuente"):
        return "frecuente"
    if op_dia >= criterios.c("completa", "freq_selectiva"):
        return "selectiva"
    return None


def meta(R_dia: float | None, pct_comp: float | None = None) -> dict:
    """La meta del 1 % (informada, nunca filtro): 2 R al día con 0,5 % de riesgo."""
    if R_dia is None:
        return {"R_dia": None, "pct_dia": None, "falta_R_dia": META_R, "falta_pct_dia": META_PCT,
                "texto": "Sin datos todavía. La meta es 1 % al día: 2 R al día con 0,5 % de riesgo (el riesgo no se sube)."}
    pct = R_dia * RISK
    fr, fp = META_R - R_dia, META_PCT - pct
    if fr <= 0:
        txt = (f"Llega a la meta: {_num(R_dia)} R al día ({_num(pct)} % de la cuenta con 0,5 % de riesgo). "
               "Ojo: la mejor de muchas siempre parece mejor de lo que es; lo dice la prueba en avance.")
    else:
        txt = (f"Le faltan {_num(fr)} R al día ({_num(fp)} % de la cuenta con 0,5 % de riesgo). El riesgo no se sube.")
    out = {"R_dia": round(R_dia, 4), "pct_dia": round(pct, 4), "falta_R_dia": round(fr, 4), "falta_pct_dia": round(fp, 4),
           "texto": txt}
    if pct_comp is not None:
        out["pct_dia_compuesto"] = round(pct_comp, 4)
    return out


def tramo(ops: list[dict], desde_dia: str, hasta_dia: str, eq0: float = 100.0, semilla: int = 20261029) -> dict:
    """Resumen de un tramo (dentro, fuera o avance): n, R medio, IC, por día natural, meta, colas y caídas."""
    dias = len(dias_entre(desde_dia, hasta_dia))
    n = len(ops)
    out = {"n": n, "dias": dias, "desde": desde_dia, "hasta": hasta_dia}
    if not n:
        out.update(R=None, op_dia=0.0, R_dia=0.0, pct_dia=0.0, meta=meta(0.0))
        return out
    R = media(ops)
    sumR = float(sum(r["R_f"] for r in ops))
    k5 = criterios.c("robustez", "sin_mayores")
    xs = np.sort(np.array([r["R_f"] for r in ops], float))
    s5 = float(xs[:-k5].sum()) if n > k5 else None   # informativo (el criterio es B2, en la robustez)
    sin5 = ({"R": round(s5 / (n - k5), 4), "R_dia": round(s5 / dias, 4), "pct_dia": round(s5 / dias * RISK, 4),
             "suma_R_de_las_5": round(float(xs[-k5:].sum()), 2)} if s5 is not None else None)
    ef = capital_final(ops, eq0)
    comp = ((ef / eq0) ** (1 / max(1, dias)) - 1) * 100 if ef > 0 else -100.0
    serie = serie_diaria(ops, desde_dia, hasta_dia)
    t, p = p_hac(serie)
    mo = momentos(serie)
    out.update(R=round(R, 4), R_sin_funding=round(media(ops, "R"), 4), ic95=ic_dias(ops, semilla=semilla),
               error_tipico_dias=(round(cluster_se(ops), 4) if cluster_se(ops) is not None else None),
               gana_pct=round(100 * float(np.mean([r["R_f"] > 0 for r in ops])), 1), op_dia=round(n / dias, 4),
               R_dia=round(sumR / dias, 4), pct_dia=round(sumR / dias * RISK, 4), suma_R=round(sumR, 2),
               pct_dia_compuesto=round(comp, 4), caida_max_R=round(max_dd_R(ops), 2),
               caida_max_pct=round(dd_cuenta(ops, eq0), 2), t_hac=round(t, 3), p_hac=round(p, 5),
               sr_dia=round(mo["sr"], 5), asimetria=round(mo["asimetria"], 4), curtosis=round(mo["curtosis"], 4),
               semanas=semanas(ops), colas=colas(ops), meta=meta(sumR / dias, comp), sin_5_mayores=sin5,
               largos={"n": sum(1 for r in ops if r["d"] > 0),
                       "R": (round(float(np.mean([r["R_f"] for r in ops if r["d"] > 0])), 4)
                             if any(r["d"] > 0 for r in ops) else None)},
               cortos={"n": sum(1 for r in ops if r["d"] < 0),
                       "R": (round(float(np.mean([r["R_f"] for r in ops if r["d"] < 0])), 4)
                             if any(r["d"] < 0 for r in ops) else None)},
               salidas=dict(Counter(r["why"] for r in ops).most_common()),
               cierres_por_tiempo=cierres_por_tiempo(ops),
               horas_media=round(float(np.mean([(r["ext"] - r["t_fill"]) / 3_600_000 for r in ops])), 1),
               stop_medio_pct=round(float(np.mean([r["sl"] for r in ops])), 3))
    return out


# ---------------------------------------------------------------- avance trimestral dentro de la hipótesis (K4)

def avance_trimestral(vars_ops: dict[str, list[dict]], corte_ms: int, fin_ms: int, min_prev: int | None = None) -> dict:
    """Cada trimestre natural del fuera de muestra se elige, entre las variantes vivas, la de mayor R medio con todo lo
    anterior (≥ min_prev operaciones previas) y se mide lo que dio en ese trimestre."""
    min_prev = min_prev or criterios.c("completa", "wf_min_previas")
    tri = []
    n_t, s_t = 0, 0.0
    for q, a, b in trimestres_entre(corte_ms, fin_ms):
        cand = []
        for vid, ops in vars_ops.items():
            prev = [r["R_f"] for r in ops if r["t"] < a]
            if len(prev) >= min_prev:
                cand.append((float(np.mean(prev)), len(prev), vid))
        if not cand:
            tri.append({"trimestre": q, "elegida": None, "n": 0, "R": None})
            continue
        cand.sort(key=lambda c: (-c[0], c[2]))
        vid = cand[0][2]
        test = [r["R_f"] for r in vars_ops[vid] if a <= r["t"] < b]
        tri.append({"trimestre": q, "elegida": vid, "R_previo": round(cand[0][0], 4), "n": len(test),
                    "R": round(float(np.mean(test)), 4) if test else None})
        n_t += len(test)
        s_t += float(sum(test))
    con = [x for x in tri if x["n"]]
    pos = sum(1 for x in con if x["R"] > 0)
    R = s_t / n_t if n_t else None
    pasa = bool(con and R is not None and R > 0 and pos >= math.ceil(len(con) / 2))
    return {"trimestres": tri, "n": n_t, "R": round(R, 4) if R is not None else None,
            "positivos": f"{pos} de {len(con)}", "pasa": pasa}


# ---------------------------------------------------------------- base emparejada (B1)

def base_dos_sentidos(ops: list[dict], opp: list[float | None], semilla: int, n_sorteos: int | None = None) -> dict:
    """Las mismas operaciones (horas, monedas y stops) con el sentido sorteado y la misma cuota de largos
    (medir_selectiva.base_azar): valor p (una cola) y percentil 95."""
    n_sorteos = n_sorteos or criterios.c("robustez", "base_sorteos")
    pares = [(r, o) for r, o in zip(ops, opp) if o is not None]
    if len(pares) < 2:
        return {"n": len(pares), "p": 1.0, "modo": "dos_sentidos"}
    same = np.array([r["R_f"] for r, _ in pares])
    other = np.array([o for _, o in pares])
    dl = np.array([r["d"] > 0 for r, _ in pares])
    obs = float(same.mean())
    rng = np.random.default_rng(semilla)
    means = np.empty(n_sorteos)
    for i in range(n_sorteos):
        perm = rng.permutation(dl)
        means[i] = np.where(perm == dl, same, other).mean()
    return {"modo": "dos_sentidos", "n": len(pares), "cuota_largos_pct": round(float(dl.mean() * 100), 1),
            "observado_R": round(obs, 4), "base_media_R": round(float(means.mean()), 4),
            "base_p95_R": round(float(np.percentile(means, 95)), 4),
            "p": round(float((1 + (means >= obs - 1e-12).sum()) / (1 + n_sorteos)), 4),
            "sentido_contrario_R": round(float(other.mean()), 4)}


def base_un_sentido(ops: list[dict], fondo: dict, semilla: int, n_sorteos: int | None = None) -> dict:
    """El mismo número de operaciones por moneda, en el mismo sentido y con la misma salida, a horas al azar del mismo
    día de la semana y la misma hora (± 1 h) de Madrid. fondo: {(moneda, día de la semana, hora): [R_f, ...]} simulado
    una vez por época y salida (etapas.fondo_azar)."""
    n_sorteos = n_sorteos or criterios.c("robustez", "base_sorteos")
    rng = np.random.default_rng(semilla)
    grupos = []
    for r in ops:
        dt = datetime.fromtimestamp(r["t"] / 1000, MAD)
        cand = []
        for hh in (dt.hour - 1, dt.hour, dt.hour + 1):
            cand += fondo.get((r["coin"], dt.weekday(), hh % 24), [])
        if cand:
            grupos.append(np.asarray(cand, float))
    if len(grupos) < 2:
        return {"modo": "un_sentido", "n": len(grupos), "p": 1.0}
    obs = float(np.mean([r["R_f"] for r in ops]))
    means = np.empty(n_sorteos)
    for i in range(n_sorteos):
        means[i] = np.mean([g[rng.integers(0, len(g))] for g in grupos])
    return {"modo": "un_sentido", "n": len(grupos), "observado_R": round(obs, 4),
            "base_media_R": round(float(means.mean()), 4), "base_p95_R": round(float(np.percentile(means, 95)), 4),
            "p": round(float((1 + (means >= obs - 1e-12).sum()) / (1 + n_sorteos)), 4)}


def modo_base(ops: list[dict]) -> str:
    if not ops:
        return "dos_sentidos"
    lg = sum(1 for r in ops if r["d"] > 0) / len(ops)
    return "dos_sentidos" if min(lg, 1 - lg) >= criterios.c("robustez", "minoria_min") else "un_sentido"


# ---------------------------------------------------------------- cuenta honesta del laboratorio (9.6)

def cuenta_honesta(por_variante: dict[str, dict], corte_ms: int, fin_ms: int, top: int = 10, min_n: int = 100) -> dict:
    """Cada trimestre del fuera de muestra se eligen las `top` mejores variantes con todo lo anterior (R medio con
    ≥ min_n operaciones previas, como laboratorio.walk_forward_q) y se mide qué dieron en el trimestre.
    por_variante: {vid: {trimestre: {"n", "suma_R"}}} (todas las variantes con etapa 2, dentro y fuera)."""
    tri, n_t, s_t = [], 0, 0.0
    for q, a, b in trimestres_entre(corte_ms, fin_ms):
        cand = []
        for vid, bq in por_variante.items():
            n = s = 0
            for qq, x in bq.items():
                if qq < q:
                    n += x["n"]
                    s += x["suma_R"]
            if n >= min_n:
                cand.append((s / n, n, vid))
        cand.sort(key=lambda c: (-c[0], c[2]))
        tests = [(c, por_variante[c[2]].get(q)) for c in cand[:top]]
        tests = [(c, t) for c, t in tests if t and t["n"]]
        if not tests:
            continue
        n = sum(t["n"] for _, t in tests)
        s = sum(t["suma_R"] for _, t in tests)
        n_t, s_t = n_t + n, s_t + s
        tri.append({"trimestre": q, "candidatas": len(cand), "top": len(tests), "n": n, "R": round(s / n, 4)})
    return {"trimestres": tri, "n": n_t, "R_medio": round(s_t / n_t, 4) if n_t else None,
            "texto": ("Sin datos aún: hace falta que haya variantes con 100 operaciones antes de cada trimestre."
                      if not n_t else
                      f"Las 10 mejores de cada trimestre (elegidas con lo anterior) dieron {_num(s_t / n_t, 3)} R por "
                      "operación en el trimestre siguiente: si es ≤ 0, lo que el laboratorio encuentra es ruido.")}


def por_trimestre(ops: list[dict]) -> dict:
    g = defaultdict(lambda: {"n": 0, "suma_R": 0.0})
    for r in ops:
        q = trimestre(r["t"])
        g[q]["n"] += 1
        g[q]["suma_R"] += r["R_f"]
    return {k: {"n": v["n"], "suma_R": round(v["suma_R"], 4)} for k, v in sorted(g.items())}
