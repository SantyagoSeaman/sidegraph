// Theme: the server renders the first page with `th-dark` or `th-light` on <html>, so there
// is no flash; a change is saved through the settings route and survives a reload.
// see design/superpowers/specs/2026-11-17-portal-design.md (D12)
import { post } from "./api.js";

const root = document.documentElement;

export function currentTheme() {
  return root.classList.contains("th-light") ? "light" : "dark";
}

export function applyTheme(theme) {
  root.classList.remove("th-dark", "th-light");
  root.classList.add(`th-${theme}`);
}

/** Show `theme` at once and save it; on failure show the previous theme again and throw. */
export async function setTheme(theme) {
  const previous = currentTheme();
  applyTheme(theme);
  try {
    await post("/api/settings", { theme });
  } catch (error) {
    applyTheme(previous);
    throw error;
  }
}
