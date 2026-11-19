// Agent view page: run a hook or a read-only MCP tool against the real store and show the exact
// text the model receives, with the size, the budget, what was cut, the emulated session state
// and the preparation the run performed. Every surface, parameter and host comes from the
// catalog; this page names none of them except the hook labels.
// see design/superpowers/specs/2026-11-17-portal-design.md (D10)
import { post } from "../api.js";
import { ErrorNotice } from "../components/banners.js";
import { useResource } from "../components/use-resource.js";
import { html, useEffect, useRef, useState } from "../ui.js";

// Words for the hook events; an event the catalog adds later is shown by its own name.
const HOOK_WORDS = {
  session_start: ["SessionStart", "the memory map at session start"],
  pre_tool_use: ["PreToolUse", "records added when the agent opens a file"],
  agent_brief: ["Agent / Task brief", "the brief added to an Agent or Task call"],
  subagent_start: ["SubagentStart", "the brief a subagent starts with"],
  stop: ["Stop", "the capture nudge at the end of a turn"],
  mcp_instructions: ["MCP server instructions", "the memory map a host reads from the server's instructions"],
};
const KINDS = [
  ["hooks", "Hooks"],
  ["tools", "MCP tools"],
];
const VIEWS = [
  ["exact", "Exact text"],
  ["readable", "Readable"],
];

const hookTitle = (event) => HOOK_WORDS[event]?.[0] ?? event;
const count = (n) => n.toLocaleString("en-US");

// ---- Forms from JSON Schema parameters ----

// The schemas are Pydantic's: a property is a plain type, an enum, or `anyOf` with `null`.
function fieldKind(schema) {
  const variants = (schema.anyOf ?? [schema]).filter((variant) => variant.type !== "null");
  const array = variants.find((variant) => variant.type === "array");
  if (array) return array.items?.type === "string" ? "lines" : "json";
  if (variants.length !== 1) return "json";
  const [only] = variants;
  if (only.enum) return "enum";
  if (only.type === "boolean") return "boolean";
  if (only.type === "integer" || only.type === "number") return "number";
  return only.type === "string" ? "text" : "json";
}

function jsonVariant(schema) {
  return (schema.anyOf ?? [schema]).find((variant) => variant.type !== "null");
}

function describeJson(schema) {
  const variant = jsonVariant(schema);
  if (variant.type === "array") return `a JSON list${variant.items?.type ? ` of ${variant.items.type}s` : ""}`;
  return `a JSON ${variant.type ?? "value"}`;
}

/** Whether a parsed JSON value has the type the schema allows, down to the items of a list. */
function matches(value, schema) {
  if (schema.anyOf) return schema.anyOf.some((variant) => matches(value, variant));
  const type = Array.isArray(value) ? "array" : value === null ? "null" : typeof value;
  if (schema.type === "integer") return Number.isInteger(value);
  if (schema.type && schema.type !== type) return false;
  return type !== "array" || !schema.items || value.every((item) => matches(item, schema.items));
}

function enumOptions(schema) {
  return (schema.anyOf ?? [schema]).find((variant) => variant.enum)?.enum ?? [];
}

function initialValues(schema) {
  const values = {};
  for (const [name, property] of Object.entries(schema.properties ?? {})) {
    const kind = fieldKind(property);
    if (kind === "boolean") values[name] = property.default ?? false;
    else if (kind === "enum") values[name] = property.default ?? enumOptions(property)[0] ?? "";
    else values[name] = "";
  }
  return values;
}

/** The arguments the form describes, or the first problem with it. Empty fields are omitted. */
function readArgs(schema, values) {
  const args = {};
  for (const [name, property] of Object.entries(schema.properties ?? {})) {
    const value = values[name];
    const kind = fieldKind(property);
    if (kind === "boolean") {
      if (value !== (property.default ?? false)) args[name] = value;
    } else if (kind === "lines") {
      // Each line goes as typed: a path with a leading space is a different path.
      const lines = value.split("\n").filter((line) => line.trim() !== "");
      if (lines.length) args[name] = lines;
    } else if (value.trim() !== "") {
      if (kind === "number") {
        const number = Number(value);
        const whole = jsonVariant(property).type === "integer";
        // A whole number is checked as typed (5 or 5.0): a long or fractional one would be rounded by Number().
        if (Number.isNaN(number) || (whole && !(/^[+-]?\d+(\.0+)?$/.test(value.trim()) && Number.isSafeInteger(number)))) {
          return { problem: `${name} must be ${whole ? "a whole number" : "a number"}.` };
        }
        args[name] = number;
      } else if (kind === "json") {
        let parsed;
        try {
          parsed = JSON.parse(value);
        } catch {
          return { problem: `${name} must be valid JSON.` };
        }
        // The catalog's schema says what the value may be; the server rejects anything else.
        if (!matches(parsed, property)) return { problem: `${name} must be ${describeJson(property)}.` };
        if (parsed !== null) args[name] = parsed;
      } else {
        // Enter in a one-line field adds a newline nobody meant; the ends of the text are cut, not its middle.
        args[name] = value.replace(/^\n+|\n+$/g, "");
      }
    }
    if ((schema.required ?? []).includes(name) && !(name in args)) {
      return { problem: `${name} is required.` };
    }
  }
  return { args };
}

