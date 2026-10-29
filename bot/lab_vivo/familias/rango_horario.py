"""Familia «rango_horario»: ruptura del rango de un tramo fijo del calendario, al cierre de una vela de 15 min.

- «asia»: rango de las 00:00 UTC a la apertura de Europa (ventana «europa» de las aperturas reales); disparo de la
  apertura de Europa al cierre de Nueva York. Días con Europa abierta. Filtro de compresión: (máximo − mínimo) / ATR
  diario en el percentil q de los 20 días previos («London breakout»).
- «fin_de_semana»: rango del sábado 00:00 UTC al lunes 00:00 (o del viernes 21:00); disparo el lunes (24 u 8 h).
- «deribit»: rango del jueves 08:00 al viernes 08:00 UTC (vencimiento de opciones de Deribit); disparo hasta el
  sábado 08:00; todos los viernes, el último del mes o el último del trimestre.

Disparo: la primera vela de 15 min que cierra por encima de máximo + m o por debajo de mínimo − m, m = máx(5 pb, 5 %
del rango). Stop: el otro extremo + 0,1 % («extremo») o el menor de eso y 2 × ATR(14) de 4 h («extremo_atr»)."""
from datetime import datetime, timedelta, timezone

import numpy as np

from .. import indicadores as ind
from .base import DAY, H, MIN, Familia, Param, Senales, atr_pct_en, emitir, validas


