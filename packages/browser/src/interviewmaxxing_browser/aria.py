"""Conservative, DOM-current ARIA listbox support, including custom menu widgets.

The observer is read-only and knows no ATS selectors. Selection-only single
comboboxes with one uniquely owned listbox and a complete unambiguous option set are
supported directly. Menu controls whose options only exist while they are open
(React selects, Rippling-style comboboxes) are probed once per document: opened,
enumerated through their own ``aria-controls`` listbox, closed again and verified
unchanged. A role-less menu button over a hidden proxy ``<select>`` (BambooHR's Fabric
select) is such a control too: its menu of menu items is found through ``data-menu-id``.
Lookups (location, state and country searches) are recognised by an empty menu. Every page script here is fixed trusted code, never provider-generated.
Bindings are ephemeral observations of a document, not reusable selector recipes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .driver import PageDriver
    from .snapshot import DomControl, DomSnapshot

# Shared by inspect.js and the action/readback helpers. No DOM or global writes.
ARIA_HELPERS = r"""
const ariaText = (v) => String(v || '').replace(/\s+/g, ' ').trim();
// aria-hidden as the page authored it. While a popup is open, an overlay manager (Floating
// UI, the aria-hidden package behind Radix) marks everything else aria-hidden, with
// data-aria-hidden: Ashby's location lookup hides the whole form that way while its
// suggestions show. That hides nothing on screen, so such a mark counts only while a modal
// dialog is open.
let ariaModal = null;
const ariaModalShown = () => {
  if (ariaModal === null) {
    ariaModal = [...document.querySelectorAll('[aria-modal="true"],dialog,[role="dialog"],[role="alertdialog"]')]
      .some((d) => (d.tagName !== 'DIALOG' || d.open) && d.getClientRects().length > 0 && getComputedStyle(d).visibility !== 'hidden');
  }
  return ariaModal;
};
const ariaHiddenAttr = (n) => n.getAttribute('aria-hidden') === 'true'
  && !(n.getAttribute('data-aria-hidden') === 'true' && !ariaModalShown());
// The nearest element from n up that is aria-hidden as authored (ariaHiddenAttr), or null.
const ariaMarked = (n) => {
  for (let x = n; x && x.nodeType === 1; x = x.parentElement) if (ariaHiddenAttr(x)) return x;
  return null;
};
const ariaVisible = (el) => {
  if (!el || !el.isConnected) return false;
  for (let n = el; n; n = n.parentElement) {
    if (n.hidden || n.hasAttribute('inert') || ariaHiddenAttr(n)) return false;
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
  }
  const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0;
};
const ariaUniqueId = (id) => id && document.querySelectorAll('#' + CSS.escape(id)).length === 1;
const ariaRefs = (el) => [...new Set(['aria-controls','aria-owns'].flatMap(
  (a) => (el.getAttribute(a) || '').split(/\s+/).filter(Boolean)))];
const ariaSafe = (el) => {
  if (!el || !ariaVisible(el) || el.getAttribute('role') !== 'combobox') return false;
  if (el.disabled || el.matches(':disabled') || el.closest('[aria-disabled=true]') || el.isContentEditable) return false;
  if (el.closest('a[href]') || el.matches('input[type=submit],input[type=image],input[type=reset]')) return false;
  // A button defaults to submit when its type is omitted, even with a widget role.
  if (el.tagName === 'BUTTON' && el.type !== 'button') return false;
  if (el.getAttribute('aria-multiselectable') === 'true' || el.multiple) return false;
  if (el.matches('input,textarea') && !(el.readOnly || el.getAttribute('aria-readonly') === 'true')) return false;
  return !el.querySelector('input,textarea,select,[contenteditable=true]');
};
const ariaControl = (el) => ({
  tag: el.tagName, id: el.id, name: el.getAttribute('name') || '',
  role: el.getAttribute('role'), readonly: !!el.readOnly,
  aria_readonly: el.getAttribute('aria-readonly'), autocomplete: el.getAttribute('aria-autocomplete'),
  labelledby: el.getAttribute('aria-labelledby'), label: el.getAttribute('aria-label'),
  labels: el.labels ? [...el.labels].map(l => ariaText(l.textContent)) : [],
  form: el.form || el.closest('form') ? (el.form || el.closest('form')).outerHTML.split('>')[0] : '',
});
const ariaRead = (el, options, box) => {
  const selected = box ? [...box.querySelectorAll('[role=option][aria-selected=true]')]
    .filter(o => o.closest('[role=listbox]') === box) : [];
  const display = ariaText(el.matches('input,textarea') ? el.value : el.textContent);
  const value = el.getAttribute('aria-valuetext');
  const matches = options.filter(o => o.label === display || (value !== null && o.label === ariaText(value)));
  if (matches.length !== 1) return '';
  const match = matches[0];
  if (selected.length > 1 || (box && box.querySelector('[role=option][aria-selected]') && selected.length !== 1)) return '';
  if (selected.length === 1 && ariaText(selected[0].getAttribute('aria-label') || selected[0].textContent) !== match.label) return '';
  return match.value;
};
const ariaObserve = (el) => {
  if (!ariaSafe(el) || !ariaUniqueId(el.id)) return null;
  const refs = ariaRefs(el);
  if (refs.length !== 1 || !ariaUniqueId(refs[0])) return null;
  const box = document.getElementById(refs[0]);
  if (box.getAttribute('role') !== 'listbox' || box.getAttribute('aria-multiselectable') === 'true' || box.closest('[aria-disabled=true]')) return null;
  const owners = [...document.querySelectorAll('[aria-controls],[aria-owns]')].filter(n => ariaRefs(n).includes(refs[0]));
  if (owners.length !== 1 || owners[0] !== el) return null;
  if (box.querySelector('[role=listbox],[role=group],[role=radiogroup],[role=radio],[role=checkbox],input,select,textarea')) return null;
  const nodes = [...box.querySelectorAll('[role=option]')];
  if (!nodes.length || nodes.length > 500) return null;
  const options = [];
  for (const node of nodes) {
    if (node.closest('[role=listbox]') !== box || node.querySelector('[role=option]')) return null;
    if (node.closest('a[href],button:not([type=button])') || node.matches('input,[contenteditable=true]') ||
        node.querySelector('a[href],button,input,select,textarea,[role=button],[contenteditable=true]')) return null;
    if (!ariaUniqueId(node.id)) return null;
    const label = ariaText(node.getAttribute('aria-label') || node.textContent);
    // The observed label is the canonical value when ARIA exposes no value.
    // Never infer a private application/backend value from a generated node id.
    const value = node.hasAttribute('data-value') ? node.getAttribute('data-value') : label;
    if (!label || !value || options.some(o => o.value === value || o.label === label)) return null;
    const setsize = node.getAttribute('aria-setsize');
    if (setsize !== null && Number(setsize) !== nodes.length) return null; // virtualized/partial set
    options.push({value, label, selector: '#' + CSS.escape(node.id), disabled: node.disabled === true || node.getAttribute('aria-disabled') === 'true',
      selected: node.getAttribute('aria-selected') === 'true'});
  }
  if (options.filter(o => o.selected).length > 1) return null;
  return {version: 1, origin: String(performance.timeOrigin), url: location.href,
    selector: '#' + CSS.escape(el.id), owner: refs[0], control: ariaControl(el), options,
    expanded: el.getAttribute('aria-expanded') === 'true', visible: ariaVisible(box),
    value: ariaRead(el, options, box)};
};
const ariaSame = (got, expected) => got && expected &&
  got.origin === expected.origin && got.url === expected.url && got.selector === expected.selector &&
  got.owner === expected.owner && JSON.stringify(got.control) === JSON.stringify(expected.control) &&
  JSON.stringify(got.options.map(({value,label,disabled}) => ({value,label,disabled}))) ===
  JSON.stringify(expected.options.map(({value,label,disabled}) => ({value,label,disabled})));
// --- menu controls whose options exist only while open ---------------------------------
const comboFieldSel = 'input:not([type=hidden]),select,textarea,[role=combobox],[role=radiogroup],' +
  '[role=checkbox],[role=switch],[role=textbox],[role=spinbutton],[role=slider],[contenteditable=true]';
