"""Fictional stories: docx parsing, deterministic analysis, bounded chunks and facts
that never invent a number. No database, provider or real document is involved."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from interviewmaxxing_generation.knowledge import stories as st

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

BAKERY = (
    "I managed a $120,000 annual paid search budget for a regional bakery chain in 2024 and "
    "grew online orders by 35%. As the marketing manager of the chain I worked with the owner "
    "and two store managers every week. The problem was that the ads counted every click as a "
    "sale, so nobody knew which campaigns paid for themselves. I set up conversion tracking in "
    "Google Ads and connected HubSpot so we could send closed orders back to the platform. "
    "My team of 2 coordinators reported to me and produced new ad copy every week. "
    "It was a busy year. "
    "I learned that clean tracking matters more than bidding tricks, because the owner only "
    "trusted numbers that matched the till. We tested landing pages for the wedding cake line "
    "and raised the form conversion rate from 3% to 7%. The store managers asked for a weekly "
    "summary, so I wrote a short report that listed orders by campaign and by store. "
    "Radio had been the chain's main channel for years, and the owner was reluctant to move "
    "money out of it. I showed the owner the order data from Google Ads next to the radio "
    "spend, and the chain moved a third of the radio budget into paid search. "
    "Over eighteen months the paid search program brought in over 4,000 online orders."
)
OVENBOARD = (
    "I built an internal reporting tool, Ovenboard, for the bakery chain's store managers. "
    "It ran on Node.js and TypeScript and pulled Meta Ads and Google Ads spend into one "
    "dashboard. The tool saved about 6 hours of manual work per week for the two store "
    "managers. Building it taught me that a report nobody opens is worse than no report."
)
VAGUE = (
    "I helped where I could and things went well. The team was friendly. "
    "We talked about the plan and agreed on next steps."
)


def docx(path: Path, paragraphs: list[list[tuple[str, bool]]], *, heading_styles: dict[int, str] | None = None) -> Path:
    """A minimal .docx: each paragraph is a list of (text, bold) runs."""
    heading_styles = heading_styles or {}

    def run(text: str, bold: bool) -> str:
        props = '<w:rPr><w:b/></w:rPr>' if bold else ""
        return f'<w:r>{props}<w:t xml:space="preserve">{text}</w:t></w:r>'

    body = ""
    for index, runs in enumerate(paragraphs):
        style = heading_styles.get(index)
        props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body += f"<w:p>{props}{''.join(run(t, b) for t, b in runs)}</w:p>"
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        archive.writestr("word/document.xml", document)
    return path


@pytest.fixture
def stories_docx(tmp_path: Path) -> Path:
    """Like the real document: one paragraph, bold run headings, no space after a heading."""
    return docx(tmp_path / "fictional-stories.docx", [[
        ("Stories 01 - Paid search for a regional bakery chain", True), (BAKERY, False),
        ("Stories 02 - Building Ovenboard", True), (OVENBOARD, False),
        ("Stories 03 - A quiet quarter", True), (VAGUE, False),
    ]])


def test_bold_run_headings_split_one_paragraph_into_numbered_stories(stories_docx: Path) -> None:
    stories = st.parse_stories(st.read_docx(stories_docx))
    assert [(s.number, s.title) for s in stories] == [
        (1, "Paid search for a regional bakery chain"), (2, "Building Ovenboard"), (3, "A quiet quarter")]
    assert stories[0].sentences[0].startswith("I managed a $120,000")
    assert stories[0].sentences[-1] == "Over eighteen months the paid search program brought in over 4,000 online orders."
    assert len(stories[0].sentences) == 12
    assert stories[0].body == " ".join(stories[0].sentences)


def test_heading_paragraphs_and_unnumbered_bold_titles_are_a_fallback(tmp_path: Path) -> None:
    path = docx(tmp_path / "headings.docx", [
        [("Paid search for a bakery", False)], [(BAKERY, False)],
        [("Ovenboard", True)], [(OVENBOARD, False)],
    ], heading_styles={0: "Heading1"})
    stories = st.parse_stories(st.read_docx(path))
    assert [(s.number, s.title) for s in stories] == [(1, "Paid search for a bakery"), (2, "Ovenboard")]
    assert len(stories[1].sentences) == 4


def test_missing_headings_and_broken_files_fail_without_text(tmp_path: Path) -> None:
    path = docx(tmp_path / "plain.docx", [[(BAKERY, False)]])
    with pytest.raises(st.StoryParseError, match="No story headings"):
        st.parse_stories(st.read_docx(path))
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip")
    with pytest.raises(st.StoryParseError, match="not a readable"):
        st.read_docx(broken)
    with pytest.raises(st.StoryParseError):
        st.read_docx(tmp_path / "missing.docx")


def test_sentence_splitting_handles_missing_spaces_and_decimals() -> None:
    assert st.split_sentences("Raised it from 3% to 8.5%.Then we stopped. Node.js stayed.") == [
        "Raised it from 3% to 8.5%.", "Then we stopped.", "Node.js stayed."]


def test_analysis_extracts_employer_role_period_tools_and_themes(stories_docx: Path) -> None:
    bakery, ovenboard, quiet = st.parse_stories(st.read_docx(stories_docx))
    analysis = st.analyse_story(bakery)
    assert (analysis.employer, analysis.role, analysis.period) == ("regional bakery chain", "marketing manager", "2024")
    assert analysis.project is None
    assert {"Google Ads", "HubSpot"} <= set(analysis.tools)
    assert {"leadership", "budgets", "measurement", "results", "failures learned from",
            "persuasion", "stakeholders"} <= set(analysis.themes)
    assert analysis.situation[0].startswith("I managed") and any("problem" in s for s in analysis.situation)
    assert any(s.startswith("I set up conversion tracking") for s in analysis.actions)
    assert any("35%" in s for s in analysis.outcomes) and any("3% to 7%" in s for s in analysis.outcomes)
    assert "conversion tracking" in analysis.skills and "team leadership" in analysis.skills
    tool = st.analyse_story(ovenboard)
    assert tool.project == "Ovenboard" and tool.employer == "bakery chain" and tool.role is None
    assert {"Node.js", "TypeScript", "Meta Ads", "Google Ads"} <= set(tool.tools)
    assert tool.period is None
    assert st.analyse_story(quiet).outcomes == () and "results" not in st.analyse_story(quiet).themes


def test_ambiguous_tool_names_need_company() -> None:
    assert st.find_tools("The page loaded instantly and the clay pot cracked.") == []
    assert st.find_tools("We used Clay, 6sense and Instantly for outreach.") == ["Clay", "6sense", "Instantly"]


def test_chunks_are_bounded_headed_deterministic_and_round_trip(stories_docx: Path) -> None:
    index = st.build_story_index(stories_docx, verified_at=NOW)
    again = st.build_story_index(stories_docx, verified_at=NOW)
    assert [c.id for c in index.chunks] == [c.id for c in again.chunks]
    assert index.file_sha256 == hashlib.sha256(stories_docx.read_bytes()).hexdigest()
    bakery = [c for c in index.chunks if c.story_id == index.stories[0].story_id]
    assert bakery[0].kind == "summary" and bakery[0].number == 0
    assert [c.kind for c in bakery[1:]] == ["section"] * (len(bakery) - 1) and len(bakery) >= 2
    for chunk in index.chunks:
        assert chunk.id == "story:" + hashlib.sha256(chunk.text.encode()).hexdigest()
        assert st.STORY_CHUNK_ID.match(chunk.id)
        assert chunk.text == chunk.text.strip() and len(chunk.text) <= st.MAX_CHUNK_CHARS
        assert chunk.word_count <= st.MAX_CHUNK_WORDS
        header = st.parse_chunk_header(chunk.text)
        assert header is not None and header["story_id"] == chunk.story_id
        assert header["title"] == chunk.title and header["themes"] == list(chunk.themes)
        assert (header["employer"] or header["project"]) == chunk.employer
    sections = [c for c in bakery if c.kind == "section"]
    assert all(c.word_count >= st.MIN_CHUNK_WORDS for c in sections[:-1])
    assert " ".join(c.text.split("\n", 1)[1] for c in sections) == index.stories[0].body
    first_line = bakery[1].text.split("\n")[0]
    assert first_line.startswith("Story 01: Paid search for a regional bakery chain | employer: regional bakery chain | role: marketing manager | period: 2024 | themes: ")
    assert "Summary of story 01." in bakery[0].text and "Tools: " in bakery[0].text
    ovenboard = [c for c in index.chunks if c.story_id == index.stories[1].story_id]
    assert ovenboard[1].text.split("\n")[0].startswith("Story 02: Building Ovenboard | employer: bakery chain | themes: ")


def test_a_long_story_splits_into_several_sections(tmp_path: Path) -> None:
    long_body = " ".join(f"In month {i} I reviewed the search terms and wrote three new ads for the "
                         f"cinnamon roll campaign, then checked the order data with the owner." for i in range(1, 40))
    path = docx(tmp_path / "long.docx", [[("Stories 01 - A long year", True), (long_body, False)]])
    index = st.build_story_index(path, verified_at=NOW)
    sections = [c for c in index.chunks if c.kind == "section"]
    assert len(sections) >= 3
    assert all(st.MIN_CHUNK_WORDS <= c.word_count <= st.MAX_CHUNK_WORDS for c in sections[:-1])
    assert all(len(c.text) <= st.MAX_CHUNK_CHARS for c in index.chunks)


def test_facts_are_verbatim_user_authored_and_never_invent_numbers(stories_docx: Path) -> None:
    index = st.build_story_index(stories_docx, verified_at=NOW)
    _bakery, ovenboard, quiet = index.stories
    chunk_ids = {c.id for c in index.chunks}
    assert index.facts
    assert {entry["reason"] for entry in index.skipped} == {"plural_subject_only"}  # "We tested landing pages ..."
    assert not any("3% to 7%" in str(fact.value) for fact in index.facts)  # the team's work stays story evidence
    assert any("3% to 7%" in chunk.text for chunk in index.chunks)
    for fact in index.facts:
        story = next(s for s in index.stories if fact.id.startswith(f"sf_{s.story_id}_"))
        assert st.numbers_in(str(fact.value)) <= st.numbers_in(story.body), fact.id
        assert fact.key in {"employment", "achievement", "experience", "skills", "project", "education"}
        assert fact.source in chunk_ids and fact.source.startswith("story:")
        # Extracted, not confirmed: UNVERIFIED until the person imports it (CONTRACTS 3).
        assert not fact.is_verified and fact.verification.method is None and fact.verification.verified_at is None
        assert fact.evidence[0].startswith(f"Story {story.number:02d}: ")
    values = {k: [str(f.value) for f in index.facts if f.key == k] for k in {f.key for f in index.facts}}
    assert "Marketing manager, regional bakery chain (2024)" in values["employment"]
    assert any(v.startswith("I managed a $120,000 annual paid search budget")
               and v.endswith("(regional bakery chain, 2024)") for v in values["achievement"])
    assert any("team of 2 coordinators" in v for v in values["achievement"] + values.get("experience", []))
    skills = [f for f in index.facts if f.key == "skills"]
    assert any("HubSpot" in " ".join(f.evidence) for f in skills)
    assert not any("It was a busy year" in str(f.value) for f in index.facts)
    assert not any(f.id.startswith(f"sf_{quiet.story_id}_") for f in index.facts)
    project = [f for f in index.facts if f.id.startswith(f"sf_{ovenboard.story_id}_")]
    assert project and all(str(f.value).endswith(" (bakery chain)") for f in project)
    assert any("Ovenboard" in str(f.value) and f.key == "project" for f in project)
    assert any("6 hours" in str(f.value) and f.key == "achievement" for f in project)
    assert len({f.id for f in index.facts}) == len(index.facts)
    again = st.build_story_index(stories_docx, verified_at=NOW)
    assert [f.id for f in again.facts] == [f.id for f in index.facts]


def test_overlong_sentences_are_skipped_not_truncated(tmp_path: Path) -> None:
    long_sentence = "I managed " + ", ".join(f"campaign {i} with a $1,000 budget" for i in range(40)) + "."
    path = docx(tmp_path / "long-sentence.docx", [[("Stories 01 - Many campaigns", True), (long_sentence, False)]])
    index = st.build_story_index(path, verified_at=NOW)
    assert index.facts == ()
    assert index.skipped[0]["reason"] == "sentence_too_long" and index.skipped[0]["chars"] > st.MAX_FACT_CHARS


def test_receipt_carries_counts_ids_and_hashes_only(stories_docx: Path) -> None:
    index = st.build_story_index(stories_docx, verified_at=NOW)
    receipt = st.story_index_receipt(index)
    text = json.dumps(receipt)
    for word in ("bakery", "Ovenboard", "HubSpot", "120,000", "coordinators", "Paid search"):
        assert word not in text
    assert receipt["story_count"] == 3 and receipt["chunk_count"] == len(index.chunks)
    assert receipt["fact_count"] == len(index.facts) == sum(receipt["facts_by_key"].values())
    assert receipt["stories"][0]["has_employer"] and receipt["stories"][0]["has_role"]
    assert receipt["stories"][1]["has_project"] and receipt["stories"][1]["has_employer"]
    assert not receipt["stories"][2]["has_employer"] and receipt["stories"][2]["fact_count"] == 0
    assert receipt["stories"][0]["title_sha256"] == hashlib.sha256(index.stories[0].title.encode()).hexdigest()
    assert all(re.fullmatch(r"story:[0-9a-f]{64}", cid) for cid in receipt["chunk_ids"])
    review = st.facts_review(index, candidate_id="default")
    assert len(review["facts"]) == len(index.facts) and review["facts"][0]["verification"]["status"] == "UNVERIFIED"
    assert "stated_years" not in receipt["stories"][0] and receipt["stories"][0]["stated_year_count"] == 1
    assert review["stories"][0]["employer"] == "regional bakery chain"


# --- round 2: resume role links and periods ---------------------------------------------------


def _roles() -> list[st.ResumeRole]:
    return [
        st.ResumeRole("exp_bakery", "Crumb & Co. Bakeries", "Marketing Manager", "2023-04", "2024-09",
                      False, ("Managed paid search for twelve stores.",)),
        st.ResumeRole("exp_tool", "Ovenboard Labs", "Founder", "2024-10", None, True,
                      ("Built a reporting tool.",)),
        st.ResumeRole("exp_old", "Glaze Agency", "PPC Specialist", "2021-01", "2023-03", False, ()),
    ]


def test_a_name_link_needs_exactly_one_distinctive_company_token(stories_docx: Path) -> None:
    bakery, ovenboard, _quiet = st.parse_stories(st.read_docx(stories_docx))
    roles = _roles()
    link = st.match_role_by_name(ovenboard, st.analyse_story(ovenboard), roles)
    assert link is not None and (link.resume_role_id, link.method) == ("exp_tool", "employer_name")
    assert (link.company, link.title, link.start, link.end, link.current) == (
        "Ovenboard Labs", "Founder", "2024-10", None, True)
    assert link.period == "2024-10 to present" and link.contains_year("2026")
    # "bakery" in the story is not "Bakeries", and generic words never link.
    assert st.company_tokens("Crumb & Co. Bakeries") == {"crumb", "bakeries"}
    assert st.company_tokens("The Marketing Agency LLC") == set()
    assert st.match_role_by_name(bakery, st.analyse_story(bakery), roles) is None
    twins = [*roles, st.ResumeRole("exp_tool2", "Ovenboard Studio", "Advisor", "2020-01", "2020-06", False, ())]
    assert st.match_role_by_name(ovenboard, st.analyse_story(ovenboard), twins) is None


def test_period_precedence_is_resume_link_then_stated_year_then_none(stories_docx: Path) -> None:
    bakery, _ovenboard, quiet = st.parse_stories(st.read_docx(stories_docx))
    link = st.StoryRoleLink(bakery.story_id, "exp_bakery", "Crumb & Co. Bakeries", "Marketing Manager",
                            "2023-04", "2024-09", False, "jev_match", 0.93, 0.97)
    assert st.stated_years(bakery) == ["2024"]
    assert st.resolve_period(bakery, link) == ("2023-04 to 2024-09", "resume_role", False)
    older = replace(link, start="2021-01", end="2022-12")
    assert st.resolve_period(bakery, older) == ("2021-01 to 2022-12", "resume_role", True)
    assert st.resolve_period(bakery, None) == ("2024", "story", False)
    assert st.resolve_period(quiet, None) == (None, "none", False)
    current = replace(link, end=None, current=True)
    assert st.resolve_period(bakery, current) == ("2023-04 to present", "resume_role", False)
    undated = replace(link, start=None, end=None)
    assert st.resolve_period(bakery, undated) == ("2024", "story", False)


def test_linked_stories_carry_the_resume_dates_in_headers_facts_and_receipts(stories_docx: Path) -> None:
    document = st.read_stories(stories_docx)
    bakery, ovenboard, quiet = document.stories
    link = st.StoryRoleLink(bakery.story_id, "exp_bakery", "Crumb & Co. Bakeries", "Marketing Manager",
                            "2023-04", "2024-09", False, "jev_match", 0.93, 0.97)
    index = st.build_story_index(document, verified_at=NOW, links={bakery.story_id: link})
    chunks = [c for c in index.chunks if c.story_id == bakery.story_id]
    header = chunks[1].text.split("\n")[0]
    assert " | resume role: Marketing Manager, Crumb & Co. Bakeries | " in header
    assert " | period: 2023-04 to 2024-09 | " in header and "2024 |" not in header.replace("2024-09", "")
    parsed = st.parse_chunk_header(chunks[1].text)
    assert parsed is not None and parsed["resume_role"] == "Marketing Manager, Crumb & Co. Bakeries"
    assert parsed["period"] == "2023-04 to 2024-09"
    assert all(c.resume_role_id == "exp_bakery" and c.period == "2023-04 to 2024-09" for c in chunks)
    assert "The resume lists this role as Marketing Manager at Crumb & Co. Bakeries, 2023-04 to 2024-09." in chunks[0].text
    facts = [f for f in index.facts if f.id.startswith(f"sf_{bakery.story_id}_")]
    assert facts
    for fact in facts:
        assert "period_source: resume_role" in fact.evidence and "resume_role_id: exp_bakery" in fact.evidence
        assert fact.source in {c.id for c in chunks}
    employment = next(f for f in facts if f.key == "employment")
    assert employment.value == "Marketing manager, regional bakery chain (Crumb & Co. Bakeries, 2023-04 to 2024-09)"
    assert all(str(f.value).endswith("(regional bakery chain; resume: Crumb & Co. Bakeries, 2023-04 to 2024-09)")
               for f in facts if f.key != "employment")
    # Unlinked and undated: no period at all, no year anywhere in the value suffix.
    tool_facts = [f for f in index.facts if f.id.startswith(f"sf_{ovenboard.story_id}_")]
    assert tool_facts and all("period_source: none" in f.evidence and str(f.value).endswith(" (bakery chain)")
                              for f in tool_facts)
    assert not any(f.id.startswith(f"sf_{quiet.story_id}_") for f in index.facts)
    # Unlinked with a stated year: the story's own year, marked as such.
    plain = st.build_story_index(document, verified_at=NOW)
    plain_facts = [f for f in plain.facts if f.id.startswith(f"sf_{bakery.story_id}_")]
    assert all("period_source: story" in f.evidence and str(f.value).endswith("(regional bakery chain, 2024)")
               for f in plain_facts if f.key != "employment")
    assert plain.chunks[1].period == "2024" and plain.chunks[1].resume_role_id is None
    # Linking changes the chunk text, so the ids differ; the facts' ids differ with their values.
    assert {c.id for c in chunks}.isdisjoint({c.id for c in plain.chunks})
    receipt = st.story_index_receipt(index)
    row = receipt["stories"][0]
    assert row["link"] == {"method": "jev_match", "resume_role_id": "exp_bakery", "confidence": 0.93, "probability": 0.97}
    assert (row["period_source"], row["stated_year_count"], row["stated_year_outside_resume_role"]) == ("resume_role", 1, False)
    assert receipt["stories"][1]["link"] is None and receipt["stories"][1]["period_source"] == "none"
    assert receipt["facts_by_period_source"] == {"none": len(tool_facts), "resume_role": len(facts)}
    assert receipt["linked_stories"] == 1 and receipt["stated_year_discrepancies"] == 0
    assert "Crumb" not in json.dumps(receipt) and "regional bakery chain" not in json.dumps(receipt)
    review = st.facts_review(index, candidate_id="default")
    assert review["stories"][0]["resume_role"]["company"] == "Crumb & Co. Bakeries"
    assert review["stories"][0]["note"] is None
    markdown = st.facts_review_markdown(review)
    assert "resume role: Marketing Manager at Crumb & Co. Bakeries, 2023-04 to 2024-09 (link by jev_match, confidence 0.93, probability 0.97)" in markdown
    assert "| `" + facts[0].id + "` |" in markdown and "**check:**" not in markdown


def test_a_stated_year_outside_the_linked_role_is_flagged_and_the_resume_dates_win(stories_docx: Path) -> None:
    document = st.read_stories(stories_docx)
    bakery = document.stories[0]
    link = st.StoryRoleLink(bakery.story_id, "exp_old", "Glaze Agency", "PPC Specialist",
                            "2021-01", "2022-12", False, "jev_match", 0.91, 0.96)
    index = st.build_story_index(document, verified_at=NOW, links={bakery.story_id: link})
    facts = [f for f in index.facts if f.id.startswith(f"sf_{bakery.story_id}_")]
    assert facts and all("2021-01 to 2022-12" in str(f.value) and "2024" not in str(f.value) for f in facts)
    # The sentence that states the conflicting year yields no fact; it stays in the chunks.
    assert not any("$120,000" in str(f.value) for f in facts)
    assert [(entry["reason"], entry["story"]) for entry in index.skipped] == [
        ("stated_year_conflicts_with_resume_role", 1), ("plural_subject_only", 1)]  # "We tested landing pages ..."
    assert any("in 2024" in c.text for c in index.chunks if c.story_id == bakery.story_id)
    receipt = st.story_index_receipt(index)
    assert receipt["stated_year_discrepancies"] == 1
    assert receipt["stories"][0]["stated_year_outside_resume_role"] is True
    review = st.facts_review(index, candidate_id="default")
    assert review["stories"][0]["note"] and "outside the linked resume role" in review["stories"][0]["note"]
    assert "**check:**" in st.facts_review_markdown(review)


# --- round 2, items 4-5: text documents, sources, resume figure flags ------------------------------


SEO_STORY = (
    "I managed SEO for a portfolio of client accounts at a link-building agency in 2025, "
    "leading a team of 4 SEO specialists and reporting to the head of delivery. "
    "We grew organic sessions by 60% across the portfolio and cut churn from 9% to 4%. "
    "I built the reporting in Looker Studio and ran weekly reviews with each client."
)


def text_document(tmp_path: Path, name: str, heading: str) -> Path:
    path = tmp_path / name
    path.write_text(f"{heading}\n\n{SEO_STORY[:120]}\n{SEO_STORY[120:]}\n\nA second paragraph of the same story.\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(("name", "heading"), [
    ("story.md", "## Stories 04 - SEO account management at Linkforge"),
    ("story.md", "**Stories 04 - SEO account management at Linkforge**"),
    ("story.txt", "Stories 04 - SEO account management at Linkforge"),
])
def test_text_and_markdown_documents_read_like_a_docx(tmp_path: Path, name: str, heading: str) -> None:
    path = text_document(tmp_path, name, heading)
    document = st.read_stories(path)
    [story] = document.stories
    assert (story.number, story.title) == (4, "SEO account management at Linkforge")
    assert story.sentences[0].startswith("I managed SEO for a portfolio") and story.sentences[-1] == "A second paragraph of the same story."
    assert len(story.sentences) == 4 and story.body.count("\n") == 0
    assert document.file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    plain = tmp_path / "plain.md"
    plain.write_text("# My SEO year\n\n" + SEO_STORY + "\n", encoding="utf-8")
    [fallback] = st.read_stories(plain).stories
    assert (fallback.number, fallback.title) == (1, "My SEO year")
    with pytest.raises(st.StoryParseError, match=r"must be a \.docx"):
        st.read_stories(tmp_path / "story.rtf")
    binary = tmp_path / "bad.txt"
    binary.write_bytes(b"\xff\xfe not text")
    with pytest.raises(st.StoryParseError, match="UTF-8"):
        st.read_stories(binary)


def test_a_second_source_links_by_name_and_flags_a_team_size_difference(tmp_path: Path) -> None:
    path = text_document(tmp_path, "story.md", "## Stories 04 - SEO account management at Linkforge")
    document = st.read_stories(path)
    [story] = document.stories
    [analysis] = document.analyses
    roles = [*_roles(), st.ResumeRole("exp_links", "Linkforge", "SEO Project Manager", "2025-07", "2026-05", False,
                                       ("Led a team of 5 SEO specialists for agency clients.",))]
    link = st.match_role_by_name(story, analysis, roles)
    assert link is not None and (link.resume_role_id, link.method) == ("exp_links", "employer_name")
    index = st.build_story_index(document, verified_at=NOW, links={story.story_id: link}, source_id="candidate-stories-seo")
    assert index.source_id == "candidate-stories-seo" and index.facts
    assert all("story_source: candidate-stories-seo" in f.evidence for f in index.facts)
    assert all(st.story_source_of(f) == "candidate-stories-seo" for f in index.facts)
    assert st.story_source_of(index.facts[0].model_copy(update={"evidence": ["Story 04: x"]})) == "candidate-stories"
    assert st.story_source_of(index.facts[0].model_copy(update={"source": "resume"})) is None
    assert all("2025-07 to 2026-05" in str(f.value) for f in index.facts)
    assert st.team_sizes("a team of 4 SEO specialists and 12 clients") == {4}
    assert st.team_sizes("Led 5 SEO specialists; team of 5") == {5}
    assert st.resume_quantity_differences(story, link, roles) == [
        {"kind": "team size", "story": [4], "resume": [5], "resume_role_id": "exp_links"}]
    assert st.resume_quantity_differences(story, None, roles) == []
    same = [st.ResumeRole("exp_links", "Linkforge", "SEO Project Manager", "2025-07", "2026-05", False,
                          ("Led a team of 4 SEO specialists.",))]
    assert st.resume_quantity_differences(story, link, same) == []
    review = st.facts_review(index, candidate_id="default", roles=roles)
    assert review["source_id"] == "candidate-stories-seo"
    assert review["stories"][0]["resume_differences"][0]["kind"] == "team size"
    assert "team size of [4]" in review["stories"][0]["note"] and "states [5]" in review["stories"][0]["note"]
    markdown = st.facts_review_markdown(review)
    assert "Source: `candidate-stories-seo`" in markdown and "**check:**" in markdown
    # The story's own figure stays in its facts; nothing is resolved here.
    assert any("team of 4" in str(f.value) for f in index.facts)


# --- round 2b: durations against the linked role's tenure -----------------------------------------


def test_duration_claims_state_their_minimum_months() -> None:
    assert st.duration_claims("almost 2 years") == [("almost 2 years", 15)]
    assert st.duration_claims("over 3 years") == [("over 3 years", 37)]
    assert st.duration_claims("2 years") == [("2 years", 21)]
    assert st.duration_claims("about 18 months") == [("about 18 months", 17)]
    assert st.duration_claims("6 months") == [("6 months", 5)]
    assert st.duration_claims("under a year") == [("under a year", 0)]
    assert st.duration_claims("a little less than two years") == [("a little less than two years", 15)]
    assert st.duration_claims("saves 25 hours per week") == []
    assert st.duration_claims("giving a 26 year old kid the budget") == []
    assert st.duration_claims("a 30-year-old brand with 3 years of ads") == [("3 years", 33)]
    link = st.StoryRoleLink("s", "exp", "Crumb & Co.", "Manager", "2022-10", "2023-09", False, "jev_match", 0.95, 0.98)
    assert st.role_tenure_months(link, date(2026, 9, 25)) == 12
    assert st.role_tenure_months(replace(link, end=None, current=True), date(2026, 9, 25)) == 48
    assert st.role_tenure_months(replace(link, start=None), date(2026, 9, 25)) is None
    conflicts = st.tenure_conflicts("Over the course of almost 2 years we generated 7 figures.", link, date(2026, 9, 25))
    assert conflicts == [{"phrase": "almost 2 years", "minimum_months": 15, "tenure_months": 12, "resume_role_id": "exp"}]
    assert st.tenure_conflicts("After 6 months of testing we doubled leads.", link, date(2026, 9, 25)) == []
    assert st.tenure_conflicts("Over the course of almost 2 years we won.", None, date(2026, 9, 25)) == []


def test_a_sentence_claiming_more_tenure_than_the_resume_yields_no_fact(tmp_path: Path) -> None:
    body = ("I managed a $40,000 paid search budget for a florist in 2023 and grew orders by 20%. "
            "Over the course of almost 2 years my team and I generated 7 figures in revenue. "
            "After 6 months of testing I doubled the lead volume.")
    path = docx(tmp_path / "tenure.docx", [[("Stories 01 - Paid search for a florist", True), (body, False)]])
    document = st.read_stories(path)
    [story] = document.stories
    link = st.StoryRoleLink(story.story_id, "exp_florist", "Petal & Stem", "Marketing Manager", "2022-10", "2023-09",
                            False, "jev_match", 0.95, 0.98)
    index = st.build_story_index(document, verified_at=NOW, links={story.story_id: link})
    values = [str(f.value) for f in index.facts]
    assert not any("almost 2 years" in v for v in values)
    assert any("6 months of testing" in v for v in values) and any("$40,000" in v for v in values)
    assert [(entry["reason"], entry["story"]) for entry in index.skipped] == [("stated_duration_conflicts_with_resume_role", 1)]
    review = st.facts_review(index, candidate_id="default", today=date(2026, 9, 25))
    assert review["stories"][0]["duration_conflicts"][0]["phrase"] == "almost 2 years"
    assert "almost 2 years" in review["stories"][0]["note"] and "lasted 12 months" in review["stories"][0]["note"]
    assert "**check:**" in st.facts_review_markdown(review)
    unlinked = st.build_story_index(document, verified_at=NOW)
    assert any("almost 2 years" in str(f.value) for f in unlinked.facts) and not unlinked.skipped


# --- round 4: unverified facts, singular first person, adjacent years, confirmation ------------


def test_plural_sentences_stay_story_evidence_and_never_become_facts(tmp_path: Path) -> None:
    body = ("I ran paid search for a regional bakery chain in 2024. We grew ARR 3x and our team of 6 "
            "closed the biggest deal of the year. We managed the account together and reported weekly. "
            "I set up conversion tracking in Google Ads.")
    path = docx(tmp_path / "plural.docx", [[("Stories 01 - Growth", True), (body, False)]])
    index = st.build_story_index(path, verified_at=NOW)
    values = [str(fact.value) for fact in index.facts]
    assert any(v.startswith("I ran paid search") for v in values)
    assert any(v.startswith("I set up conversion tracking") for v in values)
    assert not any("ARR" in v or "biggest deal" in v or "together" in v for v in values)
    plural = [entry for entry in index.skipped if entry["reason"] == "plural_subject_only"]
    assert len(plural) == 2  # the ARR result and the managed-and-reported sentence
    assert all(entry["key"] is None and "ARR" not in json.dumps(entry) for entry in plural)
    assert any("We grew ARR 3x" in chunk.text for chunk in index.chunks)  # the chunks keep the team's work
    assert all(not fact.is_verified for fact in index.facts)
    receipt = st.story_index_receipt(index)
    assert "ARR" not in json.dumps(receipt) and receipt["skipped"] == list(index.skipped)


def test_an_unlinked_story_dates_facts_only_by_adjacent_or_explicit_years(tmp_path: Path) -> None:
    apart = ("I joined a regional agency in 2019 and planned the paid search budget. "
             "By 2023 I reported weekly results to the owner.")
    path = docx(tmp_path / "apart.docx", [[("Stories 01 - Years apart", True), (apart, False)]])
    story = st.parse_stories(st.read_docx(path))[0]
    assert st.stated_years(story) == ["2019", "2023"] and st.stated_year_span(story) is None
    assert st.resolve_period(story, None) == (None, "none", False)
    index = st.build_story_index(path, verified_at=NOW)
    assert index.facts and all("period_source: none" in fact.evidence for fact in index.facts)
    assert not any("2019\u20132023" in str(fact.value) for fact in index.facts)
    adjacent = ("I joined a regional agency in 2022 and planned the paid search budget. "
                "In 2023 I reported weekly results to the owner. In 2024 I planned the whole account.")
    story = st.parse_stories(st.read_docx(docx(tmp_path / "adjacent.docx",
                                              [[("Stories 01 - Adjacent", True), (adjacent, False)]])))[0]
    assert st.stated_year_span(story) == "2022\u20132024"
    explicit = "From 2019 to 2023 I planned the paid search budget of a regional agency and reported weekly."
    story = st.parse_stories(st.read_docx(docx(tmp_path / "explicit.docx",
                                              [[("Stories 01 - Explicit", True), (explicit, False)]])))[0]
    assert st.stated_year_span(story) == "2019\u20132023"
    assert st.resolve_period(story, None) == ("2019\u20132023", "story", False)
    receipt = st.story_index_receipt(index)
    assert receipt["stories"][0]["stated_year_count"] == 2 and "stated_years" not in receipt["stories"][0]


def test_confirmable_facts_take_the_import_shape_for_unverified_facts_only(stories_docx: Path) -> None:
    from interviewmaxxing_core import FactVerification, VerificationMethod, VerificationStatus

    index = st.build_story_index(stories_docx, verified_at=NOW)
    rows = st.confirmable_facts(index.facts)
    assert rows and all(set(row) == {"id", "key", "value", "evidence"} for row in rows)
    assert [row["id"] for row in rows] == [fact.id for fact in index.facts]
    confirmed = index.facts[0].model_copy(update={"verification": FactVerification(
        status=VerificationStatus.VERIFIED, method=VerificationMethod.USER_STATED, verified_at=NOW)})
    assert st.confirmable_facts([confirmed, *index.facts[1:]]) == rows[1:]
    review = st.facts_review(index, candidate_id="default")
    markdown = st.facts_review_markdown(review, [])
    assert "import-facts" in markdown and "| UNVERIFIED |" in markdown


# --- round 5: distinctive employer names, stated periods, period mismatches, Markdown sections ----

# The pair the live story index linked by a shared "Growth" (2026-09-25); the story text is fictional.
PAIR_TITLE = "Growth Marketing Specialist, RecruitHubSports"
PAIR_COMPANY = "Shop Growth Solutions"
RECRUITING = ("I ran paid social and email campaigns for a youth sports recruiting network. "
              "I grew free athlete sign-ups by 40% in one season and wrote the weekly report for the founders.")


def _story(tmp_path: Path, heading: str, body: str = RECRUITING, name: str = "story.md") -> tuple[st.Story, st.StoryAnalysis]:
    path = tmp_path / name
    path.write_text(f"## {heading}\n\n{body}\n", encoding="utf-8")
    document = st.read_stories(path)
    return document.stories[0], document.analyses[0]


def _shop(start: str = "2023-10", end: str | None = "2024-02", current: bool = False) -> st.ResumeRole:
    return st.ResumeRole("exp_shop", PAIR_COMPANY, "Marketing Manager", start, end, current,
                         ("Managed paid search for an online store.",))


def test_a_shared_common_word_never_links_a_story_to_a_resume_role(tmp_path: Path) -> None:
    story, analysis = _story(tmp_path, PAIR_TITLE)
    # "Shop", "Growth" and "Solutions" are all common words: only the full name names the company.
    assert st.company_tokens(PAIR_COMPANY) == set() and st.company_words(PAIR_COMPANY) == ["shop", "growth", "solutions"]
    assert st.match_role_by_name(story, analysis, [_shop()]) is None
    named, named_analysis = _story(tmp_path, "Paid search for an online store",
                                   "I ran paid search at Shop Growth Solutions for an online store and cut the cost per order by 12%.",
                                   name="named.md")
    link = st.match_role_by_name(named, named_analysis, [_shop()])
    assert link is not None and (link.resume_role_id, link.method) == ("exp_shop", "employer_name")
    # A single common word never names a company, whatever the company.
    for word in ("Growth", "Solutions", "Marketing", "Consulting", "Law", "Media", "Digital", "Group", "Inc"):
        role = st.ResumeRole("exp_x", f"{word} Partners LLC", "Manager", "2020-01", "2021-01", False, ())
        assert not st.names_company([f"I led {word} work for a regional agency."], role.company), word
    # A name with distinctive words needs all of them, or its full name ("Co." may be left out).
    assert st.company_tokens("Crumb & Co. Bakeries") == {"crumb", "bakeries"}
    assert not st.names_company(["I ran the Crumb account for a bakery."], "Crumb & Co. Bakeries")
    assert st.names_company(["I ran paid search for Crumb Bakeries in Ohio."], "Crumb & Co. Bakeries")
    assert st.names_company(["I joined Acme Growth in 2021."], "Acme Growth, Inc.")
    assert st.names_company(["Acme"], "Acme Growth, Inc.")  # the one distinctive word
    assert not st.names_company(["We practiced growth solutions at the shop."], PAIR_COMPANY)  # not consecutive


@pytest.mark.parametrize(("heading", "years", "label"), [
    (f"{PAIR_TITLE} (Aug 2019 - May 2020)", ["2019", "2020"], "2019-08 to 2020-05"),
    ("Paid Media Lead, Lumen Legal | Jun 2025 - Present", ["2025"], "2025-06 to present"),
    ("SEO Manager, Glaze Agency, Mar 2024 - May 2025", ["2024", "2025"], "2024-03 to 2025-05"),
    ("Analyst, Orbit Foods (08/2019 \u2013 05/2020)", ["2019", "2020"], "2019-08 to 2020-05"),
    ("Coordinator, Orbit Foods, 2017 to 2019", ["2017", "2019"], "2017\u20132019"),
])
def test_a_heading_states_the_story_period(tmp_path: Path, heading: str, years: list[str], label: str) -> None:
    story, analysis = _story(tmp_path, heading)
    assert st.stated_years(story) == years  # the body states no year; the heading does
    period = st.story_period(story)
    assert period is not None and (period.label, period.source) == (label, "heading")
    assert analysis.period == label
    assert st.resolve_period(story, None) == (label, "story", False)
    index = st.build_story_index(st.read_stories(tmp_path / "story.md"), verified_at=NOW)
    assert all(f" | period: {label} | " in chunk.text.split("\n")[0] for chunk in index.chunks)
    assert index.facts and all(str(fact.value).endswith(f", {label})") and "period_source: story" in fact.evidence
                               for fact in index.facts)
    review = st.facts_review(index, candidate_id="default")
    assert review["stories"][0]["stated_period"] == {"label": label, "start": period.start, "end": period.end, "in": "heading"}
    markdown = st.facts_review_markdown(review)
    assert f"years the story states: {', '.join(years)}" in markdown
    assert f"- period the story states: {label} (in its heading)" in markdown
    assert st.story_index_receipt(index)["stories"][0]["stated_period_in"] == "heading"


def test_month_ranges_are_periods_and_the_heading_comes_first() -> None:
    def story(title: str, body: str) -> st.Story:
        return st.Story(1, title, tuple(st.split_sentences(body)))

    body = "From Aug 2019 to May 2020 I ran paid social for a sports network. In 2020 I grew sign-ups by 40%."
    [period] = st.stated_periods(story("Paid social", body))
    assert (period.start, period.end, period.source, period.label) == ("2019-08", "2020-05", "body", "2019-08 to 2020-05")
    # A body range dates an unlinked story; only a heading states the story's own period.
    assert st.story_period(story("Paid social", body)) is None and st.dating_period(story("Paid social", body)) == period
    both = st.stated_periods(story("Lead, Orbit Foods (Jan 2018 - Dec 2018)", body))
    assert [p.source for p in both] == ["heading", "body"] and st.story_period(story("Lead, Orbit Foods (Jan 2018 - Dec 2018)", body)).label == "2018-01 to 2018-12"  # type: ignore[union-attr]
    # Two different ranges in the body date parts of the story, not the whole story.
    two = story("Paid social", "From Jan 2020 to Mar 2020 I ran a pilot. From Jun 2021 to Dec 2021 I ran the program.")
    assert len(st.stated_periods(two)) == 2 and st.story_period(two) is None and st.dating_period(two) is None
    assert st.stated_periods(story("x", "In 2019-08 to 2020-05 I ran it; Sept. 2021 \u2014 present I advise.")) == [
        st.StatedPeriod("2019-08", "2020-05", "body"), st.StatedPeriod("2021-09", None, "body")]
    # Not periods: an end before its start, a year list, a single year, a year inside a word or a longer number.
    for text in ("May 2020 - Aug 2019 I ran it.", "In 2019 and 2023 I ran it.", "In 2021 I ran it.",
                 "A 2019-era budget, ticket 12019-2020."):
        assert st.stated_periods(story("x", text)) == [], text
    open_role = st.StoryRoleLink("s", "exp", "Orbit Foods", "Lead", "2025-07", None, True, "jev_match")
    assert st.period_overlaps_role(st.StatedPeriod("2025-06", None, "heading"), open_role, date(2026, 9, 25)) is True
    assert st.period_overlaps_role(st.StatedPeriod("2019-08", "2020-05", "heading"), open_role, date(2026, 9, 25)) is False
    assert st.period_overlaps_role(st.StatedPeriod("2019", "2019", "heading"),
                                   replace(open_role, start="2019-12", end="2020-06", current=False), date(2026, 9, 25)) is True
    assert st.period_overlaps_role(st.StatedPeriod("2019-08", "2020-05", "heading"),
                                   replace(open_role, start=None), date(2026, 9, 25)) is None


def test_a_link_the_stated_period_contradicts_is_not_used_and_is_reported(tmp_path: Path) -> None:
    story, _analysis = _story(tmp_path, f"{PAIR_TITLE} (Aug 2019 - May 2020)")
    document = st.read_stories(tmp_path / "story.md")
    link = st.StoryRoleLink(story.story_id, "exp_shop", PAIR_COMPANY, "Marketing Manager", "2023-10", "2024-02",
                            False, "employer_name")
    index = st.build_story_index(document, verified_at=NOW, links={story.story_id: link})
    assert index.link_for(story.story_id) is None
    mismatch = index.mismatch_for(story.story_id)
    assert mismatch is not None and mismatch["stated_period"] == "2019-08 to 2020-05" and mismatch["role_period"] == "2023-10 to 2024-02"
    assert (mismatch["reason"], mismatch["stated_in"], mismatch["method"]) == ("stated_period_outside_resume_role", "heading", "employer_name")
    # The story's own period, never the role's: in every chunk header and fact.
    for chunk in index.chunks:
        header = chunk.text.split("\n")[0]
        assert " | period: 2019-08 to 2020-05 | " in header and "2023-10" not in chunk.text and "resume role:" not in header
        assert chunk.resume_role_id is None and chunk.period == "2019-08 to 2020-05"
    assert index.facts and all("2023-10" not in str(f.value) and "resume_role_id: exp_shop" not in f.evidence
                               and "period_source: story" in f.evidence for f in index.facts)
    receipt = st.story_index_receipt(index)
    row = receipt["stories"][0]
    assert row["link"] is None and row["link_rejected"] == {"method": "employer_name", "resume_role_id": "exp_shop",
                                                           "reason": "stated_period_outside_resume_role"}
    assert receipt["link_period_mismatches"] == 1 and receipt["linked_stories"] == 0
    assert PAIR_COMPANY not in json.dumps(receipt) and "2019-08" not in json.dumps(receipt)
    review = st.facts_review(index, candidate_id="default")
    assert review["stories"][0]["link_rejected"]["company"] == PAIR_COMPANY and review["stories"][0]["resume_role"] is None
    assert "does not overlap the resume role" in review["stories"][0]["note"]
    markdown = st.facts_review_markdown(review)
    assert ("- not linked: Marketing Manager at Shop Growth Solutions, 2023-10 to 2024-02 (proposed by employer_name) "
            "does not overlap 2019-08 to 2020-05") in markdown and "**check:**" in markdown
    # An overlapping role keeps the link and the resume dates, as before.
    overlapping = replace(link, start="2019-06", end="2020-12", method="jev_match")
    kept = st.build_story_index(document, verified_at=NOW, links={story.story_id: overlapping})
    assert kept.link_for(story.story_id) == overlapping and kept.mismatch_for(story.story_id) is None
    assert all(" | period: 2019-06 to 2020-12 | " in c.text.split("\n")[0] for c in kept.chunks)
    # An open period against a current role overlaps; against an ended one it does not.
    present, _ = _story(tmp_path, "Paid Media Lead, Lumen Legal | Jun 2025 - Present", name="present.md")
    current = st.StoryRoleLink(present.story_id, "exp_now", "Lumen Legal", "Paid Media Lead", "2025-07", None, True, "jev_match")
    present_doc = st.read_stories(tmp_path / "present.md")
    assert st.build_story_index(present_doc, verified_at=NOW, links={present.story_id: current}).link_for(present.story_id) == current
    ended = replace(current, start="2023-10", end="2024-02", current=False)
    assert st.build_story_index(present_doc, verified_at=NOW, links={present.story_id: ended}).mismatch_for(present.story_id)


def test_a_single_stated_year_outside_the_role_is_still_only_a_flag(stories_docx: Path) -> None:
    """A year the story mentions once ("in 2024") is not a period: the resume dates win and
    the sentence yields no fact (round 2); only a stated range can reject a link."""
    document = st.read_stories(stories_docx)
    bakery = document.stories[0]
    assert st.story_period(bakery) is None and st.stated_years(bakery) == ["2024"]
    link = st.StoryRoleLink(bakery.story_id, "exp_old", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12",
                            False, "jev_match", 0.91, 0.96)
    index = st.build_story_index(document, verified_at=NOW, links={bakery.story_id: link})
    assert index.link_for(bakery.story_id) == link and index.mismatch_for(bakery.story_id) is None
    assert st.story_index_receipt(index)["stated_year_discrepancies"] == 1


MD_ROLES = ("I managed paid search for a regional credit union and cut the cost per lead by 18%. "
            "I rebuilt the conversion tracking in Google Ads.")


@pytest.mark.parametrize("layout", ["numbered_h1", "title_h1", "intro_h1", "long_h2", "colon_h2", "setext"])
def test_markdown_splits_at_h1_and_h2(tmp_path: Path, layout: str) -> None:
    first = f"{PAIR_TITLE} (Aug 2019 - May 2020)"
    second = "SEO Manager, Glaze Agency (Mar 2024 - May 2025)"
    if layout == "long_h2":
        first += " | Remote | growth marketing for a youth sports recruiting network with paid social, email and events"
        assert len(first) > 120
    if layout == "colon_h2":
        second = "SEO Manager at Glaze Agency, Mar 2024 - May 2025:"
    top = {"numbered_h1": "# Stories 05 - Work history", "title_h1": "# Work history", "intro_h1": "# Work history",
           "long_h2": "# Work history", "colon_h2": "# Work history", "setext": "Work history\n============"}[layout]
    intro = "\n\nMy roles in order, oldest first.\n" if layout == "intro_h1" else "\n"
    if layout == "setext":
        text = f"{top}\n\n{first}\n---\n\n{RECRUITING}\n\n{second}\n---\n\n{MD_ROLES}\n"
    else:
        text = f"{top}\n{intro}\n## {first}\n\n{RECRUITING}\n\n## {second}\n\n{MD_ROLES}\n"
    path = tmp_path / "work.md"
    path.write_text(text, encoding="utf-8")
    stories = st.read_stories(path).stories
    titles = [s.title for s in stories]
    expected = [first.replace("|", "/"), second]  # a title's bars become slashes, like its chunk header
    if layout == "intro_h1":
        assert titles == ["Work history", *expected] and stories[0].sentences == ("My roles in order, oldest first.",)
    else:
        assert titles == expected  # the document title over the H2 sections is not a story
    assert [s.number for s in stories] == list(range(1, len(stories) + 1))
    recruiting = next(s for s in stories if s.title == expected[0])
    assert recruiting.body == RECRUITING and st.story_period(recruiting).label == "2019-08 to 2020-05"  # type: ignore[union-attr]


def test_markdown_boundaries_keep_numbers_hashtags_and_subsections(tmp_path: Path) -> None:
    path = tmp_path / "numbered.md"
    path.write_text("## Stories 04 - SEO at Linkforge\n\n" + SEO_STORY + "\n\n### Results\n\nI kept every client.\n\n"
                    "#seo #growth\n\n## Stories 07 - Paid search at Glaze\n\n" + MD_ROLES + "\n", encoding="utf-8")
    first, second = st.read_stories(path).stories
    assert (first.number, first.title, second.number, second.title) == (4, "SEO at Linkforge", 7, "Paid search at Glaze")
    # An H3 in a numbered document stays in its story; a "#hashtag" line is text, not a heading.
    assert "Results" in first.sentences and "#seo #growth" in first.sentences and first.sentences[-1] == "#seo #growth"
    trailing = tmp_path / "trailing.md"
    trailing.write_text("## First role\n\n" + MD_ROLES + "\n\n## A heading with nothing under it\n", encoding="utf-8")
    with pytest.raises(st.StoryParseError, match="no story text"):
        st.read_stories(trailing)
    huge = tmp_path / "huge.md"
    huge.write_text("## " + "word " * 100 + "\n\n" + MD_ROLES + "\n", encoding="utf-8")
    with pytest.raises(st.StoryParseError, match="length bound"):
        st.read_stories(huge)
    emphasis = tmp_path / "emphasis.md"
    emphasis.write_text("# **Work history** #\n\n## _Paid search at Glaze_\n\n" + MD_ROLES + "\n", encoding="utf-8")
    [story] = st.read_stories(emphasis).stories
    assert story.title == "Paid search at Glaze"


def test_a_docx_title_over_heading_stories_is_a_section_title(tmp_path: Path) -> None:
    path = docx(tmp_path / "titled.docx", [
        [("Work history", False)], [("Paid search for a bakery", False)], [(BAKERY, False)],
        [("Ovenboard", False)], [(OVENBOARD, False)],
    ], heading_styles={0: "Title", 1: "Heading1", 3: "Heading1"})
    stories = st.parse_stories(st.read_docx(path))
    assert [(s.number, s.title) for s in stories] == [(1, "Paid search for a bakery"), (2, "Ovenboard")]
    # A bold run heading with no text under it is still an error, as before.
    bare = docx(tmp_path / "bare.docx", [[("Stories 01 - Empty", True)], [("Stories 02 - Full", True), (BAKERY, False)]])
    with pytest.raises(st.StoryParseError, match="no story text"):
        st.parse_stories(st.read_docx(bare))


def test_a_heading_with_bars_keeps_one_title_and_story_id_across_chunks_and_facts(tmp_path: Path) -> None:
    story, _analysis = _story(tmp_path, "Paid Media Lead | Lumen Legal | Jun 2025 - Present")
    assert story.title == "Paid Media Lead / Lumen Legal / Jun 2025 - Present"
    assert st.story_period(story).label == "2025-06 to present"  # type: ignore[union-attr]
    index = st.build_story_index(st.read_stories(tmp_path / "story.md"), verified_at=NOW)
    for chunk in index.chunks:
        header = st.parse_chunk_header(chunk.text)
        assert header is not None and header["title"] == story.title and header["story_id"] == story.story_id
    assert all(fact.evidence[0] == f"Story 01: {story.title}" for fact in index.facts)


def test_a_range_in_the_body_dates_an_unlinked_story_but_never_rejects_a_link(tmp_path: Path) -> None:
    """A range in the body may date only part of the work (a pilot before the role):
    the heading states the story's own period, and only it can reject a link."""
    body = ("From Jan 2020 to Mar 2020 I ran a paid social pilot for a sports recruiting network. "
            "I then managed the network's paid social budget and grew sign-ups by 40%.")
    story, _analysis = _story(tmp_path, "Paid social for a recruiting network", body)
    document = st.read_stories(tmp_path / "story.md")
    unlinked = st.build_story_index(document, verified_at=NOW)
    assert all(" | period: 2020-01 to 2020-03 | " in c.text.split("\n")[0] for c in unlinked.chunks)
    link = st.StoryRoleLink(story.story_id, "exp_net", "Northfield Athletics", "Paid Social Manager", "2021-01",
                            "2022-05", False, "jev_match", 0.95, 0.97)
    linked = st.build_story_index(document, verified_at=NOW, links={story.story_id: link})
    assert linked.link_for(story.story_id) == link and linked.mismatch_for(story.story_id) is None
    assert all(" | period: 2021-01 to 2022-05 | " in c.text.split("\n")[0] for c in linked.chunks)
    # The year outside the role is the round-2 flag: its sentence yields no fact.
    assert st.story_index_receipt(linked)["stated_year_discrepancies"] == 1
    assert [entry["reason"] for entry in linked.skipped] == ["stated_year_conflicts_with_resume_role"]


