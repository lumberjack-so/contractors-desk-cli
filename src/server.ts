import "dotenv/config";
import express from "express";
import { createServer } from "node:http";
import { WebSocketServer, WebSocket } from "ws";
import { RealtimeAgent, RealtimeSession } from "@openai/agents/realtime";
import { buildSystemPrompt } from "./prompt.js";
import { saveSession, type TranscriptTurn } from "./history.js";
import { allTools, TOOL_LABELS } from "./tools.js";

const PORT = Number(process.env.PORT) || 8772;
const OPENAI_API_KEY = process.env.OPENAI_API_KEY || "";

if (!OPENAI_API_KEY) {
  console.error("OPENAI_API_KEY is not set");
  process.exit(1);
}

const app = express();
const server = createServer(app);

// --- Static file serving & routes ---
app.use("/voice", express.static("static"));
app.get("/voice", (_req, res) => res.sendFile("index.html", { root: "static" }));
app.get("/health", (_req, res) => res.json({ status: "ok" }));

app.get("/test", async (_req, res) => {
  const results: Record<string, unknown> = {
    openai_key_set: Boolean(OPENAI_API_KEY),
    mcp_url: process.env.CRATCHIT_MCP_URL || "http://localhost:8771/mcp/mcp",
  };
  try {
    const mcpUrl = results.mcp_url as string;
    const resp = await fetch(mcpUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 0,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "realtime-mode-test", version: "2.0.0" },
        },
      }),
      signal: AbortSignal.timeout(10000),
    });
    const contentType = resp.headers.get("content-type") || "";
    let data: any;
    if (contentType.includes("text/event-stream")) {
      const text = await resp.text();
      for (const line of text.split("\n")) {
        if (line.startsWith("data: ")) {
          try { data = JSON.parse(line.slice(6)); break; } catch {}
        }
      }
    } else {
      data = await resp.json();
    }
    results.mcp_reachable = Boolean(data?.jsonrpc && data?.result);
  } catch (e: any) {
    results.mcp_reachable = false;
    results.mcp_error = e.message;
  }
  res.json(results);
});

// --- WebSocket server for browser clients ---
const wss = new WebSocketServer({ server, path: "/voice/ws" });

wss.on("connection", (browserWs: WebSocket) => {
  console.log("Browser WebSocket connected");
  const transcriptTurns: TranscriptTurn[] = [];

  // Build system prompt with full context
  const instructions = buildSystemPrompt();
  console.log(`Session context loaded: ${instructions.length} chars`);

  // Create RealtimeAgent
  const agent = new RealtimeAgent({
    name: "Cratchit",
    instructions,
    tools: allTools,
    voice: "ash",
  });

  // Create RealtimeSession with websocket transport
  const session = new RealtimeSession(agent, {
    apiKey: OPENAI_API_KEY,
    transport: "websocket",
    model: "gpt-realtime-1.5",
    config: {
      audio: {
        input: {
          format: { type: "audio/pcm", rate: 24000 },
          transcription: { model: "gpt-4o-transcribe", language: "en" },
          turnDetection: { type: "semantic_vad" },
        },
        output: {
          format: { type: "audio/pcm", rate: 24000 },
        },
      },
      outputModalities: ["audio"],
    },
  });

  // --- Session event handlers ---

  // Audio from OpenAI → browser
  session.on("audio", (event) => {
    if (browserWs.readyState === WebSocket.OPEN) {
      browserWs.send(Buffer.from(event.data), { binary: true });
    }
  });

  // Transcript deltas → browser
  session.on("transport_event", (event) => {
    if (browserWs.readyState !== WebSocket.OPEN) return;

    if (event.type === "conversation.item.input_audio_transcription.completed") {
      const text = (event as any).transcript?.trim();
      if (text) {
        transcriptTurns.push({ role: "user", text });
      }
    }

    // Forward raw events we care about for the browser UI
    const rawEvent = event as any;
    if (rawEvent.type === "response.audio_transcript.delta") {
      browserWs.send(
        JSON.stringify({ type: "transcript", role: "assistant", delta: rawEvent.delta }),
      );
    } else if (rawEvent.type === "input_audio_buffer.speech_started") {
      browserWs.send(JSON.stringify({ type: "status", status: "listening" }));
    } else if (rawEvent.type === "input_audio_buffer.speech_stopped") {
      browserWs.send(JSON.stringify({ type: "status", status: "processing" }));
    } else if (rawEvent.type === "response.audio.done") {
      browserWs.send(JSON.stringify({ type: "status", status: "idle" }));
    } else if (rawEvent.type === "response.audio_transcript.done") {
      const text = rawEvent.transcript?.trim();
      if (text) {
        transcriptTurns.push({ role: "assistant", text });
      }
    }
  });

  // Tool activity → browser
  session.on("agent_tool_start", (_ctx, _agent, tool, _details) => {
    if (browserWs.readyState !== WebSocket.OPEN) return;
    const label = TOOL_LABELS[tool.name || ""] || `Running ${tool.name}...`;
    browserWs.send(JSON.stringify({ type: "tool_activity", tool: tool.name, label }));
  });

  session.on("agent_tool_end", (_ctx, _agent, tool, _result, _details) => {
    if (browserWs.readyState !== WebSocket.OPEN) return;
    browserWs.send(
      JSON.stringify({ type: "tool_activity", tool: tool.name, label: "", done: true }),
    );
  });

  // Errors
  session.on("error", (err) => {
    console.error("RealtimeSession error:", err);
    if (browserWs.readyState === WebSocket.OPEN) {
      browserWs.send(JSON.stringify({ type: "error", message: String(err.error) }));
    }
  });

  // Audio interrupted
  session.on("audio_interrupted", () => {
    // Browser handles this via status changes
  });

  // --- Connect session to OpenAI ---
  session
    .connect({ apiKey: OPENAI_API_KEY })
    .then(() => {
      console.log("Connected to OpenAI Realtime API via Agents SDK");
    })
    .catch((err) => {
      console.error("Failed to connect to OpenAI:", err);
      if (browserWs.readyState === WebSocket.OPEN) {
        browserWs.send(JSON.stringify({ type: "error", message: `OpenAI connect failed: ${err}` }));
        browserWs.close();
      }
    });

  // --- Browser audio → OpenAI ---
  browserWs.on("message", (data: Buffer | string, isBinary: boolean) => {
    if (isBinary && Buffer.isBuffer(data)) {
      // PCM audio from browser
      const copy = new ArrayBuffer(data.byteLength);
      new Uint8Array(copy).set(data);
      session.sendAudio(copy);
    } else {
      // JSON control messages
      try {
        const msg = JSON.parse(data.toString());
        if (msg.type === "commit") {
          session.sendAudio(new ArrayBuffer(0), { commit: true });
        }
      } catch {}
    }
  });

  // --- Cleanup on disconnect ---
  browserWs.on("close", () => {
    console.log("Browser disconnected");
    session.close();

    if (transcriptTurns.length > 0) {
      saveSession(transcriptTurns);
      console.log(`Transcript saved: ${transcriptTurns.length} turns`);
    }
    console.log("Session closed");
  });

  browserWs.on("error", (err) => {
    console.error("Browser WebSocket error:", err);
  });
});

// --- Start server ---
server.listen(PORT, "0.0.0.0", () => {
  console.log(`Cratchit Realtime Voice v2.0 listening on port ${PORT}`);
});
