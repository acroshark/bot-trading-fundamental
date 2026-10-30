"""Skill radar: consulta la API local del bot (solo GET, solo lectura) y la resume en texto corto para el modelo local.

Todo es paper: los textos describen cifras medidas, nunca prometen rentabilidad ni dan consejos de inversión. El motor de
aperturas opera con horquilla pura (stop y objetivo fijos, sin break-even ni stop dinámico), como piden las reglas del
usuario. Los comandos permitidos están en openclaw-trading/instalar.py (SIMPLES y las opciones de cada uno)."""
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


FUENTE_FUERA = ("silenciada", "retirada")   # el motor no las cuenta (contrato 2.6): la balanza tampoco
CLASES_NO_CRIPTO = ("macro", "contexto")    # necesitan que la cripto las confirme (reflejo, contrato 2.6)


def _credibilidad(x: dict) -> float:
    """La de /api/noticias: credibilidad_ajustada (nivel, confirmaciones, peso del medio y de la fuente) AUNQUE valga 0,
    que es lo que trae una fuente silenciada o una macro sin reflejo confirmado en la cripto. Sin ella, la del triaje;
    sin ninguna, 60."""
    for k in ("credibilidad_ajustada", "credibilidad"):
        if x.get(k) is not None:
            return x[k]
    return 60


def _macro_sin_reflejo(x: dict) -> bool:
    """Macro o de contexto que la cripto no ha confirmado. /api/noticias ya les pone credibilidad_ajustada 0; esto solo
    cubre las que llegan sin credibilidad (sin ajustar no se les pudo aplicar la regla)."""
    return x.get("clase") in CLASES_NO_CRIPTO and (x.get("reflejo") or {}).get("estado") != "confirmada"


def balanza() -> str:
    """Hacia dónde empujan las noticias de las últimas 6 h: dirección × magnitud × credibilidad ajustada × importancia.
    Cuenta lo mismo que el motor: fuera las de fuentes silenciadas o retiradas y las de credibilidad ajustada 0."""
    news = get("/api/noticias?horas=6&min=4")
    by = {c: [0.0, 0.0] for c in COINS}
    top, sin_fuente, sin_peso = [], 0, 0
    for x in news:
        rep = x.get("repercusion") or {}
        d, m = rep.get("direccion") or 0, rep.get("magnitud") or 0
        if not d or x.get("archivada"):
            continue
        if x.get("estado_fuente") in FUENTE_FUERA:
            sin_fuente += 1
            continue
        if x.get("credibilidad_ajustada") is None and _macro_sin_reflejo(x):
            sin_peso += 1
            continue
        imp = x.get("importancia")
        w = abs(d) / 100 * m / 100 * _credibilidad(x) / 100 * (5 if imp is None else imp) / 10
        if w <= 0:   # credibilidad ajustada 0: macro sin reflejo confirmado o medio con peso 0
            sin_peso += 1
            continue
        coins = [a for a in rep.get("activos") or [] if a in COINS] or list(COINS)
        for c in coins:
            by[c][0 if d > 0 else 1] += w
        top.append((w, d, x))
    lines = ["Balanza de las noticias de las últimas 6 h (peso = dirección × magnitud × credibilidad ajustada × importancia):"]
    for c, (b, s) in by.items():
        tot = b + s
        lines.append(f"- {c}: " + (f"{n(b / tot * 100, 0)} % compra / {n(s / tot * 100, 0)} % venta (peso {n(tot, 2)})" if tot else "sin noticias con dirección"))
    if top:
        lines.append("Las que más pesan:")
    for w, d, x in sorted(top, key=lambda t: -t[0])[:8]:
        lines.append(f"- {'▲' if d > 0 else '▼'} {x['hora']} · {x.get('resumen') or x['titular']} ({x['fuente']}, peso {n(w, 2)})")
    if sin_fuente or sin_peso:
        lines.append(f"No cuentan, como en el motor: {sin_fuente} de fuentes silenciadas o retiradas y {sin_peso} con "
                     "credibilidad ajustada 0 (macro o geopolítica que la cripto no ha confirmado, o medio con peso 0).")
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


PAPER = ("Todo es paper (dinero ficticio): se mide y se enseñan las cifras tal cual. No es una promesa de rentabilidad "
         "ni un consejo de inversión.")
E2_LAB = "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig"   # = apertura.LAB_REF (E2 con la salida del laboratorio)
PRESET_TXT = {"seis": "6 diarias", "impulso": "E2 · impulso"}
FUENTE_PARAMS = {"preset_impulso": "el preset por defecto (E2 · impulso)",
                 "preset_impulso+entorno": "el preset E2 con cambios de BOT_IMPULSO_PARAMS",
                 "manual": "parámetros puestos a mano al arrancar",
                 "evolucion": "la evolución (estrategia_activa.json)",
                 "usuario": "decisión del usuario (scripts/activar_estrategia.py)",
                 "backtest": "la recomendación del backtest (modo «ruptura», v2)",
                 "preset_v2": "el preset V2 (modo «ruptura», de referencia)"}
INST_TXT = {"spot": "al contado", "perp": "en perpetuo"}
BTC_TXT = {"a_favor": "solo a favor de la tendencia de BTC", "no_contra": "nunca contra la tendencia de BTC",
           "ninguno": "sin filtro de BTC"}
NEWS_TXT = {"no_contra": "no si las noticias van claramente en contra", "a_favor": "solo con noticias a favor",
            "etiqueta": "las noticias solo etiquetan",
            "fiables": "veto si una noticia de un grupo fiable va en contra (solo los que pasan la puerta para operar)"}
FASE_TXT = {"esperando_apertura": "esperando la apertura", "midiendo_impulso": "midiendo el impulso",
            "esperando_entrada": "esperando la entrada", "en_posicion": "en posición", "terminada": "terminada",
            "armada": "armada", "midiendo_rango": "midiendo el rango", "rearmada": "rearmada", "esperando": "esperando",
            "sin_senal": "sin señal"}


def _r(x, d=2) -> str:
    return "–" if x is None else ("+" if x > 0 else "") + n(x, d) + " R"


