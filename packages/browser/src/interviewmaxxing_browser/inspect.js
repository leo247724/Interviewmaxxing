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

  // ---- open shadow roots ------------------------------------------------------------
  // Some sites render the application inside an open shadow root (LinkedIn's Easy Apply
  // since 2026). Elements are collected from the document and every open shadow root,
  // in composed order; a page without shadow roots reads exactly as before.
  const shadowRoots = [];
  const composedOrder = new Map();
  const walkComposed = (root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      composedOrder.set(n, composedOrder.size);
      if (n.shadowRoot && shadowRoots.length < 50) { shadowRoots.push(n.shadowRoot); walkComposed(n.shadowRoot); }
    }
  };
  walkComposed(document);
  const deepAll = (sel) => {
    const found = Array.from(document.querySelectorAll(sel));
    if (!shadowRoots.length) return found;
    for (const root of shadowRoots) found.push(...root.querySelectorAll(sel));
    return found.sort((a, b) => composedOrder.get(a) - composedOrder.get(b));
  };
  // The parent in the composed tree: a shadow root's top elements belong to its host.
  const parentOf = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;

  const cssString = (v) => '"' + String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
  const unique = (sel) => {
    try { return deepAll(sel).length === 1; } catch (e) { return false; }
  };
  // A menu button standing in for a hidden native select (menuProxy: BambooHR's Fabric
  // select) is the control; its select only names it (label, name, required) and is
  // never a control of its own.
  const menuToggles = new Map();
  for (const b of document.querySelectorAll("button[aria-haspopup]")) {
    const proxy = menuProxy(b);
    if (proxy) menuToggles.set(b, proxy);
  }
  // Menus a combobox or picker owns (listbox, menu, dialog), as the outermost element of
  // each that does not contain its owner. Wherever a widget renders an open menu (a
  // body portal or inside the form), it belongs to that widget: it never adds text to
  // another control and never shifts another element's position in a selector.
  const popupRoots = new Set();
  // The popups of menu toggles, whose own search box is part of the menu, not a field.
  const togglePopups = new Set();
  const addPopup = (owner, id) => {
    const popup = document.getElementById(id);
    if (!popup || popup.contains(owner)) return;
    let root = popup;
    while (root.parentElement && root.parentElement !== document.body && !root.parentElement.contains(owner)) {
      root = root.parentElement;
    }
    popupRoots.add(root);
    if (menuToggles.has(owner)) togglePopups.add(root);
  };
  for (const owner of document.querySelectorAll("[aria-controls], [aria-owns]")) {
    if (owner.getAttribute("role") !== "combobox" && !owner.hasAttribute("aria-haspopup")) continue;
    for (const attr of ["aria-controls", "aria-owns"]) {
      for (const id of (owner.getAttribute(attr) || "").split(/\s+/).filter(Boolean)) addPopup(owner, id);
    }
  }
  for (const toggle of menuToggles.keys()) for (const id of comboRefs(toggle)) addPopup(toggle, id);
  // A date input's calendar (Ashby's react-datepicker popper, opened inside the input's
  // own field box while it has focus) is a popup as well: it never adds text to the
  // question and never shifts a selector.
  for (const c of document.querySelectorAll('[class*="datepicker-popper"], [class*="calendar-popup"], .flatpickr-calendar')) {
    if (!Array.from(popupRoots).some((p) => p.contains(c))) popupRoots.add(c);
  }
  const selectorFor = (el) => {
    if (el.id && unique("#" + CSS.escape(el.id))) return "#" + CSS.escape(el.id);
    // Inside a shadow root the selector is "<host selector> >> <selector within the
    // root>" (Playwright chains it; the fixed read scripts resolve it the same way).
    const root = el.getRootNode();
    const shadow = root !== document && root.host ? root : null;
    const scoped = (sel) => {
      if (!shadow) return unique(sel);
      try { return shadow.querySelectorAll(sel).length === 1; } catch (e) { return false; }
    };
    const within = (sel) => (shadow ? selectorFor(shadow.host) + " >> " + sel : sel);
    const tag = el.tagName.toLowerCase();
    const name = el.getAttribute("name");
    if (name) {
      let sel = tag + "[name=" + cssString(name) + "]";
      if (scoped(sel)) return within(sel);
      if (el.type === "radio" || el.type === "checkbox") {
        sel += "[value=" + cssString(el.value) + "]";
        if (scoped(sel)) return within(sel);
      }
    }
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      if (n !== el && n.id && scoped("#" + CSS.escape(n.id))) { parts.unshift("#" + CSS.escape(n.id)); break; }
      const t = n.tagName.toLowerCase();
      const p = n.parentElement || (shadow && n.parentNode === shadow ? shadow : null);
      if (!p) { parts.unshift(t); break; }
      const same = Array.from(p.children).filter((c) => c.tagName === n.tagName);
      const index = same.indexOf(n);
      // An open menu next to this element does not make it "the first of two".
      const counted = same.filter((c) => c === n || !popupRoots.has(c));
      const popupBefore = same.slice(0, index).some((c) => popupRoots.has(c));
      parts.unshift(counted.length > 1 || popupBefore ? t + ":nth-of-type(" + (index + 1) + ")" : t);
      if (p === shadow) break;
    }
    return within(parts.join(" > "));
  };

  const hiddenByAncestor = (el) => {
    for (let n = el; n && n.nodeType === 1; n = parentOf(n)) {
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
  const byId = (id) => document.getElementById(id) ||
    shadowRoots.map((root) => root.getElementById(id)).find(Boolean) || null;
  const byIds = (value) =>
    (value || "").split(/\s+/).filter(Boolean).map(byId).filter(Boolean);
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

  // ---- file uploads -------------------------------------------------------------
  // An upload control's question is what surrounds it before anything is attached. The
  // attached file's name (a chip), upload/parse progress ("Analyzing resume...",
  // "Success!") and the trigger (an "Attach" link or button wrapped by the label) are
  // state or actions, not question wording, so they are left out of its label and
  // description; its fingerprint then stays the same after an upload.
  const FILE_NAMES = /[\w\-()[\]]+\.(?:pdf|docx?|txt|rtf|odt|pages|png|jpe?g|gif|heic|html?)\b/gi;
  const UPLOAD_STATE = /^(?:uploading|uploaded|upload (?:complete|successful|failed)|analy[sz]ing|parsing|processing|scanning|success|done|failed|couldn'?t|could not|remove|replace|change|delete|retry|no files? (?:selected|chosen|attached|uploaded))\b/i;
  const FILE_SIZE = /^\(?\d+(?:[.,]\d+)?\s*(?:bytes?|[kmg]i?b)\)?$/i;
  const squashText = (t) => String(t || "").replace(/\s+/g, " ").trim();
  // A text node that is upload state: a status word, a file size, or a file chip (a
  // file name with at most a few words around it: "cv.pdf uploaded", "Selected: cv.pdf").
  const uploadState = (text) => {
    const t = squashText(text);
    if (!t || t.length > 160) return false;
    if (UPLOAD_STATE.test(t) || FILE_SIZE.test(t)) return true;
    const rest = t.replace(FILE_NAMES, "");
    return rest !== t && squashText(rest).length <= 12;
  };
  const fileText = (root) => {
    const parts = [];
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        if (!uploadState(node.nodeValue)) parts.push(node.nodeValue);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      const tag = el.tagName;
      if (["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "SELECT", "TEXTAREA", "INPUT", "OPTION", "A", "BUTTON"].includes(tag)) return;
      if (el.matches('[role="button"],[role="progressbar"],[role="status"],[role="alert"],[aria-live]')) return;
      if (el.getAttribute("aria-hidden") === "true" || el.hidden) return;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") return;
      for (const c of el.childNodes) walk(c);
      parts.push(" ");
    };
    walk(root);
    return squashText(parts.join(" "));
  };
  const fileLabelOf = (el) => {
    const byLabelledby = byIds(el.getAttribute("aria-labelledby")).map((n) => fileText(n)).join(" ").trim();
    if (byLabelledby) return [byLabelledby, "aria-labelledby"];
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map((l) => fileText(l)).join(" ").trim();
      if (t) return [t, "label"];
      // A label that is only the uploader's button still names the question ("Upload
      // resume"), unless the button says nothing but a verb ("Attach").
      const whole = Array.from(el.labels).map((l) => textOf(l)).join(" ").trim();
      if (whole && !/^(?:attach|upload|browse|choose|select|add)(?:\s+(?:a\s+)?files?)?$/i.test(whole)) {
        return [whole, "label"];
      }
    }
    const aria = (el.getAttribute("aria-label") || "").trim();
    if (aria) return [aria, "aria-label"];
    const title = (el.getAttribute("title") || "").trim();
    if (title) return [title, "title"];
    return ["", "none"];
  };
  const UPLOAD_TRIGGER = /\b(?:attach|upload|browse|choose|select|drop|drag|add)\b/i;
  // The visible element a person uses to attach a file to a hidden input: its label,
  // an upload button or link in the input's own box, or a control naming it in
  // aria-controls. Only the text is reported; nothing is clicked.
  const uploadTriggerOf = (el, container) => {
    const nameOf = (n) => squashText((n.tagName === "INPUT" ? n.value : textOf(n)) ||
      n.getAttribute("aria-label") || n.getAttribute("title") || "");
    for (const l of el.labels || []) if (visible(l) && nameOf(l)) return nameOf(l).slice(0, 120);
    const scope = container || el.parentElement;
    if (scope) {
      // Buttons, links, labels, and focusable drop zones ("Drag 'n' drop, or click to select").
      const TRIGGERS = 'button, [role="button"], a[href], label, input[type="button"], [tabindex]:not([tabindex="-1"])';
      for (const n of [scope, ...scope.querySelectorAll(TRIGGERS)]) {
        if (n === scope && !n.matches(TRIGGERS)) continue;
        if (!visible(n)) continue;
        const t = nameOf(n);
        if (t && UPLOAD_TRIGGER.test(t)) return t.slice(0, 120);
      }
    }
    if (el.id) {
      for (const n of document.querySelectorAll("[aria-controls]")) {
        if (n.getAttribute("aria-controls").split(/\s+/).includes(el.id) && visible(n)) return (nameOf(n) || "upload").slice(0, 120);
      }
    }
    return "";
  };
  // A file input kept outside every form in an upload popup (Jobvite appends one
  // "Attachment Options" dialog per upload button to <body>) belongs to the one popup
  // button inside a form that names the same document as the popup ("Add Resume*" and
  // "Type or Paste Resume"; "Add Cover Letter"). That button stands in for the input: its
  // name is the question, its form and requiredness are the input's. Nothing is clicked.
  const DOC_KINDS = [["resume", /\b(?:r[eé]sum[eé]|cv)\b/i], ["cover letter", /\bcover\s+letter\b/i]];
  const docKinds = (text) => DOC_KINDS.filter(([, rx]) => rx.test(text || "")).map(([kind]) => kind);
  const triggerName = (b) => squashText(labelOf(b)[0] || textOf(b));
  // All of a (hidden) popup's text, one text node apart from the next.
  const popupText = (root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const parts = [];
    while (walker.nextNode()) parts.push(walker.currentNode.nodeValue);
    return parts.join(" ");
  };
  let popupUploads = null;
  const popupTriggerOf = (el) => {
    if (!popupUploads) {
      popupUploads = new Map();
      const inputs = new Map();
      for (const input of document.querySelectorAll('input[type="file"]')) {
        const popup = input.form || input.closest("form") ? null : input.closest('[role="dialog"]');
        if (popup) inputs.set(popup, [...(inputs.get(popup) || []), input]);
      }
      const buttons = inputs.size ? Array.from(document.querySelectorAll(
        'form [aria-haspopup]:not([aria-haspopup="false"]):not([aria-haspopup="listbox"])')) : [];
      const claimed = new Map();
      for (const [popup, found] of inputs) {
        const kinds = docKinds(popupText(popup));
        if (found.length !== 1 || kinds.length !== 1) continue;
        const matches = buttons.filter((b) => !popup.contains(b) && docKinds(triggerName(b)).join() === kinds[0]);
        if (matches.length !== 1) continue;
        popupUploads.set(found[0], matches[0]);
        claimed.set(matches[0], (claimed.get(matches[0]) || 0) + 1);
      }
      // One button, one popup: a button two popups name belongs to neither.
      for (const [input, b] of [...popupUploads]) if (claimed.get(b) > 1) popupUploads.delete(input);
    }
    return popupUploads.get(el) || null;
  };
  // Where such an upload shows its file: the button's nearest ancestor with an id of its
  // own that holds no visible question, else the widest such ancestor.
  const triggerBoxOf = (b) => {
    const fields = 'input:not([type=hidden]),select,textarea,[role=combobox],[role=textbox]';
    let box = null;
    for (let n = b.parentElement, d = 0; n && d < 8 && n !== document.body && n.tagName !== "FORM";
      n = n.parentElement, d++) {
      if (Array.from(n.querySelectorAll(fields)).some(visible)) break;
      box = n;
      if (n.id && unique("#" + CSS.escape(n.id))) return "#" + CSS.escape(n.id);
    }
    return box ? selectorFor(box) : "";
  };

  // ---- dialogs (modal wizards, banners, picker popups) ----------------------------
  // Python decides which dialog, if any, is the application form (a modal wizard);
  // here every element only reports the innermost dialog-like element holding it.
  const DIALOG_SEL = '[role="dialog"], [role="alertdialog"], dialog, [aria-modal="true"]';
  const dialogEls = deepAll(DIALOG_SEL).slice(0, 60);
  const dialogIndexes = new Map(dialogEls.map((d, i) => [d, i]));
  const dialogIndexOf = (el) => {
    for (let n = el; n && n.nodeType === 1; n = parentOf(n)) {
      if (dialogIndexes.has(n)) return dialogIndexes.get(n);
    }
    return -1;
  };
  const stepIn = (root) => {
    const current = root.querySelector('[aria-current="step"]');
    if (current && current.parentElement) {
      const items = Array.from(current.parentElement.children);
      return { current: items.indexOf(current) + 1, total: items.length, source: "aria-current" };
    }
    const text = (root === document.body ? document.body.innerText : root.innerText) || "";
    const m = text.match(/\bstep\s+(\d+)\s*(?:of|\/)\s*(\d+)\b/i);
    return m ? { current: Number(m[1]), total: Number(m[2]), source: "text" } : null;
  };

  // ---- forms and their controls --------------------------------------------------
  const forms = deepAll("form");
  const formIndex = (el) => (el.form ? forms.indexOf(el.form) : -1);
  // A combobox's hidden validation proxy (react-select's RequiredInput: aria-hidden,
  // tabindex -1, rendered only while the menu has no value) is part of that widget.
  const comboProxy = (el) => el.tagName === "INPUT" && el.getAttribute("aria-hidden") === "true" &&
    el.tabIndex === -1 && !!el.parentElement &&
    Array.from(el.parentElement.querySelectorAll('[role="combobox"]')).some((c) => c !== el);
  // So is a menu toggle's proxy select, and the search box in a toggle's menu.
  const proxySelects = new Set(menuToggles.values());
  const inTogglePopup = (el) => { for (const p of togglePopups) if (p.contains(el)) return true; return false; };
  const nativeControls = deepAll("input, select, textarea")
    .filter((el) => !SKIP_TYPES.has((el.type || "").toLowerCase()) && !comboProxy(el) &&
      !proxySelects.has(el) && !inTogglePopup(el));

  // A phone field's own country picker (an intl-tel-input flag, which may be a combobox
  // named "Country", or a dialog button before the number) belongs to that field: its
  // number is typed as +<code><digits>. It is never a question of its own.
  const phonePickerNodes = new Set();
  for (const tel of document.querySelectorAll("input[type=tel]")) {
    const picker = phonePicker(tel);
    if (picker && picker.kind !== "combobox") phonePickerNodes.add(picker.node);
  }
  const inPhonePicker = (el) => { for (const p of phonePickerNodes) if (p === el || p.contains(el)) return true; return false; };

  // A date input's calendar popup (Ashby's react-datepicker: a month listbox of day
  // options and unnamed month buttons, shown only while the input has focus) belongs to
  // that input: never a question, never a page button.
  const CALENDAR = '[class*="datepicker__month"], [class*="datepicker-popper"], [class*="datepicker__header"], ' +
    '[class*="calendar-popup"], [class*="DayPicker"], .flatpickr-calendar';
  const customWidgets = [];
  for (const el of deepAll("[role], [contenteditable]")) {
    // A <button> with a widget role (e.g. role="combobox") is a custom control, not an action.
    if (NATIVE.has(el.tagName)) continue;
    if (inPhonePicker(el) || inTogglePopup(el)) continue;
    if (el.closest(CALENDAR)) continue;
    const role = el.getAttribute("role");
    // A list inside a popup a combobox owns is that combobox's, never a question of its
    // own: Ashby's lookup names a wrapper around its suggestion listbox (a portal).
    if (["listbox", "menu", "tree", "grid"].includes(role) && Array.from(popupRoots).some((p) => p.contains(el))) continue;
    const editable = el.hasAttribute("contenteditable") && el.isContentEditable;
    if (!(CUSTOM_ROLES.has(role) || editable)) continue;
    // A list that is not shown is a closed popup (some menus leave theirs in the
    // document after closing), never a question of its own.
    if (role === "listbox" && !visible(el)) continue;
    if (el.querySelector("input:not([type=hidden]), select, textarea")) continue; // wraps native controls
    if (customWidgets.some((w) => w.contains(el))) continue;
    customWidgets.push(el);
  }
  for (const toggle of menuToggles.keys()) {
    if (inPhonePicker(toggle) || toggle.closest(CALENDAR) || customWidgets.some((w) => w.contains(toggle))) continue;
    customWidgets.push(toggle);
  }
  // Popups (listbox, menu) owned by a combobox, or by a role-less input with a popup,
  // belong to it.
  const ownedIds = new Set();
  const owners = nativeControls.filter((el) => el.getAttribute("role") === "combobox" || el.hasAttribute("aria-haspopup"));
  for (const w of [...customWidgets, ...owners]) {
    for (const attr of ["aria-controls", "aria-owns"]) {
      for (const id of (w.getAttribute(attr) || "").split(/\s+/).filter(Boolean)) ownedIds.add(id);
    }
    if (menuToggles.has(w)) for (const id of comboRefs(w)) ownedIds.add(id);
  }
  const ownedEls = new Set(Array.from(ownedIds).map(byId).filter(Boolean));
  const widgets = customWidgets.filter((w) => !(w.id && ownedIds.has(w.id)));
  const fieldEls = new Set([...nativeControls, ...widgets]);

  // A yes/no question drawn as toggle buttons (Ashby: two buttons with aria-pressed and a
  // display:none checkbox beside them that mirrors "yes"): the buttons are the question's
  // options, operated by clicking and read back by aria-pressed; they are not page
  // buttons. Only buttons that cannot submit a form by themselves (no form owner, or
  // type="button").
  const pressedOptionsOf = (el) => {
    if (el.type !== "checkbox" || !el.parentElement) return [];
    const buttons = Array.from(el.parentElement.children).filter((b) => b.tagName === "BUTTON" && b.hasAttribute("aria-pressed"));
    if (buttons.length < 2 || buttons.some((b) => !squashText(textOf(b)) || (b.form && b.type !== "button"))) return [];
    return buttons;
  };
  const pressedButtons = new Set();
  for (const el of nativeControls) for (const b of pressedOptionsOf(el)) pressedButtons.add(b);

  // Options of one question that do not share a name (Ashby: a checkbox or radio group
  // fieldset whose options are named after their own text, name="Yes"/name="No", or not
  // named at all): the question's box groups them. Only when every option of the box has
  // a distinct name that is empty or its own label's text (radios: any distinct names),
  // so separately named consents in one fieldset stay separate questions.
  const choiceBoxCache = new Map();
  const choiceBox = (el) => {
    if (!(el.type === "radio" || el.type === "checkbox")) return null;
    const box = el.closest('fieldset, [role="radiogroup"], [role="group"]');
    if (!box) return null;
    if (choiceBoxCache.has(box)) return choiceBoxCache.get(box);
    const inside = nativeControls.filter((o) => box.contains(o));
    const names = inside.map((o) => o.name || "");
    const ownText = (o) => squashText(Array.from(o.labels || []).map((l) => textOf(l)).join(" ")).toLowerCase();
    const grouped = inside.length > 1 && inside.every((o) => o.type === el.type)
      && new Set(names.filter(Boolean)).size === names.filter(Boolean).length
      && (el.type === "radio" || inside.every((o) => !o.name || squashText(o.name).toLowerCase() === ownText(o)));
    choiceBoxCache.set(box, grouped ? box : null);
    return grouped ? box : null;
  };
  const groupMembers = (el) => {
    const box = choiceBox(el);
    if (box) return nativeControls.filter((o) => box.contains(o));
    if (!(el.type === "radio" || el.type === "checkbox") || !el.name) return [el];
    return nativeControls.filter((o) => o.type === el.type && o.name === el.name && o.form === el.form);
  };
  const containerCache = new Map();
  // The question a control's own box states without labelling it (see questionOf).
  const QUESTION_HEADING = 'h1,h2,h3,h4,h5,h6,[role="heading"]';
  const ownLabel = (f) => !!(f.labels && f.labels.length) || f.hasAttribute("aria-labelledby") || f.hasAttribute("aria-label");
  const labelsNothing = (l) => {
    const target = l.getAttribute("for");
    if (target) return !document.getElementById(target);
    return !l.querySelector("input, select, textarea, [role]");
  };
  // A required marker drawn by CSS (Ashby: `._required_…::after { content: "*" }`) is part
  // of the question as shown.
  const drawn = (el, text) => {
    const star = (pseudo) => /^["'][*✱∗]["']$/.test((getComputedStyle(el, pseudo).content || "").trim());
    return (star("::before") ? "* " : "") + text + (star("::after") ? " *" : "");
  };
  const questionOf = (members, container) => {
    // 1. A <label> in the control's own box that labels nothing: Ashby writes
    //    <label for="<field path>"> while the input has no id at all.
    if (container) {
      const orphan = Array.from(container.querySelectorAll("label")).find((l) => visible(l) && labelsNothing(l)
        && (l.compareDocumentPosition(members[0]) & Node.DOCUMENT_POSITION_FOLLOWING));
      if (orphan && textOf(orphan)) return [drawn(orphan, textOf(orphan).slice(0, 500)), orphan.getAttribute("for") || ""];
    }
    // 2. The heading its block opens with (Breezy: <h3> just before the input, before a
    //    group's options, or first in a block its controls share), when no other field in
    //    that block has a label of its own (then the heading is a section's, not its own).
    //    A checkbox that states its own text (Breezy's SMS consent after the phone input)
    //    does not take a heading that opens another field first.
    const statement = members.length === 1 && members[0].type === "checkbox" && !!container && !!squashText(textOf(container));
    let passed = false;
    let child = members[0];
    if (members.length > 1) {
      let c = members[0].parentElement;
      while (c && !members.every((m) => c.contains(m))) c = c.parentElement;
      if (!c) return ["", ""];
      child = c;
    }
    for (let parent = child.parentElement, depth = 0; parent && depth < 4; child = parent, parent = parent.parentElement, depth++) {
      if (parent === document.body || parent.tagName === "FORM" || parent.tagName === "FIELDSET") break;
      if (Array.from(fieldEls).some((f) => parent.contains(f) && !members.includes(f) && ownLabel(f))) break;
      for (let sib = child.previousElementSibling; sib; sib = sib.previousElementSibling) {
        if (!visible(sib)) continue;
        const inner = sib.querySelectorAll(QUESTION_HEADING);
        const holds = Array.from(fieldEls).some((f) => sib.contains(f));
        const heading = sib.matches(QUESTION_HEADING) ? sib
          : inner.length === 1 && !holds && squashText(textOf(sib)) === squashText(textOf(inner[0])) ? inner[0] : null;
        if (heading) return passed && statement ? ["", ""] : [textOf(heading).slice(0, 500), ""];
        if (holds && Array.from(fieldEls).some((f) => sib.contains(f) && ownLabel(f))) return ["", ""];
        passed = passed || holds;
      }
    }
    return ["", ""];
  };
  // The largest box around a control that holds no other field: an unlabelled option's
  // own text (Breezy: <li><input type=checkbox><span>Implants</span></li>).
  const ownBox = (el) => {
    let box = null;
    for (let p = el.parentElement; p && p !== document.body && p.tagName !== "FORM"; p = p.parentElement) {
      if (Array.from(fieldEls).some((f) => f !== el && p.contains(f))) break;
      box = p;
    }
    return box;
  };
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
  const adjacentText = (container, exclude, upload) => {
    const texts = [];
    const errors = [];
    if (!container) return [texts, errors];
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        const t = node.nodeValue.replace(/\s+/g, " ").trim();
        if (t && !(upload && uploadState(t))) texts.push(t);
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
      // Around an upload control: file chips, progress and upload buttons are state.
      if (upload && el.matches('a,[role="button"],[role="progressbar"]')) return;
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
    const boundary = el.form || el.closest("form") || el.closest(DIALOG_SEL) || document.body;
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
    // A box that opens its parent takes the text before the parent (Lever: <div>Pronouns</div>
    // <div class="application-field"><ul>…the options…</ul>…</div>).
    const opens = (n) => { for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (visible(s)) return false; return true; };
    const before = (start) => {
      for (let depth = 0; depth < 3 && opens(start) && start.parentElement && start.parentElement !== document.body
           && start.parentElement.tagName !== "FORM"; depth++) {
        start = start.parentElement;
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
    if (container) return before(container);
    let start = members[0];
    while (start.parentElement && start.parentElement !== document.body && start.parentElement.tagName !== "FORM") {
      const parent = start.parentElement;
      let exclusive = true;
      for (const f of fieldEls) if (f !== members[0] && parent.contains(f)) { exclusive = false; break; }
      if (!exclusive) break;
      start = parent;
    }
    const own = before(start);
    if (own || members.length === 1) return own;
    // A group whose options share their box with another field (Lever's pronouns and their
    // "Custom" input): the text before the options' common box.
    let common = members[0].parentElement;
    while (common && !members.every((m) => common.contains(m))) common = common.parentElement;
    return common && common !== document.body && common.tagName !== "FORM" ? before(common) : "";
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
    const pressed = pressedOptionsOf(el);
    for (const b of pressed) exclude.add(b);  // its options, not text around it
    // A group's unlabelled options state their text in their own boxes: option text, not
    // text around the group.
    const unlabelled = members.length > 1 ? members.filter((m) => !(m.labels && m.labels.length)) : [];
    for (const m of unlabelled) { const b = ownBox(m); if (b) exclude.add(b); }
    const fs = el.closest("fieldset");
    if (fs) for (const d of byIds(fs.getAttribute("aria-describedby"))) exclude.add(d);
    const combo = comboLike(el);
    const displayNodes = combo ? comboDisplayNodes(el) : [];
    for (const n of displayNodes) exclude.add(n);
    const picker = phonePicker(el);
    const tag = el.tagName.toLowerCase();
    const type = tag === "input" ? (el.type || "text").toLowerCase() : tag;
    const upload = type === "file";
    if (upload) {
      // The group's own label is reported as group_label, never again as description.
      const g = el.closest('[role="group"]');
      if (g) for (const n of byIds(g.getAttribute("aria-labelledby"))) exclude.add(n);
    }
    // An upload popup's input takes its question and form from its button (popupTriggerOf);
    // the popup's own text (its menu of sources) is not about the question.
    const trigger = upload ? popupTriggerOf(el) : null;
    const [adjacent, adjacentErrors] = trigger ? [[], []] : adjacentText(container, exclude, upload);
    const [label, labelSource] = trigger ? [triggerName(trigger), "trigger"]
      : upload ? fileLabelOf(el) : labelOf(el);
    const [legend, legendSelector, legendDescribed] = legendOf(el);
    const [groupLabel, groupDescribed] = groupLabelOf(el);
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
      section_context: sectionContextOf(trigger || el),
      preceding: trigger ? "" : precedingTextOf(members, container),
      ...(([q, qFor]) => ({question: q, question_for: qFor}))(trigger ? ["", ""] : questionOf(members, container)),
      adjacent,
      adjacent_errors: adjacentErrors,
      option_text: unlabelled.includes(el) && !label ? (() => { const b = ownBox(el); return b ? textOf(b).slice(0, 300) : ""; })() : "",
      choice_group: (() => { const b = choiceBox(el); return b ? selectorFor(b) : ""; })(),
      pressed_options: pressed.map((b) => ({ label: squashText(textOf(b)), selector: selectorFor(b), pressed: b.getAttribute("aria-pressed") === "true" })),
      label_selector: el.labels && el.labels.length ? selectorFor(el.labels[0]) : null,
      required: el.required || el.getAttribute("aria-required") === "true" ||
        !!(el.closest('[aria-required="true"]')) || (!!trigger && trigger.getAttribute("aria-required") === "true"),
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
      form_index: trigger ? forms.indexOf(trigger.form || trigger.closest("form")) : formIndex(el),
      has_value: false,
      aria: ariaObserve(el) || comboFacts(el),
      phone_picker: !picker ? "" : picker.kind === "combobox" ? "combobox:" + (picker.node.id || "") : picker.kind,
      upload_trigger: !upload ? "" : trigger ? (textOf(trigger) || triggerName(trigger)).slice(0, 120)
        : uploadTriggerOf(el, container),
      upload_anchor: trigger ? triggerBoxOf(trigger) : "",
      dialog_index: dialogIndexOf(el),
    };
  };

  const describeCustom = (el) => {
    // A menu toggle is named (label, name) and made required by its proxy select.
    const proxy = menuToggles.get(el) || null;
    const container = containerFor([el], el.closest("form"));
    const exclude = new Set([...byIds(el.getAttribute("aria-labelledby")), ...byIds(el.getAttribute("aria-describedby")),
      ...popupRoots]);
    for (const id of ownedIds) { const o = byId(id); if (o) exclude.add(o); }
    if (proxy) for (const n of [...(proxy.labels || []), ...byIds(proxy.getAttribute("aria-describedby"))]) exclude.add(n);
    const [adjacent, adjacentErrors] = adjacentText(container, exclude);
    const proxyLabel = proxy ? labelOf(proxy) : ["", "none"];
    const [label, labelSource] = proxyLabel[0] ? proxyLabel : labelOf(el);
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
      type: el.getAttribute("role") || (proxy ? "combobox" : "contenteditable"),
      name: proxy ? proxy.getAttribute("name") || "" : hiddenInput ? hiddenInput.getAttribute("name") || "" : "",
      id: el.id || "",
      selector: selectorFor(el),
      role: el.getAttribute("role") || "",
      autocomplete_list: false,
      label,
      label_source: labelSource,
      described: proxy ? [...described(el), ...described(proxy)] : described(el),
      error_message: "",
      legend,
      legend_selector: legendSelector,
      legend_described: legendDescribed,
      group_label: null,
      group_described: [],
      section_context: sectionContextOf(el),
      preceding: precedingTextOf([el], container),
      ...(([q, qFor]) => ({question: q, question_for: qFor}))(questionOf([el], container)),
      adjacent,
      adjacent_errors: adjacentErrors,
      label_selector: null,
      required: el.getAttribute("aria-required") === "true" ||
        !!(proxy && (proxy.required || proxy.getAttribute("aria-required") === "true")),
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
      invalid: el.getAttribute("aria-invalid") === "true" || !!(proxy && proxy.getAttribute("aria-invalid") === "true"),
      image_alts: [],
      form_index: form ? forms.indexOf(form) : -1,
      has_value: hasValue,
      aria: ariaObserve(el) || comboFacts(el),
      phone_picker: "",
      upload_trigger: "",
      dialog_index: dialogIndexOf(el),
    };
  };

  const controls = [...nativeControls.map(describeNative), ...widgets.map(describeCustom)];
  // Keep document order.
  const order = [...nativeControls, ...widgets];
  const positioned = controls.map((c, i) => [order[i], c]);
  positioned.sort((a, b) => composedOrder.get(a[0]) - composedOrder.get(b[0]));

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
  for (const el of deepAll('button, input[type=submit], input[type=button], input[type=image], input[type=reset], [role="button"]')) {
    if (!visible(el) || inPopup(el) || comboButton(el) || el.closest(CALENDAR) || pressedButtons.has(el)) continue;
    if (CUSTOM_ROLES.has(el.getAttribute("role") || "") || menuToggles.has(el)) continue; // a widget, reported as a control
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
      dialog_index: dialogIndexOf(el),
      toggle: el.hasAttribute("aria-pressed"),
    });
  }
  // An anchor inside a form that goes nowhere ("#", empty, javascript:) is that form's
  // script-driven button (JazzHR's "Submit Application"). It never submits by itself
  // and is classified by its own wording only.
  for (const a of deepAll("form a")) {
    if (!visible(a) || inPopup(a) || a.getAttribute("role") === "button" || CUSTOM_ROLES.has(a.getAttribute("role") || "")) continue;
    const href = (a.getAttribute("href") || "").trim();
    if (href && href !== "#" && !/^javascript:/i.test(href)) continue;
    const text = textOf(a) || a.getAttribute("aria-label") || a.title || "";
    if (!text) continue;
    buttons.push({
      text: text.replace(/\s+/g, " ").trim(),
      type: "link-button",
      selector: selectorFor(a),
      disabled: a.getAttribute("aria-disabled") === "true" || /\bdisabled\b/.test(classOf(a)),
      form_index: forms.indexOf(a.closest("form")),
      submits_form: false,
      form_no_validate: false,
      effective_method: "",
      effective_action: "",
      dialog_index: dialogIndexOf(a),
    });
  }
  const links = [];
  for (const a of deepAll("a[href]")) {
    if (!visible(a) || inPopup(a)) continue;
    links.push({ text: textOf(a) || a.getAttribute("aria-label") || "", href: a.href, selector: selectorFor(a),
      dialog_index: dialogIndexOf(a) });
  }
  const headings = deepAll("h1, h2, h3")
    .filter((h) => visible(h) && !inPopup(h)).map((h) => ({ level: Number(h.tagName[1]), text: textOf(h) })).filter((h) => h.text);
  const regions = deepAll('[role="alert"], [role="status"], [aria-live]')
    .filter((r) => visible(r) && !inPopup(r)).map((r) => ({ role: r.getAttribute("role") || "live", text: textOf(r),
      dialog_index: dialogIndexOf(r) })).filter((r) => r.text);

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

  const step = stepIn(document.body);

  // Determinate progress indicators (a wizard's "25%" bar). An indeterminate one is a
  // loading signal instead.
  const determinate = (el) => el.tagName === "PROGRESS" ? el.hasAttribute("value")
    : (el.getAttribute("aria-valuenow") || "") !== "";
  const progress = [];
  for (const el of deepAll('progress, [role="progressbar"]')) {
    if (progress.length >= 20 || !visible(el) || !determinate(el)) continue;
    let value, max;
    if (el.tagName === "PROGRESS") {
      value = Number(el.value); max = Number(el.max);
    } else {
      const min = Number(el.getAttribute("aria-valuemin") || 0);
      value = Number(el.getAttribute("aria-valuenow")) - min;
      max = Number(el.getAttribute("aria-valuemax") || 100) - min;
    }
    if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0) continue;
    const text = labelOf(el)[0] || el.getAttribute("aria-valuetext") || textOf(el.parentElement);
    const owner = el.closest("form");
    progress.push({ value, max, text: text.slice(0, 200), form_index: owner ? forms.indexOf(owner) : -1,
      dialog_index: dialogIndexOf(el) });
  }
  const dialogs = dialogEls.map((d, i) => {
    const byLabelledby = byIds(d.getAttribute("aria-labelledby")).map((n) => textOf(n)).join(" ").trim();
    const heading = Array.from(d.querySelectorAll('h1,h2,h3,h4,[role="heading"]')).find(visible);
    let modal = d.getAttribute("aria-modal") === "true";
    try { modal = modal || (d.tagName === "DIALOG" && d.matches(":modal")); } catch (e) { /* older engines */ }
    return {
      index: i,
      selector: selectorFor(d),
      label: (byLabelledby || (d.getAttribute("aria-label") || "").trim() || (heading ? textOf(heading) : "")).slice(0, 300),
      modal,
      visible: visible(d),
      parent: parentOf(d) ? dialogIndexOf(parentOf(d)) : -1,
      step: stepIn(d),
    };
  });

  const CAPTCHA_FRAME = /recaptcha|hcaptcha|challenges\.cloudflare|turnstile|arkoselabs|funcaptcha|captcha/i;
  const captchaFrames = Array.from(document.querySelectorAll("iframe"))
    .filter((f) => CAPTCHA_FRAME.test(f.src || f.title || ""))
    .map((f) => ({ src: f.src || "", title: f.title || "", visible: visible(f) }));
  const frames = Array.from(document.querySelectorAll("iframe"))
    .filter((f) => !CAPTCHA_FRAME.test(f.src || f.title || "")).slice(0, 30)
    .map((f) => {
      const r = f.getBoundingClientRect();
      return { id: f.id || "", src: f.getAttribute("src") ? f.src : "", title: f.title || f.getAttribute("aria-label") || "",
        visible: visible(f), width: r.width, height: r.height };
    });
  const tokens = Array.from(document.querySelectorAll('[name="g-recaptcha-response"], [name="h-captcha-response"], [name="cf-turnstile-response"]'))
    .map((t) => ({ name: t.getAttribute("name"), filled: !!t.value }));

  const bodyText = (document.body ? document.body.innerText || "" : "").slice(0, MAX_TEXT);
  // Loading signals only delay readiness (bounded) for a page that does not classify yet.
  const LOADING_TEXT = /\b(?:loading|fetching|please wait|one moment)\b/i;
  const loadingIndicator = LOADING_TEXT.test(bodyText) ||
    deepAll('[aria-busy="true"], [role="progressbar"]')
      .some((el) => visible(el) && !(el.getAttribute("role") === "progressbar" && determinate(el)));

  // Work in progress the runtime waits out (bounded) before and while filling: busy
  // regions, progress bars and short standalone status texts ("Loading...", a button
  // reading "Loading...", "Analyzing resume...", "Uploading 40%").
  const BUSY_WORD = /^(?:loading|please wait|one moment)\b/i;
  const BUSY_PROGRESS = /^(?:uploading|parsing|processing|analy[sz]ing|autofilling|auto-filling|importing|fetching|saving|scanning)\b.*(?:\.{2,}|…|\d+\s*%)$/i;
  const busy = [];
  for (const b of document.querySelectorAll('[aria-busy="true"], [role="progressbar"]')) {
    if (busy.length >= 8) break;
    if (visible(b)) busy.push(((b.getAttribute("role") || "busy") + ": " + (textOf(b) || b.getAttribute("aria-label") || "")).slice(0, 80));
  }
  if (document.body) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node && busy.length < 8; node = walker.nextNode()) {
      const t = squashText(node.nodeValue);
      if (!t || t.length > 60 || !(BUSY_WORD.test(t) || BUSY_PROGRESS.test(t))) continue;
      const parent = node.parentElement;
      if (!parent || ["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "OPTION", "TEXTAREA"].includes(parent.tagName)) continue;
      const whole = squashText(parent.textContent);
      if (whole.length > 60 || !visible(parent)) continue;
      busy.push(("text: " + whole).slice(0, 80));
    }
  }

  // Visible dialogs with their buttons: Python recognises an offer to autofill the
  // application and its decline control ("No thanks"); nothing here is clicked.
  const promptEls = [];
  for (const d of document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open], [aria-modal="true"]')) {
    if (promptEls.length >= 4) break;
    if (!visible(d) || inPopup(d) || promptEls.some((p) => p.contains(d) || d.contains(p))) continue;
    promptEls.push(d);
  }
  const prompts = promptEls.map((d) => ({
    text: textOf(d).slice(0, 600),
    modal: d.getAttribute("aria-modal") === "true" || (d.tagName === "DIALOG" && d.matches(":modal")),
    dialog_index: dialogIndexOf(d),
    buttons: Array.from(d.querySelectorAll('button, [role="button"], a[href], input[type="button"], input[type="submit"]'))
      .filter(visible).slice(0, 12).map((b) => ({
        text: squashText((b.tagName === "INPUT" ? b.value : textOf(b)) || b.getAttribute("aria-label") || b.getAttribute("title") || "").slice(0, 120),
        selector: selectorFor(b),
        // A submit of a form other than <form method="dialog"> (which only closes the
        // dialog), and a link to another document: neither ever declines an offer.
        submits: (b.tagName === "BUTTON" || b.tagName === "INPUT") && b.type === "submit" && !!b.form &&
          String(b.getAttribute("formmethod") || b.form.getAttribute("method") || "").toLowerCase() !== "dialog",
        navigates: b.tagName === "A" && !/^\s*(?:#|javascript:|$)/i.test(b.getAttribute("href") || ""),
      })),
  }));

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
    password_visible: deepAll("input[type=password]").some(visible),
    captcha_frames: captchaFrames,
    captcha_tokens: tokens,
    captcha_widget: !!document.querySelector(".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]"),
    loading_indicator: loadingIndicator,
    busy,
    prompts,
    document: String(performance.timeOrigin) + " " + location.href,
    dialogs,
    progress,
    frames,
  };
}
