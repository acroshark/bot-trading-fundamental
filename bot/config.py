"""Configuración del bot. Los datos van a M:, fuera de OneDrive, para que la sincronización no bloquee la base de datos."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", r"M:\BotFundamental-datos" if os.name == "nt"
                               else str(Path.home() / "BotFundamental-datos")))   # Linux: Claude en la nube
DB_PATH = DATA_DIR / "bot.db"
LOG_DIR = DATA_DIR / "logs"
TMP_DIR = DATA_DIR / "tmp"   # temporales del bot (también los de SQLite): en M:, nunca en C:
DASHBOARD = ROOT / "dashboard" / "radar.html"

HOST = os.environ.get("BOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("BOT_PORT", "8765"))
# Orígenes extra (separados por comas) que pueden mandar órdenes por /events, además de la propia web del bot.
WS_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("BOT_WS_ORIGINS", "").split(",") if o.strip()]
USER_AGENT = os.environ.get("BOT_USER_AGENT", "BotFundamental/0.1 (investigacion personal)")
# La SEC pide identificarse con un contacto ("Nombre correo@dominio"); se configura con esta variable.
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", USER_AGENT)

PHASE = 2
COINS = ["BTC", "ETH", "SOL", "XRP"]
QUOTE = "USDC"

FRESH_MAX_S = 15 * 60        # más antigua que esto ya no es noticia fresca
BACKFILL_MAX_S = 6 * 3600    # en el primer sondeo de cada fuente se ignora lo anterior a esto
DEDUPE_WINDOW_S = 3 * 3600   # ventana para detectar la misma noticia en otra fuente
HISTORY_EVENTS = 1500        # eventos que recibe la web al conectarse (sin contar descartes)
HISTORY_FILTERED = 300       # descartes que recibe la web al conectarse
KEEP_FILTERED_S = 48 * 3600  # los descartes se borran de la base pasado este tiempo
KEEP_SEEN_S = 14 * 86400     # claves de piezas ya vistas (los feeds no repiten piezas tan viejas)
# Captadas, confirmaciones, verificaciones y flujo (~2.000 filas/h): nada las lee pasadas 48 h. Los triajes, planes y
# operaciones no se podan (el paper y el backtest los leen sin límite de tiempo).
KEEP_EVENTS_S = int(os.environ.get("BOT_KEEP_EVENTS_DAYS", "30")) * 86400
PRUNE_EVENT_KINDS = ("captured", "corroborated", "srcstate", "verified", "unverified", "noimpact", "flow")
FLOW_INTERVAL_S = 60
MARKET_PUSH_S = 2.0

# IA local (llama.cpp en este PC): triaje gratis antes de gastar IA de pago.
LLAMA_SERVER = Path(os.environ.get("BOT_LLAMA_SERVER", r"M:\OneDrive\herramientas\IA local LLM\llama\llama-server.exe"))
LOCAL_MODEL = Path(os.environ.get("BOT_LOCAL_MODEL", r"M:\OneDrive\herramientas\IA local LLM\modelos\Qwen3.5-9B-Q4_K_M.gguf"))
LOCAL_AI_PORT = int(os.environ.get("BOT_LOCAL_AI_PORT", "8094"))   # distinto de los de la fábrica (8080, 8082, 8084)
LOCAL_AI_ALIAS = "qwen-trading"                                    # la fábrica cierra los servidores llamados qwen3.5-9b
LOCAL_AI_CTX = int(os.environ.get("BOT_LOCAL_AI_CTX", "32768"))    # compartido con el OpenClaw de trading
LOCAL_AI_AUTOSTART = os.environ.get("BOT_LOCAL_AI_AUTOSTART", "1") != "0"
LOCAL_AI_IDLE_S = int(os.environ.get("BOT_LOCAL_AI_IDLE_S", "900"))  # 0 = siempre encendida (PC dedicado)
LOCAL_AI_MIN_FREE_MB = int(os.environ.get("BOT_LOCAL_AI_MIN_FREE_MB", "6300"))
GPU_LOCK = Path(os.environ.get("BOT_GPU_LOCK", r"M:\OpenClaw-datos\estado\gpu.lock"))  # lo crea la fábrica al usar la GPU
# Sin VRAM para el servidor propio, se usa el llama-server de la fábrica si sirve el mismo modelo, solo cuando está libre
# y nunca con gpu.lock. BOT_LOCAL_AI_SHARE=0 lo desactiva.
LOCAL_AI_SHARE = os.environ.get("BOT_LOCAL_AI_SHARE", "1") != "0"
LOCAL_AI_SHARE_PORTS = (8082, 8080, 8084)
LOCAL_AI_SHARE_MATCH = "Qwen3.5-9B"
TRIAGE_MIN = 4               # por debajo, la noticia se archiva sin gastar IA de pago

# Fuentes masivas (cientos por bolsa): registro en bot/fuentes/*.json
FEEDS_DIR = ROOT / "bot" / "fuentes"
FEEDS_CONCURRENCY = int(os.environ.get("BOT_FEEDS_CONCURRENCY", "24"))
CONTEXT_MAX_S = 6 * 3600     # entre FRESH_MAX_S y esto la noticia entra como «contexto» (sirve para preparar aperturas)

# Datos históricos y backtest
HIST_DIR = DATA_DIR / "historico"
BACKTEST_DIR = DATA_DIR / "backtest"
FEE_PCT = 0.10               # comisión por lado en los backtests y el paper trading (OKX EU, base)

# Paper trading (dinero ficticio con precios reales de OKX EU)
PAPER_EQUITY = float(os.environ.get("BOT_PAPER_EQUITY", "10000"))
# Reglas del usuario para TODA operación (26-10-2026; 28-10-2026 «dejar correr»): «o gano más de 2 o pierdo 1».
# Stop ≥ 1,5 %; toda parte que gana lo hace en ≥ 2R (ideal 3R) y deja ≥ 3 % neto de costes; nada mueve el stop antes de
# ganar ese mínimo (sin break-even ni stop dinámico). Se permite: objetivo fijo; salida parcial con objetivo ≥ 2R y ≥ 3 %
# neto; y un cerrojo: al llegar a +N R el stop sube a un suelo ≥ 2R y ≥ 3 % neto, y desde ahí puede seguir al precio
# (nunca baja). El paper apaga lo que no cumple y rechaza la operación sin objetivo ni cerrojo válidos.
# BOT_USER_RULES=0 las desactiva (solo para las pruebas de la v1).
USER_RULES = os.environ.get("BOT_USER_RULES", "1") != "0"
RULE_MIN_STOP_PCT = 1.5
RULE_MIN_RR = 2.0
RULE_DEFAULT_RR = 3.0        # objetivo por defecto cuando el estratega no da uno
RULE_MIN_NET_PCT = 3.0       # toda parte que gana deja ≥ 3 % neto de comisión y deslizamiento de ida y vuelta
RISK_PER_TRADE_PCT = float(os.environ.get("BOT_RISK_PER_TRADE_PCT", "0.5"))
MAX_DAILY_LOSS_PCT = float(os.environ.get("BOT_MAX_DAILY_LOSS_PCT", "3"))
MAX_POSITIONS = int(os.environ.get("BOT_MAX_POSITIONS", "4"))
APPROVAL_MODE = os.environ.get("BOT_APPROVAL_MODE", "auto")   # auto (solo paper) | veto | manual
VETO_SECONDS = 30

# Fase 2: agentes con la API de Claude (créditos del plan Max). Sin ANTHROPIC_API_KEY quedan apagados.
CLAUDE_VERIFIER_MODEL = "claude-haiku-5-5"
CLAUDE_IMPACT_MODEL = "claude-sonnet-5-5"
CLAUDE_STRATEGIST_MODEL = "claude-opus-5-5"
CLAUDE_DAILY_BUDGET_USD = float(os.environ.get("BOT_CLAUDE_DAILY_BUDGET_USD", "3"))
VERIFY_MIN_IMPORTANCE = 5    # solo se verifica con Claude lo que la IA local puntúa 5 o más
IMPACT_MIN_IMPORTANCE = 6
