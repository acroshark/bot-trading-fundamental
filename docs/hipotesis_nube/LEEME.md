# Hipótesis de la nube (02-11-2026)

Cuatro hipótesis del coordinador para el laboratorio vivo, escritas en la nube **sin datos**: no se ha medido ni contado
nada. Pasan la admisión sin datos (A1-A7 y A10); A8 (recorte con datos reales) y A9 (frecuencia) los hace el corredor.
El porqué de cada una está en su JSON (`logica`) y en `docs/07-laboratorio-vivo.md`, apartado 22.

| Archivo | Familia nueva | Idea |
|---|---|---|
| `h-20261102-1100-ruptura_fallida-barrida.json` | `ruptura_fallida` | La barrida de un mínimo o máximo viejo de 48 h que vuelve dentro en ≤ 3 h, a la contra (Turtle Soup) |
| `h-20261102-1100-tendencia_retroceso-grial.json` | `tendencia_retroceso` | El primer retroceso a la EMA20 con ADX ≥ 30 en 4 h y su reanudación (Holy Grail) |
| `h-20261102-1100-extremo_noticias-sin.json` | `extremo_noticias` | Un movimiento extremo de 1 h sin noticias de la moneda en 6 h, a la contra (Chan, 2003) |
| `h-20261102-1100-momento_continuo-rana.json` | `momento_continuo` | La tendencia de 28 días hecha de días pequeños y seguidos (Frog in the Pan) |

## Cómo se copian al buzón (PC, PowerShell)

Primero, el PR fusionado en main y el proyecto actualizado en el PC: el corredor usa ese código (las familias nuevas se
cargan solas y el canario se repite porque cambia la firma del código).

Las tres de velas (validar con datos y, si no hay A8, al buzón escribiendo `.tmp` y renombrando):

```powershell
Set-Location "M:\OneDrive\Bot de trading fundamental"
$py = "M:\BotFundamental-runtime\venv\Scripts\python.exe"
$cola = "M:\BotFundamental-datos\lab_vivo\cola"
foreach ($id in "h-20261102-1100-ruptura_fallida-barrida", "h-20261102-1100-tendencia_retroceso-grial", "h-20261102-1100-momento_continuo-rana") {
    $sal = & $py -X utf8 -m bot.lab_vivo validar "docs\hipotesis_nube\$id.json" | Out-String
    Write-Host $sal
    if ($sal -match "A8:") { Write-Host "$id NO se copia: A8 (mira al futuro)"; continue }
    Copy-Item "docs\hipotesis_nube\$id.json" "$cola\entrada\$id.json.tmp"
    Move-Item -Force "$cola\entrada\$id.json.tmp" "$cola\entrada\$id.json"
}
```

- NO VALE solo por **A9** en alguna variante (pocas señales): se copia igual; el corredor quita esas variantes sin
  contarlas en el N y mide las demás.
- En una pausa de las aperturas, `validar` no calcula A8 ni A9 (dice que los hará el corredor): se puede copiar igual.

La de noticias espera con las otras de noticias hasta que el archivo de noticias esté compilado:

```powershell
New-Item -ItemType Directory -Force "M:\BotFundamental-datos\lab_vivo\cola\esperando_datos\noticias" | Out-Null
Copy-Item "M:\OneDrive\Bot de trading fundamental\docs\hipotesis_nube\h-20261102-1100-extremo_noticias-sin.json" "M:\BotFundamental-datos\lab_vivo\cola\esperando_datos\noticias\"
```

Cuando `validar` ya no diga NO VALE por A6 (las noticias compiladas), se pasa a `cola\entrada\` igual que las otras.
Si entrara antes, el corredor la rechazaría entera y su id ya no se podría usar.
