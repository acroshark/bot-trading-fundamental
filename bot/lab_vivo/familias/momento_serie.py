"""Familia «momento_serie»: momento de serie temporal con z-score. z = ln(C / C_(−L)) / (σ · √L), con σ la desviación
de los rendimientos logarítmicos de las sigma_n barras previas. Señal si |z| ≥ z_min; sentido = signo(z).
Liu y Tsyvinski (2021), Moskowitz, Ooi y Pedersen (2012); con stop por volatilidad (Moreira y Muir, 2017).

- modo «flanco»: solo cuando |z| cruza z_min (la barra anterior no lo cumplía) → H02 (4 h).
- modo «estado»: cada decisión mientras dure la señal (el libro veta si la moneda está ocupada; tras una salida se
  vuelve a entrar en la siguiente decisión si la señal sigue) → H01 (diario).
Stop: k_atr × ATR(14) de las barras de atr_tf. Prioridad: |z|."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, atr_pct_en, emitir, flancos, validas


class MomentoSerie(Familia):
    nombre = "momento_serie"
    version = 1
    titulo = "Momento de serie temporal (z-score)"
    descripcion = ("El rendimiento de las últimas L barras, medido en desviaciones de los rendimientos recientes: "
                   "a favor del movimiento cuando es grande (los rendimientos de 1 a 4 semanas tienden a continuar).")
    datos = ("velas_1m",)
    calentamiento_d = 95
    fuentes = ("Liu y Tsyvinski (2021), Risks and Returns of Cryptocurrency",
               "Moskowitz, Ooi y Pedersen (2012), Time series momentum", "Moreira y Muir (2017)")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "tf_min": Param("int", 1440, (240, 1440), texto="barras de (min)"),
        "L": Param("int", 14, (6, 7, 12, 14, 18, 28), texto="barras del retorno"),
        "z_min": Param("float", 0.5, (0.5, 1.0, 1.5), texto="z mínimo"),
        "sigma_n": Param("int", 60, (60, 180), texto="barras de la desviación"),
        "modo": Param("str", "estado", ("estado", "flanco"), texto="modo"),
        "k_atr": Param("float", 0.75, (0.5, 0.75, 1.0, 1.5), texto="stop k × ATR(14)"),
        "atr_tf": Param("int", 1440, (240, 1440), texto="ATR del stop en barras de (min)"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        L, n = p["L"], p["sigma_n"]
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, p["tf_min"])
            if len(b) < 2:
                continue
            cl = np.asarray(b.c, float)
            r = ind.retornos_log(cl)
            sd = ind.desv_previa(r, n)          # de las n barras ANTERIORES a la actual
            with np.errstate(divide="ignore", invalid="ignore"):
                z = np.log(cl / ind.anterior(cl, L)) / (sd * np.sqrt(L))
            ok = validas(b) & np.isfinite(z)
            with np.errstate(invalid="ignore"):
                up, dn = ok & (z >= p["z_min"]), ok & (z <= -p["z_min"])
            if p["modo"] == "flanco":
                up, dn = flancos(up), flancos(dn)
            idx = np.flatnonzero(up | dn)
            if not len(idx):
                continue
            t = np.asarray(b.t_cierre)[idx]
            d = np.where(up[idx], 1, -1)
            sl = p["k_atr"] * atr_pct_en(m, c, p["atr_tf"], t)
            ok2 = np.isfinite(sl)
            partes.append(emitir(t[ok2], ci, d[ok2], stop_pct=sl[ok2], clave=d[ok2], prioridad=np.abs(z[idx][ok2])))
        return Senales.juntar(partes)
