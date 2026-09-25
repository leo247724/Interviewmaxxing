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
    assert plural and all(entry["key"] is None and "ARR" not in json.dumps(entry) for entry in plural)
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
