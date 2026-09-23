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

## Current authorization and model ownership update

User explicitly prohibits real application submissions while testing; final candidate JSON is pending. The private resume-derived profile is provisional for live use. All application tests use fictional localhost ATS and temporary profiles. S3 adds TEST_ONLY application mode and frontend must show it. Source browsing and bounded authorized Jev checks are still allowed; no outreach.

Frontend ownership moved to native /root/frontend_astra_max, modelgpt-6-astra effortmax, in existing build/dashboard; Opus7741a68f-82d8-4d3a-89fd-41dab783e3f6 was stopped at verifiedshell. Do not send newOpuswrites there. User requests polished intuitive anti-slop design; frontend-design skill read.

Fable5.1 is the user-corrected model (5.2 was a typo). Actualclaude-fable-5-1 verified. FableMax nowimplements I1R in core-contracts sessionf563693b-534d-4fd7-a0ab-e14f7284b14a, J2R injev-selection session8d49cec1-0ca1-4d5f-99d6-3aa21e74e66c, C4R3 inbrowser-ats session5947e2df-8de9-43a3-ae01-09ac9898b1af. Candidate terminal's FableJ2review720e14d4-2376-4977-99e9-b0d23349f2ec finished.

New isolated performance-runtime worktree/branchbuild/performance-runtime, Supersetworkspace907410ce-6121-4926-9782-c7a543cee411. Fable5.1Max P0 design/benchmark session3ea3d203-b4e1-4907-934f-766045c72ea7, active terminal1e690689-5186-4901-855f-d49ad5546f12; initial empty shelle38684b3-96bf-4d33-bf11-a3cd28c1b4c8 is unused, do notsendtaskthere. Terminalreadmaybeblank; transcriptactualassistantmetadata verifiedFableworking. Native /root/performance_astra_max isread-onlyadvisor. ParentmustrelayAstrafindings toFable fordesigniteration. No changes toactiveMVPpackagesfromP0; docs/performance andbenchmarks/performance only.

P1R2e5c14c7 approved andmerged. Parent115pipeline tests pass. Actual8-rowprivateNumbersimport completed: all23sourcecolumnsroundtrip, same-sourcepreview8unchanged/0duplicate, originalworkbookdigestunchanged. TogetherWorkactualassessmentcompletionwasrecorded ineditabletracking/notes andFollow-uplane, withoriginalsourcevaluespreserved. NoApplicationStoreDB/recordscreated. Receiptprivate .imx/pipeline-import-receipt.private.json.

## New review findings assigned or pending relay

FableI1R: claimheartbeatthroughsanctioneduserwaits (300sTTL vs600swait; 301/599svirtualclockfails), boundedunchangedunsupportedcontrols, stickyvalidationrejectionepochs acrossrestart. FableJ2R: candidate-scopedcache/history/get/links, incompletevsdifferentlocations andregionmatching, explicitremoteeligibilityhold, injectionphrasing/projectionprivacy. BrowserFableC4R3: mixed-tagambiguousrecordfallback falselycombinesotherjobacceptance. O1reviewinprogress: wrongsame-name/sizefileacceptedwithoutSHAproof, session-scopedinitialopenmaynavigateexistingprotectedtabbeforecheck, knownnavigationDriverErrorcontinuesfillingotherfields.

S3 follow-up required aftercurrentcheckpoint: candidate-scopedselectionretrieval/linking usingJ2R API; expose durabledecisiontaskstate/error/resultID toendpolling onFAILED/INTERRUPTED/cachedsameIDcompletion; rankAustinbeforelistinglimit; linkedpipelineapplicationDTO confirmationMethod/Authority. OriginalS1Rthreebugsapproved. UIAstra notified oftaskpolling/linkedreceiptseams.
