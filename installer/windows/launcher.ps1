#Requires -Version 5.1
# GPX Route Builder — Windows launcher
# Eseguito dal collegamento sul Desktop / menu Start.

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$IMAGE     = "albertoocello/gpx-route-builder:latest"
$CONTAINER = "gpx-route-builder"
$APP_DATA  = Join-Path $env:APPDATA "gpx-route-builder"
$ENV_FILE  = Join-Path $APP_DATA ".env"
$DOCKER_EXE = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"

function Show-Error($msg) {
    [System.Windows.Forms.MessageBox]::Show($msg, "GPX Route Builder",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
}
function Show-Info($msg) {
    [System.Windows.Forms.MessageBox]::Show($msg, "GPX Route Builder",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
}

# ── 1. Docker Desktop installato? ─────────────────────────────────────────────
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    $r = [System.Windows.Forms.MessageBox]::Show(
        "Docker Desktop non è installato.`n`nVerrà aperta la pagina di download.`nInstalla Docker Desktop, poi riapri GPX Route Builder.",
        "GPX Route Builder",
        [System.Windows.Forms.MessageBoxButtons]::OKCancel,
        [System.Windows.Forms.MessageBoxIcon]::Information)
    if ($r -eq [System.Windows.Forms.DialogResult]::OK) {
        Start-Process "https://www.docker.com/products/docker-desktop/"
    }
    exit 0
}

# ── 2. Docker daemon in esecuzione? ───────────────────────────────────────────
$dockerOk = $false
try { & docker info 2>$null | Out-Null; $dockerOk = $true } catch {}

if (-not $dockerOk) {
    if (-not (Test-Path $DOCKER_EXE)) {
        Show-Error "Docker Desktop non trovato in:`n$DOCKER_EXE`n`nReinstalla Docker Desktop."
        exit 1
    }
    Write-Host "Avvio Docker Desktop..."
    Start-Process $DOCKER_EXE
    $elapsed = 0
    do {
        Start-Sleep 2; $elapsed += 2
        if ($elapsed -ge 120) {
            Show-Error "Docker Desktop non risponde dopo 2 minuti.`nAprilo manualmente e riprova."
            exit 1
        }
        $dockerOk = $false
        try { & docker info 2>$null | Out-Null; $dockerOk = $true } catch {}
    } until ($dockerOk)
    Write-Host "Docker pronto."
}

# ── 3. Prima configurazione: crea .env e chiedi chiave API ────────────────────
New-Item -ItemType Directory -Force -Path $APP_DATA | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $APP_DATA "routes") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $APP_DATA "data") | Out-Null

