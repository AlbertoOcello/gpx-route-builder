# GPX Route Builder — User Manual

## Quick Installation

> For full step-by-step instructions see the [README](README_EN.md).

**What you need:**
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- An API key from [Anthropic Claude](https://console.anthropic.com), [OpenAI](https://platform.openai.com), [Google Gemini](https://aistudio.google.com) — or [Ollama](https://ollama.ai) locally (free)

**Steps:**

1. Create an empty folder on your Desktop, for example `gpx-route-builder`

2. Inside the folder create a file called **`docker-compose.yml`** with this content:

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

3. Inside the folder create a file called **`.env`** with your API key:

   ```
   AI_PROVIDER=claude
   ANTHROPIC_API_KEY=sk-ant-...
   ```

4. Open the terminal in the folder and start:

   ```bash
   docker compose up
   ```

   On first run Docker downloads the image (~500 MB) and OSM maps. Wait for `Local URL: http://localhost:8501`.

5. Open your browser at **`http://localhost:8501`**

**Updates:** `docker compose pull && docker compose up`

---

## Overview

GPX Route Builder is an AI-assisted tool for planning cycling routes. The workflow is split into two phases:

1. **Planner** — define the intent of the route (where you want to go, what experience you want)
2. **Builder** — the system generates three real routes with scoring and lets you choose

---

## Planner Tab

Here you define the ideal route. The AI orders your waypoints and adds others consistent with the chosen theme.

### Starting point
- **Name** — type the name of the city/place and click 📍 to automatically geocode lat/lon
- **Lat/Lon** — you can also enter coordinates directly if you know them

### Main parameters
- **Target distance** — the distance you want to ride (in km)
- **Route type** — Loop (return to start), Out & Back (same path both ways), One way (point A→B)
- **Scenery theme** — guides the choice of places: Nature, Historic-cultural, Scenic, Mixed
- **Athletic theme** — guides the style: Easy, Moderate, Challenging, Sport
- **Geographic direction** — orients the route: North, South, East, West, Free

### Desired waypoints
Enter the places you want to visit, one per line. You can use:
- Name (e.g. `Corinaldo`)
- Coordinates (e.g. `43.65,13.05`)

If left empty, the AI chooses freely based on the theme. If you enter points, it orders them and only adds what is needed to cover the distance.

### Road limits
- **Max SS% (state roads)** — maximum percentage of main roads in the route (default 8%)
- **Max SP% (provincial roads)** — maximum percentage of provincial roads (default 20%)
- **Roads to avoid** — specific names to always exclude (e.g. `SS16, SP7`)

### Free notes
Free text field with absolute priority — what you write here overrides any form parameter. Examples:
- "I prefer going north along the coast"
- "A few extra climbs are fine"
- "I want to go through the Cormorano Park along the river"

### Buttons
- **Plan waypoints** — calls the AI, which searches the web for the area and proposes the sequence
- **Regenerate** — regenerates with current parameters (produces different results)
- **Accept route** — saves the plan and lets you name it; moves to the Builder

### Planner results
- **Narrative** — description of the spirit of the proposed route
- **Waypoint table** — ordered list with source (user/planner) and coordinates
- **Distance estimate** — straight-line distance × 1.6 (indicative, real route may vary)
- **Web searches** — expandable, shows queries made by the AI to choose places
- **Prompt sent to Claude** — for debugging, shows exactly what the AI received

---

## Geolocate Tab

Support tool for finding precise coordinates before entering them in the Planner.

- **Search by name** — search for a place and see all Nominatim results with type badge (city, hamlet, POI...). Click a result to see it on the map and copy the coordinates
- **Click on the map** — click any point and get coordinates + address via reverse geocoding

Output: a `lat,lon` string ready to paste into the Planner Waypoint field.

---

## Builder Tab

Generates the three real GPX routes from a route accepted by the Planner.

### Select route
Choose a planned route from the dropdown. The narrative, waypoints and distance estimate are shown as a reminder.

### BRouter profiles (3 variants)
The three candidates use different profiles that determine the preferred surface type:
- **ebike_asphalt_safe** — maximises tarmac, minimum gravel
- **ebike_gravel_easy** — tarmac + light gravel (max grade 2)
- **ebike_scenic** — prefers scenic secondary roads and villages

### Builder parameters
- **Max elevation gain (m)** — hard limit: routes that exceed it show a warning
- **Max SS% (state roads) / Max SP% (provincial roads)** — thresholds for high-traffic roads

### Generate actual route
Starts the full pipeline: BRouter calculates the three tracks → OSM Tag Enricher analyses surface and traffic → Scoring Engine assigns scores → Decision Agent picks the best.

### Results
- **Candidate table** — A/B/C with real km, elevation, total score, status
- **Explore candidates** — radio buttons to see all overlaid or one at a time with map and score detail
- **GPX download** — each candidate is downloadable individually, not just the winner

### Scores
- **REAL** — calculated on real OSM data (distance, elevation, surface, traffic)
- **PLACEHOLDER** — awaiting data (if OSM Enricher has not covered that section)

---

## Analyse & Feedback Tab

### View GPX
Load any GPX file (yours, from Strava, Komoot, Garmin) and see map + basic statistics.

### Compare ride
Load the planned GPX and the real one recorded during your ride. The system:
- Overlays both tracks on the map (planned = blue, real = red)
- Calculates distance, elevation and maximum deviation with Google Maps link
- Automatically identifies the route name from the GPX filename

**Markers**: click on the map to add geo-located annotations:
- 🔴 Problem (e.g. "underpass closed") — saved as a known obstacle for future routes
- 🟢 Beautiful (e.g. "great view")
- 🟠 Caution (e.g. "rough surface")
- 🟣 Generic

### Post-ride feedback
Ride evaluation form: stars, Yes/No questions, free notes. On saving:
- "Problem" markers become known obstacles in the database
- The Builder will automatically avoid them in future routes (exclusion within 150m)
- UserMemory updates (comfortable distance/elevation via moving average)

---

## 🔋 Ride Analysis Tab

Personalised analysis of a bike or ebike ride: upload the GPX recorded during your outing, select your profile, and the AI calculates battery consumption, calories, estimated time, fatigue index and advice. At the end you can download a complete HTML report.

---

### Bike + driver profile

Before analysing you need to configure a profile describing you and your bike. You can save multiple profiles (e.g. "Ebike Road", "MTB Gravel") and select them at analysis time.

**Select or create a profile**
Choose an existing profile from the dropdown or select **"New profile"** to create one.

**Bike data**
- **Model** — bike name (e.g. `Trek Rail 9.9`, `Specialized Turbo Levo`)
- **Type** — Ebike or Regular bike (changes the available fields)
- **Bike weight** — weight of the bike in kg (affects effort and consumption estimates)
- _(ebike only)_ **Battery capacity** — in Wh (e.g. 630, 750)
- _(ebike only)_ **Battery % at start** — charge level at departure (default 100%)
- _(ebike only)_ **Minimum battery %** — safety reserve threshold not to go below
- _(ebike only)_ **Riding style** — affects estimated consumption:
  - 🔋 Battery saving — minimal assistance, only on the hardest sections
  - ⚡ Mixed riding — alternates low and medium levels
  - 😌 Full comfort — medium-high assistance, low heart rate
  - 🚀 Maximum assistance — highest level always on

**Driver data**
- **Weight** — in kg
- **Age** — years
- **Sex** — M / F / Other
- **Fitness level** — from 1 (beginner) to 5 (athlete); guides calorie and fatigue estimates
- **Max HR** — maximum heart rate in bpm (leave the estimated value if you don't know it)
- **Health notes** — free text for relevant conditions (e.g. "knee problems", "asthma")

Click **Save profile** to store it. Saved profiles are available in all future sessions.

---

### Upload GPX and analysis

**Upload GPX**
Upload the GPX file recorded during your ride (from Garmin, Wahoo, Strava, Komoot or any cycle computer). The system automatically calculates distance, elevation gain/loss and maximum altitude.

**Link planned route** _(optional)_
If the GPX was generated by the Builder (e.g. `anello_senigallia_B.gpx`), the corresponding planned route is detected automatically. You can also select it manually from the dropdown. The Planner narrative is then included in the HTML report.

**Run analysis**
Click **Analyse** to send all data to the AI. Processing takes a few seconds.

---

### Results

**Ebike only — battery row**
| Metric | Description |
|---|---|
| Battery % consumed | Estimated charge percentage used over the whole route |
| Range remaining | Km rideable with the remaining charge |
| Estimated assistance level | Average assistance level used (1 = eco → 5 = turbo) |

**All bike types**
| Metric | Description |
|---|---|
| Calories | kcal burned by the rider (net of motor assistance for ebikes) |
| Estimated time | Expected ride duration (hours and minutes) |
| Estimated avg HR | Average heart rate in bpm |
| Fatigue index | From 1 (easy) to 10 (maximum effort) |

**AI advice** — personalised bullet-point suggestions on the route, battery management, physical effort and safety.

---

### Downloadable HTML report

Click **Download HTML report** to get a self-contained file (`{gpx_name}_analysis.html`) you can open in any browser, share or archive. The report contains:

- **Title** — route name (from the GPX file or the linked planned route)
- **Header** — analysis date/time and profile name used
- **Route data** — distance, elevation ±, maximum altitude
- **Map PNG** — track rendered from the GPX file
- **Route spirit** — Planner narrative, if a route is linked
- **Analysis profile** — bike and driver data used
- **Results** — all calculated values (battery, calories, time, fatigue)
- **Advice** — AI suggestions list
- **Disclaimer** — note on estimate accuracy

---

## Debug Tab

Tool for those who want to inspect system behaviour.

For each saved route it shows:
1. **Route JSON** — waypoints, coordinates, source (user/planner), parameters
2. **Planner Prompt** — the exact text sent to Claude (system + user prompt)
3. **Builder Score** — detailed A/B/C scores if the Builder has already run
4. **Known obstacles** — list of active obstacles with coordinates and Maps link; 🔕 button to deactivate resolved ones (e.g. underpass reopened)

---

## Recommended workflow

1. **Geolocate** places of interest → copy coordinates
2. **Planner** → enter waypoints + theme + notes → plan → evaluate narrative and map → accept
3. **Builder** → select route → generate → compare A/B/C → download preferred GPX
4. **Load GPX on Garmin/Strava/Komoot** → go cycling
5. **Analyse & Feedback** → load real GPX → add markers → save feedback

The system learns from your rides and improves future routes.
