"""Familia «tendencia_diaria»: momento de series temporales con decisión a las 00:00 UTC (día UTC cerrado). Largo si el
retorno de retorno_d días supera umbral_pct y el cierre está por encima de su media; corto al revés. Una sola entrada
mientras siga la señal (por flanco del estado). Moskowitz, Ooi y Pedersen (2012); Liu y Tsyvinski (2021).

Stop: el fijo de la salida, o k × ATR(14) diario si stop_k_atr no es nulo."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, emitir, flancos, validas


class TendenciaDiaria(Familia):
    nombre = "tendencia_diaria"
    version = 1
    titulo = "Tendencia diaria (momento de series temporales)"
    descripcion = ("Cada día a las 00:00 UTC: largo si el retorno de los últimos días supera el umbral y el precio está "
                   "por encima de su media; corto al revés; una sola entrada mientras dure la señal.")
    datos = ("velas_1m",)
    calentamiento_d = 230
    fuentes = ("Moskowitz, Ooi y Pedersen (2012), Time series momentum", "Liu y Tsyvinski (2021)")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "retorno_d": Param("int", 14, (7, 14, 28), texto="días del retorno"),
        "umbral_pct": Param("float", 0.0, (0.0, 2.0, 5.0), texto="umbral del retorno (%)"),
        "media": Param("str", "ema50", ("ninguna", "ema50", "ema200"), filtro=True, neutro="ninguna",
                       texto="media que tiene que acompañar"),
        "stop_k_atr": Param("float", None, (0.5, 0.75, 1.0, 1.5), nulo=True, texto="stop k × ATR(14) diario"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        L = p["retorno_d"]
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 1440)
            if len(b) < 2:
                continue
            cl = np.asarray(b.c, float)
            ret = (cl / ind.anterior(cl, L) - 1) * 100
            if p["media"] == "ninguna":
                med_ok_l = med_ok_s = np.ones(len(cl), bool)
            else:
                e = ind.ema(cl, 50 if p["media"] == "ema50" else 200)
                with np.errstate(invalid="ignore"):
                    med_ok_l, med_ok_s = cl > e, cl < e
            ok = validas(b) & np.isfinite(ret)
            with np.errstate(invalid="ignore"):
                up = ok & (ret > p["umbral_pct"]) & med_ok_l
                dn = ok & (ret < -p["umbral_pct"]) & med_ok_s
            fu, fd = flancos(up), flancos(dn)
            idx = np.flatnonzero(fu | fd)
            if not len(idx):
                continue
            t = np.asarray(b.t_cierre)[idx]
            d = np.where(fu[idx], 1, -1)
            if p["stop_k_atr"]:
                a = ind.atr_pct(b.h, b.l, b.c, 14)[idx] * p["stop_k_atr"]
            else:
                a = np.full(len(idx), np.nan)
            partes.append(emitir(t, ci, d, stop_pct=a, clave=d, prioridad=np.abs(ret[idx])))
        return Senales.juntar(partes)
