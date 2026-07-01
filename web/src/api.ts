// Thin client for the agent API. In dev, Vite proxies /api to FastAPI, so these
// stay same-origin. Set VITE_API_BASE to point elsewhere (e.g. a deployed API).

import type {
  Account,
  ChatResponse,
  DecisionResponse,
  Transaction,
  TransferState,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export function sendMessage(
  message: string,
  sessionId: string | null
): Promise<ChatResponse> {
  return post<ChatResponse>("/api/chat", { message, session_id: sessionId });
}

export function decide(
  sessionId: string,
  referenceId: string,
  approved: boolean
): Promise<DecisionResponse> {
  return post<DecisionResponse>("/api/transfer/decision", {
    session_id: sessionId,
    reference_id: referenceId,
    approved,
  });
}

// Look up a transfer's current state so the UI can reconnect its approval card
// after a page reload. Returns null if the workflow is unknown (e.g. expired and
// aged out of retention).
export async function fetchTransferState(
  referenceId: string
): Promise<TransferState | null> {
  const res = await fetch(`${BASE}/api/transfer/${encodeURIComponent(referenceId)}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Could not load transfer (${res.status})`);
  return res.json() as Promise<TransferState>;
}

export async function fetchAccounts(): Promise<Account[]> {
  const res = await fetch(`${BASE}/api/accounts`);
  if (!res.ok) throw new Error(`Could not load accounts (${res.status})`);
  const data = (await res.json()) as { accounts: Account[] };
  return data.accounts;
}

export async function fetchTransactions(): Promise<Transaction[]> {
  const res = await fetch(`${BASE}/api/transactions`);
  if (!res.ok) throw new Error(`Could not load transactions (${res.status})`);
  const data = (await res.json()) as { transactions: Transaction[] };
  return data.transactions;
}
