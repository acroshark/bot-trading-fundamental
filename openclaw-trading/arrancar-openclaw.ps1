# Arranca el OpenClaw de trading en el puerto 18790. El de la fábrica (18789) no se toca.
# Todo su estado vive en M:\BotFundamental-openclaw, fuera de OneDrive.
$node = "M:\OpenClaw-runtime\node\node.exe"
$oc = "M:\OpenClaw-runtime\node\node_modules\openclaw\openclaw.mjs"
$env:OPENCLAW_STATE_DIR = "M:\BotFundamental-openclaw\state"
$env:OPENCLAW_CONFIG_PATH = "M:\BotFundamental-openclaw\state\openclaw.json"
$env:OPENCLAW_GATEWAY_PORT = "18790"
$logs = "M:\BotFundamental-openclaw\logs"
$env:TMPDIR = "M:\BotFundamental-openclaw\tmp"   # sus registros temporales también en M:, no en C:
New-Item -ItemType Directory -Force $logs, $env:TMPDIR | Out-Null

if (Get-NetTCPConnection -LocalPort 18790 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "El OpenClaw de trading ya está en marcha (puerto 18790)."
} else {
    Start-Process -FilePath $node -ArgumentList @($oc, "gateway", "--port", "18790") -WindowStyle Hidden `
        -RedirectStandardOutput "$logs\gateway.log" -RedirectStandardError "$logs\gateway-error.log"
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 1
        if (Get-NetTCPConnection -LocalPort 18790 -State Listen -ErrorAction SilentlyContinue) { break }
    }
}
# Solo puede ejecutar la skill radar con argumentos exactos; todo lo demás se deniega.
& $node $oc approvals set --file "M:\BotFundamental-openclaw\state\aprobaciones.json" | Out-Null
Write-Host "OpenClaw de trading listo. Panel: abrir-panel.ps1 · Chat en la terminal: chat.ps1"
