// Shared read-only helpers prepended to every extraction script. Nothing here
// clicks, types, navigates or submits; scripts only read the rendered page.
const __clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
const __lines = (el) => ((el && el.innerText) || '').split('\n').map((s) => s.trim()).filter(Boolean);
// A challenge frame counts only when it is shown: many sites embed an invisible
// reCAPTCHA Enterprise anchor (size=invisible) on ordinary pages.
const __shownChallengeFrame = () =>
  Array.from(
    document.querySelectorAll(
      'iframe[src*="challenges.cloudflare.com"], iframe[src*="hcaptcha"], iframe[src*="recaptcha"]',
    ),
  ).some((f) => {
    const src = f.getAttribute('src') || '';
    if (/size=invisible/.test(src)) return false;
    const r = f.getBoundingClientRect();
    const style = getComputedStyle(f);
    return r.width >= 100 && r.height >= 50 && style.visibility !== 'hidden' && style.display !== 'none';
  });
const __page = () => {
  const title = document.title || '';
  const head = ((document.body && document.body.innerText) || '').slice(0, 1500);
  return {
    url: location.href,
    title,
    hidden: document.hidden,
    signals: {
      challenge:
        /just a moment|attention required|verify you are human|additional verification required|unusual traffic/i.test(
          title + ' ' + head,
        ) ||
        !!document.querySelector('#challenge-form, #cf-challenge-running, #captcha-form') ||
        __shownChallengeFrame(),
      password_field: !!document.querySelector('input[type=password]'),
      denied: /access denied|403 forbidden|request blocked/i.test(title),
    },
  };
};
