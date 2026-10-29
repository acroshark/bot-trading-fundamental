"""Familia «dia_anormal»: seguir el día de rendimiento anormal (Caporale y Plastun, 2020). Al cierre de cada vela de
1 h del día UTC (01:00 a 23:00): μ y σ = media y desviación de los rendimientos diarios logarítmicos de los 60 días
previos; r_dia = ln(C / apertura del día). Evento: la primera hora del día con r_dia ≥ μ + c·σ (largo) o ≤ μ − c·σ
(corto). excepciones: las del artículo (BTC al alza y ETH a la baja van a la contra). desde_h: solo eventos desde esa
hora UTC. Cierre por tiempo de la señal: 00:00 UTC del día siguiente al del evento + 1 (d + 2).
Stop: 0,5 × |r_dia| (si se deshace la mitad del día, fuera). Prioridad: |r_dia| / σ."""
import numpy as np

from .. import indicadores as ind
from .base import DAY, H, Familia, Param, Senales, emitir, validas


class DiaAnormal(Familia):
    nombre = "dia_anormal"
    version = 1
    titulo = "Seguir el día de rendimiento anormal"
    descripcion = "Cuando el día va muy por encima (o por debajo) de lo normal, se sigue el movimiento hasta el día siguiente."
    datos = ("velas_1m",)
    calentamiento_d = 65
    fuentes = ("Caporale y Plastun (2020), Momentum effects in the cryptocurrency market after one-day abnormal returns",)
    filtros = ("regimen_btc", "volatilidad", "funding", "regimen_cripto")
    parametros = {
        "c": Param("float", 2.0, (1.5, 2.0, 2.5), texto="c × σ diaria"),
        "excepciones": Param("bool", False, texto="excepciones del artículo (BTC al alza y ETH a la baja, a la contra)"),
        "desde_h": Param("int", 1, (1, 12), texto="primera hora UTC"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            bd, b1 = m.barras(c, 1440), m.barras(c, 60)
            if len(bd) < 2 or len(b1) < 2:
                continue
            cd = np.asarray(bd.c, float)
            rd = ind.retornos_log(cd)
            td = np.asarray(bd.t)
            t1, tc1, o1, c1 = np.asarray(b1.t), np.asarray(b1.t_cierre), np.asarray(b1.o, float), np.asarray(b1.c, float)
            ok1 = validas(b1)
            ts, ds, ss, pr, fins = [], [], [], [], []
            for y in range(60, len(td)):                  # y = ayer (la última barra diaria cerrada)
                tD = int(td[y]) + DAY
                if tD < desde_ms - DAY or tD >= hasta_ms:
                    continue
                w = rd[y - 59:y + 1]                      # los 60 rendimientos diarios que acaban ayer
                w = w[np.isfinite(w)]
                if len(w) < 50:
                    continue
                mu_y, sd_y = float(w.mean()), float(w.std(ddof=1))
                if sd_y <= 0:
                    continue
                a = int(np.searchsorted(t1, tD))
                z = int(np.searchsorted(t1, tD + DAY))
                if a >= z or t1[a] != tD:
                    continue
                OD = o1[a]
                for i in range(a, min(z, len(c1))):
                    hora = (int(tc1[i]) - tD) // H
                    if hora < max(1, p["desde_h"]) or hora > 23 or not ok1[i]:
                        continue
                    r = np.log(c1[i] / OD)
                    if r >= mu_y + p["c"] * sd_y:
                        d = 1
                    elif r <= mu_y - p["c"] * sd_y:
                        d = -1
                    else:
                        continue
                    if p["excepciones"] and ((c == "BTC" and d > 0) or (c == "ETH" and d < 0)):
                        d = -d
                    ts.append(int(tc1[i]))
                    ds.append(d)
                    ss.append(0.5 * abs(r) * 100)
                    pr.append(abs(r) / sd_y)
                    fins.append(tD + 2 * DAY)
                    break
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr, fin_ms=fins))
        return Senales.juntar(partes)
