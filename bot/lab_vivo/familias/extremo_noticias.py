"""Familia «extremo_noticias»: un movimiento extremo de horas sin noticias detrás se da la vuelta; con noticias, sigue
(Chan, 2003, y Savor, 2012, en acciones: las grandes variaciones sin titulares revierten y las que tienen información
derivan). Datos: noticias_3a (las historias en racimo con el triaje de la IA local, desde que se pudieron usar).

Al cierre de cada vela de 1 h (alineada a UTC):
1. Movimiento: r = ln(C / C de hace «horas_mov» velas) y z = r / (σ × √horas_mov), con σ la desviación de los
   rendimientos de 1 h de las 720 velas anteriores. Evento: |z| ≥ k por primera vez (por flanco), con las velas del
   movimiento y la de su base (la de hace «horas_mov» velas) válidas: tras un hueco de datos, la base sería el último
   cierre antes del hueco y el movimiento, uno que nadie vio en esas horas. Un flanco a menos de «horas_mov» velas del
   anterior de la misma moneda (dé señal o no) no decide: es el mismo movimiento, que la ventana móvil vuelve a ver.
2. Noticias: las historias×moneda (las que nombran la moneda y, con generales «todas», las que no nombran ninguna:
   cripto en general y macro) en racimo de ≥ n medios que ya se podían usar en [decisión − ventana_h, decisión). No se
   mira su sentido (el de los titulares no anticipa: docs/05, sección 22): solo si las hay.
3. «sin»: solo los eventos sin ninguna; «con»: solo los que tienen alguna; «da_igual»: todos (el control). Sentido:
   «contra» (la reversión) o «favor» (la deriva).
Si en las 24 h anteriores no hay ninguna historia de ninguna moneda (un hueco del histórico), no se decide: no se sabe
si había noticias. Sin el archivo de noticias no hay señales.
Stop: a la contra, al otro lado del extremo del movimiento (las «horas_mov» velas) más un colchón de 0,2 %; a favor,
1,5 × ATR(14) de 4 h. Objetivo (solo la salida OBJ, a la contra y con el stop estructural): deshacer el movimiento (el
cierre de hace «horas_mov» velas), en R del stop que usará el libro (el de la señal, subido a 1,5 % si es menor); el
libro solo lo deja si da ≥ 2R y ≥ 3 % neto. Prioridad: |z|."""
import numpy as np

from .. import indicadores as ind
from .base import DAY, H, Familia, Param, Senales, atr_pct_en, emitir, flancos_dobles, unidades_noticias, validas

COLCHON_PCT = 0.2     # el stop a la contra, 0,2 % más allá del extremo del movimiento (como sesion_extrema)
SL_MIN = 1.5          # el stop mínimo del usuario: el libro sube a él los menores (el objetivo va en R de ese stop)
GUARDA_MS = DAY       # sin ninguna historia en las 24 h previas, el histórico tiene un hueco: no se decide


