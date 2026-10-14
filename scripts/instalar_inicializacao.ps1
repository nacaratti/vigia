# Registra o monitoramento no Agendador de Tarefas para iniciar quando você entrar no Windows.
# Reinicia sozinho se o programa cair. Rode no PowerShell (não precisa ser administrador):
#   powershell -ExecutionPolicy Bypass -File scripts\instalar_inicializacao.ps1
# Para remover:
#   Unregister-ScheduledTask -TaskName "Vigia - Monitoramento" -Confirm:$false

$raiz = Split-Path -Parent $PSScriptRoot
$python = Join-Path $raiz ".venv\Scripts\pythonw.exe"   # pythonw = sem janela de console
if (-not (Test-Path $python)) { throw "venv não encontrado em $python. Siga o README (instalação)." }

$acao = New-ScheduledTaskAction -Execute $python -Argument "main.py" -WorkingDirectory $raiz
$gatilho = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$gatilho.Delay = "PT30S"   # espera a rede subir
$config = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Vigia - Monitoramento" -Action $acao -Trigger $gatilho -Settings $config `
    -Description "Detecção de pessoas nas câmeras + painel em http://127.0.0.1:8000" -Force | Out-Null
Write-Host "Tarefa 'Vigia - Monitoramento' registrada. Ela inicia 30s após você entrar no Windows."
Write-Host "Para iniciar agora: Start-ScheduledTask -TaskName 'Vigia - Monitoramento'"
