"""Familia «noticia_racimo»: una historia que publican ≥ n medios distintos en 30 min, con el sentido que le pone la IA
local del bot, y que el precio confirma (H34 y H33 del catálogo; bot/entrada_noticias.py). Datos: noticias_3a.

Unidad: historia×moneda, la de bot/fiables.py: las monedas que nombra el triaje y, con generales «todas», las cuatro para
las historias que no nombran ninguna (cripto en general y macro). t0 = desde cuándo se pudo usar la historia (el sello
del medio que completa el racimo y el minuto del triaje). Solo hay triaje de las historias de cripto con ≥ 2 medios
(de los que sigue el bot) en 30 min y de las macro con ≥ 4 (noticias_historia.RACIMO_N): n = 2 o 3 no añade macro.
Sentido: «triaje» (alcista o bajista: |direccion| > 15) o «ruptura» (sin mirarlo: el de la ruptura). modo «contra» le da
la vuelta.
Confirmación (dentro de la ventana, solo con velas que cierran después de t0):
- «rango_4h» (H34): la primera barra de 15 min que cierra fuera del rango de las 4 h antes de t0 en ese sentido, en las
  2 h siguientes. Stop = máx(1,5 %, distancia al otro extremo del rango + 0,1 %); a la contra, el de la salida.
- «reflejo» (entrada_noticias.py): el primer cierre de 1 min en los 60 min desde t0 que se mueve a favor ≥ máx(0,5 %,
  2σ de 60 min de las 24 h anteriores) frente al cierre en t0. Stop: el de la salida.
Grupo «fiable» (H33): solo las de un grupo tipo×sentido que cumple los criterios fijados de bot/fiables.py con los 30
días UTC completos anteriores (acierto ajustado a 4 h ≥ 60 %, n ≥ 30, Wilson ≥ 50 % y constancia semanal).
Decisión: el cierre que confirma. Prioridad: medios en 30 min. Por flanco: por moneda y sentido, otra confirmación
menos de 30 min después de la anterior no sale (es la misma noticia contada otra vez)."""
import numpy as np

from .base import (MEDIOS_MODOS, mascara_medios, DAY, FIABLE, H, MIN, REFLEJO, Familia, Param, Senales, cierre_en, emitir, grupos_fiables,
                   rango_previo, sigma60_pct, ultimo_dia_completo, unidades_noticias, validas)


