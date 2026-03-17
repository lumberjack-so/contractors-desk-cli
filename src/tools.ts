import { tool } from "@openai/agents/realtime";
import { z } from "zod";

const MCP_URL = process.env.CRATCHIT_MCP_URL || "http://localhost:8771/mcp/mcp";

let mcpSessionId: string | null = null;
let mcpRequestId = 0;

function nextMcpId(): number {
  return ++mcpRequestId;
}

function parseSseData(text: string): any | null {
  for (const line of text.split("\n")) {
    if (line.startsWith("data: ")) {
      try {
        return JSON.parse(line.slice(6));
      } catch {
        continue;
      }
    }
  }
  return null;
}

async function mcpPost(
  payload: Record<string, unknown>,
  sessionId: string | null = null,
): Promise<{ data: any; sessionId: string | null }> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
  };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;

  const resp = await fetch(MCP_URL, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });

  const newSession = resp.headers.get("mcp-session-id") || sessionId;
  const contentType = resp.headers.get("content-type") || "";
  let data: any;
  if (contentType.includes("text/event-stream")) {
    data = parseSseData(await resp.text());
  } else {
    data = await resp.json();
  }
  return { data, sessionId: newSession };
}

async function ensureMcpSession(): Promise<string> {
  if (mcpSessionId) return mcpSessionId;

  const initPayload = {
    jsonrpc: "2.0",
    id: nextMcpId(),
    method: "initialize",
    params: {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "realtime-mode", version: "2.0.0" },
    },
  };

  const { data, sessionId } = await mcpPost(initPayload);
  if (sessionId) {
    mcpSessionId = sessionId;
    console.log("MCP session initialized:", sessionId);

    // Send initialized notification
    await fetch(MCP_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Mcp-Session-Id": sessionId,
      },
      body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized", params: {} }),
    });
  }

  return mcpSessionId || "";
}

async function callMcpTool(toolName: string, args: Record<string, unknown>): Promise<string> {
  try {
    const sessionId = await ensureMcpSession();
    const payload = {
      jsonrpc: "2.0",
      id: nextMcpId(),
      method: "tools/call",
      params: { name: toolName, arguments: args },
    };

    const { data, sessionId: newSession } = await mcpPost(payload, sessionId);
    if (newSession) mcpSessionId = newSession;

    if (data?.result?.content?.[0]?.text) {
      return data.result.content[0].text;
    }
    if (data?.error) return JSON.stringify(data.error);
    return data ? JSON.stringify(data) : '{"error": "empty response"}';
  } catch (e: any) {
    console.error("MCP call failed:", e);
    mcpSessionId = null;
    return JSON.stringify({ error: e.message });
  }
}

// --- Tool definitions ---

export const gmailSearch = tool({
  name: "gmail_search",
  description: "Search Craig's Gmail inbox.",
  parameters: z.object({
    query: z.string().describe("Gmail search query"),
    max_results: z.number().optional().describe("Max results to return"),
  }),
  execute: async (args) => callMcpTool("gmail_search", args),
});

export const gmailRead = tool({
  name: "gmail_read",
  description: "Read a specific email by message ID.",
  parameters: z.object({
    message_id: z.string().describe("Gmail message ID"),
  }),
  execute: async (args) => callMcpTool("gmail_read", args),
});

export const gmailSend = tool({
  name: "gmail_send",
  description: "Send an email from Craig's Gmail (chaconstruction@gmail.com).",
  parameters: z.object({
    to: z.string().describe("Recipient email address"),
    subject: z.string().describe("Email subject"),
    body: z.string().describe("Email body text"),
    reply_to_id: z.string().optional().describe("Message ID to reply to"),
  }),
  execute: async (args) => callMcpTool("gmail_send", args),
});

export const gmailDraft = tool({
  name: "gmail_draft",
  description: "Create a draft email in Craig's Gmail.",
  parameters: z.object({
    to: z.string().describe("Recipient email address"),
    subject: z.string().describe("Email subject"),
    body: z.string().describe("Email body text"),
  }),
  execute: async (args) => callMcpTool("gmail_draft", args),
});

export const calendarListEvents = tool({
  name: "calendar_list_events",
  description: "List upcoming Google Calendar events.",
  parameters: z.object({
    calendar_id: z.string().optional().describe("Calendar ID"),
    max_results: z.number().optional().describe("Max events to return"),
    query: z.string().optional().describe("Search query to filter events"),
  }),
  execute: async (args) => callMcpTool("calendar_list_events", args),
});

export const calendarCreateEvent = tool({
  name: "calendar_create_event",
  description: "Create a Google Calendar event.",
  parameters: z.object({
    summary: z.string().describe("Event title"),
    start: z.string().describe("Start time (ISO 8601)"),
    end: z.string().describe("End time (ISO 8601)"),
    description: z.string().optional().describe("Event description"),
    attendees: z.array(z.string()).optional().describe("List of attendee email addresses"),
    calendar_id: z.string().optional().describe("Calendar ID"),
  }),
  execute: async (args) => callMcpTool("calendar_create_event", args),
});

export const calendarUpdateEvent = tool({
  name: "calendar_update_event",
  description: "Update an existing Google Calendar event.",
  parameters: z.object({
    event_id: z.string().describe("Event ID to update"),
    calendar_id: z.string().optional().describe("Calendar ID"),
    summary: z.string().optional().describe("New event title"),
    start: z.string().optional().describe("New start time (ISO 8601)"),
    end: z.string().optional().describe("New end time (ISO 8601)"),
    description: z.string().optional().describe("New description"),
  }),
  execute: async (args) => callMcpTool("calendar_update_event", args),
});

export const webSearch = tool({
  name: "web_search",
  description:
    "Search the web using Brave Search. Use for current events, prices, facts, or anything not in memory.",
  parameters: z.object({
    query: z.string().describe("Search query"),
    max_results: z.number().optional().describe("Max results to return"),
  }),
  execute: async (args) => callMcpTool("web_search", args),
});

export const delegateToCratchit = tool({
  name: "delegate_to_cratchit",
  description: "Delegate a complex task to the full Cratchit OpenClaw agent for multi-step work.",
  parameters: z.object({
    task: z.string().describe("Description of the task to delegate"),
  }),
  execute: async (args) => callMcpTool("delegate_to_cratchit", args),
});

export const TOOL_LABELS: Record<string, string> = {
  gmail_search: "Searching Gmail...",
  gmail_read: "Reading email...",
  gmail_send: "Sending email...",
  gmail_draft: "Creating draft...",
  calendar_list_events: "Checking calendar...",
  calendar_create_event: "Creating event...",
  calendar_update_event: "Updating event...",
  delegate_to_cratchit: "Delegating task...",
  web_search: "Searching the web...",
};

export const allTools = [
  gmailSearch,
  gmailRead,
  gmailSend,
  gmailDraft,
  calendarListEvents,
  calendarCreateEvent,
  calendarUpdateEvent,
  webSearch,
  delegateToCratchit,
];
