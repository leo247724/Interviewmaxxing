# Current integration brief — 2026-09-22

User preference update: Austin onsite/hybrid is strongly preferred over US-wide remote. Remote remains eligible and the annual USD100000 minimum remains. Root adds canonical location_priority to JobSearchQuery/SelectionPreferences, enum STRONGLY_PREFER_ONSITE_HYBRID(default), BALANCED, PREFER_REMOTE. Preserve through frontend/service storage, Jev criteria/cache and result ordering. This is a strong preference, not an exclusion.

D0R2 (coordinator): identity merge now retains newly observed employer_job_key and evidence on a repeated source observation; next_action_due supports date | aware timestamp | None. Pipeline imported_values includes original Stage and Status. Core I1 owns apps/cli; coordinator changed only discovery/__init__/discovery tests/CONTRACTS, no CLI changes.

## Active review corrections

- C2P2 candidate worker: unreadable historical upload file errors must be isolated; a managed uploaded resume with missing metadata must not downgrade to legacy PROFILE fallback. Preserve genuine external profile references.
- C4R2 browser worker: div application cards and nested article/li records still combine a different application's accepted status with target draft identity. Tie status and target identity inside the same established record; ambiguous record boundaries => UNKNOWN. Consent checkbox groups and signature text with certification in help_text must require explicit user provenance, using the complete question before identity/fact semantics. See coordinator reviewer receipt for reproduction.
- P1R pipeline worker: source-scoped import identity and collision rejection; immutable original source values separate from latest merge baseline; reject nonblank unnamed CSV columns; handle negated outcome statuses conservatively; large nonfinite/unrepresentable numeric input becomes a precise RowIssue. Then canonical D0 adapter preserving date-only and raw Stage/Status.
- S1R service: frontend label City and region supplies Austin, TX but service assigns country TX. Preserve explicit city/region and don't invent country from region. User confirmation must remain labeled user-reported even with older site screenshot evidence; use explicit receipt confirmation method/authority (S1 DTO + frontend correction), not all(evidence.source==user).
- I1/S1 integration: application A's selected resume must remain pinned across profile changes for B and process restart. Current runner rereads global candidate profile on each resume. Persist an application-specific selected resume/artifact identity and test A/B/restart; never silently substitute another resume.

## Seams/checkpoints

Root integrated reviewed C1R3, C2R, C3R and F1R2; 437 checks passed before additive D0. Root uv.lock already includes candidate/generation and apps/web is excluded from Python workspace. C4R is still held for C4R2 despite142 browser tests. C2P1 held pending C2P2. P1 da97188 and S1 98aff0a are checkpoints under correction. F2/F3 checkpoint816afa1 publishes typed pipeline/jobs presentation contracts in apps/web/lib/{pipeline,jobs}/types.ts and README. S2 consumes actual packages; I1 create_runner factory is being built. O1 OpenCLI driver remains browser-owned after corrections.

Actual resume PDF and private8-row normalized Numbers export have been verified; coordinator owns final private imports after package review. No actual employer submission or outreach during build/tests. All offline fixtures fictional; actual source browsing and small explicit Jev tests authorized.

## Latest semantic role clarification

User is a performance marketing operator. Search examples, in preferred order: Paid Media Manager; Senior Paid Media Manager; Performance Marketing Manager; Growth Marketing Manager; Demand Generation Manager; Digital Marketing Manager. Existing marketing manager/director remain broader seeds. Canonical query/preferences now carry role_focus. Jev must evaluate responsibilities and ownership semantically, allowing adjacent paid acquisition/growth lead/director titles. Never a literal-title-only filter. Pure Data Platform Engineer is outside focus; the exact Senior Data Platform Engineer the user saw was located in fictional scripts/mock_ats.py and its tests, not live selected job evidence. Preserve equivalent titles and avoid blanket keyword exclusions on descriptions. Expose/edit roleFocus in UI and persist/pass it to Jev; include in cache/rubric.

Candidate C2P2 9eb9f72 approved and merged. Actual private profile now exists at default IMX_HOME with supplied resume,35 literal source-backed USER_STATED claims,6 roles and1 education, no saved screening/consent answers. Coordinator verified copied PDF digest and setup.complete. Do not access/use this private profile for tests.
