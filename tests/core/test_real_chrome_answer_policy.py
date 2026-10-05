"""Exercise local driver answer policies without browser, profile or provider I/O."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parents[2] / '.imx/dynamic-applications/real-chrome/ashby.py'


@pytest.fixture
def policy():
    if not DRIVER.exists():
        pytest.skip('Local operational driver is not installed')
    tree = ast.parse(DRIVER.read_text())
    names = {'us_location_only', 'policy_answer', 'yesno_for', 'years_option_regex', 'opts_for'}
    ns = {'re': re, 'SA': {'willing_to_relocate': 'Yes', 'previously_employed_here': 'No',
                          'related_to_employee': 'No'},
          'YESNO': [(r'comfortable|experience|able to|sponsor|currently', 'Yes')],
          'OPTS': [(r'experience|able to|sponsor|currently', '^yes')]}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(DRIVER), 'exec'), ns)
    return ns


@pytest.mark.parametrize(('question', 'answer'), [
    ('Can you work without visa sponsorship?', 'Yes'),
    ('Will you now or in the future require work visa sponsorship?', 'No'),
    ('Are you authorized to work in the United States?', 'Yes'),
    ('Do you have legal authorization to work in the United States?', 'Yes'),
    ('Are you authorized to work in Canada?', None),
    ('Are you not authorized to work in the United States?', None),
    ('Are you authorized to work in this country?', None),
    ('Can you work in Canada without sponsorship?', None),
    ('Are you currently located in Austin?', 'Yes'),
    ('Are you currently based in London?', None),
    ('Do you live in New York or Austin?', None),
    ('Are you willing to relocate?', 'Yes'),
    ('Have you ever worked for our company in the past?', 'No'),
    ('Do you have any relatives employed at the company?', 'No'),
])
def test_contextual_binary_questions(policy, question, answer):
    assert policy['policy_answer'](question) == (True, answer)
    assert policy['yesno_for'](question) == answer
    options = policy['opts_for'](question)
    if answer is None:
        assert options is None  # never fall through to broad positive screeners
    else:
        assert re.search(options[0], answer, re.I)
        assert not re.search(options[0], 'No' if answer == 'Yes' else 'Yes', re.I)


@pytest.mark.parametrize(('question', 'years'), [
    ('How many years of paid media experience do you have?', '7'),
    ('Years of experience in performance marketing', '8'),
    ('How many years of SEO experience do you have?', '6'),
    ('How many years of digital marketing experience do you have?', '8'),
    ('How many years of Python experience do you have?', None),
    ('How many years of SEO management experience do you have?', None),
    ('How many years of digital marketing experience in B2B SaaS?', None),
    ('Years of experience with paid media in the last five years', None),
])
def test_duration_requires_specific_evidence(policy, question, years):
    assert policy['policy_answer'](question) == (True, years)
    assert policy['yesno_for'](question) is None
    options = policy['opts_for'](question)
    if years is None:
        assert options is None
    else:
        assert re.search(options[0], years + ' years')
        assert not re.search(options[0], '10+ years')
        assert not re.search(options[0], '4 years')
        assert re.search(options[0], '5\u201310 years')


def test_standing_experience_policy_preserved(policy):
    question = 'Are you comfortable leading client engagements?'
    assert policy['policy_answer'](question) == (False, None)
    assert policy['yesno_for'](question) == 'Yes'


@pytest.mark.parametrize(('location', 'answer'), [
    ('Remote - US', 'Yes'),
    ('Austin, TX, United States', 'Yes'),
    ('USA', 'Yes'),
    ('Remote - Canada', None),
    ('US / Canada', None),
    ('US and Antarctica', None),
    ('Remote - EMEA', None),
    ('Remote', None),
    (None, None),
])
def test_generic_work_country_uses_saved_job_evidence(policy, location, answer):
    label = 'Are you legally authorized to work in the country where this position is based?'
    assert policy['policy_answer'](label, location) == (True, answer)
    assert policy['yesno_for'](label, location) == answer
    options = policy['opts_for'](label, location)
    assert (options is None) == (answer is None)


@pytest.mark.parametrize('label', [
    'Are you legally authorized to work in Canada?',
    'Are you legally authorized to work in Japan?',
    'Will you require visa sponsorship in Canada?',
])
def test_explicit_foreign_question_overrides_us_job(policy, label):
    assert policy['policy_answer'](label, 'Remote - US') == (True, None)


@pytest.mark.parametrize('location', ['Canada', 'US / Canada', 'Japan', 'Remote - EMEA'])
def test_generic_sponsorship_cannot_inherit_us_policy_for_foreign_job(policy, location):
    assert policy['policy_answer']('Will you require visa sponsorship?', location) == (True, None)


def test_residence_country_requires_profile_evidence_not_job_location(policy):
    label = 'Are you legally authorized to work in the country where you currently live?'
    assert policy['policy_answer'](label, 'US') == (True, None)
    policy['SA']['country'] = 'United States'
    assert policy['policy_answer'](label, 'Canada') == (True, 'Yes')
    policy['SA']['country'] = 'Canada'
    assert policy['policy_answer'](label, 'US') == (True, None)


def test_sponsorship_us_context_keeps_standing_answer(policy):
    assert policy['policy_answer']('Will you require visa sponsorship?', 'Remote - US') == (True, 'No')


@pytest.mark.parametrize(('label', 'answer'), [
    ('Are you willing to relocate?', 'Yes'),
    ('Would you relocate to Austin?', 'Yes'),
    ('Do you live in the Austin area or are you open to relocation to the Austin area?', 'Yes'),
    ('Do you currently reside in or will you be relocating to any of the following states?', None),
    ('Are you relocating to New York?', None),
    ('Do you have relocation plans?', None),
])
def test_relocation_willingness_is_not_existing_plans(policy, label, answer):
    assert policy['policy_answer'](label) == (True, answer)
    assert policy['yesno_for'](label) == answer


@pytest.mark.parametrize(('location', 'accepted'), [
    ('Anywhere in the United States', True),
    ('Austin, TX', True),
    ('Austin, Texas', True),
    ('Austin, TX / Canada', False),
    ('Austin, TX / United Kingdom', False),
    ('Anywhere in the United States or Canada', False),
    ('Anywhere in the United States / Atlantis', False),
    ('Austin', False),
    ('TX', False),
    ('Anywhere in the world', False),
])
def test_clear_us_location_variants_remain_fail_closed(policy, location, accepted):
    assert policy['us_location_only'](location) is accepted
    label = 'Are you legally authorized to work in the country where this position is based?'
    assert policy['policy_answer'](label, location) == (True, 'Yes' if accepted else None)


@pytest.mark.parametrize(('label', 'answer'), [
    ('What is your preferred name?*', 'Avery'),
    ('What state do you legally reside in?*', 'Texas'),
    ('What US State do you reside in?*', 'Texas'),
    ('What is your home city?*', 'Austin'),
    ('What is your home state? *', 'Texas'),
])
def test_profile_question_aliases_use_existing_verified_values(policy, label, answer):
    policy['SA'].update(first_name='Avery', city='Austin', state='TX')
    assert policy['policy_answer'](label) == (True, answer)
    assert policy['yesno_for'](label) is None
    assert re.search(policy['opts_for'](label)[0], answer)


def test_profile_alias_missing_value_holds(policy):
    assert policy['policy_answer']('What is your home state?') == (True, None)


@pytest.mark.parametrize('label', [
    'What state will you reside in?',
    'What is your prospective home city?',
    'What state do you plan to relocate to?',
    'Do you legally reside in Canada?',
])
def test_profile_alias_does_not_answer_plans_or_foreign_residence(policy, label):
    policy['SA'].update(first_name='Avery', city='Austin', state='TX')
    _, answer = policy['policy_answer'](label)
    assert answer is None


@pytest.mark.parametrize('option', ['TX', 'Texas'])
def test_state_alias_accepts_full_state_or_postal_code_exactly(policy, option):
    policy['SA']['state'] = 'TX'
    regex = policy['opts_for']('What state do you legally reside in?')[0]
    assert re.search(regex, option)
    assert not re.search(regex, option + ' or Canada')


@pytest.mark.parametrize('label', [
    'This role has a set, non-negotiable hourly rate of $40/hour. This role also does not offer PTO or medical, dental, vision benefits. Are you comfortable accepting a role with these terms?*',
    'Are you comfortable with the salary range of $60,000 to $80,000?',
    'Are your compensation expectations aligned with our budget?',
    'Are you willing to accept a position without benefits?',
    'Does this hourly rate work for you?',
    'Do you agree to the commission-only compensation terms?',
])
def test_compensation_acceptance_requires_explicit_personal_agreement(policy, label):
    assert policy['policy_answer'](label) == (True, None)
    assert policy['yesno_for'](label) is None
    assert policy['opts_for'](label) is None


@pytest.mark.parametrize('label', [
    'Are you comfortable working in a hybrid arrangement?',
    'Are you comfortable leading client engagements?',
    'Are you comfortable generating reports?',
    'Are you able to work in the office three days per week?',
])
def test_work_arrangement_and_experience_standing_policy_unchanged(policy, label):
    assert policy['policy_answer'](label) == (False, None)
    assert policy['yesno_for'](label) == 'Yes'


def test_salary_request_is_not_acceptance_question(policy):
    assert policy['policy_answer']('What are your salary expectations?') == (False, None)


@pytest.mark.parametrize('label', [
    'What are you seeking in terms of compensation?*',
    'What minimum salary would you accept?',
    'What salary would you accept?',
    'Please provide your minimum acceptable compensation.',
    'What is your desired salary?',
])
def test_candidate_compensation_expectations_are_not_offer_acceptance(policy, label):
    assert policy['policy_answer'](label) == (False, None)


@pytest.mark.parametrize('label', [
    'Our salary range is $40,000 to $60,000. Are you willing to accept it?',
    'What minimum salary would you accept within our salary range of $40,000 to $60,000?',
])
def test_candidate_question_does_not_override_concrete_offer_terms(policy, label):
    assert policy['policy_answer'](label) == (True, None)


@pytest.mark.parametrize('label', [
    'What state do you live in?',
    'What state do you currently reside in?',
    'For hiring purposes, in which state do you currently reside in?',
])
def test_open_state_question_precedes_binary_residence(policy, label):
    policy['SA'].update(city='Austin', state='TX')
    assert policy['policy_answer'](label) == (True, 'Texas')
    assert policy['yesno_for'](label) is None
    assert re.search(policy['opts_for'](label)[0], 'Texas')


def test_timezone_uses_verified_austin_and_exact_options(policy):
    label = 'What time zone are you located in?'
    assert policy['policy_answer'](label) == (True, None)
    policy['SA'].update(city='Austin', state='TX')
    assert policy['policy_answer'](label) == (True, 'Central Time (US)')
    regex = policy['opts_for'](label)[0]
    assert re.search(regex, 'America/Chicago')
    assert not re.search(regex, 'Central European Time')
