# Arranque de la API local que consumen los tableros del portafolio.
#
#   powershell -ExecutionPolicy Bypass -File backend\iniciar-api.ps1
#   powershell -ExecutionPolicy Bypass -File backend\iniciar-api.ps1 -Puerto 8010 -Origenes http://127.0.0.1:8000,http://localhost:8000
#
# Por qué existe: el tablero «Atlas vial» habla con http://127.0.0.1:8010; si el
# backend no está en marcha, la interfaz muestra «No se pudo contactar con la API
# del Atlas vial». Este guion levanta el servidor, espera a que responda, comprueba
# que el origen del portafolio está permitido por CORS y deja los registros a la
# vista. Al detenerlo (Ctrl+C) también se detiene el servidor que arrancó.

param(
    [int]$Puerto = 8010,
    [string]$Origenes = 'http://127.0.0.1:8000,http://localhost:8000',
    [int]$EsperaSegundos = 90
)

$ErrorActionPreference = 'Stop'
$backend = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $backend '.venv\Scripts\python.exe'
$salud = "http://127.0.0.1:$Puerto/api/v1/health/ready"
$api = "http://127.0.0.1:$Puerto/api/v1/atlas-vial/estado"

if (-not (Test-Path $python)) {
    throw "No se encontró el entorno virtual en $python. Crea el entorno con: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt"
}

function Test-Puerto {
    param([int]$Numero)
    try {
        $tcp = New-Object Net.Sockets.TcpClient
        $tcp.Connect('127.0.0.1', $Numero)
        $tcp.Close()
        return $true
    } catch {
        return $false
    }
}

function Test-Salud {
    try {
        $respuesta = Invoke-WebRequest -Uri $salud -UseBasicParsing -TimeoutSec 3
        return $respuesta.StatusCode -eq 200
    } catch {
        return $false
    }
}

# ¿Ya está en marcha? No se levanta una segunda instancia: se informa y se sale.
if (Test-Puerto -Numero $Puerto) {
    if (Test-Salud) {
        Write-Host "La API ya estaba en marcha en http://127.0.0.1:$Puerto (no se arranca otra copia)."
        exit 0
    }
    throw "El puerto $Puerto está ocupado por otro proceso que no responde a $salud. Ciérralo o usa -Puerto con otro número."
}

Write-Host "Levantando la API en http://127.0.0.1:$Puerto …"
$proceso = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', $Puerto) `
    -WorkingDirectory $backend -NoNewWindow -PassThru

try {
    $limite = (Get-Date).AddSeconds($EsperaSegundos)
    $listo = $false
    while ((Get-Date) -lt $limite) {
        if ($proceso.HasExited) { break }
        if (Test-Salud) { $listo = $true; break }
        Start-Sleep -Milliseconds 600
    }

    if (-not $listo) {
        throw "La API no respondió en $EsperaSegundos s. Revisa el mensaje de uvicorn más arriba (MongoDB, credenciales o módulos)."
    }

    $estado = Invoke-RestMethod -Uri $salud -TimeoutSec 5
    Write-Host "API lista: $($estado.status) · mongo: $($estado.mongo) · versión: $($estado.version)"

    # El error que motivó este guion mencionaba CORS: se comprueba de verdad, con
    # el origen real del portafolio, en lugar de suponerlo.
    foreach ($origen in ($Origenes -split ',')) {
        $origen = $origen.Trim()
        if (-not $origen) { continue }
        try {
            $r = Invoke-WebRequest -Uri $api -UseBasicParsing -TimeoutSec 10 -Headers @{ Origin = $origen }
            $permitido = $r.Headers['Access-Control-Allow-Origin']
            if ($permitido -eq $origen) {
                Write-Host "CORS correcto para $origen"
            } else {
                Write-Warning "CORS: el origen $origen no aparece en Access-Control-Allow-Origin (ALLOWED_ORIGINS en .env)."
            }
        } catch {
            Write-Warning "CORS: no se pudo comprobar el origen $origen ($($_.Exception.Message))."
        }
    }

    Write-Host "Documentación interactiva: http://127.0.0.1:$Puerto/api/docs"
    Write-Host "Tablero: http://127.0.0.1:8000/#/area/dashboards/atlas-vial"
    Write-Host "Ctrl+C detiene la API."
    Wait-Process -Id $proceso.Id
} finally {
    if (-not $proceso.HasExited) {
        Stop-Process -Id $proceso.Id -Force
    }
    Write-Host "API detenida."
}
