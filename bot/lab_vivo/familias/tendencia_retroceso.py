"""Familia «tendencia_retroceso»: el primer retroceso a la media de 20 dentro de una tendencia fuerte y su reanudación
(«Holy Grail» de Connors y Raschke, 1995; ADX y DI de Wilder, 1978). Es la «tendencia_retroceso» que docs/07 (6.6)
dejó para la fase 2: retroceso dentro de una tendencia y reanudación.

En las barras cerradas de tf_min:
1. Tendencia fuerte: ADX(14) ≥ adx_min en la barra del retroceso, con +DI > −DI (alcista) o −DI > +DI (bajista).
2. Retroceso: la primera barra cuyo mínimo toca la EMA(20) (la anterior tenía el mínimo por encima de su EMA); en
   bajista, la primera cuyo máximo la toca desde abajo.
3. Reanudación: en las «espera» barras siguientes, el primer cierre por encima del máximo de la barra anterior (la regla
   compra con una orden por encima del máximo de la barra previa; aquí se pide el cierre, que confirma); en bajista,
   por debajo de su mínimo. Ahí se decide, a favor de la tendencia. Si no llega en esas barras, el retroceso caduca.
Stop: al otro lado del extremo del retroceso (el mínimo desde la barra del retroceso hasta la decisión) más un colchón
de 0,1 %: más cerca que en una ruptura, así que el mismo tramo de tendencia da más R.
Prioridad: el ADX de la barra del retroceso."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, emitir, validas

COLCHON_PCT = 0.1     # el stop, 0,1 % más allá del extremo del retroceso


class TendenciaRetroceso(Familia):
    nombre = "tendencia_retroceso"
    version = 1
    titulo = "Primer retroceso a la EMA20 en una tendencia fuerte y su reanudación (Holy Grail)"
    descripcion = ("Con el ADX alto, cuando el precio retrocede hasta su media de 20 barras y vuelve a cerrar por encima del "
                   "máximo de la barra anterior, se entra a favor de la tendencia con el stop detrás del retroceso.")
    datos = ("velas_1m",)
    calentamiento_d = 45
    fuentes = ("Connors y Raschke (1995), Street Smarts: High Probability Short-Term Trading Strategies (Holy Grail)",
               "Wilder (1978), New Concepts in Technical Trading Systems (ADX y DI)",
               "Liu y Tsyvinski (2021), Risks and Returns of Cryptocurrency, Review of Financial Studies 34(6)")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "aperturas", "regimen_cripto")
    parametros = {
        "tf_min": Param("int", 240, (60, 240), texto="barras de (min)"),
        "adx_min": Param("float", 30.0, (25.0, 30.0), texto="ADX(14) mínimo"),
        "espera": Param("int", 3, (3, 6), texto="barras para la reanudación"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        tf, esp = int(p["tf_min"]), int(p["espera"])
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, tf)
            if len(b) < 60:
                continue
            h, l, cl = (np.asarray(x, float) for x in (b.h, b.l, b.c))
            tc = np.asarray(b.t_cierre)
            ok = validas(b)
            ax, pdi, mdi = ind.adx(h, l, cl, 14)
            e20 = ind.ema(cl, 20)
            with np.errstate(invalid="ignore"):
                fuerte = ok & (ax >= p["adx_min"]) & np.isfinite(e20)
                fuera_arriba = np.r_[False, l[:-1] > e20[:-1]]      # la barra anterior, entera por encima de su EMA
                fuera_abajo = np.r_[False, h[:-1] < e20[:-1]]
                retrocesos = {1: fuerte & (pdi > mdi) & (l <= e20) & fuera_arriba,
                              -1: fuerte & (mdi > pdi) & (h >= e20) & fuera_abajo}
            ts, ds, ss, pr = [], [], [], []
            for d, ret in retrocesos.items():
                for s in np.flatnonzero(ret):
                    ext = l[s] if d > 0 else h[s]
                    for j in range(s + 1, min(len(cl), s + 1 + esp)):
                        if not ok[j]:
                            break
                        ext = min(ext, l[j]) if d > 0 else max(ext, h[j])
                        nivel = h[j - 1] if d > 0 else l[j - 1]
                        if d * (cl[j] - nivel) > 0:          # reanuda: cierra más allá del extremo de la barra anterior
                            ts.append(int(tc[j]))
                            ds.append(d)
                            ss.append(abs(cl[j] - ext) / cl[j] * 100 + COLCHON_PCT)
                            pr.append(float(ax[s]))
                            break
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr))
        return Senales.juntar(partes)
