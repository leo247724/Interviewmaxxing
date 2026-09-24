// Read-only DOM inspector for Interviewmaxxing.
//
// Evaluated in the page (Playwright `page.evaluate`, or any driver that can run a
// function expression and return JSON). It never changes the page. It reports raw
// facts only: controls with their labels, descriptions, groups, options and state;
// buttons; links; headings; status regions; structured data; CAPTCHA widgets.
// All interpretation happens in Python (interviewmaxxing_browser.normalize).
() => {
  /* ARIA_HELPERS */
  const MAX_TEXT = 20000;
  const NATIVE = new Set(["INPUT", "SELECT", "TEXTAREA"]);
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image"]);
  const CUSTOM_ROLES = new Set([
    "combobox", "listbox", "radiogroup", "checkbox", "switch", "textbox",
    "spinbutton", "slider", "searchbox", "menu", "tree", "grid",
  ]);

  const cssString = (v) => '"' + String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
  const unique = (sel) => {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  };
  // Menus a combobox or picker owns (listbox, menu, dialog), as the outermost element of
  // each that does not contain its owner. Wherever a widget renders an open menu (a
  // body portal or inside the form), it belongs to that widget: it never adds text to
  // another control and never shifts another element's position in a selector.
  const popupRoots = new Set();
  for (const owner of document.querySelectorAll("[aria-controls], [aria-owns]")) {
    if (owner.getAttribute("role") !== "combobox" && !owner.hasAttribute("aria-haspopup")) continue;
    for (const attr of ["aria-controls", "aria-owns"]) {
      for (const id of (owner.getAttribute(attr) || "").split(/\s+/).filter(Boolean)) {
        const popup = document.getElementById(id);
        if (!popup || popup.contains(owner)) continue;
        let root = popup;
        while (root.parentElement && root.parentElement !== document.body && !root.parentElement.contains(owner)) {
          root = root.parentElement;
        }
        popupRoots.add(root);
      }
    }
  }
  const selectorFor = (el) => {
    if (el.id && unique("#" + CSS.escape(el.id))) return "#" + CSS.escape(el.id);
    const tag = el.tagName.toLowerCase();
    const name = el.getAttribute("name");
    if (name) {
      let sel = tag + "[name=" + cssString(name) + "]";
      if (unique(sel)) return sel;
      if (el.type === "radio" || el.type === "checkbox") {
        sel += "[value=" + cssString(el.value) + "]";
        if (unique(sel)) return sel;
      }
    }
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      if (n !== el && n.id && unique("#" + CSS.escape(n.id))) { parts.unshift("#" + CSS.escape(n.id)); break; }
      const t = n.tagName.toLowerCase();
      const p = n.parentElement;
      if (!p) { parts.unshift(t); break; }
      const same = Array.from(p.children).filter((c) => c.tagName === n.tagName);
      const index = same.indexOf(n);
      // An open menu next to this element does not make it "the first of two".
      const counted = same.filter((c) => c === n || !popupRoots.has(c));
      const popupBefore = same.slice(0, index).some((c) => popupRoots.has(c));
      parts.unshift(counted.length > 1 || popupBefore ? t + ":nth-of-type(" + (index + 1) + ")" : t);
    }
    return parts.join(" > ");
  };

  const hiddenByAncestor = (el) => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (n.getAttribute("aria-hidden") === "true" || n.hasAttribute("inert") || n.hidden) return true;
    }
    return false;
  };
  const cssVisible = (el) => {
    if (typeof el.checkVisibility === "function") return el.checkVisibility({ checkVisibilityCSS: true });
    const cs = getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden";
  };
  const hasBox = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    return r.right + window.scrollX > 0 && r.bottom + window.scrollY > 0;
  };
  const visible = (el) => !!el && !hiddenByAncestor(el) && cssVisible(el) && hasBox(el);

  const textOf = (root, exclude) => {
    if (!root) return "";
    const parts = [];
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) { parts.push(node.nodeValue); return; }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      if (exclude && exclude.has(el)) return;
      const tag = el.tagName;
      if (["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "SELECT", "TEXTAREA", "INPUT", "OPTION"].includes(tag)) return;
      if (el.getAttribute("aria-hidden") === "true" || el.hidden) return;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") return;
      for (const c of el.childNodes) walk(c);
      parts.push(" ");
    };
    walk(root);
    return parts.join(" ").replace(/\s+/g, " ").trim();
  };
  const classOf = (el) => (el.getAttribute && el.getAttribute("class")) || "";
  const isErrorEl = (el) =>
    el.getAttribute("role") === "alert" || /error|invalid/i.test((el.id || "") + " " + classOf(el));
  const byIds = (value) =>
    (value || "").split(/\s+/).filter(Boolean).map((id) => document.getElementById(id)).filter(Boolean);
  // A description the control names as its error message is an error, whatever it looks like.
  const errorFor = (el, d) => isErrorEl(d) ||
    (!!d.id && (el.getAttribute("aria-errormessage") || "").split(/\s+/).includes(d.id));
  const described = (el) =>
    byIds(el && el.getAttribute("aria-describedby")).map((d) => ({ text: textOf(d), error: errorFor(el, d) }))
      .filter((d) => d.text);

  const labelOf = (el) => {
    const byLabelledby = byIds(el.getAttribute("aria-labelledby")).map((n) => textOf(n)).join(" ").trim();
    if (byLabelledby) return [byLabelledby, "aria-labelledby"];
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map((l) => textOf(l)).join(" ").trim();
      // A styled uploader's hidden file input is labelled with its button's verb
      // ("Attach"); the question is the name of the uploader's group ("Resume/CV").
      if (t && el.type === "file" && /^(?:attach|upload|browse|choose|select|add)(?:\s+(?:a\s+)?files?)?$/i.test(t)) {
        const group = el.closest('[role="group"][aria-labelledby]');
        const named = group ? byIds(group.getAttribute("aria-labelledby")).map((n) => textOf(n)).join(" ").trim() : "";
        if (named) return [named, "aria-labelledby"];
      }
      if (t) return [t, "label"];
    }
    const aria = (el.getAttribute("aria-label") || "").trim();
    // A div menu whose aria-label is its placeholder ("Select", kept after a choice) or
    // repeats what it displays names no question; its question is the text around it.
    const placeholderName = el.tagName !== "INPUT" && comboLike(el) &&
      (comboPlaceholderText.test(ariaText(aria)) || ariaText(aria) === comboDisplay(el).text);
    if (aria && !placeholderName) return [aria, "aria-label"];
    const title = (el.getAttribute("title") || "").trim();
    if (title) return [title, "title"];
    return ["", "none"];
  };
  const labelVisible = (el) => !!(el.labels && Array.from(el.labels).some(visible));

  // ---- forms and their controls --------------------------------------------------
  const forms = Array.from(document.forms);
  const formIndex = (el) => (el.form ? forms.indexOf(el.form) : -1);
  // A combobox's hidden validation proxy (react-select's RequiredInput: aria-hidden,
  // tabindex -1, rendered only while the menu has no value) is part of that widget.
  const comboProxy = (el) => el.tagName === "INPUT" && el.getAttribute("aria-hidden") === "true" &&
    el.tabIndex === -1 && !!el.parentElement &&
    Array.from(el.parentElement.querySelectorAll('[role="combobox"]')).some((c) => c !== el);
  const nativeControls = Array.from(document.querySelectorAll("input, select, textarea"))
    .filter((el) => !SKIP_TYPES.has((el.type || "").toLowerCase()) && !comboProxy(el));

  const customWidgets = [];
  for (const el of document.querySelectorAll("[role], [contenteditable]")) {
    // A <button> with a widget role (e.g. role="combobox") is a custom control, not an action.
    if (NATIVE.has(el.tagName)) continue;
    const role = el.getAttribute("role");
    const editable = el.hasAttribute("contenteditable") && el.isContentEditable;
    if (!(CUSTOM_ROLES.has(role) || editable)) continue;
    // A list that is not shown is a closed popup (some menus leave theirs in the
    // document after closing), never a question of its own.
    if (role === "listbox" && !visible(el)) continue;
    if (el.querySelector("input:not([type=hidden]), select, textarea")) continue; // wraps native controls
    if (customWidgets.some((w) => w.contains(el))) continue;
    customWidgets.push(el);
  }
  // Popups (listbox, menu) owned by a combobox, or by a role-less input with a popup,
  // belong to it.
  const ownedIds = new Set();
  const owners = nativeControls.filter((el) => el.getAttribute("role") === "combobox" || el.hasAttribute("aria-haspopup"));
  for (const w of [...customWidgets, ...owners]) {
    for (const attr of ["aria-controls", "aria-owns"]) {
      for (const id of (w.getAttribute(attr) || "").split(/\s+/).filter(Boolean)) ownedIds.add(id);
    }
  }
  const ownedEls = new Set(Array.from(ownedIds).map((id) => document.getElementById(id)).filter(Boolean));
  const widgets = customWidgets.filter((w) => !(w.id && ownedIds.has(w.id)));
  const fieldEls = new Set([...nativeControls, ...widgets]);

  const groupMembers = (el) => {
    if (!(el.type === "radio" || el.type === "checkbox") || !el.name) return [el];
    return nativeControls.filter((o) => o.type === el.type && o.name === el.name && o.form === el.form);
  };
  const containerCache = new Map();
  const containerFor = (members, stopAt) => {
    const key = members[0];
    if (containerCache.has(key)) return containerCache.get(key);
    let c = members[0].parentElement;
    while (c && !members.every((m) => c.contains(m))) c = c.parentElement;
    const exclusive = (node) => {
      for (const f of fieldEls) if (node.contains(f) && !members.includes(f)) return false;
      return true;
    };
    if (!c || !exclusive(c) || c === stopAt || c === document.body) { containerCache.set(key, null); return null; }
    while (c.parentElement && c.parentElement !== stopAt && c.parentElement !== document.body && exclusive(c.parentElement)) {
      c = c.parentElement;
    }
    containerCache.set(key, c);
    return c;
  };
  const adjacentText = (container, exclude) => {
    const texts = [];
    const errors = [];
    if (!container) return [texts, errors];
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        const t = node.nodeValue.replace(/\s+/g, " ").trim();
        if (t) texts.push(t);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      if (exclude.has(el)) return;
      if (["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "SELECT", "TEXTAREA", "INPUT", "OPTION", "BUTTON", "LEGEND", "LABEL"].includes(el.tagName)) return;
      // Headings are section context (sectionContextOf), not the control's description.
      if (el.matches('h1,h2,h3,h4,h5,h6,[role="heading"]')) return;
      if (el.getAttribute("aria-hidden") === "true" || el.hidden) return;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") return;
      if (fieldEls.has(el)) return;
      if (isErrorEl(el)) { const t = textOf(el); if (t) errors.push(t); return; }
      // Live regions (character counters, upload progress) change while the user
      // types; they are not part of the question.
      if (el.getAttribute("aria-live") || el.getAttribute("role") === "status") return;
      for (const c of el.childNodes) walk(c);
    };
    for (const c of container.childNodes) walk(c);
    return [texts.length ? [texts.join(" ")] : [], errors];
  };
  const legendOf = (el) => {
    const fs = el.closest("fieldset");
    if (!fs) return [null, null, []];
    const legend = Array.from(fs.children).find((c) => c.tagName === "LEGEND");
    return [legend ? textOf(legend) : null, selectorFor(fs), described(fs)];
  };
  const groupLabelOf = (el) => {
    const g = el.closest('[role="radiogroup"], [role="group"]');
    if (!g || g.tagName === "FIELDSET") return [null, []];
    const byLabelledby = byIds(g.getAttribute("aria-labelledby")).map((n) => textOf(n)).join(" ").trim();
    return [byLabelledby || (g.getAttribute("aria-label") || "").trim() || null, described(g)];
  };
  const sectionContextOf = (el) => {
    // Subject and timeframe may live in a section heading, while the control is
    // simply labelled "Email" or "City". Only use scoped ancestor headings that
    // precede this control; never borrow a neighbouring form/section's text.
    const parts = [];
    const boundary = el.form || el.closest("form") || document.body;
    let child = el;
    for (let parent = el.parentElement, depth = 0;
         parent && parent !== document.body && depth < 12;
         child = parent, parent = parent.parentElement, depth++) {
      const preceding = [];
      for (const sibling of parent.children) {
        if (sibling === child) break;
        if (visible(sibling) && sibling.matches('h1,h2,h3,h4,h5,h6,[role="heading"],legend')) {
          const value = textOf(sibling).slice(0, 2000);
          if (value) preceding.push(value);
        }
      }
      if (preceding.length) parts.unshift(preceding[preceding.length - 1]);
      if (parent.matches('section,[role="group"],[role="radiogroup"]')) {
        const value = byIds(parent.getAttribute("aria-labelledby"))
          .filter(visible).map(n => textOf(n)).join(" ") || parent.getAttribute("aria-label");
        if (value) parts.unshift(value.slice(0, 2000));
      }
      if (parent === boundary) break;
    }
    return Array.from(new Set(parts)).slice(-6);
  };
  const imgAlts = (container) =>
    container ? Array.from(container.querySelectorAll("img[alt]")).map((i) => i.alt).filter(Boolean) : [];
  // The nearest previous sibling block with visible text, next to the control's own
  // exclusive box (or its group's container). Stops at another field's block; skips
  // headings (section context) and live regions. A question shown before an unlabeled control.
  const precedingTextOf = (members, container) => {
    let start = container;
    if (!start) {
      start = members[0];
      while (start.parentElement && start.parentElement !== document.body && start.parentElement.tagName !== "FORM") {
        const parent = start.parentElement;
        let exclusive = true;
        for (const f of fieldEls) if (f !== members[0] && parent.contains(f)) { exclusive = false; break; }
        if (!exclusive) break;
        start = parent;
      }
    }
    for (let sib = start.previousElementSibling; sib; sib = sib.previousElementSibling) {
      if (popupRoots.has(sib) || !visible(sib)) continue;
      if (sib.matches('script,style,template,button,label,legend,h1,h2,h3,h4,h5,h6,[role="heading"],[role="alert"],[role="status"],[aria-live]')) continue;
      for (const f of fieldEls) if (sib.contains(f)) return "";
      if (sib.querySelector("button, a[href]")) continue;
      const t = textOf(sib, popupRoots);
      if (t) return t.slice(0, 500);
    }
    return "";
  };

  // A menu control's shown value or placeholder (a React select's "Select..." or "+1")
  // is state, not part of the question; neither are live-region descriptions.
  const comboDescribed = (el, displayNodes) =>
    byIds(el.getAttribute("aria-describedby"))
      .filter((d) => !displayNodes.some((n) => n === d || d.contains(n) || n.contains(d)))
      .filter((d) => !d.closest('[aria-live],[role="log"],[role="status"]'))
      .map((d) => ({ text: textOf(d), error: errorFor(el, d) }))
      .filter((d) => d.text);

  const describeNative = (el) => {
    const members = groupMembers(el);
    const form = el.form;
    const container = containerFor(members, form);
    const exclude = new Set([...ownedEls, ...popupRoots]);
    for (const m of members) for (const l of m.labels || []) exclude.add(l);
    for (const m of members) for (const d of byIds(m.getAttribute("aria-describedby"))) exclude.add(d);
    const fs = el.closest("fieldset");
    if (fs) for (const d of byIds(fs.getAttribute("aria-describedby"))) exclude.add(d);
    const combo = comboLike(el);
    const displayNodes = combo ? comboDisplayNodes(el) : [];
    for (const n of displayNodes) exclude.add(n);
    const picker = phonePicker(el);
    const [adjacent, adjacentErrors] = adjacentText(container, exclude);
    const [label, labelSource] = labelOf(el);
    const [legend, legendSelector, legendDescribed] = legendOf(el);
    const [groupLabel, groupDescribed] = groupLabelOf(el);
    const tag = el.tagName.toLowerCase();
    const type = tag === "input" ? (el.type || "text").toLowerCase() : tag;
    const errTarget = byIds(el.getAttribute("aria-errormessage"))[0];
    return {
      kind: "native",
      tag,
      type,
      name: el.getAttribute("name") || "",
      id: el.id || "",
      selector: selectorFor(el),
      role: el.getAttribute("role") || "",
      autocomplete_list: (el.getAttribute("aria-autocomplete") || "") === "list",
      label,
      label_source: labelSource,
      described: combo ? comboDescribed(el, displayNodes) : described(el),
      error_message: errTarget && visible(errTarget) ? textOf(errTarget) : "",
      legend,
      legend_selector: legendSelector,
      legend_described: legendDescribed,
      group_label: groupLabel,
      group_described: groupDescribed,
      section_context: sectionContextOf(el),
      preceding: precedingTextOf(members, container),
      adjacent,
      adjacent_errors: adjacentErrors,
      label_selector: el.labels && el.labels.length ? selectorFor(el.labels[0]) : null,
      required: el.required || el.getAttribute("aria-required") === "true" ||
        !!(el.closest('[aria-required="true"]')),
      disabled: el.disabled || el.getAttribute("aria-disabled") === "true",
      visible: visible(el),
      label_visible: labelVisible(el),
      readonly: !!el.readOnly,
      value: type === "file" ? "" : String(el.value ?? ""),
      checked: !!el.checked,
      files: type === "file" ? Array.from(el.files || []).map((f) => ({ name: f.name, size: f.size })) : [],
      placeholder: el.getAttribute("placeholder") || "",
      autocomplete: (el.getAttribute("autocomplete") || "").toLowerCase(),
      accept: el.getAttribute("accept") || "",
      max_length: el.maxLength > 0 ? el.maxLength : null,
      multiple: !!el.multiple,
      options: tag === "select"
        ? Array.from(el.options).map((o) => ({
            value: o.value,
            label: (o.label || o.text || "").replace(/\s+/g, " ").trim(),
            disabled: o.disabled || !!(o.parentElement && o.parentElement.tagName === "OPTGROUP" && o.parentElement.disabled),
            selected: o.selected,
          }))
        : [],
      invalid: el.getAttribute("aria-invalid") === "true",
      image_alts: imgAlts(fs || container),
      form_index: formIndex(el),
      has_value: false,
      aria: ariaObserve(el) || comboFacts(el),
      phone_picker: !picker ? "" : picker.kind === "combobox" ? "combobox:" + (picker.node.id || "") : picker.kind,
    };
  };

  const describeCustom = (el) => {
    const container = containerFor([el], el.closest("form"));
    const exclude = new Set([...byIds(el.getAttribute("aria-labelledby")), ...byIds(el.getAttribute("aria-describedby")),
      ...popupRoots]);
    for (const id of ownedIds) { const o = document.getElementById(id); if (o) exclude.add(o); }
    const [adjacent, adjacentErrors] = adjacentText(container, exclude);
    const [label, labelSource] = labelOf(el);
    const [legend, legendSelector, legendDescribed] = legendOf(el);
    const owned = [el, ...byIds(el.getAttribute("aria-controls")), ...byIds(el.getAttribute("aria-owns"))];
    const selectedOption = owned.some((n) => n.querySelector('[aria-selected="true"]'));
    const hiddenInput = container ? container.querySelector("input[type=hidden]") : null;
    const hasValue = selectedOption || el.getAttribute("aria-checked") === "true" ||
      (el.isContentEditable && textOf(el) !== "") || !!(hiddenInput && hiddenInput.value);
    const form = el.closest("form");
    return {
      kind: "custom",
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute("role") || "contenteditable",
      name: hiddenInput ? hiddenInput.getAttribute("name") || "" : "",
      id: el.id || "",
      selector: selectorFor(el),
      role: el.getAttribute("role") || "",
      autocomplete_list: false,
      label,
      label_source: labelSource,
      described: described(el),
      error_message: "",
      legend,
      legend_selector: legendSelector,
      legend_described: legendDescribed,
      group_label: null,
      group_described: [],
      section_context: sectionContextOf(el),
      preceding: precedingTextOf([el], container),
      adjacent,
      adjacent_errors: adjacentErrors,
      label_selector: null,
      required: el.getAttribute("aria-required") === "true",
      disabled: el.getAttribute("aria-disabled") === "true",
      visible: visible(el),
      label_visible: false,
      readonly: el.getAttribute("aria-readonly") === "true",
      value: "",
      checked: false,
      files: [],
      placeholder: "",
      autocomplete: "",
      accept: "",
      max_length: null,
      multiple: false,
      options: [],
      invalid: el.getAttribute("aria-invalid") === "true",
      image_alts: [],
      form_index: form ? forms.indexOf(form) : -1,
      has_value: hasValue,
      aria: ariaObserve(el) || comboFacts(el),
      phone_picker: "",
    };
  };

  const controls = [...nativeControls.map(describeNative), ...widgets.map(describeCustom)];
  // Keep document order.
  const order = [...nativeControls, ...widgets];
  const positioned = controls.map((c, i) => [order[i], c]);
  positioned.sort((a, b) => (a[0].compareDocumentPosition(b[0]) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));

  // ---- buttons, links, page text --------------------------------------------------
  // Buttons, links, headings and live text inside an open menu belong to its widget.
  const inPopup = (el) => { for (const p of popupRoots) if (p.contains(el)) return true; return false; };
  // So do a menu control's own buttons, in the box it shares with no label or other
  // field: react-select's "Toggle flyout" and, once it holds a value, "Clear selection"
  // (Greenhouse), or a chip's remove button. They come and go with the answer.
  const comboButton = (b) => {
    if (b.tagName === "BUTTON" && (b.getAttribute("type") || "submit").toLowerCase() !== "button") return false;
    for (let n = b.parentElement, depth = 0; n && depth < 4; n = n.parentElement, depth++) {
      if (n.tagName === "FORM" || n === document.body) return false;
      if (n.querySelector("label, legend, h1, h2, h3, h4, h5, h6, [role=heading]")) return false;
      const inside = [...fieldEls].filter((f) => n.contains(f));
      if (inside.length > 1) return false;
      if (inside.length === 1) return comboLike(inside[0]);
    }
    return false;
  };
  const buttons = [];
  for (const el of document.querySelectorAll('button, input[type=submit], input[type=button], input[type=image], input[type=reset], [role="button"]')) {
    if (!visible(el) || inPopup(el) || comboButton(el)) continue;
    if (CUSTOM_ROLES.has(el.getAttribute("role") || "")) continue; // a widget, reported as a control
    // A picker trigger (a phone widget's "Change country" button) opens a dialog or
    // list; it is part of a control, never a step action, and its label is state.
    if (el.tagName === "BUTTON" && (el.getAttribute("type") || "submit").toLowerCase() === "button" &&
        ["dialog", "listbox"].includes((el.getAttribute("aria-haspopup") || "").toLowerCase())) continue;
    const isInput = el.tagName === "INPUT";
    const type = el.tagName === "BUTTON" ? (el.getAttribute("type") || "submit").toLowerCase()
      : isInput ? el.type.toLowerCase() : "role-button";
    const text = (isInput ? (el.value || el.alt || "") : textOf(el)) || el.getAttribute("aria-label") || el.title || "";
    const form = el.form || null;
    // The request this button would actually send, including formmethod/formaction overrides.
    const method = !form ? "" : (el.hasAttribute("formmethod") ? el.formMethod : form.method || "get").toLowerCase();
    const action = !form ? "" : (el.hasAttribute("formaction") ? el.formAction : form.action);
    buttons.push({
      text: text.replace(/\s+/g, " ").trim(),
      type,
      selector: selectorFor(el),
      disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
      form_index: form ? forms.indexOf(form) : (el.closest("form") ? forms.indexOf(el.closest("form")) : -1),
      submits_form: !!form && (type === "submit" || type === "image"),
      form_no_validate: !!el.formNoValidate,
      effective_method: method,
      effective_action: action,
    });
  }
  const links = [];
  for (const a of document.querySelectorAll("a[href]")) {
    if (!visible(a) || inPopup(a)) continue;
    links.push({ text: textOf(a) || a.getAttribute("aria-label") || "", href: a.href, selector: selectorFor(a) });
  }
  const headings = Array.from(document.querySelectorAll("h1, h2, h3"))
    .filter((h) => visible(h) && !inPopup(h)).map((h) => ({ level: Number(h.tagName[1]), text: textOf(h) })).filter((h) => h.text);
  const regions = Array.from(document.querySelectorAll('[role="alert"], [role="status"], [aria-live]'))
    .filter((r) => visible(r) && !inPopup(r)).map((r) => ({ role: r.getAttribute("role") || "live", text: textOf(r) })).filter((r) => r.text);

  // Record candidates: members of repeated sibling groups of block elements (cards,
  // articles, list items, rows). Python decides which groups are application records
  // and ties a status only to the outermost record that contains it.
  const RECORD_TAGS = new Set(["LI", "TR", "ARTICLE", "SECTION", "DIV", "ASIDE", "DD"]);
  const RECORD_ROLES = new Set(["listitem", "row", "article", "group"]);
  const recordish = (el) => RECORD_TAGS.has(el.tagName) || RECORD_ROLES.has(el.getAttribute("role") || "");
  const memberIndex = new Map();
  const members = [];
  let groupCount = 0;
  for (const parent of [document.body, ...document.body.querySelectorAll("*")]) {
    if (members.length >= 600) break;
    // All record-like children of one parent form one group whatever their tags:
    // a <div> card next to an <article> card is still two sibling records.
    const group = Array.from(parent.children).filter((c) => recordish(c) && visible(c));
    {
      if (group.length < 2) continue;
      const g = groupCount++;
      for (const el of group) {
        const text = textOf(el).slice(0, 4000);
        if (!text) continue;
        memberIndex.set(el, members.length);
        members.push({ el, group: g, text });
      }
    }
  }
  const recordMembers = members.map((m) => {
    const ancestors = [];
    for (let n = m.el.parentElement; n; n = n.parentElement) {
      if (memberIndex.has(n)) ancestors.push(memberIndex.get(n));
    }
    return { group: m.group, text: m.text, ancestors };
  });

  // Positive local boundaries for flat receipts/portals without record wrappers.
  // A heading scope ends before ANY following heading (also nested in a sibling).
  // We never merge the document title, separate sections, or ungrouped paragraphs.
  const HEADING = "h1,h2,h3,h4,h5,h6";
  const confirmationScopes = [];
  const covered = new Set();
  for (const heading of document.querySelectorAll(HEADING)) {
    if (!visible(heading)) continue;
    const parts = [textOf(heading)];
    covered.add(heading);
    for (let sibling = heading.nextElementSibling; sibling; sibling = sibling.nextElementSibling) {
      if (sibling.matches(HEADING) || sibling.querySelector(HEADING)) break;
      if (!visible(sibling)) continue;
      // A collection/table/form cannot become one receipt by sitting below a heading.
      if (sibling.matches("ul,ol,table,form") || sibling.querySelector("ul,ol,table,form")) break;
      parts.push(textOf(sibling));
      covered.add(sibling);
    }
    confirmationScopes.push({ heading: textOf(heading), text: parts.join("\n").slice(0, 4000) });
  }
  for (const el of document.querySelectorAll("p,div,li,td,dd,[role=status],[role=alert]")) {
    if (!visible(el) || el.querySelector("p,div,li,td,dd,h1,h2,h3,h4,h5,h6")) continue;
    let inside = false;
    for (let node = el; node; node = node.parentElement) {
      if (covered.has(node)) { inside = true; break; }
    }
    if (!inside) confirmationScopes.push({ heading: null, text: textOf(el).slice(0, 4000) });
  }

  let step = null;
  const current = document.querySelector('[aria-current="step"]');
  if (current && current.parentElement) {
    const items = Array.from(current.parentElement.children);
    step = { current: items.indexOf(current) + 1, total: items.length, source: "aria-current" };
  } else {
    const m = (document.body.innerText || "").match(/\bstep\s+(\d+)\s*(?:of|\/)\s*(\d+)\b/i);
    if (m) step = { current: Number(m[1]), total: Number(m[2]), source: "text" };
  }

  const captchaFrames = Array.from(document.querySelectorAll("iframe"))
    .filter((f) => /recaptcha|hcaptcha|challenges\.cloudflare|turnstile|arkoselabs|funcaptcha|captcha/i.test(f.src || f.title || ""))
    .map((f) => ({ src: f.src || "", title: f.title || "", visible: visible(f) }));
  const tokens = Array.from(document.querySelectorAll('[name="g-recaptcha-response"], [name="h-captcha-response"], [name="cf-turnstile-response"]'))
    .map((t) => ({ name: t.getAttribute("name"), filled: !!t.value }));

  const bodyText = (document.body ? document.body.innerText || "" : "").slice(0, MAX_TEXT);
  // Loading signals only delay readiness (bounded) for a page that does not classify yet.
  const LOADING_TEXT = /\b(?:loading|fetching|please wait|one moment)\b/i;
  const loadingIndicator = LOADING_TEXT.test(bodyText) ||
    Array.from(document.querySelectorAll('[aria-busy="true"], [role="progressbar"]')).some(visible);

  return {
    url: location.href,
    title: document.title || "",
    headings,
    regions,
    body_text: bodyText,
    record_members: recordMembers,
    confirmation_scopes: confirmationScopes.slice(0, 600),
    ld_json: Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map((s) => s.textContent || ""),
    meta: {
      og_site_name: (document.querySelector('meta[property="og:site_name"]') || {}).content || "",
      og_title: (document.querySelector('meta[property="og:title"]') || {}).content || "",
    },
    forms: forms.map((f, i) => ({
      index: i,
      selector: selectorFor(f),
      method: (f.getAttribute("method") || "get").toLowerCase(),
      action: f.action || "",
      no_validate: !!f.noValidate,
    })),
    controls: positioned.map((p) => p[1]),
    buttons,
    links,
    step,
    password_visible: Array.from(document.querySelectorAll("input[type=password]")).some(visible),
    captcha_frames: captchaFrames,
    captcha_tokens: tokens,
    captcha_widget: !!document.querySelector(".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]"),
    loading_indicator: loadingIndicator,
    document: String(performance.timeOrigin) + " " + location.href,
  };
}
