# GPX Route Builder — Manuale Utente

## Installazione rapida

> Per le istruzioni complete passo per passo vedi il [README](README.md).

**Cosa ti serve:**
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installato e avviato
- Una chiave API di [Anthropic Claude](https://console.anthropic.com), [OpenAI](https://platform.openai.com), [Google Gemini](https://aistudio.google.com) — oppure [Ollama](https://ollama.ai) in locale (gratuito)

**Passi:**

1. Crea una cartella vuota sul Desktop, ad esempio `gpx-route-builder`

2. Dentro la cartella crea il file **`docker-compose.yml`** con questo contenuto:

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

3. Dentro la cartella crea il file **`.env`** con la tua chiave API:

   ```
   AI_PROVIDER=claude
   ANTHROPIC_API_KEY=sk-ant-...
   ```

4. Apri il terminale nella cartella e avvia:

   ```bash
   docker compose up
   ```

   Al primo avvio Docker scarica l'immagine (~500 MB) e le mappe OSM. Aspetta la riga `Local URL: http://localhost:8501`.

5. Apri il browser su **`http://localhost:8501`**

**Aggiornamenti:** `docker compose pull && docker compose up`

---

## Panoramica

GPX Route Builder è uno strumento per pianificare percorsi ciclistici assistito dall'AI. Il flusso di lavoro si divide in due fasi:

1. **Planner** — definisci l'intenzione del percorso (dove vuoi andare, che esperienza vuoi)
2. **Builder** — il sistema genera tre percorsi reali con scoring e ti lascia scegliere

---

## Tab Planner

Qui definisci il percorso ideale. L'AI ordina i tuoi waypoint e ne aggiunge altri coerenti con il tema scelto.

### Partenza
- **Nome** — scrivi il nome della città/località e clicca 📍 per geocodificare automaticamente lat/lon
- **Lat/Lon** — puoi anche inserire le coordinate direttamente se le conosci

### Parametri principali
- **Distanza target** — la distanza che vuoi percorrere (in km)
- **Tipo giro** — Anello (torni al punto di partenza), Andata/Ritorno (stesso percorso), Solo andata (punto A→B)
- **Tema paesaggistico** — guida la scelta dei luoghi: Naturalistico, Storico-culturale, Panoramico, Misto
- **Tema atletico** — guida lo stile: Tranquillo, Medio, Impegnativo, Sportivo
- **Direzione geografica** — orienta il percorso: Nord, Sud, Est, Ovest, Libera

### Waypoint desiderati
Inserisci i luoghi che vuoi toccare, uno per riga. Puoi usare:
- Nome (es. `Corinaldo`)
- Coordinate (es. `43.65,13.05`)

Se lasci vuoto, l'AI sceglie liberamente in base al tema. Se inserisci dei punti, li ordina e aggiunge solo quello che serve per coprire la distanza.

### Limiti strade
- **Max SS%** — percentuale massima di Strade Statali nel percorso (default 8%)
- **Max SP%** — percentuale massima di Strade Provinciali (default 20%)
- **Strade da evitare** — nomi specifici da escludere sempre (es. `SS16, SP7`)

### Note libere
Campo testo a priorità assoluta — quello che scrivi qui sovrascrive qualsiasi parametro del form. Esempi:
- "Preferisco andare verso nord lungo la costa"
- "Va bene anche qualche salita in più"
- "Voglio passare dal Parco del Cormorano lungo il fiume"

### Bottoni
- **Pianifica waypoint** — chiama l'AI, che fa ricerche web sulla zona e propone la sequenza
- **Rigenera** — rigenera con i parametri attuali (produce risultati diversi)
- **Accetta route** — salva il piano e ti permette di dargli un nome; passa al Builder

### Risultati del Planner
- **Narrativa** — descrizione in italiano dello spirito del percorso proposto
- **Tabella waypoint** — lista ordinata con sorgente (user/planner) e coordinate
- **Stima distanza** — distanza in linea d'aria × 1.6 (indicativa, il percorso reale può variare)
- **Ricerche web** — espandibile, mostra le query fatte dall'AI per scegliere i luoghi
- **Prompt inviato a Claude** — per debug, mostra esattamente cosa ha ricevuto l'AI

---

## Tab Geolocalizza

Strumento di supporto per trovare coordinate precise prima di inserirle nel Planner.

- **Ricerca per nome** — cerca un luogo e vedi tutti i risultati Nominatim con badge di tipo (città, frazione, POI...). Clicca un risultato per vederlo sulla mappa e copiare le coordinate
- **Click sulla mappa** — clicca qualsiasi punto e ottieni le coordinate + indirizzo via reverse geocoding

Output: una stringa `lat,lon` pronta da incollare nel campo Waypoint del Planner.

---

## Tab Builder

Genera i tre percorsi GPX reali a partire da una route accettata dal Planner.

### Seleziona route
Scegli una route pianificata dal menu a tendina. Vengono mostrati narrativa, waypoint e stima distanza come promemoria.

### Profili BRouter (3 varianti)
I tre candidati usano profili diversi che determinano il tipo di fondo preferito:
- **ebike_asphalt_safe** — massimizza asfalto, minimo sterrato
- **ebike_gravel_easy** — asfalto + gravel leggero (max grade 2)
- **ebike_scenic** — preferisce strade secondarie panoramiche e borghi

### Parametri Builder
- **Dislivello max** — hard limit: percorsi che lo superano mostrano un avviso
- **Max SS% / Max SP%** — soglie per strade ad alto traffico

### Genera percorso reale
Avvia la pipeline completa: BRouter calcola le tre tracce → OSM Tag Enricher analizza superficie e traffico → Scoring Engine assegna i punteggi → Decision Agent sceglie il migliore.

### Risultati
- **Tabella candidati** — A/B/C con km reali, dislivello, score totale, stato
- **Esplora candidati** — radio button per vedere tutti sovrapposti o uno alla volta con mappa e dettaglio punteggi
- **Download GPX** — ogni candidato è scaricabile singolarmente, non solo il vincitore

### Punteggi
- **REALE** — calcolato su dati OSM reali (distanza, dislivello, superficie, traffico)
- **PLACEHOLDER** — in attesa di dati (se OSM Enricher non ha coperto quel tratto)

---

## Tab Analizza & Feedback

### Visualizza GPX
Carica qualsiasi file GPX (tuoi, da Strava, Komoot, Garmin) e vedi mappa + statistiche base.

### Confronta uscita
Carica il GPX pianificato e quello reale registrato durante l'uscita. Il sistema:
- Sovrappone le due tracce sulla mappa (pianificato = blu, reale = rosso)
- Calcola distanza, dislivello e deviazione massima con link Google Maps
- Identifica automaticamente il nome della route dal nome del file GPX

**Segnaposto**: clicca sulla mappa per aggiungere annotazioni geo-localizzate:
- 🔴 Problema (es. "sottopasso chiuso") — viene salvato come ostacolo noto per i prossimi percorsi
- 🟢 Bello (es. "bella vista")
- 🟠 Attenzione (es. "fondo dissestato")
- 🟣 Generico

### Feedback post-uscita
Form di valutazione dell'uscita: stelle, domande Sì/No, note libere. Al salvataggio:
- I segnaposto "problema" diventano ostacoli noti nel database
- Il Builder li eviterà automaticamente nei prossimi percorsi (scarto entro 150m)
- La UserMemory si aggiorna (distanza/dislivello confortevole via media mobile)

---

## Tab 🔋 Analisi Giro

Analisi personalizzata di un'uscita in bici o ebike: carica il GPX registrato durante il giro, scegli il tuo profilo, e l'AI calcola consumo batteria, calorie, tempo stimato, indice di fatica e consigli. Al termine puoi scaricare un report HTML completo.

---

### Profilo bici + driver

Prima di analizzare devi configurare un profilo che descrive te e la tua bici. Puoi salvare più profili (es. "Ebike Asfalto", "MTB Gravel") e selezionarli al momento dell'analisi.

**Seleziona o crea profilo**
Scegli un profilo esistente dal menu a tendina oppure seleziona **"Nuovo profilo"** per crearne uno.

**Dati bici**
- **Modello** — nome della bici (es. `Trek Rail 9.9`, `Specialized Turbo Levo`)
- **Tipo** — Ebike o Bici normale (cambia i campi disponibili)
- **Peso bici** — peso del mezzo in kg (influenza il calcolo di sforzo e consumo)
- _(solo ebike)_ **Capacità batteria** — in Wh (es. 630, 750)
- _(solo ebike)_ **% batteria al via** — carica al momento della partenza (default 100%)
- _(solo ebike)_ **% batteria minima** — soglia sotto cui non scendere (riserva di sicurezza)
- _(solo ebike)_ **Stile pedalata** — influenza il consumo stimato:
  - 🔋 Risparmio batteria — assistenza minima, solo nei tratti più duri
  - ⚡ Pedalata mista — alterna livelli bassi e medi
  - 😌 Comfort totale — assistenza medio-alta, frequenza cardiaca bassa
  - 🚀 Massima assistenza — livello più alto sempre

**Dati driver**
- **Peso** — in kg
- **Età** — anni
- **Sesso** — M / F / Altro
- **Livello fitness** — da 1 (principiante) a 5 (atleta); guida la stima di calorie e fatica
- **FC max** — frequenza cardiaca massima in bpm (se non la conosci, lascia il valore stimato)
- **Note salute** — campo libero per condizioni rilevanti (es. "problemi al ginocchio", "asma")

Clicca **Salva profilo** per memorizzarlo. I profili salvati sono disponibili in tutte le sessioni successive.

---

### Carica GPX e analisi

**Upload GPX**
Carica il file GPX registrato durante l'uscita (da Garmin, Wahoo, Strava, Komoot o qualsiasi ciclocomputer). Il sistema calcola automaticamente distanza, dislivello positivo/negativo e altitudine massima.

**Collega route pianificata** _(opzionale)_
Se il GPX è stato generato dal Builder (es. `anello_senigallia_B.gpx`), la route pianificata corrispondente viene rilevata automaticamente. Puoi anche selezionarla manualmente dal menu a tendina. La narrativa del Planner viene inclusa nel report HTML.

**Avvia analisi**
Clicca **Analizza** per inviare tutti i dati all'AI. L'elaborazione richiede alcuni secondi.

---

### Risultati

**Solo ebike — riga batteria**
| Metrica | Descrizione |
|---|---|
| % batteria consumata | Percentuale stimata di carica usata sull'intero percorso |
| Autonomia residua | Km percorribili con la carica rimanente |
| Livello assistenza stimato | Media del livello di supporto usato (1 = eco → 5 = turbo) |

**Tutti i tipi di bici**
| Metrica | Descrizione |
|---|---|
| Calorie | kcal consumate dal driver (al netto dell'assistenza per ebike) |
| Tempo stimato | Durata prevista dell'uscita (ore e minuti) |
| FC media stimata | Frequenza cardiaca media in bpm |
| Indice di fatica | Da 1 (leggero) a 10 (massimo sforzo) |

**Consigli AI** — bullet list di suggerimenti personalizzati sul percorso, sull'utilizzo della batteria, sulla gestione dello sforzo fisico e sulla sicurezza.

---

### Report HTML scaricabile

Clicca **Scarica report HTML** per ottenere un file autonomo (`{nome_gpx}_analysis.html`) che puoi aprire in qualsiasi browser, condividere o archiviare. Il report contiene:

- **Titolo** — nome del percorso (dal file GPX o dalla route collegata)
- **Intestazione** — data/ora analisi e nome del profilo usato
- **Dati percorso** — distanza, dislivello ±, altitudine massima
- **Mappa PNG** — traccia del percorso generata dal file GPX
- **Spirito del percorso** — narrativa del Planner, se la route è collegata
- **Profilo analisi** — dati bici e driver usati
- **Risultati** — tutti i valori calcolati (batteria, calorie, tempo, fatica)
- **Consigli** — lista di suggerimenti AI
- **Disclaimer** — nota sull'accuratezza delle stime

---

## Tab Debug

Strumento per chi vuole ispezionare il comportamento del sistema.

Per ogni route salvata mostra:
1. **JSON route** — waypoint, coordinate, sorgente (user/planner), parametri
2. **Prompt Planner** — il testo esatto inviato a Claude (system + user prompt)
3. **Score Builder** — punteggi dettagliati A/B/C se il Builder è già stato eseguito
4. **Ostacoli noti** — lista di ostacoli attivi con coordinate e link Maps; tasto 🔕 per disattivare quelli risolti (es. sottopasso riaperto)

---

## Flusso consigliato

1. **Geolocalizza** luoghi di interesse → copia coordinate
2. **Planner** → inserisci waypoint + tema + note → pianifica → valuta narrativa e mappa → accetta
3. **Builder** → seleziona route → genera → confronta A/B/C → scarica GPX preferito
4. **Carica il GPX su Garmin/Strava/Komoot** → vai in bici
5. **Analizza & Feedback** → carica GPX reale → aggiungi segnaposto → salva feedback

Il sistema impara dalle tue uscite e migliora i percorsi futuri.
