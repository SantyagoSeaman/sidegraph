// Execute the plugin against a narrow SDK double. Native Bun YAML/SDK have separate proof.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { SourceTextModule, SyntheticModule } from "node:vm";

const [entry, fixture] = process.argv.slice(2);
const options = JSON.parse(readFileSync(fixture, "utf8"));
const callbacks = {};
const events = [];
const disposed = [];
const skills = new Map(Object.entries(options.skills ?? {}));
const servers = new Map(Object.entries(options.servers ?? {}));
let error;
let cleanupError;
globalThis.Bun = {
  YAML: {
    parse(header) {
      if (!(header in options.yaml)) throw new Error("synthetic YAML parse failure");
      return structuredClone(options.yaml[header]);
    },
  },
};
const domain = (name) => ({
  async transform(callback) {
    events.push(`register:${name}`);
    if (options.failRegistration === name) throw new Error(`registration:${name}`);
    callbacks[name] = callback;
    return {
      async dispose() {
        disposed.push(name);
        if (options.failDispose === name) throw new Error(`dispose:${name}`);
      },
    };
  },
});
const module = new SourceTextModule(readFileSync(entry, "utf8"), {
  identifier: pathToFileURL(entry).href,
  initializeImportMeta(meta) { meta.url = pathToFileURL(entry).href; },
});
await module.link(async (specifier) => {
  let values;
  if (specifier === "@opencode/plugin") values = { Plugin: { define: (value) => value } };
  else if (specifier.startsWith("node:")) values = await import(specifier);
  else throw new Error(`Undeclared plugin dependency: ${specifier}`);
  return new SyntheticModule(Object.keys(values), function () {
    for (const [name, value] of Object.entries(values)) this.setExport(name, value);
  });
});
let definition;
try {
  await module.evaluate();
  definition = module.namespace.default;
  const cleanup = await definition.setup({ skill: domain("skill"), mcp: domain("mcp") });
  const skillEditor = {
    get: (id) => skills.get(id),
    add(value) {
      if (skills.has(value.id)) throw new Error(`duplicate skill: ${value.id}`);
      skills.set(value.id, value);
    },
  };
  const mcpEditor = { get: (id) => servers.get(id), set: (id, value) => servers.set(id, value) };
  for (let i = 0; i < (options.replays ?? 2); i++) {
    callbacks.skill?.(skillEditor);
    callbacks.mcp?.(mcpEditor);
  }
  try { await cleanup(); } catch (cause) { cleanupError = String(cause); }
} catch (cause) { error = String(cause); }
console.log(JSON.stringify({
  id: definition?.id, skills: Object.fromEntries(skills), servers: Object.fromEntries(servers),
  events, disposed, error, cleanupError,
}));
