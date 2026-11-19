// The live stream client: a one-use ticket, an EventSource, and our own reconnect.
// EventSource would retry with its first, already used ticket and then stop for good, so every
// error closes it and a new ticket is fetched with backoff (1, 2, 4 ... 30 s).
// see design/superpowers/specs/2026-11-17-portal-design.md (D11)
import { onClosed, post } from "./api.js";

const MAX_BACKOFF_S = 30;
const STALE_POLLS = 2;
// This many failed reconnects in a row mean the process is gone, not that the network blinked.
const STOP_AFTER_FAILURES = 2;

const listeners = new Set();
let source = null;
let timer = null;
let backoff = 1;
let started = false;
let stopped = false;
// The first open also refetches: anything that changed since the page read its data is missed.
let refetchOnOpen = true;
// Bumped whenever a pending ticket request stops being the current attempt.
let attempt = 0;
let staleTimer = null;
let pollSeconds = 5;
let state = "idle";
let failures = 0;

function emit(event) {
  for (const listener of listeners) listener(event);
}

function setState(next) {
  if (next !== "stale") clearTimeout(staleTimer);
  if (state === next) return;
  state = next;
  emit({ type: "state", state });
}

/** Subscribe to {type: "changed", what} | {type: "refetch"} | {type: "state", state}. */
export function onLive(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** "idle" | "connecting" | "live" | "reconnecting" | "stopped" | "stale" | "paused" | "closed". */
export function liveState() {
  return state;
}

function disconnect() {
  attempt += 1;
  clearTimeout(timer);
  timer = null;
  if (source) {
    source.close();
    source = null;
  }
}

function retry() {
  disconnect();
  if (stopped || document.hidden) return;
  refetchOnOpen = true;
  failures += 1;
  setState(failures >= STOP_AFTER_FAILURES ? "stopped" : "reconnecting");
  timer = setTimeout(connect, backoff * 1000);
  backoff = Math.min(backoff * 2, MAX_BACKOFF_S);
}

async function connect() {
  timer = null;
  if (stopped || document.hidden || source) return;
  if (state !== "reconnecting" && state !== "stopped") setState("connecting");
  const mine = ++attempt;
  let ticket;
  try {
    ({ ticket } = await post("/api/live-ticket", {}, { user: false }));
  } catch {
    // A 401 already closed the portal for this tab; anything else is retried with backoff.
    // An attempt that was replaced meanwhile must not tear down its replacement.
    if (mine === attempt) retry();
    return;
  }
  if (mine !== attempt || stopped || document.hidden || source) return;
  const stream = new EventSource(`/live?ticket=${encodeURIComponent(ticket)}`);
  source = stream;
  stream.onopen = () => {
    failures = 0;
    backoff = 1;
    setState("live");
    if (refetchOnOpen) {
      refetchOnOpen = false;
      emit({ type: "refetch" });
    }
  };
  stream.addEventListener("changed", (event) => {
    setState("live");
    let what = null;
    try {
      what = JSON.parse(event.data).what;
    } catch {
      // A frame without a usable body still says "something changed".
    }
    emit({ type: "changed", what });
  });
  stream.addEventListener("stale", () => {
    // The server sends no "recovered" event: the notice lapses after a couple of poll ticks.
    setState("stale");
    clearTimeout(staleTimer);
    staleTimer = setTimeout(() => state === "stale" && setState("live"), STALE_POLLS * pollSeconds * 1000);
  });
  stream.addEventListener("closing", () => stop("stopped"));
  stream.onerror = () => {
    if (source === stream) retry();
  };
}

function stop(next = "closed") {
  stopped = true;
  disconnect();
  setState(next);
}

function onVisibility() {
  if (stopped) return;
  if (document.hidden) {
    disconnect();
    setState("paused");
    return;
  }
  // Check at once, and again when the stream opens: a change in between would be missed.
  backoff = 1;
  refetchOnOpen = true;
  emit({ type: "refetch" });
  connect();
}

/** Open the stream once the page has data to keep fresh. Safe to call again. */
export function start(seconds) {
  if (started) return;
  started = true;
  pollSeconds = seconds ?? pollSeconds;
  onClosed(stop);
  document.addEventListener("visibilitychange", onVisibility);
  connect();
}
