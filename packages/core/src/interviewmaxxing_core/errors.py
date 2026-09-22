"""Errors raised by the application store."""

from __future__ import annotations


class StoreError(Exception):
    """Base class for store errors."""


class NotFound(StoreError, LookupError):
    pass


class InvalidTransition(StoreError):
    """The state machine does not allow this change (or it needs a dedicated operation)."""


class ClaimUnavailable(StoreError):
    """Another owner holds an unexpired claim on the application."""


class ClaimLost(StoreError):
    """The supplied claim is expired, released or superseded."""


class SubmissionBlocked(StoreError):
    """A submission cannot start: one may already have reached the site."""


class IdentityConflict(StoreError):
    """An identity binding would contradict existing records (e.g. two submitted
    applications, or one job page showing two different ATS identities)."""
