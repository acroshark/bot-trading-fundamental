"""Familia «apertura_impulso»: la entrada del motor de aperturas tal cual (apertura.ImpulseRun). Mide el impulso de los
primeros N min de cada apertura de bolsa y entra a favor (t:N, pb o brk), con los filtros del motor (volumen
relativo, volatilidad y BTC). Es la REFERENCIA del laboratorio y la base del canario (docs/07, 7.6): lo que pase es
operable por el motor sin código nuevo (hipotesis.a_params_motor).

news_mode va fijo en «etiqueta»: la historia no tiene noticias (como salidas_motor.py y medir_selectiva.py)."""
import numpy as np

from .base import MIN, Familia, Param, Senales, emitir

VENTANAS = ("tokio-seul", "china", "europa", "saopaulo", "nuevayork")


class AperturaImpulso(Familia):
    nombre = "apertura_impulso"
    version = 1
    titulo = "Impulso de los primeros minutos de cada apertura (motor de aperturas)"
    descripcion = ("Mide el movimiento de los primeros N min desde la apertura de cada bolsa (apertura.ImpulseRun) y "
                   "entra a favor: a +N min, en la reanudación tras retroceder o en la ruptura del rango; con los "
                   "filtros del motor (volumen relativo, volatilidad, tendencia de BTC).")
    momento = "aperturas"
    datos = ("velas_1m", "aperturas")
    calentamiento_d = 35
    fuentes = ("La operativa del usuario (motor de aperturas)", "Laboratorio de 2.000 (familia ap_mom)")
    parametros = {
        "measure_min": Param("int", 15, (15, 30, 60), texto="minutos en que se mide el impulso"),
        "min_move_pct": Param("float", 0.0, min=0.0, max=2.0, texto="impulso mínimo (%)"),
        "entry": Param("str", "t:15", ("t:15", "t:30", "t:60", "pb", "brk"), texto="entrada"),
        "rvol_min": Param("float", None, (1.5, 2.0), filtro=True, neutro=None, nulo=True,
                          texto="volumen relativo mínimo desde la apertura"),
        "vol_filter": Param("str", "todos", ("todos", "no_baja", "alta", "baja"), filtro=True, neutro="todos",
                            texto="volatilidad (ATR de 14 h en percentil de 30 días)"),
        "btc_filter": Param("str", "ninguno", ("ninguno", "no_contra", "a_favor"), filtro=True, neutro="ninguno",
                            texto="tendencia diaria de BTC"),
        "windows": Param("lista", list(VENTANAS), VENTANAS, texto="aperturas"),
    }

    def params_motor(self, p: dict) -> dict:
        """Los parámetros de apertura.impulse_params que da esta variante (sin la salida, que pone el catálogo)."""
        from ... import apertura as ap
        over = dict(ap.SEIS)
        over.update({"measure_min": p["measure_min"], "min_move_pct": p["min_move_pct"], "entry": p["entry"],
                     "rvol_min": p["rvol_min"], "vol_filter": p["vol_filter"], "btc_filter": p["btc_filter"],
                     "news_mode": "etiqueta", "windows": list(p["windows"]), "coins": list(p["monedas"])})
        return over

    def senales_crudas(self, m, p: dict, desde_ms: int, hasta_ms: int) -> Senales:
        from ... import apertura as ap
        from .. import filtros
        PS, _ = ap.impulse_params(self.params_motor(p))
        fin = PS["session_max_min"] + 2
        ops = [o for o in m.aperturas if o["id"] in p["windows"] and desde_ms - fin * MIN <= o["open_ms"] < hasta_ms]
        partes = []
        need_btc = p["btc_filter"] != "ninguno"
        need_vol = p["vol_filter"] != "todos"
        for ci, coin in self.monedas_de(m, p):
            v = m.velas(coin)
            if not len(v):
                continue
            vt = np.asarray(v.t)
            ts, ds, ws = [], [], []
            oms = np.array([o["open_ms"] for o in ops], np.int64)
            btc = filtros.tendencia_btc(m, oms) if need_btc and len(oms) else None
            vol = filtros.percentil_atr(m, coin, oms) if need_vol and len(oms) else None
            for k, o in enumerate(ops):
                om = o["open_ms"]
                ctx = {"btc_dir": int(btc[k]) if btc is not None else 0,
                       "atr_pct": (float(vol[k]) if np.isfinite(vol[k]) else None) if vol is not None else None,
                       "prev_react": None}
                prior = None
                if p["rvol_min"]:
                    prior = []
                    for po in ap.prev_session_opens(o["lead"], om, 5):
                        prior.append((po, _barras(v, vt, po, po + fin * MIN)))
                run = ap.ImpulseRun(coin, om, PS, ctx, None, prior)
                act = None
                for b in _barras(v, vt, om, om + fin * MIN):
                    act = run.feed(b)
                    if act or run.done:
                        break
                if act and act.get("action") == "enter":
                    ts.append(int(act["t"]))
                    ds.append(int(act["dir"]))
                    ws.append(VENTANAS.index(o["id"]))
            if ts:
                partes.append(emitir(ts, ci, ds, ventana=ws, clave=ws))
        return Senales.juntar(partes)


def _barras(v, vt: np.ndarray, a: int, b: int) -> list[list]:
    i, j = int(np.searchsorted(vt, a)), int(np.searchsorted(vt, b))
    return [[t_, o_, h_, l_, c_, x_, 0.0, 0.0] for t_, o_, h_, l_, c_, x_ in
            zip(vt[i:j].tolist(), np.asarray(v.o[i:j]).tolist(), np.asarray(v.h[i:j]).tolist(),
                np.asarray(v.l[i:j]).tolist(), np.asarray(v.c[i:j]).tolist(), np.asarray(v.v[i:j]).tolist())]
