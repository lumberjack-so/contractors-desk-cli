import Database from "better-sqlite3";
import { readFileSync, appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

const DB_PATH = "/home/admin/.openclaw/voice-history.db";
const VOICE_LOG_PATH = "/home/admin/.openclaw/workspace/voice-log.md";

export interface TranscriptTurn {
  role: string;
  text: string;
}

let db: Database.Database | null = null;

function getDb(): Database.Database {
  if (!db) {
    mkdirSync(dirname(DB_PATH), { recursive: true });
    db = new Database(DB_PATH);
    db.pragma("journal_mode = WAL");
    db.exec(`
      CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        transcript TEXT NOT NULL
      )
    `);
  }
  return db;
}

/** Save a session transcript to both SQLite and voice-log.md. */
export function saveSession(turns: TranscriptTurn[]): void {
  if (turns.length === 0) return;

  const ts = new Date().toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");

  // Save to SQLite
  const d = getDb();
  d.prepare("INSERT INTO sessions (timestamp, transcript) VALUES (?, ?)").run(
    ts,
    JSON.stringify(turns),
  );

  // Append to voice-log.md for backwards compatibility
  const lines = [`## Voice Session — ${ts}\n`];
  for (const turn of turns) {
    lines.push(`**${turn.role}:** ${turn.text}`);
  }
  lines.push("\n---\n");
  const block = lines.join("\n");

  try {
    mkdirSync(dirname(VOICE_LOG_PATH), { recursive: true });
    appendFileSync(VOICE_LOG_PATH, block);
  } catch (e) {
    console.error("Failed to append voice-log.md:", e);
  }
}

/** Load all sessions from the DB. Summarise oldest if total exceeds charLimit. */
export function loadHistory(charLimit: number = 80000): string {
  const d = getDb();
  const rows = d
    .prepare("SELECT id, timestamp, transcript FROM sessions ORDER BY id ASC")
    .all() as { id: number; timestamp: string; transcript: string }[];

  if (rows.length === 0) return "";

  const sections: { id: number; text: string }[] = [];
  for (const row of rows) {
    let turns: TranscriptTurn[];
    try {
      turns = JSON.parse(row.transcript);
    } catch {
      continue;
    }
    const lines = [`### Session — ${row.timestamp}`];
    for (const t of turns) {
      lines.push(`**${t.role}:** ${t.text}`);
    }
    sections.push({ id: row.id, text: lines.join("\n") });
  }

  let total = sections.reduce((sum, s) => sum + s.text.length, 0);

  // If within limit, return everything
  if (total <= charLimit) {
    return sections.map((s) => s.text).join("\n\n---\n\n");
  }

  // Otherwise, summarise oldest sessions until we fit
  const result: string[] = [];
  let remaining = charLimit;

  // Reserve space for recent sessions (work backwards)
  const recentSections: string[] = [];
  for (let i = sections.length - 1; i >= 0; i--) {
    if (remaining - sections[i].text.length < charLimit * 0.2 && i > 0) {
      break;
    }
    recentSections.unshift(sections[i].text);
    remaining -= sections[i].text.length;
  }

  // Summarise older sessions
  const oldCount = sections.length - recentSections.length;
  if (oldCount > 0) {
    const oldTurns: string[] = [];
    for (let i = 0; i < oldCount; i++) {
      const lines = sections[i].text.split("\n");
      const header = lines[0];
      const userLines = lines.filter((l) => l.startsWith("**user:**"));
      const summary =
        header + "\n" + (userLines.length > 0 ? userLines.join("\n") : "(no user input captured)");
      oldTurns.push(summary);
    }
    const summaryBlock = `### Earlier Sessions (summarised — ${oldCount} sessions)\n\n${oldTurns.join("\n\n")}`;
    if (summaryBlock.length < remaining) {
      result.push(summaryBlock);
    }
  }

  result.push(...recentSections);
  return result.join("\n\n---\n\n");
}

/** Import existing voice-log.md entries into the DB (run once on first start). */
export function importVoiceLog(): number {
  const d = getDb();
  const count = (d.prepare("SELECT COUNT(*) as c FROM sessions").get() as { c: number }).c;
  if (count > 0) return 0;

  let content: string;
  try {
    content = readFileSync(VOICE_LOG_PATH, "utf-8");
  } catch {
    return 0;
  }

  const sessionRegex = /## Voice Session — (.+)\n([\s\S]*?)(?=\n---|\n## Voice Session|$)/g;
  let match: RegExpExecArray | null;
  let imported = 0;

  const insert = d.prepare("INSERT INTO sessions (timestamp, transcript) VALUES (?, ?)");
  const tx = d.transaction(() => {
    while ((match = sessionRegex.exec(content)) !== null) {
      const ts = match[1].trim();
      const body = match[2].trim();
      const turns: TranscriptTurn[] = [];

      for (const line of body.split("\n")) {
        const m = line.match(/^\*\*(\w+):\*\*\s*(.+)$/);
        if (m) {
          turns.push({ role: m[1], text: m[2] });
        }
      }

      if (turns.length > 0) {
        insert.run(ts, JSON.stringify(turns));
        imported++;
      }
    }
  });
  tx();
  return imported;
}