@pytest.mark.parametrize(("heading", "label"), [
    (f"{PAIR_TITLE} (2019)", "2019"),
    (f"{PAIR_TITLE}, 2019 - 2020", "2019\u20132020"),
    (f"{PAIR_TITLE}, 2019 and 2020", "2019\u20132020"),
    (f"{PAIR_TITLE}, 2017 and 2020", None),  # years apart date nothing (round 4, L8)
    (PAIR_TITLE, None),
])
def test_a_heading_year_is_the_story_period_and_can_reject_a_link(tmp_path: Path, heading: str, label: str | None) -> None:
    story, _analysis = _story(tmp_path, heading)
    period = st.story_period(story)
    assert (period.label if period else None) == label
    link = st.StoryRoleLink(story.story_id, "exp_shop", PAIR_COMPANY, "Marketing Manager", "2023-10", "2024-02",
                            False, "jev_match", 0.95, 0.97)
    index = st.build_story_index(st.read_stories(tmp_path / "story.md"), verified_at=NOW, links={story.story_id: link})
    if label is None:
        assert index.link_for(story.story_id) == link  # nothing stated: the resume dates stand
    else:
        assert index.link_for(story.story_id) is None and index.mismatch_for(story.story_id)["stated_period"] == label  # type: ignore[index]
        assert all(f" | period: {label} | " in c.text.split("\n")[0] for c in index.chunks)
    # A year in the body is never a period by itself.
    body_only, _ = _story(tmp_path, PAIR_TITLE, RECRUITING + " In 2019 I also ran the email list.", name="body.md")
    assert st.story_period(body_only) is None and st.stated_years(body_only) == ["2019"]


