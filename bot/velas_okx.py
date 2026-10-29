"""Velas públicas de OKX EU para la gráfica de cada operación (GET /api/velas?fuente=okx, por defecto).

Son las del par que se operaría (BTC-USDC, ETH-USDC, SOL-USDC y XRP-USDC al contado) y salen de las rutas PÚBLICAS de
OKX EU, sin claves ni firma: GET https://eea.okx.com/api/v5/market/candles (las 1.440 últimas de cada tamaño, hasta 300
por petición) y …/history-candles (las anteriores, 100 por petición). Comprobado el 29-10-2026 con un GET público:
devuelven [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm] como texto, de la más nueva a la más vieja; confirm = "1"
si la vela ya está cerrada.

- Caché en memoria por moneda y tamaño de vela: las velas cerradas se guardan (hasta MAX_CACHE por serie) con el tramo que
  ya está completo; la vela en curso se vuelve a pedir como mucho cada VIVA_S segundos. Una gráfica que se refresca solo
  pide la página más nueva.
- Límite de peticiones propio: como mucho MAX_PETICIONES cada VENTANA_S (OKX admite 40 / 2 s en candles y 20 / 2 s en
  history-candles, por IP) y MAX_PAGINAS por respuesta.
- Si OKX falla (red, código distinto de "0", respuesta rara), lanza ErrorVelas: el servidor cae a las velas del bot y lo
  dice en el campo «fuente» (y «aviso»).
Nunca manda cabeceras de autenticación ni lee claves: la sesión es la del bot (User-Agent y certificados del sistema)."""
import asyncio
import logging
import time
from collections import deque

from . import config
from .models import now_ms

log = logging.getLogger("velas_okx")

BASE = "https://eea.okx.com"
RECIENTES = "/api/v5/market/candles"
HISTORIA = "/api/v5/market/history-candles"
BAR = {"1m": "1m", "5m": "5m", "15m": "15m"}   # tamaños de GET /api/velas → «bar» de OKX
STEP = {"1m": 60_000, "5m": 300_000, "15m": 900_000}
LIM_RECIENTES, LIM_HISTORIA = 300, 100
EN_RECIENTES = 1440            # OKX da en «candles» las 1.440 últimas velas de cada tamaño
MAX_PETICIONES, VENTANA_S = 10, 2.0
MAX_PAGINAS = 25
VIVA_S = 5.0                   # la vela en curso se vuelve a pedir como mucho cada 5 s
MAX_CACHE = 20_000             # velas cerradas guardadas por moneda y tamaño
TIMEOUT_S = 8.0


class ErrorVelas(Exception):
    pass


def inst_id(coin: str) -> str:
    return f"{coin}-{config.QUOTE}"


def nota(coin: str) -> str:
    return f"velas de OKX EU, {inst_id(coin)}"


def _fila(x) -> tuple[list, bool] | None:
    """[t, o, h, l, c] y si está cerrada, de una fila de OKX; None si no vale (texto, ceros, NaN)."""
    try:
        t, o, h, lo, c = int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])
    except (TypeError, ValueError, IndexError):
        return None
    if not all(v > 0 and v < float("inf") for v in (o, h, lo, c)):
        return None
    return [t, o, max(h, o, c), min(lo, o, c), c], str(x[8] if len(x) > 8 else "1") == "1"


