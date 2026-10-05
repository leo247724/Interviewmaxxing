---
name: real-chrome-apply
description: Mass-apply to saved or sourced jobs through Leo's real Chrome (OpenCLI bridge) on Ashby, Greenhouse, Lever, Workable and Rippling, with RAG-written answers. Use when Leo says apply to jobs, keep applying, run the application lanes, or asks how many applications went out.
---

# Real-Chrome application run

Everything lives in `.imx/dynamic-applications/real-chrome/` (gitignored). Run from `.imx/dynamic-applications` with
`IMX_HOME=/Users/leo/.interviewmaxxing IMX_CANDIDATE_ID=default` and the repo's `.venv/bin/python`.

## Standing rules (Leo's, do not relax)
- One role per company per rolling 7 days (`cap.py` `WINDOW_DAYS`, `scripts/real_chrome_policy.py` cooldown). Never apply twice to a company in a week.
- Never open linkedin.com, indeed.com, glassdoor.com, ziprecruiter.com, builtin.com. Filter job lists by host first.
- Never solve a CAPTCHA and never create accounts (Workday is out). Emailed verification codes: Leo authorized reading them from his Gmail (2026-09-29); `real-chrome/gh_mail.py` does it inside the Greenhouse lane and reads nothing else.
- Never answer a question falsely: budgets, client counts, headcounts, current salary and "are you applying via a script" stay unanswered.
- His facts: US citizen, no sponsorship, Austin TX, 7 years paid media, 6 SEO, 8 performance/digital marketing, Bachelor's (Loyola Marymount).

## Fast path (screen first; adopted after the A/B run of 2026-09-29)
Opening every form in the browser sent 1% of attempts. Screening the form offline first sent 53%.
1. Chrome must have a window open or the bridge is asleep: `open -a "Google Chrome"`.
2. Set the essay allowance: write `{"left_usd": <amount>}` to `real-chrome/essay-budget.json`. The screens draw it down.
3. `nohup python -u real-chrome/supervisor2.py >> real-chrome/sourced-run/supervisor2.log &` does the rest in a loop:
   `sourced_prep.py`, `move_cards.py`, `make_cards.py`, then per board `{gh,lever,ashby}_prescreen.py` and a lane on `<run>/jobs-ready.json`.
   Lanes run with `WF_BULK=1 WF_ARM=screened WF_CACHE_ONLY=1` (they never call the writer). Two failed screened attempts park a form.
4. New supply: `rehome_probe.py` (saved jobs on aggregator or career-site links, matched on public boards) then `rehome_merge.py`;
   a sourcing agent writes `real-chrome/sourced-2026-*.json` (exclusions in `sourcing-exclude.json`).
5. Count: scan `real-chrome/*-run/ledger.jsonl` for `result == submitted`; report `submitted_unconfirmed` separately.
6. Compare arms with `python real-chrome/ab_metrics.py <since>`. Any process change gets its own `WF_ARM` and about 10 attempts.

## Stopping things
- Never `pkill` or `killall`. Read the pid with `pgrep -f`, check its command line, then `kill <pid>`.
- Stop a lane between jobs: `real-chrome/stop_lane.sh <pid> <session> <lane log>`.
- A lane killed mid-form leaves a `pending` row in `~/.interviewmaxxing/state/company-cooldown.sqlite3` that blocks the company.
  Release it only when the ledger shows the form never reached the submit click; log it in `released-reservations.jsonl`.
- `rejected_spam` is final for that company for the week. Do not alter input behaviour to get past an employer's spam filter.
- Before releasing, read the dead lane's tab (`rc.Chrome("<WF_SESSION>").js(...)`: submit button still present and no "thank you for applying" text = unsent), then close that tab.
- While `supervisor2.py` runs, never start your own gh/lever/ashby lane on `jobs-ready.json`: the supervisor starts lanes on the same list and two lanes then open the same forms (2026-09-30 01:15). Run a board yourself only after killing the supervisor by pid, or for boards it does not manage (workable, rippling, paylocity, smartrecruiters).

## When the pools look empty
Check driver gaps before concluding that supply is gone. On 2026-09-29 these added 12 submissions after every screen said "nothing ready":
- List forms whose only failure is `No verified submit button`, `Cover letter`, `Selection changed` or a block field (`Company name`), and probe one in a tab named `imx-lead-probe*` with the driver's own `READ` script.
- Greenhouse employment and education blocks are answered by field id from `~/.interviewmaxxing/profile/default/profile.json`, never by label.
- A board that answers "too many requests" gets `real-chrome/slow_lane.sh` later, never a faster retry.
- Letters are expensive and often held; draft them only inside a screen's budget.
- `real-chrome/questions-for-leo.md` lists the facts only Leo can give; most held forms wait on those.

## When a lane holds
Read the ledger's `unanswered` / `errors`, add a rule in `ashby.py` (`standing_override`, `OPTS`, `RULES`, `TEXT_DEFAULTS`),
restart the lane after its current job. Labels may carry "✱" or "*"; rules match the cleaned label.

## Widget lessons
See memory notes `ashby-real-chrome-run-2026-09-25` and `multi-ats-run-2026-09-29` for the DOM behaviour of each ATS.