const comboPopup = (el) => (el.getAttribute('aria-haspopup') || '').toLowerCase();
// A role-less menu button standing in for a hidden native <select> (BambooHR's Fabric
// select: a button[aria-haspopup] naming its menu by data-menu-id beside an aria-hidden,
// tabindex -1 select that holds only the current value and that the <label> names). The
// button is the control and the select its proxy; null for any other element.
const menuProxy = (el) => {
  if (!el || el.tagName !== 'BUTTON' || el.type !== 'button' || el.getAttribute('role')) return null;
  if (!['true', 'menu', 'listbox'].includes(comboPopup(el))) return null;
  const menuId = el.getAttribute('data-menu-id') || '';
  if (!menuId && !ariaRefs(el).length) return null;
  const inMenu = (f) => { const m = f.closest('[data-menu-id]'); return !!m && m !== el && m.getAttribute('data-menu-id') === menuId; };
  for (let n = el.parentElement, depth = 0; n && depth < 4; n = n.parentElement, depth++) {
    if (n === document.body || n.tagName === 'FORM' || n.tagName === 'FIELDSET') return null;
    const fields = [...n.querySelectorAll('input:not([type=hidden]),select,textarea')].filter((f) => !inMenu(f));
    if (!fields.length) continue;
    const toggles = [...n.querySelectorAll('button[aria-haspopup]')].filter((b) => comboPopup(b) !== 'false');
    const proxy = fields[0];
    return fields.length === 1 && toggles.length === 1 && proxy.tagName === 'SELECT' && !proxy.multiple &&
      proxy.getAttribute('aria-hidden') === 'true' && proxy.tabIndex === -1 ? proxy : null;
  }
  return null;
};
// The popup a menu control names: aria-controls or aria-owns, else a proxy toggle's
// data-menu-id (Fabric leaves out aria-controls), which is the menu element's id.
const comboRefs = (el) => {
  const refs = ariaRefs(el);
  if (refs.length || !menuProxy(el)) return refs;
  return [el.getAttribute('data-menu-id')].filter(Boolean);
};
const comboLike = (el) => {
  if (!el || !el.isConnected || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') return false;
  if (menuProxy(el)) return !el.closest('a[href]');
  if (el.getAttribute('role') !== 'combobox' && comboPopup(el) !== 'listbox') return false;
  if (el.tagName === 'INPUT' && !['', 'text', 'search'].includes((el.getAttribute('type') || '').toLowerCase())) return false;
  if (el.tagName === 'BUTTON' && el.type !== 'button') return false;
  return !el.closest('a[href]') && !el.isContentEditable;
};
const comboEditable = (el) => el.tagName === 'INPUT' && !el.readOnly && el.getAttribute('aria-readonly') !== 'true';
// Text nodes as the browser renders them: adjacent ones run together (React writes
// "+{code}" as "+" and "1", shown "+1"); a space only where an element came between.
const ariaJoin = (texts) => ariaText(texts.map((t, i) =>
  (i && t.previousSibling !== texts[i - 1] ? ' ' : '') + t.nodeValue).join(''));
const comboOwnText = (node) => ariaJoin([...node.childNodes].filter((t) => t.nodeType === 3));
// The small box a menu input shares with its displayed value or placeholder (a React
// select's control). It never reaches a label, heading, live region or another field.
const comboRoot = (el) => {
  const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map((id) => document.getElementById(id)).filter(Boolean);
  let root = null;
  for (let n = el.parentElement, depth = 0; n && depth < 3; n = n.parentElement, depth++) {
    if (n === document.body || n.tagName === 'FORM' || n.tagName === 'FIELDSET') break;
    if (n.querySelector('label,legend,h1,h2,h3,h4,h5,h6,[role=heading],[role=alert],[role=status],[aria-live]')) break;
    if (labelled.some((l) => n.contains(l))) break;
    if ([...n.querySelectorAll(comboFieldSel)].some((f) => f !== el && !f.closest('[role=listbox]'))) break;
    root = n;
  }
  return root;
};
// Text shown on the same line as a menu input inside its box: the selected value or
// the placeholder of a React select (the input itself stays empty).
const comboDisplayNodes = (el) => {
  if (el.tagName !== 'INPUT') return [];
  const root = comboRoot(el);
  if (!root) return [];
  const box = el.getBoundingClientRect(), mid = box.top + box.height / 2;
  return [...root.querySelectorAll('*')].filter((n) => {
    if (n === el || n.contains(el) || !comboOwnText(n)) return false;
    if (n.closest('[role=listbox],[role=option],[role=button],button,svg') || ariaMarked(n)) return false;
    const r = n.getBoundingClientRect();
    return ariaVisible(n) && r.top - 1 <= mid && mid <= r.bottom + 1;
  });
};
// "-- Select --", and BambooHR's "Select" between en dashes (any dash punctuation, \p{Pd}).
const comboPlaceholderText = /^(?:(?:please\s+)?(?:select|choose|pick)(?:\s+(?:an?|one|your|the)\b.{0,40})?|search|(?:type|start typing)\b.{0,40}|\p{Pd}+\s*(?:select|choose)\b.{0,40})\s*(?:\.\.\.|…|:)?\s*\p{Pd}*$/iu;
const comboDisplay = (el) => {
  if (el.tagName === 'INPUT') {
    if (el.value) return {text: ariaText(el.value), placeholder: false};
    const nodes = comboDisplayNodes(el);
    const text = ariaText(nodes.map(comboOwnText).join(' '));
    const described = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
    const placeholder = !text || comboPlaceholderText.test(text) || text === ariaText(el.getAttribute('placeholder')) ||
      nodes.some((n) => described.includes(n.id) || /placeholder/i.test(n.getAttribute('class') || ''));
    return {text, placeholder};
  }
  const parts = [];
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const p = t.parentElement;
    const skips = p ? [p.closest('[role=listbox]'), ariaMarked(p)] : [];
    if (!skips.some((skip) => skip && skip !== el && el.contains(skip))) parts.push(t);
  }
  const text = ariaJoin(parts);
  // Text shown by an element the widget names a placeholder (Fabric's
  // fab-SelectToggle__placeholder, "Select" between en dashes) is no value.
  const named = (t) => {
    for (let n = t.parentElement; n && n !== el; n = n.parentElement) {
      if (/placeholder/i.test(n.getAttribute('class') || '')) return true;
    }
    return false;
  };
  const shown = parts.filter((t) => ariaText(t.nodeValue));
  return {text, placeholder: !text || comboPlaceholderText.test(text) || (shown.length > 0 && shown.every(named))};
};
// Stable facts of a menu control whose options are not observable yet. Only value
// (the display text, '' for a placeholder) and expanded are state.
const comboFacts = (el) => {
  if (!comboLike(el)) return null;
  const shown = comboDisplay(el);
  return {combo: 1, role: el.getAttribute('role') || '', haspopup: comboPopup(el),
    autocomplete: (el.getAttribute('aria-autocomplete') || '').toLowerCase(),
    editable: comboEditable(el), multiselectable: el.getAttribute('aria-multiselectable') === 'true',
    dialog: !!el.closest('[role=dialog],dialog'),
    value: shown.placeholder ? '' : shown.text, expanded: el.getAttribute('aria-expanded') === 'true'};
};
// Never the aria-label: menus put their placeholder or shown value there (Rippling: "Select";
// Fabric: "Country United States"). A proxy's toggle is named by its select's name and labels.
const comboIdentity = (el) => {
  const proxy = menuProxy(el);
  const labels = el.labels && el.labels.length ? el.labels : proxy && proxy.labels ? proxy.labels : [];
  return {tag: el.tagName, id: el.id, name: el.getAttribute('name') || (proxy && proxy.getAttribute('name')) || '',
    role: el.getAttribute('role') || '', haspopup: comboPopup(el),
    labelledby: el.getAttribute('aria-labelledby') || '',
    labels: [...labels].map((l) => ariaText(l.textContent))};
};
// The listbox (or menu of menu items) this control owns right now, read only through its
// aria-controls or aria-owns reference, or a proxy toggle's data-menu-id (never a
// page-wide option scan).
const comboMenu = (el) => {
  const refs = comboRefs(el);
  if (!refs.length) return null;
  if (refs.length > 1) return {error: 'several owned popups'};
  const found = document.querySelectorAll('#' + CSS.escape(refs[0]));
  // A menu named only by data-menu-id is rendered the first time it opens.
  if (!found.length && !ariaRefs(el).length) return null;
  if (found.length !== 1) return {error: 'owned popup is missing or ambiguous'};
  let box = found[0];
  const lists = '[role=listbox],[role=menu]';
  if (!box.matches(lists)) {
    const inner = box.querySelectorAll(lists);
    if (inner.length !== 1) return {error: 'owned popup is not a single listbox'};
    box = inner[0];
  }
  // A role=menu popup (Fabric) lists its choices as menu items.
  const item = box.getAttribute('role') === 'menu' ? '[role=menuitem],[role=menuitemradio]' : '[role=option]';
  const checked = (o) => o.getAttribute('role') === 'menuitemradio' && o.hasAttribute('aria-checked');
  const nodes = [...box.querySelectorAll(item)].filter((o) => o.closest(lists) === box);
  const options = nodes.slice(0, 600).map((o, index) => ({
    index, id: o.id || '', selector: ariaUniqueId(o.id) ? '#' + CSS.escape(o.id) : '',
    label: ariaText(o.getAttribute('aria-label') || o.textContent),
    value: o.hasAttribute('data-value') ? o.getAttribute('data-value') : null,
    selected: o.getAttribute('aria-selected') === 'true' || (checked(o) && o.getAttribute('aria-checked') === 'true'),
    marked: o.hasAttribute('aria-selected') || checked(o),
    // A class token naming the selection (react-select's select__option--is-selected).
    classed: [...o.classList].some((t) => /selected/i.test(t) && !/(?:un|de|non|not[-_]?)selected/i.test(t)),
    disabled: o.getAttribute('aria-disabled') === 'true' || o.hasAttribute('disabled'),
    visible: ariaVisible(o), nested: !!o.querySelector(item)}));
  const notice = ariaText([...box.childNodes].filter((n) => !(n.nodeType === 1 &&
    (n.matches(item) || n.querySelector(item)))).map((n) => n.textContent).join(' '));
  const sizes = nodes.map((o) => Number(o.getAttribute('aria-setsize'))).filter((n) => n > 0);
  let scroller = null;
  for (let n = box, i = 0; n && i < 3 && n !== document.body; n = n.parentElement, i++) {
    if (n.scrollHeight > n.clientHeight + 1 && getComputedStyle(n).overflowY !== 'visible') { scroller = n; break; }
  }
  let covered = true;
  if (scroller) {
    const rects = nodes.map((o) => o.getBoundingClientRect()).filter((r) => r.height > 0);
    covered = false;
    if (rects.length) {
      const extent = Math.max(...rects.map((r) => r.bottom)) - Math.min(...rects.map((r) => r.top));
      covered = scroller.scrollHeight - extent <= Math.max(64, 2 * extent / rects.length);
    }
  }
  return {id: box.id || '', selector: ariaUniqueId(box.id) ? '#' + CSS.escape(box.id) : '',
    visible: ariaVisible(box), count: nodes.length, options,
    multiselectable: box.getAttribute('aria-multiselectable') === 'true',
    bad: !!box.querySelector('input,select,textarea,button,[role=button],[contenteditable=true],' +
      '[role=listbox],[role=menu],[role=checkbox],[role=radio]') ||
      [...box.querySelectorAll('a[href]')].some((a) => a.closest(item)),
    // A link beside the options (a lookup's "powered by …" attribution) is not an option.
    links: [...box.querySelectorAll('a[href]')].filter((a) => !a.closest(item)).length,
    notice, loading: box.getAttribute('aria-busy') === 'true' || /\b(?:loading|searching)\b/i.test(notice),
    setsize: sizes.length ? Math.max(...sizes) : null, scrollable: !!scroller, covered};
};
const comboFieldSet = (el) => {
  const scope = el.form || el.closest('form') || document.body;
  return [...scope.querySelectorAll(comboFieldSel)].filter((f) => !f.closest('[role=listbox]') && ariaVisible(f))
    .map((f) => [f.tagName, f.getAttribute('type') || '', f.id, f.getAttribute('name') || '',
      f.getAttribute('role') || ''].join('|')).join('\n');
};
// The country picker of a tel input's own widget: an intl-tel-input container, a
// preceding dialog button or a sibling combobox, never another question's control.
const phonePicker = (el) => {
  if (!el || el.tagName !== 'INPUT' || (el.getAttribute('type') || '').toLowerCase() !== 'tel') return null;
  const iti = el.closest('.iti');
  if (iti) return {kind: 'iti', node: iti.querySelector('[aria-haspopup=dialog],[aria-haspopup=listbox]') || iti};
  for (let scope = el.parentElement, depth = 0; scope && depth < 3; scope = scope.parentElement, depth++) {
    if (scope === document.body || scope.tagName === 'FORM') break;
    const others = [...scope.querySelectorAll('input:not([type=hidden]),select,textarea')].filter((f) =>
      f !== el && f.getAttribute('role') !== 'combobox' && !f.closest('[role=dialog],dialog,[role=listbox]'));
    if (others.length) break;
    const button = [...scope.querySelectorAll('[aria-haspopup=dialog]')].find((b) =>
      !b.closest('[role=dialog],dialog') && (b.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
    if (button) return {kind: 'dialog', node: button};
    const combo = [...scope.querySelectorAll('[role=combobox]')].find((c) => c !== el && !c.closest('[role=dialog],dialog'));
    if (combo) return {kind: 'combobox', node: combo};
  }
  return null;
};
// A picker's name and title, and a picker button's own text: intl-tel-input with
// separateDialCode shows the dial code only there ("Telephone country code" / "United
// States" / "+1"). Never a whole widget's text, which would include its country list.
const phonePickerText = (picker) => {
  if (picker.kind === 'combobox') return comboDisplay(picker.node).text;
  const n = picker.node;
  const parts = [n.getAttribute('aria-label'), n.getAttribute('title')];
  if (n.hasAttribute('aria-haspopup') && !n.querySelector('[role=listbox],[role=option],[role=dialog]')) parts.push(n.textContent);
  return ariaText(parts.filter(Boolean).join(' '));
};
"""

ARIA_OBSERVE = "(selector) => {" + ARIA_HELPERS + """
  const matches = document.querySelectorAll(selector);
  return matches.length === 1 ? ariaObserve(matches[0]) : null;
}"""

# Fixed read-only guard and readback. Selection is through the driver's click
# primitive, so OpenCLI keeps its read-only evaluation boundary intact.
ARIA_STATE = "(arg) => {" + ARIA_HELPERS + """
  const fail = error => ({error});
  const matches = document.querySelectorAll(arg.selector), expected = arg.binding;
  if (String(performance.timeOrigin) !== expected.origin || location.href !== expected.url) return fail('context');
  if (matches.length !== 1) return fail('control is missing or ambiguous');
  const el = matches[0];
  if (!ariaSafe(el) || JSON.stringify(ariaControl(el)) !== JSON.stringify(expected.control)) return fail('control changed');
  const got = ariaObserve(el);
  if (arg.readback) {
    const refs = ariaRefs(el);
    if (refs.length > 1 || (refs.length === 1 && refs[0] !== expected.owner)) return fail('ownership changed');
    const box = refs.length ? document.getElementById(refs[0]) : null;
    if (box && !ariaSame(got, expected)) return fail('options changed after selection');
    const value = ariaRead(el, expected.options, box);
    return value === arg.value ? {ok:true, values:[value]} : fail('selection readback mismatch');
  }
  if (!ariaSame(got, expected)) return fail('options or ownership changed');
  const option = got.options.filter(o => o.value === arg.value);
  if (option.length !== 1 || option[0].disabled) return fail('unknown or disabled option');
  const node = document.querySelector(option[0].selector);
  return {ok:true, expanded:got.expanded, visible:got.visible, selector:option[0].selector,
    actionable:ariaVisible(node)};
}"""

COMBO_STATE = "(arg) => {" + ARIA_HELPERS + """
  let found;
  try { found = document.querySelectorAll(arg.selector); } catch (e) { return {error: 'invalid selector'}; }
  if (found.length !== 1) return {error: 'control is missing or ambiguous'};
  const el = found[0];
  const shown = comboDisplay(el);
  return {origin: String(performance.timeOrigin), url: location.href, control: comboIdentity(el),
    like: comboLike(el), editable: comboEditable(el), visible: ariaVisible(el),
    disabled: !!el.disabled || !!el.closest('[aria-disabled=true]'),
    expanded: el.getAttribute('aria-expanded') === 'true', focused: document.activeElement === el,
    display: shown.text, placeholder: shown.placeholder,
    dialog: !!el.closest('[role=dialog],dialog,[aria-modal=true]'),
    input: el.tagName === 'INPUT' ? el.value : null,
    activedescendant: el.getAttribute('aria-activedescendant') || '',
    fields: arg.fields ? comboFieldSet(el) : null, menu: comboMenu(el)};
}"""
"""Read-only state of one menu control: identity, display, expansion and the listbox
it owns right now (options with label, data-value, aria-selected, disabled, index)."""

PHONE_STATE = "(selector) => {" + ARIA_HELPERS + """
  let found;
  try { found = document.querySelectorAll(selector); } catch (e) { return {error: 'invalid selector'}; }
  if (found.length !== 1) return {error: 'control is missing or ambiguous'};
  const el = found[0], picker = phonePicker(el);
  return {origin: String(performance.timeOrigin), url: location.href, value: el.value,
    picker: picker ? {kind: picker.kind, text: phonePickerText(picker)} : null};
}"""
"""Read-only value of a tel input and the text of its own country picker."""


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def display_option(display: str, labels: Sequence[str]) -> int | None:
    """The option a closed control's display text shows: the one label it equals, or
    the one label it is a suffix of (at least two characters: a dial-code select shows
    "+1" for "United States +1"). None when it names no option or several."""
    shown = _norm(display)
    if not shown:
        return None
    equal = [i for i, label in enumerate(labels) if _norm(label) == shown]
    if equal:
        return equal[0] if len(equal) == 1 else None
    if len(shown) < 2:
        return None
    suffix = [i for i, label in enumerate(labels) if _norm(label).endswith(shown)]
    return suffix[0] if len(suffix) == 1 else None


def display_matches(display: str, label: str, labels: Sequence[str]) -> str | None:
    """How a closed control's display text relates to the chosen option ``label``:
    ``equal`` (it shows the label), ``suffix`` (the label ends with it, at least two
    characters, and no other option label does), ``shared`` (a suffix several labels end
    with, like "+1" for "United States +1" and "Canada +1": consistent with the choice,
    but the display alone cannot tell which option is selected), or None."""
    shown, wanted = _norm(display), _norm(label)
    if not shown:
        return None
    if shown == wanted:
        return "equal"
    if len(shown) < 2 or not wanted.endswith(shown):
        return None
    holders = [other for other in labels if _norm(other).endswith(shown)]
    return "suffix" if len(holders) == 1 else "shared"


def dial_code(text: str) -> str | None:
    """The digits of the first ``+<code>`` in a picker's text ("(+44)", "+1 US")."""
    match = re.search(r"\+\s?(\d{1,4})\b", text or "")
    return match.group(1) if match else None


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


