# Application backend observations

All 57 backend buckets in the resolved Saved inventory have a versioned record published to Supabase. This inventory contains 872 classified Saved URLs. Updated 2026-09-24T00:18:27.926675+00:00.

Coverage: 22 observed, 20 partial, 14 blocked, 1 manual/email. **A catalog record does not guarantee every employer variation or later step is operable.**

Observed means representative rendered form patterns were captured. Partial records contain visible steps plus unobserved options, later steps or mixed variants. Blocked records preserve login, challenge, unavailable-page or tooling gates. Manual records describe email instructions; no email was sent. See [known limits and sample URLs](review-blockers.md).

| Backend | Saved URLs | Pages sampled | Coverage | Map |
|---|---:|---:|---|---|
| greenhouse | 191 | 3 | partial | [JSON](greenhouse.json) |
| ashby | 170 | 3 | observed | [JSON](ashby.json) |
| linkedin_easy_apply | 78 | 3 | partial | [JSON](linkedin_easy_apply.json) |
| lever | 70 | 3 | observed | [JSON](lever.json) |
| workday | 51 | 3 | blocked | [JSON](workday.json) |
| workable | 43 | 5 | observed | [JSON](workable.json) |
| wellfound | 30 | 3 | blocked | [JSON](wellfound.json) |
| rippling | 28 | 3 | observed | [JSON](rippling.json) |
| custom | 21 | 21 | partial | [JSON](custom.json) |
| jazzhr | 19 | 3 | observed | [JSON](jazzhr.json) |
| paylocity | 15 | 3 | partial | [JSON](paylocity.json) |
| bamboohr | 14 | 4 | observed | [JSON](bamboohr.json) |
| breezy | 12 | 3 | observed | [JSON](breezy.json) |
| smartrecruiters | 11 | 3 | partial | [JSON](smartrecruiters.json) |
| indeed_easy_apply | 10 | 3 | blocked | [JSON](indeed_easy_apply.json) |
| icims | 8 | 3 | blocked | [JSON](icims.json) |
| dayforce | 7 | 4 | partial | [JSON](dayforce.json) |
| gem | 6 | 3 | observed | [JSON](gem.json) |
| adp | 5 | 2 | blocked | [JSON](adp.json) |
| builtin_easy_apply | 5 | 5 | partial | [JSON](builtin_easy_apply.json) |
| email | 5 | 5 | manual | [JSON](email.json) |
| jobvite | 5 | 5 | partial | [JSON](jobvite.json) |
| oracle | 5 | 3 | blocked | [JSON](oracle.json) |
| teamtailor | 5 | 3 | observed | [JSON](teamtailor.json) |
| comeet | 4 | 4 | observed | [JSON](comeet.json) |
| pinpoint | 4 | 3 | observed | [JSON](pinpoint.json) |
| successfactors | 4 | 4 | partial | [JSON](successfactors.json) |
| ukg | 4 | 4 | blocked | [JSON](ukg.json) |
| dover | 3 | 3 | observed | [JSON](dover.json) |
| gusto | 3 | 3 | observed | [JSON](gusto.json) |
| paycor | 3 | 3 | partial | [JSON](paycor.json) |
| workatastartup | 3 | 3 | blocked | [JSON](workatastartup.json) |
| clearcompany | 2 | 1 | partial | [JSON](clearcompany.json) |
| frecruit | 2 | 2 | partial | [JSON](frecruit.json) |
| hibob | 2 | 2 | observed | [JSON](hibob.json) |
| jobscore | 2 | 2 | observed | [JSON](jobscore.json) |
| kula | 2 | 2 | observed | [JSON](kula.json) |
| asana | 1 | 1 | partial | [JSON](asana.json) |
| attrax | 1 | 1 | partial | [JSON](attrax.json) |
| brassring | 1 | 1 | blocked | [JSON](brassring.json) |
| brightmove | 1 | 1 | blocked | [JSON](brightmove.json) |
| careerpuck | 1 | 1 | observed | [JSON](careerpuck.json) |
| digitalhire | 1 | 1 | blocked | [JSON](digitalhire.json) |
| elmo | 1 | 1 | blocked | [JSON](elmo.json) |
| freshteam | 1 | 1 | observed | [JSON](freshteam.json) |
| getonbrd | 1 | 1 | partial | [JSON](getonbrd.json) |
| google_forms | 1 | 1 | partial | [JSON](google_forms.json) |
| haleymarketing | 1 | 1 | observed | [JSON](haleymarketing.json) |
| isolved | 1 | 1 | partial | [JSON](isolved.json) |
| jobinfo | 1 | 1 | partial | [JSON](jobinfo.json) |
| loxo | 1 | 1 | observed | [JSON](loxo.json) |
| pcrecruiter | 1 | 1 | observed | [JSON](pcrecruiter.json) |
| recruitee | 1 | 1 | partial | [JSON](recruitee.json) |
| taleo | 1 | 1 | partial | [JSON](taleo.json) |
| trinethire | 1 | 1 | observed | [JSON](trinethire.json) |
| ycombinator | 1 | 1 | blocked | [JSON](ycombinator.json) |
| zohorecruit | 1 | 1 | blocked | [JSON](zohorecruit.json) |

Maps are untrusted context for typed routing. They cannot add fields, choose selectors, navigate, or authorize answers. Fresh DOM observations, source provenance and preparation-only guards remain authoritative.

Raw label/option/control evidence and independent evaluation releases are private under `.imx/dynamic-applications/`. Logged-in values are excluded or redacted. The catalog stores public application metadata, never candidate answers.

Publish and refresh the runtime cache:

```sh
.imx/application-urls/venv/bin/python scripts/application_schema_registry.py publish
.imx/application-urls/venv/bin/python scripts/application_schema_registry.py export --inventory .imx/dynamic-applications/all-backend-inventory.json
export IMX_SCHEMA_CATALOG_DIR="$PWD/.imx/dynamic-applications/schema-cache"
```

Migration: [`20260923234500_application_schema_maps.sql`](../../supabase/migrations/20260923234500_application_schema_maps.sql). Browser routing uses the local cache instead of per-field database requests. Exact canonical URL indexing omits conflicting classifications.

See [runtime setup](../dynamic-runtime.md), [Jev/writer routing](../dynamic-application-routing.md), and [preparation-only execution](../application-preparation.md).
