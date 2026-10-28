"""Vigilancia del reloj (hallazgo n.º 53): al arrancar y cada hora compara la hora de este PC con la de OKX (y, si OKX no
responde, con la de Binance). Las aperturas, la medida de los primeros minutos y las ventanas dependen de la hora: si el
desfase pasa de 2 s lo dice el registro y la web (mensaje «reloj»). Solo mira: nunca cambia la hora del sistema."""
import asyncio
import logging
import time

import aiohttp

from .bus import Bus
from .models import now_ms

log = logging.getLogger("reloj")
FUENTES = (("OKX", "https://www.okx.com/api/v5/public/time", lambda d: int(d["data"][0]["ts"])),
           ("Binance", "https://data-api.binance.vision/api/v3/time", lambda d: int(d["serverTime"])))
MAX_MS = 2000                # desfase a partir del cual se avisa
CADA_S = 3600
REINTENTO_S = 300            # sin respuesta de nadie, se vuelve a probar a los 5 min
INTENTOS = 3                 # por fuente: vale la medida de menor ida y vuelta (la menos asimétrica)
MAX_IDA_VUELTA_MS = 1000     # con más, la medida no sirve (DNS, conexión o bucle ocupado la descuadran) y no se avisa
TIMEOUT = aiohttp.ClientTimeout(total=10)


def desfase_ms(servidor: int, antes: float, despues: float) -> int:
    """Lo que va adelantado este PC (ms; negativo = atrasado): la hora local a mitad de la petición menos la del
    servidor. antes y después en ms locales."""
    return round((antes + despues) / 2 - servidor)


def aviso(desfase: int | None) -> str | None:
    if desfase is None or abs(desfase) <= MAX_MS:
        return None
    sentido = "adelantado" if desfase > 0 else "atrasado"
    return (f"El reloj de este PC va {abs(desfase) / 1000:.1f} s {sentido} respecto a la hora de los exchanges".replace(".", ",")
            + ": las aperturas y la medida de sus primeros minutos dependen de la hora. Sincroniza la hora de Windows "
              "(Configuración › Hora e idioma › Sincronizar ahora).")


class Reloj:
    def __init__(self, bus: Bus, session: aiohttp.ClientSession, clock=time.time, sleep=asyncio.sleep):
        self.bus, self.session, self.clock, self.sleep = bus, session, clock, sleep
        self.ultimo: dict | None = None

    async def _pedir(self, url: str):
        async with self.session.get(url, timeout=TIMEOUT) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def medir(self) -> dict:
        """Mensaje «reloj» con el desfase frente a la primera fuente que responda (o el error de todas). De cada fuente,
        INTENTOS peticiones y la de menor ida y vuelta: lo que tarda la conexión antes de que el servidor ponga su hora,
        o el bucle ocupado después, entra de un solo lado. Si aun así pasa de MAX_IDA_VUELTA_MS, no se avisa."""
        errores = []
        for nombre, url, leer in FUENTES:
            mejor = None
            for _ in range(INTENTOS):
                try:
                    antes = self.clock() * 1000
                    d = await self._pedir(url)
                    despues = self.clock() * 1000
                    m = (despues - antes, desfase_ms(leer(d), antes, despues))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    errores.append(f"{nombre}: {(str(e) or type(e).__name__)[:80]}")
                    break
                mejor = m if mejor is None or m[0] < mejor[0] else mejor
            if mejor is None:
                continue
            iv, des = round(mejor[0]), mejor[1]
            if iv > MAX_IDA_VUELTA_MS:
                return {"type": "reloj", "ts": now_ms(), "fuente": nombre, "desfase_ms": des, "ida_vuelta_ms": iv,
                        "ok": None, "aviso": None, "error": f"Ida y vuelta de {iv} ms: la medida no es fiable"}
            return {"type": "reloj", "ts": now_ms(), "fuente": nombre, "desfase_ms": des, "ida_vuelta_ms": iv,
                    "ok": abs(des) <= MAX_MS, "aviso": aviso(des), "error": None}
        return {"type": "reloj", "ts": now_ms(), "fuente": None, "desfase_ms": None, "ida_vuelta_ms": None, "ok": None,
                "aviso": None, "error": "; ".join(errores) or "sin fuentes"}

    async def run(self) -> None:
        while True:
            msg = await self.medir()
            self.ultimo = msg
            self.bus.publish(msg)
            if msg["aviso"]:
                log.warning("%s (medido con %s)", msg["aviso"], msg["fuente"])
            elif msg["error"]:
                log.info("Reloj: no se pudo comparar la hora (%s)", msg["error"])
            await self.sleep(CADA_S if msg["fuente"] else REINTENTO_S)
