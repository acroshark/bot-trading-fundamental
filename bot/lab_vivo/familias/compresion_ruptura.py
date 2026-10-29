"""Familia «compresion_ruptura»: el rango de las últimas ventana_h horas está en el percentil más bajo de los 30 días
previos (compresión) y después una barra de tf_min cierra fuera de ese rango antes de caduca_h (expansión). La
volatilidad se agrupa y vuelve a su media: tras rangos estrechos vienen amplios (Crabel, 1990, NR4/NR7; Bollinger,
2001, compresión de las bandas).

Stop: al otro lado del rango comprimido (estructural). Prioridad: compresión más extrema (percentil más bajo)."""
import numpy as np

from .. import indicadores as ind
from .base import H, Familia, Param, Senales, emitir, flancos, validas


class CompresionRuptura(Familia):
    nombre = "compresion_ruptura"
    version = 1
    titulo = "Ruptura tras una compresión del rango"
    descripcion = ("Cuando el rango de las últimas horas es de los más estrechos de 30 días, se espera la ruptura de "
                   "ese rango al cierre de una barra y se entra a favor; stop al otro lado del rango.")
    datos = ("velas_1m",)
    calentamiento_d = 35
    fuentes = ("Crabel (1990), Day Trading with Short Term Price Patterns and Opening Range Breakout",
               "Bollinger (2001), Bollinger on Bollinger Bands")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "aperturas", "regimen_cripto")
    parametros = {
        "ventana_h": Param("int", 24, (12, 24, 48), texto="horas del rango"),
        "percentil": Param("float", 0.2, (0.1, 0.2), texto="percentil máximo del rango (30 días)"),
        "tf_min": Param("int", 60, (15, 60), texto="barras de la ruptura (min)"),
        "caduca_h": Param("int", 24, (12, 24), texto="horas para romper"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        w = p["ventana_h"]
        partes = []
        for ci, c in self.monedas_de(m, p):
            b1 = m.barras(c, 60)
            if len(b1) < w + 1:
                continue
            h1, l1, c1 = np.asarray(b1.h, float), np.asarray(b1.l, float), np.asarray(b1.c, float)
            hi = np.r_[np.full(w - 1, np.nan), np.lib.stride_tricks.sliding_window_view(h1, w).max(1)]
            lo = np.r_[np.full(w - 1, np.nan), np.lib.stride_tricks.sliding_window_view(l1, w).min(1)]
            rng = (hi - lo) / c1 * 100
            pct = ind.percentil_previo(rng, 720)
            with np.errstate(invalid="ignore"):
                comp = validas(b1) & (pct <= p["percentil"])
            ev = np.flatnonzero(flancos(comp))
            if not len(ev):
                continue
            b = m.barras(c, p["tf_min"])
            tc, cl, ok = np.asarray(b.t_cierre), np.asarray(b.c, float), validas(b)
            ts, ds, ss, pr = [], [], [], []
            tce = np.asarray(b1.t_cierre)
            for i in ev:
                t0 = tce[i]
                a = int(np.searchsorted(tc, t0, "right"))
                z = int(np.searchsorted(tc, t0 + p["caduca_h"] * H, "right"))
                if a >= z:
                    continue
                seg = slice(a, z)
                up = ok[seg] & (cl[seg] > hi[i])
                dn = ok[seg] & (cl[seg] < lo[i])
                hit = np.flatnonzero(up | dn)
                if not len(hit):
                    continue
                k = a + int(hit[0])
                d = 1 if cl[k] > hi[i] else -1
                ts.append(int(tc[k]))
                ds.append(d)
                ss.append(((cl[k] - lo[i]) if d > 0 else (hi[i] - cl[k])) / cl[k] * 100)
                pr.append(1 - float(pct[i]))
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr))
        return Senales.juntar(partes)