# --- round 5, addendum item 8: confirmed facts that contradict their own evidence -------------------


def _confirmed(fact: object) -> object:
    from interviewmaxxing_core import FactVerification, VerificationMethod, VerificationStatus

    return fact.model_copy(update={"source": "user:confirmed fact import", "verification": FactVerification(  # type: ignore[attr-defined]
        status=VerificationStatus.VERIFIED, method=VerificationMethod.USER_STATED, verified_at=NOW)})


def test_a_fact_whose_evidence_dates_or_places_it_elsewhere_contradicts_itself(tmp_path: Path) -> None:
    from interviewmaxxing_core import CandidateFact, FactVerification, VerificationStatus

    roles = [st.ResumeRole("exp_harbor", "Harbor Growth Solutions", "Marketing Manager", "2023-10", "2024-02", False, ()),
             st.ResumeRole("exp_links", "Linkforge", "SEO Project Manager", "2025-07", "2026-05", False, ())]

    def fact(value: str, evidence: list[str]) -> CandidateFact:
        return CandidateFact(id="sf_0123456789abcdef_000000000001", key="achievement", value=value, source="story:" + "1" * 64,
                             verification=FactVerification(status=VerificationStatus.UNVERIFIED), evidence=evidence)

    # The live shape: a confirm file written while the story was linked by a shared word.
    stale = fact("I grew free athlete sign-ups by 40% in one season (youth sports recruiting network; resume: "
                 "Harbor Growth Solutions, 2023-10 to 2024-02)",
                 ["Story 05: Growth Marketing Specialist, Tidewater Recruiting (Aug 2019 - May 2020)",
                  "period_source: resume_role", "story_source: candidate-stories-profile", "resume_role_id: exp_harbor"])
    assert st.fact_self_contradictions(stale, roles, today=date(2026, 9, 25)) == ["evidence_period_contradicts_fact_period"]
    # The same fact dated by its own story: nothing to object to.
    fixed = fact("I grew free athlete sign-ups by 40% in one season (youth sports recruiting network, 2019-08 to 2020-05)",
                 ["Story 05: Growth Marketing Specialist, Tidewater Recruiting (Aug 2019 - May 2020)",
                  "period_source: story", "story_source: candidate-stories-profile"])
    assert st.fact_self_contradictions(fixed, roles, today=date(2026, 9, 25)) == []
    # Evidence that names another employer: the linked role, or a resume company the heading names.
    linked_elsewhere = fact("I ran link-building audits (resume: Harbor Growth Solutions, 2023-10 to 2024-02)",
                            ["Story 02: Audits", "period_source: resume_role", "resume_role_id: exp_links"])
    assert st.fact_self_contradictions(linked_elsewhere, roles) == ["evidence_employer_contradicts_fact_employer"]
    heading_elsewhere = fact("I ran link-building audits (resume: Harbor Growth Solutions, 2023-10 to 2024-02)",
                             ["Story 02: SEO audits at Linkforge", "period_source: resume_role"])
    assert st.fact_self_contradictions(heading_elsewhere, roles) == ["evidence_employer_contradicts_fact_employer"]
    # A resume fact or a user statement without dates or links has nothing to contradict.
    plain = fact("Managed a $40,000 monthly paid search budget.", ["Resume bullet"])
    assert st.fact_self_contradictions(plain, roles) == []


