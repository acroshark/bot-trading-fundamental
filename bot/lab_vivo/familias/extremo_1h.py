"""Familia «extremo_1h»: reversión tras un movimiento extremo de 1 h con volumen (no hay historia de liquidaciones: el
volumen hace de aproximación).

- «cascada» (largos al contado): r_1h ≤ −k × σ de las 720 horas previas y volumen ≥ vol_x × su mediana. Estabilización:
  en las 3 h siguientes, la primera vela de 15 min que cierra por encima de su apertura y del cierre de la vela de la
  cascada; si antes el precio perfora el mínimo de la cascada, el evento se anula (no se persigue el cuchillo). Sin
  estabilización, se entra al cierre de la vela de la cascada.
- «euforia» (cortos en perpetuo): al revés, y además la última tasa de funding ≥ su percentil funding_p de 90
  liquidaciones (salvo funding_p nulo).
Stop: al otro lado del extremo de la vela + 0,2 %. Prioridad: lo extremo del movimiento (|r| / σ).
Jegadeesh (1990), Lehmann (1990): reversión a corto plazo; Corbet y Katsiampa (2018-2020)."""
import numpy as np

from .. import indicadores as ind
from .base import H, Familia, Param, Senales, emitir, flancos, funding8, validas


class Extremo1h(Familia):
    nombre = "extremo_1h"
    version = 1
    titulo = "Rebote tras una cascada (o corto tras la euforia) de 1 h"
    descripcion = ("Tras una hora extrema con volumen, se espera a que el precio se estabilice y se entra a la contra; "
                   "si sigue empujando, se anula.")
    datos = ("velas_1m", "funding")
    calentamiento_d = 35
    fuentes = ("Jegadeesh (1990), Lehmann (1990): reversión a corto plazo",
               "Corbet y Katsiampa (2018-2020), Asymmetric mean reversion of Bitcoin price returns",
               "Amberdata (2025), Leverage, Liquidations & the $31B Deleveraging")
    filtros = ("regimen_btc", "volatilidad", "dias", "regimen_cripto")
    parametros = {
        "modo": Param("str", "cascada", ("cascada", "euforia"), texto="cascada (largo) o euforia (corto)"),
        "k": Param("float", 3.0, (3.0, 4.0, 5.0), texto="k × σ de la hora"),
        "vol_x": Param("float", 3.0, (3.0,), texto="volumen ≥ x × la mediana de 30 días"),
        "estabilizacion": Param("bool", True, texto="esperar la vela de 15 min que confirma"),
        "funding_p": Param("float", 90.0, (90.0,), nulo=True, filtro=True, neutro=None,
                           texto="funding ≥ su percentil (euforia)"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        casc = p["modo"] == "cascada"
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 60)
            if len(b) < 2:
                continue
            h, l, cl, v = (np.asarray(x, float) for x in (b.h, b.l, b.c, b.v))
            r = ind.retornos_log(cl)
            sd = ind.desv_previa(r, 720)
            ok = validas(b) & np.isfinite(sd)
            with np.errstate(invalid="ignore"):
                ev = ok & ((r <= -p["k"] * sd) if casc else (r >= p["k"] * sd))
            cand = np.flatnonzero(ev)
            with np.errstate(invalid="ignore"):
                ev[cand] = ind.volumen_relativo_en(v, 720, cand) >= p["vol_x"]
            ev = flancos(ev)
            tc = np.asarray(b.t_cierre)
            if not casc and p["funding_p"] is not None:
                ft, fr = funding8(m, c)
                pf = ind.percentil_previo(fr, 90)
                k = np.searchsorted(ft, tc, "right") - 1
                with np.errstate(invalid="ignore"):
                    alto = (k >= 0) & (np.where(k >= 0, pf[np.clip(k, 0, max(0, len(pf) - 1))], np.nan)
                                       >= p["funding_p"] / 100) if len(pf) else np.zeros(len(tc), bool)
                ev &= alto
            idx = np.flatnonzero(ev)
            if not len(idx):
                continue
            b15 = m.barras(c, 15)
            t15, tc15 = np.asarray(b15.t), np.asarray(b15.t_cierre)
            o15, h15, l15, c15 = (np.asarray(x, float) for x in (b15.o, b15.h, b15.l, b15.c))
            ts, ds, ss, pr = [], [], [], []
            fin_ultimo = -1
            for i in idx:
                t0 = int(tc[i])
                if t0 < fin_ultimo:   # un evento a la vez por moneda
                    continue
                ext = l[i] if casc else h[i]
                if not p["estabilizacion"]:
                    td, ref = t0, cl[i]
                else:
                    a, z = int(np.searchsorted(t15, t0)), int(np.searchsorted(tc15, t0 + 3 * H, "right"))
                    td = None
                    for j in range(a, z):
                        if (casc and l15[j] < ext) or (not casc and h15[j] > ext):
                            break
                        if (casc and c15[j] > o15[j] and c15[j] > cl[i]) or \
                                (not casc and c15[j] < o15[j] and c15[j] < cl[i]):
                            td, ref = int(tc15[j]), c15[j]
                            break
                    fin_ultimo = t0 + 3 * H
                    if td is None:
                        continue
                ts.append(td)
                ds.append(1 if casc else -1)
                ss.append(abs(ref - ext) / ref * 100 + 0.2)
                pr.append(abs(r[i]) / sd[i])
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr))
        return Senales.juntar(partes)