function callLabel(name, args) {
  const shown = Object.entries(args).map(([key, value]) => `${key}=${JSON.stringify(value)}`);
  return shown.length ? `${name}(${shown.join(", ")})` : name;
}

function Field({ id, name, schema, value, onInput }) {
  const kind = fieldKind(schema);
  const props = { id, value, onInput: (event) => onInput(event.target.value) };
  const hint = {
    lines: "one per line",
    json: "JSON",
    number: schema.default === undefined ? "number" : `number, default ${schema.default}`,
  }[kind];
  if (kind === "boolean") {
    return html`<label class="check av-check">
      <input type="checkbox" checked=${value} onChange=${(event) => onInput(event.target.checked)} />
      <span class="mono">${name}</span>
    </label>`;
  }
  return html`<div class="av-field">
    <label for=${id}><span class="mono">${name}</span>${hint && html` <span class="muted av-hint">· ${hint}</span>`}</label>
    ${kind === "enum"
      ? html`<select ...${props} class="av-input">
          ${enumOptions(schema).map((option) => html`<option key=${option} value=${option}>${option}</option>`)}
        </select>`
      : kind === "number"
        ? html`<input ...${props} class="av-input" type="number" placeholder=${schema.default == null ? "" : String(schema.default)} />`
        // Text may be several lines (an Agent prompt), so it is a textarea that grows with its content.
        : html`<textarea ...${props} class=${`av-input${kind === "text" ? "" : " mono"}`} rows=${kind === "text" ? 1 : 2} placeholder=${schema.default == null ? "" : String(schema.default)}></textarea>`}
  </div>`;
}

function SchemaForm({ prefix, schema, values, onChange }) {
  const entries = Object.entries(schema.properties ?? {});
  if (!entries.length) return html`<p class="muted small">This surface takes no parameters.</p>`;
  return entries.map(
    ([name, property]) => html`<${Field}
      key=${name}
      id=${`${prefix}-${name}`}
      name=${name}
      schema=${property}
      value=${values[name]}
      onInput=${(value) => onChange({ ...values, [name]: value })}
    />`,
  );
}

// ---- Tabs ----

/** A tablist: arrow keys move between tabs, and only the selected tab is in the Tab order. */
function Tabs({ label, prefix, options, value, onChange, className = "" }) {
  const move = (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const index = options.findIndex(([key]) => key === value);
    const [next] = options[(index + step + options.length) % options.length];
    onChange(next);
    document.getElementById(`${prefix}-tab-${next}`)?.focus();
  };
  return html`<div class=${`av-tabs ${className}`} role="tablist" aria-label=${label} onKeyDown=${move}>
    ${options.map(
      ([key, text]) => html`<button
        key=${key}
        id=${`${prefix}-tab-${key}`}
        type="button"
        role="tab"
        aria-selected=${key === value}
        aria-controls=${`${prefix}-panel`}
        tabindex=${key === value ? 0 : -1}
        class="av-tab"
        onClick=${() => onChange(key)}
      >${text}</button>`,
    )}
  </div>`;
}

// ---- The result ----

const escapePattern = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** The text with each record id in `ids` as a link to its card. */
function Linked({ text, ids }) {
  if (!ids.length) return text;
  const pattern = new RegExp(`(${[...ids].sort((a, b) => b.length - a.length).map(escapePattern).join("|")})`);
  return text
    .split(pattern)
    .map((part, index) =>
      index % 2 ? html`<a key=${index} href=${`#/records/${encodeURIComponent(part)}`}>${part}</a>` : part,
    );
}

