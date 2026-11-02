"""Familia «ruptura_fallida»: la barrida de un extremo viejo de varios días que falla y vuelve dentro del rango («Turtle
Soup» de Connors y Raschke, 1995, llevada a velas de 1 h).

Al cierre de cada vela de 1 h (alineada a UTC):
1. Nivel: el mínimo (o el máximo) de las velas de las «horas» anteriores, sin la actual. Tiene que ser viejo: hecho
   hace al menos el 20 % de la ventana (en la regla original, el mínimo de 20 días hecho hace ≥ 4 días). Detrás de un
   extremo visible se amontonan los stops y las órdenes de los que operan su ruptura (Osler, 2003 y 2005).
2. Barrida: una vela cuyo mínimo perfora ese nivel. Solo la primera: las siguientes ven ya un mínimo nuevo, que no es
   viejo.
3. Fallo: en esa misma vela o en las «velas_max» − 1 siguientes, el primer cierre otra vez por encima del nivel. Ahí se
   decide, a la contra de la barrida: largo al contado tras barrer un mínimo; corto en perpetuo tras barrer un máximo y
   cerrar otra vez por debajo. Si en esas velas no vuelve dentro, la barrida no falló y no hay señal.
Stop: al otro lado del extremo de la barrida (el mínimo desde la barrida hasta la decisión) más un colchón de 0,1 %.
Objetivo (solo la salida OBJ, con el stop estructural): el otro lado de la ventana antes de la barrida (su máximo, tras
barrer el mínimo), en R del stop que usará el libro (el de la señal, subido a 1,5 % si es menor); el libro solo lo deja
si da ≥ 2R y ≥ 3 % neto.
Filtro propio: volumen de la vela de la barrida ≥ vol_x × la mediana de las 720 velas anteriores (saltan los stops).
Prioridad: la antigüedad del nivel (fracción de la ventana): cuanto más viejo, más órdenes detrás."""
import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .. import indicadores as ind
from .base import Familia, Param, Senales, emitir, validas

ANTIGUEDAD = 0.2      # el nivel, hecho hace ≥ 20 % de la ventana (Connors y Raschke: 4 de 20 días)
COLCHON_PCT = 0.1     # el stop, 0,1 % más allá del extremo de la barrida
SL_MIN = 1.5          # el stop mínimo del usuario: el libro sube a él los menores (el objetivo va en R de ese stop)
TROZO = 4096


def extremo_previo(x, n: int, maximo: bool) -> tuple[np.ndarray, np.ndarray]:
    """(valor, antigüedad en velas) del máximo o el mínimo de las n velas ANTERIORES a cada i (sin la i): NaN y 0
    mientras no hay n. Con empates, el más antiguo (argmax y argmin dan el primero)."""
    x = np.asarray(x, float)
    val = np.full(len(x), np.nan)
    edad = np.zeros(len(x), np.int64)
    if n <= 0 or len(x) <= n:
        return val, edad
    w = sliding_window_view(x[:-1], n)          # w[j] = x[j : j + n] → vale para i = j + n
    for a in range(0, len(w), TROZO):
        ww = w[a:a + TROZO]
        k = ww.argmax(axis=1) if maximo else ww.argmin(axis=1)
        i = np.arange(a, a + len(ww)) + n
        val[i] = ww[np.arange(len(ww)), k]
        edad[i] = n - k                          # el extremo está en la vela i − n + k
    return val, edad


class RupturaFallida(Familia):
    nombre = "ruptura_fallida"
    version = 1
    titulo = "Barrida de un extremo viejo de varios días que falla (Turtle Soup), a la contra"
    descripcion = ("Cuando una vela de 1 h perfora un mínimo (o un máximo) viejo de varios días y en pocas horas el precio "
                   "cierra otra vez dentro del rango, se entra a la contra de la barrida, con el stop detrás de ella.")
    datos = ("velas_1m",)
    calentamiento_d = 35
    fuentes = ("Connors y Raschke (1995), Street Smarts: High Probability Short-Term Trading Strategies (Turtle Soup)",
               "Osler (2003), Currency Orders and Exchange Rate Dynamics, Journal of Finance 58(5)",
               "Osler (2005), Stop-loss orders and price cascades in currency markets, Journal of International Money "
               "and Finance 24(2)")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "aperturas", "regimen_cripto")
    parametros = {
        "horas": Param("int", 48, (24, 48, 96), texto="horas de la ventana del nivel"),
        "velas_max": Param("int", 3, (1, 3, 6), texto="velas de 1 h para volver dentro (con la de la barrida)"),
        "vol_x": Param("float", None, (1.5, 2.0), filtro=True, neutro=None, nulo=True,
                       texto="volumen de la vela de la barrida ≥ x × la mediana de 30 días"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        nb = int(p["horas"])                     # velas de 1 h de la ventana
        vmax = int(p["velas_max"])
        edad_min = int(math.ceil(ANTIGUEDAD * nb))
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 60)
            if len(b) <= nb + 1:
                continue
            h, l, cl, v = (np.asarray(x, float) for x in (b.h, b.l, b.c, b.v))
            tc = np.asarray(b.t_cierre)
            ok = validas(b)
            lo, e_lo = extremo_previo(l, nb, False)
            hi, e_hi = extremo_previo(h, nb, True)
            with np.errstate(invalid="ignore"):
                barre = {1: ok & (l < lo) & (e_lo >= edad_min), -1: ok & (h > hi) & (e_hi >= edad_min)}
            if p["vol_x"]:
                cand = np.flatnonzero(barre[1] | barre[-1])
                vr = np.full(len(cl), np.nan)
                vr[cand] = ind.volumen_relativo_en(v, 720, cand)
                with np.errstate(invalid="ignore"):
                    fuerte = vr >= p["vol_x"]
                barre = {d: x & fuerte for d, x in barre.items()}
            ts, ds, ss, obs, pr = [], [], [], [], []
            for d, nivel, otro, edad in ((1, lo, hi, e_lo), (-1, hi, lo, e_hi)):
                for i in np.flatnonzero(barre[d]):
                    L = nivel[i]
                    ext = l[i] if d > 0 else h[i]
                    for j in range(i, min(len(cl), i + vmax)):
                        if not ok[j]:
                            break
                        ext = min(ext, l[j]) if d > 0 else max(ext, h[j])
                        if d * (cl[j] - L) > 0:      # cierra otra vez dentro del rango: la barrida falló
                            sl = abs(cl[j] - ext) / cl[j] * 100 + COLCHON_PCT
                            dist = d * (otro[i] - cl[j]) / cl[j] * 100
                            ts.append(int(tc[j]))
                            ds.append(d)
                            ss.append(sl)
                            obs.append(dist / max(SL_MIN, sl) if dist > 0 else np.nan)
                            pr.append(float(edad[i]) / nb)
                            break
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr, objetivo_r=obs))
        return Senales.juntar(partes)
