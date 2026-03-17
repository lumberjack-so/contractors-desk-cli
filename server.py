"""Cratchit Realtime Voice Service — OpenAI Realtime API relay."""

import asyncio
import base64
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("realtime")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
CRATCHIT_MCP_URL = os.environ.get("CRATCHIT_MCP_URL", "http://localhost:8771/mcp/mcp")
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-1.5"

SOUL_MD_PATH = "/home/admin/.openclaw/workspace/SOUL.md"
MEMORY_MD_PATH = "/home/admin/.openclaw/workspace/MEMORY.md"
LCM_DB_PATH = "/home/admin/.openclaw/lcm.db"
VOICE_LOG_PATH = "/home/admin/.openclaw/workspace/voice-log.md"


def _read_file(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception as e:
        return f"[unavailable: {e}]"


def assemble_lcm_context(db_path: str = LCM_DB_PATH, max_chars: int = 40000) -> str:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            SELECT ci.ordinal, ss.content, ss.kind, ss.depth
            FROM context_items ci
            JOIN summary_store ss ON ci.summary_id = ss.id
            ORDER BY ci.ordinal ASC
        """)
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return ""

        parts = []
        total = 0
        for row in rows:
            chunk = row["content"]
            if total + len(chunk) > max_chars:
                break
            parts.append(chunk)
            total += len(chunk)

        return "\n\n---\n\n".join(parts)
    except Exception as e:
        return f"[LCM context unavailable: {e}]"


def _load_voice_log(max_chars: int = 10000) -> str:
    """Load tail of voice-log.md, capped at max_chars."""
    try:
        with open(VOICE_LOG_PATH, "r") as f:
            content = f.read()
        if len(content) > max_chars:
            content = content[-max_chars:]
        log.info("Voice log loaded: %d chars", len(content))
        return content
    except FileNotFoundError:
        return ""
    except Exception as e:
        log.warning("Failed to read voice log: %s", e)
        return ""


def build_system_prompt() -> str:
    soul_md = _read_file(SOUL_MD_PATH)
    memory_md = _read_file(MEMORY_MD_PATH)
    lcm_context = assemble_lcm_context()
    voice_log = _load_voice_log()

    voice_history_section = ""
    if voice_log:
        voice_history_section = f"""

---

## Voice Session History
{voice_log}"""

    return f"""{soul_md}

---

## People & Key Facts
{memory_md}

---

## Conversation History
{lcm_context}{voice_history_section}

---

## Voice Instructions
You are on a voice call. Keep responses short and punchy — 1-3 sentences max unless specifically asked for detail. When taking action (searching, sending, creating), say what you are doing in 5 words or less, do it, then confirm in 5 words or less."""

TOOLS = [
    {
        "type": "function",
        "name": "gmail_search",
        "description": "Search Craig's Gmail inbox.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max results to return", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "gmail_read",
        "description": "Read a specific email by message ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID"},
            },
            "required": ["message_id"],
        },
    },
    {
        "type": "function",
        "name": "gmail_send",
        "description": "Send an email from Craig's Gmail (chaconstruction@gmail.com).",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body text"},
                "reply_to_id": {"type": "string", "description": "Message ID to reply to", "default": None},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "type": "function",
        "name": "gmail_draft",
        "description": "Create a draft email in Craig's Gmail.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body text"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "type": "function",
        "name": "calendar_list_events",
        "description": "List upcoming Google Calendar events.",
        "parameters": {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "description": "Calendar ID", "default": "primary"},
                "max_results": {"type": "integer", "description": "Max events to return", "default": 10},
                "query": {"type": "string", "description": "Search query to filter events"},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "calendar_create_event",
        "description": "Create a Google Calendar event.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start time (ISO 8601)"},
                "end": {"type": "string", "description": "End time (ISO 8601)"},
                "description": {"type": "string", "description": "Event description", "default": ""},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of attendee email addresses",
                },
                "calendar_id": {"type": "string", "description": "Calendar ID", "default": "primary"},
            },
            "required": ["summary", "start", "end"],
        },
    },
    {
        "type": "function",
        "name": "calendar_update_event",
        "description": "Update an existing Google Calendar event.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Event ID to update"},
                "calendar_id": {"type": "string", "description": "Calendar ID", "default": "primary"},
                "summary": {"type": "string", "description": "New event title"},
                "start": {"type": "string", "description": "New start time (ISO 8601)"},
                "end": {"type": "string", "description": "New end time (ISO 8601)"},
                "description": {"type": "string", "description": "New description"},
            },
            "required": ["event_id"],
        },
    },
    {
        "type": "function",
        "name": "delegate_to_cratchit",
        "description": "Delegate a complex task to the full Cratchit OpenClaw agent for multi-step work.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Description of the task to delegate"},
            },
            "required": ["task"],
        },
    },
]

app = FastAPI(title="Cratchit Realtime Voice")

# MCP session management
_mcp_session_id: str | None = None
_mcp_lock = asyncio.Lock()
_mcp_request_id = 0


def _next_mcp_id() -> int:
    global _mcp_request_id
    _mcp_request_id += 1
    return _mcp_request_id


def _parse_sse_data(text: str) -> dict | None:
    """Extract JSON from SSE text/event-stream response."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    return None


async def _mcp_post(client: httpx.AsyncClient, payload: dict, session_id: str | None = None) -> tuple[dict | None, str | None]:
    """POST to MCP endpoint, handle SSE or JSON response. Returns (data, session_id)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    resp = await client.post(CRATCHIT_MCP_URL, json=payload, headers=headers)
    new_session = resp.headers.get("mcp-session-id", session_id)

    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        data = _parse_sse_data(resp.text)
    else:
        data = resp.json()

    return data, new_session


async def _ensure_mcp_session(client: httpx.AsyncClient) -> str:
    """Initialize MCP session if needed, return session ID."""
    global _mcp_session_id
    if _mcp_session_id:
        return _mcp_session_id

    async with _mcp_lock:
        if _mcp_session_id:
            return _mcp_session_id

        init_payload = {
            "jsonrpc": "2.0",
            "id": _next_mcp_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "realtime-mode", "version": "0.1.0"},
            },
        }
        data, session_id = await _mcp_post(client, init_payload)
        if session_id:
            _mcp_session_id = session_id
            log.info("MCP session initialized: %s", session_id)

            # Send initialized notification
            notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            headers = {"Content-Type": "application/json", "Mcp-Session-Id": session_id}
            await client.post(CRATCHIT_MCP_URL, json=notif, headers=headers)

        return _mcp_session_id or ""


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/test")
async def test():
    results = {"openai_key_set": bool(OPENAI_API_KEY), "mcp_url": CRATCHIT_MCP_URL}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            init_payload = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "realtime-mode-test", "version": "0.1.1"},
                },
            }
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            resp = await client.post(CRATCHIT_MCP_URL, json=init_payload, headers=headers)
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                data = _parse_sse_data(resp.text)
            else:
                data = resp.json()
            reachable = bool(data and "jsonrpc" in data and "result" in data)
            results["mcp_reachable"] = reachable
    except Exception as e:
        results["mcp_reachable"] = False
        results["mcp_error"] = str(e)
    return results


@app.get("/voice", response_class=HTMLResponse)
async def voice_page():
    return FileResponse("static/index.html", media_type="text/html")


async def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Call a tool on the Cratchit MCP server via MCP Streamable HTTP."""
    global _mcp_session_id
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            session_id = await _ensure_mcp_session(client)

            payload = {
                "jsonrpc": "2.0",
                "id": _next_mcp_id(),
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
            data, new_session = await _mcp_post(client, payload, session_id)
            if new_session:
                _mcp_session_id = new_session

            if data and "result" in data and "content" in data["result"]:
                contents = data["result"]["content"]
                if contents and len(contents) > 0:
                    return contents[0].get("text", json.dumps(contents))
            if data and "error" in data:
                return json.dumps(data["error"])
            return json.dumps(data) if data else '{"error": "empty response"}'
    except Exception as e:
        log.error("MCP call failed: %s", e)
        _mcp_session_id = None  # Reset session on error
        return json.dumps({"error": str(e)})


TOOL_LABELS = {
    "gmail_search": "Searching Gmail...",
    "gmail_read": "Reading email...",
    "gmail_send": "Sending email...",
    "gmail_draft": "Creating draft...",
    "calendar_list_events": "Checking calendar...",
    "calendar_create_event": "Creating event...",
    "calendar_update_event": "Updating event...",
    "delegate_to_cratchit": "Delegating task...",
}


@app.websocket("/voice/ws")
async def voice_ws(ws: WebSocket):
    await ws.accept()
    log.info("Browser WebSocket connected")

    try:
        import websockets
    except ImportError:
        await ws.send_json({"type": "error", "message": "websockets not installed"})
        await ws.close()
        return

    if not OPENAI_API_KEY:
        await ws.send_json({"type": "error", "message": "OPENAI_API_KEY not set"})
        await ws.close()
        return

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        openai_ws = await websockets.connect(OPENAI_REALTIME_URL, additional_headers=headers)
    except Exception as e:
        log.error("Failed to connect to OpenAI: %s", e)
        await ws.send_json({"type": "error", "message": f"OpenAI connect failed: {e}"})
        await ws.close()
        return

    log.info("Connected to OpenAI Realtime API")

    # Transcript collection for this session
    transcript_turns: list[dict] = []

    # Build dynamic system prompt with full context
    instructions = build_system_prompt()
    log.info(f"Session context loaded: {len(instructions)} chars")

    # Send session config
    session_update = {
        "type": "session.update",
        "session": {
            "instructions": instructions,
            "voice": "cedar",
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "tools": TOOLS,
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 500},
        },
    }
    await openai_ws.send(json.dumps(session_update))

    async def browser_to_openai():
        """Relay audio from browser to OpenAI."""
        try:
            while True:
                data = await ws.receive()
                if data.get("type") == "websocket.disconnect":
                    break
                if "bytes" in data and data["bytes"]:
                    audio_b64 = base64.b64encode(data["bytes"]).decode("ascii")
                    event = {
                        "type": "input_audio_buffer.append",
                        "audio": audio_b64,
                    }
                    await openai_ws.send(json.dumps(event))
                elif "text" in data and data["text"]:
                    msg = json.loads(data["text"])
                    if msg.get("type") == "commit":
                        await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.error("browser_to_openai error: %s", e)

    async def openai_to_browser():
        """Relay audio from OpenAI to browser; handle function calls."""
        try:
            async for raw in openai_ws:
                event = json.loads(raw)
                etype = event.get("type", "")

                if etype == "conversation.item.created":
                    item = event.get("item", {})
                    if item.get("type") == "message":
                        role = item.get("role", "unknown")
                        parts = item.get("content", [])
                        text = " ".join(
                            p.get("transcript") or p.get("text") or ""
                            for p in parts
                        ).strip()
                        if text:
                            transcript_turns.append({"role": role, "text": text})

                elif etype == "response.audio.delta":
                    audio_bytes = base64.b64decode(event["delta"])
                    await ws.send_bytes(audio_bytes)

                elif etype == "response.audio_transcript.delta":
                    await ws.send_json({
                        "type": "transcript",
                        "role": "assistant",
                        "delta": event.get("delta", ""),
                    })

                elif etype == "input_audio_buffer.speech_started":
                    await ws.send_json({"type": "status", "status": "listening"})

                elif etype == "input_audio_buffer.speech_stopped":
                    await ws.send_json({"type": "status", "status": "processing"})

                elif etype == "response.audio.done":
                    await ws.send_json({"type": "status", "status": "idle"})

                elif etype == "response.function_call_arguments.done":
                    call_id = event.get("call_id", "")
                    fn_name = event.get("name", "")
                    fn_args_raw = event.get("arguments", "{}")
                    log.info("Function call: %s(%s)", fn_name, fn_args_raw)

                    label = TOOL_LABELS.get(fn_name, f"Running {fn_name}...")
                    await ws.send_json({"type": "tool_activity", "tool": fn_name, "label": label})

                    try:
                        fn_args = json.loads(fn_args_raw)
                    except json.JSONDecodeError:
                        fn_args = {}

                    result = await call_mcp_tool(fn_name, fn_args)
                    log.info("Tool result (%s): %.200s", fn_name, result)

                    await ws.send_json({"type": "tool_activity", "tool": fn_name, "label": "", "done": True})

                    # Send result back to OpenAI
                    fn_output = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": result,
                        },
                    }
                    await openai_ws.send(json.dumps(fn_output))
                    await openai_ws.send(json.dumps({"type": "response.create"}))

                elif etype == "error":
                    log.error("OpenAI error: %s", event)
                    await ws.send_json({"type": "error", "message": event.get("error", {}).get("message", "Unknown error")})

        except Exception as e:
            log.error("openai_to_browser error: %s", e)

    try:
        await asyncio.gather(browser_to_openai(), openai_to_browser())
    except Exception as e:
        log.error("WebSocket session ended: %s", e)
    finally:
        await openai_ws.close()
        if transcript_turns:
            try:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                lines = [f"## Voice Session — {ts}\n"]
                for turn in transcript_turns:
                    lines.append(f"**{turn['role']}:** {turn['text']}")
                lines.append("\n---\n")
                block = "\n".join(lines)
                os.makedirs(os.path.dirname(VOICE_LOG_PATH), exist_ok=True)
                with open(VOICE_LOG_PATH, "a") as f:
                    f.write(block)
                log.info("Transcript saved: %d turns", len(transcript_turns))
            except Exception as e:
                log.error("Failed to save transcript: %s", e)
        log.info("Session closed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8772)
