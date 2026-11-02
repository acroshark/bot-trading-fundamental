"""Familia «momento_continuo»: la tendencia que llega poco a poco sigue; la que llega a saltos, no («Frog in the Pan»,
Da, Gurun y Warachka, 2014: los inversores atienden a los saltos y pasan por alto la información que llega en dosis
pequeñas y seguidas, así que el precio la incorpora tarde).

Al cierre de cada barra de tf_min (a las 00:00 UTC con las diarias), con L = ventana_d × 1440 / tf_min barras:
1. Momento: PRET = ln(C / C de hace L barras) y z = PRET / (σ × √L), con σ la desviación de los rendimientos de las 90
   barras anteriores.
2. Información discreta: ID = signo(PRET) × (fracción de barras en contra − fracción de barras a favor) de esas L
   barras (la medida de Da, Gurun y Warachka). Muy negativo: el camino ha sido continuo (muchas barras pequeñas a
   favor); cerca de 0 o positivo: a saltos (pocas barras grandes y muchas en contra).
3. Señal: |z| ≥ z_min e ID ≤ −id_min, en el sentido de PRET. «estado»: una decisión en cada barra mientras se cumpla
   (el libro solo entra con la moneda libre: vuelve a entrar tras una salida si la tendencia sigue); «flanco»: solo la
   primera barra. Una barra sin datos (menos del 90 % de sus velas) no decide, pero tampoco parte la racha: el flanco
   se mira sobre la condición.
Stop: k_atr × ATR(14) diario (el último día UTC cerrado). Prioridad: −ID (lo más continuo primero)."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, atr_pct_en, emitir, flancos_dobles, validas

SIGMA_N = 90          # barras de la desviación de los rendimientos


def discrecion(c, L: int) -> tuple[np.ndarray, np.ndarray]:
    """(PRET, ID) en cada barra i con las L barras que acaban en i: PRET = ln(c[i] / c[i − L]); ID = signo(PRET) ×
    (fracción de rendimientos < 0 − fracción > 0) de los L rendimientos de (i − L, i]. NaN mientras no hay L."""
    c = np.asarray(c, float)
    n = len(c)
    pret, idd = np.full(n, np.nan), np.full(n, np.nan)
    if L <= 0 or n <= L:
        return pret, idd
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.r_[0.0, np.log(c[1:] / c[:-1])]
    cp = np.r_[0, np.cumsum(r > 0)]
    cn = np.r_[0, np.cumsum(r < 0)]
    i = np.arange(L, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pret[L:] = np.log(c[L:] / c[:-L])
    pos = cp[i + 1] - cp[i + 1 - L]
    neg = cn[i + 1] - cn[i + 1 - L]
    idd[L:] = np.sign(pret[L:]) * (neg - pos) / L
    return pret, idd


class MomentoContinuo(Familia):
    nombre = "momento_continuo"
    version = 1
    titulo = "Tendencia que llega poco a poco (Frog in the Pan), a favor"
    descripcion = ("Cuando el rendimiento de las últimas semanas se ha hecho con muchas barras pequeñas a favor (información "
                   "continua) y no a saltos, se entra a su favor y se deja correr.")
    datos = ("velas_1m",)
    calentamiento_d = 130
    fuentes = ("Da, Gurun y Warachka (2014), Frog in the Pan: Continuous Information and Momentum, Review of Financial "
               "Studies 27(11)",
               "Moskowitz, Ooi y Pedersen (2012), Time series momentum, Journal of Financial Economics 104(2)",
               "Liu y Tsyvinski (2021), Risks and Returns of Cryptocurrency, Review of Financial Studies 34(6)")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "tf_min": Param("int", 1440, (240, 1440), texto="barras de (min)"),
        "ventana_d": Param("int", 28, (14, 28), texto="días del momento"),
        "z_min": Param("float", 0.5, (0.5, 1.0), texto="z mínimo del rendimiento"),
        "id_min": Param("float", 0.2, (0.1, 0.2, 0.3), texto="continuidad mínima (−ID)"),
        "modo": Param("str", "estado", ("estado", "flanco"), texto="modo"),
        "k_atr": Param("float", 0.75, (0.5, 0.75, 1.0), texto="stop k × ATR(14) diario"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        tf = int(p["tf_min"])
        L = max(2, int(p["ventana_d"]) * 1440 // tf)
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, tf)
            if len(b) <= max(L, SIGMA_N) + 1:
                continue
            cl = np.asarray(b.c, float)
            tc = np.asarray(b.t_cierre)
            ok = validas(b)
            pret, idd = discrecion(cl, L)
            sd = ind.desv_previa(ind.retornos_log(cl), SIGMA_N)
            with np.errstate(divide="ignore", invalid="ignore"):
                z = pret / (sd * np.sqrt(L))
                continuo = idd <= -p["id_min"] + 1e-12
                up = np.isfinite(z) & (z >= p["z_min"]) & continuo
                dn = np.isfinite(z) & (z <= -p["z_min"]) & continuo
            if p["modo"] == "flanco":   # el flanco, sobre la condición: una barra sin datos no parte la racha en dos
                up, dn = flancos_dobles(up, dn)
            up, dn = up & ok, dn & ok
            sel = np.flatnonzero(up | dn)
            if not len(sel):
                continue
            t = tc[sel]
            d = np.where(up[sel], 1, -1)
            sl = p["k_atr"] * atr_pct_en(m, c, 1440, t)
            okk = np.isfinite(sl) & (sl > 0)
            partes.append(emitir(t[okk], ci, d[okk], stop_pct=sl[okk], clave=d[okk], prioridad=-idd[sel][okk]))
        return Senales.juntar(partes)
