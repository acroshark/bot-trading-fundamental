"""Familia «cierre_nyse»: día fuerte del S&P 500 → la cripto lo sigue en la noche (FMI, 2022: contagio entre cripto y
bolsa). Decisión cuando se conoce el cierre de NYSE (15 min después: la regla del laboratorio para los cierres de
bolsa). r = cierre / cierre anterior − 1 del S&P 500; evento si |r| ≥ u. «favor»: sentido = signo(r); «contra»: −signo.
rezagadas: solo las monedas que en la sesión de NYSE se movieron a favor menos de 1,5 × |r|.
Stop: k_atr × ATR(14) diario. Prioridad: BTC, ETH, SOL, XRP."""
import numpy as np

from .base import MIN, Familia, Param, Senales, apertura_en, atr_pct_en, cierre_en, emitir


class CierreNyse(Familia):
    nombre = "cierre_nyse"
    version = 1
    titulo = "Día fuerte del S&P 500: la cripto lo sigue por la noche"
    descripcion = "Tras un día fuerte del S&P 500, se entra en la cripto en ese sentido al conocerse el cierre."
    datos = ("velas_1m", "bolsas", "aperturas")
    calentamiento_d = 20
    fuentes = ("FMI (2022), Cryptic Connections: Spillovers between Crypto and Equity Markets",
               "K33 Research, The entire BTC sell-off since April occurred during U.S. market hours")
    filtros = ("regimen_btc", "volatilidad", "funding", "regimen_cripto")
    parametros = {
        "u": Param("float", 0.015, (0.01, 0.015), texto="|r| mínimo del S&P 500 (fracción)"),
        "modo": Param("str", "favor", ("favor", "contra"), texto="a favor o a la contra"),
        "rezagadas": Param("bool", False, filtro=True, neutro=False, texto="solo las monedas rezagadas"),
        "k_atr": Param("float", 0.75, (0.75, 1.0), texto="stop k × ATR(14) diario"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        kt, dias, _, ret = m.bolsa("nuevayork")
        if not len(kt):
            return Senales.vacia()
        r = np.asarray(ret, float) / 100
        sel = (kt >= desde_ms) & (kt < hasta_ms) & (np.abs(r) >= p["u"])
        if not sel.any():
            return Senales.vacia()
        t = (np.asarray(kt)[sel] // MIN) * MIN
        rr = r[sel]
        d = np.where(rr > 0, 1, -1) * (1 if p["modo"] == "favor" else -1)
        ny = {o["day"]: o["open_ms"] for o in m.aperturas if o["id"] == "nuevayork"}
        aper = np.array([ny.get(x, 0) for x in np.asarray(dias, object)[sel]], np.int64)
        partes = []
        for ci, c in self.monedas_de(m, p):
            ok = np.ones(len(t), bool)
            if p["rezagadas"]:
                o0 = apertura_en(m, c, aper)
                c1 = cierre_en(m, c, aper + 390 * MIN)
                mov = (c1 / o0 - 1) * np.sign(rr)
                ok = (aper > 0) & np.isfinite(mov) & (mov < 1.5 * np.abs(rr))
            sl = p["k_atr"] * atr_pct_en(m, c, 1440, t)
            ok &= np.isfinite(sl)
            partes.append(emitir(t[ok], ci, d[ok], stop_pct=sl[ok], clave=d[ok], prioridad=-float(ci)))
        return Senales.juntar(partes)
