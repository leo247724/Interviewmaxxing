// The Google Jobs detail pane for the selected result. Google also pre-renders
// neighbouring results as previews, so only the non-preview pane is read.
const pane = document.querySelector('div[data-encoded-docid][data-is-preview="false"]');
if (!pane) return JSON.stringify({ page: __page(), active: null });
return JSON.stringify({
  page: __page(),
  active: {
    encoded_docid: pane.getAttribute('data-encoded-docid'),
    heading: __clean(pane.querySelector('h1, [role="heading"]')?.innerText),
    text: (pane.innerText || '').slice(0, 20000),
    apply_links: Array.from(pane.querySelectorAll('a[href]'))
      .filter((a) => /^apply\b/i.test(__clean(a.innerText)))
      .map((a) => ({ text: __clean(a.innerText), href: a.href }))
      .slice(0, 12),
  },
});
