# Clay visual spec (for restyling the Interviewmaxxing dashboard)

Captured 2026-09-30 from the live https://www.clay.com/ with headless Chrome at 1440px.

**Evidence keys** (paths are relative to `~/projects/website-clones/clay-clone/`):
- `[T:path]` is `RECON/probe/clay-tokens.json`. It holds computed styles, `:root` vars, frequency tallies and the hover probe.
- `[SD]` is `RECON/probe/sections-detail.json`. It holds the per-element styles of the feature, flow, action, reps and updates sections.
- `[SS]` is `RECON/screenshots/original-1440.png`, the full-page screenshot. It is also copied as `clay-1440.png` next to this file.
- `[FOLD]` is `RECON/probe/clay-tokens-fold.png`, the first-viewport screenshot.
- `[REF]` is https://www.shadcn.io/design/clay (its DESIGN.md summary). `https://www.designmd.co/d/clay` returned 403 or 429 on every attempt, so nothing from it is used.
- `GUESS` marks a value with no direct measurement.

> **Reference conflict.** `[REF]` says Clay uses a `#fffaf0` canvas, Plain Black headlines and Inter body text. The live site renders none of these. It uses Roobert for all text (`[T:fonts]`: 2110 elements use Roobertvf, 0 use Inter). Its page and panel colors are `#FFFFFF`, `#FEFDFB` and `#F4F3F0` (`[T:bgs]`). This spec follows the live site. Values that exist only in `[REF]` are labelled REF.

---

## (a) Tokens: paste into `:root`

