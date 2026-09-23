"""Loopback-only HTTP presentation service for the Interviewmaxxing frontend.

Maps canonical store state to the frontend's view models and hands all execution to
the I1 runner. See ``apps/service/README.md``.
"""

from .candidate import CandidateGateway, CandidateSetupError, ResumeEntry
from .config import ConfigError, ServiceConfig
from .errors import ApiError
from .executor import ApplicationExecutor, Dispatcher, ExecutorBusy, ServiceInteraction
from .server import FILENAME_HEADER, make_server
from .service import PresentationService

__all__ = [
    "FILENAME_HEADER",
    "ApiError",
    "ApplicationExecutor",
    "CandidateGateway",
    "CandidateSetupError",
    "ConfigError",
    "Dispatcher",
    "ExecutorBusy",
    "PresentationService",
    "ResumeEntry",
    "ServiceConfig",
    "ServiceInteraction",
    "make_server",
]
