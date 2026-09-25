"""The ``routing.trace`` event and the ``failed_fields`` metadata are diagnostic projections:
ids, stages, statuses, scores and page wording, never fact values, generated prose, review
text or values read back from the page. The events table is append-only, so nothing may be
persisted that would later need redacting."""

from interviewmaxxing_cli.runner import _traces_since, project_trace, redact_detail


def test_project_trace_drops_values_prose_and_review_text():
    trace = {
        "stage": "draft", "field_id": "cover", "field_fingerprint": "ab" * 32, "status": "WRITER_HELD",
        "facts": [{"id": "fact_1", "key": "experience", "value": "Managed a $2M budget at Acme"}],
        "sentences": ["I managed a two million dollar budget."],
        "missing_information": ["the campaign's outcome"],
        "review_feedback": "Claims an outcome the facts do not state.",
        "review_issues": [{"issue": "unsupported outcome"}],
        "answer": "I managed…", "value": "Austin", "typed_value": "Aus",
        "probabilities": {"SUPPORTED": 0.4, "UNSUPPORTED": 0.6},
        "confidence": 0.91, "reference_ids": ["fact_1"], "candidate_ids": [["simple_answer_1"]],
        "decisions": {"equivalent_0": {"choice": "o0", "confidence": 0.97, "probability": 0.98}},
        "question": "Tell us about a campaign you led",
        "reason": "x" * 1000,
        "nested": {"facts": [{"value": "secret"}], "score": 0.5},
    }
    projected = project_trace(trace)
    for key in ("facts", "sentences", "missing_information", "review_feedback", "review_issues",
                "answer", "value", "typed_value"):
        assert key not in projected
    assert projected["stage"] == "draft"
    assert projected["field_id"] == "cover"
    assert projected["status"] == "WRITER_HELD"
    assert projected["probabilities"] == {"SUPPORTED": 0.4, "UNSUPPORTED": 0.6}
    assert projected["reference_ids"] == ["fact_1"]
    assert projected["decisions"]["equivalent_0"]["choice"] == "o0"
    assert projected["question"] == "Tell us about a campaign you led"
    assert "reason" not in projected  # writer-side reasons quote model text and are dropped
    decision = project_trace({"stage": "option_equivalence", "reason": "x" * 1000})
    assert len(decision["reason"]) <= 303 and decision["reason"].endswith("…")
    assert projected["nested"] == {"score": 0.5}
    assert "secret" not in repr(projected)


def test_project_trace_keeps_enum_like_values_as_strings():
    class _Kind:
        def __str__(self) -> str:
            return "STATE"

    projected = project_trace({"stage": "residence_screener", "semantic_type": _Kind(), "count": 3})
    assert projected == {"stage": "residence_screener", "semantic_type": "STATE", "count": 3}


def test_redact_detail_keeps_the_shape_but_not_the_values():
    assert redact_detail("reads back [\"display '+ 1'\"]") == "reads back ['…']"
    assert redact_detail("reads back 'jane@example.com'") == "reads back '…'"
    assert redact_detail("option not found") == "option not found"
    assert redact_detail("reads back +1 512 555 0142 (+1)") == "reads back … (+1)"
    assert redact_detail("typed jane.doe+jobs@example.co.uk into the box") == "typed …@… into the box"
    assert redact_detail("shows https://example.com/profile/jane") == "shows …"
    assert redact_detail("2 of 3 options matched") == "2 of 3 options matched"
    assert redact_detail(None) is None
    assert len(redact_detail("a" * 900) or "") <= 301  # the limit plus the ellipsis


def test_project_trace_drops_writer_side_reasons_but_keeps_decision_reasons():
    writer = project_trace({"stage": "draft", "status": "WRITER_HELD", "reason": "Narrative needs facts: …"})
    assert "reason" not in writer and writer["status"] == "WRITER_HELD"
    decision = project_trace({"stage": "referral_policy", "status": "HELD", "reason": "Jev MALFORMED_RESPONSE"})
    assert decision["reason"] == "Jev MALFORMED_RESPONSE"


def test_traces_since_records_each_trace_once_even_when_the_list_is_truncated():
    traces = [{"stage": "a"}, {"stage": "b"}]
    assert [t["stage"] for t in _traces_since(traces)] == ["a", "b"]
    assert _traces_since(traces) == []
    traces = [*traces[1:], {"stage": "c"}]           # the resolver dropped the oldest trace
    assert [t["stage"] for t in _traces_since(traces)] == ["c"]
    assert "_recorded_by_runner" not in project_trace(traces[-1])
