"""Familia «noticia_apertura»: las noticias con sentido de las horas antes de una apertura, confirmadas por el impulso de
sus primeros minutos (la operativa del usuario: noticias → confirmación en la apertura; docs/requisitos-usuario.md y la
regla de bot/entrada_noticias.py llevada a las aperturas). Datos: noticias_3a.

En cada apertura T de las ventanas (apertura.LAB_WINDOWS, festivos fuera) y para cada moneda:
1. Noticias: las historias×moneda (las monedas que nombra el triaje y, con generales «todas», las cuatro para las que no
   nombran ninguna) que se podían usar en [T − horas, T), en racimo de ≥ n medios en 30 min (solo hay triaje de las de
   cripto con ≥ 2 y de las macro con ≥ 4), con importancia ≥ la mínima y sentido (|direccion| > 15). Con grupo
   «fiable», solo las de un grupo tipo×sentido que cumple los criterios fijados de bot/fiables.py con los 30 días UTC
   completos anteriores.
2. Sentido: el signo de la suma de sus sentidos (+1 alcista, −1 bajista); empate, nada.
3. Confirmación: el movimiento de la apertura de la vela de T al cierre de T + medida_min va a favor ≥ min_mov_pct.
4. Decisión en T + medida_min, a favor (o a la contra con modo «contra»). Stop: el de la salida.
Prioridad: |suma|."""
import numpy as np

from .base import (MEDIOS_MODOS, mascara_medios, DAY, H, MIN, Familia, Param, Senales, apertura_en, cierre_en, emitir, grupos_fiables,
                   ultimo_dia_completo, unidades_noticias)

VENTANAS = ("tokio-seul", "china", "europa", "saopaulo", "nuevayork")   # apertura.LAB_WINDOWS


class NoticiaApertura(Familia):
    nombre = "noticia_apertura"
    version = 1
    titulo = "Noticias de antes de la apertura confirmadas por su impulso"
    descripcion = ("Si las noticias de las horas previas apuntan en un sentido y los primeros minutos de la apertura "
                   "van en ese sentido, se entra a favor (o a la contra).")
    momento = "aperturas"
    datos = ("velas_1m", "aperturas", "noticias_3a")
    calentamiento_d = 35
    fuentes = ("Lopez-Lira y Tang (2023-2025), Can ChatGPT Forecast Stock Price Movements? (arXiv 2304.07619)",
               "Tetlock (2007), Giving Content to Investor Sentiment, Journal of Finance 62(3)",
               "Gao, Han, Li y Zhou (2018), Market Intraday Momentum, Journal of Financial Economics 129(2)")
    filtros = ("regimen_btc", "volatilidad")
    parametros = {
        "windows": Param("lista", list(VENTANAS), VENTANAS, texto="aperturas"),
        "horas": Param("int", 8, (4, 8, 12), texto="horas de noticias antes de la apertura"),
        "n": Param("int", 2, (2, 3, 5), texto="medios distintos en 30 min (las macro se triaron con ≥ 4)"),
        "medida_min": Param("int", 15, (15, 30, 60), texto="minutos de impulso"),
        "min_mov_pct": Param("float", 0.2, (0.0, 0.2, 0.5), texto="impulso mínimo a favor (%)"),
        "modo": Param("str", "favor", ("favor", "contra"), texto="a favor o a la contra"),
        "generales": Param("str", "todas", ("no", "todas"), texto="historias que no nombran moneda"),
        "importancia_min": Param("int", 0, (0, 5, 7), filtro=True, neutro=0, texto="importancia mínima del triaje"),
        "grupo": Param("str", "todos", ("todos", "fiable"), filtro=True, neutro="todos",
                       texto="solo grupos tipo×sentido fiables (criterios de bot/fiables.py)"),
        "medios": Param("str", "todos", MEDIOS_MODOS, filtro=True, neutro="todos",
                        texto="medios por su acierto hasta la fecha (fuera los malos / solo los buenos)"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        nt = m.noticias
        if nt is None or not len(nt):
            return Senales.vacia()
        med = p["medida_min"] * MIN
        ops = sorted({o["open_ms"]: o["id"] for o in m.aperturas if o["id"] in p["windows"]
                      and desde_ms - med <= o["open_ms"] < hasta_ms}.items())
        if not ops:
            return Senales.vacia()
        T = np.array([t for t, _ in ops], np.int64)
        win = np.array([VENTANAS.index(w) for _, w in ops], np.int8)
        gen = p["generales"] == "todas"
        sen = nt.sentido()
        f, k = unidades_noticias(nt, m.monedas, gen)
        sel = (nt.n30[f] >= p["n"]) & (nt.imp[f] >= p["importancia_min"]) & (sen[f] != 0)
        if p["medios"] != "todos":
            sel &= mascara_medios(m, nt, m.monedas, gen, p["medios"])[f]
        if p["grupo"] == "fiable" and sel.any():
            d0 = int(ultimo_dia_completo(nt.t[0]))
            d1 = int(ultimo_dia_completo(max(int(nt.t[-1]), int(T[-1]))))
            tabla, _ = grupos_fiables(m, nt, m.monedas, gen, d0, d1)
            L = ultimo_dia_completo(nt.t[f])
            g = nt.tipo[f].astype(np.int64) * 2 + (sen[f] > 0)
            ok = sel & (L >= d0) & (L <= d1)
            ok[ok] = tabla[L[ok] - d0, g[ok]]
            sel = ok
        f, k = f[sel], k[sel]
        if not len(f):
            return Senales.vacia()
        tn, sn = nt.t[f], sen[f].astype(np.int64)
        partes = []
        for ci, c in enumerate(m.monedas):
            if c not in p["monedas"]:
                continue
            mk = k == ci
            t_c, s_c = tn[mk], sn[mk]
            if not len(t_c):
                continue
            o = np.argsort(t_c, kind="stable")
            t_c, s_c = t_c[o], s_c[o]
            acum = np.r_[0, np.cumsum(s_c)]
            a = np.searchsorted(t_c, T - p["horas"] * H, "left")
            b = np.searchsorted(t_c, T, "left")      # solo las que ya se podían usar antes de la apertura
            suma = acum[b] - acum[a]
            d = np.sign(suma).astype(np.int8)
            p0, p1 = apertura_en(m, c, T), cierre_en(m, c, T + med)
            with np.errstate(divide="ignore", invalid="ignore"):
                mov = (p1 / p0 - 1) * 100
            ok = (d != 0) & np.isfinite(mov) & (d * mov >= p["min_mov_pct"] - 1e-12)
            if not ok.any():
                continue
            dd = d[ok] * (-1 if p["modo"] == "contra" else 1)
            partes.append(emitir(T[ok] + med, ci, dd, clave=dd, prioridad=np.abs(suma[ok]).astype(float),
                                 ventana=win[ok]))
        return Senales.juntar(partes)
