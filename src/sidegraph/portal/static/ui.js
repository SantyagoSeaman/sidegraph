// One import for the page code: Preact, its hooks, and htm bound to Preact's `h`.
// Markup is written as tagged templates: html`<p>${text}</p>`. Text is always a child
// value, never an HTML string, so record text cannot become markup.
import { h, render, Fragment } from "./vendor/preact.mjs";
import { useCallback, useEffect, useMemo, useRef, useState } from "./vendor/hooks.mjs";
import htm from "./vendor/htm.mjs";

const html = htm.bind(h);

export { Fragment, h, html, render, useCallback, useEffect, useMemo, useRef, useState };