```css
:root {
  /* ---- Fonts ---- */
  /* Clay's font is Roobert (Displaay Type Foundry, commercial). The live site loads
     cdn.prod.website-files.com/.../RoobertVF.woff2 [T:fontRequests].
     We have no licence, so fall back to a similar geometric grotesk. */
  --font-sans: "Roobert", "Inter Tight", "Inter", ui-sans-serif, system-ui, sans-serif; /* Roobert: SOURCE [T:rootVars --fonts--primary-font-family]; fallbacks: GUESS */
  --font-mono: "Space Mono", ui-monospace, monospace; /* SOURCE: Google Fonts request [T:fontRequests] */

  /* ---- Neutrals ("oat") ---- all SOURCE [T:rootVars] */
  --oat-50:  #FFFCFA;
  --oat-100: #FEFDFB;   /* card and page tint; the most-used bg [T:bgs #1] */
  --oat-150: #F9F8F6;
  --oat-200: #F4F3F0;   /* section panels, tabs, dividers [T:bgs #2] */
  --oat-250: #F3F2ED;   /* secondary button bg [T:buttons[0]] */
  --oat-300: #EEE9DF;
  --oat-350: #D1CDC7;   /* hairline ring colour (used at 50–60% alpha) [T:shadows] */
  --oat-400: #DAD4C8;   /* secondary button hover [T:hover[6]] */
  --oat-500: #C0BBAF;
  --oat-600: #9F9B93;
  --oat-700: #7B7974;   /* muted text, nav link hover [T:colors #2, hover] */
  --oat-800: #55534E;   /* secondary text [T:colors] */
  --oat-900: #1B1A18;   /* warm ink [T:colors #3] */
  --black:   #000000;   /* primary ink and primary CTA [T:colors #1] */
  --white:   #FFFFFF;

  /* ---- Fruit accents: 100 = tint bg, 300 = saturated, 400 = button, 500 = ink on tint ---- SOURCE [T:rootVars terra-swatches] */
  --blueberry-100:#F0F8FF; --blueberry-200:#BEDFFE; --blueberry-300:#429EFF; --blueberry-400:#395AFA; --blueberry-500:#001433;
  --slushie-100:#F0FCFF;   --slushie-200:#AAEBFD;   --slushie-300:#3BD3FD;   --slushie-400:#008BAD;   --slushie-500:#002833;
  --lime-100:#FCFEE2;      --lime-200:#EEF773;      --lime-300:#CBD810;      --lime-400:#7F7F00;      --lime-500:#102B03;
  --ube-100:#F5F3FF;       --ube-200:#C8BBFB;       --ube-300:#A17BF9;       --ube-400:#6D4CD6;       --ube-500:#160038;
  --tangerine-100:#FFF3ED; --tangerine-200:#FCC9AB; --tangerine-300:#FF7714; --tangerine-400:#B53D0A; --tangerine-500:#381005;
  --pomegranate-100:#FFF1F2; --pomegranate-200:#FCBABE; --pomegranate-300:#FB4450; --pomegranate-400:#C22E3D; --pomegranate-500:#3A0308;
  --dragonfruit-100:#FFF0FA; --dragonfruit-200:#F8B9E4; --dragonfruit-300:#FF70D2; --dragonfruit-400:#CC089E; --dragonfruit-500:#46022F;
  --lemon-100:#FEFAE8;     --lemon-200:#FBE189;     --lemon-300:#FDBE11;     --lemon-400:#9E5802;     --lemon-500:#372201;
  --matcha-100:#DBFFE0;    --matcha-500:#0DAC65;    --matcha-700:#02693E;

  /* ---- Semantic mapping ---- */
  --bg:            var(--white);        /* body is #FFF [T:body] */
  --bg-subtle:     var(--oat-100);
  --panel:         var(--oat-200);
  --fg:            var(--black);
  --fg-warm:       var(--oat-900);
  --fg-secondary:  var(--oat-800);
  --fg-muted:      var(--oat-700);
  --paragraph:     color-mix(in srgb, #1B1A18 75%, transparent); /* SOURCE [T:rootVars --_theme--appearance---paragraph] */
  --border:        #E0DEDC;             /* SOURCE [T:rootVars --_theme--appearance---border] */
  --hairline:      rgba(209,205,199,.6);/* SOURCE [T:shadows] */
  --divider:       var(--oat-200);      /* 1px frame_line [SD] */
  --primary:       var(--black);  --primary-fg: var(--white);  --primary-hover: #282C35; /* SOURCE [T:hover[7]] */
  --secondary:     var(--oat-250); --secondary-fg: var(--black); --secondary-hover: var(--oat-400);
  --accent:        var(--lime-300);  /* hero "Get a demo" [T:buttons[3]] */
  --success: #0DAC65; --warning: #FBBD41; --danger: #DD2C53; --info: #0382F7; /* SOURCE [T:rootVars] matcha-500/lemon-500/pomegranate-600/blueberry-500 */
  --focus-ring:    var(--blueberry-400); /* GUESS */

  /* ---- Type scale ---- (size / line-height / weight / tracking) */
  --text-display: 88px;  /* 1.0, 575, -0.04em   SOURCE [T:headings.h1] */
  --text-h1:      72px;  /* 1.0, 500, -0.03em   SOURCE [T:headings.h2[0]] */
  --text-h2:      48px;  /* 1.0, 500, -0.04em   SOURCE [T:headings.h3] */
  --text-h3:      44px;  /* 1.1, 500, -0.02em   SOURCE [T:headings.h2[1]] */
  --text-h4:      24px;  /* 1.2, 500, -0.01em   SOURCE :root --heading-–-h4 1.5rem; weight GUESS */
  --text-lead:    20px;  /* 26px, 400           SOURCE [SD] "Find every account in your TAM" */
  --text-body:    16px;  /* 24px, 400 normal    SOURCE [T:fontSizes #1] */
  --text-sm:      14px;  /* 19.6px, 550, -0.01em  SOURCE [T:fontSizes] */
  --text-xs:      12px;  /* 18px, 500, -0.01em  SOURCE */
  --text-eyebrow: 12px;  /* 1.2, 600, +0.09em (1.08px), UPPERCASE  SOURCE [T:small[0]] */
  --text-eyebrow-sm: 10px; /* 12px, 600, +0.08em, UPPERCASE, colour #79756D  SOURCE nav labels */
  --tracking-display: -0.04em; --tracking-heading: -0.03em; --tracking-tight: -0.01em; --tracking-eyebrow: 0.09em;

  /* ---- Radii ---- */
  --radius-xs: 8px;     /* SOURCE [T:radii] (24 uses) */
  --radius-sm: 12px;    /* buttons, tabs, small media  SOURCE [T:buttons], [SD] */
  --radius-md: 18px;    /* logo and quote cards  SOURCE [T:cards] (129 uses, the most common) */
  --radius-input: 20px; /* large textarea  SOURCE [SD home-action_input] */
  --radius-lg: 24px;    /* content cards, nav bottom corners, images  SOURCE [SD update_card] */
  --radius-xl: 32px;    /* tab showcase backgrounds  SOURCE [SD] */
  --radius-2xl: 48px;   /* big section panels  SOURCE [SD home-feature_item, home-reps_content] */
  --radius-pill: 9999px;/* SOURCE [SD home-feature_pill 1600px], :root rounded 999rem */

  /* ---- Shadows (Clay uses very few) ---- */
  --ring-hairline: inset 0 0 0 1px rgba(209,205,199,.6);                              /* SOURCE */
  --ring-tint:     inset 0 0 0 2px var(--oat-200);                                     /* SOURCE: 2px inset ring in the card's own tint */
  --shadow-sm:     0 2px 6px 2px rgba(0,0,0,.10);                                       /* SOURCE [T:shadows] */
  --shadow-md:     0 12px 24px -12px rgba(0,0,0,.12), inset 0 0 0 1px rgba(209,205,199,.6); /* SOURCE [SD home-action_scale] */
  --shadow-lg:     0 12px 36px -8px rgba(21,21,24,.10);                                 /* SOURCE [T:shadows] */
  --glow-white:    0 24px 64px 24px #FFFFFF;                                           /* SOURCE [SD home-reps_content] */

  /* ---- Spacing (4px base) ---- SOURCE [T:rootVars --spacing--*] */
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-5: 20px; --space-6: 24px;
  --space-8: 32px; --space-10: 40px; --space-12: 48px; --space-16: 64px; --space-20: 80px; --space-24: 96px; --space-32: 128px;
  --gap-sm: 8px; --gap-md: 24px; --gap-main: 40px;  /* SOURCE [T:rootVars grid--gap-*] */

  /* ---- Layout ---- */
  --container: 1280px;  /* SOURCE [T:containers container-regular] */
  --page-inset: 36px;   /* nav sits 36px in from the viewport edge  SOURCE [T:nav] */
  --nav-h: 59px;        /* SOURCE [T:nav nav__layout] */

  /* ---- Motion ---- SOURCE [T:transitions] */
  --ease-out-quart: cubic-bezier(0.165, 0.84, 0.44, 1);   /* transform, box-shadow */
  --ease-out-circ:  cubic-bezier(0.075, 0.82, 0.165, 1);  /* background-colour */
  --ease-out-expo:  cubic-bezier(0.19, 1, 0.22, 1);
  --dur-micro: 100ms;  /* colour 0.1s ease-out (48 uses) */
  --dur-fast: 150ms;
  --dur-base: 300ms;   /* button bg */
  --dur-slow: 400ms;   /* transform */
}
```

