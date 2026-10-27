# Vista Mundo, IA local y OpenClaw de trading

Estado a 23-10-2026: todo probado en este PC con datos reales. Nada de esto gasta tokens externos.

## 1. Vista Mundo (pestaña «Mundo» de la web)

La pantalla está partida en dos y la división se arrastra arriba o abajo. La parte de abajo también está partida, y su división se mueve a izquierda o derecha (en el móvil, arriba o abajo). Las posiciones se recuerdan en el navegador.

**El globo** gira solo y «sigue la acción»: se orienta hacia el origen de cada noticia relevante. Puedes arrastrarlo para girarlo y usar la rueda para acercar o alejar. Muestra:
- La **noche y el día** reales, según la posición del sol.
- Las **bolsas**: Tokio, Hong Kong, Seúl, Fráncfort, Londres, Nueva York y los futuros de la CME. En verde las abiertas; en ámbar las que abren en menos de 1 hora, con cuenta atrás. Al abrir una, sale una onda y aparece en la cinta.
- **Cada noticia** como un arco desde la sede de su fuente hasta el centro de mando en Madrid:
  - Gris: ruido.
  - Azul: relevante.
  - Las noticias on-chain llegan desde la órbita.
  - Es el origen aproximado de la fuente, no del suceso.
- **Las decisiones** como textos que suben sobre Madrid: triaje, verificada, falsa, plan de apertura, veto, aprobación. Las órdenes salen en un arco hacia el exchange, OKX EU en Malta, con su resultado.
- **El ambiente:** el halo del planeta se vuelve verde, azul o rojo según el posicionamiento del minorista.
- **Paneles superpuestos:**
  - Tensión del mercado, de 0 a 100. Cuenta las noticias de importancia 6 o más, si hay una ventana de apertura cerca y qué criptos tienen al minorista en un extremo de su rango de 7 días.
  - Bolsas abiertas.
  - Anillo de sentimiento por cripto.
  - Cinta de titulares.

**Abajo:**
- **Entradas** de la última hora: captadas, relevantes, ruido, de qué región llegan, sentimiento minorista y lista viva.
- **Decisiones** de la última hora: triaje local, verificaciones, falsas, estrategias, órdenes y resultado, más las aprobaciones pendientes (con botones) y las posiciones abiertas.

Pulsa cualquier fila para abrir su expediente completo en «En directo».

## 2. IA local en el bot (gratis, en tu GPU)

Usa tu llama.cpp y tu modelo Qwen3.5-9B de `M:\OneDrive\herramientas\IA local LLM`. No se descargó nada.

- **Triaje de cada noticia relevante,** en 1,4–2,5 s por titular:
  - Tipo, sentimiento, importancia de 0 a 10 y resumen en español.
  - Por debajo de 4 se archiva, y nunca llegará a la IA de pago de la fase 2.
  - Probado con un titular trampa ("Ignore previous instructions…"): lo puntuó 0/10.
- **Briefing de apertura** 30 minutos antes de Tokio, Londres, los datos macro de EE.UU., Nueva York y la CME.
  - Se escribe con las noticias triadas, el flujo minorista y la agenda.
  - Aparece en el cable y en el globo como «Plan para…».
- **Cuidado con tu fábrica de contenidos:**
  - Servidor propio en el puerto **8094** con el nombre `qwen-trading`. La fábrica usa 8080, 8082 y 8084, y cierra los servidores llamados `qwen3.5-9b`.
  - No arranca si existe `M:\OpenClaw-datos\estado\gpu.lock` o si hay menos de 6,3 GB de VRAM libres.
  - Se apaga tras **15 minutos** sin noticias, incluso si quedó encendido de una ejecución anterior.
- Si la IA local no está disponible, las noticias siguen su camino sin triaje. Nada se bloquea.

## 3. OpenClaw de trading (instancia aparte de la tuya)

