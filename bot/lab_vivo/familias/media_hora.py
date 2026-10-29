"""Familia «media_hora» (solo informativa, fuera de reglas): momento intradía de la primera media hora a la última
(Gao, Han, Li y Zhou, 2018, «Market intraday momentum»). r1 = rendimiento de la primera media hora de la sesión (día UTC,
NYSE o Tokio); sentido = signo(r1) (con z_min, solo si |r1| ≥ z_min × σ de las primeras medias horas de 60 sesiones).
Entrada al empezar la última media hora de la sesión y cierre al final (fin_ms; hasta «00:30 d+1» en el día UTC).
Sin stop ni objetivo: la salida INFO. Nunca puede pasar a la cartera."""
from datetime import datetime, timezone

import numpy as np

from .base import DAY, MIN, Familia, Param, Senales, apertura_en, cierre_en, emitir


class MediaHora(Familia):
    nombre = "media_hora"
    version = 1
    titulo = "Primera media hora → última media hora (momento intradía)"
    descripcion = "El sentido de la primera media hora de la sesión anticipa el de la última media hora."
    datos = ("velas_1m", "aperturas")
    calentamiento_d = 70
    fuentes = ("Gao, Han, Li y Zhou (2018), Market intraday momentum, JFE 129(2)",
               "Shen, Urquhart y Wang (2022), Bitcoin intraday time series momentum")
    filtros = ()
    parametros = {
        "sesion": Param("str", "utc", ("utc", "nyse", "tokio"), texto="sesión"),
        "z_min": Param("float", 0.0, (0.0, 1.0), texto="|r1| mínimo en σ (0 = cualquiera)"),
        "hasta": Param("str", "fin", ("fin", "00:30 d+1"), texto="cierre"),
    }

    def sesiones(self, m, p, desde_ms, hasta_ms) -> list[tuple[int, int, int, int]]:
        """(apertura, fin de la primera media hora, inicio de la última media hora, cierre)."""
        from ... import regions
        out = []
        if p["sesion"] == "utc":
            for tD in range((desde_ms // DAY - 70) * DAY, (hasta_ms // DAY + 1) * DAY, DAY):
                fin = tD + DAY + (30 * MIN if p["hasta"] == "00:30 d+1" else 0)
                out.append((tD, tD + 30 * MIN, tD + DAY - 30 * MIN, fin))
        else:
            wid, key = ("nuevayork", "nuevayork") if p["sesion"] == "nyse" else ("tokio-seul", "tokio")
            mk = regions.MARKETS[key]
            for o in m.aperturas:
                if o["id"] != wid or o["open_ms"] < desde_ms - 70 * DAY or o["open_ms"] >= hasta_ms:
                    continue
                d = datetime.fromtimestamp(o["open_ms"] / 1000, timezone.utc).astimezone(
                    __import__("zoneinfo").ZoneInfo(mk.tz)).date()
                cierre = int(regions.close_utc_on(mk, d).timestamp() * 1000)
                out.append((o["open_ms"], o["open_ms"] + 30 * MIN, cierre - 30 * MIN, cierre))
        return out

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        ses = self.sesiones(m, p, desde_ms, hasta_ms)
        if not ses:
            return Senales.vacia()
        a = np.array([s[0] for s in ses], np.int64)
        b = np.array([s[1] for s in ses], np.int64)
        e = np.array([s[2] for s in ses], np.int64)
        f = np.array([s[3] for s in ses], np.int64)
        partes = []
        for ci, c in self.monedas_de(m, p):
            r1 = np.log(cierre_en(m, c, b) / apertura_en(m, c, a))
            ts, ds, fins = [], [], []
            for k in range(len(ses)):
                if not np.isfinite(r1[k]) or r1[k] == 0 or not desde_ms <= e[k] < hasta_ms:
                    continue
                if p["z_min"] > 0:
                    prev = r1[max(0, k - 60):k]
                    prev = prev[np.isfinite(prev)]
                    if len(prev) < 30 or abs(r1[k]) < p["z_min"] * prev.std(ddof=1):
                        continue
                ts.append(int(e[k]))
                ds.append(int(np.sign(r1[k])))
                fins.append(int(f[k]))
            if ts:
                partes.append(emitir(ts, ci, ds, clave=ds, fin_ms=fins))
        return Senales.juntar(partes)
