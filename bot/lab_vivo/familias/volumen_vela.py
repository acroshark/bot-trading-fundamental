"""Familia «volumen_vela»: vela de 1 h con volumen anómalo que cierra en su extremo (Bouri, Lau, Lucey y Roubaud,
2019; Balcilar et al., 2017: el volumen anticipa rendimiento y volatilidad). Largo: volumen ≥ v × la mediana de las 720
horas previas, r_1h ≥ +1,5 × σ de 720 horas y cierre en el 20 % alto de la vela. Corto simétrico. ruptura_24h: además
el cierre supera el máximo (o el mínimo) de las 24 h previas.
Stop: al otro extremo de la vela + 0,1 %. Prioridad: volumen relativo."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, emitir, validas


class VolumenVela(Familia):
    nombre = "volumen_vela"
    version = 1
    titulo = "Vela de 1 h con volumen anómalo que cierra en su extremo"
    descripcion = "Una hora con mucho volumen que cierra en su extremo señala presión que sigue; stop al otro lado de la vela."
    datos = ("velas_1m",)
    calentamiento_d = 35
    fuentes = ("Bouri, Lau, Lucey y Roubaud (2019), Trading volume and the predictability of return and volatility",
               "Balcilar, Bouri, Gupta y Roubaud (2017), Can volume predict Bitcoin returns and volatility?")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "aperturas", "regimen_cripto")
    parametros = {
        "v": Param("float", 3.0, (3.0, 5.0), texto="volumen ≥ v × la mediana de 30 días"),
        "ruptura_24h": Param("bool", False, filtro=True, neutro=False, texto="además, ruptura de las 24 h previas"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 60)
            if len(b) < 2:
                continue
            h, l, cl, v = (np.asarray(x, float) for x in (b.h, b.l, b.c, b.v))
            r = ind.retornos_log(cl)
            sd = ind.desv_previa(r, 720)
            rg = h - l
            ok = validas(b) & np.isfinite(sd) & (rg > 0)
            with np.errstate(invalid="ignore"):
                up = ok & (r >= 1.5 * sd) & (cl >= l + 0.8 * rg)
                dn = ok & (r <= -1.5 * sd) & (cl <= h - 0.8 * rg)
            vr = np.full(len(cl), np.nan)
            cand = np.flatnonzero(up | dn)
            vr[cand] = ind.volumen_relativo_en(v, 720, cand)
            with np.errstate(invalid="ignore"):
                up &= vr >= p["v"]
                dn &= vr >= p["v"]
                if p["ruptura_24h"]:
                    up &= cl > ind.max_previo(h, 24)
                    dn &= cl < ind.min_previo(l, 24)
            idx = np.flatnonzero(up | dn)
            if not len(idx):
                continue
            d = np.where(up[idx], 1, -1)
            sl = np.where(d > 0, (cl[idx] - l[idx]), (h[idx] - cl[idx])) / cl[idx] * 100 + 0.1
            partes.append(emitir(np.asarray(b.t_cierre)[idx], ci, d, stop_pct=sl, clave=d,
                                 prioridad=np.nan_to_num(vr[idx])))
        return Senales.juntar(partes)
