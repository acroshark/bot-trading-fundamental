"""Familia «ruptura_volatilidad»: ruptura de volatilidad diaria de Larry Williams (la más usada en cripto en Corea:
k = 0,5 con el día que empieza a las 00:00 UTC). Rango = máximo − mínimo de ayer (día UTC). Largo cuando el máximo
de una vela de 1 min llega a la apertura del día + k × rango; corto cuando el mínimo baja a la apertura − k × rango.
Solo el primer disparo del día por moneda (si los dos lados saltan en el mismo minuto, nada). La decisión es al cierre
de la vela que toca el nivel (la entrada, en la siguiente: más prudente que una orden stop).

k: fijo (0,5 o 0,7) o «ruido20» = media de 20 días de 1 − |C − O| / (H − L). Filtro sma20: largos solo con el cierre
de ayer por encima de su SMA de 20 días y cortos solo por debajo. Stop: hasta la apertura del día (volver a la
apertura invalida la ruptura), estructural."""
import numpy as np

from .. import indicadores as ind
from .base import DAY, MIN, Familia, Param, Senales, emitir


class RupturaVolatilidad(Familia):
    nombre = "ruptura_volatilidad"
    version = 1
    titulo = "Ruptura de volatilidad diaria (Larry Williams)"
    descripcion = ("Si el precio se aleja de la apertura del día más de k veces el rango de ayer, se sigue ese "
                   "desequilibrio; primer disparo del día; stop en la apertura.")
    datos = ("velas_1m",)
    calentamiento_d = 30
    fuentes = ("Larry Williams, Long-Term Secrets to Short-Term Trading (Wiley, 2011)",
               "LuxAlgo, Volatility Breakout", "WikiDocs, pyupbit: 변동성 돌파 전략")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "k": Param("float", 0.5, (0.5, 0.7), texto="k × rango de ayer"),
        "k_modo": Param("str", "fijo", ("fijo", "ruido20"), texto="k fijo o el ruido medio de 20 días"),
        "filtro_sma20": Param("bool", False, filtro=True, neutro=False,
                              texto="a favor de la SMA de 20 días (cierre de ayer)"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            den = m.denso(c)
            b = m.barras(c, 1440)
            if den is None or len(b) < 1:
                continue
            o_, h_, l_, c_ = (np.asarray(x, float) for x in (b.o, b.h, b.l, b.c))
            rango = h_ - l_
            with np.errstate(divide="ignore", invalid="ignore"):
                ruido = 1 - np.abs(c_ - o_) / np.where(rango > 0, rango, np.nan)
            s20 = ind.sma(c_, 20)
            bt = np.asarray(b.t)
            ho, hh, hl, hc = np.asarray(den.o), np.asarray(den.h), np.asarray(den.l), np.asarray(den.c)
            ts, ds, ss = [], [], []
            for k in range(1, len(bt) + 1):
                tD = int(bt[k - 1]) + DAY          # el día siguiente al de la barra k − 1 (ayer)
                if tD + MIN > (den.t0 + den.n * MIN) or tD < max(desde_ms - DAY, den.t0) or tD >= hasta_ms:
                    continue
                y = k - 1
                kk = p["k"]
                if p["k_modo"] == "ruido20":   # el ruido medio de los 20 días que acaban ayer
                    w = ruido[y - 19:y + 1] if y >= 19 else np.zeros(0)
                    kk = float(np.nanmean(w)) if np.isfinite(w).sum() >= 15 else np.nan
                if not np.isfinite(kk) or rango[y] <= 0:
                    continue
                a = (tD - den.t0) // MIN
                z = min(a + 1440, den.n)
                OD = ho[a]
                lh, ll = OD + kk * rango[y], OD - kk * rango[y]
                up = hh[a:z] >= lh
                dn = hl[a:z] <= ll
                hit = np.flatnonzero(up | dn)
                if not len(hit):
                    continue
                i = int(hit[0])
                if up[i] and dn[i]:
                    continue
                d = 1 if up[i] else -1
                if p["filtro_sma20"]:
                    if not np.isfinite(s20[y]) or (d > 0 and not c_[y] > s20[y]) or (d < 0 and not c_[y] < s20[y]):
                        continue
                px = hc[a + i]
                ts.append(den.t0 + (a + i + 1) * MIN)
                ds.append(d)
                ss.append(abs(px - OD) / px * 100)
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds))
        return Senales.juntar(partes)
