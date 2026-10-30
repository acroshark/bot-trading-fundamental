"""Informe de cobertura y calidad del histórico de noticias de 3 años (bot/noticias_historia.py). SOLO PAPER: mide los
datos; no prueba ninguna estrategia (eso lo hace el corredor del laboratorio vivo y cuenta en su número de pruebas).

Qué mide (noticias_historia/informe.json e informe.md):
1. Descarga: días de GDELT hechos y archivos que faltan; meses de los 24 medios de WordPress.
2. Cobertura por mes, por medio y por moneda: filas cripto y macro, historias (deduplicadas como la mesa), racimos
   seleccionados para la IA local y triados.
3. Retraso: de la primera publicación a cuándo se pudo usar (el racimo), por fuente; y frente al precio, si el sentido
   de la IA local coincide con lo que la moneda ya hizo en las 12 h previas o con lo que hace en las 4 h siguientes
   («los titulares cuentan lo que ya pasó», docs/05, sección 22).
4. Que repite lo ya medido:
   - 91 días (29-07 → 27-10-2026, controles/fuentes): acierto a 4 h ajustado (rendimiento − media de los rendimientos
     a 4 h de todos los minutos del día UTC de esa moneda, desde el minuto en que se pudo usar), frente al 48,0 % de los
     787 titulares de Claude y el 49,2 % de los titulares públicos; y cuántos de los 48.272 titulares de aquel estudio
     están en este histórico.
   - 54 movimientos bruscos (controles/bruscos): el consenso de las historias triadas de las 12 h antes (signo de la
     suma de sus sentidos, de esa moneda o generales) frente al 46,5 % de los medios (20 de 43).
Uso: python -X utf8 -m bot.noticias_informe"""
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import config
from . import noticias_historia as nh

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
REF_91 = {"claude_787": 0.4799, "publicos_91d": 0.492, "texto": "controles/fuentes/medir_claude.json (787 titulares, "
          "1.417 titular×moneda) y docs/05, sección 20 (titulares públicos de 91 días)"}
REF_BRUSCOS = {"aciertos": 20, "con_consenso": 43, "acierto": 0.4651, "previas_12h_mismo_sentido": 0.7287,
               "texto": "controles/bruscos/resultado.json (54 episodios, 2.000 titulares con el sentido de Claude)"}


def _ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _mes(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m")


def wilson_bajo(k: float, n: float, z: float = 1.645) -> float:
    if n <= 0:
        return 0.0
    p = k / n
    den = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0))
    return (c - r) / den


