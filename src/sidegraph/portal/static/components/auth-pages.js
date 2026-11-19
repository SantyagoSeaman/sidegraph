// Pages shown instead of the app: no token in this tab, and the portal has closed.
// see design/superpowers/specs/2026-11-17-portal-design.md (D1, D3)
import { html } from "../ui.js";

function Notice({ title, children }) {
  return html`<main class="auth-page">
    <h1>${title}</h1>
    <p>${children}</p>
  </main>`;
}

/** A tab with no token makes no API call. */
export function NoTokenPage() {
  return html`<${Notice} title="Open the portal from its link">
    Open the portal from the link in the agent's reply or the terminal.
  <//>`;
}

/** The token was refused or the server said it is closing; nothing is requested again. */
export function ClosedPage() {
  return html`<${Notice} title="The portal you opened has closed">
    Ask the agent to open it again, or run <code>sidegraph-portal</code>.
  <//>`;
}
