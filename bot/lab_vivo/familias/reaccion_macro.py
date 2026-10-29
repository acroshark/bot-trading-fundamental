"""Familia «reaccion_macro»: seguir (o no) la reacción de la cripto a IPC, empleo y Fed (Corbet et al., 2020; Pyo y
Lee, 2020; Block Scholes, 2026). t = hora del dato (se sabe de antemano). R = cierre a t + W / apertura en t − 1;
z = R / σ_W, con σ_W la desviación de los rendimientos de W min de las ventanas de W min no solapadas de los 30 días
previos. Evento si |z| ≥ z_min. «favor»: sentido = signo(R); «contra»: −signo(R). Decisión a t + W.
Stop: máx(1,5 %, |R|) (si la reacción se deshace entera, fuera). Prioridad: |z|."""
import numpy as np

from .base import DAY, MIN, Familia, Param, Senales, apertura_en, cierre_en, emitir


class ReaccionMacro(Familia):
    nombre = "reaccion_macro"
    version = 1
    titulo = "Reacción de la cripto a IPC, empleo y Fed"
    descripcion = "Si la cripto reacciona con fuerza a un dato macro en los primeros minutos, se sigue (o se va a la contra)."
    datos = ("velas_1m", "macro")
    calentamiento_d = 35
    fuentes = ("Corbet, Larkin, Lucey, Meegan y Yarovaya (2020), Cryptocurrency reaction to FOMC Announcements",
               "Pyo y Lee (2020), Do FOMC and macroeconomic announcements affect Bitcoin prices?",
               "Block Scholes (2026), Is Bitcoin showing greater sensitivity to US CPI releases again?")
    filtros = ("regimen_btc", "volatilidad", "regimen_cripto")
    parametros = {
        "eventos": Param("lista", ["CPI", "NFP"], ("CPI", "NFP", "FOMC"), texto="datos macro"),
        "W": Param("int", 15, (15, 60, 90), texto="minutos de la reacción"),
        "modo": Param("str", "favor", ("favor", "contra"), texto="a favor o a la contra"),
        "z_min": Param("float", 2.0, (2.0,), texto="z mínimo"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        mt, mn = m.macro
        evs = [int(t) for t, nm in zip(np.asarray(mt).tolist(), mn)
               if any(e in str(nm).split("+") for e in p["eventos"])]
        W = p["W"]
        evs = [t for t in evs if desde_ms <= t + W * MIN < hasta_ms]
        if not evs:
            return Senales.vacia()
        te = np.asarray(evs, np.int64)
        partes = []
        for ci, c in self.monedas_de(m, p):
            den = m.denso(c)
            if den is None:
                continue
            o0 = apertura_en(m, c, te)
            c1 = cierre_en(m, c, te + W * MIN)
            cc = np.asarray(den.c, float)
            ts, ds, ss, pr = [], [], [], []
            for k, t in enumerate(te):
                if not (np.isfinite(o0[k]) and np.isfinite(c1[k])):
                    continue
                a = (t - 30 * DAY - den.t0) // MIN
                z = (t - den.t0) // MIN
                if a < 0 or z <= a:
                    continue
                w = cc[a:z:W]
                rr = np.diff(np.log(w))
                if len(rr) < 100:
                    continue
                sd = float(rr.std(ddof=1))
                R = float(np.log(c1[k] / o0[k]))
                if sd <= 0 or abs(R / sd) < p["z_min"]:
                    continue
                d = int(np.sign(R)) * (1 if p["modo"] == "favor" else -1)
                ts.append(int(t) + W * MIN)
                ds.append(d)
                ss.append(abs(R) * 100)
                pr.append(abs(R / sd))
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr))
        return Senales.juntar(partes)
