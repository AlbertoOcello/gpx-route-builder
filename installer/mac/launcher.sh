#!/bin/bash
# GPX Route Builder — launcher interno al .app bundle
# Eseguito da macOS quando l'utente apre l'app.

set -uo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

IMAGE="albertoocello/gpx-route-builder:latest"
CONTAINER="gpx-route-builder"
APP_DATA="$HOME/.gpx-route-builder"
ENV_FILE="$APP_DATA/.env"

notify() {
    osascript -e "display notification \"$1\" with title \"GPX Route Builder\"" 2>/dev/null || true
}
error_dialog() {
    osascript -e "display dialog \"$1\" buttons {\"OK\"} default button 1 with icon stop with title \"GPX Route Builder\"" 2>/dev/null || true
}

# ── 1. Docker Desktop installato? ─────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    osascript -e 'display dialog "Docker Desktop non è installato.\n\nVerrà aperta la pagina di download.\nInstalla Docker Desktop, poi riapri GPX Route Builder." buttons {"Apri sito", "Annulla"} default button 1 with title "GPX Route Builder"' 2>/dev/null | grep -q "Apri" && open "https://www.docker.com/products/docker-desktop/"
    exit 0
fi

# ── 2. Docker daemon in esecuzione? ───────────────────────────────────────────
if ! docker info >/dev/null 2>&1; then
    notify "Avvio Docker Desktop..."
    open -a Docker
    ELAPSED=0
    until docker info >/dev/null 2>&1; do
        sleep 2; ELAPSED=$((ELAPSED+2))
        if [ "$ELAPSED" -ge 120 ]; then
            error_dialog "Docker Desktop non risponde dopo 2 minuti.\nAprilo manualmente e riprova."
            exit 1
        fi
    done
fi

# ── 3. Prima configurazione: crea .env e chiedi chiave API ────────────────────
mkdir -p "$APP_DATA/routes" "$APP_DATA/data"

if [ ! -f "$ENV_FILE" ]; then
    PROVIDER=$(osascript 2>/dev/null <<'APPLE'
set ch to choose from list {"Claude (Anthropic) — consigliato", "OpenAI", "Google Gemini", "Ollama (locale, gratuito)"} \
    with prompt "Scegli il provider AI:" default items {"Claude (Anthropic) — consigliato"} \
    without multiple selections allowed and empty selection allowed
if ch is false then error "annullato"
return item 1 of ch
APPLE
    ) || exit 0

    if echo "$PROVIDER" | grep -q "Ollama"; then
        printf 'AI_PROVIDER=ollama\nOLLAMA_URL=http://host.docker.internal:11434\nAI_MODEL=llama3.2\n' > "$ENV_FILE"
        osascript -e 'display dialog "Configurato per Ollama.\n\nAssicurati che Ollama sia avviato prima di usare l'\''app." buttons {"OK"} default button 1 with title "GPX Route Builder"' 2>/dev/null || true
    else
        case "$PROVIDER" in
            *Claude*) KEY_ENV="ANTHROPIC_API_KEY"; HINT="sk-ant-..."; PCODE="claude" ;;
            *OpenAI*) KEY_ENV="OPENAI_API_KEY";    HINT="sk-...";     PCODE="openai" ;;
            *Gemini*) KEY_ENV="GEMINI_API_KEY";    HINT="AIza...";    PCODE="gemini" ;;
        esac
        API_KEY=$(osascript 2>/dev/null <<APPLE
set k to text returned of (display dialog "Inserisci la chiave API ${KEY_ENV}:\n\nLa trovi nella console del provider." default answer "${HINT}" with hidden answer buttons {"Annulla", "OK"} default button "OK" with title "GPX Route Builder")
if k is "${HINT}" or k is "" then error "vuota"
return k
APPLE
        ) || { rm -f "$ENV_FILE"; exit 0; }
        printf 'AI_PROVIDER=%s\n%s=%s\n' "$PCODE" "$KEY_ENV" "$API_KEY" > "$ENV_FILE"
    fi
fi

# ── 4. Pull immagine aggiornata ───────────────────────────────────────────────
notify "Controllo aggiornamenti..."
docker pull "$IMAGE" 2>&1 | tail -3

# ── 5. Ciclo di vita del container ────────────────────────────────────────────
if docker ps --format "{{.Names}}" 2>/dev/null | grep -q "^${CONTAINER}$"; then
    : # già in esecuzione
elif docker ps -a --format "{{.Names}}" 2>/dev/null | grep -q "^${CONTAINER}$"; then
    docker start "$CONTAINER" >/dev/null
else
    docker run -d \
        --name "$CONTAINER" \
        -p 8501:8501 \
        -p 17777:17777 \
        --env-file "$ENV_FILE" \
        -e BROUTER_URL=http://localhost:17777 \
        -v "$APP_DATA/routes:/app/routes" \
        -v "$APP_DATA/data:/app/data" \
        -v "gpx_rb_segments4:/app/brouter/segments4" \
        --restart unless-stopped \
        "$IMAGE" >/dev/null
fi

# ── 6. Polling e apertura browser ─────────────────────────────────────────────
notify "Avvio in corso..."
ELAPSED=0
until curl -sf http://localhost:8501/_stcore/health >/dev/null 2>&1; do
    sleep 2; ELAPSED=$((ELAPSED+2))
    if [ "$ELAPSED" -ge 120 ]; then
        error_dialog "L'applicazione non risponde dopo 2 minuti.\nControlla Docker Desktop e riprova."
        exit 1
    fi
done
sleep 1
open http://localhost:8501
notify "Pronto — browser aperto."
