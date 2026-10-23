"""Breadcrumb trail view models for pages below the main fleet view.

Framework-free on purpose: templates consume only these frozen dataclasses,
so the follow-up pages (attempt log, progress) reuse the same mechanism by
building their own trail and handing it to the template.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class Breadcrumb:
    """One crumb: display label plus an optional URL.

    A crumb without a URL is the current page and renders as plain text
    (with ``aria-current="page"``); a crumb with a URL renders as a link.
    """

    label: str
    url: str | None = None


def history_trail(instance: str) -> tuple[Breadcrumb, ...]:
    """Breadcrumb trail for the Attempt history page: ``Fleet › <instance>``."""
    return (Breadcrumb(label="Fleet", url="/"), Breadcrumb(label=instance))


def _history_url(instance: str) -> str:
    """Link target of the instance crumb: the instance's Attempt history."""
    return f"/instances/{quote(instance, safe='')}/history"


def attempt_trail(
    instance: str, issue: int | str, started_at: str
) -> tuple[Breadcrumb, ...]:
    """Breadcrumb trail for the Attempt log page.

    ``Fleet › <instance> › #<issue> · <started>``: Fleet links home, the
    instance links to its Attempt history, and the attempt is plain
    current-page text.
    """
    return (
        Breadcrumb(label="Fleet", url="/"),
        Breadcrumb(label=instance, url=_history_url(instance)),
        Breadcrumb(label=f"#{issue} · {started_at}"),
    )


def attempt_progress_trail(
    instance: str, issue: int | str, started_at: str, attempt_url: str
) -> tuple[Breadcrumb, ...]:
    """Breadcrumb trail for the Attempt progress page.

    ``Fleet › <instance> › #<issue> · <started> › Progress``: like the
    Attempt log trail with the attempt linking to its log and Progress as
    plain current-page text.
    """
    return (
        Breadcrumb(label="Fleet", url="/"),
        Breadcrumb(label=instance, url=_history_url(instance)),
        Breadcrumb(label=f"#{issue} · {started_at}", url=attempt_url),
        Breadcrumb(label="Progress"),
    )
