// Read-only DOM inspector for Interviewmaxxing.
//
// Evaluated in the page (Playwright `page.evaluate`, or any driver that can run a
// function expression and return JSON). It never changes the page. It reports raw
// facts only: controls with their labels, descriptions, groups, options and state;
// buttons; links; headings; status regions; structured data; CAPTCHA widgets.
// All interpretation happens in Python (interviewmaxxing_browser.normalize).
() => {
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
      parts.unshift(same.length > 1 ? t + ":nth-of-type(" + (same.indexOf(n) + 1) + ")" : t);
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
  const described = (el) =>
    byIds(el && el.getAttribute("aria-describedby")).map((d) => ({ text: textOf(d), error: isErrorEl(d) }))
      .filter((d) => d.text);

  const labelOf = (el) => {
    const byLabelledby = byIds(el.getAttribute("aria-labelledby")).map((n) => textOf(n)).join(" ").trim();
    if (byLabelledby) return [byLabelledby, "aria-labelledby"];
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map((l) => textOf(l)).join(" ").trim();
      if (t) return [t, "label"];
    }
    const aria = (el.getAttribute("aria-label") || "").trim();
    if (aria) return [aria, "aria-label"];
    const title = (el.getAttribute("title") || "").trim();
    if (title) return [title, "title"];
    return ["", "none"];
  };
  const labelVisible = (el) => !!(el.labels && Array.from(el.labels).some(visible));

  // ---- forms and their controls --------------------------------------------------
  const forms = Array.from(document.forms);
  const formIndex = (el) => (el.form ? forms.indexOf(el.form) : -1);
  const nativeControls = Array.from(document.querySelectorAll("input, select, textarea"))
    .filter((el) => !SKIP_TYPES.has((el.type || "").toLowerCase()));

  const customWidgets = [];
  for (const el of document.querySelectorAll("[role], [contenteditable]")) {
    // A <button> with a widget role (e.g. role="combobox") is a custom control, not an action.
    if (NATIVE.has(el.tagName)) continue;
    const role = el.getAttribute("role");
    const editable = el.hasAttribute("contenteditable") && el.isContentEditable;
    if (!(CUSTOM_ROLES.has(role) || editable)) continue;
    if (el.querySelector("input:not([type=hidden]), select, textarea")) continue; // wraps native controls
    if (customWidgets.some((w) => w.contains(el))) continue;
    customWidgets.push(el);
  }
  // Popups (listbox, menu) owned by a combobox belong to it.
  const ownedIds = new Set();
  for (const w of customWidgets) {
    for (const attr of ["aria-controls", "aria-owns"]) {
      for (const id of (w.getAttribute(attr) || "").split(/\s+/).filter(Boolean)) ownedIds.add(id);
    }
  }
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
  const imgAlts = (container) =>
    container ? Array.from(container.querySelectorAll("img[alt]")).map((i) => i.alt).filter(Boolean) : [];

  const describeNative = (el) => {
    const members = groupMembers(el);
    const form = el.form;
    const container = containerFor(members, form);
    const exclude = new Set();
    for (const m of members) for (const l of m.labels || []) exclude.add(l);
    for (const m of members) for (const d of byIds(m.getAttribute("aria-describedby"))) exclude.add(d);
    const fs = el.closest("fieldset");
    if (fs) for (const d of byIds(fs.getAttribute("aria-describedby"))) exclude.add(d);
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
      described: described(el),
      error_message: errTarget && visible(errTarget) ? textOf(errTarget) : "",
      legend,
      legend_selector: legendSelector,
      legend_described: legendDescribed,
      group_label: groupLabel,
      group_described: groupDescribed,
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
    };
  };

  const describeCustom = (el) => {
    const container = containerFor([el], el.closest("form"));
    const exclude = new Set([...byIds(el.getAttribute("aria-labelledby")), ...byIds(el.getAttribute("aria-describedby"))]);
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
    };
  };

  const controls = [...nativeControls.map(describeNative), ...widgets.map(describeCustom)];
  // Keep document order.
  const order = [...nativeControls, ...widgets];
  const positioned = controls.map((c, i) => [order[i], c]);
  positioned.sort((a, b) => (a[0].compareDocumentPosition(b[0]) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));

  // ---- buttons, links, page text --------------------------------------------------
  const buttons = [];
  for (const el of document.querySelectorAll('button, input[type=submit], input[type=button], input[type=image], input[type=reset], [role="button"]')) {
    if (!visible(el)) continue;
    if (CUSTOM_ROLES.has(el.getAttribute("role") || "")) continue; // a widget, reported as a control
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
    if (!visible(a)) continue;
    links.push({ text: textOf(a) || a.getAttribute("aria-label") || "", href: a.href, selector: selectorFor(a) });
  }
  const headings = Array.from(document.querySelectorAll("h1, h2, h3"))
    .filter(visible).map((h) => ({ level: Number(h.tagName[1]), text: textOf(h) })).filter((h) => h.text);
  const regions = Array.from(document.querySelectorAll('[role="alert"], [role="status"], [aria-live]'))
    .filter(visible).map((r) => ({ role: r.getAttribute("role") || "live", text: textOf(r) })).filter((r) => r.text);

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
    const byKey = new Map();
    for (const child of parent.children) {
      if (!recordish(child) || !visible(child)) continue;
      const key = child.tagName + "|" + (child.getAttribute("role") || "");
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key).push(child);
    }
    for (const group of byKey.values()) {
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

  return {
    url: location.href,
    title: document.title || "",
    headings,
    regions,
    body_text: (document.body ? document.body.innerText || "" : "").slice(0, MAX_TEXT),
    record_members: recordMembers,
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
  };
}