**Colour-role pairs for tinted surfaces** (SOURCE `[SD home-feature_item]`, `[T:headings.h3]`). Each tinted card uses the 100 tint as its background, the 500 ink for text, and the 400 shade for its button:
- blueberry: `#F0F8FF` / `#001433` / `#395AFA`
- tangerine: `#FFF3ED` / `#381005` / `#B53D0A`
- lime: `#FCFEE2` / `#102B03` / `#7F7F00`
- dragonfruit: `#FFF0FA` / `#46022F` / `#CC089E`
- ube card: `#F5F3FF` background (SOURCE `[SD update_card]`)
- pomegranate-light card: `#FCBABE` background

---

## (b) Component recipes

**Top nav bar.** Evidence: `[T:nav]`, `[T:navLinks]`, `[T:hover]`, `[FOLD]`.
- The bar floats: white background, 59px tall, inset 36px from each side.
- Top corners are square; bottom corners are rounded 24px. It sits under a thin dark announcement strip.
- Padding is 7.5px top, 8.5px bottom, 24px sides. The logo is on the left, followed by the links.
- Links are 16px (14px/500 in the inner `p`), black, with 10px×8px padding. On hover the text colour changes to `#7B7974`. There is no underline and no background change.
- The right cluster holds a ⌘K search hint, a "Log in" text link (12.8px/500), a secondary button and a primary button, all at the small button size.
- The only transition is border-colour and background-colour at 0.25s ease-out.
- For the dashboard: use the same white floating bar with rounded bottom corners, or a full-width bar with a 1px `--divider` bottom border. Treat the second option as GUESS.

**Buttons.** Evidence: `[T:buttons]`, `[T:hover]`, `[SD]`.
- Base style for every button:
  - 12px radius (not a pill). Font weight 500, tracking -0.01em.
  - 1px transparent border. No shadow.
  - Hover changes the background colour only: 0.3s `--ease-out-circ`. There is no lift and no scale.
