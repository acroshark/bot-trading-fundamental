"""Familia «cociente_alt_btc»: reversión del cociente alt/BTC operada solo en la alt (una pata: las reglas del usuario
no permiten pares). Cada día a las 00:00 UTC: x = ln(C_alt / C_BTC); z = (x − media de los «ventana» valores previos) /
su desviación. Largo en la alt si z ≤ −z0 y BTC no está bajista; corto si z ≥ +z0 y BTC no está alcista (filtro_btc).
Cada día mientras dure (el libro veta si la alt está ocupada). Stop: k_atr × ATR(14) diario de la alt.
Fil y Kristoufek (2020), pares en cripto; Deribit Insights sobre ETH/BTC. Prioridad: |z|."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, emitir, tendencia_diaria


class CocienteAltBtc(Familia):
    nombre = "cociente_alt_btc"
    version = 1
    titulo = "Reversión del cociente alt/BTC (operada en la alt)"
    descripcion = "Cuando la alt se separa mucho de BTC frente a su media reciente, se opera su vuelta en la alt."
    datos = ("velas_1m",)
    calentamiento_d = 95
    fuentes = ("Fil y Kristoufek (2020), Pairs Trading in Cryptocurrency Markets",
               "Deribit Insights, ETH/BTC: new regime or just mean reversion soon?")
    filtros = ("volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "z0": Param("float", 2.0, (1.5, 2.0), texto="z mínimo"),
        "ventana": Param("int", 20, (20, 60), texto="días de la media del cociente"),
        "filtro_btc": Param("bool", True, filtro=True, neutro=False, texto="BTC no en contra"),
        "k_atr": Param("float", 0.75, (0.75, 1.0), texto="stop k × ATR(14) diario"),
        "monedas": Param("lista", ["ETH", "SOL", "XRP"], ("ETH", "SOL", "XRP"), texto="alts"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        if "BTC" not in m.monedas:
            return Senales.vacia()
        bb = m.barras("BTC", 1440)
        tb, cb = np.asarray(bb.t_cierre), np.asarray(bb.c, float)
        partes = []
        for ci, c in self.monedas_de(m, p):
            if c == "BTC":
                continue
            ba = m.barras(c, 1440)
            ta = np.asarray(ba.t_cierre)
            k = np.searchsorted(tb, ta)
            ok = (k < len(tb)) & (tb[np.clip(k, 0, len(tb) - 1)] == ta)
            x = np.full(len(ta), np.nan)
            x[ok] = np.log(np.asarray(ba.c, float)[ok] / cb[k[ok]])
            mu, sd = ind.media_previa(x, p["ventana"]), ind.desv_previa(x, p["ventana"])
            with np.errstate(divide="ignore", invalid="ignore"):
                z = (x - mu) / sd
            btc = tendencia_diaria(m, "BTC", ta) if p["filtro_btc"] else np.zeros(len(ta), np.int8)
            with np.errstate(invalid="ignore"):
                up = np.isfinite(z) & (z <= -p["z0"]) & (btc >= 0)
                dn = np.isfinite(z) & (z >= p["z0"]) & (btc <= 0)
            idx = np.flatnonzero(up | dn)
            if not len(idx):
                continue
            sl = p["k_atr"] * ind.atr_pct(ba.h, ba.l, ba.c, 14)[idx]
            d = np.where(up[idx], 1, -1)
            okk = np.isfinite(sl)
            partes.append(emitir(ta[idx][okk], ci, d[okk], stop_pct=sl[okk], clave=d[okk],
                                 prioridad=np.abs(z[idx][okk])))
        return Senales.juntar(partes)
