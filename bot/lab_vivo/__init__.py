"""Laboratorio vivo (docs/07-laboratorio-vivo.md): un proceso aparte, 24/7 y con prioridad baja, que recibe hipótesis en
JSON y las somete a una validación por etapas con la gestión verificada del paper, las comisiones reales, el funding
histórico y la corrección por el número de pruebas acumulado. Todo es paper: no opera, no envía órdenes y no toca la
cartera, bot.db ni estrategia_activa.json. Lo que lo supera todo pasa a una prueba en avance VIRTUAL y, si la supera,
se propone al usuario; el paso a la cartera paper lo decide él.

El bot en marcha no importa este paquete: el servidor solo leerá sus archivos JSON (apartado 14 de docs/07)."""

VERSION = 1
NOMBRE = f"lab_vivo {VERSION}"
