# Fase 1: captura de noticias, bus de eventos y web en vivo

Estado a 23-10-2026: en marcha y probada con datos reales. Esta fase no usa IA ni dinero.

## Cómo arrancarlo

Haz doble clic en `arrancar-bot.ps1`, o ejecuta en PowerShell:

```powershell
M:\BotFundamental-runtime\venv\Scripts\python.exe -X utf8 -m bot
```

La web se abre en **http://127.0.0.1:8765**. Para pararlo, pulsa Ctrl+C. Si el bot se cae o lo reinicias, la web se reconecta sola y recupera el historial.

## Dónde está cada cosa

| Qué | Dónde |
|---|---|
| Código | `bot/` en esta carpeta |
| Web | `dashboard/radar.html`. El bot la sirve en modo en vivo; abierta sola funciona como simulación |
| Entorno de Python | `M:\BotFundamental-runtime\venv`, fuera de OneDrive |
| Base de datos (eventos y noticias vistas) | `M:\BotFundamental-datos\bot.db` |
| Registro | `M:\BotFundamental-datos\logs\bot.log` (rota a los 5 MB) |
| Caché de pip | `M:\Caches\pip`, la que ya tenías configurada |

## Qué hace

1. **16 cazadores** consultan fuentes gratuitas cada 15–180 s. Cada uno funciona aparte: si una fuente cae, reintenta con espera creciente y las demás siguen.
2. El **filtro por reglas** decide en milisegundos, sin IA:
   - Universo: BTC, ETH, SOL o XRP, o un tema macro o regulatorio que afecta a todas.
   - Frescura: menos de 15 minutos desde la publicación.
   - Opinión: descarta predicciones y titulares en forma de pregunta.
   - Fuentes oficiales: solo deja pasar lo que mueve mercados (FOMC, IPC, empleo…), no cada nota institucional.
3. **Repetidas:** si otra fuente publica la misma noticia en las 3 horas siguientes, se apunta como **confirmación** de la primera. Será la materia prima del verificador.
4. **Precios en directo** de OKX EU (pares USDC) por WebSocket, con el diferencial de compra y venta.
5. **Flujo minorista** cada minuto, con datos públicos de futuros de Binance y de Coinbase:
   - ratio de cuentas largo/corto,
   - funding,
   - variación del interés abierto en 1 hora,
   - proporción de compras agresivas,
   - prima de Coinbase.
6. **Agenda macro:** calendario oficial del BLS (IPC, empleo, IPP, JOLTS…) y reuniones de la Fed de 2026–2027.
7. **Bus de eventos:** guarda cada evento en SQLite y lo envía por WebSocket a la web. Al conectarse, la web recibe los últimos 1.500 eventos.

## Fuentes

| Fuente | Nivel | Cada | Notas |
|---|---|---|---|
| Tree News (API pública) | T1 (los tuits que reenvía, T2) | 15 s | Incluye cuentas clave de X sin pagar la API de X |
| Anuncios OKX EU, Binance, Upbit | T0 | 30 s | Binance no sirve para operar desde España, pero sus anuncios mueven el precio global |
| Reserva Federal, BLS, SEC (prensa), BCE | T0 | 60–120 s | RSS oficiales |
| SEC EDGAR (búsqueda de texto) | T0 | 180 s | Registros 8-K, S-1 y 19b-4 que citan bitcoin, ethereum, solana o XRP |
| Estado de Coinbase y Kraken | T0 | 120 s | Caídas e incidencias |
| CoinDesk, Cointelegraph, The Block, Decrypt, Bitcoin Magazine | T2 | 60–120 s | RSS |
| Precios OKX EU | Mercado | continuo | WebSocket |
| Posicionamiento (Binance, Coinbase) | Mercado | 60 s | Endpoints públicos, sin cuenta |

Descartadas en las pruebas:
- CryptoSlate: bloquea a los bots con Cloudflare.
- Calendario de ForexFactory: devuelve "rate limited". Lo sustituye el calendario oficial del BLS.
- WebSocket gratuito de Tree News: llega con retraso. Su API pública es más rápida.

## Configuración opcional (variables de entorno)

| Variable | Para qué |
|---|---|
| `SEC_USER_AGENT` | La SEC pide identificarse con nombre y correo, por ejemplo `"Tu Nombre tu@correo.com"`. Ahora funciona sin él, pero es lo correcto. |
| `BOT_DATA_DIR`, `BOT_PORT`, `BOT_HOST` | Cambiar la carpeta de datos o el puerto. Por defecto la web solo escucha en este PC. |

## Pruebas

```powershell
M:\BotFundamental-runtime\venv\Scripts\python.exe -X utf8 -m pytest -q
```

Son 23 pruebas: filtro, lectores de cada fuente con muestras reales, mesa de noticias (repetidas y confirmaciones) y servidor con WebSocket.

## Qué falta (fase 2)

- **Verificador (Haiku 5.5):**
  - Puntúa cada noticia relevante con su nivel, las confirmaciones y las señales de alarma.
  - Se prueba con el banco de casos falsos y verdaderos.
- **Analista de impacto (Sonnet 5.5)** y **estratega (Opus 5.5)**, que prepara cada apertura con la agenda macro.
- **Clave de API de Claude**, pagada con los créditos de tu plan Max.
  - Hay que activar los créditos en claude.ai › Ajustes › Facturación. Eso solo puedes hacerlo tú.
  - Se fija un límite de gasto en la Consola.
- Opcional: **Telegram** (canales de titulares) con tu cuenta. Necesita tu número y un código, que tendrás que introducir tú.
