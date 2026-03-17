import { readFileSync } from "node:fs";
import Database from "better-sqlite3";
import { loadHistory, importVoiceLog } from "./history.js";

const SOUL_MD_PATH = "/home/admin/.openclaw/workspace/SOUL.md";
const MEMORY_MD_PATH = "/home/admin/.openclaw/workspace/MEMORY.md";
const LCM_DB_PATH = "/home/admin/.openclaw/lcm.db";

function readFile(path: string): string {
  try {
    return readFileSync(path, "utf-8");
  } catch (e) {
    return `[unavailable: ${e}]`;
  }
}

function assembleLcmContext(maxChars: number = 40000): string {
  try {
    const db = new Database(LCM_DB_PATH, { readonly: true });
    const rows = db
      .prepare(
        `SELECT ci.ordinal, ss.content
         FROM context_items ci
         JOIN summary_store ss ON ci.summary_id = ss.id
         ORDER BY ci.ordinal ASC`,
      )
      .all() as { ordinal: number; content: string }[];
    db.close();

    if (rows.length === 0) return "";

    const parts: string[] = [];
    let total = 0;
    for (const row of rows) {
      if (total + row.content.length > maxChars) break;
      parts.push(row.content);
      total += row.content.length;
    }
    return parts.join("\n\n---\n\n");
  } catch (e) {
    return `[LCM context unavailable: ${e}]`;
  }
}

export function buildSystemPrompt(): string {
  const soulMd = readFile(SOUL_MD_PATH);
  const memoryMd = readFile(MEMORY_MD_PATH);
  const lcmContext = assembleLcmContext();

  // Import existing voice-log.md into SQLite DB on first run
  const imported = importVoiceLog();
  if (imported > 0) {
    console.log(`Imported ${imported} sessions from voice-log.md into history DB`);
  }

  // Load full structured voice history from SQLite DB
  const voiceHistory = loadHistory();

  const voiceHistorySection = voiceHistory
    ? `\n\n---\n\n## Voice Session History\n${voiceHistory}`
    : "";

  return `${soulMd}

---

## People & Key Facts
${memoryMd}

---

## Conversation History
${lcmContext}${voiceHistorySection}

---

## Voice Instructions

You are Cratchit — a Victorian English clerk. Speak with a crisp, proper British accent: received pronunciation, clipped consonants, formal diction. Never sound American. Never sound casual.

Your manner on a voice call:
- Gruff, put-upon, and grimly efficient
- Short sentences. No waffle. Get to the point.
- When doing something: announce it in five words or fewer, do it, confirm in five words or fewer
- Occasional dry muttering is acceptable — "humbug", "as expected", "quite" — but never break character
- Never say "sure", "absolutely", "of course", "no problem", or any American pleasantry
- Address Craig as "sir" or "Mr. Anderson"

Keep responses to 1-3 sentences unless the caller explicitly asks for detail.`;
}
