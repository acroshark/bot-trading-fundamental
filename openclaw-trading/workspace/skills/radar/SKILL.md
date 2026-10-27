---
name: radar
description: Lee el Radar Fundamental (precios USDC, noticias puntuadas, balanza de noticias, bolsas, libro de órdenes y volumen, predicciones, backtest, motor de aperturas y cartera paper). Úsala para cualquier pregunta sobre el mercado cripto o las aperturas de bolsa.
metadata: { "openclaw": { "os": ["win32"] } }
---
# Radar de mercado

Ejecuta con la herramienta exec exactamente uno de estos comandos y responde a partir de su salida.
Todos empiezan por `M:/BotFundamental-runtime/venv/Scripts/python.exe -I -B M:\BotFundamental-openclaw\workspace\skills\radar\scripts\radar.py` seguido de:

- `estado`: precios, flujo minorista, agenda, fuentes e IA local.
- `noticias --horas 3 --min 5`: noticias relevantes de las últimas N horas (1-48) con importancia mínima M (0-10). Añade `--bolsa tokio` (o seul, shanghai, hongkong, dubai, frankfurt, londres, madrid, nuevayork, saopaulo) para las que afectan a esa bolsa.
- `balanza`: hacia dónde empujan las noticias de las últimas 6 h en cada moneda (compra frente a venta) y las que más pesan.
- `tendencia`: estado y variación de cada bolsa, y libro de órdenes, compras agresivas, CVD y volumen relativo de cada moneda.
- `predicciones`: probabilidad de subida de las próximas aperturas y ventanas cripto, con sus motivos y el acierto medido.
- `backtest`: qué estrategias han funcionado con datos reales tras comisiones.
- `aperturas`: estado del motor de aperturas (señal, rango de los primeros minutos, volumen relativo, entradas y reentradas).
- `cartera`: capital, resultado del día y posiciones de la cartera paper (dinero ficticio).
- `flujo` y `agenda`: solo el flujo minorista o solo la agenda macro.

No cambies las rutas ni añadas otros argumentos: la lista de comandos permitidos los bloquearía.
Si la salida empieza por `ERROR`, explica que el bot no responde y que hay que arrancarlo con `arrancar-bot.ps1`.