# --- probing ------------------------------------------------------------------------------

_POLL_S = 0.1
"""Interval between reads while waiting for a menu to open, close or settle."""
_OPEN_WAIT_S = 1.5
"""Per opening attempt (click, then ArrowDown)."""
_CLOSE_WAIT_S = 1.0
"""For a menu to close after an option was clicked."""
_COMMIT_WAIT_S = 3.0
"""For a lookup to show its chosen suggestion again: a site that saves the choice first
(Ashby) empties the input until its save returns, then writes the suggestion back."""
_CLOSE_STEP_S = 0.5
"""Per closing step (Escape, toggle click, outside press)."""
_CLOSE_STEPS = ("escape", "toggle", "outside")
_MAX_OPTIONS = 500
_FILTER_THRESHOLD = 20
"""Input menus with more options than this are filtered by typing the exact label."""
_SCROLL_ATTEMPTS = 5
_SUGGESTION_WAIT_S = 6.0
_SUGGESTION_STABLE_S = 0.4
_NO_SUGGESTION_GRACE_S = 3.0
"""A first query may load the site's place-search library before it answers."""
_MAX_SUGGESTIONS = 20
_TYPE_DELAY_S = 0.03


@dataclass(frozen=True)
class MenuObservation:
    """What probing learned about one closed menu control in one document."""

    kind: str
    """``select`` (a complete static option set), ``lookup`` (no options until the user
    types) or ``unobservable`` (the control stays UNSUPPORTED)."""
    reason: str = ""
    selector: str = ""
    origin: str = ""
    url: str = ""
    control: Mapping[str, Any] = field(default_factory=dict)
    options: tuple[Mapping[str, Any], ...] = ()
    open_method: str = "click"
    closed_display: str = ""
    listbox_id: str = ""
    editable: bool = False
    seconds: float = 0.0
    abort: bool = False
    """Probing stops for the rest of this document (it changed, or a menu stayed open)."""
    close_method: str = ""
    """The closing step that closed the probed menu (see ``_close_menu``)."""

    @property
    def listbox_id_pattern(self) -> str:
        """The owned listbox id with digit runs generalized: ids may be regenerated on
        every open, so a binding never stores the id itself."""
        return re.sub(r"\d+", r"\\d+", re.escape(self.listbox_id)) if self.listbox_id else ""

    @property
    def signature(self) -> str:
        items = [[o["label"], o["value"], bool(o["disabled"])] for o in self.options]
        return hashlib.sha256(json.dumps(items).encode()).hexdigest()

    def binding(self, facts: Mapping[str, Any], confirmed: str | None = None) -> dict[str, Any] | None:
        """The document-current ``DomControl.aria`` for a probed control whose closed
        facts (display text, expansion) are ``facts``. ``confirmed`` is the option value
        this runtime selected and verified by reopening the menu; it names the selection
        only while the display is a suffix shared by several options that it ends with."""
        if self.kind not in ("select", "lookup"):
            return None
        display = str(facts.get("value") or "")
        base: dict[str, Any] = {
            "probed": True, "kind": self.kind, "origin": self.origin, "url": self.url,
            "selector": self.selector, "control": dict(self.control),
            "open_method": self.open_method, "close_method": self.close_method,
            "closed_display": self.closed_display,
            "listbox_id_pattern": self.listbox_id_pattern, "editable": self.editable,
            "signature": self.signature, "expanded": bool(facts.get("expanded")), "visible": True,
        }
        if self.kind == "lookup":
            return {**base, "options": [], "value": display}
        labels = [str(o["label"]) for o in self.options]
        shown = display_option(display, labels)
        if shown is None and confirmed is not None:
            verified = [i for i, o in enumerate(self.options) if o["value"] == confirmed]
            if verified and display_matches(display, labels[verified[0]], labels) == "shared":
                shown = verified[0]
        options = [{"value": o["value"], "label": o["label"], "disabled": bool(o["disabled"]),
                    "index": o["index"], "selected": i == shown}
                   for i, o in enumerate(self.options)]
        return {**base, "options": options,
                "value": options[shown]["value"] if shown is not None else ""}


