"""Validate an application handoff and link its canonical record before dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from interviewmaxxing_core import ApplicationStore, JobListing, normalize_application_url
from interviewmaxxing_pipeline import ItemNotFound, PipelineItem, PipelineUpdate, RevisionConflict

from . import errors
from .models import StartApplicationInput
from .pipeline_api import PipelineApi
from .views import SAFE_ID


@dataclass(frozen=True)
class ApplicationLink:
    entry_id: str | None
    listing_id: str | None
    url: str


class ApplicationLinks:
    def __init__(self, pipeline: PipelineApi, *,
                 get_listing: Callable[[str], JobListing | None],
                 track_listing: Callable[[str], str]) -> None:
        self.pipeline = pipeline
        self.get_listing = get_listing
        self.track_listing = track_listing

    def _listing(self, listing_id: str) -> JobListing:
        listing = self.get_listing(listing_id) if SAFE_ID.fullmatch(listing_id) else None
        if listing is None:
            raise errors.invalid("That listing isn't saved.", {"listingId": "Unknown listing."})
        return listing

    def _entry(self, entry_id: str) -> PipelineItem:
        if not SAFE_ID.fullmatch(entry_id):
            raise errors.invalid("That pipeline entry isn't saved.",
                                 {"pipelineEntryId": "Unknown pipeline entry."})
        with self.pipeline.store() as store:
            try:
                return store.get_item(self.pipeline.candidate_id, entry_id)
            except ItemNotFound as exc:
                raise errors.invalid("That pipeline entry isn't saved.",
                                     {"pipelineEntryId": "Unknown pipeline entry."}) from exc

    @staticmethod
    def _urls(listing: JobListing) -> set[str]:
        urls = [listing.application_url, listing.posting_url]
        urls.extend(url for source in listing.provenance
                    for url in (source.application_url, source.posting_url))
        return {normalize_application_url(url) for url in urls if url}

    def _validate(self, link: ApplicationLink, apps: ApplicationStore) -> PipelineItem | None:
        item = self._entry(link.entry_id) if link.entry_id is not None else None
        listing = self._listing(link.listing_id) if link.listing_id is not None else None
        item_listing = self._listing(item.listing_id) if item and item.listing_id else None
        if listing is not None and item_listing is not None and listing.id != item_listing.id:
            raise errors.invalid("The listing and pipeline entry refer to different jobs.",
                                 {"listingId": "Choose the listing linked to this entry."})
        listing = listing or item_listing
        known = self._urls(listing) if listing else set()
        item_url = normalize_application_url(item.application_url) \
            if item and item.application_url else None
        if item and link.listing_id and item_listing is None and (not item_url or item_url not in known):
            raise errors.invalid("The listing cannot be matched to this pipeline entry.",
                                 {"listingId": "This listing isn't linked to this entry."})
        requested = normalize_application_url(link.url)
        if known and requested not in known:
            raise errors.invalid("The application URL doesn't match this saved job.",
                                 {"applicationUrl": "Use this job's saved posting or application URL."})
        if item_url and requested != item_url and not (item_url in known and requested in known):
            raise errors.invalid("The application URL doesn't match this pipeline entry.",
                                 {"applicationUrl": "Use this entry's saved application URL."})
        if item and item.application_id:
            existing = apps.find_application(self.pipeline.candidate_id, link.url)
            if existing is None or existing.id != item.application_id:
                raise errors.conflict("This entry already links to another application.",
                                      {"pipelineEntryId": "The existing application link was kept."})
        return item

    def prepare(self, body: StartApplicationInput, apps: ApplicationStore) -> ApplicationLink | None:
        if body.pipeline_entry_id is None and body.listing_id is None:
            return None
        link = ApplicationLink(body.pipeline_entry_id, body.listing_id, body.application_url.strip())
        self._validate(link, apps)
        if link.entry_id is None and link.listing_id is not None:
            listing = self._listing(link.listing_id)
            item = self.pipeline.item_for_listing(listing.id)
            if item is not None:
                link = ApplicationLink(item.id, listing.id, link.url)
                self._validate(link, apps)
        return link

    def bind(self, link: ApplicationLink, application_id: str, apps: ApplicationStore) -> None:
        if link.entry_id is None:
            assert link.listing_id is not None
            link = ApplicationLink(self.track_listing(link.listing_id), link.listing_id, link.url)
        item = self._validate(link, apps)
        assert item is not None
        if item.application_id == application_id:
            return
        with self.pipeline.store() as store:
            try:
                store.update_item(self.pipeline.candidate_id, item.id,
                                  PipelineUpdate(application_id=application_id),
                                  expected_revision=item.revision)
            except (ItemNotFound, RevisionConflict) as exc:
                raise errors.conflict(
                    "The entry changed before it could be linked. Nothing was started; retry the handoff.",
                    {"pipelineEntryId": "Reload the entry and try again."},
                ) from exc
