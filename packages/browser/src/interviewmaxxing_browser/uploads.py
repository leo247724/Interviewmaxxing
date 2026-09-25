"""Read-only observation of a file upload after the runtime attached a file.

Hosted application forms rarely show a bare ``<input type=file>``: a styled "Attach"
button or a drop zone sits over a hidden input, the page may move the chosen file into
its own state (and empty the input), show a chip with the file name, run an
asynchronous upload behind a spinner ("Uploading...", "Analyzing resume...") and
announce the result in a live region. :data:`UPLOAD_STATE` reads all of that for one
control without changing the page (it is allowlisted for OpenCLI like every other read
script); :class:`UploadState` holds the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

UPLOAD_STATE = r"""({selector, names, anchor, stuck}) => {
  const squash = (t) => String(t || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const shown = (el) => {
    if (!el || !el.isConnected) return false;
    for (let n = el; n; n = n.parentElement) {
      if (n.hidden || n.hasAttribute('inert') || n.getAttribute('aria-hidden') === 'true') return false;
      const s = getComputedStyle(n);
      if (s.display === 'none' || s.visibility === 'hidden') return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  let found = [];
  try { found = Array.from(document.querySelectorAll(selector)); } catch (e) { found = []; }
  let anchored = null;
  if (anchor) { try { const a = document.querySelectorAll(anchor); if (a.length === 1) anchored = a[0]; } catch (e) { anchored = null; } }
  // When the selector now names another kind of element or several, the input the file
  // was set on is gone (an uploader handed its id on to a fresh input and to a hidden
  // field of its preview): its container recorded before attaching is read instead.
  const replaced = found.length > 1 || (found.length === 1 && found[0].type !== 'file');
  const el = replaced && anchored ? null : (found[0] || null);
  const wanted = names.map((n) => squash(n).toLowerCase()).filter(Boolean);
  const mentions = (t) => { const s = squash(t).toLowerCase(); return wanted.some((n) => s.includes(n)); };
  const files = el && el.files ? Array.from(el.files).map((f) => ({name: f.name, size: f.size})) : [];
  // The control's own box: the largest ancestor (at most the form) with no other file input.
  const others = Array.from(document.querySelectorAll('input[type="file"]')).filter((f) => f !== el);
  let scope = null;
  if (el) {
    for (let n = el.parentElement, i = 0; n && i < 8 && n !== document.body; n = n.parentElement, i++) {
      if (others.some((o) => n.contains(o))) break;
      scope = n;
      if (n.tagName === 'FORM') break;
    }
  }
  // Without the input (an uploader replaced it with the file's name), its container as
  // recorded before attaching, when that still names exactly one element.
  const box = scope || (el ? null : anchored) || (el && el.form) || document.body;
  const chip = mentions(box.innerText);
  let notice = null;
  const DONE = /\b(?:uploaded|attached|upload(?:ed)? (?:complete|successful)|success(?:ful(?:ly)?)?)\b/i;
  const BAD = /\b(?:fail|error|could ?n[o'\u2019]?t|unable|too large|exceeds|invalid|not supported|unsupported)/i;
  for (const r of document.querySelectorAll('[aria-live], [role="status"], [role="alert"]')) {
    if (!shown(r)) continue;
    const t = squash(r.innerText);
    if (!t || BAD.test(t)) continue;
    if (mentions(t) || (box.contains(r) && DONE.test(t))) { notice = t.slice(0, 200); break; }
  }
  // Progress counts in the control's own field only: the largest box around the input,
  // inside its container, that holds no other visible field. A helper elsewhere in the
  // form that stays "Loading..." (Lever's "Apply with LinkedIn" above the resume) is not
  // this upload's progress, and neither is a marker that already outlasted a whole
  // bounded wait in this document (``stuck``, worded as the inspector reports it).
  const FIELDS = 'input:not([type="hidden"]), select, textarea';
  let field = null;
  if (el) {
    for (let n = el.parentElement; n && box.contains(n); n = n.parentElement) {
      if (Array.from(n.querySelectorAll(FIELDS)).some((f) => f !== el && shown(f))) break;
      field = n;
      if (n === box) break;
    }
  }
  field = field || (el && el.parentElement) || box;
  const known = new Set((stuck || []).map((s) => squash(s)));
  const BUSY = /^(?:uploading|parsing|processing|analy[sz]ing|scanning|loading|autofilling|reading|please wait)\b/i;
  let busy = false;
  for (const b of field.querySelectorAll('[aria-busy="true"], [role="progressbar"]')) {
    if (!shown(b)) continue;
    const marker = ((b.getAttribute('role') || 'busy') + ': ' + (squash(b.innerText) || b.getAttribute('aria-label') || '')).slice(0, 80);
    if (!known.has(squash(marker))) { busy = true; break; }
  }
  if (!busy) {
    const walker = document.createTreeWalker(field, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const t = squash(n.nodeValue);
      if (!t || t.length > 80 || !BUSY.test(t) || !shown(n.parentElement)) continue;
      if (!known.has(squash(('text: ' + squash(n.parentElement.textContent)).slice(0, 80)))) { busy = true; break; }
    }
  }
  const FAILED = /\b(?:upload(?:ing)? failed|failed to (?:upload|attach)|could ?n[o'\u2019]?t (?:upload|attach)|unable to (?:upload|attach)|too large|exceeds the maximum|file type is not (?:allowed|supported)|unsupported file|invalid file)\b/i;
  let error = null;
  for (const n of box.querySelectorAll('[role="alert"], [aria-live], [class*="error" i], [id*="error" i]')) {
    if (!shown(n)) continue;
    const t = squash(n.innerText);
    if (t && FAILED.test(t)) { error = t.slice(0, 200); break; }
  }
  return {connected: !!el, files, chip, notice, busy, error};
}"""
"""Read-only: what one upload control and its surroundings show now. ``names`` are the
file names to look for (the file the runtime attached); ``anchor`` (optional) is the
uploader's own container, read when the input itself is gone; ``stuck`` (optional) are
busy markers, as the inspector words them ("text: Loading…"), that already outlasted a
whole bounded wait in this document and so are not this upload's progress."""


@dataclass(frozen=True)
class UploadState:
    """One read of an upload control after attaching ``names``."""

    connected: bool = False
    """The control is in the document."""
    files: list[dict[str, Any]] = field(default_factory=list)
    """``input.files`` as name/size pairs (empty when the page moved the file away)."""
    chip: bool = False
    """Visible text in the control's own box names the file (a file chip)."""
    notice: str | None = None
    """A live region or status message that names the file or says it was uploaded."""
    busy: bool = False
    """A spinner, progress bar or "Uploading..."-style text in the control's own field."""
    error: str | None = None
    """A visible upload failure message ("Upload failed", "File too large")."""

    @classmethod
    def from_raw(cls, raw: Any) -> UploadState:
        if not isinstance(raw, dict):
            return cls()
        files = raw.get("files")
        return cls(
            connected=bool(raw.get("connected")),
            files=[f for f in files if isinstance(f, dict)] if isinstance(files, list) else [],
            chip=bool(raw.get("chip")),
            notice=str(raw["notice"]) if raw.get("notice") else None,
            busy=bool(raw.get("busy")),
            error=str(raw["error"]) if raw.get("error") else None,
        )

    def holds(self, name: str, size: int) -> bool:
        """``input.files`` is exactly this one file (name and size)."""
        return self.files == [{"name": name, "size": size}]

    @property
    def shown(self) -> bool:
        """The page itself shows the file as attached (a chip or a notice)."""
        return self.chip or self.notice is not None

    def confirms(self, name: str, size: int) -> bool:
        """Readback of an attachment: the control holds the file, or the page moved it
        into its own state (the input is empty) and shows it attached."""
        return self.holds(name, size) or (not self.files and self.shown)
