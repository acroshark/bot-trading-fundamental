# Bot de trading fundamental (cripto, SOLO PAPER)

Sistema multiagente 24/7 basado en noticias frescas verificadas y aperturas de bolsa, para BTC, ETH, SOL y XRP en USDC.
La plataforma elegida es OKX EU (MiCA). Todo es **paper**: nunca se opera con dinero real ni se da consejo de inversión personalizado.
El usuario habla español: código, comentarios, textos de la web y documentación van en español.

## Reglas del usuario (intocables)
- Relación beneficio/riesgo ≥ 2:1 (ideal 3:1). Stop nunca menor del 1,5 % (el preset usa 3 % y objetivo 3R).
- Sin break-even ni stop dinámico: bracket puro, «o gano más de 2 o pierdo 1, siempre».
- Objetivo ≥ 3 % neto de costes. Largos al contado y cortos en perpetuo. Una posición por moneda; límites diarios de entradas y riesgo.
- Operativa: medir y seguir el impulso de los primeros minutos de cada apertura, respaldado por las noticias y por las aperturas anteriores.
- Historia para backtests: como mucho 3 años.
- Están en `config.USER_RULES`. El paper rechaza lo que no las cumpla. Ningún cambio automático (evolución, aprendizaje) puede romperlas.

## Honestidad con los datos
- Los backtests no muestran ventaja robusta. Ninguna de las 690 estrategias generales funciona fuera de muestra.
- El laboratorio de 2.000 estrategias (3 años) dejó 2 prometedoras y 0 robustas.
- No se promete rentabilidad: se mide en paper y se enseñan las cifras. Lo único sin medir es la señal en vivo de las noticias.
- Las expectativas del usuario (≥ 3 % por apertura) se contrastan con datos, sin discutir por discutir.
- Desde el 28-10-2026 (14:49) la cartera de prueba opera «6 diarias» por decisión del usuario (`apertura.SEIS`, activada con `scripts/activar_estrategia.py --preset seis`): impulso de 15 min sin filtros, stop 1,5 %, 3R, como mucho 24 h, ≤ 6 entradas y 3 % de riesgo al día. En el backtest con el propio motor pierde: −0,12 R por operación fuera de muestra, ≈ −0,3 % de la cuenta por jornada. Se mide en vivo tal cual.
- La anterior (E2, impulso): +0,14 R por operación fuera de muestra con la salida del laboratorio y +0,35 R con la de 7 días, pero esta segunda se eligió después de ver los datos y no está validada. `--preset impulso` vuelve a ella.
- Noticias fiables (`bot/fiables.py`): con los criterios fijados (≥ 60 % ajustado, constancia por día, n y días mínimos) pasan grupos que no se distinguen del azar y ninguno pasa la puerta para operar, así que la entrada por noticia (`bot/entrada_noticias.py`) aún no abre nada. 91 días de titulares públicos: 49,2 %, ningún medio pasa (docs/05, sección 20).
- Con ≥ 6 operaciones al día y las reglas del usuario, la mejor variante pierde (≈ −0,4 % de la cuenta al día fuera de muestra). Ni sabiendo el sentido a 24 h se pasa de +1,9 %/día con 0,5 % de riesgo. Para no perder, la noticia tendría que acertar el sentido un 60 %, y las grabadas aciertan el 49 % (docs/05, sección 19).
- Nunca se maquillan resultados ni se cambia el riesgo para llegar a un objetivo. La cartera paper de prueba (100 € ficticios desde el 28-10-2026) se mide tal cual.

