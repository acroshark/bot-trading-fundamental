"""Skill radar: consulta la API local del bot (solo lectura) y la resume en texto corto para el modelo local."""
import json
import sys
import urllib.error
import urllib.request

BOT = "http://127.0.0.1:8765"
BOLSAS = ("tokio", "seul", "shanghai", "hongkong", "dubai", "frankfurt", "londres", "madrid", "nuevayork", "saopaulo")
COINS = ("BTC", "ETH", "SOL", "XRP")


def get(path: str):
    with urllib.request.urlopen(BOT + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def n(x, d=2):
    return "–" if x is None else f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(x, d=2):
    return "–" if x is None else ("+" if x > 0 else "") + n(x, d) + " %"


def arg(argv: list[str], name: str, default):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def flujo(st) -> list[str]:
    out = ["Flujo minorista (futuros de Binance y prima de Coinbase):"]
    for c, f in st["flujo_minorista"].items():
        out.append(f"- {c}: minorista {f['sesgo_minorista']}{' (EXTREMO)' if f['extremo'] else ''}, {f['pct_cuentas_largo']} % de cuentas en largo, "
                   f"ratio L/S {n(f['ratio_ls'])}, funding {n(f['funding_pct_8h'], 4)} %/8 h, interés abierto {n(f['oi_1h_pct'], 1)} % en 1 h, "
                   f"compras agresivas {f['compras_agresivas_pct']} %, prima Coinbase {n(f['prima_coinbase_pct'], 3)} %")
    return out


def agenda(st) -> list[str]:
    items = [f"- {e['cuando']}: {e['evento']} ({e['fuente']}, impacto {e['impacto']})" for e in st["agenda_macro"]]
    return ["Agenda macro (hora de Madrid):"] + (items or ["- Sin eventos próximos"])


def noticias(argv: list[str]) -> str:
    horas, minimo, bolsa = int(arg(argv, "--horas", 3)), int(arg(argv, "--min", 0)), arg(argv, "--bolsa", None)
    news = get(f"/api/noticias?horas={horas}&min={minimo}" + (f"&bolsa={bolsa}" if bolsa else ""))
    donde = f" que afectan a {bolsa}" if bolsa else ""
    if not news:
        return f"No hay noticias relevantes{donde} en las últimas {horas} h con importancia {minimo} o más."
    lines = [f"Noticias relevantes{donde} de las últimas {horas} h (más recientes primero):"]
    for x in news[:15]:
        imp = f"{x['importancia']}/10" if x.get("importancia") is not None else "sin triaje"
        rep = x.get("repercusion") or {}
        cred = x.get("credibilidad_ajustada")
        rep_txt = f" (dirección {rep.get('direccion')}, magnitud {rep.get('magnitud')})" if rep else ""
        lines.append(f"- {x['hora']} · {imp} · {x.get('tipo') or '?'} · {x.get('sentimiento') or '?'}{rep_txt}"
                     f"{f' · credibilidad {cred} %' if cred is not None else ''} · {', '.join(x.get('monedas') or [])} · "
                     f"{x['fuente']} ({x['nivel']}){' · ' + str(x['confirmaciones']) + ' confirmaciones' if x.get('confirmaciones') else ''}"
                     f"{'' if x.get('fresca', True) else ' · contexto'}\n  {x.get('resumen') or x['titular']}")
    return "\n".join(lines)


def balanza() -> str:
    """Hacia dónde empujan las noticias de las últimas 6 h: dirección × magnitud × credibilidad, con decaimiento."""
    news = get("/api/noticias?horas=6&min=4")
    by = {c: [0.0, 0.0] for c in COINS}
    top = []
    for x in news:
        rep = x.get("repercusion") or {}
        d, m = rep.get("direccion") or 0, rep.get("magnitud") or 0
        if not d or x.get("archivada"):
            continue
        w = abs(d) / 100 * m / 100 * (x.get("credibilidad_ajustada") or x.get("credibilidad") or 60) / 100 * (x.get("importancia") or 5) / 10
        coins = [a for a in rep.get("activos") or [] if a in COINS] or list(COINS)
        for c in coins:
            by[c][0 if d > 0 else 1] += w
        top.append((w, d, x))
    lines = ["Balanza de las noticias de las últimas 6 h (peso = dirección × magnitud × credibilidad × importancia):"]
    for c, (b, s) in by.items():
        tot = b + s
        lines.append(f"- {c}: " + (f"{n(b / tot * 100, 0)} % compra / {n(s / tot * 100, 0)} % venta (peso {n(tot, 2)})" if tot else "sin noticias con dirección"))
    lines.append("Las que más pesan:")
    for w, d, x in sorted(top, key=lambda t: -t[0])[:8]:
        lines.append(f"- {'▲' if d > 0 else '▼'} {x['hora']} · {x.get('resumen') or x['titular']} ({x['fuente']}, peso {n(w, 2)})")
    lines.append("Es una lectura de las noticias, no una orden ni un consejo de inversión.")
    return "\n".join(lines)


ESTADO_TXT = {"en_curso": "en curso", "terminada": "terminada", "por_abrir": "por abrir"}


def _rate(x) -> str:
    return "–" if x is None else f"{n(x * 100, 0)} %"


def _hora(ms) -> str:
    """Hora en UTC, sin ambigüedad (/api/estado da las de Madrid)."""
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%d-%m %H:%M UTC")


def aperturas_cripto(ca: dict) -> list[str]:
    """Cómo se mueve la cripto en cada apertura de bolsa (/api/cripto_aperturas): nunca el índice."""
    mk = ca.get("markets") or {}
    if not mk:
        estado = ca.get("estado")
        return ["Cripto en las aperturas de bolsa: " + ("el bot está calculando lo histórico; vuelve en un minuto."
                                                         if estado == "arrancando" else "sin datos todavía.")]
    lines = ["Cripto en cada apertura de bolsa (lo que hacen BTC, ETH, SOL y XRP, no el índice):"]
    for k, m in mk.items():
        st = m.get("state")
        if st in ("en_curso", "terminada"):
            src, rk, gk = m.get("coins") or {}, "ret_pct", "range_pct"
            what = (f"desde la apertura ({m.get('minutes_since_open')} min)" if st == "en_curso" else "a las 4 h")
            t0 = m.get("open_ms")
        else:
            last = m.get("last") or {}
            src, rk, gk, t0 = last.get("coins") or {}, "ret_4h_pct", "range_4h_pct", last.get("open_ms")
            what = "su última apertura, a las 4 h"
        coins = ", ".join(f"{c} {pct(v.get(rk))} (rango {n(v.get(gk))} %)" for c, v in src.items() if isinstance(v, dict))
        head = f"- {m.get('name', k)}: {ESTADO_TXT.get(st, st or '?')}"
        if isinstance(t0, (int, float)):
            head += f", abrió el {_hora(t0)}"
        lines.append(f"{head} · {what}: {coins or 'sin datos'}")
        if m.get("aviso"):
            lines.append(f"  aviso: {m['aviso']}")
        h = m.get("hist") or {}
        hc = h.get("coins") or {}
        if hc:
            lines.append(f"  histórico ({h.get('n')} aperturas desde {h.get('desde')}): rango ≥ 2 % en 4 h "
                         + ", ".join(f"{c} {_rate((v or {}).get('range_ge2_rate'))}" for c, v in hc.items())
                         + " · sube en 4 h " + ", ".join(f"{c} {_rate((v or {}).get('up_rate'))}" for c, v in hc.items())
                         + " (lo que pasó, no una previsión)")
    tot = ca.get("total") or {}
    if tot.get("coins"):
        lines.append(f"Todas las aperturas juntas ({tot.get('n')} desde {tot.get('desde')}): rango ≥ 2 % en 4 h "
                     + ", ".join(f"{c} {_rate((v or {}).get('range_ge2_rate'))}" for c, v in tot["coins"].items())
                     + f"; alguna de las 4, {_rate(tot.get('any_range_ge2_rate'))} (en 8 h, "
                     + f"{_rate(tot.get('any_range_ge2_8h_rate'))}). Que se mueva no dice hacia dónde.")
    ch = ca.get("chain") or {}
    if ch.get("base_rate") is not None:
        lines.append(f"Encadenamiento: tras una apertura en positivo, la siguiente sube el {_rate(ch.get('after_up_rate'))} "
                     f"de las veces (de media, el {_rate(ch.get('base_rate'))}; {ch.get('n')} casos): NO predice.")
        if ch.get("nota"):
            lines.append(f"  {ch['nota']}")
    return lines


def tendencia() -> str:
    """La cripto en cada apertura de bolsa y el libro de órdenes de cada moneda. Al usuario no le importa el índice."""
    try:
        lines = aperturas_cripto(get("/api/cripto_aperturas"))
    except urllib.error.HTTPError:
        lines = ["Cripto en las aperturas de bolsa: este bot aún no lo publica (hay que reiniciarlo con la versión nueva)."]
    t = get("/api/tendencia")
    lines.append("Libro de órdenes y operaciones cripto (OKX EU + Binance):")
    for c, o in ((t.get("orderflow") or {}).get("coins") or {}).items():
        lines.append(f"- {c}: tendencia {o.get('trend') or 'sin datos'}, desequilibrio del libro {n((o.get('imbalance') or 0) * 100, 0)} %, "
                     f"compras agresivas 5 min {n((o.get('buy_ratio_5m') or 0) * 100, 0)} %, CVD 15 min {n(o.get('cvd_15m'), 2)}, "
                     f"volumen 5 min {n((o.get('vol_5m_usd') or 0) / 1e6, 1)} M$ (×{n(o.get('vol_ratio'), 1)} de lo normal), "
                     f"15 min {pct(o.get('ret_15m_pct'))}, 60 min {pct(o.get('ret_60m_pct'))}")
    return "\n".join(lines)


def predicciones() -> str:
    """Solo las de cripto: las de los índices (kind «bolsa») ya no le interesan al usuario y no cuentan en la señal."""
    p = get("/api/predicciones")
    items = sorted((i for i in p.get("items") or [] if i.get("kind") == "cripto"),
                   key=lambda i: i.get("when_utc") or 0)[:12]
    lines = ["Predicciones de cripto del bot (no son órdenes):"]
    for i in items:
        drv = "; ".join(f"{d['factor']} ({'+' if d['effect_pp'] >= 0 else ''}{n(d['effect_pp'], 1)} pp)" for d in (i.get("drivers") or [])[:3])
        lines.append(f"- {i['label']} · {i['event']} · {i['horizon']}: {n(i['prob_up'] * 100, 0)} % de que suba, "
                     f"esperado {pct(i.get('expected_pct'))}, confianza {i['confidence']} (n={i.get('n_samples')}). {drv}")
    acc = ((p.get("accuracy") or {}).get("by_kind") or {}).get("cripto") or {}
    if acc.get("evaluated"):
        lines.append(f"Acierto medido de las de cripto: {n(acc['hit_rate'] * 100, 0)} % en {acc['evaluated']} evaluadas.")
    return "\n".join(lines if items else ["Aún no hay predicciones de cripto."])


def backtest() -> str:
    b = get("/api/backtest?curvas=0")
    st = b.get("strategies") or []
    if not st:
        return "El backtest aún no se ha generado."
    cnt = {}
    for s in st:
        cnt[s["verdict"]] = cnt.get(s["verdict"], 0) + 1
    lines = [f"Backtest con datos reales: {len(st)} estrategias · " + ", ".join(f"{k} {v}" for k, v in cnt.items()) + ".",
             "Mejores operables (tras comisiones):"]
    ops = sorted((s for s in st if s.get("actionable")), key=lambda s: -(s.get("avg_ret_pct") or -99))[:8]
    for s in ops:
        lines.append(f"- {s['name']}: {s['verdict']}, n={s['n']}, acierto {n(s['hit_rate'] * 100, 0)} % (base {n(s['base_rate'] * 100, 0)} %), "
                     f"media {pct(s.get('avg_ret_pct'), 3)} por operación, fuera de muestra {n((s.get('oos') or {}).get('hit_rate', 0) * 100, 0)} %")
    try:
        a = get("/api/aperturas")
        best = (a.get("backtest") or {}).get("best") or []
        if best:
            lines.append("Motor de aperturas (velas de 1 min):")
            lines += [f"- {x.get('name') or x.get('id')}: {json.dumps({k: v for k, v in x.items() if k not in ('name', 'id')}, ensure_ascii=False)[:220]}" for x in best[:5]]
    except (urllib.error.URLError, ValueError):
        pass
    return "\n".join(lines)


def cartera() -> str:
    pf = get("/api/cartera") or {}
    if not pf:
        return "La cartera paper aún no está disponible."
    lines = [f"Cartera paper (dinero ficticio): capital {n(pf.get('equity'))} USDC, hoy {pct(pf.get('day_pnl_pct'))} ({n(pf.get('day_pnl'))} USDC)."]
    for p in pf.get("positions") or []:
        lines.append(f"- {'Compra' if p['side'] == 'long' else 'Venta'} {p['coin']} a {n(p['entry'], 4)}, stop {n(p['sl'], 4)}, "
                     f"estrategia {p.get('strategy')}, ventana {p.get('window')}")
    if not pf.get("positions"):
        lines.append("Sin posiciones abiertas.")
    return "\n".join(lines)


def aperturas() -> str:
    try:
        a = get("/api/aperturas")
    except urllib.error.HTTPError:
        return "El motor de aperturas aún no está en marcha en el bot."
    lines = ["Motor de aperturas (señal de noticias → confirmación en la apertura → entrada → stop dinámico → reentradas):"]
    for w in a.get("windows") or []:
        lines.append(f"- {w.get('name')} · {w.get('state')}")
        for c, x in (w.get("coins") or {}).items():
            lines.append(f"  {c}: señal {x.get('signal')}, fuerza {n(x.get('strength'), 2)}, rvol {n(x.get('rvol'), 1)}, "
                         f"entradas {x.get('entries', 0)} · {x.get('last_why') or x.get('why') or ''}")
    return "\n".join(lines if len(lines) > 1 else lines + ["- Ninguna ventana activa ahora."])


def main(argv: list[str]) -> str:
    cmd = argv[0] if argv else "estado"
    simple = {"balanza": balanza, "tendencia": tendencia, "predicciones": predicciones, "backtest": backtest,
              "cartera": cartera, "aperturas": aperturas}
    if cmd == "noticias":
        return noticias(argv)
    if cmd in simple:
        return simple[cmd]()
    st = get("/api/estado")
    if cmd == "flujo":
        return "\n".join(flujo(st))
    if cmd == "agenda":
        return "\n".join(agenda(st))
    precios = ", ".join(f"{c} {n(p, 4 if p < 10 else 2)}" for c, p in st["precios_usdc"].items())
    caidas = [f["nombre"] for f in st["fuentes"] if f["ok"] is False]
    masivas = st.get("fuentes_masivas") or {}
    return "\n".join([f"Datos a las {st['hora_madrid']} (Madrid). Precios en USDC (OKX EU): {precios}."]
                     + flujo(st) + agenda(st)
                     + [f"Fuentes directas: {sum(1 for f in st['fuentes'] if f['ok'])} de {len(st['fuentes'])} funcionando"
                        + (f"; caídas: {', '.join(caidas)}" if caidas else "") + "."
                        + (f" Feeds de noticias: {masivas.get('ok')} de {masivas.get('feeds')} funcionando." if masivas else ""),
                        f"IA local: {'encendida' if st['ia_local']['encendida'] else 'apagada'} ({st['ia_local']['estado'] or 'sin detalle'})."])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")   # con -I no vale PYTHONIOENCODING y Windows usaría cp1252
    try:
        print(main(sys.argv[1:]))
    except Exception as e:  # el modelo debe saber que el bot no responde, no ver una traza
        print(f"ERROR: el bot no responde en {BOT} ({type(e).__name__}: {e})")
