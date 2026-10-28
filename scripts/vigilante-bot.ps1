# Vigilante del bot: cada minuto comprueba /health y, si falla dos veces seguidas, vuelve a arrancar el bot.
# Uso (oculto y sin depender de ninguna ventana ni sesión):
#   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File scripts\vigilante-bot.ps1
# Para pararlo: crea el archivo M:\BotFundamental-datos\parar-vigilante (o mata su proceso). Registro en logs\vigilante.log.
$ErrorActionPreference = "Continue"
$proyecto = Split-Path -Parent $PSScriptRoot
$python = "M:\BotFundamental-runtime\venv\Scripts\python.exe"
$datos = "M:\BotFundamental-datos"
$registro = Join-Path $datos "logs\vigilante.log"
$parar = Join-Path $datos "parar-vigilante"
$url = "http://127.0.0.1:8765/health"

$unico = New-Object System.Threading.Mutex($false, "Global\RadarFundamentalVigilante")
if (-not $unico.WaitOne(0)) { exit 0 }   # ya hay otro vigilante

function Apunta([string]$texto) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $texto" | Out-File -FilePath $registro -Append -Encoding utf8
}

function Vivo {
    try { return (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 10).StatusCode -eq 200 } catch { return $false }
}

Apunta "Vigilante en marcha (PID $PID)"
$fallos = 0
while (-not (Test-Path $parar)) {
    if (Vivo) { $fallos = 0 }
    else {
        $fallos++
        $dueno = (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
        if ($dueno -and $fallos -ge 5) {   # escucha pero no contesta en 5 min: colgado; se cierra con sus hijos
            Apunta "El bot (PID $dueno) está colgado: se cierra"
            taskkill /PID $dueno /T /F | Out-Null
            Start-Sleep -Seconds 3
            $dueno = $null
        }
        if (-not $dueno -and $fallos -ge 2) {   # nadie escucha en el puerto: así nunca hay dos bots a la vez
            Apunta "El bot no responde: se vuelve a arrancar"
            Start-Process -FilePath $python -ArgumentList "-X", "utf8", "-m", "bot" -WorkingDirectory $proyecto -WindowStyle Hidden
            $fallos = 0
            for ($i = 0; $i -lt 90 -and -not (Vivo); $i++) { Start-Sleep -Seconds 2 }
            if (Vivo) { Apunta "Bot arrancado de nuevo" } else { Apunta "El bot sigue sin responder tras 3 min (mira logs\bot.log)" }
        }
    }
    Start-Sleep -Seconds 60
}
Apunta "Vigilante parado (existe $parar)"