def _ultimo_viernes(y: int, mth: int) -> int:
    d = datetime(y + (mth == 12), mth % 12 + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    return d.day


class RangoHorario(Familia):
    nombre = "rango_horario"
    version = 1
    titulo = "Ruptura del rango de un tramo del calendario"
    descripcion = ("Se marca el rango de un tramo fijo (Asia antes de Europa, el fin de semana o el día previo al "
                   "vencimiento de Deribit) y se sigue la primera ruptura al cierre de 15 min.")
    momento = "continuo"
    datos = ("velas_1m", "aperturas")
    calentamiento_d = 30
    fuentes = ("Eross, McGroarty, Urquhart y Wolfe (2019), The intraday dynamics of bitcoin",
               "«London breakout» (ruptura del rango de Asia)", "Deribit, Settlement (viernes 08:00 UTC)")
    filtros = ("regimen_btc", "volatilidad", "funding", "regimen_cripto")
    parametros = {
        "rango": Param("str", "asia", ("asia", "fin_de_semana", "deribit"), texto="tramo"),
        "desde": Param("str", "sabado", ("sabado", "viernes21"), texto="inicio del rango del fin de semana"),
        "disparo_h": Param("int", 24, (8, 24), texto="horas de disparo del lunes"),
        "q": Param("float", None, (30.0, 50.0), filtro=True, neutro=None, nulo=True,
                   texto="compresión: rango / ATR diario en el percentil ≤ q de 20 días"),
        "rvol15": Param("float", None, (2.0,), filtro=True, neutro=None, nulo=True,
                        texto="volumen de la vela de 15 min ≥ x × la mediana de 24 h"),
        "vencimientos": Param("str", "todos", ("todos", "mensual", "trimestral"), texto="vencimientos de Deribit"),
        "stop_modo": Param("str", "extremo", ("extremo", "extremo_atr"), texto="stop"),
    }

    def tramos(self, m, p, desde_ms, hasta_ms) -> list[tuple[int, int, int, int]]:
        """(rango desde, rango hasta, disparo desde, disparo hasta) del calendario."""
        from ... import regions
        out = []
        a = (desde_ms // DAY - 8) * DAY
        z = (hasta_ms // DAY + 1) * DAY
        if p["rango"] == "asia":
            eu = {o["open_ms"] // DAY: o["open_ms"] for o in m.aperturas if o["id"] == "europa"}
            for dd, te in sorted(eu.items()):
                tD = dd * DAY
                if not a <= tD < z:
                    continue
                dia = datetime.fromtimestamp(tD / 1000, timezone.utc).date()
                ny = int(regions.close_utc_on(regions.MARKETS["nuevayork"], dia).timestamp() * 1000)
                out.append((tD, te, te, ny))
        else:
            for tD in range(a, z, DAY):
                wd = (tD // DAY + 3) % 7   # 0 lunes … 6 domingo (UTC)
                if p["rango"] == "fin_de_semana" and wd == 0:
                    ini = tD - 2 * DAY if p["desde"] == "sabado" else tD - 3 * DAY + 21 * H
                    out.append((ini, tD, tD, tD + p["disparo_h"] * H))
                elif p["rango"] == "deribit" and wd == 4:
                    d = datetime.fromtimestamp(tD / 1000, timezone.utc)
                    ult = d.day == _ultimo_viernes(d.year, d.month)
                    if p["vencimientos"] == "mensual" and not ult:
                        continue
                    if p["vencimientos"] == "trimestral" and not (ult and d.month in (3, 6, 9, 12)):
                        continue
                    t8 = tD + 8 * H
                    out.append((t8 - DAY, t8, t8, t8 + DAY))
        return out

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        tramos = self.tramos(m, p, desde_ms, hasta_ms)
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 15)
            if len(b) < 2 or not tramos:
                continue
            t, tc = np.asarray(b.t), np.asarray(b.t_cierre)
            h, l, cl, v = (np.asarray(x, float) for x in (b.h, b.l, b.c, b.v))
            ok = validas(b)
            vr = ind.volumen_relativo(v, 96) if p["rvol15"] else None
            atr_d = atr_pct_en(m, c, 1440, np.array([r[1] for r in tramos], np.int64))
            ratios = []
            ts, ds, ss = [], [], []
            for k, (ra, rb, da, db) in enumerate(tramos):
                i0, i1 = int(np.searchsorted(t, ra)), int(np.searchsorted(t, rb))
                if i1 - i0 < max(1, 0.9 * (rb - ra) / (15 * MIN)):
                    ratios.append(np.nan)
                    continue
                hi, lo = float(h[i0:i1].max()), float(l[i0:i1].min())
                ref = float(cl[i1 - 1])
                rat = (hi - lo) / ref * 100 / atr_d[k] if np.isfinite(atr_d[k]) and atr_d[k] > 0 else np.nan
                ratios.append(rat)
                if p["q"] is not None:
                    prev = np.array([x for x in ratios[-21:-1] if np.isfinite(x)])
                    if len(prev) < 10 or not np.isfinite(rat) or (prev < rat).mean() > p["q"] / 100:
                        continue
                mg = max(5e-4 * ref, 0.05 * (hi - lo))
                j0, j1 = int(np.searchsorted(t, da)), int(np.searchsorted(tc, db, "right"))
                if j0 >= j1:
                    continue
                seg = slice(j0, j1)
                up = ok[seg] & (cl[seg] > hi + mg)
                dn = ok[seg] & (cl[seg] < lo - mg)
                if vr is not None:
                    with np.errstate(invalid="ignore"):
                        fuerte = vr[seg] >= p["rvol15"]
                hit = np.flatnonzero(up | dn)
                if not len(hit):
                    continue
                j = j0 + int(hit[0])
                if vr is not None and not fuerte[int(hit[0])]:
                    continue
                d = 1 if cl[j] > hi + mg else -1
                s = ((cl[j] - lo) if d > 0 else (hi - cl[j])) / cl[j] * 100 + 0.1
                if p["stop_modo"] == "extremo_atr":
                    a4 = atr_pct_en(m, c, 240, np.array([tc[j]], np.int64))[0]
                    if np.isfinite(a4):
                        s = min(s, 2 * a4)
                ts.append(int(tc[j]))
                ds.append(d)
                ss.append(s)
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=-np.asarray(ts, float)))
        return Senales.juntar(partes)
