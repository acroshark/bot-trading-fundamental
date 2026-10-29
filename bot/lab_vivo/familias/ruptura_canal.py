"""Familia «ruptura_canal»: cierre de una barra por encima del máximo (o por debajo del mínimo) de las barras de las
horas anteriores, por flanco (la primera barra que rompe). Reglas de las «tortugas» (Faith, 2007) y seguimiento de
tendencia (Hurst, Ooi y Pedersen, 2017); en 4 h es el Donchian de H03 del catálogo.

Stop: «atr» = stop_atr_k × ATR(14) de las barras de stop_atr_tf, en %; «canal» = hasta el otro lado del canal.
Prioridad: (cierre − canal) / ATR."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, atr_pct_en, emitir, flancos_dobles, validas


class RupturaCanal(Familia):
    nombre = "ruptura_canal"
    version = 1
    titulo = "Ruptura del canal de varios días"
    descripcion = ("Largo cuando una barra cierra por encima del máximo de las barras de las «horas» anteriores (corto "
                   "por debajo del mínimo); solo la primera barra que rompe.")
    datos = ("velas_1m",)
    calentamiento_d = 40
    fuentes = ("Faith (2007), Way of the Turtle", "Hurst, Ooi y Pedersen (2017)", "Zarattini, Pagani y Barbon (2025)")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "aperturas", "regimen_cripto")
    parametros = {
        "horas": Param("int", 48, (24, 48, 80, 96, 168, 220), texto="horas del canal"),
        "tf_min": Param("int", 60, (15, 60, 240), texto="barras de (min)"),
        "stop": Param("str", "atr", ("atr", "canal"), texto="stop"),
        "stop_atr_k": Param("float", 1.5, (1.0, 1.5, 2.0), texto="k × ATR del stop"),
        "stop_atr_tf": Param("int", 60, (60, 240, 1440), texto="ATR del stop en barras de (min)"),
        "vol_x": Param("float", None, (1.5, 2.0), filtro=True, neutro=None, nulo=True,
                       texto="volumen de la barra de ruptura ≥ x × la mediana de las 20 anteriores"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        tf = p["tf_min"]
        nb = max(2, p["horas"] * 60 // tf)
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, tf)
            if len(b) < 2:
                continue
            h, l, cl = np.asarray(b.h, float), np.asarray(b.l, float), np.asarray(b.c, float)
            hi, lo = ind.max_previo(h, nb), ind.min_previo(l, nb)
            ok = validas(b) & np.isfinite(hi)
            with np.errstate(invalid="ignore"):
                up, dn = ok & (cl > hi), ok & (cl < lo)
            if p["vol_x"]:
                vr = ind.volumen_relativo(b.v, 20)
                with np.errstate(invalid="ignore"):
                    fuerte = vr >= p["vol_x"]
                up_f, dn_f = flancos_dobles(up, dn)
                up_f &= fuerte
                dn_f &= fuerte
            else:
                up_f, dn_f = flancos_dobles(up, dn)
            idx = np.flatnonzero(up_f | dn_f)
            if not len(idx):
                continue
            t = np.asarray(b.t_cierre)[idx]
            d = np.where(up_f[idx], 1, -1)
            atr = atr_pct_en(m, c, p["stop_atr_tf"], t)
            if p["stop"] == "atr":
                sl = p["stop_atr_k"] * atr
            else:
                sl = np.where(d > 0, (cl[idx] - lo[idx]) / cl[idx], (hi[idx] - cl[idx]) / cl[idx]) * 100
            with np.errstate(divide="ignore", invalid="ignore"):
                dist = np.where(d > 0, cl[idx] - hi[idx], lo[idx] - cl[idx]) / cl[idx] * 100 / atr
            buenos = np.isfinite(sl) & (sl > 0)
            partes.append(emitir(t[buenos], ci, d[buenos], stop_pct=sl[buenos], clave=d[buenos],
                                 prioridad=np.nan_to_num(dist[buenos])))
        return Senales.juntar(partes)
