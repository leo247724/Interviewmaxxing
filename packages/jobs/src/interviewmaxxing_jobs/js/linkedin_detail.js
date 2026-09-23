// LinkedIn job detail page (/jobs/view/<id>/, layout observed 2026-09-22). The top
// card has no stable classes, so the text above "About the job" is returned as
// lines and parsed in Python. The description is the first expandable text box
// after the "About the job" heading (older layouts: #job-details).
const main = document.querySelector('main') || document.body;
const full = main.innerText || '';
const cut = full.indexOf('About the job');
const top = (cut >= 0 ? full.slice(0, cut) : full.slice(0, 1500))
  .split('\n')
  .map((s) => s.trim())
  .filter(Boolean);

let description = null;
const heading = Array.from(main.querySelectorAll('h1, h2, h3, h4, span, p, div')).find(
  (e) => e.children.length === 0 && __clean(e.textContent) === 'About the job',
);
if (heading) {
  const box = Array.from(main.querySelectorAll('[data-testid="expandable-text-box"]')).find(
    (b) => heading.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING,
  );
  if (box) description = box.innerText;
}
if (!description) {
  const old = document.querySelector('#job-details, .jobs-description__content, .jobs-box__html-content');
  if (old) description = old.innerText;
}

const applyLinks = Array.from(main.querySelectorAll('a[href]'))
  .filter((a) => /apply/i.test(a.getAttribute('aria-label') || '') || __clean(a.innerText) === 'Apply')
  .map((a) => ({ text: __clean(a.innerText), label: a.getAttribute('aria-label'), href: a.href }));
const easyApply = Array.from(main.querySelectorAll('button, a')).some((e) =>
  /easy apply/i.test((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')),
);

return JSON.stringify({
  page: __page(),
  document_title: document.title,
  top_lines: top.slice(0, 40),
  description,
  apply_links: applyLinks.slice(0, 5),
  easy_apply: easyApply,
  closed: /no longer accepting applications/i.test(full.slice(0, 6000)),
  unavailable: /this job is no longer available|job not found|page not found/i.test(full.slice(0, 3000)),
});