def _open(state: Mapping[str, Any]) -> bool:
    menu = state.get("menu")
    return bool(state.get("expanded")) and isinstance(menu, dict) and not menu.get("error") \
        and bool(menu.get("visible"))


def _listed(state: Mapping[str, Any]) -> bool:
    """A lookup's owned list is showing options, whether or not the input exposes
    ``aria-expanded`` (Rippling's location input never does)."""
    menu = state.get("menu")
    return isinstance(menu, dict) and not menu.get("error") and bool(menu.get("visible")) \
        and int(menu.get("count") or 0) > 0


def _labels(state: Mapping[str, Any]) -> list[str]:
    menu = state.get("menu")
    if not isinstance(menu, dict) or menu.get("error"):
        return []
    return [str(o["label"]) for o in menu.get("options", [])]


def _incomplete(menu: Mapping[str, Any]) -> bool:
    """The rendered options may be a window of a longer (virtualized) list."""
    setsize = menu.get("setsize")
    return (isinstance(setsize, int) and setsize > int(menu.get("count", 0))) or (
        bool(menu.get("scrollable")) and not menu.get("covered"))


Reader = Callable[[], Awaitable[dict[str, Any]]]


async def _poll(read: Reader, done: Callable[[dict[str, Any]], bool], timeout_s: float) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    end = loop.time() + max(0.0, timeout_s)
    state = await read()
    while not done(state) and loop.time() < end:
        await asyncio.sleep(min(_POLL_S, max(0.0, end - loop.time())))
        state = await read()
    return state


