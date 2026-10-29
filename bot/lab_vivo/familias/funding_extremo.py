"""Familia «funding_extremo»: funding extremo a la contra en la hora de la liquidación, con confirmación del precio.
En cada liquidación t: f = tasa liquidada en t (por 8 h). Alta: f ≥ percentil p_hi de las 90 liquidaciones previas y
f ≥ f_hi; baja: f ≤ percentil p_lo y f ≤ f_lo. Confirmación: la vela de 1 h de t a t + 1 h cierra contra la multitud
(alta: cierre < apertura → corto en perpetuo; baja: cierre > apertura → largo al contado). Sin confirmación se decide
en t. Los apalancados que pagan mucho funding se ven forzados a cerrar (He, Manela, Ross y von Wachter).
Stop: hasta el máximo (corto) o el mínimo (largo) de las 8 h previas + 0,1 %. Prioridad: |percentil − 50|."""
import numpy as np

from .base import H, Familia, Param, Senales, apertura_en, cierre_en, decision_ms, emitir, funding8


class FundingExtremo(Familia):
    nombre = "funding_extremo"
    version = 1
    titulo = "Funding extremo a la contra (con confirmación)"
    descripcion = ("Cuando la tasa de funding liquidada es extrema y la hora siguiente va contra la multitud "
                   "apalancada, se opera a la contra de esa multitud.")
    datos = ("velas_1m", "funding")
    calentamiento_d = 35
    fuentes = ("He, Manela, Ross y von Wachter (2022-2024), Fundamentals of Perpetual Futures",
               "Presto Labs, Can funding rate predict price change?", "CoinGlass, Funding Rates")
    filtros = ("regimen_btc", "volatilidad", "dias", "regimen_cripto")
    parametros = {
        "p_hi": Param("float", 95.0, (90.0, 95.0), nulo=True, texto="percentil alto (90 liquidaciones)"),
        "p_lo": Param("float", 5.0, (5.0, 10.0), nulo=True, texto="percentil bajo"),
        "f_hi": Param("float", 0.0003, (0.0002, 0.0003, 0.0005), texto="tasa alta mínima (fracción por 8 h)"),
        "f_lo": Param("float", -0.0001, (-0.0002, -0.0001, -0.00005), texto="tasa baja máxima"),
        "confirmacion": Param("bool", True, filtro=True, neutro=False, texto="confirmación de la hora siguiente"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            ft, fr = funding8(m, c)
            if len(ft) < 2:
                continue
            ts, ds, ss, pr = [], [], [], []
            for k in range(90, len(ft)):
                t = int(ft[k])
                if t < desde_ms - 2 * H or t >= hasta_ms:
                    continue
                prev = fr[k - 90:k]
                f = fr[k]
                pct = float((prev < f).mean()) * 100
                alta = f >= p["f_hi"] and (p["p_hi"] is None or pct >= p["p_hi"])
                baja = f <= p["f_lo"] and (p["p_lo"] is None or pct <= p["p_lo"])
                if not (alta or baja):
                    continue
                d = -1 if alta else 1
                td = int(decision_ms(t))
                if p["confirmacion"]:
                    t_ini = td - (td % H) if td % H else td
                    o1 = apertura_en(m, c, np.array([t_ini]))[0]
                    c1 = cierre_en(m, c, np.array([t_ini + H]))[0]
                    if not (np.isfinite(o1) and np.isfinite(c1)) or (d < 0 and not c1 < o1) or (d > 0 and not c1 > o1):
                        continue
                    td = t_ini + H
                den = m.denso(c)
                a = max(0, (td - 8 * H - den.t0) // 60_000)
                z = min(den.n, (td - den.t0) // 60_000)
                if z - a < 60:
                    continue
                ref = float(np.asarray(den.c)[z - 1])
                ext = float(np.asarray(den.h)[a:z].max()) if d < 0 else float(np.asarray(den.l)[a:z].min())
                ts.append(td)
                ds.append(d)
                ss.append(abs(ext - ref) / ref * 100 + 0.1)
                pr.append(abs(pct - 50))
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr))
        return Senales.juntar(partes)
