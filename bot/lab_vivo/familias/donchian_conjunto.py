"""Familia «donchian_conjunto»: conjunto de canales de Donchian diarios (Zarattini, Pagani y Barbon, 2025), lo mejor
documentado en cripto. Cada día a las 00:00 UTC, para N ∈ {10, 20, 30, 60, 90}: estado_N = +1 si el cierre supera el
máximo de los N días previos, −1 si baja del mínimo y, si no, el estado anterior. P = media de los 5 estados.
Largo cuando P pasa a ≥ +u desde < +u; corto cuando pasa a ≤ −u desde > −u. Reentrada: si P sigue ≥ u (≤ −u) y el
cierre marca un nuevo máximo (mínimo) de 10 días (el libro veta si la moneda sigue ocupada).
Stop: k_atr × ATR(14) diario. Prioridad: |P|."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, emitir, validas

NS = (10, 20, 30, 60, 90)


class DonchianConjunto(Familia):
    nombre = "donchian_conjunto"
    version = 1
    titulo = "Conjunto de canales de Donchian diarios"
    descripcion = ("Cinco canales de Donchian (10 a 90 días) votan; se entra cuando la media de sus estados cruza el "
                   "umbral y se reentra en nuevos máximos (mínimos) de 10 días mientras siga la señal.")
    datos = ("velas_1m",)
    calentamiento_d = 130
    fuentes = ("Zarattini, Pagani y Barbon (2025), Catching Crypto Trends",
               "Faith (2007), las tortugas", "Moreira y Muir (2017)")
    filtros = ("regimen_btc", "volatilidad", "funding", "regimen_cripto")
    parametros = {
        "u": Param("float", 0.6, (0.6, 1.0), texto="umbral de la media de los estados"),
        "k_atr": Param("float", 1.0, (1.0, 1.5), texto="stop k × ATR(14) diario"),
        "reentrada": Param("bool", True, texto="reentrada en nuevos extremos de 10 días"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        u = p["u"]
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 1440)
            if len(b) < 2:
                continue
            h, l, cl = np.asarray(b.h, float), np.asarray(b.l, float), np.asarray(b.c, float)
            est = np.zeros((len(NS), len(cl)))
            for k, N in enumerate(NS):
                hi, lo = ind.max_previo(h, N), ind.min_previo(l, N)
                s = 0.0
                for i in range(len(cl)):
                    if np.isfinite(hi[i]):
                        if cl[i] > hi[i]:
                            s = 1.0
                        elif cl[i] < lo[i]:
                            s = -1.0
                    est[k, i] = s
            P = est.mean(0)
            Pa = np.r_[0.0, P[:-1]]
            ok = validas(b) & np.isfinite(ind.max_previo(h, max(NS)))
            up = ok & (P >= u - 1e-12) & (Pa < u - 1e-12)
            dn = ok & (P <= -u + 1e-12) & (Pa > -u + 1e-12)
            if p["reentrada"]:
                h10, l10 = ind.max_previo(h, 10), ind.min_previo(l, 10)
                with np.errstate(invalid="ignore"):
                    up |= ok & (P >= u - 1e-12) & (Pa >= u - 1e-12) & (cl > h10)
                    dn |= ok & (P <= -u + 1e-12) & (Pa <= -u + 1e-12) & (cl < l10)
            idx = np.flatnonzero(up | dn)
            if not len(idx):
                continue
            t = np.asarray(b.t_cierre)[idx]
            d = np.where(up[idx], 1, -1)
            sl = p["k_atr"] * ind.atr_pct(h, l, cl, 14)[idx]
            okk = np.isfinite(sl)
            partes.append(emitir(t[okk], ci, d[okk], stop_pct=sl[okk], clave=d[okk], prioridad=np.abs(P[idx][okk])))
        return Senales.juntar(partes)
