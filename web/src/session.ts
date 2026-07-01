// A stable per-browser session id, generated on first load and persisted in
// localStorage. It is sent in the body of API calls so the backend can keep each
// browser's agent conversation separate (see agent/server.py). This is a
// conversation key, not an auth credential — the shared demo bank is the same
// for everyone.

const KEY = "session_id";

function generateId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  // Fallback for browsers without crypto.randomUUID.
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function getSessionId(): string {
  let id: string | null = null;
  try {
    id = localStorage.getItem(KEY);
    if (!id) {
      id = generateId();
      localStorage.setItem(KEY, id);
    }
  } catch {
    // localStorage can be unavailable (private mode, blocked storage). Fall back
    // to an ephemeral id for this page load so the app still works.
    id = id || generateId();
  }
  return id;
}

// Reference id of a transfer awaiting approval, remembered across reloads so the
// UI can reconnect to the still-running workflow and re-render its card.
const PENDING_KEY = "pending_transfer_ref";

export function getPendingRef(): string | null {
  try {
    return localStorage.getItem(PENDING_KEY);
  } catch {
    return null;
  }
}

export function setPendingRef(ref: string): void {
  try {
    localStorage.setItem(PENDING_KEY, ref);
  } catch {
    /* storage unavailable; reconnection just won't survive a reload */
  }
}

export function clearPendingRef(): void {
  try {
    localStorage.removeItem(PENDING_KEY);
  } catch {
    /* nothing to do */
  }
}
