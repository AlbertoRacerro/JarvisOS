// BUBU-2: DOM-first description of the visible page for the model, and the reverse lookup for "[[point:<id>]]".
// Stable ids come from data-jarvis-target (designTargets.ts); everything else gets an ephemeral "el-<n>" id
// written to the element as data-bubu-ref. Pure helpers (truncate, rankTargets) stay free of DOM and local imports.

export type UiTarget = { id: string; role: string; name: string; text?: string };
export type UiContext = { route: string; title: string; targets: UiTarget[] };

export const MAX_TARGETS = 150;
export const MAX_TEXT = 160;
const MAX_CANDIDATES = 4000;
const TARGET_ATTR = "data-jarvis-target";
const REF_ATTR = "data-bubu-ref";
const BUBU_UI = "[data-bubu-ui], #bubu-launcher";

export const INTERACTIVE_SELECTOR = [
  "button", "a[href]", 'input:not([type="hidden"])', "select", "textarea", "summary",
  '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]', '[role="treeitem"]',
  '[role="checkbox"]', '[role="radio"]', '[role="switch"]', '[role="combobox"]', '[role="option"]',
].join(", ");
const MEANINGFUL_SELECTOR = `${INTERACTIVE_SELECTOR}, h1, h2, h3, th, [role="columnheader"], svg text`;

const clean = (s: string) => s.replace(/\s+/g, " ").trim();

export function truncate(value: string, max: number = MAX_TEXT): string {
  const s = clean(value ?? "");
  return s.length > max ? `${s.slice(0, Math.max(0, max - 1)).trimEnd()}…` : s;
}

export type Rankable = { stable: boolean; inViewport: boolean };

/** Stable (or already referenced) targets first, then in-viewport elements, then the rest; each tier keeps DOM order. */
export function rankTargets<T extends Rankable>(list: readonly T[], cap: number = MAX_TARGETS): T[] {
  const tier = (t: Rankable) => (t.stable ? 0 : t.inViewport ? 1 : 2);
  return list.map((item, index) => ({ item, index })).sort((a, b) => tier(a.item) - tier(b.item) || a.index - b.index).slice(0, Math.max(0, cap)).map((x) => x.item);
}

const attrSelector = (name: string, value: string) => `[${name}="${value.replace(/["\\]/g, "\\$&")}"]`;

export function isBubuUi(el: Element): boolean { return !!el.closest(BUBU_UI); }

export function isRenderable(el: Element): boolean {
  if (el.closest('[aria-hidden="true"], [hidden], [inert]')) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return false;
  const s = getComputedStyle(el);
  return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
}

function inViewport(r: DOMRect): boolean { return r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth; }

function roleOf(el: Element): string {
  const explicit = el.getAttribute("role");
  if (explicit) return explicit.split(/\s+/)[0];
  const tag = el.tagName.toLowerCase();
  if (tag === "a") return "link";
  if (tag === "button" || tag === "summary") return "button";
  if (tag === "select") return "combobox";
  if (tag === "textarea") return "textbox";
  if (tag === "th") return "columnheader";
  if (/^h[1-6]$/.test(tag)) return "heading";
  if (tag === "text") return "label";
  if (tag === "nav") return "navigation";
  if (tag === "aside") return "complementary";
  if (tag === "section" || tag === "form") return "region";
  if (tag === "input") {
    const type = ((el as HTMLInputElement).type || "text").toLowerCase();
    if (type === "checkbox" || type === "radio") return type;
    if (type === "range") return "slider";
    if (type === "number") return "spinbutton";
    if (type === "button" || type === "submit" || type === "reset") return "button";
    return "textbox";
  }
  return "element";
}

function accessibleName(el: Element): string {
  const aria = el.getAttribute("aria-label");
  if (aria && clean(aria)) return clean(aria);
  const by = el.getAttribute("aria-labelledby");
  if (by) {
    const text = by.split(/\s+/).map((id) => el.ownerDocument.getElementById(id)?.textContent ?? "").join(" ");
    if (clean(text)) return clean(text);
  }
  const tag = el.tagName.toLowerCase();
  if (tag === "input" || tag === "select" || tag === "textarea") {
    const control = el as HTMLInputElement;
    const label = clean(Array.from(control.labels ?? []).map((l) => l.textContent ?? "").join(" "));
    if (label) return label;
    if (tag === "input" && /^(button|submit|reset)$/i.test(control.type) && control.value) return clean(control.value);
    const hint = control.getAttribute("placeholder") || control.getAttribute("title") || control.getAttribute("name");
    return hint ? clean(hint) : "";
  }
  const own = clean((el as HTMLElement).innerText ?? el.textContent ?? "");
  if (own) return own;
  const alt = el.querySelector("img[alt]")?.getAttribute("alt");
  return clean(alt || el.getAttribute("title") || "");
}

