"""Breadcrumb trail view models for pages below the main fleet view.

Framework-free on purpose: templates consume only these frozen dataclasses,
so the follow-up pages (attempt log, progress) reuse the same mechanism by
building their own trail and handing it to the template.
"""

from __future__ import annotations

from dataclasses import dataclass


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
