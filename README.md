🇮🇹 [Italiano](README.md) | 🇬🇧 [English](README_EN.md)

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

### Passo 1 — Assicurati che Docker Desktop sia avviato

Apri Docker Desktop dal menu Applicazioni (Mac) o dal Desktop (Windows) e aspetta che l'icona smetta di animarsi. Docker deve essere in esecuzione prima di procedere.

---

### Passo 2 — Crea la cartella del progetto

Crea una nuova cartella vuota sul tuo computer dove vuoi tenere l'applicazione, ad esempio `gpx-route-builder` sul Desktop.

**Apri il terminale in quella cartella:**

- **Mac**: apri il Finder, naviga nella cartella, poi tasto destro → **"Servizi" → "Nuovo terminale nella cartella"**
  _(oppure apri Terminale e trascina la cartella nella finestra)_
- **Windows**: apri Esplora File, naviga nella cartella, poi clicca sulla barra degli indirizzi in alto, digita `cmd` e premi **Invio**

---

### Passo 3 — Crea il file `docker-compose.yml`

Nella cartella appena creata, crea un file di testo chiamato **`docker-compose.yml`** con questo contenuto:

```yaml
services:
  app:
    image: albertoocello/gpx-route-builder:latest
    ports:
      - "8501:8501"
    environment:
      - AI_PROVIDER=${AI_PROVIDER:-claude}
      - AI_MODEL=${AI_MODEL:-}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - GEMINI_API_KEY=${GEMINI_API_KEY:-}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - OLLAMA_URL=${OLLAMA_URL:-http://ollama:11434}
    volumes:
      - ./routes:/app/routes
      - ./data:/app/data
      - segments4:/app/brouter/segments4
    restart: unless-stopped

volumes:
  segments4:
```

> **Come creare il file:**
> - **Mac**: apri TextEdit, vai su **Formato → Converti in formato testo normale**, incolla il contenuto, poi **File → Salva** con nome `docker-compose.yml` (rimuovi l'estensione `.txt` se presente).
> - **Windows**: apri Blocco Note, incolla il contenuto, poi **File → Salva con nome**, nella casella del nome scrivi `docker-compose.yml`, e nella voce **"Tipo"** scegli **"Tutti i file (*.*)"**, poi salva.

---

### Passo 4 — Crea il file `.env` con la tua chiave API

Nella stessa cartella crea un file chiamato **`.env`** (sì, inizia con un punto) con questo contenuto:

```
AI_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...
```

Sostituisci `sk-ant-...` con la tua chiave API vera.

> **Come ottenere la chiave API:**
> - **Claude (Anthropic)** — vai su [console.anthropic.com](https://console.anthropic.com) → **API Keys** → **Create Key**
> - **OpenAI** — vai su [platform.openai.com/api-keys](https://platform.openai.com/api-keys) → **Create new secret key**; poi nel file `.env` usa `AI_PROVIDER=openai` e `OPENAI_API_KEY=sk-...`
> - **Google Gemini** — vai su [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) → **Create API key**; poi usa `AI_PROVIDER=gemini` e `GEMINI_API_KEY=...`
> - **Ollama (gratuito, locale)** — nessuna chiave, usa `AI_PROVIDER=ollama` (vedi la sezione Ollama in fondo)

> **Come creare il file `.env`:**
> - **Mac**: nel Terminale aperto nella cartella, digita `touch .env`, poi aprilo con `open -e .env` (si apre in TextEdit).
> - **Windows**: nel prompt dei comandi, digita `copy NUL .env`, poi aprilo con `notepad .env`.

---

### Passo 5 — Avvia l'applicazione

Nel terminale aperto nella cartella del progetto, digita:

```bash
docker compose up
```

Al primo avvio Docker scarica automaticamente l'immagine dell'applicazione (~500 MB) e le mappe ciclistiche OSM della zona. **Ci vogliono alcuni minuti** — aspetta che nel terminale compaia una riga simile a:

```
  You can now view your Streamlit app in your browser.
  Local URL: http://localhost:8501
```

---

### Passo 6 — Apri il browser

Apri il tuo browser preferito e vai a:

```
http://localhost:8501
```

L'applicazione è pronta. Puoi lasciare il terminale aperto in background — non chiuderlo, altrimenti l'applicazione si ferma.

Per fermare l'applicazione: torna nel terminale e premi **Ctrl+C**.

---

## Aggiornamenti

Quando esce una nuova versione, nella cartella del progetto apri il terminale e digita:

```bash
docker compose pull
docker compose up
```

Docker scarica automaticamente la versione aggiornata.

---

## Uso con Ollama (AI gratuita, senza chiavi)

Se vuoi usare Ollama invece di un provider cloud, modifica il file `.env`:

```
AI_PROVIDER=ollama
OLLAMA_URL=http://ollama:11434
AI_MODEL=llama3.2
```

E avvia con il profilo Ollama:

```bash
docker compose --profile ollama up
```

Ollama scarica il modello al primo avvio (dipende dalla dimensione del modello, può volerci del tempo).

---

## Struttura del menu

| Tab | Cosa fa |
|---|---|
| **Planner** | Definisci waypoint e tema del percorso con l'AI |
| **Geolocalizza** | Cerca coordinate di un luogo o clicca sulla mappa |
| **Builder** | Genera i 3 GPX reali con scoring |
| **Analizza & Feedback** | Confronta pianificato vs reale, aggiungi note |
| **🔋 Analisi Giro** | Analisi personalizzata ebike/bici: batteria, calorie, tempo, fatica, consigli AI + report HTML scaricabile |
| **Debug** | Ispeziona prompt, score e ostacoli noti |

---

## Note

- I file GPX generati sono compatibili con Garmin, Komoot, Strava e qualsiasi app che legge il formato standard GPX.
- I percorsi e le preferenze utente sono salvati localmente sul tuo computer nelle cartelle `routes/` e `data/` create automaticamente. Niente viene inviato a server esterni (eccetto le chiamate al provider AI scelto).
- Per cambiare zona geografica (default: Marche, Italia) aggiungi `BROUTER_TILE=E13_N44` (o le coordinate del tile desiderato) nel file `.env`.

---

## Per gli sviluppatori

Per lavorare sul codice sorgente, clonare il repository e compilare l'immagine localmente:

```bash
git clone https://github.com/AlbertoOcello/gpx-route-builder.git
cd gpx-route-builder
cp .env.example .env
# modifica .env con la tua chiave API
```

Nel file `docker-compose.yml` del repo, sostituisci la riga `image:` con `build: .`:

```yaml
services:
  app:
    build: .          # compila localmente
    # image: albertoocello/gpx-route-builder:latest
```

Poi avvia con rebuild:

```bash
docker compose up --build
```

Per pubblicare una nuova immagine su Docker Hub:

```bash
docker build -t albertoocello/gpx-route-builder:latest .
docker push albertoocello/gpx-route-builder:latest
```
