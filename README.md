# YouTube Downloader & Player

A modern web application to watch and download YouTube videos without ads. Built with Python, Flask, and yt-dlp.

## Features

- **Watch in Browser** - Stream videos directly in your browser with native HTML5 player
- **Quality Selection** - Choose video resolution (up to 4K/8K) and audio quality separately
- **Subtitles/Languages** - Select subtitle language (manual or auto-generated)
- **Download** - Download videos with selected quality and subtitles
- **No Ads** - Clean viewing experience without YouTube ads
- **Responsive UI** - Works on desktop and mobile

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Installation

```bash
# Clone the repository
git clone https://github.com/WewennJr/yt-downloader.git
cd yt-downloader

# Install dependencies with uv
uv sync

# Or with pip
pip install -e .
```

### Running the Web App

```bash
# Using uv
uv run yt-dl-web

# Or directly
python -m yt_downloader.web
```

The app will be available at `http://localhost:5000`

### Command Line Usage

```bash
# Download a video (original CLI)
uv run yt-dl
# Enter URL when prompted
```

## Usage

1. Open `http://localhost:5000` in your browser
2. Paste a YouTube URL (e.g., `https://www.youtube.com/watch?v=dQw4w9WgXcQ`)
3. Click "Load Video" to fetch available formats
4. Click **Play in Browser** to watch, or **Download** to save

## Project Structure

```
yt-downloader/
├── pyproject.toml          # Project config & dependencies
├── README.md               # This file
├── .gitignore
└── src/
    └── yt_downloader/
        ├── __init__.py
        ├── app.py          # Original CLI downloader
        ├── web.py          # Flask web application
        ├── templates/
        │   └── index.html  # Frontend UI
        └── static/         # Static assets (CSS, JS)
            └── style.css   # Basic styling
```

## Technology Stack

- **Backend**: Flask 3.x, yt-dlp, requests
- **Frontend**: Vanilla HTML/CSS/JavaScript (no frameworks)
- **Video Processing**: yt-dlp for extraction, native browser playback

## Configuration

The app runs on `0.0.0.0:5000` by default. To change:

```python
# In src/yt_downloader/web.py
PORT = 5000  # Change to desired port
```

## License

MIT License - feel free to use and modify.

## Disclaimer

This tool is for personal use only. Respect YouTube's Terms of Service and copyright laws. Downloading videos may violate YouTube's ToS. Use responsibly.