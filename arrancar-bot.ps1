# Arranca el bot y abre la web en el navegador. Los datos se guardan en M:\BotFundamental-datos.
# Para pararlo: Ctrl+C en esta ventana.
$py = "M:\BotFundamental-runtime\venv\Scripts\python.exe"
$bot = Start-Process -FilePath $py -ArgumentList "-X", "utf8", "-m", "bot" -WorkingDirectory $PSScriptRoot -PassThru -NoNewWindow
Start-Sleep -Seconds 3
Start-Process "http://127.0.0.1:8765"
Wait-Process -Id $bot.Id
