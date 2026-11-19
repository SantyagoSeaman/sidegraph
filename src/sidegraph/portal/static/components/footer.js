// The footer: how many files changed under the store. The portal never commits.
// see design/superpowers/specs/2026-11-17-portal-design.md (D13)
import { html } from "../ui.js";

/** `storeChanges` is null when the store is not in a git checkout: then there is no footer. */
export function Footer({ storeChanges }) {
  if (storeChanges === null || storeChanges === undefined) return null;
  const files = storeChanges === 1 ? "1 file" : `${storeChanges} files`;
  return html`<footer class=${`footer ${storeChanges ? "footer-changed" : ""}`}>
    <strong>${files} changed under the store</strong>
    <span class="muted">The portal never commits. Commit them in a pull request.</span>
  </footer>`;
}
