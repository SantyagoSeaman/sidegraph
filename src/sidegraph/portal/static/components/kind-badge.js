// The kind of a record as a badge: a decision's kind, or "fact" / "domain".
import { html } from "../ui.js";

/** `row` needs `type` and, for a decision, `kind`: a RecordRow or a card's own record. */
export function KindBadge({ row }) {
  const label = row.type === "decision" ? row.kind : row.type;
  return html`<span class=${`kind-badge kind-${label}`}>${label.toUpperCase()}</span>`;
}