- Sizes:
  - Small: 13.9px text, 8px×16px padding, 39px tall.
  - Large: 18px text, 9px×18px padding, 47px tall.
  - Card button: 16px text, 8px×16px padding, 42px tall.
- Variants:
  - **Primary:** black background, white text. Hover background `#282C35`.
  - **Secondary:** background `#F3F2ED`, black text. Hover background `#DAD4C8`.
  - **Accent (use sparingly):** lime `#CBD810` background, black text.
  - **On-colour card:** the card's 400 shade as background with white text, or a white background with ink text.
  - **Ghost:** transparent background, black text. Hover sets the text to `#7B7974`, matching the nav links. GUESS: add a `--oat-200` background on hover for icon buttons.
- CTAs often have a trailing → arrow (`[FOLD]`).

**Cards.** Evidence: `[SD update_card]`, `[T:cards logo-card]`.
- Default card:
  - Background `--oat-200` (`#F4F3F0`) or a fruit 100 tint.
  - 24px radius, no border, no shadow.
  - Images inside are clipped to 24px (or 12px when small).
- Small or quote card: background `#FEFDFB`, 18px radius, 16px×20px padding.
- Large feature panel: 48px radius, 48px padding (`home-reps_content`).
- On a white page, the change of fill is what separates a card. Do not add a drop shadow.
- If the card sits on an oat panel, set it on white and add `--ring-hairline` (GUESS for the dashboard).

**Tables and lists.** No table exists on clay.com, so everything here is GUESS derived from the tokens.
- Header row: 12px/600 uppercase eyebrow style with +0.08em tracking, colour `#7B7974`, on an `--oat-100` background.
- Rows: 14–16px text, 48–52px tall, separated by 1px `--divider` (`#F4F3F0`) or `#E0DEDC` lines. No vertical rules.
- Row hover: background `--oat-100`.
- Put the table inside a white 24px-radius container with `--ring-hairline`. Numbers use tabular figures.

**Badges and pills.**
- Pill: `--radius-pill`, 32px tall, 0×12px padding (SOURCE `[SD home-feature_pill]`).
- Active pill: the solid 400 shade with a 1px ring in the card tint.
- Eyebrow tag (SOURCE `[T:small]`): uppercase, 12px/600, +1.08px tracking, in the section's ink or muted `#79756D`.
- Status badges (GUESS): 100 tint background with 500 ink text and pill radius. Success uses the matcha tints, warning lemon, danger pomegranate, info blueberry.

**Inputs and selects.** Evidence: `[SD home-action_input]`, `home-action_scale`.
- The large composer is a white field with 20px radius and no border. Padding is 11px top, 16px sides, 16px bottom. Text is 16px/1.3, tracking -0.01em, colour `#1B1A18`.
- It sits in a wrapper with a 23px radius and `--shadow-md` (hairline ring plus a soft drop). An oat-200 footer strip under the field holds the chip buttons.
- Standard input: `:root --input--border-radius` is 0.5rem (SOURCE `[T:rootVars]`). In practice, use 12px to match the buttons.
- Standard input sizing (GUESS): 40–44px tall, 1px `#E0DEDC` border or `--ring-hairline`.
- Focus state (GUESS): 2px `--blueberry-400` ring.
- Selects look like the input and get a Phosphor `CaretDown` icon.

**Tabs.** Evidence: `[SD tab-btn]`.
- A horizontal row of chips 44px tall with 12px radius, 10px×16px padding, 16px/500 text, colour `#1B1A18`.
- Inactive chips have an `#F4F3F0` background.
- The active chip fills with a fruit 200 colour, for example `#AAEBFD` slushie-200.
- Each tab carries its own colour identity (`cc-lime`, `cc-ube` and so on).
- There is no underline indicator.

**Empty states.** GUESS, based on the brand.
- Centred in an oat-200 panel with 24px radius.
- Content: a small illustration or a Phosphor icon inside a fruit-tint circle, a 24px/500 heading at -0.01em, one line of body text at 16px in `--fg-secondary`, then one primary button.
- The copy is friendly and direct.

**Modals.** GUESS.
- Panel: white, 24px radius, `--shadow-lg` plus `--ring-hairline`, 32px padding, max width 560px.
- Overlay: `rgba(27,26,24,.4)`, based on the gradients measured on the site.
- Title: 24px/500.
- Footer buttons are right-aligned, secondary then primary.
- Enter animation: 0.3s `--ease-out-quart` scale from 0.98 plus a fade.
- The cookie dialog in `[FOLD]` supports this look: white, rounded about 20px, soft shadow.

