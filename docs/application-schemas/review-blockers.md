# Application pages needing further access or verification

These are observed limitations from the blank-form mapping pass. A visible CAPTCHA widget may require action only later; its presence is not a completed challenge. Candidate fields remained blank, so gated later steps are unobserved. No account was created, resume uploaded, personal answer entered or application submitted.

Each map preserves all sample URLs and evidence paths. Samples below are starting points; the linked map distinguishes unavailable postings from backend patterns.

## greenhouse — partial

2 of 3 samples are native Greenhouse job-boards forms. The third (?gh_jid= on an employer domain) rendered a NON-Greenhouse third-party careers-site form; see edge_cases.

reCAPTCHA Enterprise anchor iframe (recaptcha.net, invisible/badge style) present on both native samples. Expect a challenge or token check at submit; not triggered. No login required to view the blank form. Employer custom yes/no screening questions (work authorization, sponsorship, country/state residency) are required and may be disqualifying.

[Complete map](greenhouse.json) · [Sample 1](https://job-boards.greenhouse.io/firmpilotailawfirmmarketing/jobs/5425748008) · [Sample 2](https://job-boards.greenhouse.io/vercel/jobs/6011904004)

## ashby — observed

reCAPTCHA v2 anchor iframe (recaptcha.net/api2) on every /application page; expect token check at submit; not triggered none; blank form viewable without login

[Complete map](ashby.json) · [Sample 1](https://jobs.ashbyhq.com/canals/71fef8ca-7c37-41d7-ae2b-80fa0930c6a2/application) · [Sample 2](https://jobs.ashbyhq.com/tempo-io/a3a1dd8d-4a05-4dfc-bcd4-4879dc4aaae4/application)

## linkedin_easy_apply — partial

LinkedIn login required for Easy Apply; session was already authenticated, no challenge shown Page 1 is prefilled with account email/phone/name/location. Runtime must never export or send these values to models; observation should emit labels and has_value flags only. Pressing Next persists an application draft on LinkedIn; abandoning triggers a Save/Discard prompt none seen on page 1

[Complete map](linkedin_easy_apply.json) · [Sample 1](https://www.linkedin.com/jobs/view/4467558620/) · [Sample 2](https://www.linkedin.com/jobs/view/4462675702/)

## lever — observed

hCaptcha iframes (newassets.hcaptcha.com) on every /apply page; not triggered none

[Complete map](lever.json) · [Sample 1](https://jobs.lever.co/airslate/9a4add3b-1bbc-453d-94da-502113da3c53/apply) · [Sample 2](https://jobs.lever.co/rover/f05fbca8-fea6-45d4-948d-4e16b14eac9e/apply)

## workday — blocked

Create Account (email/password/consent) or Sign In required before any application step anti-bot honeypot field present; automated fills must skip it CrowdStrike tenant shows a cookie-consent banner on the intro

[Complete map](workday.json) · [Sample 1](https://tinuiti.wd12.myworkdayjobs.com/Tinuiti/job/USA---Remote/Director--Paid-Social_R26_649/apply) · [Sample 2](https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers/job/USA---Remote/Sr-Strategics-Field-Marketing-Manager--Remote-_R29441/apply)

## workable — observed

none on load none cookie consent dialog present; may intercept clicks Flosum posting closed (not_found redirect); do not count in coverage jobs.workable.com/view/... pages need resolution to apply.workable.com; 'Apply now' click did not navigate in a background tab

[Complete map](workable.json) · [Sample 1](https://apply.workable.com/atak-interactive/j/4D2899C4B9/apply/) · [Sample 2](https://apply.workable.com/arcsite/j/AD29CC0E77/apply/)

## wellfound — blocked

3 different employers inspected. Profile jgd7jms9 is logged out of Wellfound; the job page renders fully (description, company, similar jobs) but 'Apply Now' produced no observable response and no navigation to a form or login screen. No application form was observable; not a mapped form. The remaining 27 listings share the same host/page template and were not opened (no new information expected while logged out).

Logged out; 'Apply Now' inert. Not bypassed; no account created.

[Complete map](wellfound.json) · [Sample 1](https://wellfound.com/jobs/3997434-demand-generation-manager) · [Sample 2](https://wellfound.com/jobs/4522318-senior-manager-ecommerce-retention)

## rippling — observed

All three samples are native ats.rippling.com single-page apply forms (step=application). Custom questions differ per employer; core identity block and EEO block are identical across all three.

Custom-question div comboboxes did not expand on click (aria-expanded stayed false); focus + ArrowDown expanded them. Escape did NOT close keyboard-opened lists (they remained in DOM), so option reads must target ul#field-<n>-list specifically. Phone country code and 'What state do you live in?' lists render no options until a query is typed. Not typed (read-only policy).

[Complete map](rippling.json) · [Sample 1](https://ats.rippling.com/kalkomey/jobs/0c5ec66e-5502-444f-a212-640df2cc2411) · [Sample 2](https://ats.rippling.com/incredible-health/jobs/c64ef23a-e11d-4e10-8495-fdcfdf706bb0)

## custom — partial

Every distinct origin was opened read-only. 'custom' is not one backend; the per-sample form_shape is the schema. Counts: observed 11 (cu-02,03,04,05,08,09,10,14,17,20 + cu-15/19 job pages are description-only), partial 5 (cu-01,06,18,21 multi-step gated; cu-08 options), blocked_auth 5 (Aquent, 4x Amazon), closed 2 (3rd + Lamar). Closed URLs excluded from mapped coverage.

Aquent talent account; Amazon.jobs passport account (4 listings). reCAPTCHA v2 on Corporate Tools (3 listings); expect challenge at submit. 3rd + Lamar career pages 404. MaleMD (10 questions), Rocket Alumni (interview), Digital Neighbor (step 2), Remotivate (later steps) require transmitting step-1 data first. OpenCLI click did not open custom comboboxes (Corporate Tools A x5, Hercules x2) and did not fire MaleMD's start button (in-page click did).

[Complete map](custom.json) · [Sample 1](https://screening.malemd.com/) · [Sample 2](https://www.corporatetools.com/jobs/apply-now?j=Marketing-Manager)

## jazzhr — observed

Two samples are the modern applytojob.com/apply/<key>/<slug> single-page form (fully mapped, blank, visible). Third is the legacy /apply/jobs/details/<key> 'Job Listings' variant where the identical form_submit_new_resume form is present in the DOM but hidden; the 'Apply Now' anchor click did not reveal it within 3s, so its controls were read from the hidden DOM (labels/options only).

reCAPTCHA v2 (same sitekey 6LeCbWYs... on all three employers) must be solved by a human before submit. jz-03 form not revealed by one Apply Now click; needs human/user check or second attempt with scroll.

[Complete map](jazzhr.json) · [Sample 1](https://smadexslu.applytojob.com/apply/nLmSQMBn5H/Ad-Operations-Specialist) · [Sample 2](https://nrtc.applytojob.com/apply/mmEjmeu8pH/Digital-Marketing-Specialist-Remote)

## paylocity — partial

Paylocity is a multi-step wizard (Step 1 of 4/5/6 depending on employer). Only Step 1 (contact/profile) was observed blank; later steps are gated behind required Step-1 fields and 'Next Step' and were NOT visited. react-widgets dropdown option lists could not be opened by click/keyboard/picker through the bridge (aria-expanded stayed false), so their options are unobserved.

react-widgets DropdownList popups never rendered under bridge click / focus+ArrowDown / picker-span click (aria-expanded stayed 'false'). Human or a different event strategy (mousedown/pointerdown) needed. Steps 2..N require completing Step 1 required fields; not attempted (no typing).

[Complete map](paylocity.json) · [Sample 1](https://recruiting.paylocity.com/Recruiting/Jobs/Details/4512227) · [Sample 2](https://recruiting.paylocity.com/Recruiting/Jobs/Details/4464941)

## bamboohr — observed

All four are the current BambooHR careers SPA (<sub>.bamboohr.com/careers/<id>). The application is an in-page card: the 'Apply for This Job' button only flips React state (currentCard=application); bridge coordinate clicks and Enter did NOT trigger it, but the element's own .click() did. /careers/<id>/apply is a 404. Select menus (State/Country/Highest Education) never opened under click, keyboard or synthetic mousedown, so those option lists are unobserved.

Bridge click/Enter did not flip the card; only element.click() worked. Runtime must use a DOM-level click or the user must click. Fabric select buttons did not open via click, ArrowDown, or synthetic mousedown within 1.5s. reCAPTCHA v2 required before submit. Hidden 'Please leave this field blank' input; filling it will likely flag the submission as spam.

[Complete map](bamboohr.json) · [Sample 1](https://321theagency.bamboohr.com/careers/168) · [Sample 2](https://sasso.bamboohr.com/careers/28)

## breezy — observed

Two native <sub>.breezy.hr/p/<id>-<slug>/apply pages and one custom-domain variant (careers.kitbash3d.com) with identical Breezy markup. All single-page blank forms; native <select> options read inline; no captcha widget found.

hp_7f2b must stay empty. bz-03 requires a Loom video link. Work History / Education add-entry fields not observed.

[Complete map](breezy.json) · [Sample 1](https://lovevery.breezy.hr/p/c940df90bf06-senior-director-performance-marketing) · [Sample 2](https://careers.kitbash3d.com/p/4b0a1dd90fda-growth-marketing-manager)

## smartrecruiters — partial

All three resolve to the SmartRecruiters 'Easy apply' oneclick-ui (Angular web components, controls inside nested shadow roots — extractor v5 shadow traversal was required). Only step 1 (Personal information + Resume + message) was observed blank; the 'Next' button leads to screening questions/consents that are gated behind required step-1 fields and were NOT visited.

All controls live in nested shadow roots; querySelector from document sees nothing. Runtime must traverse shadowRoot (extract_form.js v5 qsaDeep) or use opencli state (which already pierces shadow). Screening questions unobserved; require typing to reach. Two input#file-input (autofill vs resume). Bind via ancestor data-test (apply-with-resume-container vs resume-upload).

[Complete map](smartrecruiters.json) · [Sample 1](https://jobs.smartrecruiters.com/AllianceAnimalHealth/744000149701344-integrated-marketing-manager-remote-?oga=true) · [Sample 2](https://jobs.smartrecruiters.com/Wise/744000150172789-senior-business-marketing-lead-north-america)

## indeed_easy_apply — blocked

3 employers attempted (Growth Hacking LLC, Marriott via SimplyHired; LegalEdge via indeed.com). No application form was observable: SimplyHired's Quick Apply requires a SimplyHired/Indeed account (unauthenticated /out link 307s to /api/auth/logout; page shows 'Sign In / Create Account' and 'We are partnering with Indeed to provide you with one account for both platforms'), and the direct indeed.com listing is behind a bot challenge that was not bypassed. Not a mapped form.

SimplyHired Quick Apply requires the shared SimplyHired/Indeed account; profile jgd7jms9 is not logged in. indeed.com/viewjob served Cloudflare 'Additional Verification Required'.

[Complete map](indeed_easy_apply.json) · [Sample 1](https://www.simplyhired.com/job/Lse14zgGqlBaC4FEsqfTYW9wl0FFj4J0WBJFLJAty2PG_kbHdA9YWQ) · [Sample 2](https://www.simplyhired.com/job/MgzX8yyHCS56zHElNAp9EVzegBo_zM7YEftz63eKy3LUpL-Dvjq95Q)

## icims — blocked

iCIMS renders inside a same-origin iframe (in_iframe=1). 'Apply for this job online' (mode=apply) redirects to /jobs/<id>/<slug>/login — an 'Enter Your Information' email-first step with hCaptcha and privacy/GDPR consents. The application form itself is behind that gate (email + captcha + account/guest step) and was NOT observed. Three employers' gate pages were mapped; the gate content varies per employer (privacy checkbox, GDPR select, phone field).

Email-first login/registration required before any application field is shown. hCaptcha on the gate. Content is a same-origin iframe; runtime must read contentDocument (cap_frame.sh) or navigate the frame URL. Direct navigation to ?in_iframe=1 is redirected back to the wrapper.

[Complete map](icims.json) · [Sample 1](https://careers-leftfieldlabs.icims.com/jobs/8465/senior-growth-marketing-manager/job) · [Sample 2](https://careers-americas.icims.com/jobs/26354/paid-ads-marketing-manager%2c-dx/job?mode=apply)

## dayforce — partial

Dayforce Candidate Portal (jobs.dayforcehcm.com). <job>/apply shows 'Already Have an Account? Sign In' vs 'Apply without an Account'. The guest path opens a 3-step 'Manual Application' (Candidate Info -> Questionnaire -> Submit). Step 1 was observed blank on 3 employers; the Questionnaire and Submit steps are gated behind required step-1 fields and reCAPTCHA and were NOT visited. DeVry's /apply redirected back to the job page (no guest option reachable).

Buttons need a pointerdown..click event sequence; plain click() and bridge clicks are ignored; navigation completes 6-10s later. Questionnaire/Submit steps need step-1 completion + reCAPTCHA. DeVry /apply redirected to job page — guest path unavailable.

[Complete map](dayforce.json) · [Sample 1](https://jobs.dayforcehcm.com/en-US/trinetx1/CANDIDATEPORTAL/jobs/1013) · [Sample 2](https://jobs.dayforcehcm.com/en-US/devry/CANDIDATEPORTAL/jobs/10062)

## gem — observed

jobs.gem.com/<company>/<id>: the blank application form is rendered on the job page under 'Ready to apply? Powered by Gem'. All questions are visible statically (radio/checkbox groups inline). Two submit buttons: 'Apply and save' and 'Apply without saving'. hCaptcha present on gm-01 only.

hCaptcha on gm-01. No label/for or aria association; runtime must use preceding text blocks (extract_form.js preceding_text/ancestor_context).

[Complete map](gem.json) · [Sample 1](https://jobs.gem.com/hallow/am9icG9zdDoHqdO5ZyTtLGXm5APKUyxi) · [Sample 2](https://jobs.gem.com/mindbloom/9a0044fa-348d-4f6c-92b9-7ea9fe27b655)

## adp — blocked

guest identity + captcha before any form captcha widget present on the identity step cookie preferences dialog

[Complete map](adp.json) · [Sample 1](https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=9054646f-9aae-42c8-8167-93d86fefbae4&ccId=19000101_000001&jobId=574964&lang=en_US) · [Sample 2](https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=125e53c7-163f-45f8-8299-63a9ec645332&ccId=19000101_000001&jobId=9205238812544_1&lang=en_US)

## builtin_easy_apply — partial

5 of 5 listings observed. Built In did not host an application form for any of them ('Easy Apply' prior was wrong for all five). The runtime profile is logged into Built In (header shows Sign Out / Edit Profile); with that session the ?handler=ApplyRedirect GET 302s straight to the external ATS. Unauthenticated (curl) the same URL 302s to the job page with ?applyRequired=true. Coverage counts as redirect mapping, not form mapping.

Built In session required for the ApplyRedirect to reach the employer ATS; the runtime profile was already logged in. target=_blank apply anchor; an untracked tab was opened by the single click test on bi-03 and could not be closed via the OpenCLI session (not listed). Human should close any stray builtin/rippling tab.

[Complete map](builtin_easy_apply.json) · [Sample 1](https://builtin.com/job/associate-director-paid-media/11141807) · [Sample 2](https://builtin.com/job/demand-generation-manager/10877873)

## email — manual

5 of 5 listings (4 employers) observed. None has a web application form; each instructs the candidate to email a mailbox. Requirements were read from the rendered page text. No email was composed or sent.

No web form to fill; application happens over email, outside the browser executor.

[Complete map](email.json) · [Sample 1](https://www.brandogdigital.com/senior-digital-marketing-manager-paid-social-aug-2026) · [Sample 2](https://evron.marketing/careers.html)

## jobvite — partial

Data Consent region/language select + I Accept on 3 of 4 tenants reCAPTCHA v2 on forms Next requires all required fields

[Complete map](jobvite.json) · [Sample 1](https://jobs.jobvite.com/legalzoom/job/oRtEAfwi/apply) · [Sample 2](https://jobs.jobvite.com/varonis/job/oZJvAfwx/apply)

## oracle — blocked

email + verification code before any form captcha widget on gate present APPLY NOW click reported success without navigation in a background tab; open /apply/email directly

[Complete map](oracle.json) · [Sample 1](https://fa-euxw-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/241460) · [Sample 2](https://iaaviz.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Jobs-at-Icertis/job/7285)

## teamtailor — observed

none seen none (employee/Connect logins irrelevant) cookie consent banner on all three

[Complete map](teamtailor.json) · [Sample 1](https://careers.thinkjuice.com/jobs/7645370-account-manager-digital-marketing/applications/new) · [Sample 2](https://jobs.carrtalent.com/jobs/697366-director-of-paid-media/applications/new)

## comeet — observed

All 4 employers mapped. Form is an Angular app inside a cross-origin iframe (www.comeet.co/jobs/<company-uid>/<position-uid>/apply?token=…); the bridge exposed 0 cross-origin frame targets, so the iframe src was opened directly in the owned tab. Title of the iframe document is 'Spark Hire Recruit Jobs' (Comeet is now Spark Hire Recruit).

reCAPTCHA v2 anchor present on all 4; not triggered Form is cross-origin (comeet.co) inside comeet.com; bridge frame listing returned 0 targets — open iframe src directly or drive via CDP frame targeting none Required US-residence/authorization, time-zone, salary-range-comfort questions

[Complete map](comeet.json) · [Sample 1](https://www.comeet.com/jobs/outerbox/49.00D/lead-paid-search-strategist/0E.271) · [Sample 2](https://www.comeet.com/jobs/framesecurity/FA.002/director-of-growth-marketing/6A.276)

## pinpoint — observed

Portnox (native pinpointhq.com, /en/ locale), Brivo (custom domain careers.brivo.com), Thorne (native, no locale prefix). Second Portnox listing skipped as same employer template.

reCAPTCHA Enterprise anchor iframes (recaptcha.net) on Brivo custom-domain instance; none on Portnox/Thorne native pages No login required; 'Apply with LinkedIn' is optional OAuth Yes/No work-authorization, sponsorship, criminal-history and referral radios; state-residency select. Radios are not DOM-required, so server-side validation is unknown. Thorne marks Gender/Gender Identity/Ethnicity/Disability/Veteran as required; must route to human ('Prefer Not To Say' exists).

[Complete map](pinpoint.json) · [Sample 1](https://portnox.pinpointhq.com/en/postings/e0dc5ba9-0edf-4802-8b07-6bae0f0dea00/applications/new) · [Sample 2](https://careers.brivo.com/postings/d59ec26e-3235-498a-bdc5-631cb764d831/applications/new)

## successfactors — partial

Bausch + Lomb: full blank combined register+apply page mapped (all picklists opened except 3 dependent/disabled). Hexagon: tenant configured sign-in-first; Create-Account page viewed (no application questions before account). Ottobock: position filled. Farmers: /talentcommunity/apply deep link bounces to careers home without the posting-page referral; no posting URL in inventory.

Variant B tenants require sign-in/account before questions (Hexagon). Variant A creates the account at Apply time (password fields on the blank form). /talentcommunity/apply/<id>/ bounces to careers home without posting referral (Bausch, Farmers) Ottobock filled reCAPTCHA field only inside sign-in modal; none seen on apply form

[Complete map](successfactors.json) · [Sample 1](https://career5.successfactors.eu/sfcareer/jobreqcareer?company=Ottobock&jobId=7223) · [Sample 2](https://careers.bauschlomb.com/job/USA-Remote-Senior-Manager%2C-Retail-Media-and-Commerce-Activation/1372507457/)

## ukg — blocked

All 4 samples (3 UKG Pro Recruiting tenants on recruiting.ultipro.com / rec.pro.ukg.net, 1 UKG Ready tenant on saashr.com) redirect the Apply action to an account login/sign-up gate before any application question is rendered. No account was created. The blank application form was NOT observed; nothing below about the form itself is mapped.

Account login/sign-up required before any application content; 4/4 samples, 2 UKG products 'Apply now' is inside a <ukg-button> shadow root; text/ref selectors fail, JS click on shadow button works

[Complete map](ukg.json) · [Sample 1](https://recruiting.ultipro.com/UNI1053/JobBoard/9b1b7eec-8714-d785-6d27-8ee3d0405521/OpportunityDetail?opportunityId=4eea98bb-2d62-424c-a947-1384c0bfda9f) · [Sample 2](https://mcclatchy.rec.pro.ukg.net/MCC1008MCLTC/JobBoard/ff11d963-22db-4278-a2b7-709f3b882262/OpportunityDetail?opportunityId=8c49b517-0b8b-4645-a75e-b3d932a41f7e)

## dover — observed

All 3 employers mapped. React/MUI form; labels are NOT programmatically linked to inputs (label element is a sibling above; input ids are React ':rN:' and custom question names are UUIDs). Question text must be read from the preceding visible label in DOM order. Re-captured 23:59Z after reviewer request: raw per-control preceding_label/section_heading now stored; the top input[type=file] sits in the 'Autofill from resume' block (question_container) while the bottom one follows the 'Resume (optional)' / 'Resume *' label.

none none seen US work-authorization radios on 2 of 3 no label/for or aria linkage; question text must be derived from DOM order

[Complete map](dover.json) · [Sample 1](https://app.dover.com/apply/well-oiled-operations/53c50d98-b434-4a11-8739-669f47c65903) · [Sample 2](https://app.dover.com/apply/Jivpad/a6582918-d89b-45c2-bd33-2cf7008acd7a)

## gusto — observed

Both employers mapped (Padel39 x2 postings, Keep Aware). Rails form; clean <label for> bindings.

none none seen Resume always required; cover letter required on some employers

[Complete map](gusto.json) · [Sample 1](https://jobs.gusto.com/postings/padel39-director-of-marketing-4eed2f0c-50a4-42e7-8e23-0f83ed374501/applicants/new) · [Sample 2](https://jobs.gusto.com/postings/keep-aware-growth-marketing-manager-114b51c8-a8b5-46cc-b465-d5774dc0e725/applicants/new)

## paycor — partial

Civitas: full blank 'Apply with a Resume' page mapped (step 1 of a multi-step flow). AccessHope: pre-screening questionnaire (8 Yes/No) shown BEFORE the resume page; questionnaire mapped, later steps gated behind 'Continue'. Harvest Group: tenant embeds the form in an iframe on harvestgroup.com and frame-busts direct navigation; bridge listed no cross-origin frame target, so the form was not observed.

Harvest Group hosts the form in an iframe on its own domain with frame-busting; not observable via this bridge Both native variants require posting a step ('Continue'/'Submit') to see the next page; not exercised Sponsorship / I-9 / OPT / remote-readiness Yes/No screening at AccessHope none seen

[Complete map](paycor.json) · [Sample 1](https://recruitingbypaycor.com/career/JobIntroduction.action?clientId=8a7883c67c102844017c138ec8eb000e&id=8a7883ac9cbb2ae3019cd4a1c7412082&lang=en) · [Sample 2](https://recruitingbypaycor.com/career/JobIntroduction.action?clientId=8a7883c68b8cdc91018bb4d838d208e3&id=8a7887aca0abfca001a0c99f54b9075a&source=&lang=en)

## workatastartup — blocked

All 3 assigned listings redirect to the Y Combinator account login gate before any application form renders. No blank form was observable without authentication. Not a mapped form.

Every /application?signup_job_id= URL 302s to account.ycombinator.com with a continue= parameter. Profile jgd7jms9 is not logged into YC. Not bypassed.

[Complete map](workatastartup.json) · [Sample 1](https://www.workatastartup.com/application?signup_job_id=107793) · [Sample 2](https://www.workatastartup.com/application?signup_job_id=100377)

## clearcompany — partial

Singleton coverage. Step 1 ('Enter your information below to begin the application') mapped blank. The 'Continue' button posts first/last/email and creates a candidate record, so later steps (resume, questions, EEO) were NOT observed. SPA renders after ~6s; first capture at 4s was empty.

Later steps require submitting name/email first (creates candidate record) SPA needs >4s to render; inputs have no name/id — bind by placeholder/preceding label

[Complete map](clearcompany.json) · [Sample 1](https://rendevor.clearcompany.com/careers/jobs/4c3f1008-2608-c49f-bc9c-52d5d5a42c18/apply)

## frecruit — partial

Singleton employer (Sage, two vacancies on the same portal). Job page -> 'Apply' -> fRecruit__ApplyRegister 'Get Started' page: registration form (blank, mapped) or 'Log in' by email. Application questions live behind registration and were NOT observed. Second vacancy (VN43379) not opened: same portal/gate.

Registration/login required before application form none on gate

[Complete map](frecruit.json) · [Sample 1](https://sagehr.my.salesforce-sites.com/careers/fRecruit__ApplyJob?vacancyNo=VN43382&source=LinkedIn) · [Sample 2](https://sagehr.my.salesforce-sites.com/careers/fRecruit__ApplyRegister?portal=USA&startURL=%2Fapex%2FfRecruit__Apply%3Fportal%3DUSA%26source%3DLinkedIn%26vacancyNo%3DVN43382)

## hibob — observed

Both employers mapped. Angular/React SPA; multiple <form> wrappers without action; inputs named by JSON-pointer-like paths (/candidate/firstName, /question/<id>, bfe-… for EEO).

none resume input not in DOM until 'Add file' clicked; EEO question titles not label-linked (read preceding text)

[Complete map](hibob.json) · [Sample 1](https://paradium.careers.hibob.com/jobs/9da86884-4462-4124-a9bb-037a8d867b9b/apply?utm_medium=…&utm_source=linkedin (tracking params elided)) · [Sample 2](https://mythicalgames.careers.hibob.com/jobs/653c29fa-35b9-4443-94b7-8c9359815d14/apply)

## jobscore — observed

Both employers mapped. Classic Rails form; no DOM required attributes at all (validation is JS/server side), so requiredness is unknown from markup. reCAPTCHA v2 anchors present.

reCAPTCHA v2 optional only no required/aria-required markers in DOM

[Complete map](jobscore.json) · [Sample 1](https://careers.jobscore.com/careers/obility/jobs/geo-seo-strategy-director-czRp0YNNroMOs3cbJIvLs0) · [Sample 2](https://careers.jobscore.com/careers/unified/jobs/campaign-manager-remote-cJT61mFOPipQRHcJ6xk0mF)

## kula — observed

Both employers mapped. React SPA; the application form is a second <form> on the posting page. Text inputs report a 0x0 bounding box (CSS layout) although the form text is rendered, so a naive visibility filter drops them — the raw controls.json keeps them with visible=false and their info.* ids. Requiredness comes from the trailing '*' in the label and 'This field is required' helper text; only the resume input and radios carry the required attribute. Google reCAPTCHA v2 anchor present; a textarea#g-recaptcha-response exists.

reCAPTCHA v2 Culture Index survey link with Yes/No attestation (Propellic) inputs report zero-size rects; react-select menu did not open via synthetic click

[Complete map](kula.json) · [Sample 1](https://careers.kula.ai/linkby/39585) · [Sample 2](https://careers.kula.ai/propellic/19774)

## asana — partial

none seen none

[Complete map](asana.json) · [Sample 1](https://form.asana.com/?hash=c0684ceecd1ecce21964968f7d97307e8f3b9cb9f1c6c900de19a2f55f99da07&id=1195165197934723)

## attrax — partial

Singleton (Wise, wise.jobs on Attrax). The Attrax job page has no form; its 'Apply' links (/Workflow?workflowId=...&vacancyId=4069) redirect to SmartRecruiters 'Easy apply' (jobs.smartrecruiters.com/oneclick-ui/...). The SmartRecruiters form is built from web components (shadow DOM); a shadow-DOM walker captured the blank fields. Backend actually observed: smartrecruiters (mapped by worker fable_b); fields recorded here as evidence of the redirect destination.

No login required for page 1; later pages unobserved. Form is invisible to light-DOM extractors (0 inputs); shadow-DOM traversal is required.

[Complete map](attrax.json) · [Sample 1](https://wise.jobs/job/regional-marketing-lead-canada-in-austin-jid-4069)

## brassring — blocked

Singleton (Kendra Scott on BrassRing / Infinite Talent TGnewUI). Job details page -> 'Apply to job' -> 'Sign In' page (email + password, or 'Skip sign in') -> 'Privacy Policy' page: 'Note: You must AGREE to proceed.' with Agree/Disagree buttons. Agreeing to an employer policy requires the user's explicit permission, so I stopped there; the application form itself was NOT observed.

Employer privacy policy must be agreed ('Agree') before the form renders. Not clicked. Sign-in gate can be skipped via 'Skip sign in'; account not required at this point.

[Complete map](brassring.json) · [Sample 1](https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=26224&siteid=5247&PageType=JobDetails&jobid=1415916)

## brightmove — blocked

Create Profile and Apply / Login and Apply

[Complete map](brightmove.json) · [Sample 1](https://portal.brightmove.com/jb.do?reqGK=27783838)

## careerpuck — observed

none

[Complete map](careerpuck.json) · [Sample 1](https://app.careerpuck.com/job-board/sky-society/job/5TRbO7zs)

## digitalhire — blocked

apply routes into beta.app.digitalhire.com with a sign-in entry point Apply link target=_blank

[Complete map](digitalhire.json) · [Sample 1](https://jobs.digitalhire.com/job-listing/opening/7L53dmZlJdf7WxnomB97tE)

## elmo — blocked

Sign in or Create an account before applying

[Complete map](elmo.json) · [Sample 1](https://lskd.elmotalent.com.au/careers/lskdcareers/job/view/299)

## freshteam — observed

Singleton coverage (DDMR). Form is embedded at the bottom of the posting page (anchor #applicant-form). Text inputs report 0x0 rects before the 'Apply Now' anchor scroll; raw controls.json keeps them (visible=false in first capture, later capture after reveal). Required = trailing '*' in label text (no required attr). Google reCAPTCHA v2 (anchor + bframe) present.

reCAPTCHA v2 none

[Complete map](freshteam.json) · [Sample 1](https://ddmr.freshteam.com/jobs/JOSVrfDwIrGe/performance-marketing-strategist)

## getonbrd — partial

Singleton. Step 1 of 3 ('Experience') fully observed blank. Steps 2 ('Basic information') and 3 ('Preview') are server-gated behind completing step 1 (min 300/100 chars in rich-text editors) and were NOT visited; a direct GET with ?step=basic was redirected back to step 1. The runtime profile is already logged into Get on Board as the user; the page footer shows profile name/email that will be shared with the employer (redacted in evidence).

Page rendered in a logged-in Get on Board session (profile jgd7jms9). Logged-out behaviour not observed; the profile-share notice implies an account is required. Steps 2-3 require step-1 text; not typed by policy. Logged-in profile name/email are rendered on the page and 'will be shared'; runtime must not export them.

[Complete map](getonbrd.json) · [Sample 1](https://www.getonbrd.com/jobs/digital-marketing-lead-autoraptor-remote/applications/new?raw_referrer=https%3A%2F%2Fwww.linkedin.com%2F&ref=www.linkedin.com)

## google_forms — partial

no Google sign-in required; email collected via field none page 2+ unobserved

[Complete map](google_forms.json) · [Sample 1](https://forms.gle/kDMcQHUztTV7VqvJA)

## haleymarketing — observed

reCAPTCHA v2 present optional Login hidden inputs named action/arg clobber form properties; use getAttribute

[Complete map](haleymarketing.json) · [Sample 1](https://jobs.methodrecruiting.com/index.smpl?arg=jb_apply&POST_ID=14136956&is_related=)

## isolved — partial

first submit creates the applicant record before resume/questions optional Login (account/login.php) for returning applicants none on page 1

[Complete map](isolved.json) · [Sample 1](https://bluewheelmedia.isolvedhire.com/jobs/1651563)

## jobinfo — partial

Singleton (Homeaglow on jobinfo.com). Description page has an 'Apply Now' input[type=button] whose onclick sets window.location to apply.php?jid=<id>. apply.php shows 'Step 1 of 4: Upload your resume' (Uploadcare widget + paste-resume-text form) and 'Just enter your email address to begin:'; steps 2-4 are gated behind an email/resume submission and were NOT observed.

Steps 2-4 require an email or resume submission first.

[Complete map](jobinfo.json) · [Sample 1](https://cid128182.jobinfo.com/public/description.php?jid=9972753)

## loxo — observed

none seen none

[Complete map](loxo.json) · [Sample 1](https://pod1.app.loxo.co/job/MzI2ODctZXBhMnhmdWpva2Yyem0xYw==/form?source_type=app)

## pcrecruiter — observed

form not in top document none seen none

[Complete map](pcrecruiter.json) · [Sample 1](https://www2.pcrecruiter.net/pcrbin/jobboard.aspx?action=detail&recordid=499245027064467&apply=y&uid=BrainWorks.brainworks&src=%7C%7C%7C%7C)

## recruitee — partial

job not found

[Complete map](recruitee.json) · [Sample 1](https://careers.lansweeper.com/o/senior-manager-demand-generation)

## taleo — partial

Singleton (Candela Medical, Taleo Business Edition careers v2). The job page has 0 forms; 'Apply Now' -> applyRequisition renders ONE document (form#TBE_theForm, 143 elements) containing all 6 steps; steps 2-6 are hidden until step 1 'Register' (email + new password) is completed. Registration creates an account, which is prohibited, so nothing was entered; but every step's blank questions/options were captured from the hidden DOM.

Step 1 requires creating a candidate account (email + password) before any other step is shown/validated.

[Complete map](taleo.json) · [Sample 1](https://lde.tbe.taleo.net/lde02/ats/careers/v2/viewRequisition?org=SYNEMEDI&cws=37&rid=3014)

## trinethire — observed

Singleton (LawLytics, TriNet Hire). The job page renders the blank application form inline (form#new_applicant, method post). No login, no captcha, no required attributes; the page shows no asterisks either, so required-ness is unknown until server validation.

No auth, no captcha, no required markers observed.

[Complete map](trinethire.json) · [Sample 1](https://app.trinethire.com/companies/28896/jobs/32970-demand-generation-director)

## ycombinator — blocked

Singleton inventory. The ycombinator.com job page is a read-only description with no form; its only apply control links to the Work at a Startup application behind the YC login gate. Not a mapped form.

Apply link targets the YC account authenticate page; same gate as workatastartup.

[Complete map](ycombinator.json) · [Sample 1](https://www.ycombinator.com/companies/waydev/jobs/ecnmEt7-head-of-growth)

## zohorecruit — blocked

'I'm interested' produced no observable change in a background tab (2 attempts) present; may intercept

[Complete map](zohorecruit.json) · [Sample 1](https://myamazonguy.zohorecruit.com/jobs/Careers/770609000012738857/Head-of-Performance-Marketing)