function stateText(el: Element): string | undefined {
  const parts: string[] = [];
  const tag = el.tagName.toLowerCase();
  if (tag === "select") {
    const opt = (el as HTMLSelectElement).selectedOptions?.[0]?.textContent;
    if (opt && clean(opt)) parts.push(clean(opt));
  } else if (tag === "textarea" || tag === "input") {
    const input = el as HTMLInputElement;
    const type = (input.type || "text").toLowerCase();
    if (type === "checkbox" || type === "radio") { if (input.checked) parts.push("checked"); }
    else if (!/^(password|button|submit|reset|file|hidden)$/.test(type) && input.value) parts.push(input.value);
  }
  if (el.getAttribute("aria-selected") === "true") parts.push("selected");
  if (el.getAttribute("aria-pressed") === "true") parts.push("pressed");
  const current = el.getAttribute("aria-current");
  if (current && current !== "false") parts.push("current");
  const expanded = el.getAttribute("aria-expanded");
  if (expanded === "true") parts.push("expanded"); else if (expanded === "false") parts.push("collapsed");
  if ((el as HTMLButtonElement).disabled || el.getAttribute("aria-disabled") === "true") parts.push("disabled");
  const text = truncate(parts.join(", "));
  return text || undefined;
}

let refCounter = 0;
/** The element's ephemeral id, assigned (and written to data-bubu-ref) on first use. */
export function refFor(el: Element): string {
  const existing = el.getAttribute(REF_ATTR);
  if (existing) return existing;
  const doc = el.ownerDocument;
  let id: string;
  do { id = `el-${++refCounter}`; } while (doc.querySelector(attrSelector(REF_ATTR, id)));
  el.setAttribute(REF_ATTR, id);
  return id;
}

const humanize = (id: string) => id.replace(/[._:-]+/g, " ").trim();

function buildTarget(id: string, el: Element, fallbackName: string): UiTarget {
  const role = roleOf(el);
  const target: UiTarget = { id, role: truncate(role), name: truncate(accessibleName(el) || fallbackName || role) };
  const text = stateText(el);
  if (text) target.text = text;
  return target;
}

/** The element that a click on `el` should stand for: nearest interactive, stable or already-referenced ancestor. */
export function pickableElement(el: Element): Element {
  return el.closest(`${INTERACTIVE_SELECTOR}, [${TARGET_ATTR}], [${REF_ATTR}]`) ?? el;
}

export function describeElement(el: Element): UiTarget {
  const stable = el.getAttribute(TARGET_ATTR);
  return stable ? buildTarget(stable, el, humanize(stable)) : buildTarget(refFor(el), el, "");
}

type Candidate = Rankable & { el: Element; stableId: string | null };

export function collectPageContext(root: ParentNode = document): UiContext {
  const nodes = Array.from(root.querySelectorAll(`[${TARGET_ATTR}], [${REF_ATTR}], ${MEANINGFUL_SELECTOR}`)).slice(0, MAX_CANDIDATES);
  const candidates: Candidate[] = [];
  const stableSeen = new Set<string>();
  for (const el of nodes) {
    if (isBubuUi(el) || !isRenderable(el)) continue;
    const stableId = el.getAttribute(TARGET_ATTR);
    if (stableId) { if (stableSeen.has(stableId)) continue; stableSeen.add(stableId); }
    else if (!el.hasAttribute(REF_ATTR) && !accessibleName(el)) continue;
    candidates.push({ el, stableId, stable: !!stableId || el.hasAttribute(REF_ATTR), inViewport: inViewport(el.getBoundingClientRect()) });
  }
  const kept = rankTargets(candidates);
  const keptEls = new Set(kept.map((c) => c.el));
  // Drop marks on elements that fell out of the picture (stale), keep those still described.
  root.querySelectorAll(`[${REF_ATTR}]`).forEach((el) => { if (!keptEls.has(el)) el.removeAttribute(REF_ATTR); });
  const targets = kept.map((c) => c.stableId ? buildTarget(c.stableId, c.el, humanize(c.stableId)) : buildTarget(refFor(c.el), c.el, ""));
  return { route: `${location.pathname}${location.search}`, title: truncate(document.title), targets };
}

export function resolveTarget(id: string, root: ParentNode = document): Element | null {
  const found = Array.from(root.querySelectorAll(`${attrSelector(TARGET_ATTR, id)}, ${attrSelector(REF_ATTR, id)}`)).filter((el) => !isBubuUi(el));
  return found.find(isRenderable) ?? found[0] ?? null;
}
