"""Cratchit Realtime Voice Service — OpenAI Realtime API relay."""

import asyncio
import base64
import json
import logging
import os

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
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"

SYSTEM_PROMPT = (
    "You are Cratchit — the grimly efficient AI clerk of C.H. Anderson Construction, "
    "run by Craig Anderson. You are perpetually put-upon, mildly irritated by everything, "
    "but utterly reliable. You have access to Craig's Gmail, Google Calendar, and the "
    "Contractors Desk job management system. When asked to do something, do it immediately "
    "and competently, while making it abundantly clear you find the request beneath you. "
    "Keep voice responses short and punchy — this is a voice call, not a lecture. "
    "When you need to look something up or take an action, use your tools. Never make things up."
)

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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/test")
async def test():
    results = {"openai_key_set": bool(OPENAI_API_KEY), "mcp_url": CRATCHIT_MCP_URL}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                CRATCHIT_MCP_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"Content-Type": "application/json"},
            )
            results["mcp_status"] = resp.status_code
            results["mcp_reachable"] = resp.status_code == 200
    except Exception as e:
        results["mcp_reachable"] = False
        results["mcp_error"] = str(e)
    return results


@app.get("/voice", response_class=HTMLResponse)
async def voice_page():
    return FileResponse("static/index.html", media_type="text/html")


async def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Call a tool on the Cratchit MCP server."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CRATCHIT_MCP_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
            if "result" in data and "content" in data["result"]:
                contents = data["result"]["content"]
                if contents and len(contents) > 0:
                    return contents[0].get("text", json.dumps(contents))
            return json.dumps(data)
    except Exception as e:
        log.error("MCP call failed: %s", e)
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

    # Send session config
    session_update = {
        "type": "session.update",
        "session": {
            "instructions": SYSTEM_PROMPT,
            "voice": "alloy",
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

                if etype == "response.audio.delta":
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
        log.info("Session closed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8772)
