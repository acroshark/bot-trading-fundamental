"""Familia «rsi2_tendencia»: retroceso dentro de la tendencia con el RSI de 2 periodos (Connors y Alvarez, «Short
Term Trading Strategies That Work»). Tendencia diaria (días cerrados): alcista si cierre > SMA50 y SMA20 > SMA50;
bajista al revés. RSI(2) de Wilder sobre los cierres del marco. Largo: alcista y RSI(2) ≤ lo; corto: bajista y RSI(2)
≥ hi (por flanco). Stop: k_atr × ATR(14) de las barras de atr_tf. Prioridad: lo extremo del RSI."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, atr_pct_en, emitir, flancos_dobles, tendencia_diaria, validas


class Rsi2Tendencia(Familia):
    nombre = "rsi2_tendencia"
    version = 1
    titulo = "Retroceso en tendencia con RSI(2) (Connors)"
    descripcion = "En una tendencia diaria clara, se compra el retroceso fuerte (o se vende el rebote) que marca el RSI(2)."
    datos = ("velas_1m",)
    calentamiento_d = 70
    fuentes = ("Connors y Alvarez (2009), Short Term Trading Strategies That Work", "LuxAlgo, RSI-2")
    filtros = ("volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "marco": Param("int", 240, (60, 240, 1440), texto="barras del RSI (min)"),
        "lo": Param("float", 10.0, (5.0, 10.0), texto="RSI(2) máximo para comprar"),
        "hi": Param("float", 90.0, (90.0, 95.0), texto="RSI(2) mínimo para vender"),
        "k_atr": Param("float", 1.5, (1.0, 1.5), texto="stop k × ATR(14)"),
        "atr_tf": Param("int", 240, (240, 1440), texto="ATR del stop en barras de (min)"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, p["marco"])
            if len(b) < 3:
                continue
            cl = np.asarray(b.c, float)
            rs = ind.rsi(cl, 2)
            tc = np.asarray(b.t_cierre)
            tend = tendencia_diaria(m, c, tc)
            ok = validas(b) & np.isfinite(rs)
            with np.errstate(invalid="ignore"):
                up, dn = flancos_dobles(ok & (tend > 0) & (rs <= p["lo"]), ok & (tend < 0) & (rs >= p["hi"]))
            idx = np.flatnonzero(up | dn)
            if not len(idx):
                continue
            t = tc[idx]
            d = np.where(up[idx], 1, -1)
            sl = p["k_atr"] * atr_pct_en(m, c, p["atr_tf"], t)
            okk = np.isfinite(sl)
            pr = np.where(d > 0, 100 - rs[idx], rs[idx])
            partes.append(emitir(t[okk], ci, d[okk], stop_pct=sl[okk], clave=d[okk], prioridad=pr[okk]))
        return Senales.juntar(partes)
