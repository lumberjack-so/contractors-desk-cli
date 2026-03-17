# Cratchit Realtime Voice

OpenAI Realtime API voice interface for Cratchit, the AI assistant for C.H. Anderson Construction.

## Overview

A FastAPI service that relays voice audio between a browser and OpenAI's Realtime API, with tool bridging to the Cratchit MCP server for Gmail, Google Calendar, and Contractors Desk operations.

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /voice` | Browser voice widget |
| `WS /voice/ws` | WebSocket relay (browser ↔ OpenAI Realtime) |
| `GET /health` | Health check |
| `GET /test` | Connectivity test |

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit with your OPENAI_API_KEY
uvicorn server:app --host 0.0.0.0 --port 8772
```

## Configuration

Environment variables (`.env`):
- `OPENAI_API_KEY` — OpenAI API key with Realtime API access
- `CRATCHIT_MCP_URL` — MCP server endpoint (default: `http://localhost:8771/mcp/mcp`)

## Systemd

```bash
sudo systemctl enable cratchit-realtime
sudo systemctl start cratchit-realtime
```

## Architecture

```
Browser ←→ /voice/ws (FastAPI WebSocket)
                ↕
        OpenAI Realtime API (wss://)
                ↕
        Tool calls → Cratchit MCP Server (http://localhost:8771/mcp/mcp)
```

Audio format: PCM 16-bit, 24kHz, mono.
