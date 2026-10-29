# Modo real con OKX EU (preparado y APAGADO)

El bot sigue siendo **paper** mientras el usuario no meta sus claves y active el modo real él mismo. Este documento tiene cuatro partes:

1. **[API de OKX EU](#api-de-okx-eu)**: lo que hace falta saber de la API para clientes del EEE (OKX EU).
2. **[Modo real: cómo activarlo y cómo pararlo](#modo-real-cómo-activarlo-y-cómo-pararlo)**: para el usuario, paso a paso.
3. **[Qué hace el bot con dinero real](#qué-hace-el-bot-con-dinero-real)**: órdenes, límites duros y lo que no hará nunca.
4. **[Contrato de cableado](#contrato-de-cableado-para-el-coordinador)**: cómo enganchar `bot/okx_real.py` al motor, al servidor y a la web.

Código: `bot/okx_real.py` (modos, cliente REST, `RealBroker`) y `bot/okx_firma.py` (firma y guarda de secretos). Pruebas: `tests/test_okx_real.py` (178, con un OKX simulado: ninguna usa la red ni claves de verdad). Plantilla de claves: `M:\BotFundamental-datos\secretos\okx.env.ejemplo`.

**Honestidad con los datos:** el modo real no cambia lo que dicen los backtests. «6 diarias» pierde en el backtest con el propio motor (−0,12 R por operación fuera de muestra) y ninguna estrategia ha mostrado ventaja robusta (`docs/05`). Con dinero real se pierde dinero real. Por eso los límites de abajo son duros y el modo real arranca con 10 USDC por operación como mucho.

Comprobado el **29-10-2026** con la documentación oficial y con peticiones **públicas sin firmar** (GET de solo lectura). No se ha enviado ninguna petición firmada ni ninguna orden, ni de demo ni real. Lo que no se ha podido confirmar así va marcado como **sin confirmar**; casi todo se resuelve con las comprobaciones de solo lectura del apartado 9, que el bot hará con las claves del usuario la primera vez.

Fuentes principales:
- Documentación de la API para el EEE: <https://my.okx.com/docs-v5/en/> (la de las secciones citadas abajo).
- Documentación general: <https://www.okx.com/docs-v5/en/>.
- SDK oficial en Python (solo como referencia del algoritmo de firma; el bot no lo usa): <https://github.com/okxapi/python-okx/blob/master/okx/utils.py>.

---

## API de OKX EU

### 1. Dominios y rutas base

| Uso | Real | Demo (`OKX_DEMO=1`) |
|---|---|---|
| REST | `https://eea.okx.com` | `https://eea.okx.com` + cabecera `x-simulated-trading: 1` |
| WebSocket público | `wss://wseea.okx.com/ws/v5/public` | `wss://wseeapap.okx.com/ws/v5/public` |
| WebSocket privado (cuenta, posiciones, órdenes) | `wss://wseea.okx.com/ws/v5/private` | `wss://wseeapap.okx.com/ws/v5/private` |
| WebSocket «business» (órdenes algo, velas) | `wss://wseea.okx.com/ws/v5/business` | `wss://wseeapap.okx.com/ws/v5/business` |

- **El dominio de la API del EEE es `eea.okx.com`.** La documentación general dice: «EU users (registered on my.okx.com): Use `eea.okx.com` as your API domain» y «Using `openapi.okx.com` will not work for the regions below». `my.okx.com` es la web (registro, página de claves `/account/my-api` y la documentación del EEE), no el dominio de la API. `www.okx.com` y `openapi.okx.com` son los de la cuenta global. Fuente: [Overview](https://www.okx.com/docs-v5/en/#overview), [Production Trading Services (EEE)](https://my.okx.com/docs-v5/en/#overview-production-trading-services).
- Los tres dominios contestan a las rutas **públicas** (comprobado: `GET /api/v5/public/instruments` da `200` en `eea.okx.com`, `www.okx.com` y `openapi.okx.com`), pero los datos no son iguales: `eea.okx.com` devuelve los límites de posición del EEE (p. ej. `posLmtAmt` 250.000 en los X-Perps frente a 50.000.000 en `www.okx.com`). El modo real usa `eea.okx.com` también para lo público.
- **El modo demo existe para el EEE.** La documentación del EEE trae sus propias direcciones de demo (tabla de arriba) y dice: «`x-simulated-trading: 1` needs to be added to the header of the Demo Trading request». En demo no funcionan, entre otras, «withdraw, deposit, purchase/redemption». Las claves de demo se crean aparte (Trade → Demo Trading → Personal Center → Demo Trading API) y no valen en real: mezclar entorno y clave da el error `50101` («APIKey does not match current environment»). Fuente: [Demo Trading Services (EEE)](https://my.okx.com/docs-v5/en/#overview-demo-trading-services). **Sin confirmar:** que los X-Perps estén en demo para una cuenta del EEE.
- En real el bot no manda la cabecera `x-simulated-trading` (el SDK oficial manda `0`; omitirla es lo mismo según la documentación, que solo la pide para demo).
- **Puerto 8443:** la documentación avisa de que el 8443 de WebSocket se retira («WebSocket port 8443 will be retired soon. Please migrate to port 443», aviso `64010`). `bot/market.py` y `bot/trend.py` usan hoy `wss://wseea.okx.com:8443/ws/v5/public`; conviene pasarlos a `wss://wseea.okx.com/ws/v5/public` (443). Fuente: [Notification](https://my.okx.com/docs-v5/en/#overview-websocket-notification). No lo cambio aquí porque esos archivos los edita otro flujo.

### 2. Firma de las peticiones privadas (REST)

Cabeceras obligatorias en toda petición privada ([REST Authentication](https://my.okx.com/docs-v5/en/#overview-rest-authentication-making-requests)):

| Cabecera | Valor |
|---|---|
| `OK-ACCESS-KEY` | la clave de API (texto) |
| `OK-ACCESS-SIGN` | la firma en Base64 (abajo) |
| `OK-ACCESS-TIMESTAMP` | hora UTC en ISO 8601 **con milisegundos**, p. ej. `2020-12-08T09:08:57.715Z` |
| `OK-ACCESS-PASSPHRASE` | la frase de paso que el usuario puso al crear la clave |
| `Content-Type` | `application/json` (los cuerpos van en JSON) |

Firma ([Signature](https://my.okx.com/docs-v5/en/#overview-rest-authentication-signature)):

```
prehash = timestamp + MÉTODO + rutaConConsulta + cuerpo
OK-ACCESS-SIGN = Base64( HMAC-SHA256( clave = SecretKey, mensaje = prehash ) )
```

- `timestamp`: exactamente el mismo texto que va en `OK-ACCESS-TIMESTAMP`.
- `MÉTODO`: en mayúsculas (`GET`, `POST`).
- `rutaConConsulta`: la ruta **con** la cadena de consulta en los GET («`GET` request parameters are counted as requestpath, not body»), p. ej. `/api/v5/account/balance?ccy=BTC`. Se firma la cadena exactamente como se envía (mismo orden de parámetros). El bot la monta como el SDK oficial, **sin codificar** (`?ccy=USDC,BTC`, no `%2C`), solo admite en ella letras, números y `_ . , : -`, y la envía tal cual (`yarl.URL(..., encoded=True)`): con `urlencode` la coma saldría firmada como `%2C` y enviada como `,`, y la firma no valdría (`50113`).
- `cuerpo`: el texto JSON exacto que se envía en los POST; vacío en los GET. Hay que serializar una sola vez y firmar y enviar los mismos bytes.
- El servidor rechaza una hora desfasada más de 30 s (error `50102`); la documentación recomienda sincronizar con `GET /api/v5/public/time` (público). Comprobado: responde `{"code":"0","data":[{"ts":"…"}]}`.

El ejemplo oficial es `sign=CryptoJS.enc.Base64.stringify(CryptoJS.HmacSHA256(timestamp + 'GET' + '/api/v5/account/balance?ccy=BTC', SecretKey))`. **La documentación no trae un vector de prueba reproducible:** sus firmas de ejemplo (la de la cabecera de demo y la del login de WebSocket) no salen de la SecretKey de ejemplo que publica (comprobado). Para `tests/test_okx_real.py` propongo estos vectores, calculados con `hmac` de Python y comprobados con `openssl dgst -sha256 -hmac … -binary | base64`, con un secreto **falso** que no es la clave de nadie:

| SecretKey (falsa) | Mensaje (prehash) | Firma esperada |
|---|---|---|
| `SECRETO_FALSO_SOLO_PARA_PRUEBAS` | `2020-12-08T09:08:57.715ZGET/api/v5/account/balance?ccy=BTC` | `hmUuaby5SkxPIOoaU720J2F14BPNwWnb+vHnznQPzwk=` |
| `SECRETO_FALSO_SOLO_PARA_PRUEBAS` | `2020-12-08T09:08:57.715ZPOST/api/v5/trade/order{"instId":"BTC-USDC","tdMode":"cash","side":"buy","ordType":"market","sz":"25","tgtCcy":"quote_ccy","tradeQuoteCcy":"USDC"}` | `qFbT5fQQnyqojR2aPUiUf1ESmLqp5nlRQOgU3K7AlH0=` |
| `SECRETO_FALSO_SOLO_PARA_PRUEBAS` | `1538054050GET/users/self/verify` (login de WebSocket) | `hPKFlqJ1WTfTZSeGBCr1Z925EvAzblK3avhRds+BIBc=` |

El SDK oficial hace lo mismo: `pre_hash = str(timestamp) + str.upper(method) + request_path + body` y `base64.b64encode(hmac(secret, prehash, sha256))`. Ojo: el SDK, con `debug=True`, escribe las cabeceras en el registro; el bot no debe registrar nunca cabeceras ni cuerpos firmados.

**Login del WebSocket privado** ([Login](https://my.okx.com/docs-v5/en/#overview-websocket-login)): `{"op":"login","args":[{"apiKey","passphrase","timestamp","sign"}]}` con `timestamp` en **segundos Unix** (no ISO) y `sign` = Base64(HMAC-SHA256(SecretKey, timestamp + `GET` + `/users/self/verify`)). Caduca a los 30 s. Mantener la conexión: si no llega nada en N < 30 s, mandar el texto `ping` y esperar `pong`.

**Cabecera `expTime`** (opcional, [Transaction Timeouts](https://my.okx.com/docs-v5/en/#overview-transaction-timeouts)): en `trade/order`, `batch-orders`, `amend-order` y `amend-batch-orders`, una hora límite en ms; si llega después, OKX la descarta. Útil para no ejecutar una orden vieja tras un corte de red.

### 3. Instrumentos

Comprobado con `GET https://eea.okx.com/api/v5/public/instruments?instType=SPOT`, `…?instType=FUTURES` y `…?instType=SWAP` (públicas, sin firmar) el 29-10-2026 hacia las 01:00 UTC. Precios de referencia de `GET /api/v5/market/ticker` en el mismo momento.

**Contado (largos).** Los cuatro existen como `BTC-USDC`, `ETH-USDC`, `SOL-USDC` y `XRP-USDC`, `state: live`, `groupId` 12 (grupo de comisión):

| instId | minSz | lotSz (paso de cantidad) | tickSz (paso de precio) | Mínimo aprox. | maxMktAmt (USDC por orden de mercado) |
|---|---|---|---|---|---|
| `BTC-USDC` | 0,00001 BTC | 0,00000001 | 0,1 | ≈ 0,81 USDC | 1.000.000 |
| `ETH-USDC` | 0,001 ETH | 0,000001 | 0,01 | ≈ 2,35 USDC | 180.000 |
| `SOL-USDC` | 0,01 SOL | 0,00001 | 0,01 | ≈ 1,02 USDC | 25.000 |
| `XRP-USDC` | 1 XRP | 0,001 | 0,0001 | ≈ 1,32 USDC | 280.000 |

- Son libros unificados: `tradeQuoteCcyList` = `["USDG","USD","USDC","RLUSD"]`. No existe `BTC-USD` como instId (comprobado: `51001`). Al ordenar hay que mandar `tradeQuoteCcy: "USDC"`; la documentación dice que para usuarios de ciertas regiones es obligatorio y que, si falta, puede dar `51000` ([Place order](https://my.okx.com/docs-v5/en/#order-book-trading-trade-post-place-order), nota `tradeQuoteCcy`). **Sin confirmar** si el EEE es una de esas regiones: se manda siempre.
- La lista que vale para la cuenta es la privada `GET /api/v5/account/instruments?instType=SPOT` («Retrieve available instruments info of current account»), que trae el `tradeQuoteCcyList` de esa cuenta.

**Cortos: X-Perps (el derivado del EEE).** OKX ofrece en el EEE los **X-Perps**: derivados con vencimiento a 5 años y funding cada 8 h, de OKX Europe Markets Ltd (licencia de la MFSA de Malta, MiFID II), con apalancamiento de hasta 10x para minoristas y un test de idoneidad obligatorio. Fuentes: [What are X-Perps (OKX)](https://www.okx.com/en-eu/learn/what-are-x-perps-guide-for-european-traders), [Selected margin (OKX)](https://www.okx.com/en-eu/help/whats-selected-margin-eea). En la API son `instType: FUTURES` con `ruleType: xperp` («perpetual-style futures», [Get instruments](https://my.okx.com/docs-v5/en/#public-data-rest-api-get-instruments)):

| instId (hoy) | instFamily | ctVal (valor de 1 contrato) | 1 contrato aprox. | lotSz = minSz | tickSz | lever máx. publicado | maxMktSz (contratos) |
|---|---|---|---|---|---|---|---|
| `BTC-USD_UM_XPERP-310404` | `BTC-USD_UM_XPERP` | 0,0001 BTC | ≈ 8,09 USD | 1 | 0,1 | 50 | 20.000 |
| `ETH-USD_UM_XPERP-310404` | `ETH-USD_UM_XPERP` | 0,001 ETH | ≈ 2,35 USD | 1 | 0,01 | 50 | 50.000 |
| `SOL-USD_UM_XPERP-310404` | `SOL-USD_UM_XPERP` | 0,01 SOL | ≈ 1,02 USD | 1 | 0,01 | 50 | 100.000 |
| `XRP-USD_UM_XPERP-310404` | `XRP-USD_UM_XPERP` | 1 XRP | ≈ 1,32 USD | 1 | 0,0001 | 20 | 50.000 |

Común a los cuatro: `ctType: linear`, `settleCcy: USD`, `uly` = `BTC-USD` (etc.), `alias: this_five_years`, `expTime` = 4-4-2031 08:00 UTC, `futureSettlement: false`, `groupId` 7, `posLmtAmt` 250.000 y `posLmtPct` 30 (en `eea.okx.com`).

- **El instId lleva la fecha de vencimiento** (`…-310404`). No se escribe a mano: el bot lo resuelve al arrancar (y cada vez que vuelve a comprobar la cuenta: al cambiar de modo o de claves) con `GET /api/v5/public/instruments?instType=FUTURES&instFamily=BTC-USD_UM_XPERP` (comprobado, devuelve uno), quedándose con `state: live` y `ruleType: xperp`. `alias` está marcado como obsoleto en la documentación («use expTime»), así que no se usa para elegir.
- `sz` va en **contratos enteros**: nominal = `sz × ctVal × precio`. Con el tamaño por riesgo del paper (0,5 % de 100 € con stop del 2 % → ≈ 25 € de nominal) salen 3 contratos de BTC (≈ 24 USD): siempre se redondea **hacia abajo** para no pasar del riesgo; si sale 0, no se opera.
- **Funding:** `GET /api/v5/public/funding-rate?instId=BTC-USD_UM_XPERP-310404` funciona en público («Applicable to SWAP and X-Perps FUTURES»). Comprobado: liquidaciones a las 00:00, 08:00 y 16:00 UTC; tasa actual 0,0001 (0,01 %) en las cuatro; tope ±0,375 %. También `GET /api/v5/public/mark-price?instType=FUTURES&instId=…` y `GET /api/v5/market/ticker?instId=…`.
- **Margen:** la ayuda de OKX dice que en X-Perps hay dos modos, **Selected Margin** (fondo común con los activos elegidos) e **Isolated** (un importe fijo de un activo por posición, que limita la pérdida a ese importe), y que los activos que se queden en la cuenta de fondos («Funding Account») **no cuentan como margen**: el usuario tiene que pasarlos él a la cuenta de trading. El bot no lo hará nunca (apartado 5). **Sin confirmar:** a qué `mgnMode` de la API corresponde Selected Margin (lo probable es `cross`), qué activo usa el margen aislado de un contrato liquidado en USD (¿`ccy: USDC`?) y si `GET /api/v5/public/position-tiers?instType=FUTURES&tdMode=isolated&instFamily=BTC-USD_UM_XPERP` (contesta igual con `isolated` y con `cross`: máx. 50x en el primer tramo) refleja lo que puede usar una cuenta minorista (10x según OKX).
- **Recomendación para el bot:** modo de posición `net_mode` (una posición por moneda), margen **aislado** y apalancamiento bajo fijado con `POST /api/v5/account/set-leverage` (`instId`, `lever`, `mgnMode: isolated`; en `net_mode` no lleva `posSide`). Con exposición ≤ 100 % el paper ya opera sin apalancamiento efectivo; 2x deja holgura de margen. En `net_mode`, `posSide` se omite o va `net`, y los cierres llevan `reduceOnly: true`. En `long_short_mode` habría que mandar `posSide: long|short` en todo (si falta, `51000`).
- **Perpetuos SWAP:** la lista pública del EEE trae los `…-USDT-SWAP` y los inversos `…-USD-SWAP`, y **ningún perpetuo con margen en USDC**. Que estén en la lista pública no quiere decir que una cuenta del EEE pueda operarlos (la lista pública es casi idéntica en `www.okx.com`). **Sin confirmar**, y lo más probable es que no: el producto de derivados del EEE son los X-Perps. Lo confirma `GET /api/v5/account/instruments?instType=SWAP` con las claves; una orden no permitida da `51155` («restricted due to local compliance requirements»).

### 4. Órdenes, salidas y consultas

Todas las rutas son de [Order Book Trading](https://my.okx.com/docs-v5/en/#order-book-trading) y [Trading Account](https://my.okx.com/docs-v5/en/#trading-account). Los límites son los de la documentación del EEE.

**Abrir**

| Qué | Ruta | Campos clave | Límite |
|---|---|---|---|
| Largo al contado, a mercado por importe | `POST /api/v5/trade/order` | `instId: BTC-USDC`, `tdMode: cash`, `side: buy`, `ordType: market`, `sz` = USDC a gastar, `tgtCcy: quote_ccy` (por defecto en compras), `tradeQuoteCcy: USDC`, `clOrdId` | 60 / 2 s por instrumento |
| Largo al contado, límite | igual | `ordType: limit`, `px` (múltiplo de `tickSz`), `sz` en BTC (múltiplo de `lotSz`) | igual |
| Corto en X-Perp | igual | `instId` del X-Perp, `tdMode: isolated` (o `cross`, sin confirmar), `side: sell`, `ordType: market` o `limit`, `sz` en contratos | igual |

- En las órdenes de mercado al contado: `banAmend: true` rechaza la orden si no hay saldo en vez de reducirla en silencio; `slippagePct` (0 a 0,05) limita el deslizamiento. OKX reserva un 5 % de más del saldo en las órdenes de mercado («reserves/freezes your quote currency by an additional 5%»): hay que dejar ese margen libre.
- `clOrdId` (hasta 32 caracteres alfanuméricos) es único entre las órdenes **pendientes** de la cuenta. Tras un error de red, antes de reintentar se consulta `GET /api/v5/trade/order?instId=…&clOrdId=…` para no duplicar la entrada.
- La respuesta trae `sCode`/`sMsg` por orden: `code: "0"` arriba no basta, hay que mirar `sCode` de cada una.

**Stop y objetivo en el propio exchange**

1. **Pegados a la entrada (`attachAlgoOrds`)** en `POST /api/v5/trade/order`: `slTriggerPx` + `slOrdPx` (`-1` = a mercado), `tpTriggerPx` + `tpOrdPx`, `slTriggerPxType`/`tpTriggerPxType` (`last`, `index`, `mark`; por defecto `last`). Se crean **solo cuando la entrada se llena**; si la entrada se cancela sin llenarse, no se crean. No se pueden pegar a una compra a mercado con `tgtCcy: base_ccy` (por eso se compra por importe). Las salidas parciales con varios objetivos («split TPs», con `sz` por objetivo) **no valen al contado** (`51077`) y exigen que la suma de objetivos sea toda la orden (`51083`).
2. **Órdenes algo aparte (`POST /api/v5/trade/order-algo`)**, 20 / 2 s por instrumento ([Place algo order](https://my.okx.com/docs-v5/en/#order-book-trading-algo-trading-post-place-algo-order)):
   - `ordType: conditional`: un solo stop o un solo objetivo. Ojo: «When placing net TP/SL order (ordType=conditional) and both take-profit and stop-loss parameters are sent, only stop-loss logic will be performed».
   - `ordType: oco`: stop y objetivo a la vez; el que salta cancela el otro. Es la horquilla «o gano 2 o pierdo 1» en el exchange.
   - `ordType: trigger`: orden (mercado con `orderPx: -1` o límite) al cruzar `triggerPx`.
   - `ordType: move_order_stop` (trailing): `callbackRatio` (fracción) **o** `callbackSpread` (distancia en precio) y `activePx` opcional (empieza a seguir al llegar a ese precio). Disparo del corto/venta = máximo desde que se activa − distancia.
   - En derivados: `reduceOnly: true`; `closeFraction: "1"` cierra toda la posición (solo `conditional`/`oco`, a mercado, y como mucho una por posición: `51088`). `cxlOnClosePos: true` (con `reduceOnly`) cancela la algo cuando la posición se cierra del todo.
   - `triggerPxType`: al contado solo `last`; en derivados la documentación recomienda `mark`. Para que el real se parezca al paper (que mira el último precio), `last` en los dos.
   - Las algo **no congelan saldo** al colocarse. Una orden **límite** de venta al contado sí congela las monedas, y entonces un stop por la cantidad total no encuentra saldo: al contado cada algo cubre su parte.
3. **Modificar:** `POST /api/v5/trade/amend-algos` (`newSz`, `newSlTriggerPx`, `newSlOrdPx`, `newTpTriggerPx`, `newTpOrdPx`, `cxlOnFail`), 20 / 2 s; **no** vale para trailing («not including Move_order_stop»): un trailing se cancela y se vuelve a poner. Una orden normal se modifica con `POST /api/v5/trade/amend-order` (como mucho 3 modificaciones en curso por orden, `51513`).

**La salida A del paper en el exchange (propuesta inicial; el código la hace con `amend-algos` en vez de un trailing de OKX y cierra con una orden `reduceOnly` en vez de `close-position`, que no está en su lista blanca: ver «Órdenes en OKX», más abajo).** El preset «6 diarias v2» (`apertura.SEIS2`): stop 2 %, la mitad sale en +3R, y la otra mitad, al llegar a +3R, sube su stop al suelo de +2R y lo sigue a 2R por detrás del mejor precio (nunca baja); 24 h sin activar, 7 días activado. Antes de +3R nada mueve el stop. Con la API:

1. Entrada (mercado). Al llenarse, con la cantidad **llenada** (`accFillSz`), dos algo por mitades:
   - **A** (mitad 1): `oco` con stop en −1R y objetivo en +3R.
   - **B** (mitad 2): `conditional` con stop en −1R.
   En X-Perps, las dos con `reduceOnly: true`. Al contado, cada una por su mitad exacta (redondeada a `lotSz`; la suma no puede pasar de lo comprado).
2. Cuando **A** cierra por objetivo (+3R = el nivel del cerrojo), el bot:
   - sube el stop de **B** al suelo (+2R) con `amend-algos` (`newSlTriggerPx`);
   - pone un **trailing** `move_order_stop` por la mitad 2 con `callbackSpread` = 2R en precio (`entrada × 2 % × 2`) y `activePx` = +4R. Así el stop efectivo es el mayor de los dos: el suelo de +2R y, desde +4R, el máximo − 2R, que es lo mismo que `paper.lock_update`. `callbackRatio` no vale: el paper sigue a una distancia fija en precio, no en porcentaje.
3. Cuando salta uno de los dos (B o el trailing), el bot cancela el otro **en el acto** (`POST /api/v5/trade/cancel-algos`). En X-Perps el sobrante es `reduceOnly` y no puede abrir nada; al contado podría vender monedas del usuario o fallar por falta de saldo. Por eso la cuenta debe tener solo lo del bot en esas monedas.
4. **Cierre por tiempo** (24 h o 7 días): no existe en el exchange. Lo hace el bot: cancela las algo de esa posición y cierra a mercado (`POST /api/v5/trade/close-position` en X-Perps con `mgnMode` y `autoCxl: true`; venta a mercado al contado). Si el bot está caído, el stop y el objetivo siguen protegiendo la posición en OKX, pero el cierre por tiempo se retrasa hasta que vuelva.

**Sin confirmar** (probar en demo antes de usar dinero): que `cancel-algos` cancele un `move_order_stop` (la documentación lo admite en sus parámetros, pero el comentario de su ejemplo dice «not including … Trailing Stop order»; la antigua `cancel-advance-algos` ya no aparece); qué pasa con un stop al contado cuya cantidad supera el saldo disponible; y si `attachAlgoOrds` admite un trailing (la tabla trae `callbackRatio`/`callbackSpread`/`activePx` «only applicable when ordType = move_order_stop», pero no dice dónde va ese `ordType`).

**Salida parcial y cierre**
- Al contado: venta (`side: sell`) por la cantidad de la parte, a mercado o límite.
- En X-Perps: orden contraria con `reduceOnly: true` y `sz` de la parte (en `net_mode`). Si `sz` pasa de la posición, la orden entera se rechaza («the system does not auto-trim»). Cierre total: `POST /api/v5/trade/close-position` (`instId`, `mgnMode`, `posSide` solo en `long_short_mode`, `autoCxl`), 20 / 2 s.

**Cancelar**
- `POST /api/v5/trade/cancel-order` (`instId` + `ordId` o `clOrdId`), 60 / 2 s.
- `POST /api/v5/trade/cancel-algos` (lista de hasta 10 `{instId, algoId|algoClOrdId}`), 20 órdenes / 2 s.
- `POST /api/v5/trade/cancel-all-after` (temporizador de 10 a 120 s que cancela las órdenes pendientes si el bot deja de latir). **No se usa:** cancelaría también el objetivo límite y la documentación dice que no es para la estrategia. Lo que tiene que quedar en el exchange si el bot cae es justo el stop.

**Consultar**

| Qué | Ruta | Límite |
|---|---|---|
| Una orden | `GET /api/v5/trade/order?instId=…&ordId=…` (o `clOrdId`) | 60 / 2 s |
| Órdenes pendientes | `GET /api/v5/trade/orders-pending` | 60 / 2 s |
| Historial 7 días | `GET /api/v5/trade/orders-history?instType=…` | 40 / 2 s |
| Ejecuciones 3 días | `GET /api/v5/trade/fills` | 60 / 2 s |
| Una algo | `GET /api/v5/trade/order-algo?algoId=…` | 20 / 2 s |
| Algo pendientes | `GET /api/v5/trade/orders-algo-pending?ordType=oco` (`ordType` obligatorio) | 20 / 2 s |
| Historial de algo | `GET /api/v5/trade/orders-algo-history?ordType=…&state=…` | 20 / 2 s |
| Posiciones | `GET /api/v5/account/positions?instType=FUTURES` | 10 / 2 s |
| Saldo | `GET /api/v5/account/balance?ccy=USDC,BTC,ETH,SOL,XRP` (`totalEq` en USD; por moneda `eq`, `cashBal`, `availBal`, `frozenBal`, `eqUsd`) | 10 / 2 s |
| Comisión de la cuenta | `GET /api/v5/account/trade-fee?instType=SPOT&instId=BTC-USDC` y `?instType=FUTURES&instFamily=BTC-USD_UM_XPERP` | 5 / 2 s |
| Configuración | `GET /api/v5/account/config` (`acctLv`, `posMode`, `perm`, `ip`, `kycLv`) | 5 / 2 s |
| Instrumentos de la cuenta | `GET /api/v5/account/instruments?instType=SPOT` (y `FUTURES`) | 20 / 2 s |
| Tamaño máximo | `GET /api/v5/account/max-size?instId=…&tdMode=…` | 20 / 2 s |
| Apalancamiento | `GET /api/v5/account/leverage-info?instId=…&mgnMode=isolated` | 20 / 2 s |
| Límite de órdenes de la cuenta | `GET /api/v5/trade/account-rate-limit` | 1 / s |

- **Comisiones:** en `trade-fee` un número **negativo es comisión** y uno positivo, reembolso. Al contado, los pares en USDC usan `takerUSDC`/`makerUSDC` o el `feeGroup` con `groupId` 12; los X-Perps, `groupId` 7 (ese número no sale en la lista de grupos de la documentación, que solo trae el 8 y el 10 de XPERP). Según la ayuda de OKX, en el EEE el contado del nivel normal cuesta **0,20 % creador / 0,35 % tomador** sin cuenta de X-Perps y **0,08 % / 0,10 %** con ella ([comparativa](https://www.okx.com/en-eu/help/eea-xperp-spot-trading-fees-comparison), [tarifas desde el 22-4-2026](https://www.okx.com/en-eu/help/notice-updates-to-okx-eea-trading-fees)); los X-Perps, 0,02 % / 0,05 % ([costes de X-Perps](https://www.okx.com/en-eu/help/okx-x-perps-eea-fees-overview)). El paper supone 0,10 % por lado (`config.FEE_PCT`): solo es verdad con la cuenta de X-Perps abierta. El modo real tiene que leer la comisión real al arrancar y usarla en la regla de «≥ 3 % neto de costes»; si es mayor que la que supone el plan, la operación no pasa.
- **Estados:** orden `live`, `partially_filled`, `filled`, `canceled` (y `mmp_canceled`, que no aplica); `orders-pending` solo devuelve las dos primeras. Algo: `live`, `pause`, `partially_effective`, `effective` (disparada), `canceled`, `order_failed`.
- **WebSocket privado** (para no sondear): canales `orders`, `positions`, `account` y `balance_and_position` en `/ws/v5/private`; `orders-algo` y `algo-advance` (trailing) en `/ws/v5/business`. No mandan foto inicial de órdenes: al conectar (y al reconectar) hay que leer el estado por REST.

**Límites de peticiones** ([Rate Limits](https://my.okx.com/docs-v5/en/#overview-rate-limits))
- Al pasarse: `50011` («Rate limit reached»). Lo privado cuenta por usuario; lo público sin firmar, por IP.
- Órdenes, modificaciones y cancelaciones: límites independientes, por instrumento, compartidos entre REST y WebSocket. Además, 1.000 órdenes nuevas o modificaciones cada 2 s por cuenta (`50061`).
- WebSocket: 3 conexiones nuevas por segundo por IP; 480 `subscribe`/`unsubscribe`/`login` por hora y conexión; 30 conexiones por canal privado y cuenta; se corta si pasan 30 s sin datos.
- El bot, con ≤ 6 entradas al día y 4 posiciones, está muy lejos de todos. Los GET se reintentan con espera ante `50011`, `50013` y `50001`; las órdenes (POST) solo ante `50011`/`50061`, porque `50013` y `50001` no garantizan que la orden no se procesara (se comprueba por `clOrdId`).

**Errores que el bot tiene que tratar aparte:** `50102` (hora desfasada: resincronizar con `/public/time`), `50110` (IP fuera de la lista de la clave), `50111`/`50113`/`50105` (clave, firma o frase de paso incorrectas: parar, sin reintentar), `50101` (clave de demo en real o al revés), `50120` (la clave no tiene permiso), `51155` (instrumento restringido en la región), `51020`/`51121`/`51007` (por debajo del mínimo o fuera del paso de cantidad), `51278`/`51280` (stop del lado equivocado del último precio: «SL trigger price cannot be lower/higher than the last price»), `51277`/`51279` y `51046`–`51049` (objetivo del lado equivocado; sin confirmar), `51400`/`51503`/`51603` (la orden ya no existe o ya se llenó), `59000` (no se puede cambiar el modo con posiciones u órdenes abiertas).

### 5. Lo que el bot NO tendrá (para el revisor)

El bot no podrá nunca retirar fondos ni moverlos entre cuentas. En `bot/okx_real.py` (y `bot/okx_firma.py` si existe) no puede aparecer **ninguna** de estas rutas, ni montada por partes:

- Retiradas: `/api/v5/asset/withdrawal`, `/api/v5/asset/cancel-withdrawal`, `/api/v5/fiat/create-withdrawal`, `/api/v5/fiat/cancel-withdrawal`.
- Transferencias: `/api/v5/asset/transfer` (entre la cuenta de fondos y la de trading, **y la permite el permiso «Trade»**: «Trade: Can place and cancel orders, funding transfer, make settings») y `/api/v5/asset/subaccount/transfer`.
- Subcuentas: todo `/api/v5/users/subaccount/…` (crear subcuentas, crear o cambiar sus claves, `set-transfer-out`), `/api/v5/account/subaccount/…` y `/api/v5/asset/subaccount/…`.
- Otros movimientos de dinero: `/api/v5/asset/convert/…`, `/api/v5/fiat/buy-sell/…`, `/api/v5/finance/…` (ahorro y staking), `/api/v5/account/spot-manual-borrow-repay`, `/api/v5/account/set-auto-repay`, `/api/v5/trade/one-click-repay-v2`.
- Tampoco, aunque no saquen dinero, porque el bot no las necesita: `/api/v5/account/position/margin-balance` (añadir o quitar margen de una posición aislada), `/api/v5/asset/deposit-address` y `/api/v5/account/demo-adjust-balance`.

Cómo se comprueba:
- `bot/okx_real.py` tiene una **lista blanca** de `(método, ruta)` (las de los apartados 3 y 4, más `GET /api/v5/public/time`) y rechaza cualquier otra antes de firmar. La prueba de `tests/test_okx_real.py` recorre la lista y falla si alguna empieza por `/api/v5/asset/`, `/api/v5/users/`, `/api/v5/fiat/`, `/api/v5/finance/` o `/api/v5/account/subaccount`, o contiene `withdraw`, `transfer` o `borrow`.
- Búsqueda para el revisor: `git grep -nE "asset/|users/subaccount|fiat/|finance/|subaccount|withdraw|transfer|borrow-repay|one-click-repay|margin-balance|demo-adjust" -- bot/okx_real.py bot/okx_firma.py`. Solo puede salir la lista de prefijos prohibidos del filtro o texto de comentarios; nunca una ruta que se llame.
- Al arrancar, el bot lee `GET /api/v5/account/config` y **se niega a operar** si `perm` contiene `withdraw`, y avisa si `ip` está vacío. Instrucciones para el usuario al crear la clave en `my.okx.com` → API: permisos **Read + Trade**, nunca **Withdraw**, y la clave atada a la IP de su PC. Las claves con permiso de trading sin IP caducan tras 14 días sin uso ([API key security](https://my.okx.com/docs-v5/en/#overview-api-key-creation-api-key-security)).
- Como «Trade» permite técnicamente `asset/transfer`, la protección es el código (lista blanca y prueba), no el permiso de la clave. Pasar dinero de la cuenta de fondos a la de trading (necesario para el margen de los X-Perps) lo hace el usuario a mano en la web.

### 6. Modos de cuenta y primer uso

- La cuenta tiene un modo (`acctLv`): 1 contado, 2 futuros, 3 margen multidivisa, 4 margen de cartera. «You need to set on the Web/App for the first set of every account mode»: el bot **no** cambia el modo de cuenta. Lo lee y, si no le sirve, para y lo explica. **Sin confirmar:** qué modo deja operar X-Perps a una cuenta del EEE.
- El modo de posición (`posMode`) se cambia con `POST /api/v5/account/set-position-mode` (`net_mode`), solo sin posiciones ni órdenes abiertas (`59000`). Propuesta: el bot no lo cambia solo; si no es `net_mode`, lo dice en el panel y el usuario decide.
- Antes de operar X-Perps, el usuario tiene que pasar el test de idoneidad de OKX y abrir la cuenta de X-Perps en la web; sin eso, las órdenes fallarán (código exacto sin confirmar).
- OKX avisa en su documentación de que seguir usando la API supone aceptar su «OKX API Agreement». Lo acepta el usuario, no el bot.

### 7. Datos públicos que ya puede usar el bot (sin claves)

- `GET /api/v5/public/instruments?instType=SPOT|FUTURES[&instFamily=…|&instId=…]`: 20 / 2 s por IP.
- `GET /api/v5/public/funding-rate?instId=<X-Perp>`, `GET /api/v5/public/mark-price?instType=FUTURES&instId=…`, `GET /api/v5/public/time`: 10 / 2 s.
- `GET /api/v5/market/ticker?instId=…`: 20 / 2 s.
- `GET /api/v5/public/position-tiers?instType=FUTURES&tdMode=isolated&instFamily=…`.
- `GET /api/v5/system/status` (mantenimientos).

Con esto el paper puede usar el funding real de los X-Perps en lugar del de Binance (`docs/04`, `market.py`, hoy «aproximación»).

### 8. Resumen de lo confirmado y lo que falta

| Punto | Estado |
|---|---|
| REST del EEE en `https://eea.okx.com`; WS en `wseea.okx.com` (real) y `wseeapap.okx.com` (demo) | Confirmado (documentación del EEE) |
| Demo con `x-simulated-trading: 1` para el EEE | Confirmado (documentación); X-Perps en demo, sin confirmar |
| Firma HMAC-SHA256 + Base64 de `timestamp + MÉTODO + ruta?consulta + cuerpo`, hora ISO con ms | Confirmado (documentación y SDK); vectores propios, verificados con `openssl` |
| `BTC/ETH/SOL/XRP-USDC` al contado, mínimos y pasos | Confirmado (GET público en `eea.okx.com`) |
| `tradeQuoteCcy: USDC` obligatorio en el EEE | Sin confirmar (se manda siempre) |
| X-Perps `…-USD_UM_XPERP-<fecha>`, `FUTURES`, `ctVal`, `lotSz`, `tickSz`, funding cada 8 h | Confirmado (GET público) |
| Que la cuenta del usuario pueda operar X-Perps por API | Sin confirmar (test de idoneidad, cuenta de X-Perps, `account/instruments`) |
| `mgnMode` de «Selected Margin» y activo del margen aislado | Sin confirmar |
| Apalancamiento máximo real de la cuenta (10x según OKX, 50x en los tramos públicos) | Sin confirmar (`leverage-info`) |
| Perpetuos `SWAP` para el EEE | Sin confirmar (probablemente no) |
| `cancel-algos` cancela un trailing; trailing en `attachAlgoOrds` | Sin confirmar (probar en demo) |
| Comisiones: contado 0,20/0,35 % o 0,08/0,10 % según la cuenta de X-Perps; X-Perps 0,02/0,05 % | Ayuda oficial de OKX; la cifra de la cuenta sale de `trade-fee` |

### 9. Comprobaciones de solo lectura al activar (las hará el bot con las claves del usuario)

Todas son GET firmados que no mueven nada. Ninguna se ha hecho todavía.

1. `GET /api/v5/public/time`: desfase del reloj (< 5 s, o se corrige).
2. `GET /api/v5/account/config`: `perm` sin `withdraw` (si lo tiene, no arranca); `ip` no vacío (si lo está, aviso); `acctLv` y `posMode`.
3. `GET /api/v5/account/instruments?instType=SPOT` y `?instType=FUTURES`: que estén los cuatro `-USDC` y los cuatro X-Perps, con su `tradeQuoteCcyList`.
4. `GET /api/v5/account/trade-fee` (contado y X-Perps): comisión real para la regla del 3 % neto.
5. `GET /api/v5/account/balance`: saldo en la cuenta de trading (hoy es 0: el usuario aún no ha metido dinero).
6. `GET /api/v5/account/leverage-info?instId=<X-Perp>&mgnMode=isolated`: apalancamiento disponible.
7. Primero todo esto con `OKX_DEMO=1` y claves de demo; las órdenes de prueba en demo las decide y activa el usuario.

`RealBroker.arrancar()` hace los pasos 1 a 5 (más la reconciliación) en cuanto hay claves, también en solo lectura. El 6 no lo usa: fija 2x con `set-leverage` antes del primer X-Perp.

---

## Modo real: cómo activarlo y cómo pararlo

### Los cuatro modos

| Modo | Cuándo | Qué hace |
|---|---|---|
| **apagado** | Por defecto. Sin `okx.env` o con alguna de las tres claves vacía | Nada: ni una petición privada |
| **solo lectura** | Claves completas, pero falta algo de lo que pide el modo real (o hay parada de emergencia) | Lee saldo, comisiones, posiciones y órdenes. No abre nada, no mueve stops ni cierra por tiempo. Solo si el bot ya tiene posiciones **suyas en esa misma cuenta** manda órdenes que **reducen el riesgo**: poner o reponer su stop, cancelar entradas suyas y, si pides la parada con cierre, cerrarlas. El cliente rechaza antes de firmar cualquier otra orden (`reduce_riesgo`: cancelar; vender al contado; en X-Perps, solo `reduceOnly`) |
| **demo** | Claves de demo y `OKX_DEMO=1` | Opera en la demo de OKX (dinero ficticio, cabecera `x-simulated-trading: 1`) |
| **real** | Claves reales **y** `OKX_DEMO=0` **y** el bot arrancado con `BOT_MODO_REAL=1` **y** tu confirmación escrita en la web **para esas mismas claves** **y** sin parada de emergencia | Opera con tu dinero en OKX EU |

Si falta cualquiera de las cinco condiciones del modo real, el bot se queda en solo lectura y el panel «Cuenta real» dice cuál falta. Los archivos se vuelven a mirar cada 5 s: no hace falta reiniciar para pasar de solo lectura a real, salvo para poner `BOT_MODO_REAL=1`.

La confirmación (`secretos\real_confirmado`) guarda la fecha y una **huella** de las claves (8 caracteres de un resumen SHA-256: no se puede sacar de ella ninguna clave). Solo se acepta con las tres claves y `OKX_DEMO=0`; una confirmación dada en demo no se guarda, y si cambias de claves deja de valer (hay que confirmar otra vez).

Cada operación queda apuntada con su **cuenta y entorno** (demo o real; un resumen del `uid` de OKX, o de la huella de las claves si OKX no lo da). Lo abierto en la demo, o con otra cuenta, se **aparta**: el bot no lo gestiona, no lo cierra y no cuenta en tus límites ni en tus resultados con las claves nuevas; el panel lo enseña en «apartadas». El capital de partida y el del día también van por cuenta. Al cambiar de claves o de entorno, **lo que esperaba tu aprobación o iba a salir se anula** (evento `humanko` o `blocked`): una orden pedida en la demo nunca sale en la cuenta real. La aprobación automática que elijas en la demo se queda en veto fuera de ella (y así lo enseñan el panel y la cartera).

### Cómo activarlo, paso a paso (para ti)

Hoy tu cuenta no tiene dinero: el bot puede leerla, pero no abrirá nada hasta que haya saldo en la **cuenta de trading** (no en la de fondos).

1. **Crea la clave de API** en <https://my.okx.com> → Perfil → API:
   - Permisos: **Lectura** y **Operar**. **Nunca «Retirar»**: si la clave lo tiene, el bot se niega a operar.
   - **Restricción de IP**: la IP pública de tu PC (sin IP, OKX caduca la clave a los 14 días sin uso y el panel lo avisa).
   - Apunta la **frase de paso** que elijas: OKX no la guarda.
   - Mejor empieza con una clave de **demo** (Operar → Trading demo → Centro personal → API de demo).
2. **Copia la plantilla**: `M:\BotFundamental-datos\secretos\okx.env.ejemplo` → `okx.env`, en la misma carpeta. Rellena `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE` y deja `OKX_DEMO=1` mientras pruebas en demo. Esa carpeta está fuera del repositorio y fuera de OneDrive: no la muevas.
3. **Arranca el bot** como siempre. Con `OKX_DEMO=1` opera en la demo. Mira el panel «Cuenta real»: modo, saldo, comisión real, si hay cortos (X-Perps) y las posiciones.
4. **En OKX, a mano y solo tú**:
   - Pasa a la **cuenta de trading** el dinero con el que quieras operar (el bot nunca mueve dinero entre cuentas).
   - Si quieres cortos: haz el test de idoneidad y abre la cuenta de X-Perps. Sin ella, el bot opera solo largos al contado y lo dice. Con ella, además, la comisión del contado baja de 0,20/0,35 % a 0,08/0,10 %.
   - Modo de posición «neto» (`net_mode`). Si no, el bot no hace cortos y lo explica.
5. **Pasar a real** (cuando la demo te convenza):
   - En `okx.env`, pon las claves **reales** y `OKX_DEMO=0`. El bot pasa a solo lectura y comprueba la cuenta.
   - Arranca el bot con la variable `BOT_MODO_REAL=1` (en `abrir-radar.ps1` o en el acceso directo: `$env:BOT_MODO_REAL = "1"` antes de lanzar el bot).
   - En la web, panel «Cuenta real», escribe la frase **`ACTIVO EL MODO REAL`** (ya con las claves reales en `okx.env`). Se guarda con la fecha y la huella de esas claves en `secretos\real_confirmado`.
   - Desde ese momento, el modo es **real**. Cada orden espera 30 s tu veto (o tu aprobación, si eliges «a mano»): con dinero real no hay aprobación automática.
6. **Revisa los límites** en el panel. Puedes bajarlos cuando quieras; no se pueden subir por encima de los de la tabla de abajo.

### Cómo pararlo

| Qué quieres | Cómo | Qué pasa |
|---|---|---|
| **Parada de emergencia** | Botón «Parada de emergencia» del panel, o crea el archivo `M:\BotFundamental-datos\secretos\PARAR_REAL` (vacío) | No entra nada más; cancela las entradas pendientes (las que esperan tu aprobación, las que iban a salir y las órdenes de entrada del bot vivas en OKX); una entrada que estaba a medias se resuelve (si se llenó, recibe su stop); **no cierra las posiciones**; pasa a solo lectura y borra la confirmación. Los stops y objetivos siguen puestos en OKX, pero el bot ya no mueve el stop ni cierra por tiempo (solo repone un stop que falte) |
| Parada y además cerrar todo | Botón «Parada y cerrar posiciones», o `PARAR_REAL` con la palabra `CERRAR` dentro | Lo mismo y cierra a mercado las posiciones del bot. Vale también **después** de una parada sin cierre y en solo lectura. Si un cierre no sale, se reintenta (con espera creciente) hasta que sale, y el aviso dice cuáles no se han podido cerrar todavía. Si el bot cae antes de hacer la parada en OKX (o no hay claves ni cuenta leída), la hace al volver: un cierre pedido no se pierde con un reinicio |
| Pausa (sin entradas nuevas) | Botón «Detener» del panel (igual que en paper) | No entra nada nuevo; las abiertas se siguen gestionando (cerrojo, seguimiento, cierre por tiempo). «Reanudar» lo quita |
| Volver tras una parada | Escribe otra vez `ACTIVO EL MODO REAL` en el panel (en demo, la misma frase) | Borra `PARAR_REAL`, guarda la confirmación nueva (solo en real) y el bot vuelve a comprobar la cuenta antes de operar. **Borrar `PARAR_REAL` a mano no basta**: la parada queda también en memoria y en la base (`real_state.parada`), así que no se levanta sola aunque no se haya podido escribir el archivo, ni tras reiniciar el bot |
| Apagarlo del todo | Borra `okx.env` o vacía una de las claves | Modo apagado: ni una petición privada |
| Que no haga nada nunca más con esa clave | Bórrala en la web de OKX | OKX la rechaza y el bot se bloquea y lo dice |

Si el bot se cae con posiciones abiertas, **el stop y el objetivo siguen en OKX**. Lo que se retrasa hasta que vuelva es el cierre por tiempo y el movimiento del stop del cerrojo. Al volver, reconcilia: manda lo que diga OKX.

**«SIN STOP EN OKX»**: si OKX rechaza el stop de una parte y tampoco deja cerrarla (por ejemplo, con OKX caído), el panel lo marca en rojo (`sin_stop`), sale un aviso fuerte y el bot vuelve a intentar poner el stop o cerrar cada 5, 10, 20… hasta 60 s. Si lo ves, pon el stop o cierra tú en la web de OKX.

---

## Qué hace el bot con dinero real

### Límites duros

| Límite | Por defecto (techo) | Desde la web | Dónde |
|---|---|---|---|
| Importe máximo por operación | **10 USDC** | Solo bajar | `techos()["max_usdc"]`; el techo solo lo cambia la variable `BOT_REAL_MAX_USDC` en tu PC |
| Pérdida máxima del día | **3 %** del saldo de OKX al empezar el día de Madrid | Solo bajar | Nunca más de 3 % (ni aunque `BOT_MAX_DAILY_LOSS_PCT` diga más) |
| Posiciones abiertas | **4** (cuentan también las posiciones de OKX que el bot no conoce) | Solo bajar | Nunca más de 4 |
| Una por moneda | Siempre | — | Incluye pendientes, en curso y posiciones de fuera del bot |
| Entradas al día | **6** | Solo bajar | Nunca más de 6 |
| Riesgo por operación | 0,5 % (`BOT_RISK_PER_TRADE_PCT`) con la comisión real | — | El tamaño es el menor de: riesgo, importe máximo, 40 % del saldo y saldo disponible (con el 5 % que congela OKX en las compras a mercado) |
| Reglas del usuario | Siempre, **también con `BOT_USER_RULES=0`** | — | Stop ≥ 1,5 %; objetivo, parcial y suelo del cerrojo ≥ 2R y ≥ 3 % neto **con la comisión real de tu cuenta**; sin break-even ni stop dinámico; sin objetivo ni cerrojo válidos, no hay operación |
| Aprobación | Veto de 30 s (o a mano) | Veto o a mano | La automática solo en demo |
| Entradas | Solo inmediatas (`timing: "now"`) | — | Las programadas siguen en paper |
| Controles del paper | Los mismos y con los mismos textos | — | Diferencial ≤ 5 pb (`market.spread` o el libro), sin dato macro de alto impacto en 10 min (`calendar` del bus), exposición total ≤ 100 % del capital (los X-Perps por su margen, la mitad del nominal; cuentan las posiciones del bot, las que están enviando o en curso y las de OKX fuera del bot) y nominal en X-Perps ≤ 2 × capital |

La pérdida del día es la **peor** de dos medidas: la variación del saldo total de OKX (`totalEq`) desde el inicio del día de Madrid y lo que han hecho las operaciones del bot (cerradas de hoy más el resultado de las abiertas). Así, meter dinero a mitad del día no tapa una pérdida, y sacarlo cuenta como pérdida (por el lado seguro). Con la cuenta a 0 no se fija capital de partida: se toma el primer saldo mayor que 0. Al superar el límite no entra nada hasta la medianoche de Madrid; las posiciones abiertas siguen con su stop.

### Órdenes en OKX

- **Entrada**: a mercado. Al contado, compra por cantidad (`tgtCcy: base_ccy`, `tradeQuoteCcy: USDC`, `banAmend: true`); la comisión de compra sale de lo comprado y el bot protege lo que de verdad queda (redondeado a `lotSz`). Cortos (y largos en perpetuo, si el ticket lo pide) en X-Perps, en contratos enteros, margen aislado y apalancamiento 2x (`set-leverage` la primera vez). Cantidades siempre **hacia abajo** (nunca más riesgo); por debajo de `minSz`, no hay operación.
- **Stop y objetivo en el exchange** en cuanto la entrada se llena, desde el **precio medio real**: una OCO (stop + objetivo) para la parte con objetivo y una condicional (solo stop) para la que sigue al cerrojo. El stop se redondea hacia fuera (nunca más cerca que el plan) y el objetivo y el suelo, hacia el lado que asegura más. Disparo con el último precio (`last`), como el paper. En X-Perps, `reduceOnly`.
- **Si no se puede poner el stop** (tres intentos, cada uno con un `algoClOrdId` nuevo), la posición se cierra al momento. **Puede quedar una parte sin stop**: si OKX rechaza a la vez el stop y el cierre (por ejemplo, OKX caído). Entonces la parte queda «pendiente», el panel la marca **SIN STOP** con un aviso fuerte, y cada pocos segundos (5, 10, 20… hasta 60 s) se busca su algo, se pone otra o se cierra, también en solo lectura y durante la parada. Si una algo queda en duda (corte de red), no se pone otra hasta saber si OKX la tiene (se busca por su `algoClOrdId` durante 10 s): nunca dos stops por la misma parte.
- **Salida A** («6 diarias v2»): la mitad sale en +3R (la OCO). Al llegar al cerrojo (+3R por el precio que ve el bot o porque OKX ejecutó la parcial), el stop de lo que queda sube al suelo de +2R con `amend-algos`; desde ahí sigue al mejor precio a 2R (`paper.lock_update`), moviéndose en OKX cuando sube al menos 0,1R y como mucho cada 5 s. **El stop solo sube** (en un corto, solo baja) y **nunca se mueve antes del cerrojo**. Si las mitades no llegan al mínimo de OKX (p. ej. 1 contrato de BTC), va entera, sin parcial, y el evento lo dice.
  - Si el último precio **ya ha cruzado** el stop nuevo (hueco, latencia del sondeo) o OKX rechaza el cambio por eso (`51278`/`51280`, sin confirmar), se cierra a mercado lo que queda con el motivo `Stop del cerrojo` o `Stop de seguimiento`, como el paper (que cierra en ese stop). Así una ganancia asegurada no vuelve a −1R.
  - Si OKX rechaza `amend-algos` por otra cosa dos veces seguidas, se cancela esa algo (comprobando que ya no está viva) y se pone otra con el stop nuevo; si no se puede, esa parte se cierra a mercado.
- **Cierre por tiempo, manual y de la parada**: a mercado, y se reintenta con espera creciente hasta que sale. Antes de mandar nada se comprueba en OKX que la posición está (saldo de la moneda al contado; posición con su signo en X-Perps): **nunca se vende ni se compra a ciegas**. Al contado, primero se cancelan las algo y **solo se vende lo que OKX confirma sin algo viva** (si no, una algo olvidada podría vender después monedas tuyas); si la venta no sale, el stop vuelve a OKX. En X-Perps, primero se cierra (orden a mercado `reduceOnly` por los contratos del bot, no `close-position`) y después se cancelan; una algo que no se pudo cancelar la limpia la reconciliación.
- **Llenados parciales**: una entrada que no se llena del todo se cancela y se protege lo llenado. Un cierre parcial cierra solo lo llenado; lo que queda sigue abierto con su stop (en X-Perps, la algo de la parte partida se cambia por otra de su tamaño) y el cierre se reintenta.
- **Idempotencia**: cada orden lleva su `clOrdId` (`bfr` + 14 caracteres + sufijo) y las órdenes a mercado, la cabecera `expTime` (hora de OKX + 10 s desde el envío, también tras `set-leverage`: si OKX la recibe más tarde, la descarta). Tras un corte de red o con OKX ocupado **no se repite a ciegas ni se da por perdida**: se consulta por `clOrdId` cada segundo hasta pasado `expTime` + 5 s. Solo si entonces OKX no la tiene se da por no enviada. Si tampoco se puede consultar, la entrada se queda «enviando» (cuenta en los límites, el panel la marca) y se consulta cada 2 s hasta saber cómo quedó; un cierre en duda deja sus partes sin reponer el stop encima hasta saberlo.
- **Reintentos**: un GET se repite con espera ante el límite de peticiones (`50011`, `50061`) y OKX ocupado (`50013`, `50001`, HTTP 5xx). Un **POST** solo se repite ante lo que seguro no se procesó (`50011`, `50061`, `50102` y HTTP 429); con `50013`, `50001`, `50004`, `50026`, HTTP 5xx, una respuesta que no es de OKX (sin `code`) o un corte, queda en duda y se comprueba por `clOrdId` (arriba). Resincroniza la hora ante `50102`; sin reintentos ante clave, firma, frase, IP o permiso (`501xx`): el bot se bloquea y lo dice. Limitador propio por ruta, a la mitad de los límites de la documentación. No sigue redirecciones (no reenvía cabeceras firmadas a otro sitio) y la URL va tal cual se firmó.
- **Reconciliación** al arrancar y cada 5 min (manda OKX): cada 5 min se vuelven a leer además los permisos de la clave (si le das «Retirar», el bot deja de operar hasta que se lo quites); las algo que saltaron con el bot parado cierran su parte; una algo que OKX ya no encuentra (ni pendiente ni consultable durante 10 s) deja su parte **sin stop**: si la parte sigue en tu cuenta, se vuelve a proteger o se cierra; si no, se da por cerrada fuera del bot; si cambiaste el stop en la web de OKX, el bot toma el tuyo; una entrada a medias se resuelve; las partes sin stop se vuelven a proteger; una algo del bot sin enlazar se casa con su parte por su `algoClOrdId` (el bot cayó entre ponerla y guardarla) y, si es de una posición ya cerrada, está duplicada o es de un X-Perp sin posición, se cancela; **las que no se sabe de qué son (¿base de datos perdida?) no se tocan** y se avisan; las órdenes de entrada viejas del bot se cancelan; las posiciones de OKX que el bot no conoce se avisan, cuentan en los límites y no se tocan.
- **Sin funding en el resultado**: lo liquida OKX en el saldo; el resultado del día se mide con el saldo.

### Lo que el bot no hará nunca

- Retirar fondos, transferir entre cuentas, tocar subcuentas, convertir, prestar o pedir prestado: **lista blanca** de 18 rutas (`RUTAS_PERMITIDAS`), comprobada antes de firmar, y pruebas que fallan si aparece una ruta fuera de ella o montada por partes. Ninguna de las rutas del apartado 5.
- Operar con una clave que tenga permiso de retirada (se mira al arrancar y cada 5 min).
- Gestionar, cerrar o contar en tus resultados lo abierto con otra cuenta o en otro entorno (demo o real), ni mandar en una cuenta una orden pedida o aprobada en otra.
- Mandar un cierre sin comprobar antes en OKX que la posición está, ni repetir a ciegas una orden en duda.
- Escribir `okx.env` (solo crea `okx.env.ejemplo` si falta) ni enseñar un valor de las claves: registro, eventos, base y web pasan por `okx_firma.limpiar`/`FiltroSecretos`, que tapan las claves conocidas y lo que se les parece. Las claves no se pueden pasar a JSON ni copiar.
- Cambiar el modo de cuenta o el modo de posición, ni aceptar acuerdos de OKX.
- Usar `cancel-all-after` (cancelaría también los stops si el bot cae).

---

## Contrato de cableado (para el coordinador)

`bot/okx_real.py` está escrito y probado, pero **no está enganchado a nada**: ni `bot/__main__.py`, ni `bot/server.py`, ni la web lo importan todavía. Esto es lo que hay que hacer.

### 1. Construcción (`bot/__main__.py`)

```python
from .okx_real import RealBroker
real = RealBroker(bus, store, lambda: state("market", {}), session=session,   # la sesión aiohttp del bot
                  get_orderflow=lambda: state("orderflow", {}))                # opcional: diferencial del libro
# … en el gather: real.run()
```

- Siempre se puede construir: sin `okx.env` queda **apagado** y no hace ninguna petición privada. Crea `secretos\okx.env.ejemplo` si falta.
- `run()` hace las comprobaciones de arranque y luego una ronda por segundo; ninguna excepción lo tumba (salvo la cancelación al apagar).
- Usa el mismo `market` del bus que el paper (precios de OKX EU con `updated`; un precio de más de 15 s no sirve; `spread` para el control del diferencial) y la agenda (`bus.state["calendar"]`) para la pausa macro, igual que el paper. `get_orderflow` (opcional, como en el paper) da el diferencial del libro si falta el del ticker.
- Una orden en duda puede tener la ronda ocupada hasta ~15 s (consultas por `clOrdId` hasta pasado su `expTime`); durante ese rato el resto de posiciones siguen protegidas por sus stops en OKX.

**Qué broker recibe los tickets.** `RealBroker` tiene la interfaz de `PaperBroker`, así que el motor de aperturas, la entrada por noticia y la fase 2 no notan la diferencia. Propuesta:

- Una variable `BOT_BROKER=paper|real` (por defecto `paper`). Con `real`, `submit` y `get_portfolio` del motor de aperturas (y de la entrada por noticia, si se quiere) son los de `RealBroker`; con `paper`, como hoy.
- El paper puede seguir corriendo para la web y sus resultados, pero **sin recibir los mismos tickets**: los dos publican eventos con la misma story (`riskok`, `filled`, `closed`…) y `decisiones` los mezclaría. Si se quiere medir en paper y operar en real a la vez, hace falta un envoltorio que mande al paper con otra story (p. ej. sufijo `-paper`) y que el motor cuente los límites con la cartera real.
- `RealBroker.portfolio()["halted"]` es `True` mientras no se pueda abrir nada (apagado, solo lectura, parada, pausa o pérdida del día): el motor ya respeta ese campo y no envía.

### 2. Interfaz (igual que `PaperBroker`)

| Método | Igual que el paper | Diferencias |
|---|---|---|
| `submit(ticket, story, headline, strategy, window) -> bool` | Síncrono; `True` si pasa los controles | La orden sale en la ronda siguiente, tras la aprobación. Solo `timing: "now"` |
| `portfolio(now=None) -> dict` | Formato 2.5 (`positions`, `pending`, `scheduled`, `closed`, `limits`, `mode`, `halted`, `caps`) | `scheduled` siempre vacío; `positions` lleva también las posiciones de OKX fuera del bot (`externa: true`); `pending` incluye las que esperan salir; `limits` añade `max_day_entries` y `max_usdc`; bloque `real` con modo y motivo |
| `decide(story, ok, by=None)`, `cancel(story, why)`, `close(story, why)` | Sí | `close` marca la posición para cerrar (`cierre_pedido`) y se reintenta con espera creciente hasta que sale; `False` si no está abierta o no se puede escribir en OKX |
| `set_mode(mode)` | Sí | `"auto"` solo en demo (devuelve `False` en real). `aprobacion_efectiva()` da la que se aplica: «auto» elegida en la demo es «veto» fuera de ella; es la que sale en `portfolio()["mode"]` y en `vista_real()["aprobacion"]` |
| `halt(why)`, `resume(why)` | Sí | Pausa guardada en `real_state` |
| `results()` | Formato 3.2 | `real: true`, `modo`; sin funding |
| `announce()`, `run()` | Sí | — |
| `publish_portfolio()` | Sí (alias) | Publica **`cuenta_real`**, no `portfolio` (el `portfolio` del bus sigue siendo del paper) |
| `parada_emergencia(motivo, cerrar_posiciones=False) -> bool` | Nuevo | Ver «Cómo pararlo». `True` si ha hecho algo nuevo (la primera parada, o el cierre pedido después de una parada sin cierre); `False` si ya estaba todo pedido |
| `set_limites(cambios) -> dict` | Nuevo | `{ok, why, limites, techos}`; claves `max_usdc`, `perdida_dia_pct`, `max_posiciones`, `max_entradas_dia` |
| `confirmar(frase) -> dict` | Nuevo | `{ok, why}`; frase exacta `ACTIVO EL MODO REAL` (sin distinguir mayúsculas ni espacios de más). Con claves reales (`OKX_DEMO=0`) guarda la confirmación atada a la huella de esas claves y quita la parada; con claves de demo solo quita la parada; sin claves, `ok: false` |
| `vista_real(now=None) -> dict` | Nuevo | Lo del panel (abajo) |
| `ronda(now=None)`, `arrancar(now=None)`, `reconciliar(now=None)` | — | Asíncronos (el paper tiene `step` síncrono) |
| `puede_abrir()`, `puede_escribir()`, `puede_reducir()` | — | Abrir; mandar cualquier orden; mandar solo las que reducen el riesgo (también en solo lectura y en la parada) |

### 3. Eventos del bus

- **Los mismos tipos que el paper**, con los mismos agentes y la misma forma de `patch`: `riskok`/`riskko` (`risk`, con `checks`, `why` y `motivo_codigo`), `humanwait`/`humanok`/`humanko` (`human`), `order`, `filled` (`exec`, `trade_id`), `manage` (cerrojo, seguimiento y parcial, con `patch.partial` como el paper), `blocked` (`exec.motivo_codigo`) y `closed` (`result`, `trade_id`, `tone`). Los motivos de cierre usan los nombres del paper (`Stop de pérdida`, `Stop del cerrojo`, `Stop de seguimiento`, `Objetivo (…)`, `Objetivo parcial (…)`, `Cierre por tiempo (…)`), así `decisiones` los clasifica sin cambios. Nuevo código de veto: «Tope diario de entradas…» → `tope_dia`.
- Todo evento del modo real lleva **`patch.real = {modo, demo}`** para distinguirlo del paper; los títulos dicen «(dinero real)», «(demo de OKX)» o «(solo lectura)».
- **Nuevo `kind: "real_aviso"`** (agente `real`, story `real` o la de la posición): cambios de modo, parada hecha (con lo que de verdad salió: `patch.aviso.resultado` = `{canceladas, cerradas, fallidas, errores}`), reconciliación, posiciones fuera del bot o apartadas de otra cuenta, stop que no se pudo poner o que OKX canceló, entrada u orden en duda, cierre parcial. **`patch.aviso.sin_stop: true`** marca una posición que se ha quedado SIN STOP en OKX (aviso fuerte: la web debería sacarlo en rojo y no esconderlo); `sin_stop: false`, que ya lo vuelve a tener. La línea de estado del agente `real` empieza por «¡SIN STOP EN OKX: …!» mientras dure.
- **Estado `agent: "real"`** (`{"type":"agent","agent":"real","patch":{"off":…, "now":…}}`): una línea de estado. La web tiene que conocer el agente `real`.
- **Nuevo mensaje de estado `cuenta_real`** (se guarda como último estado del bus y va en la foto inicial): el contenido de `vista_real()` (abajo). Sale al cambiar algo y tras cada mando.

### 4. Rutas y mensajes para la web (`bot/server.py`)

- **`GET /api/real`** → `real.vista_real()` (o `{"modo": "apagado"}` si no se construyó). Nunca lleva claves.
- **Mandos por WebSocket**, con respuesta en `orden_web` (`area: "real"`). **Origen más estricto que el paper:** hoy `origin_ok` de `bot/server.py` admite un mensaje **sin** cabecera `Origin`, es decir, cualquier proceso del propio PC (por ejemplo el OpenClaw local). Para los mandos `type: "real"` (y para `decision`, `close` y `cancel` de una story del modo real) hay que **exigir** `Origin` y que sea el de la propia web (`http://127.0.0.1:8765` o `http://localhost:8765`); sin `Origin`, rechazar. Lo mismo para cualquier ruta HTTP que llegue a `confirmar`, `set_limites` o `parada_emergencia` (la parada sí puede admitirse sin `Origin`: solo reduce el riesgo). Ningún agente (OpenClaw, Claude, la fase 2) debe tener acceso a `confirmar` ni a `decide` del modo real.

| Mensaje | Llama a | Respuesta |
|---|---|---|
| `{"type":"real","accion":"confirmar","frase":"ACTIVO EL MODO REAL"}` | `real.confirmar(frase)` | `{ok, why}` |
| `{"type":"real","accion":"parar","cerrar":false}` | `real.parada_emergencia("Parada de emergencia desde la web", cerrar)` | `{ok: true}` |
| `{"type":"real","accion":"limites","limites":{"max_usdc":5}}` | `real.set_limites(limites)` | `{ok, why}` |
| `{"type":"real","accion":"detener"}` / `"reanudar"` | `real.halt()` / `real.resume()` | `{ok}` |
| `{"type":"real","accion":"aprobacion","mode":"veto"}` | `real.set_mode(mode)` | `{ok}` |
| `decision`, `close`, `cancel` con una story del modo real (está en `real.pending`, `real.en_curso` o `real.positions`) | `real.decide` / `real.close` / `real.cancel` | como hoy |

  `cerrar` tiene que ser `true` de JSON (no la cadena) para cerrar posiciones. La web no recibe nunca nada de `okx.env`; ningún mando escribe ese archivo.
- `docs/04-contratos.md`: añadir `cuenta_real`, `real_aviso`, el agente `real`, `GET /api/real`, los mandos `type: "real"` y las tablas `real_trades` y `real_state`.

### 5. Panel «Cuenta real» (web)

Con los campos de `cuenta_real` / `GET /api/real`:

- **Cabecera**: `modo` (apagado / solo lectura / demo / real) en grande, con color propio para `real`; `etiqueta`; `por_que` (por qué no abre, si no puede) y `motivos`; `bloqueo` y `avisos` (IP sin atar, permiso de retirada…). Importes en **USDC de verdad**, sin mezclarlos nunca con los euros ficticios del paper.
- **Cuenta**: `cuenta.equity`, `cuenta.usdc_disponible`, `cuenta.monedas`, `cuenta.day_pnl` y `day_pnl_pct`, `comisiones` (contado y X-Perps, en % por lado), `cortos.ok` y `cortos.por_que`, `cuenta.posMode`, `cuenta.acctLv`, `cuenta.ip_atada`, `cuenta.permisos`.
- **Límites**: `limites` con su `techo` al lado y un control para bajarlos (`accion: "limites"`); `entradas_hoy` de `limites.max_entradas_dia`.
- **Posiciones**: `posiciones` (entrada, stop actual en OKX, objetivo, suelo, cerrojo, `partes` con sus algo y su `estado`: `viva`, `pendiente`, `cerrada`; `sin_stop` y `sin_stop_desde`), `sin_stop` (lista de monedas SIN STOP en OKX o con la entrada sin confirmar: **banda roja** arriba del panel mientras no esté vacía), `externas` (en rojo: fuera del bot), `apartadas` (de otra cuenta o de la demo: el bot no las toca; `entorno`, `modo`, `instId`, `sz`), `pendientes` (con cuenta atrás del veto y botones aprobar / vetar), `cerradas`.
- **Mandos**: «Parada de emergencia» y «Parada y cerrar posiciones» (con confirmación en la propia web; el segundo también se ofrece con una parada ya hecha), «Detener» / «Reanudar», aprobación veto / a mano, y la caja para escribir `frase_confirmacion` cuando `archivos.claves` es `true` y el modo no es real o hay `parada` (mostrando `archivos.confirmado` si ya hay confirmación para las claves de ahora). `parada` (con `motivo`, `fecha`, `cerrar`) dice que hay una parada activa; `parando`, que se está ejecutando.
- **Estado técnico**: `listo`, `reconciliacion` (cuándo, cambios), `ultimo_error`, `peticiones`, `archivos.carpeta` y si existen `okx.env` y `PARAR_REAL` (solo si existen, nunca su contenido).

### 6. Base de datos

En `bot.db` (las crea `RealBroker` al construirse; aditivas, el paper no se toca):

- `real_trades(id, story, opened, closed, body)`: una fila por operación real; abiertas con `closed` NULL (incluidas las que están «enviando»), cerradas con el formato 3.2 más `instId`, `modo`, `entorno` (`demo`/`real`), `cuenta` (resumen de la cuenta: 12 caracteres de un SHA-256), `cl` y `partes`. Las abiertas llevan además `exp_ms`, `cierre_pedido`, `cierres_dudosos` y, por parte, `cls`/`dudosas` (los `algoClOrdId` intentados) para resolver lo que quedó en duda tras una caída.
- `real_state(key, body)`: `limites`, `aprobacion`, `manual_halt`, `halt`, `parada` (la parada de emergencia pegajosa, con `archivo` = la última `PARAR_REAL` atendida y `hecha` = cuándo se hizo en OKX; sin `hecha`, al arrancar se vuelve a poner en cola), y por cuenta `day_start:<cuenta>` e `inicio:<cuenta>`.

### 7. Sin confirmar en el código (probar en demo antes de usar dinero)

Además de lo del apartado 8 de la API: que `order-algo` acepte `tradeQuoteCcy` al contado; que `amend-algos` mueva el stop de una OCO con `newSlTriggerPx` (si no, el bot cambia la algo por otra: más peticiones, mismo resultado); que `GET /trade/order-algo` traiga `ordId` (o `ordIdList`) y `actualSide` de la orden disparada, y que admita `algoClOrdId`; que `GET /trade/orders-algo-pending` admita `ordType=oco`; los nombres de los campos de `trade-fee` en el EEE (`taker`, `takerUSDC`); que el margen aislado de los X-Perps salga de USDC (`TD_PERP = "isolated"`); que `perm` de `account/config` diga `withdraw` cuando la clave puede retirar y que traiga `uid` (si no, la cuenta se identifica por la huella de las claves y, al cambiar de clave en la misma cuenta, lo abierto se aparta en vez de gestionarse); que la cabecera `expTime` se acepte en `trade/order` también en demo y qué código da una orden caducada; que `GET /trade/order?clOrdId=` encuentre una orden llena o cancelada poco después; que el stop del lado equivocado del último precio dé `51278`/`51280` en `amend-algos`; que, tras `cancel-algos`, `GET /trade/order-algo` dé `canceled` (el bot no vende al contado hasta verlo); que `GET /trade/order-algo?algoId=` siga encontrando una algo ya terminada (si no la encuentra durante 10 s, el bot la trata como desaparecida: mira en la cuenta si la parte sigue y, si sigue, le pone otro stop; la reconciliación cancela un duplicado); y que una respuesta `200` sin `code` no llegue nunca de OKX (el bot la trata como orden en duda).
