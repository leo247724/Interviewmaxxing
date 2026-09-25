# Simple application answers

Edit one small JSON map with your contact details. Each key has a string value or
`null` for an unanswered field. The map imports into the existing candidate profile;
the application runner keeps reading that profile as its source of truth.

The map covers contact questions repeatedly seen in the real application forms:
names, email, phone, LinkedIn, websites and address components. It also accepts
forty explicit reusable answers, stored through the existing saved-answer system,
and one statement key, `career_motivation`, stored as a verified fact (below).
Each starts as `null`; nothing is filled in for you.

| Map key | Example wording on a form |
| --- | --- |
| `first_name` | First name, given name |
| `last_name` | Last name, surname, family name |
| `preferred_name` | Preferred name |
| `email` | Email, email address |
| `phone` | Phone, phone number, mobile number |
| `linkedin_url` | LinkedIn, LinkedIn URL, LinkedIn profile |
| `website_url` | Website, personal website |
| `github_url` | GitHub profile |
| `street_address` | Street address, address line 1 |
| `city` | City, town |
| `state` | State, province, region |
| `postal_code` | ZIP, postal code |
| `country` | Country of residence |
| `gender` | Gender |
| `where_are_you_based` | Where are you based? |
| `requires_visa_sponsorship` | Will you now or in the future require visa sponsorship for employment? |
| `referral_source` | Where did you hear about us? / How did you hear about us? |
| `referred_by_current_employee` | Were you referred to this position by a current employee? |
| `above_age_18` | Are you above the age of 18? |
| `authorized_to_work_us` | Are you currently authorized to work in the US? |
| `school` | School, university |
| `degree` | Degree, degree type |
| `hispanic_latino` | Are you Hispanic/Latino? |
| `veteran_status` | Veteran Status |
| `education_discipline` | Education Discipline, field of study |
| `education_start_date` | Education start date |
| `education_end_date` | Education end date |
| `previously_employed_here` | Have you previously been employed by this company? |
| `previously_interviewed_here` | Have you previously interviewed with this company? |
| `related_to_employee` | Are you related to any current employee of this company? |
| `willing_to_relocate` | Are you willing to relocate? / Are you open to relocation? |
| `open_to_other_positions` | Would you like to be considered for other open positions? |
| `willing_to_provide_references` | Are you willing to provide references? |
| `desired_salary` | What is your desired salary? / What are your salary expectations? |
| `english_proficiency` | What is your level of proficiency in English? |
| `available_time_zones` | Which time zones are you available to work in? |
| `travel_willingness` | How much are you willing to travel for work? |
| `earliest_start_date` | What is your earliest start date? / When can you start? |
| `work_authorization_status` | What is your U.S. work authorization status? (one code below) |
| `race_ethnicity` | Race/Ethnicity / What is your race/ethnicity? |
| `disability_status` | Disability Status / Do you have a disability? |
| `pronouns` | What pronouns do you use? / Pronouns |
| `family_government_official` | Are you or anyone in your immediate family a government official? |
| `non_compete_agreement` | Have you signed any non-competition or non-solicitation agreement …? |
| `uses_ai_tools` | Have you used AI tools to help prepare this application? |
| `familiar_with_company` | Before applying, how familiar were you with this company? |
| `career_motivation` | Not a form question: two or three sentences about what you look for in a role, written once and cited by "What interests you about …?" narratives |
| `county` | County / County of residence |
| `acknowledge_privacy_notice` | I have read and understand the employer's applicant privacy notice and data processing terms. |
| `certify_information_true` | The information I provide in this application is true, complete and accurate. |
| `consent_to_contact` | The employer may contact me about this application. |
| `consent_reference_checks` | The employer may contact the references I provide. |
| `consent_background_check` | I consent to a background check, subject to applicable law. |
| `work_location_preference` | Location Preference / Preferred work arrangement (remote, hybrid or on-site) |

Full name is derived from first and last name. Location is derived from city,
state and country, omitting unanswered components. The selected resume already
exists in the profile and uses the approved document upload path. Change it through
the dashboard's resume selection. A portfolio request needs an actual portfolio;
it does not automatically use a personal website or LinkedIn URL.

Jev identifies the question's meaning and whose information it asks for, then the
resolver copies the exact local value. These wording examples are illustrative,
not substring rules. A supervisor's email cannot use your email. Missing values
stay unanswered. Jev does not invent contact details.

## Editing and importing

From the repository directory, export your current profile to a private file:

```sh
uv run --no-sync python scripts/simple_answers.py export \
  --file ~/.interviewmaxxing/profile/default/simple-answers.json
```

Export refuses to overwrite an existing map. The exported values need your review,
especially if the current profile was originally populated from a resume.
Alternatively, start with [the blank template](../examples/simple-answers.example.json).
Keep personal copies outside Git; `.imx/` is also ignored by this repository.

