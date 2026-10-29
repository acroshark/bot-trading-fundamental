"""Familia «flujo_tomador»: desequilibrio del flujo tomador (compras y ventas a mercado), con la compra del tomador de
las velas de 1 min (la columna 9 de los zips de Binance, que este laboratorio sí carga).

- «S4»: por minuto, D = 2 × compra del tomador − volumen. S4 = Σ D / Σ V de los minutos 0 y 1 de cada cuarto de hora de
  las últimas 4 h (16 cuartos: el flujo algorítmico periódico, Kim y Hansen, 2026). z frente a las 180 velas de 4 h
  previas. Al cierre de cada vela de 4 h.
- «S1»: S1 = Σ D / Σ V de los 60 min de la hora, z frente a las 720 horas previas y, con confirmacion_r, el rendimiento
  de la hora del mismo signo y ≥ 1σ.
«favor»: sentido = signo(z) si |z| ≥ z0 (por flanco); «contra»: −signo(z). Stop: 1,5 × ATR(14) de 4 h. Prioridad: |z|."""
import numpy as np

from .. import indicadores as ind
from .base import MIN, Familia, Param, Senales, atr_pct_en, emitir, flancos_dobles


class FlujoTomador(Familia):
    nombre = "flujo_tomador"
    version = 1
    titulo = "Desequilibrio del flujo tomador"
    descripcion = "Cuando las compras (o ventas) a mercado dominan mucho más de lo normal, se sigue (o se va contra) ese flujo."
    datos = ("velas_1m", "flujo_tomador")
    calentamiento_d = 35
    fuentes = ("Kim y Hansen (2026), The Quarter-Hour Effect (arXiv 2607.09426)",
               "Bouri, Lau, Lucey y Roubaud (2019), Trading volume and the predictability of return and volatility")
    filtros = ("regimen_btc", "volatilidad", "funding", "regimen_cripto")
    parametros = {
        "senal": Param("str", "S4", ("S4", "S1"), texto="medida del flujo"),
        "z0": Param("float", 2.0, (2.0, 3.0), texto="z mínimo"),
        "modo": Param("str", "favor", ("favor", "contra"), texto="a favor o a la contra"),
        "confirmacion_r": Param("float", None, (1.0,), nulo=True, filtro=True, neutro=None,
                                texto="la hora tiene que moverse ≥ x σ en el mismo sentido (S1)"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        tf, look = (240, 180) if p["senal"] == "S4" else (60, 720)
        partes = []
        for ci, c in self.monedas_de(m, p):
            den = m.denso(c)
            b = m.barras(c, tf)
            if den is None or len(b) < 2:
                continue
            tfm = tf * MIN
            i0 = (int(b.t[0]) - den.t0) // MIN
            nb = len(b)
            v = np.asarray(den.v[i0:i0 + nb * tf], float).reshape(nb, tf)
            tb = np.asarray(den.tb[i0:i0 + nb * tf], float).reshape(nb, tf)
            D = 2 * tb - v
            if p["senal"] == "S4":
                q = (np.arange(tf) % 15) < 2
                SD, SV = D[:, q].sum(1), v[:, q].sum(1)
            else:
                SD, SV = D.sum(1), v.sum(1)
            with np.errstate(divide="ignore", invalid="ignore"):
                S = np.where(SV > 0, SD / SV, np.nan)
            mu, sd = ind.media_previa(S, look), ind.desv_previa(S, look)
            with np.errstate(divide="ignore", invalid="ignore"):
                z = (S - mu) / sd
            ok = np.isfinite(z) & (np.asarray(b.n_validas) >= 0.9 * tf)
            with np.errstate(invalid="ignore"):
                up, dn = flancos_dobles(ok & (z >= p["z0"]), ok & (z <= -p["z0"]))
            if p["senal"] == "S1" and p["confirmacion_r"]:
                r = ind.retornos_log(np.asarray(b.c, float))
                sr = ind.desv_previa(r, 720)
                with np.errstate(invalid="ignore"):
                    up &= r >= p["confirmacion_r"] * sr
                    dn &= r <= -p["confirmacion_r"] * sr
            idx = np.flatnonzero(up | dn)
            if not len(idx):
                continue
            t = np.asarray(b.t)[idx] + tfm
            d = np.where(up[idx], 1, -1) * (1 if p["modo"] == "favor" else -1)
            sl = 1.5 * atr_pct_en(m, c, 240, t)
            okk = np.isfinite(sl)
            partes.append(emitir(t[okk], ci, d[okk], stop_pct=sl[okk], clave=d[okk], prioridad=np.abs(z[idx][okk])))
        return Senales.juntar(partes)
