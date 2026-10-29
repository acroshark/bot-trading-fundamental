# Arranque y vigilante del laboratorio vivo (docs/07, apartado 16). Nunca toca el bot (otro proceso, otro vigilante).
# Cada minuto mira si el corredor está vivo: el PID de corredor.lock es DE VERDAD el corredor (su línea de órdenes lleva
# «bot.lab_vivo correr» y se creó cuando dice el lock) y estado.json tiene un latido de hace menos de 3 min. Con 2 fallos
# seguidos y sin corredor, lo arranca suelto con Win32_Process.Create (como el bot: no depende de esta ventana ni de
# ninguna sesión); si el corredor vive pero el latido tiene más de 10 min (colgado), lo cierra con sus hijos y lo vuelve
# a arrancar. Un lock viejo (el corredor murió sin soltarlo) cuyo PID ha reutilizado Windows para otro proceso no se
# toca NUNCA: ni se mata ni se le cambia la prioridad (podría ser el bot, llama-server o explorer). El corredor se pone él
# mismo prioridad baja, afinidad a 3 núcleos, su Job Object con el límite de memoria, los temporales en lab_vivo\tmp y
# los hilos de BLAS en 1.
#
# Uso (oculto):
#   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File scripts\lab-vivo.ps1 [-Procesos 2] [-Cpus "3,4,5"] [-MemoriaMB 2500] [-Prueba]
#   -Suelto: relanza este vigilante suelto (Win32_Process.Create) y sale (para lanzarlo desde una consola o una tarea).
#   -Prueba: dice qué haría (una vuelta), sin arrancar ni parar nada.
#   -Lab: otra carpeta del laboratorio (solo para las pruebas; por defecto M:\BotFundamental-datos\lab_vivo).
# Para pararlo todo: crea el archivo M:\BotFundamental-datos\lab_vivo\parar (el corredor sale solo; el vigilante lo
# espera hasta 2 min, lo cierra si sigue y termina). Registro en lab_vivo\registro\vigilante.log.
param([int]$Procesos = 2, [string]$Cpus = "3,4,5", [int]$MemoriaMB = 2500, [switch]$Prueba, [switch]$Suelto,
      [string]$Lab = "M:\BotFundamental-datos\lab_vivo")

$ErrorActionPreference = "Continue"
$proyecto = Split-Path -Parent $PSScriptRoot
$venv = "M:\BotFundamental-runtime\venv\Scripts"
$python = Join-Path $venv "pythonw.exe"
if (-not (Test-Path $python)) { $python = Join-Path $venv "python.exe" }
$porDefecto = "M:\BotFundamental-datos\lab_vivo"
$lab = $Lab
$registro = Join-Path $lab "registro\vigilante.log"
$parar = Join-Path $lab "parar"
$lock = Join-Path $lab "corredor.lock"
$estado = Join-Path $lab "estado.json"
$ORDEN = 'bot\.lab_vivo\s+correr'      # lo que lleva la línea de órdenes del corredor (corredor.ORDEN)
$TOLERANCIA_MS = 2000                  # recursos.TOLERANCIA_INICIO_MS

function Apunta([string]$texto) {
    $linea = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $texto"
    if ($Prueba) { Write-Host $linea; return }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $registro) | Out-Null
    $linea | Out-File -FilePath $registro -Append -Encoding utf8
}

function Suelto([string]$linea, [string]$carpeta) {
    # Proceso suelto, fuera de esta sesión y de su Job Object (como el bot)
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $linea; CurrentDirectory = $carpeta }
    if ($r.ReturnValue -ne 0) { Apunta "Win32_Process.Create falló (código $($r.ReturnValue)): $linea"; return $null }
    return [int]$r.ProcessId
}

if ($Suelto) {
    $args2 = "-Procesos $Procesos -Cpus `"$Cpus`" -MemoriaMB $MemoriaMB"
    if ($lab -ne $porDefecto) { $args2 += " -Lab `"$lab`"" }
    $linea = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" $args2"
    if ($Prueba) { Write-Host "Lanzaría suelto: $linea"; exit 0 }
    $p = Suelto $linea $proyecto
    Write-Host "Vigilante del laboratorio vivo lanzado suelto (PID $p)"
    exit 0
}

$nombreMutex = "Global\RadarLabVivoVigilante"
if ($lab -ne $porDefecto) { $nombreMutex += "-" + [Math]::Abs($lab.ToLowerInvariant().GetHashCode()) }
$unico = New-Object System.Threading.Mutex($false, $nombreMutex)
if (-not $unico.WaitOne(0)) { if ($Prueba) { Write-Host "Ya hay otro vigilante del laboratorio" }; exit 0 }

function LeerLock {
    try { return (Get-Content $lock -Raw -Encoding utf8 -ErrorAction Stop | ConvertFrom-Json) } catch { return $null }
}

