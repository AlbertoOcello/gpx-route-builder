🇮🇹 [Italiano](README.md) | 🇬🇧 [English](README_EN.md)

# GPX Route Builder (by A. Ocello)

Plan your cycling routes with the help of AI. Enter distance, terrain type and preferred places — the system generates three GPX routes ready to load on Garmin, Strava or Komoot.

---

## What it does

- Plans cycling routes based on your preferences (distance, elevation, surface type, places)
- Generates three GPX variants with automatic scoring
- Compares the planned route with the real one after your ride
- Learns from your rides and improves future routes

---

## Requirements

- **Docker Desktop**
  - Mac: [download here](https://www.docker.com/products/docker-desktop/)
  - Windows: [download here](https://www.docker.com/products/docker-desktop/)

    > **Windows only — before installing Docker, install WSL2:**
    >
    > WSL2 is a Windows component that Docker requires. To install it:
    >
    > 1. Press the **Start** key, type `PowerShell`, then right-click the icon and choose **"Run as administrator"**.
    > 2. If Windows asks "Do you want to allow this app to make changes?", click **Yes**.
    > 3. In the black window that opens, paste this command and press **Enter**:
    >    ```
    >    wsl --install
    >    ```
    > 4. Wait for it to finish (a couple of minutes), then **restart your PC**.
    >
    > After restarting you can install Docker Desktop normally.

- An API key from one of these AI providers:
  - [Anthropic Claude](https://console.anthropic.com) (recommended)
  - [OpenAI](https://platform.openai.com)
  - [Google Gemini](https://aistudio.google.com)
  - or [Ollama](https://ollama.ai) locally (free, no key required)

---

## Simple Installation (users)

Download the installer from the [Releases page](https://github.com/AlbertoOcello/gpx-route-builder/releases/latest):

| System | File to download |
|---|---|
| **Mac** (macOS 11+) | `GPX-Route-Builder-Mac.dmg` |
| **Windows** (10/11) | `GPX-Route-Builder-Setup.exe` |

### Mac

1. Open the `.dmg` file
2. Drag **GPX Route Builder** into the **Applications** folder
3. Open the app from Launchpad or Applications
   > **First launch — Gatekeeper:** if you see "cannot be opened", go to **System Settings → Privacy & Security** and click **"Open Anyway"**
4. On first launch choose your AI provider and enter your API key
5. The browser opens automatically at `http://localhost:8501`

### Windows

1. Run `GPX-Route-Builder-Setup.exe`
   > **SmartScreen:** click **"More info" → "Run anyway"**
2. Follow the wizard (no administrator rights required)
3. Double-click the Desktop icon
4. On first launch choose your AI provider and enter your API key
5. The browser opens automatically at `http://localhost:8501`

> **Docker Desktop** is required — if not installed, the app guides you through the download.

---

## Manual Installation (advanced)

### Step 1 — Make sure Docker Desktop is running

Open Docker Desktop from the Applications menu (Mac) or Desktop (Windows) and wait until the icon stops animating. Docker must be running before you proceed.

---

### Step 2 — Create the project folder

Create a new empty folder on your computer where you want to keep the application, for example `gpx-route-builder` on your Desktop.

**Open the terminal in that folder:**

- **Mac**: open Finder, navigate to the folder, then right-click → **"Services" → "New Terminal at Folder"**
  _(or open Terminal and drag the folder into the window)_
- **Windows**: open File Explorer, navigate to the folder, then click the address bar at the top, type `cmd` and press **Enter**

---

### Step 3 — Create the `docker-compose.yml` file

In the folder you just created, create a text file called **`docker-compose.yml`** with this content:

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

> **How to create the file:**
> - **Mac**: open TextEdit, go to **Format → Make Plain Text**, paste the content, then **File → Save** with the name `docker-compose.yml` (remove the `.txt` extension if present).
> - **Windows**: open Notepad, paste the content, then **File → Save As**, type `docker-compose.yml` in the filename box, and in the **"Save as type"** dropdown choose **"All Files (*.*)"**, then save.

---

### Step 4 — Create the `.env` file with your API key

In the same folder create a file called **`.env`** (yes, it starts with a dot) with this content:

```
AI_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...
```

Replace `sk-ant-...` with your actual API key.

> **How to get an API key:**
> - **Claude (Anthropic)** — go to [console.anthropic.com](https://console.anthropic.com) → **API Keys** → **Create Key**
> - **OpenAI** — go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys) → **Create new secret key**; in the `.env` file use `AI_PROVIDER=openai` and `OPENAI_API_KEY=sk-...`
> - **Google Gemini** — go to [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) → **Create API key**; use `AI_PROVIDER=gemini` and `GEMINI_API_KEY=...`
> - **Ollama (free, local)** — no key needed, use `AI_PROVIDER=ollama` (see the Ollama section below)

> **How to create the `.env` file:**
> - **Mac**: in the Terminal opened in the folder, type `touch .env`, then open it with `open -e .env` (opens in TextEdit).
> - **Windows**: in the command prompt, type `copy NUL .env`, then open it with `notepad .env`.

---

### Step 5 — Start the application

**Easy method (recommended):** download `start.command` (Mac) or `start.bat` (Windows) from the [project GitHub page](https://github.com/AlbertoOcello/gpx-route-builder) and place it in the same folder. Then:

- **Mac**: double-click `start.command`
  > The first time macOS may block the file. Go to **System Settings → Privacy & Security**, scroll down to find the message about `start.command` and click **"Open Anyway"**.
- **Windows**: double-click `start.bat`

The script starts Docker Desktop if it is not already open, launches the application and opens the browser automatically.

**Manual method (terminal):** in the terminal opened in the project folder:

```bash
docker compose up
```

On the first run Docker automatically downloads the application image (~500 MB) and the OSM cycling maps for your area. **This takes a few minutes** — wait until you see a line like:

```
  You can now view your Streamlit app in your browser.
  Local URL: http://localhost:8501
```

---

### Step 6 — Open the browser

```
http://localhost:8501
```

_(If you used `start.command` or `start.bat` the browser opens automatically.)_

The application is ready. You can leave the terminal open in the background — do not close it, otherwise the application will stop.

To stop the application: go back to the terminal and press **Ctrl+C**.

---

## Updates

When a new version is released, double-click `start.command` / `start.bat` — it automatically pulls the updated image before launching.

Alternatively, from the terminal in the project folder:

```bash
docker compose pull
docker compose up
```

---

## Using Ollama (free AI, no keys)

If you want to use Ollama instead of a cloud provider, edit the `.env` file:

```
AI_PROVIDER=ollama
OLLAMA_URL=http://ollama:11434
AI_MODEL=llama3.2
```

And start with the Ollama profile:

```bash
docker compose --profile ollama up
```

Ollama downloads the model on first launch (this may take some time depending on model size).

---

## Menu structure

| Tab | What it does |
|---|---|
| **Planner** | Define waypoints and route theme with AI |
| **Geolocate** | Search coordinates for a place or click on the map |
| **Builder** | Generate the 3 real GPX files with scoring |
| **Analyse & Feedback** | Compare planned vs real route, add notes |
| **🔋 Ride Analysis** | Personalised ebike/bike analysis: battery, calories, time, fatigue, AI tips + downloadable HTML report |
| **Debug** | Inspect prompts, scores and known obstacles |

---

## Notes

- Generated GPX files are compatible with Garmin, Komoot, Strava and any app that reads the standard GPX format.
- Routes and user preferences are saved locally on your computer in the `routes/` and `data/` folders created automatically. Nothing is sent to external servers (except calls to the chosen AI provider).
- To change the geographic area (default: Marche, Italy) add `BROUTER_TILE=E13_N44` (or the coordinates of the desired tile) in the `.env` file.

---

## For developers

To work on the source code, clone the repository and build the image locally:

```bash
git clone https://github.com/AlbertoOcello/gpx-route-builder.git
cd gpx-route-builder
cp .env.example .env
# edit .env with your API key
```

In the repo's `docker-compose.yml`, replace the `image:` line with `build: .`:

```yaml
services:
  app:
    build: .          # build locally
    # image: albertoocello/gpx-route-builder:latest
```

Then start with rebuild:

```bash
docker compose up --build
```

To publish a new image to Docker Hub:

```bash
docker build -t albertoocello/gpx-route-builder:latest .
docker push albertoocello/gpx-route-builder:latest
```
