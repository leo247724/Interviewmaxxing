# Simple application answers

Edit one small JSON map with your contact details. Each key has a string value or
`null` for an unanswered field. The map imports into the existing candidate profile;
the application runner keeps reading that profile as its source of truth.

The map covers contact questions repeatedly seen in the real application forms:
names, email, phone, LinkedIn, websites and address components. It also accepts
fourteen explicit reusable answers, stored through the existing saved-answer system.

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
contact value. The fourteen additional reusable-answer keys may be omitted or `null`;
that adds no new answer and leaves earlier confirmed saved answers intact.
First name, last name and a valid email are required for import. Whitespace-only
strings become `null`; phone and postal codes remain strings to preserve formatting
and leading zeroes. Unknown keys and duplicate JSON keys are rejected. An unchanged
import preserves the original verification time.

Import preserves the selected resume, work history, facts and unrelated saved answers. It
does not open a browser or prepare or submit an application. Export and import
write owner-only files; command output lists keys without printing their values.

Nonblank values for the fourteen additional keys become explicitly
user-confirmed **GLOBAL** saved answers,
reusable across applications when the complete question matches. Sponsorship must
be `"Yes"`, `"No"`, or `null`; it does not establish work authorization. The employee
referral, age, work-authorization and Hispanic/Latino answers also accept `"Yes"`,
`"No"`, or `null`. Referral
wording also accepts “How did you hear about us?” and “Where did you first hear about
the company?”. A referrer's name is a different question. Per Leo's instruction,
`"Company career page"` is the referral default for all applications.

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
