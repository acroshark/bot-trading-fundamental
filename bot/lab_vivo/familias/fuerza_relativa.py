"""Familia «fuerza_relativa»: las 4 monedas comparadas entre sí cada día a las 00:00 UTC (día UTC cerrado).

- «momento» (momento dual; Liu, Tsyvinski y Wu, 2022; Dobrynskaya, 2023): fuerza_i = ln(C_i / C_i(−L)) / σ_i(60).
  Largo en la 1.ª si su fuerza > 0 (y, con ventaja, si supera a la 2.ª en ese margen); corto en la 4.ª si < 0.
  Cada día mientras dure (el libro veta si la moneda está ocupada; la posición no se cierra al dejar de ser la 1.ª).
- «reversion» (reversión a corto plazo; Caporale y Plastun, 2019): d_i = r_i − media de las 4; z_i = d_i / desviación
  de d_i en los 60 días previos. Largo con z ≤ −z0 si BTC no está bajista; corto con z ≥ +z0 si BTC no está alcista.
Stop: k_atr × ATR(14) diario de la moneda. Prioridad: largos antes que cortos («momento») o |z| («reversion»)."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, tendencia_diaria


class FuerzaRelativa(Familia):
    nombre = "fuerza_relativa"
    version = 1
    titulo = "Fuerza relativa entre BTC, ETH, SOL y XRP"
    descripcion = "Se ordena cada día la fuerza de las 4 monedas y se opera la más fuerte, la más débil o la que más se separa."
    datos = ("velas_1m",)
    calentamiento_d = 95
    fuentes = ("Liu, Tsyvinski y Wu (2022), Common Risk Factors in Cryptocurrency",
               "Dobrynskaya (2023), Cryptocurrency Momentum and Reversal",
               "Caporale y Plastun (2019), Price overreactions in the cryptocurrency market")
    filtros = ("volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "modo": Param("str", "momento", ("momento", "reversion"), texto="modo"),
        "L": Param("int", 7, (3, 7, 14), texto="días del momento"),
        "ventaja": Param("float", 0.0, (0.0, 0.5), texto="ventaja mínima de la 1.ª sobre la 2.ª"),
        "z0": Param("float", 2.0, (1.5, 2.0, 2.5), texto="z mínimo de la separación"),
        "dias_r": Param("int", 1, (1, 3), texto="días del retorno de la separación"),
        "k_atr": Param("float", 0.75, (0.5, 0.75, 1.0), texto="stop k × ATR(14) diario"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        mons = [c for c in m.monedas]
        bars = {c: m.barras(c, 1440) for c in mons}
        if min(len(b) for b in bars.values()) < 2:
            return Senales.vacia()
        tcomun = sorted(set.intersection(*(set(np.asarray(b.t_cierre).tolist()) for b in bars.values())))
        tc = np.asarray(tcomun, np.int64)
        C = np.array([np.asarray(bars[c].c, float)[np.searchsorted(bars[c].t_cierre, tc)] for c in mons])
        ATR = np.array([ind.atr_pct(bars[c].h, bars[c].l, bars[c].c, 14)[np.searchsorted(bars[c].t_cierre, tc)]
                        for c in mons])
        sel = np.array([c in p["monedas"] for c in mons])
        ts, ms, ds, ss, pr = [], [], [], [], []
        if p["modo"] == "momento":
            L = p["L"]
            r = np.array([ind.retornos_log(x) for x in C])
            sd = np.array([ind.desv_previa(x, 60) for x in r])
            with np.errstate(divide="ignore", invalid="ignore"):
                F = np.log(C / np.array([ind.anterior(x, L) for x in C])) / sd
            for i in range(len(tc)):
                f = np.where(sel, F[:, i], np.nan)
                if np.isfinite(f).sum() < 2:
                    continue
                o = np.argsort(-np.nan_to_num(f, nan=-np.inf))
                top, seg = o[0], o[1]
                if f[top] > 0 and (p["ventaja"] <= 0 or f[top] - f[seg] >= p["ventaja"]):
                    ts.append(tc[i]); ms.append(top); ds.append(1); ss.append(p["k_atr"] * ATR[top, i]); pr.append(1.0)
                fin = [k for k in o if np.isfinite(f[k])][-1]
                if f[fin] < 0 and fin != top:
                    ts.append(tc[i]); ms.append(fin); ds.append(-1); ss.append(p["k_atr"] * ATR[fin, i]); pr.append(0.0)
        else:
            k = p["dias_r"]
            r = np.array([np.r_[np.full(k, np.nan), np.log(x[k:] / x[:-k])] for x in C])
            dev = r - np.nanmean(r, axis=0)
            sd = np.array([ind.desv_previa(x, 60) for x in dev])
            with np.errstate(divide="ignore", invalid="ignore"):
                Z = dev / sd
            btc = tendencia_diaria(m, "BTC", tc) if "BTC" in mons else np.zeros(len(tc), np.int8)
            for i in range(len(tc)):
                for j in range(len(mons)):
                    if not sel[j] or not np.isfinite(Z[j, i]):
                        continue
                    if Z[j, i] <= -p["z0"] and btc[i] >= 0:
                        ts.append(tc[i]); ms.append(j); ds.append(1); ss.append(p["k_atr"] * ATR[j, i]); pr.append(abs(Z[j, i]))
                    elif Z[j, i] >= p["z0"] and btc[i] <= 0:
                        ts.append(tc[i]); ms.append(j); ds.append(-1); ss.append(p["k_atr"] * ATR[j, i]); pr.append(abs(Z[j, i]))
        if not ts:
            return Senales.vacia()
        ss = np.asarray(ss, float)
        ok = np.isfinite(ss)
        return Senales(t=np.asarray(ts, np.int64)[ok], moneda=np.asarray(ms, np.int8)[ok], dir=np.asarray(ds, np.int8)[ok],
                       stop_pct=ss[ok], clave=np.asarray(ds, np.int32)[ok], prioridad=np.asarray(pr, float)[ok])