## Estructura
- `bot/`: el bot (Python 3.11, asyncio). Arranque y cableado en `bot/__main__.py`; configuración en `bot/config.py` (todo con variables `BOT_*`).
  - Captura: `feeds.py` (5.714 RSS verificados en `bot/fuentes/*.json`), `hunters.py`, `filters.py`, `topics.py`, `desk.py`.
  - Análisis: `local_ai.py` (triaje con llama.cpp + Qwen local), `claude_agents.py` (fase 2: verificación, impacto y estrategia con la API de Claude), `predict.py`.
  - Mercado: `market.py` (precios, libro, flujo y funding de Binance), `trend.py`, `agenda.py`, `regions.py`.
  - Operativa: `apertura.py` (motor de aperturas; presets «impulso» = E2 y «seis» = 6 diarias), `entrada_noticias.py` (entrada por noticia fiable confirmada por el precio), `paper.py` (broker paper con perpetuos y funding).
  - Medida: `backtest.py`, `backtest_aperturas.py`, `laboratorio.py`, `historia_extra.py`, `vigilancia.py` (prueba en avance virtual de la lista congelada), `reaccion.py` (la cripto en cada apertura, mensaje `cripto_aperturas`; historia en `BACKTEST_DIR/reaccion_aperturas.json`), `reflejo.py` (si la cripto confirma una noticia macro).
  - Sistema que aprende: `aprendizaje.py` (acierto de noticias y medios ajustado al mercado, pesos), `fuentes_vivas.py` (estados prueba, activa, silenciada y retirada; descubrimiento), `evolucion.py` (historial solo de añadir, campeón frente a aspirantes, informe semanal, propuestas), `fiables.py` (grupos de noticias fiables, recalculados cada hora, con su azar de referencia).
  - Infraestructura: `bus.py`, `store.py` (SQLite WAL), `server.py` (API HTTP y WebSocket en 127.0.0.1:8765).
- `dashboard/radar.html`: la web, en un solo archivo, con modo en vivo y modo de demostración. Lleva el backtest incrustado mediante `scripts/incrustar_backtest.py`.
- `docs/04-contratos.md`: contrato de mensajes, rutas y tablas. **Actualízalo con cada cambio** de mensajes, rutas o esquemas.
- `docs/05-fuentes-tendencia-backtest-fase2.md`: guía con cifras reales.
- `openclaw-trading/`: OpenClaw con IA local para el PC de la RTX 3060 Ti.
- `.claude/skills/trader-*`: skills de day-trader-skill (MIT).

## Pruebas
- `python -m pip install -r requirements.txt`, luego `python -m pytest -q`. Ninguna prueba usa la red.
- Las de la web (`tests/test_radar_web.py`) necesitan Node (`NODE` o `node` en el PATH); sin Node se saltan.
- `tests/conftest.py` pone los temporales en M: (Windows) o en el temporal del sistema (Linux) y desactiva por defecto las reglas del usuario (`BOT_USER_RULES=0`). Las pruebas de las reglas las activan.
- Cada cambio lleva sus pruebas.

## Entornos
- **Arranque sin nadie delante:** `scripts/vigilante-bot.ps1` relanza el bot si cae. `scripts/nueva_cartera_paper.py` empieza otra cartera paper y archiva la anterior.
- **PC del usuario (Windows):** datos en `M:\BotFundamental-datos` (bot.db, backtests, velas) y entorno en `M:\BotFundamental-runtime\venv`. Los datos van siempre a M:, nunca a C:. Para 24/7 se arranca con el acceso directo «Radar Fundamental» (`abrir-radar.ps1`).
- **Claude en la nube (Linux):** no están los datos, ni la IA local, ni las claves. Se puede trabajar en el código y las pruebas. `BOT_DATA_DIR` va por defecto a `~/BotFundamental-datos`. Al arrancar, el gancho SessionStart de `.claude/settings.json` ejecuta `scripts/nube-preparar.sh`, que instala `requirements.txt` solo si `CLAUDE_CODE_REMOTE=true`. Los resultados reales (backtests, paper, aprendizaje) solo existen en el PC del usuario.
- Nunca subas secretos: claves de API, tokens de Telegram ni tokens del gateway de OpenClaw. Las claves las pone el usuario en su PC.
