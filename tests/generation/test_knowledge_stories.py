"""Fictional stories: docx parsing, deterministic analysis, bounded chunks and facts
that never invent a number. No database, provider or real document is involved."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import UTC, datetime
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
    assert index.facts and not index.skipped
    for fact in index.facts:
        story = next(s for s in index.stories if fact.id.startswith(f"sf_{s.story_id}_"))
        assert st.numbers_in(str(fact.value)) <= st.numbers_in(story.body), fact.id
        assert fact.key in {"employment", "achievement", "experience", "skills", "project", "education"}
        assert fact.source in chunk_ids and fact.source.startswith("story:")
        assert fact.is_verified and fact.verification.method is not None
        assert fact.verification.method.value == "USER_STATED" and fact.verification.verified_at == NOW
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
    assert len(review["facts"]) == len(index.facts) and review["facts"][0]["verification"]["status"] == "VERIFIED"
    assert review["stories"][0]["employer"] == "regional bakery chain"
