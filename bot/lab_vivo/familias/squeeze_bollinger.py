"""Familia «squeeze_bollinger»: ruptura tras la compresión de las bandas de Bollinger («The Squeeze», John Bollinger).
BBW = 4·σ20 / SMA20. Compresión: BBW ≤ percentil p de las 125 barras previas (o, en modo «ttm», las bandas de
Bollinger dentro del canal de Keltner de 20 y keltner_k × ATR). Disparo: en las 12 barras siguientes a una barra en
compresión, la primera que cierra fuera de la banda (C > SMA20 + 2σ → largo; C < SMA20 − 2σ → corto) con volumen ≥
1,5 × la mediana de las 20 previas. Stop: hasta la SMA20 + 0,1 % (la banda central invalida la ruptura).
Prioridad: compresión más extrema."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, emitir, validas


class SqueezeBollinger(Familia):
    nombre = "squeeze_bollinger"
    version = 1
    titulo = "Ruptura tras la compresión de Bollinger (The Squeeze)"
    descripcion = ("Tras un periodo de bandas de Bollinger muy estrechas, la primera vela que cierra fuera de la banda "
                   "con volumen marca el sentido de la expansión.")
    datos = ("velas_1m",)
    calentamiento_d = 30
    fuentes = ("Bollinger (2001), Bollinger on Bollinger Bands", "LuxAlgo, Bollinger Squeeze",
               "Technical Analysis of Stocks & Commodities: The Squeeze")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "p": Param("float", 10.0, (5.0, 10.0), texto="percentil de la anchura (125 barras)"),
        "marco": Param("int", 240, (60, 240), texto="barras de (min)"),
        "compresion": Param("str", "bbw", ("bbw", "ttm"), texto="medida de la compresión"),
        "keltner_k": Param("float", 1.5, (1.5,), texto="k del canal de Keltner (modo ttm)"),
        "vol_x": Param("float", 1.5, (1.5,), texto="volumen de la ruptura ≥ x × la mediana de 20"),
        "barras_max": Param("int", 12, (12,), texto="barras tras la compresión"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, p["marco"])
            if len(b) < 2:
                continue
            h, l, cl, v = (np.asarray(x, float) for x in (b.h, b.l, b.c, b.v))
            mid, up_b, lo_b, bbw = ind.bollinger(cl, 20, 2.0)
            if p["compresion"] == "ttm":
                e = ind.ema(cl, 20)
                a = ind.atr(h, l, cl, 20)
                comp = (up_b < e + p["keltner_k"] * a) & (lo_b > e - p["keltner_k"] * a)
            else:
                pct = ind.percentil_previo(bbw, 125)
                with np.errstate(invalid="ignore"):
                    comp = pct <= p["p"] / 100
            comp = comp & np.isfinite(bbw)
            vr = ind.volumen_relativo(v, 20)
            ok = validas(b) & np.isfinite(mid)
            with np.errstate(invalid="ignore"):
                fuera_up = ok & (cl > up_b) & (vr >= p["vol_x"])
                fuera_dn = ok & (cl < lo_b) & (vr >= p["vol_x"])
            ts, ds, ss, pr = [], [], [], []
            ult, usado = -10 ** 9, True
            tc = np.asarray(b.t_cierre)
            pct_b = ind.percentil_previo(bbw, 125)
            for i in range(len(cl)):
                if (fuera_up[i] or fuera_dn[i]) and not usado and 1 <= i - ult <= p["barras_max"]:
                    d = 1 if fuera_up[i] else -1
                    ts.append(int(tc[i]))
                    ds.append(d)
                    ss.append(abs(cl[i] - mid[i]) / cl[i] * 100 + 0.1)
                    pr.append(1 - (float(pct_b[ult]) if np.isfinite(pct_b[ult]) else 0.5))
                    usado = True
                if comp[i]:
                    ult, usado = i, False
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr))
        return Senales.juntar(partes)
