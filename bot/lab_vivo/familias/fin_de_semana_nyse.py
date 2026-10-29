"""Familia «fin_de_semana_nyse»: el movimiento de la cripto durante el fin de semana, ¿se corrige al abrir la bolsa de
Nueva York el lunes? (antes, el «hueco del CME»; el CME cotiza 24/7 desde el 29-05-2026, así que el mecanismo cambia:
se informa en dos tramos). Referencia: cierre de NYSE del último día hábil de la semana (apertura + 390 min).
Decisión: apertura de NYSE del siguiente día hábil (o el domingo a la reapertura antigua del CME, 18:00 de Nueva York).
M = precio en la decisión / precio de referencia − 1; evento si |M| ≥ umbral. «contra»: sentido = −signo(M); «favor»:
+signo(M). Stop: máx(1,5 %, 0,4 × |M|). Con la salida OBJ, el objetivo es el precio de referencia (cerrar el hueco):
objetivo_r = |M| / stop (el libro solo lo deja si da ≥ 2R y ≥ 3 % neto)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .base import DAY, MIN, Familia, Param, Senales, cierre_en, emitir

NY = ZoneInfo("America/New_York")


class FinDeSemanaNyse(Familia):
    nombre = "fin_de_semana_nyse"
    version = 1
    titulo = "Movimiento del fin de semana frente a la apertura de NYSE"
    descripcion = "Si la cripto se mueve mucho el fin de semana, se opera su corrección (o su continuación) al abrir NYSE."
    datos = ("velas_1m", "aperturas")
    calentamiento_d = 10
    fuentes = ("TradingView, What is CME gap: do they always fill?",
               "The Block (2026), CME Group to launch 24/7 crypto futures and options trading on May 29",
               "Caporale y Plastun (2019), The day of the week effect in the cryptocurrency market")
    filtros = ("regimen_btc", "volatilidad", "regimen_cripto")
    parametros = {
        "umbral": Param("float", 0.03, (0.03, 0.05), texto="|M| mínimo (fracción)"),
        "modo": Param("str", "contra", ("contra", "favor"), texto="a la contra o a favor"),
        "decision": Param("str", "nyse", ("nyse", "domingo_cme"), texto="momento de la decisión"),
    }

    def tramos(self, m) -> list[tuple[int, int]]:
        """(cierre de NYSE antes del fin de semana, apertura de NYSE después): días hábiles separados por ≥ 1,5 días."""
        ny = sorted(o["open_ms"] for o in m.aperturas if o["id"] == "nuevayork")
        return [(a + 390 * MIN, b) for a, b in zip(ny[:-1], ny[1:]) if 1.5 * DAY <= b - a <= 6 * DAY]

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        pares = []
        for ref, dec in self.tramos(m):
            if p["decision"] == "domingo_cme":
                d = datetime.fromtimestamp(ref / 1000, NY).date()
                while d.weekday() != 6:
                    d += timedelta(days=1)
                dec = int(datetime(d.year, d.month, d.day, 18, 0, tzinfo=NY).timestamp() * 1000)
            if desde_ms <= dec < hasta_ms:
                pares.append((ref, dec))
        if not pares:
            return Senales.vacia()
        refs = np.array([r for r, _ in pares], np.int64)
        decs = np.array([d for _, d in pares], np.int64)
        partes = []
        for ci, c in self.monedas_de(m, p):
            pr_, pd_ = cierre_en(m, c, refs), cierre_en(m, c, decs)
            M = pd_ / pr_ - 1
            ok = np.isfinite(M) & (np.abs(M) >= p["umbral"])
            if not ok.any():
                continue
            d = np.where(M > 0, 1, -1) * (-1 if p["modo"] == "contra" else 1)
            s = np.maximum(1.5, 0.4 * np.abs(M) * 100)
            dist = np.abs(pr_ - pd_) / pd_ * 100
            obj = np.where(p["modo"] == "contra", dist / s, np.nan)
            partes.append(emitir(decs[ok], ci, d[ok], stop_pct=s[ok], clave=d[ok], prioridad=np.abs(M[ok]),
                                 objetivo_r=obj[ok]))
        return Senales.juntar(partes)
