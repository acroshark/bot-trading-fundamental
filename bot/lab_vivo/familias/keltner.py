"""Familia «keltner»: salida del canal de Keltner en velas de 1 h a favor de la tendencia de 4 h. Largo cuando el
cierre de 1 h supera EMA20 + k × ATR(20) y la vela anterior no; tendencia de 4 h (la última cerrada): EMA20 > EMA50 y
cierre > EMA50; volumen de la hora ≥ rvol × la mediana de las 720 horas previas. Corto simétrico.
Stop: 2 × ATR(14) de 1 h. Prioridad: volumen relativo."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, emitir, flancos_dobles, validas


class Keltner(Familia):
    nombre = "keltner"
    version = 1
    titulo = "Salida del canal de Keltner (1 h) a favor de la tendencia de 4 h"
    descripcion = ("Cuando una vela de 1 h cierra fuera de su canal de Keltner con volumen y a favor de la tendencia "
                   "de 4 h, se sigue el movimiento.")
    datos = ("velas_1m",)
    calentamiento_d = 35
    fuentes = ("Chester Keltner (1960) y Linda Raschke: canales de Keltner con ATR",
               "Hudson y Urquhart (2021), Technical trading and cryptocurrencies")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "aperturas", "regimen_cripto")
    parametros = {
        "k": Param("float", 2.0, (2.0, 2.5), texto="k × ATR(20) del canal"),
        "filtro_4h": Param("bool", True, filtro=True, neutro=False, texto="a favor de la tendencia de 4 h"),
        "rvol": Param("float", 1.5, (1.5, 2.5), filtro=True, neutro=None, nulo=True,
                      texto="volumen de la hora ≥ x × la mediana de 30 días"),
        "k_atr_stop": Param("float", 2.0, (1.5, 2.0), texto="stop k × ATR(14) de 1 h"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 60)
            if len(b) < 2:
                continue
            h, l, cl, v = (np.asarray(x, float) for x in (b.h, b.l, b.c, b.v))
            e20 = ind.ema(cl, 20)
            a20 = ind.atr(h, l, cl, 20)
            up_b, dn_b = e20 + p["k"] * a20, e20 - p["k"] * a20
            ok = validas(b) & np.isfinite(up_b)
            with np.errstate(invalid="ignore"):
                up, dn = flancos_dobles(ok & (cl > up_b), ok & (cl < dn_b))
            rv = np.ones(len(cl))
            if p["rvol"]:
                cand = np.flatnonzero(up | dn)
                rv = np.full(len(cl), np.nan)
                rv[cand] = ind.volumen_relativo_en(v, 720, cand)
                with np.errstate(invalid="ignore"):
                    up &= rv >= p["rvol"]
                    dn &= rv >= p["rvol"]
            tc = np.asarray(b.t_cierre)
            if p["filtro_4h"]:
                b4 = m.barras(c, 240)
                if len(b4) < 2:
                    continue
                c4 = np.asarray(b4.c, float)
                e20_4, e50_4 = ind.ema(c4, 20), ind.ema(c4, 50)
                k4 = np.searchsorted(b4.t_cierre, tc, "right") - 1
                kk = np.clip(k4, 0, len(c4) - 1)
                with np.errstate(invalid="ignore"):
                    alc = (k4 >= 0) & (e20_4[kk] > e50_4[kk]) & (c4[kk] > e50_4[kk])
                    baj = (k4 >= 0) & (e20_4[kk] < e50_4[kk]) & (c4[kk] < e50_4[kk])
                up &= alc
                dn &= baj
            idx = np.flatnonzero(up | dn)
            if not len(idx):
                continue
            a14 = ind.atr_pct(h, l, cl, 14)
            sl = p["k_atr_stop"] * a14[idx]
            d = np.where(up[idx], 1, -1)
            okk = np.isfinite(sl)
            partes.append(emitir(tc[idx][okk], ci, d[okk], stop_pct=sl[okk], clave=d[okk],
                                 prioridad=np.nan_to_num(rv[idx][okk])))
        return Senales.juntar(partes)