**Toasts.** GUESS.
- Dark `#1B1A18` background with white 14px/500 text, 12px radius, `--shadow-lg`, 12px×16px padding.
- A semantic dot or Phosphor icon on the left.
- Slides in from the bottom-right over 0.4s `--ease-out-quart`.

---

## (c) Layout rules

- **Content width:** 1280px maximum (SOURCE). The page inset is 36px on the nav and 80px on content at 1440px.
- **Gutters and gaps:** the main grid gap is 40px, the medium gap 24px and the small gap 8px (SOURCE).
- **Two-column splits:** 592px + 592px with a 96px gap (SOURCE `[SD home-feature_img]` and the h3 widths).
- **Section rhythm:**
  - Section padding is usually 64px top and bottom, ranging from 48px to 96px (SOURCE `[T:sections]`).
  - The `:root` section-padding tokens are 3, 4, 6 and 8rem.
  - Sections alternate between a white background and large oat or tint panels with 48px radius.
- **Headings:** section heads are centred with a maximum width of about 640px and use `text-wrap: balance` (SOURCE: class `u-text-balance`).
- **Dashboard translation (GUESS):**
  - Content max-width 1280px, page padding 24–36px.
  - Card grid gap 24px. Stacked page sections 48px apart.
  - Page title 44–48px/500 at -0.04em. Card titles 20–24px/500.

---

## (d) What makes Clay feel like Clay

1. **One grotesk for everything.** Roobert is used at every level. Hierarchy comes only from size and very tight negative tracking (down to -0.04em). Display weight is 500–575 and never bold. (SOURCE)
2. **Warm "oat" neutrals.** Greys lean yellow-brown: `#F4F3F0`, `#7B7974`, `#1B1A18`. Surfaces are white or `#FEFDFB` rather than a heavy cream. (SOURCE)
3. **Fruit-coded colour.** Each product area owns a named hue: blueberry, lime, ube, tangerine, pomegranate, dragonfruit, slushie. The 100 tint is used for the surface and a near-black version of the same hue for the text. (SOURCE)
4. **Big soft radii that step up with size.** Buttons 12px, small cards 18px, cards 24px, panels 32–48px. Pills appear only for tags. (SOURCE)
5. **Almost no shadows.** Depth comes from fill changes and inset hairline rings. The one real drop shadow is soft and has a negative spread. (SOURCE)
6. **Black primary CTA with an oat secondary.** Hover darkens the background only. The lime accent is saved for the hero. (SOURCE)
7. **Uppercase tracked eyebrows** at 12px/600/+0.09em, placed above headings. (SOURCE)
8. **Calm motion.** Colour changes take about 100ms and background changes 300ms with easeOutCirc. Transforms use easeOutQuart. There is no bounce. (SOURCE)

---

## (e) Do not

- Do not use Inter or Plain Black as the main face just because `[REF]` names them. The live site renders Roobert. If Roobert cannot be licensed, pick a geometric grotesk such as Inter Tight, Geist or Manrope and keep the tight tracking.
- Do not use bold (700) headings or positive tracking on headings.
- Do not use pill-shaped buttons. Buttons are 12px rounded rectangles.
- Do not use cool blue-greys (Tailwind `slate`/`gray`) or pure `#F5F5F5` greys. Use the oat scale.
- Do not use heavy or coloured drop shadows, glassmorphism, or neon or purple-to-blue gradient hero backgrounds. The only gradients on the site are white or oat fades that mask edges.
- Do not use a dark-mode-first dark navy UI. Clay is light, and even the footer is `#FFFDF9`.
- Do not use more than one saturated accent per view. A tint belongs to a feature area; do not paint every card a different fruit.
- Do not use hover lifts (`translateY`) or scale on buttons, and do not use borders heavier than 1px.
- Do not mix icon sets. Use Phosphor (regular).

---

## Gaps

- `designmd.co/d/clay` blocked both automated fetch and headless Chrome (HTTP 403/429), so none of its tokens are captured.
- The site has no tables, modals, toasts, form selects or empty states. Those recipes above are GUESS values built from the measured tokens.
- Roobert is a commercial font. Using it needs a licence; otherwise use the fallback stack.
- Dark-theme tokens were not captured because Clay has no dark theme.
