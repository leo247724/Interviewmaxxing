// Built In job page (/job/<slug>/<id>). The page publishes a schema.org JobPosting
// (JSON-LD), which carries the employer description, explicit salary bounds and
// remote eligibility. The APPLY button is a Built In redirect handler and is not
// followed here.
let posting = null;
for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
  try {
    const data = JSON.parse(s.textContent);
    const nodes = Array.isArray(data) ? data : data['@graph'] || [data];
    posting = nodes.find((n) => n && n['@type'] === 'JobPosting') || posting;
  } catch (e) {
    // ignore malformed blocks; absence is reported as posting: null
  }
}
const main = document.querySelector('main') || document.body;
const text = (main.innerText || '').slice(0, 4000);
const apply = document.querySelector('#applyButton');
return JSON.stringify({
  page: __page(),
  posting,
  h1: __clean(document.querySelector('h1')?.innerText),
  apply_href: apply ? apply.href : null,
  closed: /no longer accepting applications|this job (is|has been) (closed|filled)|job (is )?no longer available|this job has expired/i.test(text),
});
