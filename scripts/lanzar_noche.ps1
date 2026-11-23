# Noche desatendida en UN paso (DVF, 2026-11-22, sesión 67).
#
#   1. Lanza la corrida con lanzar_corrida.ps1 -Desacoplado (mismos parámetros).
#   2. Lanza scripts\evaluar_noche.py DESACOPLADO (WMI, fuera del árbol de la app): mantiene la PC
#      sin suspenderse hasta que terminen el entrenamiento Y la evaluación, y deja el informe en
#      models\runs\<Run>\chequeos\noche_<fecha>.md.
#   -Tope: hora en que se libera el pedido de no suspender pase lo que pase (default: -Hasta + 3 h).
#
# Ojo: no evita la suspensión al CERRAR LA TAPA. Dejarla abierta y enchufada.
#
# Uso:
#   powershell -ExecutionPolicy Bypass -File scripts\lanzar_noche.ps1 -Run corrida7_diego -InitFrom models\inicio_corrida7 -Workers 6 -StrikerEntropy 0.012 -KeeperEntropy 0.01 -Lr 1e-4 -Hasta 2026-11-23T08:00 -RefStriker models\runs\corrida6_diego\chequeos\20261121_0911_striker.npz -RefKeeper models\runs\corrida6_diego\chequeos\20261121_0911_keeper.npz

param([Parameter(Mandatory = $true)][string]$Run,
	[Parameter(Mandatory = $true)][string]$InitFrom,
	[int]$Workers = 6, [string]$StrikerEntropy = "0.015", [string]$KeeperEntropy = "0.01",
	[string]$Lr = "1e-4",
	[Parameter(Mandatory = $true)][string]$Hasta,
	[string]$RefStriker = "", [string]$RefKeeper = "",
	[string]$Tope = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\lanzar_corrida.ps1" -Run $Run -InitFrom $InitFrom -Workers $Workers `
	-StrikerEntropy $StrikerEntropy -KeeperEntropy $KeeperEntropy -Lr $Lr -Hasta $Hasta -Desacoplado
if ($LASTEXITCODE -ne 0) { throw "lanzar_corrida.ps1 falló: no lanzo la evaluación." }
$supPid = (Get-Content "models\runs\_salidas\$($Run)_supervisor_pid.txt" -Raw).Trim()

if (-not $Tope) { $Tope = ([datetime]::Parse($Hasta)).AddHours(3).ToString("yyyy-MM-ddTHH:mm") }
$py = (Resolve-Path "env\Scripts\python.exe").Path
$argv = @("scripts\evaluar_noche.py", "--run", $Run, "--esperar-pid", $supPid, "--tope", $Tope)
if ($RefStriker -and $RefKeeper) { $argv += @("--ref-striker", "`"$RefStriker`"", "--ref-keeper", "`"$RefKeeper`"") }
$out = Join-Path $root "models\runs\_salidas\$($Run)_evaluar_noche.txt"
$cmdLine = "cmd.exe /c `"`"$py`" $($argv -join ' ') > `"$out`" 2>&1`""
# PriorityClass 16384 = BelowNormal; ShowWindow 0 = oculto
$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ PriorityClass = [uint32]16384; ShowWindow = [uint16]0 }
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine; CurrentDirectory = $root; ProcessStartupInformation = $si }
if ($r.ReturnValue -ne 0) { throw "No pude lanzar evaluar_noche.py (código $($r.ReturnValue)). La corrida SÍ quedó lanzada." }
"Evaluación de la noche esperando al supervisor $supPid (no suspender hasta que termine, tope $Tope). Log: models\runs\$Run\evaluar_noche.log"
