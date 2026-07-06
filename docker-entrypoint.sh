#!/bin/bash
set -e

SEGMENTS_DIR="/app/brouter/segments4"
mkdir -p "$SEGMENTS_DIR"

# Auto-download rd5 segments for Italy (Adriatic coast + central) if not present
# Files sourced from http://brouter.de/brouter/segments4/
BASE_URL="http://brouter.de/brouter/segments4"

download_if_missing() {
    local file="$SEGMENTS_DIR/$1"
    if [ ! -f "$file" ]; then
        echo "[entrypoint] Downloading $1 ..."
        wget -q -O "$file" "$BASE_URL/$1" || { echo "[entrypoint] WARN: Failed to download $1"; rm -f "$file"; }
    fi
}

# E5/N43 — Senigallia, Ancona, Jesi area
download_if_missing "E5_N43.rd5"
# E5/N44 — Pesaro, Fano, Rimini area
download_if_missing "E5_N44.rd5"
# E4/N43 — Appennino, Perugia area
download_if_missing "E4_N43.rd5"
# E4/N44 — Bologna area
download_if_missing "E4_N44.rd5"

# ── BRouter: avvio in background ──────────────────────────────────────────────
BROUTER_PORT=17777
BROUTER_JAR="/app/brouter/brouter.jar"
BROUTER_CHECK="http://localhost:${BROUTER_PORT}/brouter?lonlats=13.22,43.71|13.50,43.60&profile=trekking&alternativeidx=0&format=gpx"

echo "[entrypoint] Avvio BRouter in background (porta ${BROUTER_PORT})..."
java -cp "${BROUTER_JAR}" btools.server.RouteServer \
    /app/brouter/segments4 \
    /app/brouter/profiles2 \
    /app/brouter/customprofiles \
    "${BROUTER_PORT}" \
    > /var/log/brouter.log 2>&1 &
BROUTER_PID=$!

echo "[entrypoint] BRouter PID=${BROUTER_PID} — attendo che sia pronto..."
BROUTER_READY=0
for i in $(seq 1 36); do
    # Controlla prima che il processo sia ancora in vita
    if ! kill -0 "${BROUTER_PID}" 2>/dev/null; then
        echo "[entrypoint] ERRORE: BRouter (PID=${BROUTER_PID}) è terminato inaspettatamente."
        echo "[entrypoint] --- log BRouter ---"
        cat /var/log/brouter.log || true
        echo "[entrypoint] -------------------"
        break
    fi
    if curl -sf --max-time 5 "${BROUTER_CHECK}" > /dev/null 2>&1; then
        echo "[entrypoint] BRouter pronto dopo $((i * 5))s."
        BROUTER_READY=1
        break
    fi
    echo "[entrypoint] Tentativo ${i}/36 — BRouter non ancora pronto, ritento tra 5s..."
    sleep 5
done

if [ "${BROUTER_READY}" -eq 0 ]; then
    echo "[entrypoint] WARN: BRouter non risponde su porta ${BROUTER_PORT} dopo 180s — Streamlit partirà comunque."
fi

# ── Ollama: pull modello se AI_PROVIDER=ollama ─────────────────────────────
if [ "${AI_PROVIDER}" = "ollama" ]; then
    OLLAMA_BASE="${OLLAMA_URL:-http://ollama:11434}"
    OLLAMA_MODEL="${AI_MODEL:-llama3}"

    echo "[entrypoint] AI_PROVIDER=ollama — attendo che Ollama sia pronto su ${OLLAMA_BASE}..."
    for i in $(seq 1 20); do
        if curl -sf "${OLLAMA_BASE}/api/tags" > /dev/null 2>&1; then
            echo "[entrypoint] Ollama pronto."
            break
        fi
        echo "[entrypoint] Tentativo ${i}/20 — Ollama non ancora pronto, ritento tra 3s..."
        sleep 3
    done

    if ! curl -sf "${OLLAMA_BASE}/api/tags" > /dev/null 2>&1; then
        echo "[entrypoint] WARN: Ollama non raggiungibile su ${OLLAMA_BASE} — salto il pull."
    else
        # Verifica se il modello è già presente
        if curl -sf "${OLLAMA_BASE}/api/tags" | grep -q "\"${OLLAMA_MODEL}\""; then
            echo "[entrypoint] Modello '${OLLAMA_MODEL}' già presente — nessun pull necessario."
        else
            echo "[entrypoint] Pull modello '${OLLAMA_MODEL}' da Ollama..."
            curl -sf -X POST "${OLLAMA_BASE}/api/pull" \
                -H "Content-Type: application/json" \
                -d "{\"name\":\"${OLLAMA_MODEL}\",\"stream\":false}" \
                | grep -o '"status":"[^"]*"' | tail -1 \
                || echo "[entrypoint] WARN: pull completato con errori — verifica manualmente."
            echo "[entrypoint] Pull '${OLLAMA_MODEL}' completato."
        fi
    fi
fi

exec "$@"
