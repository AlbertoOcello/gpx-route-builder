#!/bin/bash
# GPX Route Builder — avvio rapido (Mac)
# Doppio clic da Finder per avviare Docker e aprire l'app nel browser.

# Porta nella cartella dello script (richiesto per doppio clic da Finder)
cd "$(dirname "$0")"

echo "======================================"
echo "  GPX Route Builder"
echo "======================================"
echo ""

# ── 1. Docker Desktop ─────────────────────────────────────────────────────────

if ! docker info >/dev/null 2>&1; then
    if [ ! -d "/Applications/Docker.app" ]; then
        echo "ERRORE: Docker Desktop non trovato."
        echo "Scaricalo da: https://www.docker.com/products/docker-desktop/"
        echo ""
        read -rp "Premi Invio per chiudere..."
        exit 1
    fi

    echo "Avvio Docker Desktop..."
    open -a Docker

    echo "Attendo che Docker sia pronto"
    ELAPSED=0
    until docker info >/dev/null 2>&1; do
        sleep 3
        ELAPSED=$((ELAPSED + 3))
        printf "  %ds..." "$ELAPSED"
        if [ "$ELAPSED" -ge 120 ]; then
            echo ""
            echo ""
            echo "ERRORE: Docker non risponde dopo 2 minuti."
            echo "Apri Docker Desktop manualmente e riprova."
            read -rp "Premi Invio per chiudere..."
            exit 1
        fi
    done
    echo ""
    echo "Docker pronto."
fi

echo ""
echo "Controllo aggiornamenti..."
docker compose pull

echo ""
echo "Avvio GPX Route Builder..."
echo "(il browser si apre automaticamente non appena l'app e' pronta)"
echo "Per fermare l'applicazione: premi Ctrl+C in questa finestra."
echo ""

# ── 2. Apri il browser quando Streamlit risponde ──────────────────────────────

(
    until curl -sf http://localhost:8501/_stcore/health >/dev/null 2>&1; do
        sleep 2
    done
    sleep 1
    open http://localhost:8501
) &

# ── 3. docker compose up (foreground — i log restano visibili) ────────────────

docker compose up
