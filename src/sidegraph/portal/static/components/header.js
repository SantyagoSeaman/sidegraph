// The header: brand, the main navigation with the queue count, the graph page's search, and the theme toggle.
// see design/superpowers/specs/2026-11-17-portal-design.md (D12, D13)
import { describeError } from "../api.js";
import { currentTheme, setTheme } from "../theme.js";
import { SearchBox } from "./search-box.js";
import { html, useState } from "../ui.js";

const SUN =
  "M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4";

function ThemeToggle() {
  const [theme, setThemeState] = useState(currentTheme());
  const [failure, setFailure] = useState(null);
  const [saving, setSaving] = useState(false);
  const next = theme === "dark" ? "light" : "dark";
  const toggle = async () => {
    if (saving) return;
    setFailure(null);
    setThemeState(next);
    setSaving(true);
    try {
      await setTheme(next);
    } catch (error) {
      setThemeState(theme);
      setFailure(describeError(error));
    }
    setSaving(false);
  };
  return html`<button
      type="button"
      class="btn theme-toggle"
      aria-label=${`Switch to the ${next} theme`}
      aria-disabled=${saving}
      onClick=${toggle}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        ${theme === "dark"
          ? html`<circle cx="12" cy="12" r="4" /><path d=${SUN} />`
          : html`<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />`}
      </svg>
      ${next === "light" ? "Light" : "Dark"}
    </button>
    ${failure && html`<span class="muted" role="alert">${failure}</span>`}`;
}

/** `pages` is app.js's registry: {key, label, badge?}; `queueCount` feeds the Queue badge. */
export function Header({ session, pages, route, queueCount }) {
  const where = [session.repo_name, session.branch].filter(Boolean).join(" · ");
  return html`<header class="header">
    <div class="brand">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true">
        <circle cx="6" cy="6" r="3" /><circle cx="18" cy="8" r="3" /><circle cx="10" cy="18" r="3" />
        <path d="M8.5 7.5l6.5 0.5M7.5 8.5l1.8 6.8M16 10.5l-4.5 5.5" />
      </svg>
      <span class="brand-name">Sidegraph</span>
      <span class="mono muted small">${location.host}</span>
    </div>
    <nav aria-label="Main" class="nav">
      ${pages.map(
        (page) => html`<a
          key=${page.key}
          href=${`#/${page.key}`}
          aria-current=${route.page === page.key ? "page" : undefined}
          class="nav-link"
        >
          ${page.label}
          ${page.badge && queueCount > 0 && html`<span class="count-badge" aria-label=${`${queueCount} waiting`}>${queueCount}</span>`}
        </a>`,
      )}
    </nav>
    <div class="header-spacer"></div>
    ${route.page === "graph" && html`<${SearchBox} />`}
    <${ThemeToggle} />
    ${where && html`<span class="small muted">${where}</span>`}
  </header>`;
}
