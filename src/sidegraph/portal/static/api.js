// The portal's HTTP client: the token header, the activity header, contract errors.
// see design/superpowers/specs/2026-11-17-portal-design.md (D3, D11) and
// design/superpowers/specs/2026-11-17-portal-api-contract.md

const TOKEN_KEY = "sidegraph.portal.token";
const RECENT_INPUT_MS = 1500;
// The code of an error response that carries no JSON envelope (a proxy, a crash).
const STATUS_CODES = { 401: "unauthorized", 403: "bad_origin", 404: "not_found", 421: "bad_host", 503: "busy" };

let memoryToken = null;
let lastInputAt = -Infinity;
let closed = false;
let busyWaiting = 0;
const closedListeners = new Set();
const busyListeners = new Set();

// Wording for contract error codes; `details.reason` refines `not_found` and `refused`.
const MESSAGES = {
  unauthorized: "The portal you opened has closed.",
  bad_host: "Open the portal at the address printed when it started.",
  bad_origin: "This request must come from the portal page.",
  not_found: "Not found.",
  not_current: "There is a newer version of this record.",
  pending_successor: "A proposed replacement is waiting in the queue.",
  pending_proposal: "This record is still a proposal. Review it in the queue.",
  pending_evidence: "Proposed evidence is waiting in the queue.",
  evidence_conflict: "The version or its evidence changed. Review the record before trying again.",
  operation_in_progress: "Another save is already running for this draft.",
  op_key_reused: "This save was already used for different text. Start again.",
  invalid: "The request was not valid.",
  busy: "The store is busy. Try again in a moment.",
  refused: "The store refused this request.",
  network: "Could not reach the portal.",
  closed: "The portal has closed.",
  server: "The portal hit an error.",
};
const NOT_FOUND_REASONS = {
  not_on_this_branch: "This record is not on this branch.",
  snippet_unavailable: "No source snippet is available for this anchor.",
};
const REFUSED_REASONS = {
  domain_accepted_meanwhile: "This domain was accepted in the meantime.",
  opposite_verdict: "This item was already accepted, rejected or dropped.",
  not_proposed: "This item is no longer proposed.",
  unknown_id: "This item is no longer available.",
  evidence_conflict: "The version or its evidence changed. Review the record before trying again.",
  ratification_failed: "Reopen the queue and check this item's status before retrying.",
  no_repository_root: "Sidegraph found no repository root, so code cannot be shown.",
};

export class ApiError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/** The message to show a person for a failed request. */
export function describeError(error) {
  if (!(error instanceof ApiError)) return "Something went wrong.";
  const { code, details } = error;
  // The server's `message` is display text: it answers where our wording has no entry.
  if (code === "not_found") return NOT_FOUND_REASONS[details.reason] ?? error.message;
  if (code === "refused") return REFUSED_REASONS[details.reason] ?? error.message;
  if (code === "invalid") return error.message || MESSAGES.invalid;
  return MESSAGES[code] ?? error.message ?? "Something went wrong.";
}

/** Move `#t=<token>` from the address into this tab's sessionStorage, then drop the fragment. */
export function captureToken() {
  const token = new URLSearchParams(location.hash.slice(1)).get("t");
  if (token) {
    memoryToken = token;
    try {
      sessionStorage.setItem(TOKEN_KEY, token);
    } catch {
      // Storage can be blocked; the token then lives only until this page reloads.
    }
    history.replaceState(null, "", location.pathname + location.search);
  }
  return hasToken();
}

function token() {
  if (memoryToken) return memoryToken;
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function hasToken() {
  return Boolean(token());
}

// A click or keystroke marks the next requests as the person's own (D1 idle timeout).
for (const type of ["click", "keydown", "input"]) {
  document.addEventListener(type, () => (lastInputAt = performance.now()), true);
}

/** Stop all further requests: the portal is gone or this tab's token is not valid. */
export function markClosed() {
  if (closed) return;
  closed = true;
  for (const listener of closedListeners) listener();
}

export function isClosed() {
  return closed;
}

export function onClosed(listener) {
  closedListeners.add(listener);
  return () => closedListeners.delete(listener);
}

/** Subscribe to "a request is waiting out a busy store": listener(count). */
export function onBusy(listener) {
  busyListeners.add(listener);
  return () => busyListeners.delete(listener);
}

function setBusy(delta) {
  busyWaiting += delta;
  for (const listener of busyListeners) listener(busyWaiting);
}

function recentInput() {
  return performance.now() - lastInputAt < RECENT_INPUT_MS;
}

function url(path, params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === false || value === "") continue;
    query.set(key, value === true ? "true" : String(value));
  }
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}

async function send(method, path, { params, body, user, signal } = {}) {
  if (closed) throw new ApiError(0, "closed", MESSAGES.closed);
  const headers = { "X-Sidegraph-Token": token() ?? "" };
  if (user ?? method === "POST") headers["X-Sidegraph-Activity"] = "user";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(url(path, params), {
      method,
      headers,
      signal,
      credentials: "omit",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "network", MESSAGES.network);
  }
  let payload = null;
  let parsed = true;
  try {
    payload = await response.json();
  } catch (error) {
    if (error.name === "AbortError" || signal?.aborted) throw new DOMException("Aborted", "AbortError");
    parsed = false;
  }
  if (response.ok) {
    // Success bodies are JSON; a cut or empty one is a failed read, never "no data".
    if (!parsed) throw new ApiError(0, "network", MESSAGES.network);
    return payload;
  }
  const detail = payload?.error ?? {};
  const code = detail.code ?? STATUS_CODES[response.status] ?? (response.status >= 500 ? "server" : "invalid");
  const error = new ApiError(
    response.status,
    code,
    detail.message ?? MESSAGES[code] ?? MESSAGES.invalid,
    detail.details ?? {},
  );
  if (error.code === "unauthorized") markClosed();
  throw error;
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

/**
 * GET JSON. A busy store (503) keeps the caller's last data and retries with backoff;
 * `onBusy` tells the shell to say so. `options.user` forces or clears the activity header.
 */
export async function get(path, params, options = {}) {
  // Decided once for the first attempt; a retry after backoff is never the person's own.
  let user = options.user ?? recentInput();
  let delay = 1000;
  let waiting = false;
  try {
    for (;;) {
      try {
        return await send("GET", path, { ...options, user, params });
      } catch (error) {
        if (!(error instanceof ApiError) || error.code !== "busy") throw error;
        if (!waiting) setBusy(1);
        waiting = true;
        user = false;
        await sleep(delay, options.signal);
        delay = Math.min(delay * 2, 10000);
      }
    }
  } finally {
    if (waiting) setBusy(-1);
  }
}

/** POST JSON. Clicks and keystrokes call this, so it counts as activity unless told not to. */
export function post(path, body = {}, options = {}) {
  return send("POST", path, { ...options, body });
}
