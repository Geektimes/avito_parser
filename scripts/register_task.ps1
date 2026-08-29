# Регистрация ежечасной задачи AvitoMonitor в планировщике Windows.
#
#   .\scripts\register_task.ps1                # создать/пересоздать задачу
#   .\scripts\register_task.ps1 -Remove        # удалить
#   .\scripts\register_task.ps1 -IntervalHours 2
#
param(
    [switch]$Remove,
    [string]$TaskName = "AvitoMonitor",
    [int]$IntervalHours = 1
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Wrapper = Join-Path $ProjectRoot "run_monitor.ps1"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Задача '$TaskName' удалена."
    exit 0
}

if (-not (Test-Path $Wrapper)) {
    throw "Не найден $Wrapper — сначала создайте обёртку."
}
if (-not (Test-Path (Join-Path $ProjectRoot ".venv\Scripts\python.exe"))) {
    throw "Нет venv в $ProjectRoot\.venv — установите зависимости."
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Wrapper`""

# Первая цель через 2 минуты, далее повтор каждые IntervalHours часов бессрочно.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger `
    -Description "Ежечасный мониторинг новых объявлений Avito (SQLite)" `
    -Force | Out-Null

Write-Host "Задача '$TaskName' зарегистрирована: каждый $IntervalHours ч, старт через ~2 мин."
Write-Host "Лог: $ProjectRoot\logs\avito.log. Удалить: .\scripts\register_task.ps1 -Remove"
