// Google Jobs list (udm=8 results for a "<title> jobs in <place>" query, observed
// 2026-09-22). Each result is a [data-share-url] block whose inner element id is
// Google's job document id.
const items = Array.from(document.querySelectorAll('[data-share-url]')).map((el, index) => ({
  index,
  doc_id: el.querySelector('[data-preview-id]')?.id || null,
  share_url: el.getAttribute('data-share-url'),
  lines: __lines(el).slice(0, 20),
}));
return JSON.stringify({ page: __page(), items });