function Preparation({ prepared }) {
  const moved = prepared.adopted_moves;
  return html`${moved > 0 &&
    html`<p class="av-note" role="status">
      ${`Preparing this view ran the same sync the agent's next call would run; it updated ${moved} ${moved === 1 ? "record's" : "records'"} code locations.`}
    </p>`}
    ${prepared.skipped_reason &&
    html`<p class="av-note av-note-warn" role="status">
      No sync was run for this view. ${prepared.skipped_reason}
    </p>`}`;
}

function Chips({ response }) {
  const { chars, budget, cut } = response;
  return html`<div class="av-chips">
    <span class="av-chip">${count(chars)} ${chars === 1 ? "character" : "characters"}</span>
    ${budget != null && html`<span class="av-chip">Budget ${count(budget)}${cut ? "" : ": nothing cut"}</span>`}
    ${cut && html`<span class="av-chip av-chip-warn">${cut}</span>`}
    <span class="av-chip av-chip-warn">Not counted in sidegraph-stats</span>
  </div>`;
}

function Emulation({ label, response }) {
  const [view, setView] = useState("exact");
  const { text, emitted, not_emitted_reason, assumptions, firing_conditions, ids_in_text } = response;
  return html`<div class="av-result">
    <div class="av-title">
      <h2>What the model receives</h2>
      <span class="mono muted av-call">${label}</span>
    </div>
    <${Chips} response=${response} />
    <${Preparation} prepared=${response.prepared} />
    <div class="av-state">
      <div>
        <h3>Emulated session</h3>
        <ul class="av-list">${assumptions.map((item) => html`<li key=${item}>${item}</li>`)}</ul>
      </div>
      ${firing_conditions.length > 0 &&
      html`<div>
        <h3>Emitted only when</h3>
        <ul class="av-list">${firing_conditions.map((item) => html`<li key=${item}>${item}</li>`)}</ul>
      </div>`}
    </div>
    ${!emitted
      ? html`<p class="av-note av-note-warn" role="status">
          Nothing is sent to the model here${not_emitted_reason ? `: ${not_emitted_reason}` : "."}
        </p>`
      : html`<${Tabs} label="View" prefix="av-view" options=${VIEWS} value=${view} onChange=${setView} />
          <div id="av-view-panel" role="tabpanel" aria-labelledby=${`av-view-tab-${view}`}>
            ${view === "exact"
              ? html`<pre class="av-text mono">${text}</pre>`
              : html`<div class="av-readable"><${Linked} text=${text} ids=${ids_in_text} /></div>`}
          </div>`}
  </div>`;
}

// ---- The page ----

function Picker({ kind, catalog, host, selected, onSelect }) {
  if (kind === "tools") {
    return html`<div class="av-picker">
      <span class="muted small">Read-only tools only. Tools that write are not offered here.</span>
      ${catalog.tools.map(
        (tool) => html`<button
          key=${tool.name}
          type="button"
          class="av-item mono"
          aria-pressed=${selected === tool.name}
          onClick=${() => onSelect(tool.name)}
        >${tool.name}</button>`,
      )}
    </div>`;
  }
  return html`<div class="av-picker">
    <span class="muted small">What the agent gets without asking. Each hook runs with the real renderer on a made-up event.</span>
    ${catalog.hook_surfaces.map((surface) => {
      const wired = surface.wired_on.includes(host);
      return html`<button
        key=${surface.event}
        type="button"
        class="av-item"
        aria-pressed=${selected === surface.event}
        onClick=${() => onSelect(surface.event)}
      >
        <strong>${hookTitle(surface.event)}</strong>
        ${HOOK_WORDS[surface.event] && html`<span class="muted small">${HOOK_WORDS[surface.event][1]}</span>`}
        ${!wired && html`<span class="av-tag">Not wired on ${host}</span>`}
      </button>`;
    })}
  </div>`;
}