class Precios:
    """Cierres de 1 min de Binance (los de HIST_DIR/1m que usa el laboratorio), por moneda, para el periodo."""

    def __init__(self, desde_ms: int, hasta_ms: int):
        from .lab_vivo import datos
        self.t, self.c, self.deriva = {}, {}, {}
        meses = datos.meses_entre(desde_ms - DAY, hasta_ms + 2 * DAY)
        for coin in config.COINS:
            v = datos.cargar_velas(datos.archivos_moneda(coin, meses), desde_ms - DAY, hasta_ms + 2 * DAY)
            t, c = np.asarray(v.t, np.int64), np.asarray(v.c, float)
            self.t[coin], self.c[coin] = t, c
            # deriva del día: media del rendimiento a 4 h de todos sus minutos (como controles/fuentes)
            r = self.cierre(coin, t + MIN + 240 * MIN) / c - 1
            ok = np.isfinite(r)
            dia = (t[ok] // DAY).astype(np.int64)
            if len(dia):
                d0 = int(dia.min())
                s = np.bincount(dia - d0, weights=r[ok])
                n = np.bincount(dia - d0)
                self.deriva[coin] = (d0, np.where(n > 0, s / np.maximum(n, 1), np.nan))

    def cierre(self, coin: str, t_ms) -> np.ndarray:
        """Cierre de la última vela de 1 min cerrada en t (como mucho 5 min antes); NaN si no hay."""
        t, c = self.t.get(coin), self.c.get(coin)
        ts = np.atleast_1d(np.asarray(t_ms, np.int64))
        out = np.full(len(ts), np.nan)
        if t is None or not len(t):
            return out
        k = np.searchsorted(t, ts - MIN, "right") - 1
        ok = (k >= 0) & (ts - MIN - t[np.clip(k, 0, len(t) - 1)] <= 5 * MIN)
        out[ok] = c[k[ok]]
        return out

    def deriva_en(self, coin: str, dia: np.ndarray) -> np.ndarray:
        d0, v = self.deriva.get(coin, (0, np.zeros(0)))
        i = np.asarray(dia, np.int64) - d0
        out = np.full(len(i), np.nan)
        ok = (i >= 0) & (i < len(v))
        out[ok] = v[i[ok]]
        return out


def _unidades(hs: list[dict], tri: dict) -> list[dict]:
    """historia×moneda con sentido (la unidad de fiables): las monedas del triaje o, si no nombra ninguna, las cuatro."""
    out = []
    for h in hs:
        x = tri.get(h["id"])
        if not x:
            continue
        t = x["triage"]
        d = int((t.get("repercusion") or {}).get("direccion") or 0)
        sen = 1 if d > 15 else -1 if d < -15 else 0
        mons = [c for c in t.get("monedas") or [] if c in config.COINS]
        for c in mons or list(config.COINS):
            out.append({"id": h["id"], "uso": h["uso"], "t0": h["t0"], "coin": c, "sen": sen, "dom": h["dom"],
                        "fuente": h["fuente"], "clase": h["clase"], "tipo": t.get("tipo"), "general": not mons,
                        "imp": int(t.get("importancia") or 0)})
    return out


def _aciertos(us: list[dict], pr: Precios) -> list[dict]:
    """Añade r240 (desde el minuto en que se pudo usar), la deriva del día y r_prev12 (las 12 h antes de publicarse)."""
    por = defaultdict(list)
    for u in us:
        por[u["coin"]].append(u)
    for coin, lst in por.items():
        uso = np.array([u["uso"] for u in lst], np.int64)
        t0 = np.array([u["t0"] for u in lst], np.int64)
        p0, p4 = pr.cierre(coin, uso), pr.cierre(coin, uso + 240 * MIN)
        q0, q12 = pr.cierre(coin, t0 - 12 * H), pr.cierre(coin, t0)
        der = pr.deriva_en(coin, uso // DAY)
        with np.errstate(divide="ignore", invalid="ignore"):
            r4, rp = p4 / p0 - 1, q12 / q0 - 1
        for k, u in enumerate(lst):
            u["r240"] = float(r4[k]) if np.isfinite(r4[k]) else None
            u["der"] = float(der[k]) if np.isfinite(der[k]) else None
            u["rprev12"] = float(rp[k]) if np.isfinite(rp[k]) else None
    return us


def _acierto(us: list[dict]) -> dict:
    v = [u for u in us if u["sen"] and u.get("r240") is not None and u.get("der") is not None]
    if not v:
        return {"n": 0}
    adj = sum(1 for u in v if u["sen"] * (u["r240"] - u["der"]) > 0)
    bru = sum(1 for u in v if u["sen"] * u["r240"] > 0)
    horas = len({u["uso"] // H for u in v})
    hs = {u["id"] for u in v}
    return {"n": len(v), "historias": len(hs), "horas": horas, "acierto_4h_ajustado": round(adj / len(v), 4),
            "acierto_4h_bruto": round(bru / len(v), 4),
            "wilson90_inf_horas": round(wilson_bajo(adj / len(v) * horas, horas), 4)}


def _medios(us: list[dict]) -> dict:
    """Los medios (el de la primera pieza de cada historia) por su acierto ajustado a 4 h en todo el tramo: los cortes
    del filtro de medios (familias/base.MEDIOS): con muestra (≥ 20 historias en ≥ 10 días), malos (cota alta de Wilson
    al 90 % < 50 %) y buenos (acierto ≥ 60 % y cota baja ≥ 50 %)."""
    por_h = defaultdict(list)
    for u in us:
        if u["sen"] and u.get("r240") is not None and u.get("der") is not None:
            por_h[(u["id"], nh.medio_corto(u["dom"]), u["uso"] // DAY)].append(u["sen"] * (u["r240"] - u["der"]) > 0)
    por_m = defaultdict(lambda: [0, 0.0, set()])
    for (hid, med, dia), hits in por_h.items():
        x = por_m[med]
        x[0] += 1
        x[1] += sum(hits) / len(hits)
        x[2].add(dia)
    con, malos, buenos = [], [], []
    for med, (n, k, ds) in por_m.items():
        if n >= 20 and len(ds) >= 10:
            fila = (med, n, round(k / n, 3))
            con.append(fila)
            if 1 - wilson_bajo(n - k, n) < 0.5:
                malos.append(fila)
            if k / n >= 0.6 and wilson_bajo(k, n) >= 0.5:
                buenos.append(fila)
    return {"medios": len(por_m), "con_muestra": len(con), "malos": len(malos), "buenos": len(buenos),
            "lista_malos": sorted(malos, key=lambda x: x[2])[:20], "lista_buenos": sorted(buenos, key=lambda x: -x[2])[:20],
            "mejores_con_muestra": sorted(con, key=lambda x: -x[2])[:10]}


def informe(base: Path = nh.BASE, desde: str = nh.DESDE, hasta: str = nh.HASTA) -> dict:
    """Todo el informe (lo que aún no se puede medir sale como «pendiente»)."""
    gd = base / "gdelt" / "dias"
    dias = nh.dias_entre(desde, hasta)
    res: dict = {"generado": datetime.now(timezone.utc).isoformat(timespec="seconds"), "periodo": [desde, hasta]}
    # 1. descarga
    hechos, faltan_arch, sin_lista, por_mes = 0, [], [], defaultdict(Counter)
    for d in dias:
        p = gd / f"{d}.json"
        if not p.exists():
            sin_lista.append(d)
            continue
        r = json.loads(p.read_text("utf-8"))
        hechos += bool(r.get("completo"))
        if r.get("faltan"):
            faltan_arch.append({"dia": d, "faltan": len(r["faltan"])})
        m = d[:7]
        por_mes[m]["gdelt_dias"] += 1
        por_mes[m]["gdelt_archivos"] += r.get("archivos_ok", 0)
        for k in ("filas", "con_titular", "cripto", "macro", "tema_cripto_gdelt"):
            por_mes[m][f"gdelt_{k}"] += (r.get("filas") or {}).get(k, 0)
    lista = nh.lista_gdelt(desde, hasta, base) if (base / "gdelt" / "lista").exists() else []
    con_archivos = {f"{f[0][:4]}-{f[0][4:6]}-{f[0][6:8]}" for f in lista}
    res["descarga"] = {"gdelt": {"dias_periodo": len(dias), "dias_hechos": hechos, "dias_con_archivos_en_gdelt":
                                 len(con_archivos & set(dias)), "dias_sin_archivos_en_gdelt":
                                 sorted(set(dias) - con_archivos), "dias_por_hacer": len(sin_lista),
                                 "dias_con_archivos_que_fallan": faltan_arch[:50]}}
    wp_res = {}
    for s in nh.WP:
        car = base / "wp" / nh.medio_de(s)
        meses_ok, posts, guard = 0, 0, 0
        for p in sorted(car.glob("*.json")):
            r = json.loads(p.read_text("utf-8"))
            if p.stem < desde[:7] or p.stem > hasta[:7]:
                continue
            meses_ok += 1
            posts += (r.get("cuentas") or {}).get("posts", 0)
            guard += r.get("guardadas", 0)
            por_mes[p.stem]["wp_posts"] += (r.get("cuentas") or {}).get("posts", 0)
            por_mes[p.stem]["wp_cripto"] += (r.get("cuentas") or {}).get("cripto", 0)
            por_mes[p.stem]["wp_macro"] += (r.get("cuentas") or {}).get("macro", 0)
        wp_res[nh.medio_de(s)] = {"meses": meses_ok, "posts": posts, "guardadas": guard}
    res["descarga"]["wp"] = {"sitios": len(nh.WP), "meses_periodo": len(nh.meses_entre(desde, hasta)),
                             "por_sitio": wp_res}
    # 2. historias, selección y triaje
    tri = nh.triadas(base)
    hs_all, sel = [], []
    medios_abren, medios_racimo, por_moneda = Counter(), Counter(), defaultdict(Counter)
    for p in sorted((base / "historias").glob("*.jsonl.gz")):
        for h in nh.leer_jsonl_gz(p):
            m = _mes(h["s0"])
            por_mes[m][f"historias_{h['clase']}"] += 1
            medios_abren[nh.medio_corto(h["dom"])] += 1
            for c in h.get("mon") or []:
                por_moneda[c][m[:4]] += 1
            u = nh.seleccion(h)
            if u is not None:
                h = {**h, "uso": u}
                sel.append(h)
                por_mes[m][f"seleccionadas_{h['clase']}"] += 1
                por_mes[m]["triadas"] += h["id"] in tri
                for d, _, _ in h["medios"]:
                    medios_racimo[nh.medio_corto(d)] += 1
                hs_all.append(h)
    res["cobertura_mes"] = {m: dict(v) for m, v in sorted(por_mes.items())}
    res["medios"] = {"abren_historia": medios_abren.most_common(40), "en_racimos": medios_racimo.most_common(40),
                     "distintos_que_abren": len(medios_abren)}
    res["monedas_nombradas_por_anyo"] = {c: dict(sorted(v.items())) for c, v in por_moneda.items()}
    res["seleccion"] = {"regla": {"racimo_n": nh.RACIMO_N, "minutos": nh.RACIMO_MIN}, "seleccionadas": len(sel),
                        "triadas": sum(1 for h in sel if h["id"] in tri),
                        "por_clase": dict(Counter(h["clase"] for h in sel))}
    # 3. retraso de publicación (sin precio)
    ret = defaultdict(list)
    for h in sel:
        ret[h["fuente"]].append((h["uso"] - h["t0"]) / MIN)
    res["retraso_publicacion_min"] = {f: {"n": len(v), "p10": round(float(np.percentile(v, 10)), 1),
                                          "p50": round(float(np.percentile(v, 50)), 1),
                                          "p90": round(float(np.percentile(v, 90)), 1)} for f, v in ret.items() if v}
    # 4. con el precio: solo con historias triadas
    tr_sel = [h for h in sel if h["id"] in tri]
    if not tr_sel:
        res["precio"] = "pendiente: aún no hay historias triadas"
        return res
    a = min(h["uso"] for h in tr_sel)
    b = max(h["uso"] for h in tr_sel)
    pr = Precios(a, b)
    us = _aciertos(_unidades(tr_sel, tri), pr)
    con = [u for u in us if u["sen"] and u.get("rprev12") is not None and u.get("r240") is not None]
    res["retraso_frente_al_precio"] = {
        "n": len(con),
        "sentido_igual_que_las_12h_previas": round(sum(1 for u in con if u["sen"] * u["rprev12"] > 0) / len(con), 4)
        if con else None,
        "sentido_igual_que_las_4h_siguientes": round(sum(1 for u in con if u["sen"] * u["r240"] > 0) / len(con), 4)
        if con else None,
        "media_abs_12h_previas_pct": round(float(np.mean([abs(u["rprev12"]) for u in con])) * 100, 3) if con else None,
        "media_abs_4h_siguientes_pct": round(float(np.mean([abs(u["r240"]) for u in con])) * 100, 3) if con else None,
        "referencia": REF_BRUSCOS["previas_12h_mismo_sentido"]}
    res["acierto_todo"] = _acierto(us)
    res["acierto_por_anyo"] = {y: _acierto([u for u in us if _mes(u["uso"])[:4] == y])
                               for y in sorted({_mes(u["uso"])[:4] for u in us})}
    res["acierto_por_clase"] = {c: _acierto([u for u in us if u["clase"] == c]) for c in nh.CLASES}
    # 4a. los 91 días
    a91, b91 = _ms(nh.VENTANA_91[0]), _ms(nh.VENTANA_91[1])
    u91 = [u for u in us if a91 <= u["uso"] < b91]
    por_medio = defaultdict(list)
    for u in u91:
        por_medio[nh.medio_corto(u["dom"])].append(u)
    res["replica_91"] = {"ventana": list(nh.VENTANA_91), "todas": _acierto(u91), "referencia": REF_91,
                         "por_medio": {m: _acierto(v) for m, v in sorted(por_medio.items(), key=lambda x: -len(x[1]))
                                       if len(v) >= 20},
                         "titulares_del_estudio_en_el_historico": _solape_91(base)}
    # 4b. los 54 bruscos
    res["replica_bruscos"] = _bruscos(us)
    # 5. los medios por su acierto (los cortes del filtro de medios), en todo el tramo y en los 91 días
    res["medios_por_acierto"] = {"todo": _medios(us), "dias_91": _medios(u91)}
    return res


def _solape_91(base: Path) -> dict:
    """Cuántos titulares de historia_noticias/noticias.jsonl (91 días, 22 medios) están en las filas del histórico."""
    p = config.DATA_DIR / "historia_noticias" / "noticias.jsonl"
    if not p.exists():
        return {"pendiente": "sin historia_noticias/noticias.jsonl"}
    url = lambda u: (u or "").split("?")[0].rstrip("/").lower().replace("https://", "").replace("http://", "") \
        .replace("www.", "")   # noqa: E731
    nuestras = set()
    for mes in ("2026-07", "2026-08", "2026-09", "2026-10"):
        for s in nh.WP:
            f = base / "wp" / nh.medio_de(s) / f"{mes}.jsonl.gz"
            if f.exists():
                nuestras.update(url(r.get("url")) for r in nh.leer_jsonl_gz(f))
    gd = base / "gdelt" / "dias"
    for f in sorted(gd.glob("2026-0[7-9]-*.jsonl.gz")) + sorted(gd.glob("2026-10-*.jsonl.gz")):
        nuestras.update(url(r.get("url")) for r in nh.leer_jsonl_gz(f))
    tot, dentro, filt, filt_dentro, por_medio = 0, 0, 0, 0, defaultdict(lambda: [0, 0, 0, 0])
    with open(p, encoding="utf-8") as f:
        for x in f:
            try:
                r = json.loads(x)
            except ValueError:
                continue
            tot += 1
            ok = url(r.get("url")) in nuestras
            dentro += ok
            pasa = nh.clasificar(r.get("titular") or "", "wp:" + (r.get("medio") or "")) is not None
            filt += pasa
            filt_dentro += pasa and ok
            pm = por_medio[r.get("medio") or "?"]
            pm[0] += 1
            pm[1] += ok
            pm[2] += pasa
            pm[3] += pasa and ok
    return {"titulares": tot, "en_el_historico": dentro, "parte": round(dentro / tot, 4) if tot else None,
            "que_el_filtro_da_cripto_o_macro": filt, "de_esos_en_el_historico": filt_dentro,
            "parte_de_esos": round(filt_dentro / filt, 4) if filt else None,
            "nota": "se comparan las URL; el histórico solo guarda lo que el filtro del bot da como cripto o macro",
            "por_medio": {m: {"titulares": v[0], "en_el_historico": v[1], "filtro_cripto_o_macro": v[2],
                              "de_esos_en_el_historico": v[3]} for m, v in sorted(por_medio.items())}}


def _bruscos(us: list[dict]) -> dict:
    p = config.BACKTEST_DIR / "controles" / "bruscos" / "episodios.json"
    q = config.BACKTEST_DIR / "controles" / "bruscos" / "resultado.json"
    if not p.exists():
        return {"pendiente": "sin controles/bruscos/episodios.json"}
    d = json.loads(p.read_text("utf-8"))
    medidos = None
    if q.exists():
        r = json.loads(q.read_text("utf-8"))
        medidos = {e["episodio"] for e in (r.get("episodios") or {}).get("lista_160") or [] if e.get("medido")}
    eps = {e["id"]: e for e in d["episodios"]}
    por_coin = defaultdict(list)
    for u in us:
        if u["sen"]:
            por_coin[u["coin"]].append(u)
    out = {}
    for nombre, ids in (("los_54", sorted(medidos or [])), ("los_160", sorted(eps))):
        cons, ok_, sin = 0, 0, 0
        for i in ids:
            e = eps.get(i)
            if not e:
                continue
            a, b = e["ventana_antes"]["desde_ms"], e["ventana_antes"]["hasta_ms"]
            s = sum(u["sen"] for u in por_coin[e["moneda"]] if a <= u["uso"] < b)
            if s == 0:
                sin += 1
                continue
            cons += 1
            ok_ += int(np.sign(s) == e["sentido"])
        out[nombre] = {"episodios": len(ids), "con_consenso": cons, "aciertos": ok_,
                       "acierto": round(ok_ / cons, 4) if cons else None, "sin_noticias_o_empate": sin}
    ctrl = [v for v in d["ventanas"] if v.get("tipo") == "control"]
    alarmas = 0
    for v in ctrl:
        e = eps.get(v["episodio"])
        s = sum(u["sen"] for u in por_coin[v["moneda"]] if v["desde_ms"] <= u["uso"] < v["hasta_ms"])
        alarmas += int(e is not None and s != 0 and np.sign(s) == e["sentido"])
    out["controles"] = {"ventanas": len(ctrl), "consenso_en_el_sentido_del_episodio": alarmas,
                        "parte": round(alarmas / len(ctrl), 4) if ctrl else None}
    out["referencia"] = REF_BRUSCOS
    return out


def a_markdown(r: dict) -> str:
    L = [f"# Histórico de noticias de 3 años: cobertura y calidad ({r['generado'][:16].replace('T', ' ')} UTC)", ""]
    g = r["descarga"]["gdelt"]
    L += ["## Descarga", "",
          f"- GDELT (GKG en inglés): {g['dias_hechos']} de {g['dias_periodo']} días hechos; "
          f"{len(g['dias_sin_archivos_en_gdelt'])} días sin archivos en GDELT; {g['dias_por_hacer']} por hacer.",
          f"- 24 medios de cripto (WordPress): {sum(v['meses'] for v in r['descarga']['wp']['por_sitio'].values())} "
          f"meses-medio de {24 * r['descarga']['wp']['meses_periodo']}.", ""]
    s = r["seleccion"]
    L += ["## Selección para la IA local", "",
          f"- Regla: {s['regla']}; seleccionadas {s['seleccionadas']}, triadas {s['triadas']} "
          f"({s['por_clase']}).", ""]
    if isinstance(r.get("precio"), str):
        L += [f"- Con el precio: {r['precio']}.", ""]
        return "\n".join(L)
    rf = r["retraso_frente_al_precio"]
    L += ["## Retraso frente al precio", "",
          f"- Sentido de la IA igual que las 12 h previas: {rf['sentido_igual_que_las_12h_previas']}; igual que las 4 h "
          f"siguientes: {rf['sentido_igual_que_las_4h_siguientes']} (n = {rf['n']}; en los bruscos, "
          f"{REF_BRUSCOS['previas_12h_mismo_sentido']} iban con las 12 h previas).", ""]
    a = r["replica_91"]["todas"]
    L += ["## ¿Repite lo medido?", "",
          f"- 91 días: acierto a 4 h ajustado {a.get('acierto_4h_ajustado')} (n = {a.get('n')}), frente a "
          f"{REF_91['claude_787']} (Claude) y {REF_91['publicos_91d']} (titulares públicos).",
          f"- Bruscos (54): {r['replica_bruscos'].get('los_54')} frente a 20 de 43 (0,4651).", ""]
    mp = r.get("medios_por_acierto", {}).get("todo", {})
    L += ["## Medios por su acierto", "",
          f"- {mp.get('con_muestra')} de {mp.get('medios')} medios con muestra; malos (claramente < 50 %): "
          f"{mp.get('malos')}; buenos (≥ 60 %): {mp.get('buenos')}.", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    r = informe()
    nh.escribir_atomico(nh.BASE / "informe.json", json.dumps(r, ensure_ascii=False, indent=1).encode())
    nh.escribir_atomico(nh.BASE / "informe.md", a_markdown(r).encode())
    print(a_markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
