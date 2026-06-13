// Conversation store for the Ask-the-trace assistant. Conversations are kept in memory and
// mirrored to localStorage under a versioned key so they survive reloads. Each conversation is
// tagged with its source ('public' = the SYFI pool, 'user' = the uploaded trace) and titled from
// its first user message. There is no seed data — history starts empty.

export type AskHistorySource = 'public' | 'user';

/** A rendered plot attached to an assistant turn (the model's generated image). */
export interface AskStoredImage {
  /** Inline data URL produced by the executor / sandbox. */
  dataUrl: string;
  /** Artifact path or a friendly fallback, used as the caption. */
  caption: string;
}

export interface AskStoredMessage {
  role: 'user' | 'assistant';
  /** The user prompt (role 'user') or the assistant's markdown-ish answer (role 'assistant'). */
  text: string;
  /** Assistant-only: rendered plots. */
  images?: AskStoredImage[];
  /** Assistant-only: the joined tool code shown in the "Show the code" fold. */
  code?: string;
}

export interface Conversation {
  id: string;
  source: AskHistorySource;
  title: string | null;
  /** A short relative timestamp for the history drawer (e.g. 'Now'). */
  ts: string;
  messages: AskStoredMessage[];
}

const STORAGE_KEY = 'ask-trace-history-v1';

function uid(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `conv-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** Minimal, defensive shape check so a corrupt/legacy blob can't crash the assistant. */
function isConversation(value: unknown): value is Conversation {
  if (!value || typeof value !== 'object') return false;
  const c = value as Record<string, unknown>;
  return (
    typeof c.id === 'string' &&
    (c.source === 'public' || c.source === 'user') &&
    (c.title === null || typeof c.title === 'string') &&
    typeof c.ts === 'string' &&
    Array.isArray(c.messages)
  );
}

/** In-memory list of conversations, newest first. */
export class AskHistory {
  private conversations: Conversation[] = [];

  constructor() {
    this.load();
  }

  list(): Conversation[] {
    return this.conversations;
  }

  byId(id: string | null): Conversation | undefined {
    return id == null ? undefined : this.conversations.find((c) => c.id === id);
  }

  /** Create a fresh, empty conversation for `source` and put it at the front. */
  create(source: AskHistorySource): Conversation {
    const conv: Conversation = { id: uid(), source, title: null, ts: 'Now', messages: [] };
    this.conversations.unshift(conv);
    this.save();
    return conv;
  }

  remove(id: string): void {
    this.conversations = this.conversations.filter((c) => c.id !== id);
    this.save();
  }

  /** Persist any mutation a caller made to a conversation it holds a reference to. */
  touch(): void {
    this.save();
  }

  private load(): void {
    try {
      const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed: unknown = JSON.parse(raw);
      if (Array.isArray(parsed)) this.conversations = parsed.filter(isConversation);
    } catch {
      this.conversations = [];
    }
  }

  private save(): void {
    try {
      globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(this.conversations));
    } catch {
      /* storage full or unavailable — keep working from memory */
    }
  }
}

/** Title a conversation from its first user message (truncated). */
export function titleFromMessage(text: string): string {
  const trimmed = text.trim();
  return trimmed.length > 48 ? `${trimmed.slice(0, 46)}…` : trimmed;
}