function AgentView({ catalog, session }) {
  const [kind, setKind] = useState("hooks");
  const [chosen, setChosen] = useState({});
  const [drafts, setDrafts] = useState({});
  const [hostChoice, setHostChoice] = useState(null);
  const [problem, setProblem] = useState(null);
  const [pending, setPending] = useState(false);
  const [outcome, setOutcome] = useState(null);
  const [failure, setFailure] = useState(null);
  const running = useRef(false);
  const result = useRef(null);
  // Stacked on a narrow screen the result is below the form: bring a new one into view.
  useEffect(() => {
    if (result.current && (outcome || failure)) result.current.scrollIntoView({ block: "nearest" });
  }, [outcome, failure]);

  // The session's host; Claude Code when the portal runs from the command line (D10).
  const host = hostChoice ?? [session.host_family, "claude"].find((family) => catalog.host_families.includes(family)) ?? catalog.host_families[0];
  const surfaces = kind === "tools" ? catalog.tools : catalog.hook_surfaces;
  const idOf = (surface) => (kind === "tools" ? surface.name : surface.event);
  const surface = surfaces.find((item) => idOf(item) === chosen[kind]) ?? surfaces[0];
  const schema = surface?.params_schema ?? {};
  const draftKey = surface && `${kind}:${idOf(surface)}`;
  const values = drafts[draftKey] ?? initialValues(schema);

  // One run at a time. A failed run keeps the last result on screen, and Try again sends the
  // failed request itself, whatever the form shows by then.
  const send = async (request) => {
    if (running.current) return;
    running.current = true;
    setPending(true);
    try {
      const response =
        request.route === "tool"
          ? await post("/api/agent-view/tool", request.body)
          : await post("/api/agent-view/hook", request.body);
      setOutcome({ label: request.label, response });
      setFailure(null);
    } catch (error) {
      setFailure({ error, request });
    } finally {
      running.current = false;
      setPending(false);
    }
  };

  const run = () => {
    const read = readArgs(schema, values);
    setProblem(read.problem ?? null);
    if (read.problem || !surface) return;
    const name = idOf(surface);
    const tool = kind === "tools";
    send({
      route: tool ? "tool" : "hook",
      body: tool
        ? { name, args: read.args, host_family: host }
        : { event: name, params: read.args, host_family: host },
      label: `${callLabel(tool ? name : hookTitle(name), read.args)} · ${host}`,
    });
  };

  return html`<div class="av-layout">
    <aside class="panel av-side" aria-label="What to emulate">
      <${Tabs} label="Kind" prefix="av-kind" options=${KINDS} value=${kind} onChange=${(next) => {
        setKind(next);
        setProblem(null);
      }} />
      <div id="av-kind-panel" role="tabpanel" aria-labelledby=${`av-kind-tab-${kind}`} class="av-side-body">
        <div class="av-field">
          <label for="av-host">Host</label>
          <select id="av-host" class="av-input" value=${host} onChange=${(event) => setHostChoice(event.target.value)}>
            ${catalog.host_families.map((family) => html`<option key=${family} value=${family}>${family}</option>`)}
          </select>
          <span class="muted small">Which host's wiring to emulate. It starts as this session's host.</span>
        </div>
        ${surface
          ? html`<${Picker}
                kind=${kind}
                catalog=${catalog}
                host=${host}
                selected=${idOf(surface)}
                onSelect=${(id) => {
                  setChosen({ ...chosen, [kind]: id });
                  setProblem(null);
                }}
              />
              <div class="av-form">
                <${SchemaForm}
                  key=${draftKey}
                  prefix=${`av-${draftKey.replace(":", "-")}`}
                  schema=${schema}
                  values=${values}
                  onChange=${(next) => {
                    setDrafts({ ...drafts, [draftKey]: next });
                    setProblem(null);
                  }}
                />
              </div>`
          : html`<p class="muted">Nothing to emulate in this category.</p>`}
      </div>
      ${problem && html`<p class="av-problem" role="alert">${problem}</p>`}
      <button type="button" class="btn btn-primary av-run" aria-disabled=${pending || !surface} onClick=${run}>
        ${pending ? "Running…" : "Run"}
      </button>
    </aside>
    <section class="av-main" aria-label="Result" ref=${result}>
      ${pending && html`<p class="muted" role="status">Running. This can take a few seconds while the store is prepared.</p>`}
      ${failure &&
      html`<${ErrorNotice} error=${failure.error} onRetry=${() => send(failure.request)} />
        <p class="muted small">Failed: <span class="mono av-call">${failure.request.label}</span>.${outcome && " The result below is from the last run that worked."}</p>`}
      ${outcome?.response && html`<${Emulation} key=${outcome.label} label=${outcome.label} response=${outcome.response} />`}
      ${!outcome && !failure && !pending &&
      html`<p class="empty">Pick a hook or a tool and press Run. The page shows exactly the text a session would be given.</p>`}
    </section>
  </div>`;
}

export default function Page({ session }) {
  const catalog = useResource("/api/agent-view/catalog");
  return html`<section class="page av-page">
    <h1>Agent view</h1>
    ${catalog.error && html`<${ErrorNotice} error=${catalog.error} onRetry=${catalog.reload} />`}
    ${!catalog.data && !catalog.error && html`<p class="muted">Loading…</p>`}
    ${catalog.data && html`<${AgentView} catalog=${catalog.data} session=${session} />`}
  </section>`;
}
