# Abre el panel web del OpenClaw de trading con su token (el token no sale de este PC).
$env:OPENCLAW_STATE_DIR = "M:\BotFundamental-openclaw\state"
$env:OPENCLAW_CONFIG_PATH = "M:\BotFundamental-openclaw\state\openclaw.json"
& "M:\OpenClaw-runtime\node\node.exe" "M:\OpenClaw-runtime\node\node_modules\openclaw\openclaw.mjs" dashboard
