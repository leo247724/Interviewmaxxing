// Built In /jobs results (layout observed 2026-09-22). Card attributes are only
// identifiable by their Font Awesome icon, so each is read next to its icon.
const cards = Array.from(document.querySelectorAll('[data-id="job-card"]')).map((card) => {
  const byIcon = (name) => {
    const icon = card.querySelector('i.' + name);
    const box = icon && (icon.closest('.d-flex.align-items-start') || icon.parentElement?.parentElement);
    return box ? __clean(box.innerText) || null : null;
  };
  const link = card.querySelector('a[data-id="job-card-title"]');
  return {
    id: (card.id || '').replace(/^job-card-/, '') || null,
    title: __clean(link?.innerText),
    href: link?.getAttribute('href') || null,
    company: __clean(card.querySelector('[data-id="company-title"]')?.innerText),
    posted: byIcon('fa-clock'),
    arrangement: byIcon('fa-house-building'),
    location: byIcon('fa-location-dot'),
    salary: byIcon('fa-sack-dollar'),
    level: byIcon('fa-trophy'),
  };
});
return JSON.stringify({
  page: __page(),
  cards,
  page_links: Array.from(document.querySelectorAll('a[href*="page="]')).map((a) => a.getAttribute('href')),
});
