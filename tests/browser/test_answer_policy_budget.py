"""Round 12: ``CallBudget.allow_calls``, the room a pass whose calls are counted before it starts
(one answer-policy decision per open screener) adds on top of the form's allowance. No provider
is called: budgets only reserve and record fictional receipts."""
from __future__ import annotations

import pytest

from interviewmaxxing_browser.ai import providers
from interviewmaxxing_browser.ai.providers import AIHold, CallBudget, CallReceipt

MODEL = "typesafe/jev-1.13"


def limits(budget: CallBudget) -> tuple[int, float]:
    """The budget's call and USD limits."""
    return budget.max_calls, budget.max_usd


def test_allow_calls_and_allow_form_share_the_form_caps() -> None:
    """The caps allow_calls respects are the form caps allow_form uses (WP14 pins their values):
    a budget allow_form took to its caps gets no more room from allow_calls."""
    budget = CallBudget(scales_with_form=True)
    budget.allow_form(1000)
    assert limits(budget) == (providers.FORM_CAP_CALLS, pytest.approx(providers.FORM_CAP_USD))
    budget.allow_calls(10, 0.50)
    assert limits(budget) == (providers.FORM_CAP_CALLS, pytest.approx(providers.FORM_CAP_USD))


def test_a_fixed_budget_keeps_its_limits() -> None:
    """Without scales_with_form, allow_calls changes nothing, whatever it asks for."""
    budget = CallBudget()
    budget.allow_calls(10, 0.40)
    assert limits(budget) == (48, 0.50)
    fixed = CallBudget(max_calls=5, max_usd=0.05)
    fixed.allow_calls(200, 5.0)
    assert limits(fixed) == (5, 0.05)


def test_no_calls_add_no_room() -> None:
    """With calls <= 0 neither limit moves, even when USD is offered."""
    budget = CallBudget(scales_with_form=True)
    budget.allow_form(2)
    before = limits(budget)
    for calls in (0, -1, -12):
        budget.allow_calls(calls, 0.50)
        assert limits(budget) == before, calls


def test_the_room_lands_on_top_of_a_fresh_scaling_budget() -> None:
    """With nothing used, each pass adds its calls and USD to the current limits."""
    budget = CallBudget(scales_with_form=True)
    budget.allow_calls(4, 0.02)
    assert budget.max_calls == 52 and budget.max_usd == pytest.approx(0.52)
    budget.allow_calls(6, 0.03)
    assert budget.max_calls == 58 and budget.max_usd == pytest.approx(0.55)


def test_the_room_lands_on_top_of_a_forms_allowance() -> None:
    """After allow_form, the pass's room is added to the form's allowance."""
    budget = CallBudget(scales_with_form=True)
    budget.allow_form(1)
    form_calls, form_usd = limits(budget)
    assert form_calls == providers.FORM_BASE_CALLS + providers.FORM_WRITER_CALLS
    assert form_usd == pytest.approx(providers.FORM_BASE_USD + providers.FORM_WRITER_USD)
    budget.allow_calls(10, 0.05)
    assert budget.max_calls == form_calls + 10
    assert budget.max_usd == pytest.approx(form_usd + 0.05)


def test_the_room_starts_from_what_was_used_when_that_passed_the_limit() -> None:
    """max(limit, used) + room: calls or USD already used beyond a limit are the base."""
    budget = CallBudget(scales_with_form=True, max_calls=5, calls=9, max_usd=0.10,
                        reserved_usd=0.35)
    budget.allow_calls(2, 0.05)
    assert budget.max_calls == 11 and budget.max_usd == pytest.approx(0.40)
    # A reported cost above its reservation counts as used (USD 0.25 reserved, 0.60 reported).
    spent = CallBudget(scales_with_form=True, max_calls=1, max_usd=0.25)
    spent.reserve(b"{}", 0.25)
    spent.record(CallReceipt("policy", MODEL, MODEL, 0.1, 0.60, 0.25, "OK"))
    assert spent.reserved_usd == pytest.approx(0.60)
    spent.allow_calls(2, 0.10)
    assert spent.max_calls == 3 and spent.max_usd == pytest.approx(0.70)


def test_the_caps_bound_the_room() -> None:
    """However much room is asked for, the limits stop at the form caps."""
    budget = CallBudget(scales_with_form=True)
    budget.allow_form(3)
    budget.allow_calls(providers.FORM_CAP_CALLS, providers.FORM_CAP_USD)
    assert budget.max_calls == providers.FORM_CAP_CALLS
    assert budget.max_usd == pytest.approx(providers.FORM_CAP_USD)
    fresh = CallBudget(scales_with_form=True)
    fresh.allow_calls(1000, 50.0)
    assert fresh.max_calls == providers.FORM_CAP_CALLS
    assert fresh.max_usd == pytest.approx(providers.FORM_CAP_USD)


def test_a_budget_at_its_call_cap_gets_no_more_calls() -> None:
    """Once the capped number of calls is used, the cap refuses the room and the next call is
    held."""
    budget = CallBudget(scales_with_form=True)
    budget.allow_form(1000)
    for _ in range(providers.FORM_CAP_CALLS):
        budget.reserve(b"{}", 0.001)
    budget.allow_calls(4, 0.02)
    assert budget.max_calls == providers.FORM_CAP_CALLS
    with pytest.raises(AIHold, match="budget exhausted"):
        budget.reserve(b"{}", 0.001)


def test_a_negative_usd_adds_no_money_but_the_calls_still_count() -> None:
    """A negative USD adds nothing (the USD limit never drops) while the calls are added."""
    budget = CallBudget(scales_with_form=True)
    budget.allow_calls(3, -1.00)
    assert budget.max_calls == 51 and budget.max_usd == pytest.approx(0.50)
    budget.allow_form(1)
    form_usd = budget.max_usd
    budget.allow_calls(2, -0.25)
    assert budget.max_usd == pytest.approx(form_usd)


def test_the_room_only_moves_the_limits_and_lets_the_counted_calls_run() -> None:
    """allow_calls spends and records nothing; an exhausted budget then makes exactly the calls
    it was given room for."""
    budget = CallBudget(scales_with_form=True, max_calls=2, max_usd=0.50)
    budget.reserve(b"{}", 0.25)
    budget.reserve(b"{}", 0.25)
    with pytest.raises(AIHold, match="budget exhausted"):
        budget.reserve(b"{}", 0.25)
    budget.allow_calls(2, 0.50)
    assert (budget.calls, budget.reserved_usd, budget.receipts) == (2, 0.50, [])
    assert limits(budget) == (4, 1.00)
    budget.reserve(b"{}", 0.25)
    budget.reserve(b"{}", 0.25)
    with pytest.raises(AIHold, match="budget exhausted"):
        budget.reserve(b"{}", 0.25)
