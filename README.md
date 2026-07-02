# GPX Route Builder (by A. Ocello)

Pianifica i tuoi percorsi ciclistici con l'aiuto dell'AI. Inserisci distanza, tipo di terreno e luoghi preferiti — il sistema genera tre percorsi GPX pronti da caricare su Garmin, Strava o Komoot.

---

## Cosa fa

- Pianifica percorsi ciclistici in base alle tue preferenze (distanza, dislivello, tipo di fondo, luoghi)
- Genera tre varianti GPX con scoring automatico
- Confronta il percorso pianificato con quello reale dopo l'uscita
- Impara dalle tue uscite e migliora i percorsi nel tempo

---

## Requisiti

- **Docker Desktop**
  - Mac: [scarica qui](https://www.docker.com/products/docker-desktop/)
  - Windows: [scarica qui](https://www.docker.com/products/docker-desktop/)

    > **Solo Windows — prima di installare Docker, installa WSL2:**
    >
    > WSL2 è un componente di Windows che Docker richiede per funzionare. Per installarlo:
    >
    > 1. Premi il tasto **Start**, digita `PowerShell`, poi fai clic destro sull'icona che appare e scegli **"Esegui come amministratore"**.
    > 2. Se Windows ti chiede "Vuoi consentire a questa app di apportare modifiche?", clicca **Sì**.
    > 3. Nella finestra nera che si apre, incolla questo comando e premi **Invio**:
    >    ```
    >    wsl --install
    >    ```
    > 4. Aspetta che finisca (ci vogliono un paio di minuti), poi **riavvia il PC**.
    >
    > Dopo il riavvio puoi installare Docker Desktop normalmente.
- Una chiave API di uno di questi provider AI:
  - [Anthropic Claude](https://console.anthropic.com) (consigliato)
  - [OpenAI](https://platform.openai.com)
  - [Google Gemini](https://aistudio.google.com)
  - oppure [Ollama](https://ollama.ai) in locale (gratuito, nessuna chiave necessaria)

---

## Installazione

### 1. Scarica il progetto

```bash
git clone https://github.com/AlbertoOcello/gpx-route-builder.git
cd gpx-route-builder
```

### 2. Configura la chiave API

Copia il file di esempio e inserisci la tua chiave:

```bash
cp .env.example .env
```

Apri `.env` con un editor di testo e modifica:

```
# Scegli il provider: claude | openai | gemini | ollama
AI_PROVIDER=claude
AI_MODEL=claude-sonnet-4-6

# Inserisci la chiave del provider scelto
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# GEMINI_API_KEY=...

# Solo per Ollama (nessuna chiave necessaria)
# AI_PROVIDER=ollama
# OLLAMA_URL=http://localhost:11434
# AI_MODEL=llama3.2
```

### 3. Avvia

```bash
docker compose up
```

Al primo avvio scarica automaticamente le mappe OSM della zona (può richiedere qualche minuto).

### 4. Apri il browser

```
http://localhost:8501
```

---

## Aggiornamenti

```bash
git pull
docker compose up --build
```

---

## Struttura del menu

| Tab | Cosa fa |
|---|---|
| **Planner** | Definisci waypoint e tema del percorso con l'AI |
| **Geolocalizza** | Cerca coordinate di un luogo o clicca sulla mappa |
| **Builder** | Genera i 3 GPX reali con scoring |
| **Analizza & Feedback** | Confronta pianificato vs reale, aggiungi note |
| **Debug** | Ispeziona prompt, score e ostacoli noti |

---

## Note

- I file GPX generati sono compatibili con Garmin, Komoot, Strava e qualsiasi app che legge il formato standard GPX.
- Il database e le preferenze utente sono salvati localmente sul tuo computer, niente viene inviato a server esterni (eccetto le chiamate al provider AI scelto).
- Per cambiare zona geografica (default: Marche, Italia) modifica `BROUTER_TILE` in `.env`.