if (-not (Test-Path $ENV_FILE)) {
    # Form di configurazione
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "GPX Route Builder — Configurazione iniziale"
    $form.Size = New-Object System.Drawing.Size(440, 250)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false; $form.MinimizeBox = $false

    $lbl1 = New-Object System.Windows.Forms.Label
    $lbl1.Text = "Scegli il provider AI:"; $lbl1.Location = "20,20"; $lbl1.Size = "390,20"
    $form.Controls.Add($lbl1)

    $combo = New-Object System.Windows.Forms.ComboBox
    $combo.Location = "20,44"; $combo.Size = "390,25"; $combo.DropDownStyle = "DropDownList"
    @("Claude (Anthropic) — consigliato", "OpenAI", "Google Gemini", "Ollama (locale, gratuito)") |
        ForEach-Object { $combo.Items.Add($_) | Out-Null }
    $combo.SelectedIndex = 0
    $form.Controls.Add($combo)

    $lbl2 = New-Object System.Windows.Forms.Label
    $lbl2.Text = "Chiave API:"; $lbl2.Location = "20,90"; $lbl2.Size = "390,20"
    $form.Controls.Add($lbl2)

    $txt = New-Object System.Windows.Forms.TextBox
    $txt.Location = "20,112"; $txt.Size = "390,25"; $txt.UseSystemPasswordChar = $true
    $form.Controls.Add($txt)

    $lbl3 = New-Object System.Windows.Forms.Label
    $lbl3.Text = ""; $lbl3.Location = "20,142"; $lbl3.Size = "390,20"; $lbl3.ForeColor = [System.Drawing.Color]::Gray
    $form.Controls.Add($lbl3)

    $btn = New-Object System.Windows.Forms.Button
    $btn.Text = "Continua →"; $btn.Location = "300,175"; $btn.Size = "110,32"
    $btn.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.AcceptButton = $btn; $form.Controls.Add($btn)

    $hints = @{
        "Claude (Anthropic) — consigliato" = "console.anthropic.com → API Keys"
        "OpenAI"  = "platform.openai.com/api-keys"
        "Google Gemini" = "aistudio.google.com/app/apikey"
        "Ollama (locale, gratuito)" = "(nessuna chiave necessaria)"
    }
    $combo.Add_SelectedIndexChanged({
        $sel = $combo.SelectedItem.ToString()
        $lbl3.Text = $hints[$sel]
        $txt.Enabled = -not ($sel -like "*Ollama*")
        if ($sel -like "*Ollama*") { $lbl2.Text = "Chiave API: (non necessaria)" } else { $lbl2.Text = "Chiave API:" }
    })

    $r = $form.ShowDialog()
    if ($r -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }

    $sel = $combo.SelectedItem.ToString()
    $apiKey = $txt.Text.Trim()

    if ($sel -like "*Ollama*") {
        "AI_PROVIDER=ollama`nOLLAMA_URL=http://host.docker.internal:11434`nAI_MODEL=llama3.2" |
            Out-File $ENV_FILE -Encoding ASCII
    } else {
        if (-not $apiKey) { Show-Error "Chiave API non inserita."; exit 1 }
        switch -Wildcard ($sel) {
            "*Claude*" { $pcode = "claude"; $kname = "ANTHROPIC_API_KEY" }
            "*OpenAI*" { $pcode = "openai"; $kname = "OPENAI_API_KEY" }
            "*Gemini*" { $pcode = "gemini"; $kname = "GEMINI_API_KEY" }
        }
        "AI_PROVIDER=$pcode`n$kname=$apiKey" | Out-File $ENV_FILE -Encoding ASCII
    }
}

# ── 4. Pull immagine aggiornata ───────────────────────────────────────────────
Write-Host "Controllo aggiornamenti..."
& docker pull $IMAGE

# ── 5. Ciclo di vita del container ────────────────────────────────────────────
$running = & docker ps --format "{{.Names}}" 2>$null | Where-Object { $_ -eq $CONTAINER }
$exists  = & docker ps -a --format "{{.Names}}" 2>$null | Where-Object { $_ -eq $CONTAINER }

if ($running) {
    Write-Host "Container gia' in esecuzione."
} elseif ($exists) {
    & docker start $CONTAINER | Out-Null
    Write-Host "Container riavviato."
} else {
    Write-Host "Avvio container..."
    & docker run -d `
        --name $CONTAINER `
        -p 8501:8501 `
        -p 17777:17777 `
        --env-file $ENV_FILE `
        -e BROUTER_URL=http://localhost:17777 `
        -v "${APP_DATA}\routes:/app/routes" `
        -v "${APP_DATA}\data:/app/data" `
        -v "gpx_rb_segments4:/app/brouter/segments4" `
        --restart unless-stopped `
        $IMAGE | Out-Null
}

# ── 6. Polling e apertura browser ─────────────────────────────────────────────
Write-Host "Attendo che l'app sia pronta..."
$elapsed = 0
do {
    Start-Sleep 2; $elapsed += 2
    if ($elapsed -ge 120) {
        Show-Error "L'applicazione non risponde dopo 2 minuti.`nControlla Docker Desktop e riprova."
        exit 1
    }
    $ready = $false
    try {
        $r = Invoke-WebRequest "http://localhost:8501/_stcore/health" -UseBasicParsing -TimeoutSec 2
        $ready = ($r.StatusCode -eq 200)
    } catch {}
} until ($ready)

Start-Sleep 1
Start-Process "http://localhost:8501"
Write-Host "Browser aperto. L'app e' pronta."
