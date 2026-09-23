// Indeed job page (/viewjob?jk=<key>). The page publishes a schema.org JobPosting
// (JSON-LD) with the full description; the header gives the visible company,
// location and pay. Apply controls are read, never clicked.
let posting = null;
for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
  try {
    const data = JSON.parse(s.textContent);
    const nodes = Array.isArray(data) ? data : data['@graph'] || [data];
    posting = nodes.find((n) => n && n['@type'] === 'JobPosting') || posting;
  } catch (e) {
    // ignore malformed blocks
  }
}
const header = document.querySelector('[data-testid="desktop-job-header"], .jobsearch-InfoHeaderContainer');
const details = document.querySelector('[data-testid="jobDetailsSection"], #jobDetailsSection');
const applyArea = document.querySelector('[data-testid="primary-apply-action"], #jobsearch-ViewJobButtons-container, .jobsearch-IndeedApplyButton-contentWrapper');
const body = ((document.body && document.body.innerText) || '').slice(0, 5000);
return JSON.stringify({
  page: __page(),
  posting,
  header_lines: __lines(header).slice(0, 20),
  detail_lines: __lines(details).slice(0, 40),
  description_text: document.querySelector('#jobDescriptionText')?.innerText || null,
  apply_links: Array.from((applyArea || document).querySelectorAll('a[href], button'))
    .filter((e) => /apply/i.test((e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '')))
    .map((e) => ({ text: __clean(e.innerText), label: e.getAttribute('aria-label'), href: e.href || null }))
    .slice(0, 5),
  expired: /this job has expired|job (is )?no longer available|no longer accepting applications/i.test(body),
});