async def _settled(read: Reader, state: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """Once a menu is open, wait (bounded) until two reads show the same options."""
    loop = asyncio.get_running_loop()
    end = loop.time() + max(0.0, timeout_s)
    while loop.time() < end:
        await asyncio.sleep(_POLL_S)
        fresh = await read()
        if not _open(fresh) or _labels(fresh) == _labels(state):
            return fresh
        state = fresh
    return state


def _reader(driver: PageDriver, selector: str, *, fields: bool = False) -> Reader:
    from .driver import NotActionable

    async def read() -> dict[str, Any]:
        state = await driver.evaluate(COMBO_STATE, {"selector": selector, "fields": fields})
        if not isinstance(state, dict) or state.get("error"):
            reason = state.get("error") if isinstance(state, dict) else "invalid response"
            raise NotActionable(f"menu control is not readable: {reason}")
        return state

    return read


async def _open_menu(driver: PageDriver, selector: str, read: Reader, state: dict[str, Any],
                     method: str, wait_s: float = _OPEN_WAIT_S,
                     deadline: float | None = None) -> tuple[dict[str, Any], str]:
    """Open a menu without toggling it: focus; if it is still closed, click (unless it
    is known to open only from the keyboard) and wait for ``aria-expanded``; if it is
    still closed, press ArrowDown and wait again. Returns the state and the method
    that opened it (``click`` or ``arrowdown``)."""
    loop = asyncio.get_running_loop()

    def budget() -> float:
        return wait_s if deadline is None else max(0.1, min(wait_s, deadline - loop.time()))

    def expanded(s: dict[str, Any]) -> bool:
        return bool(s.get("expanded"))

    if not state.get("expanded"):
        await driver.focus(selector)
        state = await read()
    if not state.get("expanded") and method == "click":
        await driver.click(selector)
        state = await _poll(read, expanded, budget())
    if not state.get("expanded"):
        await driver.press(selector, "ArrowDown")
        state = await _poll(read, expanded, budget())
        method = "arrowdown"
    if state.get("expanded") and not _open(state):
        state = await _poll(read, _open, min(0.5, budget()))
    return state, method


async def _close_menu(driver: PageDriver, selector: str, read: Reader, method: str,
                      closer: str = "") -> tuple[bool, str]:
    """Close an open menu the ways a person does, one step at a time until
    ``aria-expanded`` is false (a keyboard-opened list may stay in the DOM): Escape; one
    toggle click on a click-opened control; a press outside every control (a popover
    that ignores both). ``closer``, the step that closed this page's menus before, goes
    first. Inside a dialog the toggle goes first and nothing is pressed outside. Returns whether the menu is closed and the step that closed it (``""`` when
    it was not open). Raises ``CapabilityUnsupported`` (after the other steps) when this
    session cannot press keys or outside the menu."""
    from .driver import CapabilityUnsupported

    state = await read()
    if not state.get("expanded"):
        return True, ""
    steps = [step for step in _CLOSE_STEPS if step != "toggle" or method == "click"]
    if closer in steps:
        steps.remove(closer)
        steps.insert(0, closer)
    if state.get("dialog"):
        # Inside a dialog, Escape and a press outside may close the dialog itself (a
        # wizard step): the control's own toggle goes first, and nothing is pressed
        # outside.
        steps = [step for step in ("toggle", "escape") if step in steps]
    unsupported: CapabilityUnsupported | None = None
    for step in steps:
        try:
            if step == "escape":
                await driver.press(selector, "Escape")
            elif step == "toggle":
                await driver.click(selector)
            else:
                await driver.dismiss()
        except CapabilityUnsupported as exc:
            unsupported = exc
            continue
        state = await _poll(read, lambda s: not s.get("expanded"), _CLOSE_STEP_S)
        if not state.get("expanded"):
            if unsupported is not None:
                raise unsupported
            return True, step
    if unsupported is not None:
        raise unsupported
    return False, ""


def _classify(opened: dict[str, Any], before: Mapping[str, Any], selector: str,
              method: str) -> MenuObservation:
    base = MenuObservation("unobservable", selector=selector, origin=str(before["origin"]),
                           url=str(before["url"]), control=dict(before["control"]),
                           open_method=method, closed_display=str(before.get("display") or ""),
                           editable=bool(before.get("editable")))
    lookup = replace(base, kind="lookup", reason="options appear only after typing")
    menu = opened.get("menu")
    if isinstance(menu, dict) and menu.get("multiselectable"):
        return replace(base, reason="multi-select menus are operated by the user")
    if not _open(opened):
        return lookup if base.editable else replace(base, reason="the menu did not open")
    assert isinstance(menu, dict)
    base = replace(base, listbox_id=str(menu.get("id") or ""))
    lookup = replace(lookup, listbox_id=base.listbox_id)
    if menu.get("bad") or menu.get("links"):
        return replace(base, reason="the menu holds other controls")
    options = menu.get("options") or []
    if not options:
        return lookup if base.editable else replace(base, reason="the menu is empty")
    if int(menu.get("count", 0)) > _MAX_OPTIONS or len(options) > _MAX_OPTIONS:
        return replace(base, reason="the menu has too many options")
    if _incomplete(menu):
        return replace(base, reason="the menu list is virtualized or incomplete")
    probed: list[dict[str, Any]] = []
    for option in options:
        label = str(option.get("label") or "")
        value = option.get("value")
        value = label if value is None else str(value)
        if not label or not value or option.get("nested"):
            return replace(base, reason="an option has no usable label")
        probed.append({"label": label, "value": value, "disabled": bool(option.get("disabled")),
                       "index": int(option.get("index", len(probed))),
                       "selected": bool(option.get("selected"))})
    labels = [o["label"] for o in probed]
    values = [o["value"] for o in probed]
    if len(set(labels)) != len(labels) or len(set(values)) != len(values):
        return replace(base, reason="options are not distinct")
    return replace(base, kind="select", reason="", options=tuple(probed))


async def probe_menu(driver: PageDriver, selector: str, *, timeout_s: float = 3.0,
                     open_wait_s: float = _OPEN_WAIT_S, closer: str = "") -> MenuObservation:
    """Open one closed menu control, read its owned options, close it (``closer`` first,
    see ``_close_menu``) and verify that the page is as it was. Never types, never
    chooses."""
    from .driver import CapabilityUnsupported, DriverError, PageContextLost

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    read = _reader(driver, selector)
    read_fields = _reader(driver, selector, fields=True)
    before = await read_fields()
    if not before.get("like") or not before.get("visible") or before.get("disabled") \
            or before.get("expanded"):
        return MenuObservation("unobservable", reason="not a closed, enabled menu control")
    document = (before.get("origin"), before.get("url"))

    def same_document(state: Mapping[str, Any]) -> None:
        if (state.get("origin"), state.get("url")) != document:
            raise PageContextLost("the page changed while probing a menu")

    method = "click"
    observation = MenuObservation("unobservable", reason="the menu could not be probed")
    failure: DriverError | None = None
    try:
        opened, method = await _open_menu(driver, selector, read, before, "click",
                                          open_wait_s, deadline)
        same_document(opened)
        if _open(opened):
            opened = await _settled(read, opened, max(0.1, min(0.5, deadline - loop.time())))
            menu = opened.get("menu")
            attempts = 0
            # A list whose rendered options do not cover its scroll height, or whose
            # aria-setsize exceeds them, may be virtualized: scroll to the end (bounded).
            while (_open(opened) and isinstance(menu, dict) and _incomplete(menu)
                   and menu.get("selector") and attempts < _SCROLL_ATTEMPTS
                   and loop.time() < deadline and int(menu.get("count", 0)) <= _MAX_OPTIONS):
                attempts += 1
                await driver.scroll_to_end(str(menu["selector"]))
                await asyncio.sleep(0.15)
                opened = await read()
                menu = opened.get("menu")
            same_document(opened)
        observation = _classify(opened, before, selector, method)
    except CapabilityUnsupported as exc:
        observation = MenuObservation("unobservable", reason=str(exc))
    except DriverError as exc:
        failure = exc
    closed = False
    try:
        closed, closer = await _close_menu(driver, selector, read, method, closer)
    except CapabilityUnsupported as exc:
        # Keys cannot be pressed here: a menu this session cannot close or re-verify is
        # left to the user.
        observation = MenuObservation("unobservable", reason=str(exc))
        closed = not (await read()).get("expanded")
    except DriverError:
        closed = False
    if failure is not None:
        raise failure
    if not closed:
        return MenuObservation("unobservable", reason="the menu did not close again", abort=True)
    after = await read_fields()
    same_document(after)
    if (after.get("expanded") or after.get("display") != before.get("display")
            or after.get("placeholder") != before.get("placeholder")
            or after.get("fields") != before.get("fields")):
        return MenuObservation("unobservable", reason="probing changed the form", abort=True)
    return replace(observation, close_method=closer)


class MenuProbe:
    """Per-document cache of probed menu controls, with the probing budget.

    Observations are keyed by the control's id (or selector) and label, scoped to one
    document (``DomSnapshot.document``) and dropped on navigation or context loss, so
    later inspections of the same document reuse them without reopening anything."""

    def __init__(self, *, max_probes: int = 24, max_seconds: float = 20.0,
                 per_control_s: float = 3.0, open_wait_s: float = _OPEN_WAIT_S) -> None:
        self.max_probes = max_probes
        self.max_seconds = max_seconds
        self.per_control_s = per_control_s
        self.open_wait_s = open_wait_s
        self.document = ""
        self.observations: dict[str, MenuObservation] = {}
        self.confirmed: dict[str, str] = {}
        """Probed selector -> option value this runtime selected and verified by reopening."""
        self.probes = 0
        self.seconds = 0.0
        self.stopped = ""
        self.closer = ""
        """The closing step that last closed a probed menu of this document."""
        self.log: list[tuple[str, str, float]] = []
        """(selector, kind, seconds) of every probe, for diagnostics and reports."""

    def reset(self, document: str = "") -> None:
        self.document = document
        self.observations = {}
        self.confirmed = {}
        self.probes = 0
        self.seconds = 0.0
        self.stopped = ""
        self.closer = ""

    def confirm(self, selector: str, value: str) -> None:
        """Record a selection verified by the widget's own selected state, so a display
        shared by several options (a dial code) still names it in this document."""
        self.confirmed[selector] = value

    @staticmethod
    def key(control: DomControl) -> str:
        return json.dumps([control.id or control.selector, control.label])

    @staticmethod
    def candidate(control: DomControl, form_index: int) -> bool:
        """A visible, enabled, closed single-choice menu control of the selected form
        whose options are not observable yet (never inside a dialog)."""
        facts = control.aria or {}
        return (
            bool(facts.get("combo")) and control.form_index == form_index
            and control.visible and not control.disabled
            and not facts.get("expanded") and not facts.get("dialog")
            and not facts.get("multiselectable")
            and (facts.get("haspopup") in ("listbox", "true", "menu") or facts.get("autocomplete") == "list")
        )

    def targets(self, snapshot: DomSnapshot, form_index: int | None) -> list[DomControl]:
        if form_index is None or snapshot.document != self.document:
            return []
        return [c for c in snapshot.controls
                if self.candidate(c, form_index) and self.key(c) not in self.observations]

    def merge(self, snapshot: DomSnapshot) -> DomSnapshot:
        """Attach cached observations to their (closed or open) controls and drop the
        leftover lists of probed keyboard menus. Pure; never touches the page."""
        if snapshot.document != self.document:
            self.reset(snapshot.document)
        if not self.observations:
            return snapshot
        popups = {o.listbox_id for o in self.observations.values() if o.listbox_id}
        controls = []
        changed = False
        for control in snapshot.controls:
            if control.kind == "custom" and control.type == "listbox" and control.id in popups:
                changed = True
                continue
            facts = control.aria or {}
            observation = None
            if facts.get("combo") or facts.get("version") == 1:
                observation = self.observations.get(self.key(control))
            if observation is not None and facts.get("version") == 1:
                # A probed menu that is open right now can also be read directly; it is
                # still the probed control (same binding closed or open).
                shown = [o.get("label") for o in facts.get("options", [])
                         if facts.get("value") and o.get("value") == facts.get("value")]
                facts = {"value": shown[0] if len(shown) == 1 else "",
                         "expanded": bool(facts.get("expanded"))}
            binding = (observation.binding(facts, self.confirmed.get(observation.selector))
                       if observation is not None else None)
            if binding is None and observation is not None:
                # A menu its probe could not observe (or close) stays with the user, open or
                # closed alike: it is never operated through a list it happens to show.
                controls.append(control.model_copy(update={"aria": {
                    "combo": 1, "unobservable": observation.reason,
                    "value": str(facts.get("value") or ""), "expanded": bool(facts.get("expanded"))}}))
                changed = True
                continue
            if binding is None:
                controls.append(control)
                continue
            controls.append(control.model_copy(update={"aria": binding}))
            changed = True
        return snapshot.model_copy(update={"controls": controls}) if changed else snapshot

    async def probe(self, driver: PageDriver, targets: Sequence[DomControl]) -> bool:
        """Probe uncached targets within the budget (at most ``max_probes`` controls and
        ``max_seconds`` per document, ``per_control_s`` each). Returns whether anything
        was opened, so the caller re-reads the page with every menu closed."""
        from .driver import DriverError, PageContextLost

        loop = asyncio.get_running_loop()
        touched = False
        for control in targets:
            key = self.key(control)
            if key in self.observations:
                continue
            if self.stopped:
                self.observations[key] = MenuObservation("unobservable", reason=self.stopped)
                continue
            if self.probes >= self.max_probes or self.seconds >= self.max_seconds:
                self.observations[key] = MenuObservation(
                    "unobservable", reason="the menu probing budget for this page is spent")
                continue
            started = loop.time()
            try:
                observation = await probe_menu(driver, control.selector, timeout_s=self.per_control_s,
                                               open_wait_s=self.open_wait_s, closer=self.closer)
            except PageContextLost:
                observation = MenuObservation("unobservable", abort=True,
                                              reason="the page changed while probing")
            except DriverError as exc:
                observation = MenuObservation("unobservable", reason=f"the menu could not be probed: {exc}")
            elapsed = loop.time() - started
            touched = True
            self.probes += 1
            self.seconds += elapsed
            self.log.append((control.selector, observation.kind, elapsed))
            self.observations[key] = replace(observation, seconds=elapsed)
            if observation.close_method:
                self.closer = observation.close_method
            if observation.abort:
                self.stopped = observation.reason
        return touched


# --- selection ----------------------------------------------------------------------------


async def select_accessible(
    driver: PageDriver, selector: str, values: list[str], binding: dict[str, Any],
    *, identity_check: Callable[[], Awaitable[None]] | None = None,
    before_action: Callable[[], Awaitable[None]] | None = None,
) -> list[str]:
    """Select one exact observed option; reject stale/ambiguous state at each step."""
    from .driver import NotActionable, PageContextLost

    if len(values) != 1 or binding.get("selector") != selector:
        raise NotActionable("ARIA selection requires one exact current control and option")
    options = binding.get("options", [])
    wanted = [o for o in options if o.get("value") == values[0] and not o.get("disabled")]
    if len(wanted) != 1:
        raise NotActionable("ARIA selection has no unique enabled observed option")
    if binding.get("probed"):
        return await _select_probed(driver, selector, wanted[0], binding,
                                    identity_check=identity_check, before_action=before_action)

    async def state(*, readback: bool = False) -> dict[str, Any]:
        response = await driver.evaluate(ARIA_STATE, {
            "selector": selector, "value": values[0], "binding": binding, "readback": readback,
        })
        if not isinstance(response, dict) or not response.get("ok"):
            reason = response.get("error") if isinstance(response, dict) else "invalid response"
            if reason == "context":
                raise PageContextLost("document changed during ARIA selection; re-inspect")
            raise NotActionable(f"ARIA selection held: {reason}")
        return response

    async def check_identity() -> None:
        if identity_check is not None:
            await identity_check()

    async def current_before_action() -> dict[str, Any]:
        # Opening a menu can synchronously change the question's subject/help or
        # the employer context. Runtime freshness must run again before choosing.
        if before_action is not None:
            await before_action()
        current = await state()
        await check_identity()
        return current

    before = await current_before_action()
    if not before["expanded"] or not before["visible"]:
        await driver.click(selector)
    before = await current_before_action()  # fresh refs after any portal transition
    if not before["expanded"] or not before["visible"] or not before["actionable"]:
        raise NotActionable("ARIA selection held: owned menu did not become actionable")
    # The choice selector is freshly derived from this owned listbox, never from
    # a page-wide text search or the language model's output.
    await driver.click(before["selector"])
    after = await state(readback=True)
    await check_identity()
    return list(after["values"])


def _check_probed(state: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    from .driver import NotActionable, PageContextLost

    if state.get("origin") != binding.get("origin") or state.get("url") != binding.get("url"):
        raise PageContextLost("the document changed while operating a menu; re-inspect")
    if state.get("control") != binding.get("control") or not state.get("like"):
        raise NotActionable("menu control changed; re-inspect")


def _probed_options(state: Mapping[str, Any], binding: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The owned listbox's options, required to be the probed option set."""
    from .driver import NotActionable

    menu = state.get("menu")
    if not isinstance(menu, dict) or menu.get("error") or menu.get("multiselectable") or menu.get("bad") \
            or (menu.get("links") and binding.get("kind") != "lookup"):
        raise NotActionable("the menu does not expose one single-choice listbox")
    pattern = str(binding.get("listbox_id_pattern") or "")
    if pattern and not re.fullmatch(pattern, str(menu.get("id") or "")):
        raise NotActionable("the menu opened a different listbox than when it was inspected")
    options: list[dict[str, Any]] = []
    for option in menu.get("options") or []:
        label = str(option.get("label") or "")
        value = option.get("value")
        options.append({**option, "value": label if value is None else str(value)})
    return options


async def _select_probed(
    driver: PageDriver, selector: str, wanted: Mapping[str, Any], binding: Mapping[str, Any],
    *, identity_check: Callable[[], Awaitable[None]] | None,
    before_action: Callable[[], Awaitable[None]] | None,
) -> list[str]:
    """Select one option of a probed menu control and verify it.

    Opens the way the probe did (never clicking an open menu), re-resolves the listbox
    from ``aria-controls`` after opening, clicks the one matching option freshly derived
    from the owned listbox (an input menu of more than 20 options that does not render it
    is filtered by typing, see ``_filter_queries``), then reads back: the menu closed and
    the display shows the label. A display that names no option is re-resolved the same
    way as a suffix display.
    When the display only shows a suffix of it (a dial code, possibly one several
    options share), the menu is reopened once and its own selection state must name
    the chosen option (see ``_selection_names``). Returns the values read back;
    anything else than ``[value]`` is a verification mismatch."""
    from .driver import NotActionable

    label = str(wanted["label"])
    all_options = list(binding.get("options") or [])
    labels = [str(o["label"]) for o in all_options]
    read = _reader(driver, selector)

    async def fresh() -> None:
        if before_action is not None:
            await before_action()
        if identity_check is not None:
            await identity_check()

    method = str(binding.get("open_method") or "click")
    closer = str(binding.get("close_method") or "")
    await fresh()
    state = await read()
    _check_probed(state, binding)
    shows = "" if state.get("placeholder") else str(state.get("display") or "")
    if not state.get("expanded") and display_matches(shows, label, labels) == "equal":
        # It already shows exactly this option (pre-filled, BambooHR's "United States"):
        # verified by its display, and not operated again.
        return [str(wanted["value"])]
    state, method = await _open_menu(driver, selector, read, state, method)
    _check_probed(state, binding)
    if not _open(state):
        await _close_menu(driver, selector, read, method, closer)
        raise NotActionable("the menu did not open")
    state = await _settled(read, state, 0.5)
    shown = _probed_options(state, binding)
    expected = [[o["label"], o["value"], bool(o["disabled"])] for o in all_options]
    if [[o["label"], o["value"], bool(o["disabled"])] for o in shown] != expected:
        await _close_menu(driver, selector, read, method, closer)
        raise NotActionable("the menu's options changed since inspection; re-inspect")
    await fresh()

    def pick(options: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        found = [o for o in options if _norm(str(o["label"])) == _norm(label)
                 and (o["value"] == wanted["value"] or o["value"] == o["label"])]
        if len(found) != 1 or found[0].get("disabled") or not found[0].get("selector") \
                or not found[0].get("visible"):
            return None
        return found[0]

    target = pick(shown)
    typed = target is None and bool(binding.get("editable")) and len(all_options) > _FILTER_THRESHOLD
    if typed:
        # The option is not rendered: filter the long input menu by typing. Sites filter
        # on an option's name, not on what decorates it ("+1", a flag), so the label is
        # tried, then its name without a trailing code, then its first word.
        for query in _filter_queries(label):
            await driver.clear_text(selector)
            await driver.type_text(selector, query, delay_s=0.0)
            state = await _poll(read, lambda s: _open(s) and _norm(label) in [_norm(x) for x in _labels(s)],
                                _OPEN_WAIT_S)
            state = await _settled(read, state, 0.5)
            target = pick(_probed_options(state, binding)) if _open(state) else None
            if target is not None:
                break
    if target is None:
        if typed:
            await driver.clear_text(selector)  # leave no filter text behind
        await _close_menu(driver, selector, read, method, closer)
        raise NotActionable("the menu has no unique visible option with that label")
    if identity_check is not None:
        await identity_check()
    await driver.click(str(target["selector"]))

    after = await _poll(read, lambda s: not s.get("expanded") or not s.get("menu"), _CLOSE_WAIT_S)
    _check_probed(after, binding)
    if after.get("expanded") and after.get("menu"):
        # A menu that stays open after a choice (a popover only an outside press closes)
        # is closed the way the probe closed it; the display then shows what it holds.
        await _close_menu(driver, selector, read, method, closer)
        after = await read()
        _check_probed(after, binding)
    if (not after.get("expanded") or not after.get("menu")) and (after.get("placeholder") or not after.get("display")):
        # A site that saves the choice first (Ashby) shows nothing until its save returns.
        after = await _poll(read, lambda s: bool(s.get("display")) and not s.get("placeholder"), _COMMIT_WAIT_S)
        _check_probed(after, binding)
    closed = not after.get("expanded") or not after.get("menu")
    display = "" if after.get("placeholder") else str(after.get("display") or "")
    shown_index = display_option(display, labels)
    relation = display_matches(display, label, labels)
    if closed and relation == "equal":
        # Rules (1) and (2): the menu closed and the control shows the chosen label.
        if identity_check is not None:
            await identity_check()
        return [str(wanted["value"])]
    confirmed, confirmation = False, ""
    # A display that names no option at all (read from decoration, or text split oddly)
    # is not the field's value: the reopened menu's own selection decides, as it does
    # when only a suffix is shown, but only when the display could be our choice (its
    # letters and digits appear in the label in order: "+ 1" for "United States (+1)").
    # An unrecognised placeholder ("Country *") or a display naming another option stays
    # a mismatch: APG and Downshift menus mark the option they highlight on opening
    # aria-selected, so a click that did not take would otherwise read as confirmed.
    unnamed = (bool(display) and relation is None and shown_index is None
               and _consistent(display, label))
    if closed and (relation is not None or unnamed):
        # Only a suffix is shown (a dial code, possibly shared by several options): the
        # reopened menu's own selection state must name the chosen option.
        reopened, method = await _open_menu(driver, selector, read, after, method)
        _check_probed(reopened, binding)
        if _open(reopened):
            confirmed, confirmation = _selection_names(
                _probed_options(reopened, binding), str(reopened.get("activedescendant") or ""), label)
        else:
            confirmation = "the menu did not reopen"
    if not (await _close_menu(driver, selector, read, method, closer))[0]:
        raise NotActionable("the menu did not close after reading back the selection")
    if identity_check is not None:
        await identity_check()
    if closed and (relation is not None or unnamed) and confirmed:
        return [str(wanted["value"])]
    observed = [str(all_options[shown_index]["value"])] if shown_index is not None else []
    if relation == "shared":
        observed.append(f"display {display!r} is shared by several options")
    if confirmation:
        observed.append(confirmation)
    if not closed:
        observed.append("menu still open")
    return observed or [f"display {display!r}"]


def _consistent(display: str, label: str) -> bool:
    """The display's letters and digits appear in the label's, in order."""
    shown = [c for c in display.casefold() if c.isalnum()]
    wanted = iter(c for c in label.casefold() if c.isalnum())
    return bool(shown) and all(any(c == w for w in wanted) for c in shown)


def _filter_queries(label: str) -> list[str]:
    """What to type into a long input menu to show one option: its label, its name
    without a trailing code or parenthetical ("United States" of "United States +1"),
    then its first word."""
    queries = [label]
    name = re.sub(r"\s*(?:\([^)]*\)|\+\s?\d[\d\s-]*)\s*$", "", label).strip()
    words = label.split()
    for query in (name, words[0] if words else ""):
        if query and query not in queries:
            queries.append(query)
    return queries


def _selection_names(options: Sequence[Mapping[str, Any]], activedescendant: str,
                     label: str) -> tuple[bool, str]:
    """Whether a reopened menu marks ``label`` as its selection, and how: the first
    signal the widget exposes decides, in order: ``aria-selected`` (exactly one option
    true), ``aria-activedescendant``, a class token naming the selection (exactly one
    option, like react-select's ``select__option--is-selected``, which it keeps on Apple
    platforms where it leaves out both attributes). No signal is no confirmation."""
    flagged = [o for o in options if o.get("selected")]
    active = [o for o in options if activedescendant and o.get("id") == activedescendant]
    if flagged or any(o.get("marked") for o in options):
        if len(flagged) != 1:
            return False, f"aria-selected marks {len(flagged)} options"
        if len(active) == 1 and active[0] is not flagged[0]:
            # A highlight marked aria-selected (APG, Downshift) is not a selection.
            return False, (f"aria-selected on {flagged[0]['label']!r} but aria-activedescendant "
                           f"on {active[0]['label']!r}")
        return _norm(str(flagged[0]["label"])) == _norm(label), f"aria-selected on {flagged[0]['label']!r}"
    if len(active) == 1:
        return (_norm(str(active[0]["label"])) == _norm(label),
                f"aria-activedescendant on {active[0]['label']!r}")
    classed = [o for o in options if o.get("classed")]
    if len(classed) == 1:
        return _norm(str(classed[0]["label"])) == _norm(label), f"selected class on {classed[0]['label']!r}"
    if classed:
        return False, f"a selected class on {len(classed)} options"
    return False, "no option is marked selected on reopening"


# --- lookups (typeaheads) -------------------------------------------------------------------


@dataclass(frozen=True)
class LookupOutcome:
    chosen: str | None
    """The suggestion that was clicked, or None when nothing was committed."""
    verified: bool = False
    suggestions: tuple[str, ...] = ()
    detail: str = ""


async def fill_lookup(
    driver: PageDriver, selector: str, text: str, binding: Mapping[str, Any],
    choose: Callable[[str, Sequence[str]], list[int]],
    *, before_action: Callable[[], Awaitable[None]] | None = None,
) -> LookupOutcome:
    """Type ``text`` into a lookup and commit the one suggestion ``choose`` accepts.

    Suggestions are read from the listbox the input owns once they are stable for
    400 ms (at most 6 s; 3 s when none appear). With exactly one accepted suggestion it
    is clicked and read back; otherwise the input is cleared again and the suggestions
    are returned."""
    from .driver import NotActionable

    read = _reader(driver, selector)
    state = await read()
    _check_probed(state, binding)
    await driver.clear_text(selector)
    if (await read()).get("input"):
        raise NotActionable("the lookup input could not be cleared")
    await driver.type_text(selector, text, delay_s=_TYPE_DELAY_S)

    loop = asyncio.get_running_loop()
    started = loop.time()
    end = started + _SUGGESTION_WAIT_S
    last_key: tuple[Any, ...] | None = None
    stable_since = started
    state = await read()
    while True:
        _check_probed(state, binding)
        menu = state.get("menu")
        present = _open(state) or _listed(state)
        loading = present and isinstance(menu, dict) and bool(menu.get("loading"))
        key = (present, loading, tuple(_labels(state)))
        now = loop.time()
        if key != last_key:
            last_key, stable_since = key, now
        settled = now - stable_since >= _SUGGESTION_STABLE_S and not loading
        if settled and (present or now - started >= _NO_SUGGESTION_GRACE_S):
            break
        if now >= end:
            break
        await asyncio.sleep(_POLL_S)
        state = await read()
    suggestions = [label[:200] for label in _labels(state)[:_MAX_SUGGESTIONS] if label]
    picks = choose(text, suggestions)
    if len(picks) != 1:
        await driver.clear_text(selector)
        cleared = await read()
        if cleared.get("expanded"):
            await _close_menu(driver, selector, read, "arrowdown", str(binding.get("close_method") or ""))
        if (await read()).get("input"):
            raise NotActionable("the lookup input could not be cleared after no unique match")
        if not suggestions:
            detail = "the site offered no suggestions for the typed value"
        elif not picks:
            detail = "no suggestion matches the typed value"
        else:
            detail = f"{len(picks)} suggestions match the typed value"
        return LookupOutcome(None, suggestions=tuple(suggestions), detail=detail)
    chosen = suggestions[picks[0]]
    if before_action is not None:
        await before_action()
    state = await read()
    _check_probed(state, binding)
    showing = _open(state) or _listed(state)
    options = _probed_options(state, {**binding, "listbox_id_pattern": ""}) if showing else []
    target = [o for o in options if o["label"][:200] == chosen]
    if len(target) != 1 or not target[0].get("selector") or not target[0].get("visible"):
        await driver.clear_text(selector)
        return LookupOutcome(None, suggestions=tuple(suggestions),
                             detail="the chosen suggestion is no longer shown")
    await driver.click(str(target[0]["selector"]))
    after = await _poll(read, lambda s: not s.get("expanded") or not s.get("menu"), _CLOSE_WAIT_S)
    if not after.get("expanded") or not after.get("menu"):
        after = await _poll(read, lambda s: bool(s.get("display")) and not s.get("placeholder"), _COMMIT_WAIT_S)
    _check_probed(after, binding)
    display = str(after.get("display") or "")
    typed = str(after.get("input") or "")
    closed = not after.get("expanded") or not after.get("menu")
    verified = (closed and not after.get("placeholder") and _norm(display) == _norm(chosen)
                and (typed == "" or _norm(typed) == _norm(chosen)))
    detail = "" if verified else f"shows {display!r} after choosing {chosen!r}"
    return LookupOutcome(chosen, verified=verified, suggestions=tuple(suggestions), detail=detail)


# --- phone numbers with a country picker ------------------------------------------------------


async def fill_phone(driver: PageDriver, selector: str, text: str) -> tuple[bool, str]:
    """Type a phone number into a tel input whose widget has a country picker, as
    given (``+<code><number>`` lets the widget pick the country). Reads back that the
    input holds the same digits, ignoring formatting and a separately shown dial code,
    and that the picker shows the typed dial code. Returns (ok, what was read back)."""
    from .driver import NotActionable

    async def read() -> dict[str, Any]:
        state = await driver.evaluate(PHONE_STATE, selector)
        if not isinstance(state, dict) or state.get("error"):
            raise NotActionable("the phone input is not readable")
        return state

    await driver.clear_text(selector)
    if (await read()).get("value"):
        raise NotActionable("the phone input could not be cleared")
    await driver.type_text(selector, text, delay_s=_TYPE_DELAY_S)
    state = await _poll(read, lambda s: bool(s.get("value")), 0.5)
    value = str(state.get("value") or "")
    picker = state.get("picker") if isinstance(state.get("picker"), dict) else {}
    picker_text = str((picker or {}).get("text") or "")
    typed, got = _digits(text), _digits(value)
    code = dial_code(picker_text)
    international = text.strip().startswith("+")
    digits_ok = got == typed or bool(international and code and typed.startswith(code)
                                     and got == typed[len(code):])
    code_ok = not international or bool(code and typed.startswith(code))
    return digits_ok and code_ok, f"{value} ({picker_text or 'no country picker'})"


# --- opt-in single-menu observation ------------------------------------------------------------


ARIA_EXPANSION = "(selector) => {" + ARIA_HELPERS + """
  const nodes = document.querySelectorAll(selector);
  if (nodes.length !== 1) return null;
  const el = nodes[0];
  if (!ariaSafe(el) || !ariaUniqueId(el.id) || el.getAttribute('aria-haspopup') !== 'listbox') return null;
  if (ariaRefs(el).length > 1) return null;
  return {origin:String(performance.timeOrigin), url:location.href, control:ariaControl(el),
    expanded:el.getAttribute('aria-expanded') === 'true',
    value:el.matches('input,textarea') ? el.value : ariaText(el.textContent)};
}"""


async def expand_accessible(driver: PageDriver, selector: str) -> DomSnapshot:
    """Optionally open one safe, selection-only menu and return a fresh snapshot.

    This is deliberately NOT invoked by inspection or classification. A caller
    explicitly requesting menu observation may invoke it once. It never types,
    chooses an option, clicks a general button, or toggles an already open menu.
    The menu remains open so the returned option binding is DOM-current.
    """
    from .driver import NotActionable, PageContextLost
    from .snapshot import DomSnapshot, inspector_script

    before = await driver.evaluate(ARIA_EXPANSION, selector)
    if not isinstance(before, dict):
        raise NotActionable("menu observation requires a unique selection-only listbox combobox")
    if not before["expanded"]:
        await driver.click(selector)
    snapshot = DomSnapshot.model_validate(await driver.evaluate(inspector_script()))
    after = await driver.evaluate(ARIA_EXPANSION, selector)
    if not isinstance(after, dict):
        raise NotActionable("menu control changed during observation")
    if before["origin"] != after["origin"] or before["url"] != after["url"]:
        raise PageContextLost("document changed while observing a menu")
    if before["control"] != after["control"] or before["value"] != after["value"]:
        raise NotActionable("menu expansion changed the field; re-inspect before continuing")
    observations = [c.aria for c in snapshot.controls
                    if c.selector == selector and c.aria and c.aria.get("version") == 1]
    if len(observations) != 1 or not observations[0]["expanded"] or not observations[0]["visible"]:
        raise NotActionable("menu observation did not expose a unique complete owned listbox")
    return snapshot