Edit the values, then check them without changing the active profile:

```sh
uv run --no-sync python scripts/simple_answers.py validate \
  --file ~/.interviewmaxxing/profile/default/simple-answers.json
```

When the values are correct, import them:

```sh
uv run --no-sync python scripts/simple_answers.py import \
  --file ~/.interviewmaxxing/profile/default/simple-answers.json
```

Import records the changed contact details as user-confirmed. This is a complete
contact snapshot: keep all thirteen contact keys, and use `null` to clear an optional
contact value. The forty additional reusable-answer keys and `career_motivation`
may be omitted or `null`;
that adds no new answer and leaves earlier confirmed saved answers intact.
First name, last name and a valid email are required for import. Whitespace-only
strings become `null`; phone and postal codes remain strings to preserve formatting
and leading zeroes. Unknown keys and duplicate JSON keys are rejected. An unchanged
import preserves the original verification time.

Import preserves the selected resume, work history, facts and unrelated saved answers. It
does not open a browser or prepare or submit an application. Export and import
write owner-only files; command output lists keys without printing their values.

Nonblank values for the forty additional keys become explicitly
user-confirmed **GLOBAL** saved answers,
reusable across applications when the complete question matches. Sponsorship must
be `"Yes"`, `"No"`, or `null`; it does not establish work authorization. The employee
referral, age, work-authorization and Hispanic/Latino answers also accept `"Yes"`,
`"No"`, or `null`. So do previous employment, previous interviews, being related to an
employee, relocation, other positions and references. So do government official,
non-compete, AI tools and the five statements. Desired salary, English
proficiency, time zones, travel, earliest start date, race/ethnicity, disability status,
pronouns, familiarity with the company, county and the work-location preference are free
text in your words.

`work_authorization_status` takes exactly one of these codes:
- `us_citizen`: I am a U.S. citizen.
- `us_permanent_resident`: I hold a green card.
- `asylee`: I was granted asylum in the U.S.
- `refugee`: I was admitted to the U.S. as a refugee.
- `daca`: I am a DACA recipient with an Employment Authorization Document (EAD).
- `tps`: I hold Temporary Protected Status with an EAD.
- `pending_adjustment`: my green card application (adjustment of status) is pending and I
  hold an EAD.
- `dependent_ead`: I am a dependent spouse (such as H-4 or L-2) with an EAD.
- `ead_opt`: I am an F-1 student on OPT or STEM OPT with an EAD.
- `h1b`: I hold an H-1B visa.
- `tn`: I hold a TN visa.
- `other_visa`: I am authorized on another visa.
- `not_authorized`: I am not authorized to work in the U.S.

Every work-authorization and sponsorship question is derived from it, whatever its wording
("for any employer", "permanent or temporary", "now or in the future"). The derivation is
described in [dynamic-application-routing.md](dynamic-application-routing.md), "Work
authorization from the stated status". What each code settles:
- **Never needs sponsorship.** A citizen, permanent resident, asylee or refugee is
  authorized for any employer and never needs sponsorship, now or in the future.
- **Will need sponsorship later.** OPT and STEM OPT authorize work now but need an
  employer's sponsorship later, so "now or in the future" is Yes.
- **Needs a new employer's sponsorship.** H-1B and TN holders need it.
- **Future left to you.** DACA, TPS, a pending adjustment and a dependent EAD authorize work
  for any employer for now. Whether they will need sponsorship later is left to you.
- **Sponsorship never derived.** With `other_visa`, a sponsorship question is not derived at
  all; only your own `requires_visa_sponsorship` answer can say it.

A question that asks for the status itself in a text box ("Work authorization status") gets
the code's wording in your words ("U.S. citizen"), never the code.

`work_location_preference` (round 10) is your work-arrangement preference in your words:
"Remote", "Hybrid", "On-site", "Remote or hybrid". A single-choice select whose options are all
work modes (remote, hybrid, on-site, in-office, work from home …) takes it whatever the site
typed the field as, so Upstart's "Location Preference" select is no longer read as your
address. The value is placed on the exact option when one names it, otherwise Jev maps it
onto the site's option wording ("Fully remote", "Hybrid (2-3 days in office)") at the usual
gate. A question about your current or previous arrangement is not a preference and stays
unanswered. Without the key the select is held for you; the verified address never answers
it.

`desired_salary` (round 10) states one amount with its unit and, ideally, its currency:
"USD 95,000 per year", "$45/hr", "95k annually". Three readings need no model decision
([dynamic-application-routing.md](dynamic-application-routing.md), "Round 10"):
- a base, annual, expected or target salary wording ("What is your desired base salary?",
  "Expected annual salary (USD)") gets the value as saved, because a plain desired salary
  states a base figure; a question that names a unit or currency the value does not carry
  holds, and so does an OTE, total-compensation, bonus or equity wording, or a saved value
  that names OTE, total or bonus itself;
