"""Offline performance harness for Interviewmaxxing (task P0).

Standard library only. Nothing here opens a browser, calls a provider, or reads the
private profile. ``imx_perf.measure`` optionally imports the real packages when they
are importable (for example from a sibling worktree's task venv) to time local
operations on temporary databases with fictional data.

Every reported number is labelled ``observed``, ``modeled`` or ``external``; see
``docs/performance/architecture.md`` §0 for the definitions.
"""

__version__ = "0.1.0"
