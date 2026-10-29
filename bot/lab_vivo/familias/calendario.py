"""Familia «calendario»: entradas largas por calendario, sin más señal que la hora.

- «hora»: todos los días a la hora UTC (21:00, las horas fuertes de BTC según Quantpedia; o 00:00); dia «viernes»:
  solo los viernes.
- «cambio_de_mes»: el último día natural del mes a las 00:00 UTC (desde −1) o dos días antes (−2) (efecto de cambio
  de mes: compras de los flujos que entran a fin de mes).
- «previa_macro»: antes_h horas antes de un dato macro (FOMC, IPC o empleo), la deriva previa (Lucca y Moench, 2015).
fin: cierre por tiempo de la señal para las variantes informativas («+2h», «dia4» = día 4 del mes siguiente,
«evento» = la hora del dato). Stop: el fijo de la salida o k_atr × ATR(14) diario. Prioridad: BTC, ETH, SOL, XRP."""
from datetime import datetime, timedelta, timezone

import numpy as np

from .base import DAY, H, Familia, Param, Senales, atr_pct_en, dia_semana_utc, emitir


class Calendario(Familia):
    nombre = "calendario"
    version = 1
    titulo = "Entradas por calendario (hora, cambio de mes, previa de datos macro)"
    descripcion = "Largo en las horas o días que la literatura señala como fuertes para la cripto."
    datos = ("velas_1m", "macro")
    calentamiento_d = 20
    fuentes = ("Quantpedia (2022), Are There Seasonal Intraday or Overnight Anomalies in Bitcoin?",
               "Turn-of-the-month effect en BTC, ETH y LTC (Managerial Finance, 2022)",
               "Lucca y Moench (2015), The Pre-FOMC Announcement Drift")
    filtros = ("regimen_btc", "volatilidad", "funding", "regimen_cripto")
    parametros = {
        "modo": Param("str", "hora", ("hora", "cambio_de_mes", "previa_macro"), texto="modo"),
        "hora": Param("int", 21, (0, 21), texto="hora UTC"),
        "dia": Param("str", "todos", ("todos", "viernes"), texto="días"),
        "desde": Param("int", -1, (-2, -1), texto="días antes del fin de mes"),
        "eventos": Param("lista", ["FOMC"], ("CPI", "NFP", "FOMC"), texto="datos macro"),
        "antes_h": Param("int", 24, (24, 48), texto="horas antes del dato"),
        "fin": Param("str", "ninguno", ("ninguno", "+2h", "dia4", "evento"), texto="cierre por tiempo de la señal"),
        "k_atr": Param("float", None, (0.75, 1.0), nulo=True, texto="stop k × ATR(14) diario"),
        "lados": Param("str", "largos", ("largos",), texto="sentidos"),
    }

    def tiempos(self, m, p, desde_ms, hasta_ms) -> tuple[np.ndarray, np.ndarray]:
        a, z = (desde_ms // DAY) * DAY, (hasta_ms // DAY + 1) * DAY
        ts, fins = [], []
        if p["modo"] == "hora":
            for tD in range(a, z, DAY):
                t = tD + p["hora"] * H
                if p["dia"] == "viernes" and dia_semana_utc([t])[0] != 4:
                    continue
                ts.append(t)
                fins.append(t + 2 * H if p["fin"] == "+2h" else 0)
        elif p["modo"] == "cambio_de_mes":
            d = datetime.fromtimestamp(a / 1000, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            while int(d.timestamp() * 1000) < z:
                nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
                ult = nxt - timedelta(days=1)
                t = int((ult + timedelta(days=p["desde"] + 1)).timestamp() * 1000)
                ts.append(t)
                fins.append(int((nxt + timedelta(days=3)).timestamp() * 1000) if p["fin"] == "dia4" else 0)
                d = nxt
        else:
            mt, mn = m.macro
            for t_ev, nm in zip(np.asarray(mt).tolist(), mn):
                if not any(e in str(nm).split("+") for e in p["eventos"]):
                    continue
                t = int(t_ev) - p["antes_h"] * H
                ts.append(t // 60_000 * 60_000)
                fins.append(int(t_ev) if p["fin"] == "evento" else 0)
        ts, fins = np.asarray(ts, np.int64), np.asarray(fins, np.int64)
        sel = (ts >= desde_ms) & (ts < hasta_ms)
        return ts[sel], fins[sel]

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        ts, fins = self.tiempos(m, p, desde_ms, hasta_ms)
        if not len(ts):
            return Senales.vacia()
        partes = []
        for ci, c in self.monedas_de(m, p):
            sl = p["k_atr"] * atr_pct_en(m, c, 1440, ts) if p["k_atr"] else np.full(len(ts), np.nan)
            ok = np.isfinite(sl) if p["k_atr"] else np.ones(len(ts), bool)
            partes.append(emitir(ts[ok], ci, 1, stop_pct=sl[ok], prioridad=-float(ci),
                                 fin_ms=np.where(fins[ok] > 0, fins[ok], 0)))
        return Senales.juntar(partes)
