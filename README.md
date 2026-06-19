# 🧩 weBIGeo Tiles Server

![Version](https://img.shields.io/badge/version-0.1-blue) ![License](https://img.shields.io/github/license/webigeo/tiles-server)

Server that processes and serves geo tiles from various sources (e.g. weather stations) for weather, snow, and other map overlays.

This is currently a **skeleton**: it only exposes a status endpoint and a landing page. The tile processing and serving logic will be added incrementally. It shares its scaffolding (server setup, logging, notifications, database plumbing) with the [weBIGeo Cloud Server](https://github.com/weBIGeo/clouds-server).

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python server.py
```

Configuration is managed and documented in [`config.py`](config.py) (copy [`config.example.py`](config.example.py) to `config.py` to customize). See [`index.html`](docs/index.html) for the landing page.
