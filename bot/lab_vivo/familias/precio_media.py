"""Familia «precio_media»: el precio frente a su media móvil diaria (Detzel et al., 2021; Faber, 2007). Cada día a las
00:00 UTC: ratio = C / SMA_N − 1. Largo cuando el ratio pasa a > +banda desde ≤ +banda; corto cuando pasa a < −banda
desde ≥ −banda. Stop: k_atr × ATR(14) diario. Prioridad: |ratio| / ATR."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, emitir, validas


class PrecioMedia(Familia):
    nombre = "precio_media"
    version = 1
    titulo = "Precio frente a su media móvil diaria"
    descripcion = "Se entra cuando el cierre diario se separa de su media de N días más que la banda, en ese sentido."
    datos = ("velas_1m",)
    calentamiento_d = 70
    fuentes = ("Detzel, Liu, Strauss, Zhou y Zhu (2021), Financial Management 50(1)",
               "Faber (2007), A Quantitative Approach to Tactical Asset Allocation")
    filtros = ("regimen_btc", "volatilidad", "funding", "regimen_cripto")
    parametros = {
        "N": Param("int", 20, (10, 20, 50), texto="días de la media"),
        "banda": Param("float", 0.01, (0.01, 0.02), texto="banda alrededor de la media"),
        "k_atr": Param("float", 1.0, (0.75, 1.0, 1.5), texto="stop k × ATR(14) diario"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 1440)
            if len(b) < 2:
                continue
            cl = np.asarray(b.c, float)
            ratio = cl / ind.sma(cl, p["N"]) - 1
            ra = ind.anterior(ratio, 1)
            ok = validas(b) & np.isfinite(ratio) & np.isfinite(ra)
            bd = p["banda"]
            with np.errstate(invalid="ignore"):
                up = ok & (ratio > bd) & (ra <= bd)
                dn = ok & (ratio < -bd) & (ra >= -bd)
            idx = np.flatnonzero(up | dn)
            if not len(idx):
                continue
            atr = ind.atr_pct(b.h, b.l, b.c, 14)
            t = np.asarray(b.t_cierre)[idx]
            d = np.where(up[idx], 1, -1)
            sl = p["k_atr"] * atr[idx]
            with np.errstate(divide="ignore", invalid="ignore"):
                pr = np.abs(ratio[idx]) * 100 / atr[idx]
            okk = np.isfinite(sl)
            partes.append(emitir(t[okk], ci, d[okk], stop_pct=sl[okk], clave=d[okk], prioridad=np.nan_to_num(pr[okk])))
        return Senales.juntar(partes)
