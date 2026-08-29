# Обёртка для запуска мониторинга из планировщика Windows:
# задаёт рабочую директорию (планировщик её не гарантирует) и запускает один проход.
Set-Location -LiteralPath $PSScriptRoot
& ".\.venv\Scripts\python.exe" -X utf8 app\main.py --once --jitter 240 --cookies-file cookies.txt
