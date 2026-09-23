// LinkedIn Jobs search results (two-pane /jobs/search/ layout observed 2026-09-22).
// Every result <li> carries its job id; LinkedIn renders card bodies lazily, and a
// background (document.hidden) tab only renders the first few, so `rendered` says
// which cards have visible fields.
const cards = Array.from(document.querySelectorAll('li[data-occludable-job-id]')).map((li) => {
  const q = (s) => li.querySelector(s);
  const link = q('a.job-card-list__title--link, a.job-card-container__link');
  return {
    id: li.getAttribute('data-occludable-job-id'),
    rendered: !!q('.job-card-container'),
    title: __clean(q('.job-card-list__title--link strong')?.innerText),
    title_label: link?.getAttribute('aria-label') || null,
    company: __clean(q('.artdeco-entity-lockup__subtitle')?.innerText),
    caption: __clean(q('.artdeco-entity-lockup__caption')?.innerText),
    metadata: __clean(q('.artdeco-entity-lockup__metadata')?.innerText),
    footer: Array.from(li.querySelectorAll('.job-card-container__footer-item'))
      .map((e) => __clean(e.innerText))
      .filter(Boolean),
    href: link?.getAttribute('href') || null,
  };
});
return JSON.stringify({
  page: __page(),
  cards,
  results_text: __clean(document.querySelector('.jobs-search-results-list__subtitle')?.innerText),
  page_state: __clean(document.querySelector('.jobs-search-pagination__page-state')?.innerText),
  has_next: !!document.querySelector('button.jobs-search-pagination__button--next:not([disabled])'),
  no_results: !!document.querySelector('.jobs-search-no-results-banner, .jobs-search-two-pane__no-results-banner--expand'),
});
