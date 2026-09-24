"""Conservative, DOM-current ARIA listbox support.

The observer is read-only and knows no ATS selectors. Only selection-only, single
comboboxes with one uniquely owned listbox and a complete unambiguous option set
are supported. The action script is fixed trusted code, never provider-generated.
Bindings are ephemeral observations of a document, not reusable selector recipes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .driver import PageDriver
    from .snapshot import DomSnapshot

# Shared by inspect.js and the action/readback helpers. No DOM or global writes.
ARIA_HELPERS = r"""
const ariaText = (v) => String(v || '').replace(/\s+/g, ' ').trim();
const ariaVisible = (el) => {
  if (!el || !el.isConnected) return false;
  for (let n = el; n; n = n.parentElement) {
    if (n.hidden || n.hasAttribute('inert') || n.getAttribute('aria-hidden') === 'true') return false;
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
    observations = [c.aria for c in snapshot.controls if c.selector == selector and c.aria]
    if len(observations) != 1 or not observations[0]["expanded"] or not observations[0]["visible"]:
        raise NotActionable("menu observation did not expose a unique complete owned listbox")
    return snapshot
