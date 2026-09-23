// Indeed US /jobs results (layout observed 2026-09-22). The location element also
// holds a personalised commute estimate and transit stop; those are removed so only
// the stated job location is returned.
const cards = Array.from(document.querySelectorAll('.job_seen_beacon')).map((card) => {
  const link = card.querySelector('a[data-jk]') || card.querySelector('h2.jobTitle a');
  const where = card.querySelector('[data-testid="text-location"]');
  let location = null;
  if (where) {
    const copy = where.cloneNode(true);
    copy
      .querySelectorAll('[data-testid="jcs-commute-snippet"], [data-testid^="transit"]')
      .forEach((e) => e.remove());
    location = __clean(copy.textContent).replace(/^[·•\s]+/, '') || null;
  }
  return {
    jk: link?.getAttribute('data-jk') || null,
    title: __clean(
      card.querySelector('[id^="jobTitle-"], a[data-jk] span[title], h2.jobTitle span[title]')?.getAttribute('title') ||
        card.querySelector('[id^="jobTitle-"], h2.jobTitle span')?.innerText,
    ),
    href: link?.getAttribute('href') || null,
    company: __clean(card.querySelector('[data-testid="company-name"]')?.innerText),
    location,
    salary: __clean(card.querySelector('[data-testid*="salary-snippet"], .salary-snippet-container')?.innerText) || null,
    attributes: Array.from(card.querySelectorAll('[data-testid~="attribute_snippet_testid"]'))
      .map((e) => __clean(e.innerText))
      .filter(Boolean),
  };
});
return JSON.stringify({
  page: __page(),
  cards,
  count_text: __clean(document.querySelector('.jobsearch-JobCountAndSortPane-jobCount, [data-testid="searchCountPages"]')?.innerText),
  next_href: document.querySelector('a[data-testid="pagination-page-next"]')?.getAttribute('href') || null,
  no_results: !!document.querySelector('[data-testid="noResultsMessage"], [data-testid="empty-serp-result"]'),
});
