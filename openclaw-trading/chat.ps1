# Chat en la terminal con el analista de mercado (modelo local, sin coste).
$env:OPENCLAW_STATE_DIR = "M:\BotFundamental-openclaw\state"
$env:OPENCLAW_CONFIG_PATH = "M:\BotFundamental-openclaw\state\openclaw.json"
& "M:\OpenClaw-runtime\node\node.exe" "M:\OpenClaw-runtime\node\node_modules\openclaw\openclaw.mjs" chat --agent trading