class VelasOKX:
    """Velas públicas de OKX EU con caché y límite de peticiones. get_json(url, params) -> dict es inyectable (pruebas:
    sin red); por defecto, un GET sin firmar con la sesión aiohttp del bot."""

    def __init__(self, session=None, get_json=None, clock=now_ms, mono=time.monotonic, dormir=asyncio.sleep,
                 base: str = BASE):
        self.session, self.base = session, base.rstrip("/")
        self._get = get_json or self._get_http
        self.clock, self._mono, self._dormir = clock, mono, dormir
        # (moneda, tf) → {velas: {t: fila cerrada}, lo, hi (tramo [lo, hi) ya completo), viva (la vela en curso),
        # pedida (hora monótona de la última petición)}
        self._series: dict[tuple[str, str], dict] = {}
        self._envios: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.peticiones = 0
        self.ultimo_error: str | None = None

    async def _get_http(self, url: str, params: dict) -> dict:
        if self.session is None:
            raise ErrorVelas("sin sesión HTTP")
        import aiohttp
        async with self.session.get(url, params=params, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as r:
            if r.status != 200:
                raise ErrorVelas(f"HTTP {r.status}")
            return await r.json(content_type=None)

    async def _turno(self) -> None:
        """Como mucho MAX_PETICIONES cada VENTANA_S (las públicas de OKX cuentan por IP)."""
        while True:
            t = self._mono()
            while self._envios and t - self._envios[0] >= VENTANA_S:
                self._envios.popleft()
            if len(self._envios) < MAX_PETICIONES:
                self._envios.append(t)
                return
            await self._dormir(max(0.05, VENTANA_S - (t - self._envios[0])))

    async def _pagina(self, coin: str, tf: str, antes_de: int, historia: bool) -> list:
        """Una página de velas que abren antes de «antes_de» (after de OKX), de la más nueva a la más vieja."""
        await self._turno()
        ruta, lim = (HISTORIA, LIM_HISTORIA) if historia else (RECIENTES, LIM_RECIENTES)
        params = {"instId": inst_id(coin), "bar": BAR[tf], "after": str(int(antes_de)), "limit": str(lim)}
        self.peticiones += 1
        try:
            d = await self._get(self.base + ruta, params)
        except ErrorVelas:
            raise
        except Exception as e:
            raise ErrorVelas(f"{type(e).__name__}: {e}"[:200]) from e
        if not isinstance(d, dict) or str(d.get("code")) != "0" or not isinstance(d.get("data"), list):
            msg = d.get("msg") if isinstance(d, dict) else None
            raise ErrorVelas(f"OKX respondió {d.get('code') if isinstance(d, dict) else '?'}"
                             + (f": {str(msg)[:120]}" if msg else ""))
        return d["data"]

    async def _traer(self, coin: str, tf: str, start: int, end: int, s: dict) -> tuple[int, int]:
        """Pide hacia atrás desde end hasta start (como mucho MAX_PAGINAS páginas), guarda las velas cerradas y la que
        está en curso aparte. Devuelve el tramo [desde, hasta) que queda completo: hasta la primera vela sin cerrar y
        hasta donde se llegó (o desde start si OKX no tiene nada más antiguo)."""
        step, now = STEP[tf], self.clock()
        antes, paginas, forzar, sin_cerrar = end, 0, False, None
        while antes > start and paginas < MAX_PAGINAS:
            historia = forzar or antes <= now - (EN_RECIENTES - 1) * step   # «candles» solo da las 1.440 últimas
            filas = await self._pagina(coin, tf, antes, historia)
            paginas += 1
            ts = []
            for x in filas:
                f = _fila(x)
                if f is None:
                    continue
                v, cerrada = f
                ts.append(v[0])
                if cerrada:
                    s["velas"][v[0]] = v
                else:
                    sin_cerrar = v[0] if sin_cerrar is None else min(sin_cerrar, v[0])
                    if v[0] >= (s.get("viva") or [0])[0]:
                        s["viva"] = v
            if not ts:
                if not historia:   # la página de recientes ya no llega: lo anterior está en la historia
                    forzar = True
                    continue
                antes = start      # OKX no tiene nada más antiguo: el tramo está completo
                break
            antes = min(ts)
        s["pedida"] = self._mono()
        hasta = min(end, now // step * step, *([sin_cerrar] if sin_cerrar is not None else []))
        if len(s["velas"]) > MAX_CACHE:   # se quedan las más nuevas
            for t in sorted(s["velas"])[:len(s["velas"]) - MAX_CACHE]:
                del s["velas"][t]
        return max(start, antes), max(hasta, max(start, antes))

    async def velas(self, coin: str, a: int, b: int, tf: str) -> list[list]:
        """Velas [t, o, h, l, c] de tf que abren en [a, b), ordenadas (la última puede estar en curso). Solo pide a OKX
        lo que falta: un tramo nuevo entero y, de uno ya visto, lo más nuevo (la vela en curso, como mucho cada VIVA_S).
        Lanza ErrorVelas si OKX no responde."""
        if coin not in config.COINS or tf not in STEP:
            raise ErrorVelas("moneda o tamaño de vela no válidos")
        step = STEP[tf]
        a = a // step * step
        async with self._lock:
            s = self._series.setdefault((coin, tf), {"velas": {}, "lo": None, "hi": None, "viva": None, "pedida": 0.0})
            now = self.clock()
            try:
                if s["lo"] is None or a < s["lo"] or a > s["hi"]:   # tramo nuevo, más antiguo o separado: entero
                    s["lo"], s["hi"] = await self._traer(coin, tf, a, b, s)
                elif b > s["hi"] and (min(b, now // step * step) > s["hi"]
                                      or self._mono() - s["pedida"] >= VIVA_S):   # solo lo nuevo
                    lo2, hi2 = await self._traer(coin, tf, s["hi"], b, s)
                    if lo2 > s["hi"]:   # no se llegó a juntar con lo que había: vale desde lo nuevo
                        s["lo"] = lo2
                    s["hi"] = hi2
                self.ultimo_error = None
            except ErrorVelas as e:
                self.ultimo_error = str(e)
                raise
        out = [v for t, v in sorted(s["velas"].items()) if a <= t < b]
        viva = s.get("viva")
        if viva and a <= viva[0] < b and viva[0] not in s["velas"] and (not out or viva[0] > out[-1][0]):
            out.append(list(viva))
        return out

    async def respuesta(self, coin: str, desde: int, hasta: int, tf: str, maximo: int) -> dict | None:
        """GET /api/velas con fuente okx: como server.velas_respuesta (como mucho «maximo» velas, las más recientes) con
        fuente, instId y nota. None si OKX no tiene ninguna vela en el tramo; ErrorVelas si no responde.
        hasta_datos = hasta dónde llegan las velas, como con las del bot: el fin de la última si está cerrada; si está en
        curso, ahora (no el fin de esa vela, que aún no ha llegado: la web mete los precios en vivo desde aquí)."""
        step = STEP[tf]
        a0 = desde // step * step
        a = max(a0, (hasta - 1) // step * step - (maximo - 1) * step)
        rows = (await self.velas(coin, a, hasta, tf))[-maximo:]
        if not rows:
            return None
        return {"coin": coin, "tf": tf, "desde": rows[0][0], "hasta": hasta,
                "hasta_datos": max(rows[-1][0], min(rows[-1][0] + step, hasta, self.clock())), "recortada": a > a0,
                "n": len(rows), "velas": rows, "fuente": "okx", "mercado": "OKX EU", "instId": inst_id(coin),
                "nota": nota(coin)}

    def estado(self) -> dict:
        return {"peticiones": self.peticiones, "series": len(self._series), "ultimo_error": self.ultimo_error}
