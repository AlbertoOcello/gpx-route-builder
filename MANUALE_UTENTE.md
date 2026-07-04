# GPX Route Builder — Manuale Utente

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