function CorredorDelLock([object]$l) {
    # El proceso del lock SOLO si es de verdad el corredor: el PID vive, su línea de órdenes lleva «bot.lab_vivo
    # correr» y se creó cuando dice el lock («creado», ± 2 s; un lock antiguo sin «creado»: antes de su «ts»).
    if (-not $l -or -not $l.pid) { return $null }
    $id = 0
    if (-not [int]::TryParse([string]$l.pid, [ref]$id) -or $id -le 0) { return $null }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    if ([string]$p.CommandLine -notmatch $ORDEN) { return $null }
    try { $creado = ([DateTimeOffset]$p.CreationDate).ToUnixTimeMilliseconds() } catch { return $null }
    if ($l.creado) { if ([math]::Abs($creado - [int64]$l.creado) -gt $TOLERANCIA_MS) { return $null } }
    elseif ($l.ts) { if ($creado -gt [int64]$l.ts + $TOLERANCIA_MS) { return $null } }
    return $p
}

function AvisaLockViejo([object]$l) {
    # Un lock cuyo PID vive pero NO es el corredor: no se mata ni se toca (lo reemplaza el corredor al arrancar)
    if ($l -and $l.pid -and (Get-Process -Id ([int]$l.pid) -ErrorAction SilentlyContinue)) {
        Apunta "corredor.lock viejo: el PID $($l.pid) es ahora de otro proceso (no se toca)"
    }
}

function EdadLatidoS {
    try {
        $e = Get-Content $estado -Raw -Encoding utf8 -ErrorAction Stop | ConvertFrom-Json
        $ahora = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        return [int](($ahora - [int64]$e.latido) / 1000)
    } catch { return 99999 }
}

function Cerrar([object]$c, [string]$por) {
    # Solo un corredor verificado (CorredorDelLock), con sus tareas (/T)
    if (-not $c) { return }
    if ($Prueba) { Apunta "Cerraría el corredor ($por, PID $($c.ProcessId))"; return }
    taskkill /PID $c.ProcessId /T /F | Out-Null
    Apunta "Corredor cerrado ($por, PID $($c.ProcessId))"
}

function Arrancar {
    $linea = "`"$python`" -X utf8 -m bot.lab_vivo correr --procesos $Procesos --cpus $Cpus --memoria-mb $MemoriaMB"
    if ($Prueba) { Apunta "Arrancaría el corredor: $linea (en $proyecto)"; return }
    $id = Suelto $linea $proyecto
    if ($id) {
        Start-Sleep -Seconds 5
        try {   # el corredor también se las pone él mismo (y las hereda su Job Object)
            $mask = 0; foreach ($cc in ($Cpus -split ",")) { if ($cc -match '^\d+$' -and [int]$cc -lt [Environment]::ProcessorCount) { $mask = $mask -bor (1 -shl [int]$cc) } }
            $ids = @($id)                                  # el lanzador que acabamos de crear es nuestro
            $c = CorredorDelLock (LeerLock)                # y el del lock, solo si es de verdad el corredor
            if ($c) { $ids += [int]$c.ProcessId }
            foreach ($pp in $ids) {
                $proc = Get-Process -Id $pp -ErrorAction SilentlyContinue
                if ($proc) { $proc.PriorityClass = 'BelowNormal'; if ($mask) { $proc.ProcessorAffinity = [IntPtr]$mask } }
            }
        } catch { }
        Apunta "Corredor arrancado (PID $id)"
    }
}

Apunta "Vigilante del laboratorio vivo en marcha (PID $PID)$(if ($Prueba) { ' · prueba: no arranca ni para nada' })"
$fallos = 0
while ($true) {
    if (Test-Path $parar) {
        Apunta "Existe $parar`: espero hasta 2 min a que el corredor salga"
        $l = LeerLock
        $c = CorredorDelLock $l
        for ($i = 0; $i -lt 24 -and $c -and -not $Prueba; $i++) { Start-Sleep -Seconds 5; $c = CorredorDelLock $l }
        Cerrar $c "no salió solo con el archivo parar"
        Apunta "Vigilante parado"
        break
    }
    $l = LeerLock
    $c = CorredorDelLock $l
    $edad = EdadLatidoS
    if ($c -and $edad -lt 180) { $fallos = 0 }
    elseif ($c -and $edad -gt 600) {
        Apunta "Corredor colgado (PID $($c.ProcessId), latido de hace $edad s): se cierra con sus tareas y se arranca de nuevo"
        Cerrar $c "colgado"
        if (-not $Prueba) { Start-Sleep -Seconds 3 }
        Arrancar
        $fallos = 0
    }
    elseif (-not $c) {
        AvisaLockViejo $l
        $fallos++
        if ($fallos -ge 2 -or $Prueba) { Apunta "El corredor no está en marcha: se arranca"; Arrancar; $fallos = 0 }
    }
    if ($Prueba) { break }
    Start-Sleep -Seconds 60
}
