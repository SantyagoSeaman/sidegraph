import { Plugin } from "@opencode/plugin";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const RUNTIME_SOURCE = "git+https://github.com/SantyagoSeaman/sidegraph.git@main";
const SKILLS = fileURLToPath(new URL("./plugin/sidegraph/skills/", import.meta.url));

async function loadSkills() {
  const entries = await readdir(SKILLS, { withFileTypes: true });
  const names = entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name).sort();
  if (!names.length) throw new Error(`Sidegraph has no packaged skills in ${SKILLS}`);
  return Promise.all(names.map(async (directory) => {
    const path = join(SKILLS, directory, "SKILL.md");
    try {
      const text = await readFile(path, "utf8");
      const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(text);
      if (!frontmatter) throw new Error("missing YAML frontmatter");
      const metadata = Bun.YAML.parse(frontmatter[1]);
      if (!metadata || typeof metadata.name !== "string" || !metadata.name.trim()
          || typeof metadata.description !== "string" || !metadata.description.trim()) {
        throw new Error("frontmatter requires nonempty name and description strings");
      }
      return {
        id: `sidegraph-${directory}`, name: metadata.name, description: metadata.description,
        path, content: text.slice(frontmatter[0].length),
      };
    } catch (error) {
      throw new Error(`Cannot load Sidegraph skill ${path}: ${error.message}`, { cause: error });
    }
  }));
}

export default Plugin.define({
  id: "sidegraph",
  async setup(ctx) {
    // Validate every packaged input before creating either registration.
    const skills = await loadSkills();
    const skillRegistration = await ctx.skill.transform((editor) => {
      for (const skill of skills) {
        if (editor.get(skill.id) === undefined) editor.add(skill);
      }
    });
    try {
      const mcpRegistration = await ctx.mcp.transform((editor) => {
        if (editor.get("sidegraph") === undefined) {
          editor.set("sidegraph", {
            type: "local",
            command: ["uvx", "--from", RUNTIME_SOURCE, "sidegraph-mcp", "--repo-root"],
            environment: { SIDEGRAPH_DIR: ".sidegraph", SIDEGRAPH_GRAPH: "graphify-out/graph.json" },
          });
        }
      });
      return async () => {
        try { await mcpRegistration.dispose(); }
        finally { await skillRegistration.dispose(); }
      };
    } catch (error) {
      await skillRegistration.dispose();
      throw error;
    }
  },
});
