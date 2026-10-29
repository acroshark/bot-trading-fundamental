"""Familia «funding_ruptura»: ruptura contra la multitud apalancada. Al cierre de cada vela de 4 h: F3d = media de
las 9 últimas liquidaciones (3 días, por 8 h); su percentil frente a las F3d de los 30 días previos. Corto: F3d en el
percentil ≥ p_hi y cierre por debajo del mínimo de las «velas» de 4 h previas (la primera vez). Largo (squeeze): F3d en
el percentil ≤ p_lo, F3d < 0 y cierre por encima del máximo (la primera vez).
Stop: 2 × ATR(14) de 4 h (el «2N» de las tortugas). Prioridad: |percentil − 50|."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, atr_pct_en, emitir, flancos_dobles, funding8, validas


class FundingRuptura(Familia):
    nombre = "funding_ruptura"
    version = 1
    titulo = "Ruptura contra la multitud apalancada (funding de 3 días)"
    descripcion = ("Con el funding de 3 días en un extremo, la ruptura del canal de 5 días en contra de los "
                   "apalancados fuerza sus cierres.")
    datos = ("velas_1m", "funding")
    calentamiento_d = 40
    fuentes = ("Amberdata (2025), Leverage, Liquidations & the $31B Deleveraging",
               "BitMEX (T3 2025), estudio del funding", "Faith (2007), las tortugas")
    filtros = ("regimen_btc", "volatilidad", "dias", "regimen_cripto")
    parametros = {
        "p_hi": Param("float", 90.0, (80.0, 90.0), texto="percentil alto de F3d (30 días)"),
        "p_lo": Param("float", 10.0, (10.0, 20.0), texto="percentil bajo de F3d"),
        "velas": Param("int", 30, (18, 30), texto="velas de 4 h del canal"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            ft, fr = funding8(m, c)
            b = m.barras(c, 240)
            if len(ft) < 2 or len(b) < 2:
                continue
            f3 = np.convolve(fr, np.ones(9) / 9, mode="full")[:len(fr)]
            f3[:8] = np.nan
            pct_f = ind.percentil_previo(f3, 90)
            tc = np.asarray(b.t_cierre)
            k = np.searchsorted(ft, tc, "right") - 1
            okf = k >= 0
            kk = np.clip(k, 0, len(ft) - 1)
            F, P = np.where(okf, f3[kk], np.nan), np.where(okf, pct_f[kk], np.nan) * 100
            h, l, cl = np.asarray(b.h, float), np.asarray(b.l, float), np.asarray(b.c, float)
            hi, lo = ind.max_previo(h, p["velas"]), ind.min_previo(l, p["velas"])
            ok = validas(b) & np.isfinite(hi) & np.isfinite(P)
            with np.errstate(invalid="ignore"):
                up_b, dn_b = flancos_dobles(ok & (cl > hi), ok & (cl < lo))
                up = up_b & (P <= p["p_lo"]) & (F < 0)
                dn = dn_b & (P >= p["p_hi"])
            idx = np.flatnonzero(up | dn)
            if not len(idx):
                continue
            t = tc[idx]
            d = np.where(up[idx], 1, -1)
            sl = 2 * atr_pct_en(m, c, 240, t)
            o2 = np.isfinite(sl)
            partes.append(emitir(t[o2], ci, d[o2], stop_pct=sl[o2], clave=d[o2], prioridad=np.abs(P[idx][o2] - 50)))
        return Senales.juntar(partes)
