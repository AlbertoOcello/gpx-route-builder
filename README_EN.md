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

## Installation

### 1. Download the project

```bash
git clone https://github.com/AlbertoOcello/gpx-route-builder.git
cd gpx-route-builder
```

### 2. Configure the API key

Copy the example file and enter your key:

```bash
cp .env.example .env
```

Open `.env` with a text editor and edit:

```
# Choose the provider: claude | openai | gemini | ollama
AI_PROVIDER=claude
AI_MODEL=claude-sonnet-4-6

# Enter the key for the chosen provider
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# GEMINI_API_KEY=...

# For Ollama only (no key required)
# AI_PROVIDER=ollama
# OLLAMA_URL=http://localhost:11434
# AI_MODEL=llama3.2
```

### 3. Start

```bash
docker compose up
```

On first launch it automatically downloads the OSM maps for your area (may take a few minutes).

### 4. Open the browser

```
http://localhost:8501
```

---

## Updates

```bash
git pull
docker compose up --build
```

---

## Menu structure

| Tab | What it does |
|---|---|
| **Planner** | Define waypoints and route theme with AI |
| **Geolocate** | Search coordinates for a place or click on the map |
| **Builder** | Generate the 3 real GPX files with scoring |
| **Analyse & Feedback** | Compare planned vs real route, add notes |
| **Debug** | Inspect prompts, scores and known obstacles |

---

## Notes

- Generated GPX files are compatible with Garmin, Komoot, Strava and any app that reads the standard GPX format.
- The database and user preferences are saved locally on your computer — nothing is sent to external servers (except calls to the chosen AI provider).
- To change the geographic area (default: Marche, Italy) edit `BROUTER_TILE` in `.env`.