def _dia(ms) -> str:
    import datetime
    return "–" if not isinstance(ms, (int, float)) else \
        datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%d-%m-%Y")


def _corta(s, k: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= k else s[:k - 1].rstrip() + "…"


def _ic(ic) -> str:
    """Intervalo del 90 % de una proporción (acierto), en %."""
    return f"IC 90 % {n(ic[0] * 100, 0)}-{n(ic[1] * 100, 0)} %" if isinstance(ic, list) and len(ic) == 2 \
        and None not in ic else "IC 90 % –"


def _ic_r(ic) -> str:
    """Intervalo del 95 % de un R medio (en R, no en proporción)."""
    return f"IC 95 % {n(ic[0])} a {n(ic[1])} R" if isinstance(ic, list) and len(ic) == 2 and None not in ic \
        else "IC 95 % –"


def get_o_nada(path: str):
    """GET que devuelve None cuando la ruta da 404 (módulo apagado o aún sin datos) en vez de lanzar."""
    try:
        return get(path)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _motor() -> dict | None:
    """Último «openings» (/api/aperturas) o None si el motor no responde."""
    try:
        return get("/api/aperturas")
    except (urllib.error.URLError, ValueError):
        return None


def _bt_motor() -> dict | None:
    """/api/backtest/impulso: {impulso, lab, seis} (bot/resumen_backtest.py) o None si aún no hay (404). Si el bot no
    responde, lanza: main() lo convierte en «ERROR: el bot no responde» en vez de decir que no hay backtest."""
    return get_o_nada("/api/backtest/impulso")


def _lab_e2(lab) -> dict | None:
    """E2 con la salida del laboratorio (la de la misma apertura del día siguiente) en laboratorio.json."""
    for s in ((lab or {}).get("robust") or []) + ((lab or {}).get("promising") or []):
        if isinstance(s, dict) and s.get("id") == E2_LAB:
            return s.get("oos") or {}
    return None


def _bt_texto(preset, bt) -> str:
    """Lo que dice el backtest de la estrategia en marcha, sin adornos."""
    if preset == "seis":
        f = ((bt or {}).get("seis") or {}).get("fuera") or {}
        cifra = (f"{_r(f['mean_R'])} por operación fuera de muestra ({f.get('n')} operaciones)"
                 if f.get("mean_R") is not None else
                 "unos -0,12 R por operación fuera de muestra (backtest de 3 años con el propio motor)")
        return (f"Backtest: «6 diarias» pierde, {cifra}. La puso el usuario para medirla en paper tal cual: no hay "
                "ventaja demostrada.")
    if preset == "impulso":
        f = ((bt or {}).get("impulso") or {}).get("fuera") or {}
        lab = _lab_e2((bt or {}).get("lab"))
        partes = []
        if lab and lab.get("mean_R") is not None:
            partes.append(f"{_r(lab['mean_R'])} por operación fuera de muestra con la salida del laboratorio "
                          f"({lab.get('trades')} operaciones)")
        if f.get("mean_R") is not None:
            partes.append(f"{_r(f['mean_R'])} con la salida de 7 días que opera el motor ({f.get('n')} operaciones), "
                          "pero esa salida se eligió después de ver los datos y no está validada")
        return "Backtest de E2: " + ("; ".join(partes) if partes else "aún no está en este bot") + "."
    return "Backtest: estos parámetros no son un preset y no tienen backtest propio."


def _estrategia_lineas(a: dict) -> list[str]:
    """Qué estrategia opera el motor: preset, nombre, de dónde salen los parámetros, versión cargada, pausa y día."""
    p = a.get("params") or {}
    preset = p.get("preset")
    nombre = p.get("nombre") or PRESET_TXT.get(preset) or ("modo impulso con parámetros propios"
                                                           if a.get("strategy") == "impulso" else "modo «ruptura» (v2)")
    lines = [f"Estrategia activa del motor de aperturas (cartera paper): {nombre}"
             + (f" (preset «{preset}»)" if preset else " (no es un preset)") + "."]
    if p.get("descripcion"):
        lines.append(f"  {p['descripcion']}.")
    src, ec = a.get("params_source"), a.get("estrategia_cargada") or {}
    carga = f"Origen de los parámetros: {FUENTE_PARAMS.get(src, src or 'desconocido')}"
    if isinstance(ec.get("ts"), (int, float)):
        carga += f"; cargados el {_hora(ec['ts'])}"
    if ec.get("firma"):
        carga += f" (versión {str(ec['firma'])[:8]}" + (f", registro {ec['registro']}" if ec.get("registro") else "") + ")"
    lines.append(carga + ".")
    if a.get("paused") or ec.get("pausada"):
        lines.append("PAUSADA: el motor mide pero no abre entradas nuevas; las abiertas siguen en el paper"
                     + (f" (motivo: {ec['motivo']})" if ec.get("motivo") else "") + ".")
    else:
        lines.append("En marcha: no está pausada.")
    if a.get("notice"):
        lines.append(f"Aviso del motor: {a['notice']}")
    d = a.get("day") or {}
    if d:
        lines.append(f"Hoy ({d.get('date')}, día de Madrid): {d.get('entries', 0)} de {d.get('max_entries', '–')} entradas y "
                     f"{n(d.get('risk_pct'))} % de {n(d.get('max_risk_pct'))} % de riesgo (topes compartidos con la "
                     "entrada por noticia).")
    return lines


def _reglas_lineas(p: dict) -> list[str]:
    """Las reglas del modo impulso en español (las mismas que el «why» de cada ventana del motor)."""
    sl, r, hold = p.get("sl_pct"), p.get("target_r"), p.get("max_hold_h")
    entry = str(p.get("entry") or "")
    ent = (f"a +{entry[2:]} min de la apertura" if entry.startswith("t:") else
           {"pb": "en la reanudación tras un retroceso", "brk": "en la ruptura del rango"}.get(entry, entry or "–"))
    mv = p.get("min_move_pct")
    filtros = [f"volumen relativo ≥ {n(p['rvol_min'], 1)}" if p.get("rvol_min") else "sin filtro de volumen",
               BTC_TXT.get(p.get("btc_filter"), f"BTC: {p.get('btc_filter')}"),
               NEWS_TXT.get(p.get("news_mode"), f"noticias: {p.get('news_mode')}")]
    tope = ("sin tope de tiempo (7 días de seguridad)" if hold is None or hold >= 168 - 1e-9
            else f"como mucho {n(hold, 0)} h")
    obj = f" (+{n(sl * r, 1)} %)" if isinstance(sl, (int, float)) and isinstance(r, (int, float)) else ""
    return [f"Entrada: mide el impulso de los {p.get('measure_min', '–')} primeros min de cada apertura "
            f"({'cualquier movimiento' if not mv else f'si pasa de ±{n(mv, 1)} %'}) y entra {ent}, a favor del impulso; "
            + "; ".join(filtros) + ".",
            f"Salida: horquilla pura, stop {n(sl, 1)} % y objetivo {n(r, 1)}R{obj}, {tope}; sin break-even ni stop "
            f"dinámico; solo si el objetivo deja ≥ {n(p.get('min_net_pct'), 1)} % neto de costes.",
            f"Largos {INST_TXT.get(p.get('long_instrument'), '–')} y cortos {INST_TXT.get(p.get('short_instrument'), '–')}; "
            f"una posición por moneda. Ventanas: {', '.join(p.get('windows') or []) or '–'}. "
            f"Monedas: {', '.join(p.get('coins') or []) or '–'}."]


def estrategia() -> str:
    """La estrategia que opera ahora el motor de aperturas en la cartera paper (/api/aperturas) y su backtest."""
    a = get_o_nada("/api/aperturas")
    if not a or not a.get("params"):
        return "El motor de aperturas aún no está en marcha en el bot: no se sabe qué estrategia opera."
    p = a["params"]
    lines = _estrategia_lineas(a)
    if a.get("strategy") == "impulso":
        lines += _reglas_lineas(p)
    lines.append(_bt_texto(p.get("preset"), _bt_motor()))
    lines.append(PAPER)
    return "\n".join(lines)


def _salidas(s) -> str:
    return ", ".join(f"{k} {v}" for k, v in (s or {}).items()) or "–"


def _bt_bloque(nombre: str, d: dict, activa: bool) -> list[str]:
    f, de = d.get("fuera") or {}, d.get("dentro") or {}
    out = [f"- {nombre}" + (" ← la que opera ahora el paper" if activa else "") + ":"]
    if f:
        out.append(f"  fuera de muestra ({_dia(f.get('desde'))} a {_dia(f.get('hasta'))}): {f.get('n')} operaciones, "
                   f"{_r(f.get('mean_R'))} de media, ganadoras {_rate(f.get('win_rate'))}, R acumulado "
                   f"{_r(f.get('total_R'), 1)} (caída máxima {n(f.get('max_dd_R'), 1)} R); salidas: {_salidas(f.get('salidas'))}")
    if de:
        out.append(f"  dentro de muestra: {de.get('n')} operaciones, {_r(de.get('mean_R'))} de media, "
                   f"ganadoras {_rate(de.get('win_rate'))}")
    if d.get("nota"):
        out.append(f"  nota: {_corta(d['nota'], 600)}")
    return out


def _bt_lab(lab: dict) -> list[str]:
    sm, per = lab.get("summary") or {}, lab.get("period") or {}
    out = [f"Laboratorio ({per.get('from', '–')} a {per.get('to', '–')}, fuera de muestra desde {per.get('split', '–')}): "
           f"{sm.get('tested', '–')} estrategias probadas, {sm.get('promising', 0)} prometedoras y {sm.get('robust', 0)} "
           f"robustas (por azar se esperarían unas {sm.get('expected_by_chance', '–')})."]
    for s in (lab.get("robust") or []) + (lab.get("promising") or []):
        o = s.get("oos") or {}
        out.append(f"- {s.get('class') or '?'}: {_corta(s.get('name'), 170)} · fuera de muestra {o.get('trades')} "
                   f"operaciones, {_r(o.get('mean_R'))} de media, ganadoras {_rate(o.get('win_rate'))}")
    av = lab.get("avance") or {}
    if av:
        out.append(f"Cuenta honesta en avance (cada trimestre, las {av.get('top')} mejores hasta entonces, 0,5 % de riesgo, "
                   f"desde {av.get('desde')}): {pct(av.get('total_pct'), 1)} en {av.get('days')} días, caída máxima "
                   f"{n(av.get('max_dd_pct'), 1)} %.")
    return out


def _bt_general() -> list[str]:
    """Las estrategias generales de BACKTEST_DIR/resultados.json (bolsas, noticias, flujo): ninguna con ventaja robusta."""
    st = (get_o_nada("/api/backtest?curvas=0") or {}).get("strategies") or []
    if not st:
        return ["Estrategias generales: el backtest aún no se ha generado."]
    cnt = {}
    for s in st:
        cnt[s["verdict"]] = cnt.get(s["verdict"], 0) + 1
    lines = [f"Estrategias generales con datos reales: {len(st)} · " + ", ".join(f"{k} {v}" for k, v in cnt.items()) + ".",
             "Las de mejor media entre las operables (tras comisiones; no son recomendaciones):"]
    ops = sorted((s for s in st if s.get("actionable")), key=lambda s: -(s.get("avg_ret_pct") or -99))[:5]
    for s in ops:
        lines.append(f"- {s['name']}: {s['verdict']}, n={s['n']}, acierto {n(s['hit_rate'] * 100, 0)} % (base {n(s['base_rate'] * 100, 0)} %), "
                     f"media {pct(s.get('avg_ret_pct'), 3)} por operación, fuera de muestra {n((s.get('oos') or {}).get('hit_rate', 0) * 100, 0)} %")
    return lines


def backtest() -> str:
    """El backtest del motor (/api/backtest/impulso: la estrategia en marcha, «6 diarias», E2 y el laboratorio) y el de
    las estrategias generales (/api/backtest)."""
    bt = _bt_motor()
    preset = ((_motor() or {}).get("params") or {}).get("preset")
    lines = []
    if bt:
        lines.append("Backtest del motor de aperturas (velas de 1 min, tras comisiones; R = resultado entre lo arriesgado):")
        seis, imp = bt.get("seis"), bt.get("impulso")
        partes = [("seis", seis), ("impulso", imp)] if preset == "seis" else [("impulso", imp), ("seis", seis)]
        nombres = {"seis": f"«{(seis or {}).get('nombre') or '6 diarias'}» (preset seis: impulso de 15 min sin filtros, "
                           "stop 1,5 %, 3R, como mucho 24 h)",
                   "impulso": "E2 · impulso con la salida de 7 días (preset impulso)"}
        for k, d in partes:
            if isinstance(d, dict):
                lines += _bt_bloque(nombres[k], d, preset == k)
        if bt.get("lab"):
            lines += _bt_lab(bt["lab"])
        if preset in ("seis", "impulso"):
            lines.append(_bt_texto(preset, bt))
    else:
        lines.append("Backtest del motor de aperturas: aún no está en este bot (python -m bot.laboratorio y "
                     "controles/compara_impulso.py).")
    lines += _bt_general()
    lines.append("Son simulaciones con datos pasados: ninguna estrategia tiene ventaja robusta fuera de muestra. " + PAPER)
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


def _plan_txt(pl: dict) -> str:
    partes = []
    if pl.get("entry") is not None:
        partes.append(f"entrada {n(pl['entry'], 4)}")
    if pl.get("stop") is not None:
        partes.append(f"stop {n(pl['stop'], 4)}" + (f" ({n(pl['stopPct'], 1)} %)" if pl.get("stopPct") is not None else ""))
    if pl.get("target") is not None:
        partes.append(f"objetivo {n(pl['target'], 4)}" + (f" ({n(pl['rr'], 1)}R)" if pl.get("rr") is not None else ""))
    if pl.get("net_pct") is not None:
        partes.append(f"neto {pct(pl['net_pct'])} tras costes de {n(pl.get('cost_pct'))} %")
    if pl.get("instrument"):
        partes.append(INST_TXT.get(pl["instrument"], pl["instrument"]))
    txt = ", ".join(partes) or "sin cifras"
    if pl.get("ok") is False:
        txt += f" · NO cumple las reglas: {pl.get('why') or 'sin motivo'}"
    return txt


def aperturas() -> str:
    """Estado del motor de aperturas en cada ventana: fase, sentido, instrumento, volumen relativo, entradas y plan."""
    a = get_o_nada("/api/aperturas")
    if not a or (not a.get("windows") and not a.get("params")):
        return "El motor de aperturas aún no está en marcha en el bot."
    lines = _estrategia_lineas(a) if a.get("params") else []
    if a.get("strategy") == "impulso":
        lines.append("Cómo opera: mide el impulso de los primeros minutos de cada apertura → entra a favor si pasa los "
                     "filtros → horquilla pura (stop y objetivo fijos; sin break-even ni stop dinámico) → una posición "
                     "por moneda y topes diarios de entradas y riesgo.")
    else:
        lines.append("Modo «ruptura» (v2), solo de referencia; con las reglas del usuario también va con horquilla pura "
                     "(sin break-even ni stop dinámico).")
    previo = None
    for w in a.get("windows") or []:
        st = w.get("state")
        head = f"- {w.get('name')} · {FASE_TXT.get(st, st or '?')}"
        if isinstance(w.get("open_utc"), (int, float)):
            head += f", abre el {_hora(w['open_utc'])}"
        lines.append(head)
        if w.get("why") and w["why"] != previo:
            lines.append(f"  {w['why']}")
            previo = w["why"]
        for c, x in (w.get("coins") or {}).items():
            sig = x.get("signal")
            partes = [FASE_TXT.get(x.get("state"), x.get("state") or "?")]
            if sig:
                partes.append("largo" if sig > 0 else "corto")
            if x.get("instrument"):
                partes.append(INST_TXT.get(x["instrument"], x["instrument"]))
            if x.get("rvol") is not None:
                partes.append(f"rvol {n(x['rvol'], 1)}")
            partes.append(f"entradas {x.get('entries', 0)}")
            why = x.get("why") or x.get("last_why") or ""
            lines.append(f"  {c}: {', '.join(partes)}" + (f" · {why}" if why else ""))
            if isinstance(x.get("plan"), dict) and x["plan"]:
                lines.append(f"    plan: {_plan_txt(x['plan'])}")
    if not a.get("windows"):
        lines.append("- Ninguna ventana activa ahora.")
    lines.append(PAPER)
    return "\n".join(lines)


ESTADO_MOD = {"ok": "al día", "arrancando": "arrancando", "datos_atrasados": "con los datos atrasados",
              "sin_datos": "sin datos", "error": "con un error", "sin_estrategias": "sin estrategias que vigilar",
              "apagada": "apagada", "apagado": "apagado"}


def vigilancia() -> str:
    """La prueba en avance de las estrategias del laboratorio (/api/vigilancia): operaciones virtuales frente al backtest."""
    v = get("/api/vigilancia")
    est = v.get("estado")
    if est == "apagada":
        return "La vigilancia está apagada en el bot (BOT_VIGILANCIA=0)."
    lines = ["Vigilancia: prueba en avance de las estrategias del laboratorio con operaciones VIRTUALES (sin dinero, ni "
             f"siquiera ficticio). Estado: {ESTADO_MOD.get(est, est or '?')}" + (f" ({v['nota']})" if v.get("nota") else "") + "."]
    ctx = []
    if v.get("desde_dia"):
        ctx.append(f"cuenta desde el {v['desde_dia']}")
    if v.get("congelada"):
        ctx.append(f"lista congelada el {v['congelada']}")
    if v.get("aperturas_evaluadas") is not None:
        ctx.append(f"{v['aperturas_evaluadas']} aperturas evaluadas")
    px = v.get("proxima") or {}
    if px.get("nombre"):
        ctx.append(f"próxima: {px['nombre']}" + (f" (se evalúa hacia el {_hora(px['evalua_ms'])})"
                                                 if isinstance(px.get("evalua_ms"), (int, float)) else ""))
    if ctx:
        txt = "; ".join(ctx)
        lines.append(txt[0].upper() + txt[1:] + ".")
    ests = v.get("estrategias") or []
    for e in ests:
        vv, bk, rg = e.get("vivo") or {}, e.get("backtest") or {}, e.get("reglas") or {}
        etq = (["variante del usuario: sin tope, solo los 7 días de seguridad"]
               if e.get("variante_de") or e.get("clase") == "variante" else [e.get("clase") or "?"])
        if e.get("en_paper"):
            etq.append("la que reproduce el paper")
        lines.append(f"- {e.get('codigo') or '?'} ({', '.join(etq)}): {_corta(e.get('nombre'), 150)}")
        lines.append(f"  en vivo: {vv.get('operaciones', 0)} cerradas, R medio {_r(vv.get('R_medio'))} "
                     f"({_ic_r(vv.get('ic95'))}), ganadoras {_rate(vv.get('ganadoras'))}, abiertas "
                     f"{vv.get('abiertas', 0)} · backtest fuera de muestra: {bk.get('operaciones', '–')} operaciones, "
                     f"R medio {_r(bk.get('R_medio'))} · stop {n(rg.get('sl_pct'), 1)} %, {n(rg.get('r'), 1)}R, "
                     f"{rg.get('dur_texto') or rg.get('dur') or '–'}")
    if not ests:
        lines.append("Sin estrategias vigiladas todavía.")
    lines.append("Con pocas operaciones la comparación con el backtest no dice nada. No es una previsión ni un consejo de "
                 "inversión.")
    return "\n".join(lines)


TIPO_GRUPO = {"medio": "medio", "fuente": "feed", "tipo_sentido": "tipo de noticia", "medio_sentido": "medio y sentido"}


def fiables() -> str:
    """Noticias fiables (/api/noticias_fiables): cuántos grupos pasan, cuántos operan, el azar y la nota."""
    f = get_o_nada("/api/noticias_fiables")
    if not f:
        return "Aún no hay cálculo de noticias fiables (el bot lo hace al arrancar y cada hora)."
    ab = f.get("azar_bloques") or {}
    lines = [f"Noticias fiables (solo mide; criterios fijados de antemano): {f.get('dias', '–')} días con datos "
             f"({f.get('desde', '–')} a {f.get('hasta', '–')}), {f.get('historias', '–')} noticias distintas.",
             f"Pasan el criterio {f.get('pasan', 0)} de {f.get('con_muestra', 0)} grupos con la muestra mínima "
             f"({f.get('probados', 0)} probados). Por puro azar pasarían unos {n(ab.get('media'), 1)} (p = "
             f"{n(ab.get('p'), 3)}, barajando dentro de cada hora); con la cuenta por moneda y día, "
             f"{n(f.get('por_azar'), 1)} (p = {n(f.get('p_azar'), 3)})."]
    p = ab.get("p") if ab.get("p") is not None else f.get("p_azar")
    if p is not None:
        lines.append("Conclusión: " + ("los que pasan no se distinguen del azar." if p >= 0.05
                                       else "pasan más grupos de los que daría el azar (p < 0,05)."))
    op = f.get("operan") or 0
    lines.append(f"Pasan la puerta para operar: {op}." + (
        " Sin ninguno, la entrada por noticia no abre nada y el veto de «6 diarias» no veta nada." if not op else
        " Solo esos se usan para la entrada por noticia y para el veto de «6 diarias» (en paper)."))
    if f.get("nota"):
        lines.append(f"Nota: {f['nota']}")
    gs = [g for g in f.get("grupos") or [] if g.get("estado") == "fiable"]
    if gs:
        lines.append("Grupos «fiable» (acierto a 4 h ajustado al mercado):")
    for g in gs[:10]:
        sent = {1: " al alza", -1: " a la baja"}.get(g.get("sentido"), "")
        lines.append(f"- {g.get('nombre')} ({TIPO_GRUPO.get(g.get('tipo'), g.get('tipo'))}{sent}): acierto "
                     f"{_rate(g.get('acierto'))} (sin ajustar {_rate(g.get('acierto_bruto'))}), {g.get('n')} casos de "
                     f"{g.get('n_historias')} noticias distintas en {g.get('dias')} días; "
                     + ("opera" if g.get("opera") else "no opera: " + ("; ".join(g.get("no_opera") or []) or "sin motivo")))
    if not gs:
        lines.append("Ningún grupo está ahora en «fiable».")
    lines.append("Medir no es predecir: nada de esto es un consejo de inversión.")
    return "\n".join(lines)


APR_POR = ("nivel", "region", "idioma", "tipo", "ventana", "fase", "rol", "moneda")
POR_TXT = {"nivel": "nivel de la fuente", "region": "región", "idioma": "idioma", "tipo": "tipo de noticia",
           "ventana": "ventana de apertura", "fase": "fase respecto a la apertura (previa, sesión o fuera)",
           "rol": "rol (primera en darla o confirmación)", "moneda": "moneda"}
VEREDICTO = {"pocos_datos": "pocos datos"}
PM_ORIGEN = ("paper", "vigilancia")
PM_ESTADO = {"pendiente": "explicación de la IA pendiente", "hecho": "explicado", "error": "la IA falló"}


def _medio_txt(m: dict) -> str:
    return (f"- {m.get('nombre') or m.get('medio')} ({m.get('medio')}" + (f", {m['nivel']}" if m.get("nivel") else "")
            + f"): acierto {_rate(m.get('acierto'))} ajustado al mercado ({_ic(m.get('ic90'))}; sin ajustar "
            f"{_rate(m.get('bruto'))}, base {_rate(m.get('base'))}), {n(m.get('casos'), 0)} casos "
            f"({m.get('independientes', 0)} independientes) en {m.get('dias', 0)} días, peso {n(m.get('peso'))}"
            + (" (fijado a mano)" if m.get("peso_manual") is not None else "")
            + f", {VEREDICTO.get(m.get('veredicto'), m.get('veredicto') or '?')}")


def _pm_txt(x: dict) -> str:
    txt = x.get("texto") or x.get("resumen") or "sin explicación todavía"
    return (f"- {_hora(x['cerrada']) if isinstance(x.get('cerrada'), (int, float)) else '–'} · "
            f"{'vigilancia (virtual)' if x.get('origen') == 'vigilancia' else x.get('origen') or '?'} · "
            f"{x.get('moneda') or '?'} · {_r(x.get('R'))} · {PM_ESTADO.get(x.get('estado'), x.get('estado') or '?')}: "
            f"{_corta(txt, 500)}")


def estadisticas(por: str) -> str:
    """Acierto de las noticias por una dimensión (/api/aprendizaje/estadisticas?por=)."""
    if por not in APR_POR:
        return f"--por debe ser uno de: {', '.join(APR_POR)}."
    xs = get_o_nada(f"/api/aprendizaje/estadisticas?por={por}")
    if xs is None:
        return "El aprendizaje no está en marcha en el bot (BOT_APRENDIZAJE=0 o no arrancó)."
    if not xs:
        return f"Aún no hay estadísticas por {POR_TXT[por]}."
    lines = [f"Acierto de las noticias por {POR_TXT[por]} (ajustado al mercado y contraído hacia el 50 %; 1 h y 4 h juntas):"]
    for x in xs[:15]:
        pr = x.get("principal") or x   # la ruta da el resumen completo; el mensaje, el compacto
        lines.append(f"- {x.get('clave')}: acierto {_rate(pr.get('p_aj', x.get('acierto')))} "
                     f"({_ic(pr.get('ic90_aj', x.get('ic90')))}; sin ajustar {_rate(pr.get('bruto'))}, base "
                     f"{_rate(pr.get('base'))}), {n(pr.get('casos'), 0)} casos ({pr.get('independientes', 0)} "
                     f"independientes) en {pr.get('dias', 0)} días")
    lines.append("Con pocos casos independientes o pocos días no se concluye nada. No es un consejo de inversión.")
    return "\n".join(lines)


def aprendizaje(argv: list[str]) -> str:
    """Lo que mide el aprendizaje (/api/aprendizaje): acierto global, ranking de medios y post-mortems."""
    por = arg(argv, "--por", None)
    if por is not None:
        return estadisticas(por)
    a = get("/api/aprendizaje")
    if a.get("estado") == "apagado":
        return "El aprendizaje está apagado en el bot (BOT_APRENDIZAJE=0)."
    lines = ["Aprendizaje (mide cada noticia triada con lo que hizo después el precio; no opera nada): "
             f"{ESTADO_MOD.get(a.get('estado'), a.get('estado') or '?')}" + (f". {a['nota']}" if a.get("nota") else "") + "."]
    t, par = a.get("totales") or {}, a.get("params") or {}
    if t:
        lines.append(f"Medido: {t.get('completas', 0)} de {t.get('filas', 0)} filas completas ({t.get('pendientes', 0)} "
                     f"pendientes, {t.get('sin_datos', 0)} sin datos) de {t.get('historias', 0)} noticias; "
                     f"{t.get('medios_con_datos', 0)} de {t.get('medios', 0)} medios con la muestra mínima.")
    g = a.get("global")
    if g:
        lines.append(f"Todas juntas: acierto {_rate(g.get('acierto'))} ajustado al mercado ({_ic(g.get('ic90'))}; sin "
                     f"ajustar {_rate(g.get('bruto'))}, base {_rate(g.get('base'))}) en {n(g.get('casos'), 0)} casos "
                     f"({g.get('independientes', 0)} independientes).")
    if not t.get("medios_con_datos") and not a.get("medios_mejores"):
        lines.append(f"Ningún medio tiene aún {par.get('min_casos', 20)} casos independientes en {par.get('min_dias', 10)} "
                     "días: no se concluye nada y todos los pesos siguen en 1.")
    rk = a.get("medios") or a.get("medios_mejores") or []
    if rk:
        lines.append(f"Ranking de medios (por peso y acierto; peso de 0 a 1,5, 1 = neutro; {min(8, len(rk))} de {len(rk)}):")
        lines += [_medio_txt(m) for m in rk[:8]]
    peores = a.get("medios_peores") or []
    if peores:
        lines.append("Los que peor resultado dan (con la muestra mínima):")
        lines += [_medio_txt(m) for m in peores[:5]]
    pm, pms = t.get("postmortems") or {}, a.get("postmortems") or []
    lines.append(f"Post-mortems de las operaciones cerradas: {pm.get('hechos', 0)} hechos, {pm.get('pendientes', 0)} "
                 f"pendientes, {pm.get('error', 0)} con error" + (". Los últimos:" if pms else "."))
    lines += [_pm_txt(x) for x in pms[:5]]
    lines.append("Más detalle: `aprendizaje --por region` (o nivel, idioma, tipo, ventana, fase, rol, moneda) y `postmortems`.")
    lines.append(_corta(a.get("aviso"), 500) if a.get("aviso") else
                 "Con pocos casos no se concluye nada. No es un consejo de inversión.")
    return "\n".join(lines)


def postmortems(argv: list[str]) -> str:
    """Explicación de las últimas operaciones cerradas (/api/aprendizaje/postmortems), paper o vigilancia."""
    origen = arg(argv, "--origen", None)
    if origen is not None and origen not in PM_ORIGEN:
        return f"--origen debe ser uno de: {', '.join(PM_ORIGEN)}."
    xs = get_o_nada("/api/aprendizaje/postmortems?limit=10" + (f"&origen={origen}" if origen else ""))
    if xs is None:
        return "El aprendizaje no está en marcha en el bot (BOT_APRENDIZAJE=0 o no arrancó)."
    donde = {"paper": " de la cartera paper", "vigilancia": " de la vigilancia (virtuales)"}.get(origen, "")
    if not xs:
        return f"Aún no hay post-mortems{donde}."
    lines = [f"Post-mortems{donde} (de la más reciente a la más antigua; explicación, nunca decisión):"]
    lines += [_pm_txt(x) for x in xs]
    lines.append(PAPER)
    return "\n".join(lines)


MODO_TXT = {"proponer": "proponer: nada se aplica solo",
            "automatico": "automático: promociones y pausas se aplican solas, siempre con las reglas del usuario"}
TIPO_PROP = {"estrategia_promovida": "sustituir el campeón", "estrategia_degradada": "pausar el campeón",
             "parametro_cambiado": "ajuste de parámetros"}
PROP_ESTADOS = ("pendiente", "aplicada", "descartada", "caducada", "revertida")


def evolucion() -> str:
    """Campeón y aspirantes, propuestas pendientes e informe semanal (/api/evolucion)."""
    e = get("/api/evolucion")
    if e.get("estado") == "apagada":
        return "La evolución está apagada en el bot (BOT_EVOLUCION=0)."
    modo = e.get("modo")
    lines = [f"Evolución (solo paper; modo {MODO_TXT.get(modo, modo or '?')}): "
             f"{ESTADO_MOD.get(e.get('estado'), e.get('estado') or '?')}" + (f". {e['nota']}" if e.get("nota") else "") + "."]
    c = e.get("campeon") or {}
    if c:
        vv = c.get("vivo") or {}
        de = {"preset": "el preset", "evolucion": "la evolución", "usuario": "el usuario"}.get(c.get("fuente"), c.get("fuente"))
        carga = ("pendiente de carga en el motor" if c.get("pendiente_reinicio") else
                 "confirmado por el motor" if c.get("confirmado") else "sin confirmar por el motor")
        lines.append(f"Campeón (la estrategia que opera el motor): {c.get('nombre') or '?'}"
                     + (f" ({c['codigo']})" if c.get("codigo") else "") + f", puesto por {de}, {carga}"
                     + (", PAUSADO" if c.get("pausada") else "")
                     + (f", desde el {_hora(c['desde'])}" if isinstance(c.get("desde"), (int, float)) else "") + ".")
        lines.append(f"  En el paper: {vv.get('operaciones', 0)} cerradas, R medio {_r(vv.get('R_medio'))} "
                     f"({_ic_r(vv.get('ic95'))}), ganadoras {_rate(vv.get('ganadoras'))}; {c.get('en_curso', 0)} en curso."
                     + (f" {vv['muestra']}" if vv.get("muestra") else ""))
        gem, vir = c.get("gemelo") or {}, c.get("virtual") or {}
        if gem.get("codigo"):
            lines.append(f"  Gemelo virtual en la vigilancia: {gem['codigo']}"
                         + (f", {vir.get('operaciones')} operaciones con R medio {_r(vir.get('R_medio'))}"
                            if vir.get("operaciones") is not None else "") + ".")
        dg = c.get("degradacion") or {}
        if dg.get("evaluada"):
            lines.append(f"  Degradación: R contraído {_r(dg.get('R_contraido'))}, "
                         + ("con evidencia clara." if dg.get("evidencia_clara") else "sin evidencia clara."))
        if c.get("nota"):
            lines.append(f"  {c['nota']}")
    asp = e.get("aspirantes") or []
    if asp:
        lines.append("Aspirantes (para sustituir al campeón: ≥ 40 operaciones nuevas, ventaja con p < 0,05 corregida, "
                     "reglas del usuario y confirmación con datos posteriores):")
    for a in asp[:8]:
        vv, fr = a.get("vivo") or {}, a.get("frente") or {}
        conf = (fr.get("confirmacion") or {}).get("texto")
        lines.append(f"- {a.get('codigo') or a.get('id')} ({a.get('clase') or '?'}): {vv.get('operaciones', 0)} operaciones, "
                     f"R medio {_r(vv.get('R_medio'))}"
                     + (f"; frente al campeón {_r(fr.get('dif_R'))}" if fr.get("dif_R") is not None else "")
                     + ("; cumple" if fr.get("cumple") else ("; le falta: " + "; ".join(fr["faltan"]) if fr.get("faltan") else ""))
                     + (f"; {conf}" if conf else "")
                     + (f"; no compatible: {a['por_que_no']}" if a.get("compatible") is False and a.get("por_que_no") else ""))
    props = e.get("propuestas") or []
    lines.append(f"Propuestas pendientes: {len(props)}" + ("; las primeras:" if len(props) > 6 else ":" if props else "."))
    for p in props[:6]:
        lines.append(f"- {p.get('titulo') or p.get('sujeto') or '?'} ({TIPO_PROP.get(p.get('tipo'), p.get('tipo'))})"
                     + (f": {_corta(p['texto'], 300)}" if p.get("texto") else ""))
    inf = e.get("informe_semanal") or {}
    if inf:
        lines.append(f"Informe semanal {inf.get('semana') or ''}: {inf.get('titulo') or ''}".rstrip(": "))
        for s in inf.get("secciones") or []:
            ls = [_corta(x, 220) for x in (s.get("lineas") or [])[:4]]
            if ls:
                lines.append(f"  {s.get('titulo')}: " + " · ".join(ls))
        if not inf.get("secciones") and inf.get("resumen"):
            lines.append(f"  {inf['resumen']}")
    elif (e.get("informe") or {}).get("titulo"):
        i = e["informe"]
        lines.append(f"Informe semanal {i.get('semana') or ''}: {i['titulo']}. {i.get('resumen') or ''}".rstrip())
    else:
        lines.append("Aún no hay informe semanal (sale los lunes a las 7:00 de Madrid).")
    lines.append("Las propuestas se aplican o descartan desde la web; esta skill solo las lee. " + PAPER)
    return "\n".join(lines)


def propuestas(argv: list[str]) -> str:
    """Propuestas de la evolución (/api/evolucion/propuestas), todas o de un estado."""
    est = arg(argv, "--estado", None)
    if est is not None and est not in PROP_ESTADOS:
        return f"--estado debe ser uno de: {', '.join(PROP_ESTADOS)}."
    r = get_o_nada("/api/evolucion/propuestas?limite=15" + (f"&estado={est}" if est else ""))
    if r is None:
        return "La evolución no está en marcha en el bot (BOT_EVOLUCION=0 o no arrancó)."
    ps = r.get("propuestas") or []
    cual = f" en estado «{est}»" if est else ""
    if not ps:
        return f"No hay propuestas de la evolución{cual}."
    lines = [f"Propuestas de la evolución{cual} (de la más reciente a la más antigua; solo paper):"]
    for p in ps:
        cu = p.get("cuerpo") or {}
        lines.append(f"- {_hora(p['ts']) if isinstance(p.get('ts'), (int, float)) else '–'} · "
                     f"{TIPO_PROP.get(p.get('tipo'), p.get('tipo'))} · {p.get('estado') or '?'}"
                     + (f" por {p['resuelta_por']}" if p.get("resuelta_por") else "")
                     + f": {cu.get('titulo') or p.get('sujeto') or '?'}"
                     + (f". {_corta(cu['texto'], 300)}" if cu.get("texto") else "")
                     + (f" (nota: {p['nota']})" if p.get("nota") else ""))
    lines.append("Se aplican o descartan desde la web; esta skill solo las lee.")
    return "\n".join(lines)


CAND_TXT = {"verificado": "con RSS verificado, pendientes de que los añadas", "pendiente": "pendientes de buscar su RSS",
            "anadido": "añadidos en prueba", "sin_rss": "sin RSS válido", "bloqueado": "bloqueados (robots o el servidor)",
            "en_registro": "ya estaban en el registro", "error": "con fallo de red"}
EST_FUENTE = (("activa", "activos"), ("prueba", "en prueba"), ("silenciada", "silenciados"), ("retirada", "retirados"))


def fuentes() -> str:
    """Fuentes vivas (/api/fuentes_vivas): cuántas en cada estado, las silenciadas, los candidatos y los últimos cambios."""
    fv = get("/api/fuentes_vivas?estado=silenciada&limite=15")
    if fv.get("estado") == "apagado":
        return "Las fuentes vivas no están en marcha en el bot: todas las fuentes cuentan como activas."
    lines = ["Fuentes vivas: deciden qué noticias cuentan (una silenciada llega con peso 0 y una retirada ni se lee); "
             "no tocan las reglas de operación.",
             f"Revisión automática {'encendida' if fv.get('auto') else 'apagada (las reglas solo sugieren)'}; "
             f"descubrimiento de medios {'encendido' if fv.get('descubrir') else 'apagado'}."]
    for clave, nombre in (("feeds", "Feeds"), ("medios", "Medios")):
        c = fv.get(clave) or {}
        if c:
            lines.append(f"{nombre}: " + ", ".join(f"{c.get(k, 0)} {t}" for k, t in EST_FUENTE) + ".")
    sil = [x for x in fv.get("lista") or [] if x.get("estado") in FUENTE_FUERA]
    if sil:
        lines.append("Silenciadas (hasta 15):")
    for x in sil[:15]:
        lines.append(f"- {x.get('nombre') or x.get('id')} ({x.get('tipo') or '?'}" + (f", {x['region']}" if x.get("region") else "")
                     + f"): {x.get('motivo') or 'sin motivo'}"
                     + (f", desde el {_hora(x['desde'])}" if isinstance(x.get("desde"), (int, float)) else "")
                     + (" (a mano)" if x.get("manual") else ""))
    if not sil:
        lines.append("Ninguna fuente silenciada ahora.")
    ca = fv.get("candidatos")
    cands = ca if isinstance(ca, list) else []
    cuenta = ca if isinstance(ca, dict) else {}
    if cands:
        for x in cands:
            cuenta[x.get("estado")] = cuenta.get(x.get("estado"), 0) + 1
    resumen = ", ".join(f"{v} {CAND_TXT.get(k, k)}" for k, v in cuenta.items() if v)
    lines.append("Candidatos a feed propio (medios que llegan por agregadores y dan antes las noticias o aciertan): "
                 + (resumen or "ninguno") + ".")
    orden = list(CAND_TXT)
    for x in sorted(cands, key=lambda x: orden.index(x.get("estado")) if x.get("estado") in orden else 99)[:8]:
        lines.append(f"- {x.get('nombre') or x.get('medio')} ({x.get('medio')}): {x.get('estado_texto') or x.get('estado')}"
                     + (f". {x['nota']}" if x.get("nota") else "") + (f" · RSS {x['rss']}" if x.get("rss") else ""))
    cambios = fv.get("cambios") or []
    if cambios:
        lines.append("Últimos cambios:")
    for x in cambios[:6]:
        lines.append(f"- {_hora(x['ts']) if isinstance(x.get('ts'), (int, float)) else '–'} · "
                     f"{x.get('texto') or x.get('accion')}: {x.get('nombre') or x.get('id')}"
                     + (f" ({x['motivo']})" if x.get("motivo") else "")
                     + (" · automático" if x.get("auto") else (f" · por {x['por']}" if x.get("por") else "")))
    lines.append("Silenciar, restaurar o añadir se hace desde la web; esta skill solo lee.")
    return "\n".join(lines)


# Comandos: los simples (sin argumentos) y los que llevan opciones. openclaw-trading/instalar.py solo permite estos.
SIMPLES = {"balanza": balanza, "tendencia": tendencia, "predicciones": predicciones, "backtest": backtest,
           "cartera": cartera, "aperturas": aperturas, "estrategia": estrategia, "evolucion": evolucion,
           "fuentes": fuentes, "vigilancia": vigilancia, "fiables": fiables}
CON_OPCIONES = {"noticias": noticias, "aprendizaje": aprendizaje, "postmortems": postmortems, "propuestas": propuestas}
DE_ESTADO = ("estado", "flujo", "agenda")   # salen de /api/estado


def main(argv: list[str]) -> str:
    cmd = argv[0] if argv else "estado"
    if cmd in CON_OPCIONES:
        return CON_OPCIONES[cmd](argv)
    if cmd in SIMPLES:
        return SIMPLES[cmd]()
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