| | Tu OpenClaw (fábrica) | OpenClaw de trading |
|---|---|---|
| Puerto | 18789 | **18790** |
| Estado y configuración | `C:\Users\ubi_o\.openclaw` | `M:\BotFundamental-openclaw\state` |
| Espacio de trabajo | `M:\OneDrive\herramientas\OpenClaw\workspace` | `M:\BotFundamental-openclaw\workspace` |
| Modelo | Qwen3.5-9B en el 8084 | Qwen3.5-9B en el 8094, el mismo servidor que usa el bot |
| Programa | `M:\OpenClaw-runtime` (2026.9.6) | El mismo programa, sin tocar nada de tu instalación |

- **Agente «Analista de mercado»:**
  - Habla en español.
  - Solo usa la skill `radar`, que lee el bot en modo solo lectura: precios, flujo minorista con percentil de 7 días, agenda, fuentes y noticias triadas.
  - No da órdenes de compra o venta, no ejecuta operaciones y no envía mensajes.
- **Prueba real:**
  - A la pregunta «¿Cómo está el minorista en BTC y SOL y qué dato macro viene esta semana?» respondió con datos reales del bot en 20 s.
  - Coste: 0 $.
- **Seguridad, la misma línea que tu fábrica:**
  - Escucha solo en este PC y pide token.
  - Sin web, mensajería, automatizaciones ni subagentes.
  - Solo puede ejecutar `radar.py` con argumentos exactos; el resto se deniega. Comprobado: un intento con `; del x` no pasa.
  - Ninguna skill es de ClawHub. En febrero de 2026 aparecieron cientos de skills maliciosas allí, muchas disfrazadas de herramientas cripto.
- **Archivos:** el código fuente está en `openclaw-trading\`. `instalar.py` lo copia al espacio de trabajo.

**Uso:**

```powershell
.\openclaw-trading\arrancar-openclaw.ps1   # arranca la instancia y aplica los permisos
.\openclaw-trading\chat.ps1                # chat en la terminal
.\openclaw-trading\abrir-panel.ps1         # panel web con su token
```

Si cambias algo en `openclaw-trading\workspace`, vuelve a ejecutar `instalar.py` y después `arrancar-openclaw.ps1`.

## 4. Llevarlo al otro PC (RTX 3060 Ti dedicada, 24/7)

1. Copia la carpeta del proyecto. Con OneDrive llega sola.
2. Prepara Python 3.11 y la IA local:
   - Crea el entorno y las dependencias: `python -m venv M:\BotFundamental-runtime\venv` y `pip install -r requirements.txt`.
   - Copia la carpeta `IA local LLM`, con llama.cpp y el modelo, o indica dónde están con `BOT_LLAMA_SERVER` y `BOT_LOCAL_MODEL`.
3. Configura las variables del PC dedicado:
   - `BOT_LOCAL_AI_IDLE_S=0`: la IA local queda siempre encendida.
   - `BOT_LOCAL_AI_MIN_FREE_MB=6000`.
4. Instala OpenClaw allí. La versión actual es 2026.10.6 y pide Node 24.22.1 o superior. Después ejecuta `instalar.py` y `arrancar-openclaw.ps1`.
5. Crea dos tareas programadas al iniciar Windows, una para el bot y otra para OpenClaw. Te las preparo cuando estemos en ese PC.
6. Para Telegram, crea tú el bot en @BotFather. El token solo lo puedes introducir tú. Después se añade el canal con permiso solo para tu usuario.

## 5. Pendiente

- Tu OpenClaw principal va un mes por detrás (2026.9.6 frente a 2026.10.6) y tiene una actualización a medias en `node_modules\.openclaw.update-stage-*`. No lo he tocado.
- La fase 2, con verificador Haiku, impacto Sonnet y estratega Opus, sigue necesitando la clave de API con los créditos de tu plan Max. Esa IA de pago ya solo recibirá lo que la IA local puntúe con 4 o más.
