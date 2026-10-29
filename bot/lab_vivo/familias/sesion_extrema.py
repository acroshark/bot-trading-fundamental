"""Familia «sesion_extrema»: movimiento extremo de 8 h (una sesión), a la contra o a favor. Al cierre de cada vela de
1 h: r8 = ln(C / C de hace 8 h); σ8 = desviación de r8 en ventanas de 8 h no solapadas de los 30 días previos.
Evento: |r8| ≥ c × σ8 por primera vez (no se repite en las 8 h siguientes). «contra»: sentido = −signo(r8); «favor»:
+signo(r8). lado «caidas»: solo tras caídas.
Stop: a la contra, hasta el extremo de la ventana de 8 h + 0,2 %; a favor, 1,5 × ATR(14) de 4 h.
Caporale y Plastun (2019); Miralles-Quirós (2022). Prioridad: |r8| / σ8."""
import numpy as np

from .base import Familia, Param, Senales, atr_pct_en, emitir, validas


class SesionExtrema(Familia):
    nombre = "sesion_extrema"
    version = 1
    titulo = "Movimiento extremo de 8 h: a la contra o a favor"
    descripcion = "Tras un movimiento de 8 h muy por encima de lo normal, se opera su reversión (o su continuación)."
    datos = ("velas_1m",)
    calentamiento_d = 35
    fuentes = ("Caporale y Plastun (2019), Price overreactions in the cryptocurrency market",
               "Miralles-Quirós y Miralles-Quirós (2022), Intraday Bitcoin price shocks")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "c": Param("float", 2.5, (2.5, 3.0), texto="c × σ8"),
        "modo": Param("str", "contra", ("contra", "favor"), texto="a la contra o a favor"),
        "lado": Param("str", "ambos", ("ambos", "caidas"), texto="tras subidas y caídas o solo tras caídas"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 60)
            if len(b) < 2:
                continue
            h, l, cl = np.asarray(b.h, float), np.asarray(b.l, float), np.asarray(b.c, float)
            ok = validas(b)
            with np.errstate(divide="ignore", invalid="ignore"):
                r8 = np.r_[np.full(8, np.nan), np.log(cl[8:] / cl[:-8])]
            tc = np.asarray(b.t_cierre)
            # σ8 en i: desviación de los 90 r8 no solapados que acaban en i − 1 (la misma clase módulo 8)
            s8 = np.full(len(cl), np.nan)
            for c8 in range(8):
                idx = np.arange(c8, len(cl), 8)
                x = r8[idx]
                sd = np.full(len(idx), np.nan)
                if len(idx) >= 90:
                    w = np.lib.stride_tricks.sliding_window_view(x, 90)
                    okw = np.isfinite(w).sum(1) >= 45
                    with np.errstate(invalid="ignore", divide="ignore"):
                        sd[89:] = np.where(okw, np.nanstd(w, axis=1, ddof=1), np.nan)
                pos = idx + 1                        # el valor vale para la barra siguiente a la última de la ventana
                ok_p = pos < len(cl)
                s8[pos[ok_p]] = sd[ok_p]
            with np.errstate(invalid="ignore"):
                cand = np.flatnonzero(ok & np.isfinite(r8) & np.isfinite(s8) & (s8 > 0) & (np.abs(r8) >= p["c"] * s8))
            ts, ds, ss, pr = [], [], [], []
            ultimo = -10 ** 9
            for i in cand:
                if i < 728 or i - ultimo < 8:
                    continue
                if p["lado"] == "caidas" and r8[i] > 0:
                    continue
                ultimo = i
                d = -int(np.sign(r8[i])) if p["modo"] == "contra" else int(np.sign(r8[i]))
                if p["modo"] == "contra":
                    ext = l[i - 7:i + 1].min() if d > 0 else h[i - 7:i + 1].max()
                    sv = abs(cl[i] - ext) / cl[i] * 100 + 0.2
                else:
                    sv = 1.5 * atr_pct_en(m, c, 240, np.array([tc[i]], np.int64))[0]
                if not np.isfinite(sv):
                    continue
                ts.append(int(tc[i]))
                ds.append(d)
                ss.append(sv)
                pr.append(abs(r8[i]) / s8[i])
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr))
        return Senales.juntar(partes)
