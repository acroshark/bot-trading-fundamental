# Abre el Radar Fundamental: arranca el bot si no está en marcha, su vigilante y el laboratorio vivo (si ya están en
# marcha no hace nada con ellos) y pone una ventana en cada monitor.
#   Monitor principal: Mundo (globo). Segundo monitor: Mando (todo de un vistazo).
#   Con un solo monitor abre solo la principal; el botón «2.ª pantalla» de la web abre la otra.
# Opciones: -Principal mundo|mando|directo|...  -Secundario ...  -PantallaCompleta  -Prueba (solo dice qué haría)
param([string]$Principal = "mundo", [string]$Secundario = "mando", [switch]$PantallaCompleta, [switch]$Prueba)

$proyecto = $PSScriptRoot
$python = "M:\BotFundamental-runtime\venv\Scripts\python.exe"
$url = "http://127.0.0.1:8765/"
$perfiles = "M:\BotFundamental-runtime\navegador"   # perfiles propios del navegador, en M: (uno por ventana para poder colocarlas)

function BotVivo {
    try { return (Invoke-WebRequest "$($url)health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { return $false }
}

if (-not (BotVivo)) {
    if ($Prueba) { Write-Host "Arrancaría el bot (minimizado) con $python" }
    else {
        Start-Process -FilePath $python -ArgumentList "-X", "utf8", "-m", "bot" -WorkingDirectory $proyecto -WindowStyle Minimized
        for ($i = 0; $i -lt 60 -and -not (BotVivo); $i++) { Start-Sleep -Milliseconds 500 }
        if (-not (BotVivo)) {
            Add-Type -AssemblyName PresentationFramework
            [System.Windows.MessageBox]::Show("El bot no ha arrancado. Mira el registro en M:\BotFundamental-datos\logs\bot.log", "Radar Fundamental") | Out-Null
            exit 1
        }
    }
}

# El vigilante del bot (lo relanza si cae) y el laboratorio vivo (scripts/lab-vivo.ps1, con su propio vigilante),
# sueltos con Win32_Process.Create: no dependen de esta ventana. Cada uno tiene su mutex: si ya está en marcha, el nuevo
# sale solo. Para pararlos: los archivos M:\BotFundamental-datos\parar-vigilante y lab_vivo\parar.
$sueltos = @("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$proyecto\scripts\vigilante-bot.ps1`"",
             "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$proyecto\scripts\lab-vivo.ps1`" -Suelto")
foreach ($orden in $sueltos) {
    if ($Prueba) { Write-Host "Arrancaría suelto (si ya está en marcha, sale solo): $orden" }
    else { Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $orden; CurrentDirectory = $proyecto } | Out-Null }
}

$navegador = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
               "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
               "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $navegador) { Start-Process "$($url)#$Principal"; exit 0 }   # sin Chrome ni Edge: el navegador por defecto

Add-Type -AssemblyName System.Windows.Forms
$pantallas = @([System.Windows.Forms.Screen]::AllScreens | Sort-Object @{ Expression = { -not $_.Primary } }, @{ Expression = { $_.Bounds.X } })

function AbrirVentana([string]$vista, $pantalla, [int]$n) {
    $a = $pantalla.WorkingArea
    $argumentos = @("--user-data-dir=$perfiles\ventana-$n", "--app=$($url)#$vista", "--window-position=$($a.X),$($a.Y)",
                    "--window-size=$($a.Width),$($a.Height)", "--no-first-run", "--no-default-browser-check")
    if ($PantallaCompleta) { $argumentos += "--start-fullscreen" }
    if ($Prueba) { Write-Host "Abriría «${vista}» en $($pantalla.DeviceName) ($($a.Width)x$($a.Height) en $($a.X),$($a.Y))" }
    else { Start-Process -FilePath $navegador -ArgumentList $argumentos; Start-Sleep -Milliseconds 700 }
}

AbrirVentana $Principal $pantallas[0] 1
if ($pantallas.Count -ge 2) { AbrirVentana $Secundario $pantallas[1] 2 }
elseif ($Prueba) { Write-Host "Solo hay un monitor: la vista «${Secundario}» se abre con el botón «2.ª pantalla» de la web." }