def test_confirmed_facts_of_a_story_whose_link_changed_are_superseded(tmp_path: Path) -> None:
    from interviewmaxxing_core import CandidateProfile

    story, _analysis = _story(tmp_path, f"{PAIR_TITLE} (Aug 2019 - May 2020)")
    document = st.read_stories(tmp_path / "story.md")
    wrong = st.StoryRoleLink(story.story_id, "exp_shop", PAIR_COMPANY, "Marketing Manager", "2023-10", "2024-02",
                             False, "employer_name")
    before = st.build_story_index(document, verified_at=NOW, links={story.story_id: wrong}, source_id="profile-src")
    # Facts confirmed while the story still carried the wrong link record it in their evidence.
    confirmed = [_confirmed(f.model_copy(update={"evidence": [*f.evidence, "resume_role_id: exp_shop"]}))
                 for f in before.facts]
    unconfirmed = before.facts[0].model_copy(update={"id": before.facts[0].id[:-1] + "f"})
    gone = _confirmed(before.facts[0].model_copy(update={"id": "sf_ffffffffffffffff_000000000001"}))
    other_source = _confirmed(before.facts[0].model_copy(update={
        "id": before.facts[0].id[:-1] + "e", "evidence": [line.replace("profile-src", "docx") for line in before.facts[0].evidence]}))
    fixture = Path(__file__).parents[1] / "fixtures" / "core" / "candidate_profile.json"
    profile = CandidateProfile.model_validate(json.loads(fixture.read_text()))
    profile = profile.model_copy(update={"facts": [*profile.facts, *confirmed, unconfirmed, gone, other_source]})
    now_index = st.build_story_index(document, verified_at=NOW, links={story.story_id: wrong}, source_id="profile-src")
    assert now_index.link_for(story.story_id) is None  # the heading period rejects the wrong link now
    rows = st.superseded_story_facts(profile, now_index)
    assert rows == [*({"id": f.id, "superseded": True, "reason": "story_link_changed"} for f in confirmed),
                    {"id": gone.id, "superseded": True, "reason": "story_no_longer_in_document"}]
    review = st.facts_review(now_index, candidate_id="default")
    review["superseded"] = rows
    markdown = st.facts_review_markdown(review)
    assert "## Superseded confirmed facts" in markdown and "remove-facts --ids " + confirmed[0].id in markdown  # type: ignore[attr-defined]
