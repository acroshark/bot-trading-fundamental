"""Familia «funding_hora»: presión antes de la liquidación del funding y rebote después. En cada liquidación t:
f_prev = la última tasa ya liquidada (la de t − 8 h; la prevista en vivo no está en la historia). Largo si f_prev ≥
f_min y el rendimiento de t − 60 min a t ≤ −c × σ de las horas de los 30 días previos; corto si f_prev ≤ −0,01 % y el
rendimiento ≥ +c × σ. Decisión en t (entrada en la vela siguiente). salida_min > 0: cierre por tiempo de la señal
(solo para la variante informativa). Prioridad: |rendimiento| / σ."""
import numpy as np

from .. import indicadores as ind
from .base import MIN, Familia, Param, Senales, cierre_en, decision_ms, emitir, funding8


class FundingHora(Familia):
    nombre = "funding_hora"
    version = 1
    titulo = "Presión antes de la liquidación del funding y rebote"
    descripcion = ("Los que pagan funding alto cierran antes de la liquidación y empujan el precio; después de la "
                   "liquidación la presión desaparece y el precio rebota.")
    datos = ("velas_1m", "funding")
    calentamiento_d = 35
    fuentes = ("Presto Labs, Can funding rate predict price change?", "CoinGlass, Funding Rates")
    filtros = ("regimen_btc", "volatilidad", "dias", "regimen_cripto")
    parametros = {
        "f_min": Param("float", 0.0003, (0.0003, 0.0005), nulo=True, filtro=True, neutro=None,
                       texto="tasa previa mínima (fracción por 8 h)"),
        "c": Param("float", 1.0, (1.0, 1.5), texto="c × σ de la hora previa"),
        "salida_min": Param("int", 0, (0, 60), texto="cierre por tiempo de la señal (min; 0 = el de la salida)"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            ft, fr = funding8(m, c)
            b = m.barras(c, 60)
            if len(ft) < 2 or len(b) < 2:
                continue
            r = ind.retornos_log(np.asarray(b.c, float))
            sd = ind.desv_previa(r, 720)
            tc = np.asarray(b.t_cierre)
            ts, ds, pr = [], [], []
            for k in range(1, len(ft)):
                t = int(decision_ms(ft[k]))              # la vela que abre tras el sello de la liquidación
                if t < desde_ms or t >= hasta_ms:
                    continue
                liq = int(ft[k]) // MIN * MIN            # la liquidación (hora en punto)
                f_prev = fr[k - 1]
                c0, c1 = cierre_en(m, c, np.array([liq - 60 * MIN, liq]))
                j = int(np.searchsorted(tc, liq, "right")) - 1
                if not (np.isfinite(c0) and np.isfinite(c1)) or j < 0 or not np.isfinite(sd[j]) or sd[j] <= 0:
                    continue
                rr = np.log(c1 / c0)
                if (p["f_min"] is None or f_prev >= p["f_min"]) and rr <= -p["c"] * sd[j]:
                    d = 1
                elif (p["f_min"] is None or f_prev <= -0.0001) and rr >= p["c"] * sd[j]:
                    d = -1
                else:
                    continue
                ts.append(t)
                ds.append(d)
                pr.append(abs(rr) / sd[j])
            if ts:
                ts_a = np.asarray(ts, np.int64)
                fin = ts_a + p["salida_min"] * MIN if p["salida_min"] else None
                partes.append(emitir(ts_a, ci, ds, clave=ds, prioridad=pr, fin_ms=fin))
        return Senales.juntar(partes)