- a select of salary ranges takes the one range whose bounds contain the amount in the same
  unit, and holds when none does, when two share the boundary, when the unit or currency
  differs, or when nothing states which unit the ranges are in;
- a select whose options are all pay periods (Hourly / Monthly / Yearly) takes the unit the
  value states, whatever the select's label.

`authorized_to_work_us` and `requires_visa_sponsorship` stay your own answers. Case, dashes
and spaces do not matter, and "H-1B" is `h1b`. A status that contradicts one of them fails
import and names both keys. A profile saved before the vocabulary grew may still contain
such a pair: then nothing is derived, and your own answers decide. The contradictions are:
- A citizen, permanent resident, asylee or refugee cannot require sponsorship or be
  unauthorized.
- DACA, TPS, a pending adjustment and a dependent EAD cannot be unauthorized.
- OPT cannot be unauthorized and cannot need no sponsorship.
- H-1B cannot need no sponsorship.
- `not_authorized` cannot be authorized or need no sponsorship.
- TN and `other_visa` are not checked.

The five statements (`acknowledge_privacy_notice` through `consent_background_check`) are
definitions you confirm once. A site's consent or attestation reuses your answer only when
one Jev decision finds it fully covered by exactly one of them, adding no further
obligation. Anything more holds with the statement quoted, for example "no AI tools during
interviews", a non-compete, arbitration or drug testing. Some obligations hold before any
decision, unless one of your saved statements names the same kind:
- drug or alcohol tests;
- contacting previous or current employers;
- non-compete, non-solicitation or non-disclosure;
- arbitration or waivers;
- AI tools;
- at-will employment;
- a background check, which only `consent_background_check` names;
- credit, driving-record, fingerprint, social-media or ongoing screening. Each key that has a semantic type
imports with it: referral, sponsorship, work authorization, EEO (gender, Hispanic/Latino,
race/ethnicity, veteran, disability), pronouns, location, school, degree, relocation,
salary, start date and the statements (consent or attestation). Education discipline stays
untyped, because sites type "Discipline" as a custom question. An older untyped answer to
the same question is superseded by the typed one.
Each key lists a few observed wordings. With AI routing, a differently worded
question can reuse the answer when Jev finds it asks exactly the same thing
([dynamic-application-routing.md](dynamic-application-routing.md), "Reworded
questions"). Referral
wording also accepts “How did you hear about us?” and “Where did you first hear about
the company?”. A referrer's name is a different question. Per Leo's instruction,
`"Company career page"` is the referral default for all applications. With AI routing,
a form that does not offer that exact option is never held on it. The option meaning
the company's careers page or website is chosen, otherwise "Other", otherwise a job
board or LinkedIn, otherwise the first option
(see [dynamic-application-routing.md](dynamic-application-routing.md)).

The importer accepts the original longer sponsorship key (including its accidental
space), `Where_did_you_first_hear_about_company`, and
`Were_you_referred_to_this_position_by_a_current_employee` as aliases. The supplied
`are_you_above_the_age_of_18`, `Are_you_currently_authorized_to_work_in_the_US`,
`School` and `Degree` keys also work. Exports use the shorter
keys above. JSON cannot have a comma after the last value before `}`.

Age above 18 does not supply a birth date or answer a different age threshold.
US work authorization does not establish citizenship or authorization in another
country. A bachelor's degree does not supply a major or graduation date.

Education dates accept `YYYY-MM` or month/year text and are stored as `YYYY-MM`,
without inventing a day. They apply only to education questions; bare “Start date”
and job availability or employment-history dates do not use them. An end date cannot
precede the start date. The importer also derives month-name and year answers for
separate controls whose complete question identifies the education start or end
date. Bare “Month” and “Year” remain ambiguous. Hispanic/Latino “No” does not infer race,
and “not a protected
veteran” does not imply that the person never served in the military.

Other protected attributes, salary, consent and narratives
keep their scoped saved-answer / user-input route. No values are inferred from
these defaults. A job-search salary floor is not an answer to desired salary.

## `career_motivation`: what you look for in a role

`career_motivation` is the one key that is not an answer to a form question. Write two or
three sentences, once, about what you look for in a role (the kind of work, the way results
are measured, the environment). Import stores it as a verified, user-stated candidate fact
(`id` and `key` `career_motivation`, source `user:simple-answers`, never a saved answer), so
the writer may cite it: an interest, motivation or "why us" narrative states, as its
reason, the alignment between the job description's cited requirements and your cited
experience, and cites this statement when one exists. With `null` the reason must come from a cited story
passage about this kind of work, else the question holds; a null never erases an earlier statement, an unchanged one keeps its
verification time, and export reads the statement back from the profile. The import
report lists `facts_updated` (keys only, never the text).