class NoticiaRacimo(Familia):
    nombre = "noticia_racimo"
    version = 1
    titulo = "Racimo de noticias de varios medios confirmado por el precio"
    descripcion = ("Cuando ≥ n medios distintos publican la misma historia en 30 min y el precio rompe en su sentido, "
                   "se entra a favor (o a la contra).")
    datos = ("velas_1m", "noticias_3a")
    calentamiento_d = 35
    fuentes = ("Lopez-Lira y Tang (2023-2025), Can ChatGPT Forecast Stock Price Movements? (arXiv 2304.07619)",
               "Liu y Tsyvinski (2021), Risks and Returns of Cryptocurrency, Review of Financial Studies 34(6)",
               "Tetlock (2007), Giving Content to Investor Sentiment, Journal of Finance 62(3)")
    filtros = ("regimen_btc", "volatilidad")
    parametros = {
        "n": Param("int", 3, (2, 3, 5), texto="medios distintos en 30 min (las macro se triaron con ≥ 4)"),
        "sentido": Param("str", "triaje", ("triaje", "ruptura"), texto="sentido de la entrada"),
        "modo": Param("str", "favor", ("favor", "contra"), texto="a favor o a la contra"),
        "confirmacion": Param("str", "rango_4h", ("rango_4h", "reflejo"), texto="confirmación del precio"),
        "generales": Param("str", "no", ("no", "todas"), texto="historias que no nombran moneda"),
        "importancia_min": Param("int", 0, (0, 5, 7), filtro=True, neutro=0, texto="importancia mínima del triaje"),
        "grupo": Param("str", "todos", ("todos", "fiable"), filtro=True, neutro="todos",
                       texto="solo grupos tipo×sentido fiables (criterios de bot/fiables.py)"),
        "medios": Param("str", "todos", MEDIOS_MODOS, filtro=True, neutro="todos",
                        texto="medios por su acierto hasta la fecha (fuera los malos / solo los buenos)"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        nt = m.noticias
        if nt is None or not len(nt):
            return Senales.vacia()
        conf = p["confirmacion"]
        espera = 2 * H if conf == "rango_4h" else REFLEJO["ventana_min"] * MIN
        nt = nt.tramo(desde_ms - espera - DAY, hasta_ms)   # las que pueden confirmar dentro de [desde, hasta)
        if not len(nt):
            return Senales.vacia()
        gen = p["generales"] == "todas"
        sen = nt.sentido()
        f, k = unidades_noticias(nt, m.monedas, gen)
        sel = (nt.n30[f] >= p["n"]) & (nt.imp[f] >= p["importancia_min"])
        if p["medios"] != "todos":
            sel &= mascara_medios(m, nt, m.monedas, gen, p["medios"])[f]
        if p["sentido"] == "triaje":
            sel &= sen[f] != 0
        f, k = f[sel], k[sel]
        if not len(f):
            return Senales.vacia()
        if p["grupo"] == "fiable":
            todo = m.noticias   # los grupos se miden con todas las anteriores (las que caben en la ventana de datos)
            d0, d1 = int(ultimo_dia_completo(nt.t[0])), int(ultimo_dia_completo(nt.t[-1]))
            tabla, _ = grupos_fiables(m, todo, m.monedas, gen, d0, d1)
            L = ultimo_dia_completo(nt.t[f])
            g = nt.tipo[f].astype(np.int64) * 2 + (sen[f] > 0)
            ok = (L >= d0) & (L <= d1)
            ok[ok] = tabla[L[ok] - d0, g[ok]]
            f, k = f[ok], k[ok]
        partes = []
        for ci, c in enumerate(m.monedas):
            if c not in p["monedas"]:
                continue
            filas = f[k == ci]
            if not len(filas):
                continue
            t0s = nt.t[filas]
            dirs = sen[filas] if p["sentido"] == "triaje" else np.zeros(len(filas), np.int8)
            if conf == "rango_4h":
                ts, ds, ss = self._rango(m, c, t0s, dirs)
            else:
                ts, ds, ss = self._reflejo(m, c, t0s, dirs)
            ok = ts >= 0
            if not ok.any():
                continue
            pr = nt.n30[filas][ok].astype(float)
            ts, ds, ss = ts[ok], ds[ok], ss[ok]
            if p["modo"] == "contra":   # el stop estructural es del lado de la ruptura: a la contra, el de la salida
                ds, ss = -ds, np.full(len(ds), np.nan)
            o = np.lexsort((-pr, ts))
            ts, ds, ss, pr = ts[o], ds[o], ss[o], pr[o]
            keep = np.ones(len(ts), bool)   # por flanco: otra confirmación en el mismo sentido < 30 min después, fuera
            ult = {1: -10 ** 18, -1: -10 ** 18}
            for i in range(len(ts)):
                if ts[i] - ult[int(ds[i])] < 30 * MIN:
                    keep[i] = False
                else:
                    ult[int(ds[i])] = int(ts[i])
            partes.append(emitir(ts[keep], ci, ds[keep], stop_pct=ss[keep], clave=ds[keep], prioridad=pr[keep]))
        return Senales.juntar(partes)

    @staticmethod
    def _rango(m, c, t0s, dirs):
        """H34: barra de 15 min que cierra fuera del rango de las 4 h previas, en las 2 h siguientes."""
        b = m.barras(c, 15)
        n = len(t0s)
        ts, ds, ss = np.full(n, -1, np.int64), np.zeros(n, np.int8), np.full(n, np.nan)
        if not len(b):
            return ts, ds, ss
        tc, cl = np.asarray(b.t_cierre), np.asarray(b.c, float)
        vb = validas(b)
        for i, t0 in enumerate(t0s.tolist()):
            hi, lo, fv = rango_previo(m, c, t0, 240)
            if not (fv >= 0.9 and np.isfinite(hi) and np.isfinite(lo)):
                continue
            j0 = int(np.searchsorted(tc, t0, "right"))
            j1 = int(np.searchsorted(tc, t0 + 2 * H, "right"))
            for j in range(j0, j1):
                if not vb[j]:
                    continue
                arriba, abajo = cl[j] > hi, cl[j] < lo
                d = int(dirs[i])
                if (d >= 0 and arriba) or (d <= 0 and abajo):
                    d = 1 if arriba else -1
                    otro = lo if d > 0 else hi
                    ts[i], ds[i] = int(tc[j]), d
                    ss[i] = max(1.5, abs(cl[j] - otro) / cl[j] * 100 + 0.1)
                    break
        return ts, ds, ss

    @staticmethod
    def _reflejo(m, c, t0s, dirs):
        """entrada_noticias.confirmar: primer cierre de 1 min en (t0, t0 + 60 min] a favor ≥ máx(0,5 %, 2σ60)."""
        d_ = m.denso(c)
        n = len(t0s)
        ts, ds, ss = np.full(n, -1, np.int64), np.zeros(n, np.int8), np.full(n, np.nan)
        if d_ is None or d_.n == 0:
            return ts, ds, ss
        cc, vv = np.asarray(d_.c, float), np.asarray(d_.valid, bool)
        p0s = cierre_en(m, c, t0s)
        for i, t0 in enumerate(t0s.tolist()):
            p0 = p0s[i]
            if not np.isfinite(p0) or p0 <= 0:
                continue
            sg = sigma60_pct(m, c, t0)
            umbral = max(REFLEJO["umbral_pct"], REFLEJO["k_sigma"] * sg) if sg else REFLEJO["umbral_pct"]
            a = int((t0 - d_.t0) // MIN)            # la vela que abre en t0 cierra en t0 + 1 min
            b = min(d_.n, a + REFLEJO["ventana_min"])
            if a < 0 or b <= a:
                continue
            mov = (cc[a:b] / p0 - 1) * 100
            d = int(dirs[i])
            ok = vv[a:b] & ((d * mov >= umbral - 1e-12) if d else (np.abs(mov) >= umbral - 1e-12))
            hit = np.flatnonzero(ok)
            if len(hit):
                j = int(hit[0])
                ts[i] = int(d_.t0 + (a + j + 1) * MIN)
                ds[i] = d if d else (1 if mov[j] > 0 else -1)
        return ts, ds, ss
