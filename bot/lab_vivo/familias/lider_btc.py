"""Familia «lider_btc»: las alts siguen con retraso a un impulso de BTC (el precio se descubre en BTC; las alts, con
beta > 1, recorren más y más tarde). Al cierre de cada vela de 15 min: impulso si |r15 de BTC| ≥ u con volumen ≥ 3 ×
la mediana de 15 min de 30 días (la primera vela de cada episodio); β = pendiente de r15 de la alt frente a r15 de BTC en los 30 días previos; rezagada si
r15_alt / (β × r15_BTC) < 0,5. Se entra en la alt rezagada en el sentido de BTC. solo_mayor_beta: solo la de mayor β.
«donchian»: el impulso es una ruptura de Donchian de 4 h (N = 20) de BTC y la alt no ha roto su propio canal.
Stop: 2 × ATR(14) de 1 h de la alt (de 4 h con «donchian»). Prioridad: rezago × β.
Sifat, Mohamad y Shariff (2019): adelanto y retraso entre BTC y ETH."""
import numpy as np

from .. import indicadores as ind
from .base import Familia, Param, Senales, atr_pct_en, emitir, flancos, flancos_dobles, validas

N30 = 2880


def _rolling_beta(x: np.ndarray, y: np.ndarray, n: int) -> np.ndarray:
    """Pendiente MCO de y sobre x con las n barras ANTERIORES (sin la actual)."""
    ok = np.isfinite(x) & np.isfinite(y)
    x0, y0 = np.where(ok, x, 0.0), np.where(ok, y, 0.0)
    cs = lambda a: np.r_[0.0, np.cumsum(a)]   # noqa: E731
    Sx, Sy, Sxx, Sxy, Sn = cs(x0), cs(y0), cs(x0 * x0), cs(x0 * y0), cs(ok.astype(float))
    out = np.full(len(x), np.nan)
    i = np.arange(n, len(x))
    nn = Sn[i] - Sn[i - n]
    mx, my = (Sx[i] - Sx[i - n]) / np.maximum(nn, 1), (Sy[i] - Sy[i - n]) / np.maximum(nn, 1)
    vx = (Sxx[i] - Sxx[i - n]) / np.maximum(nn, 1) - mx * mx
    cxy = (Sxy[i] - Sxy[i - n]) / np.maximum(nn, 1) - mx * my
    with np.errstate(divide="ignore", invalid="ignore"):
        out[i] = np.where((nn >= n // 2) & (vx > 0), cxy / vx, np.nan)
    return out


class LiderBtc(Familia):
    nombre = "lider_btc"
    version = 1
    titulo = "Las alts siguen con retraso a un impulso de BTC"
    descripcion = "Tras un impulso fuerte de BTC, se entra en la alt que aún no se ha movido lo que le toca por su beta."
    datos = ("velas_1m",)
    calentamiento_d = 35
    fuentes = ("Sifat, Mohamad y Shariff (2019), Lead-lag relationship between Bitcoin and Ethereum",
               "Medición tic a tic del retraso entre criptomonedas (Investment Management and Financial Innovations)")
    filtros = ("regimen_btc", "volatilidad", "funding", "dias", "regimen_cripto")
    parametros = {
        "senal": Param("str", "impulso15", ("impulso15", "donchian4h_btc"), texto="impulso de BTC"),
        "u": Param("float", 0.01, (0.007, 0.01, 0.015), texto="|r15| mínimo de BTC (fracción)"),
        "N": Param("int", 20, (20,), texto="velas de 4 h del canal (donchian)"),
        "solo_mayor_beta": Param("bool", False, texto="solo la alt de mayor β"),
        "monedas": Param("lista", ["ETH", "SOL", "XRP"], ("BTC", "ETH", "SOL", "XRP"), texto="monedas"),
    }

    def senales_crudas(self, m, p, desde_ms, hasta_ms) -> Senales:
        if "BTC" not in m.monedas:
            return Senales.vacia()
        alts = [(i, c) for i, c in self.monedas_de(m, p) if c != "BTC"]
        if not alts:
            return Senales.vacia()
        if p["senal"] == "donchian4h_btc":
            return self._donchian(m, p, alts)
        bb = m.barras("BTC", 15)
        if len(bb) < 2:
            return Senales.vacia()
        t = np.asarray(bb.t)
        rb = ind.retornos_log(np.asarray(bb.c, float))
        with np.errstate(invalid="ignore"):
            imp = validas(bb) & (np.abs(rb) >= p["u"])
        cand = np.flatnonzero(imp)
        vr_c = ind.volumen_relativo_en(bb.v, N30, cand)
        with np.errstate(invalid="ignore"):
            imp[cand] = vr_c >= 3
        imp = flancos(imp)   # un impulso por episodio (la primera vela que lo cumple)
        cand = []
        for ci, c in alts:
            ba = m.barras(c, 15)
            ta = np.asarray(ba.t)
            pos = np.searchsorted(ta, t)
            dentro = pos < len(ta)
            igual = dentro & (ta[np.clip(pos, 0, len(ta) - 1)] == t)
            ra = np.full(len(t), np.nan)
            ra_full = ind.retornos_log(np.asarray(ba.c, float))
            ra[igual] = ra_full[pos[igual]]
            beta = _rolling_beta(rb, ra, N30)
            with np.errstate(divide="ignore", invalid="ignore"):
                rez = ra / (beta * rb)
                ok = imp & igual & np.isfinite(rez) & (beta > 0) & (rez < 0.5)
            for i in np.flatnonzero(ok):
                cand.append((int(t[i]) + 15 * 60_000, ci, c, int(np.sign(rb[i])), float(beta[i]), float(1 - rez[i])))
        if not cand:
            return Senales.vacia()
        if p["solo_mayor_beta"]:
            mejor = {}
            for x in cand:
                if x[0] not in mejor or x[4] > mejor[x[0]][4]:
                    mejor[x[0]] = x
            cand = list(mejor.values())
        ts = np.array([x[0] for x in cand], np.int64)
        out = []
        for ci, c in alts:
            sel = [k for k, x in enumerate(cand) if x[1] == ci]
            if not sel:
                continue
            tt = ts[sel]
            sl = 2 * atr_pct_en(m, c, 60, tt)
            ok = np.isfinite(sl)
            out.append(emitir(tt[ok], ci, np.array([cand[k][3] for k in sel])[ok], stop_pct=sl[ok],
                              clave=np.array([cand[k][3] for k in sel])[ok],
                              prioridad=np.array([cand[k][4] * cand[k][5] for k in sel])[ok]))
        return Senales.juntar(out)

    def _donchian(self, m, p, alts) -> Senales:
        N = p["N"]
        bb = m.barras("BTC", 240)
        if len(bb) < 2:
            return Senales.vacia()
        h, l, cl = np.asarray(bb.h, float), np.asarray(bb.l, float), np.asarray(bb.c, float)
        hi, lo = ind.max_previo(h, N), ind.min_previo(l, N)
        ok = validas(bb) & np.isfinite(hi)
        with np.errstate(invalid="ignore"):
            up, dn = flancos_dobles(ok & (cl > hi), ok & (cl < lo))
        idx = np.flatnonzero(up | dn)
        tc = np.asarray(bb.t_cierre)
        out = []
        for ci, c in alts:
            ba = m.barras(c, 240)
            if len(ba) < 2:
                continue
            ha, la, ca = np.asarray(ba.h, float), np.asarray(ba.l, float), np.asarray(ba.c, float)
            hia, loa = ind.max_previo(ha, N), ind.min_previo(la, N)
            tca = np.asarray(ba.t_cierre)
            ts, ds = [], []
            for i in idx:
                k = int(np.searchsorted(tca, tc[i]))
                if k >= len(tca) or tca[k] != tc[i] or not np.isfinite(hia[k]):
                    continue
                d = 1 if up[i] else -1
                rota = ca[k] > hia[k] if d > 0 else ca[k] < loa[k]
                if not rota:
                    ts.append(int(tc[i]))
                    ds.append(d)
            if ts:
                tt = np.asarray(ts, np.int64)
                sl = 2 * atr_pct_en(m, c, 240, tt)
                okk = np.isfinite(sl)
                out.append(emitir(tt[okk], ci, np.asarray(ds)[okk], stop_pct=sl[okk], clave=np.asarray(ds)[okk]))
        return Senales.juntar(out)