class ExtremoNoticias(Familia):
    nombre = "extremo_noticias"
    version = 1
    titulo = "Movimiento extremo de horas con o sin noticias: la vuelta si no las hay, la deriva si las hay"
    descripcion = ("Tras un movimiento extremo de 1 a 4 h se mira si había noticias de esa moneda en las horas previas: sin "
                   "noticias se espera la vuelta (era liquidez); con noticias, la continuación (era información).")
    datos = ("velas_1m", "noticias_3a")
    calentamiento_d = 35
    fuentes = ("Chan (2003), Stock price reaction to news and no-news: drift and reversal after headlines, Journal of "
               "Financial Economics 70(2)",
               "Savor (2012), Stock returns after major price shocks: The impact of information, Journal of Financial "
               "Economics 106(3)",
               "Caporale y Plastun (2019), Price overreactions in the cryptocurrency market")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "noticias": Param("str", "sin", ("sin", "con", "da_igual"), texto="eventos sin noticias, con noticias o todos"),
        "modo": Param("str", "contra", ("contra", "favor"), texto="a la contra o a favor del movimiento"),
        "horas_mov": Param("int", 1, (1, 4), texto="horas del movimiento"),
        "k": Param("float", 3.0, (3.0, 4.0), texto="k × σ del movimiento"),
        "ventana_h": Param("int", 6, (6, 12), texto="horas de noticias antes de la decisión"),
        "n": Param("int", 2, (2, 3), texto="medios distintos en 30 min (solo hay triaje de los racimos: cripto con ≥ 2 "
                                           "y macro con ≥ 3)"),
        "generales": Param("str", "todas", ("todas", "no"), texto="historias que no nombran moneda"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        nt = m.noticias
        if nt is None or not len(nt):
            return Senales.vacia()
        w = int(p["horas_mov"])
        ven = int(p["ventana_h"]) * H
        f, k = unidades_noticias(nt, m.monedas, p["generales"] == "todas")
        sel = nt.n30[f] >= p["n"]
        f, k = f[sel], k[sel]
        t_todas = np.asarray(nt.t, np.int64)            # todas las historias: para ver los huecos del histórico
        partes = []
        for ci, c in self.monedas_de(m, p):
            b = m.barras(c, 60)
            if len(b) <= 720 + w:
                continue
            h, l, cl = (np.asarray(x, float) for x in (b.h, b.l, b.c))
            tc = np.asarray(b.t_cierre)
            v0 = validas(b)
            ok = v0.copy()
            for q in range(1, w + 1):                   # las velas del movimiento y la de su base, válidas
                ok &= np.r_[np.zeros(q, bool), v0[:-q]]
            sd = ind.desv_previa(ind.retornos_log(cl), 720)
            atr4 = atr_pct_en(m, c, 240, tc) if p["modo"] == "favor" else None   # una vez por moneda, no por evento
            with np.errstate(divide="ignore", invalid="ignore"):
                r = np.r_[np.full(w, np.nan), np.log(cl[w:] / cl[:-w])]
                z = r / (sd * np.sqrt(w))
                up, dn = flancos_dobles(ok & (z >= p["k"]), ok & (z <= -p["k"]))
            tn = np.sort(np.asarray(nt.t, np.int64)[f[k == ci]])
            ts, ds, ss, obs, pr = [], [], [], [], []
            ultimo = -10 ** 9
            for i in np.flatnonzero(up | dn):
                previo, ultimo = ultimo, i
                if i - previo < w:                      # el mismo movimiento (cuenta desde el flanco anterior)
                    continue
                t = int(tc[i])
                if np.searchsorted(t_todas, t, "left") == np.searchsorted(t_todas, t - GUARDA_MS, "left"):
                    continue                            # un hueco del histórico: no se sabe si había noticias
                hay = np.searchsorted(tn, t, "left") > np.searchsorted(tn, t - ven, "left")
                if (p["noticias"] == "sin" and hay) or (p["noticias"] == "con" and not hay):
                    continue
                sg = 1 if r[i] > 0 else -1
                d = -sg if p["modo"] == "contra" else sg
                if p["modo"] == "contra":
                    ext = h[i - w + 1:i + 1].max() if d < 0 else l[i - w + 1:i + 1].min()
                    sl = abs(ext - cl[i]) / cl[i] * 100 + COLCHON_PCT
                    ob = abs(cl[i] - cl[i - w]) / cl[i] * 100 / max(SL_MIN, sl)
                else:
                    sl = 1.5 * float(atr4[i])
                    ob = np.nan
                if not np.isfinite(sl) or sl <= 0:
                    continue
                ts.append(t)
                ds.append(d)
                ss.append(sl)
                obs.append(ob)
                pr.append(float(abs(z[i])))
            if ts:
                partes.append(emitir(ts, ci, ds, stop_pct=ss, clave=ds, prioridad=pr, objetivo_r=obs))
        return Senales.juntar(partes)
