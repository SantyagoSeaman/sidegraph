// Data hooks for pages: fetch a route, keep the last data while a refetch runs or fails,
// and refetch when the live stream reports a change. A busy store (503) is retried by api.js.
import { get, onBusy } from "../api.js";
import { liveState, onLive } from "../live.js";
import { useEffect, useRef, useState } from "../ui.js";

/**
 * `watch` lists the live `what` values (records, graph, advice, sync) that refetch this
 * resource; a reconnect or a tab becoming visible refetches it regardless.
 * Returns {data, error, loading, reload}.
 */
export function useResource(path, { params, watch = [], enabled = true } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: enabled });
  const control = useRef(null);
  const key = `${path}?${JSON.stringify(params ?? {})}`;

  const load = (options = {}) => {
    control.current?.abort();
    const abort = new AbortController();
    control.current = abort;
    setState((previous) => ({ ...previous, error: null, loading: true }));
    // Only the newest request may change the state; an aborted or superseded one is dropped.
    get(path, params, { ...options, signal: abort.signal }).then(
      (data) => control.current === abort && setState({ data, error: null, loading: false }),
      (error) => {
        if (error.name === "AbortError" || control.current !== abort) return;
        setState((previous) => ({ data: previous.data, error, loading: false }));
      },
    );
  };

  useEffect(() => {
    if (!enabled) return undefined;
    load();
    return () => control.current?.abort();
  }, [key, enabled]);

  useEffect(
    () =>
      onLive((event) => {
        if (!enabled) return;
        if (event.type === "refetch" || (event.type === "changed" && watch.includes(event.what))) {
          load({ user: false });
        }
      }),
    [key, enabled, watch.join(",")],
  );

  return { ...state, reload: () => load() };
}

/** The live stream's state, re-rendering on change. */
export function useLiveState() {
  const [state, setState] = useState(liveState());
  useEffect(() => {
    const off = onLive((event) => event.type === "state" && setState(event.state));
    // The state may have changed between the first render and this subscription.
    setState(liveState());
    return off;
  }, []);
  return state;
}

/** How many requests are waiting out a busy store. */
export function useBusy() {
  const [count, setCount] = useState(0);
  useEffect(() => onBusy(setCount), []);
  return count;
}
