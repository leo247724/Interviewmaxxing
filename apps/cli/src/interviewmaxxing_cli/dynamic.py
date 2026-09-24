"""Explicit runtime configuration and observation-only browser entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from interviewmaxxing_browser.annotations import FormAnnotator
from interviewmaxxing_browser.opencli import OpenCliConfig, OpenCliSessionFactory
from interviewmaxxing_browser.schema_catalog import load_schema_hint
from interviewmaxxing_browser.session import PlaywrightSessionFactory
from interviewmaxxing_core import BrowserOptions, PacketResolver


@dataclass(frozen=True)
class DynamicOptions:
    browser: Literal["playwright", "opencli"] = "playwright"
    opencli_profile: str | None = None
    ai_routing: bool = False
    env_file: Path | None = None
    writer_model: str | None = None
    rag_connection_file: Path | None = None

    def validate(self) -> None:
        if self.rag_connection_file is not None and (
            not self.ai_routing or not self.rag_connection_file.is_absolute()
        ):
            raise ValueError("--rag-connection-file requires --ai-routing and an absolute path")
        if self.opencli_profile and self.browser != "opencli":
            raise ValueError("--opencli-profile requires --browser opencli")
        if self.ai_routing and (self.env_file is None or not self.writer_model):
            raise ValueError("--ai-routing requires --env-file and --writer-model")
        if not self.ai_routing and (self.env_file is not None or self.writer_model):
            raise ValueError("--env-file and --writer-model require --ai-routing")
        if self.ai_routing and self.writer_model != "anthropic/claude-opus-5.5":
            raise ValueError("--writer-model must explicitly select anthropic/claude-opus-5.5")


def runtime_components(options: DynamicOptions) -> tuple[PlaywrightSessionFactory | OpenCliSessionFactory, PacketResolver | None]:
    options.validate()
    annotator: FormAnnotator | None = None
    resolver: PacketResolver | None = None
    if options.ai_routing:
        from interviewmaxxing_browser.ai import build_ai_runtime
        from interviewmaxxing_selection.credentials import CredentialError

        assert options.env_file is not None and options.writer_model is not None
        try:
            if options.rag_connection_file is not None:
                annotator, resolver = build_ai_runtime(env_file=options.env_file,
                    writer_model=options.writer_model, rag_connection_file=options.rag_connection_file)
            else:
                annotator, resolver = build_ai_runtime(env_file=options.env_file,
                    writer_model=options.writer_model)
        except CredentialError as exc:
            raise ValueError(f"AI credentials unavailable: {exc}") from None
    if options.browser == "opencli":
        return OpenCliSessionFactory(OpenCliConfig(profile=options.opencli_profile),
                                     annotator=annotator, schema_hint_loader=load_schema_hint), resolver
    return PlaywrightSessionFactory(annotator=annotator,
                                     schema_hint_loader=load_schema_hint), resolver


async def classify_url(url: str, *, options: DynamicOptions,
                       artifacts_dir: Path, headless: bool = False) -> dict[str, Any]:
    """No candidate read, packet resolution, field fill, action or status-store write."""
    factory, _ = runtime_components(options)
    browser = await factory.start(BrowserOptions(
        artifacts_dir=artifacts_dir, artifacts_root=artifacts_dir.parent,
        headless=headless, allow_submission=False,
    ))
    try:
        # Concrete factories expose observe; ApplicationBrowser.open may follow an
        # Apply link/button and is deliberately not the classification entrypoint.
        page = await browser.observe(url)
        result: dict[str, Any] = {
            "mode": "observation_only", "submitted": False,
            "inspection": page.model_dump(mode="json"),
        }
        annotator = getattr(browser, "annotator", None)
        if annotator is not None:
            result["provider_metadata"] = annotator.decisions.budget.metadata()
            if page.form is not None:
                report = annotator.report_for(page.form)
                if report is not None:
                    result["routing"] = report.model_dump(mode="json")
        return result
    finally:
        await browser.close()
