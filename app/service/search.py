"""
Holds an ordered list of enabled source clients and resolves a single
ratingKey's full metadata (or children) by trying each client in turn.

app/services.py builds one SearchService per source category (for
type-scoped matching, see app/service/match.py) plus one combined
SearchService over all enabled categories (for ratingKey lookups, which
carry no type information - see app/service/metadata.py).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from app.client.base import SourceClient, SourceMetadata, SourceUnavailableError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SearchService:
    def __init__(self, clients: list[SourceClient]):
        #: Enabled source clients, in priority order.
        self.clients = clients

    def get_entry(self, rating_key: str) -> SourceMetadata | None:
        """Full metadata for a single ratingKey, from the first client that
        has it."""
        return self._first_hit(
            f"get_entry({rating_key!r})",
            lambda client: client.get_entry(rating_key),
        )

    def get_children(self, rating_key: str, episode_order: str | None = None) -> list[SourceMetadata] | None:
        """Direct children (Seasons for a Show, Episodes for a Season) - see
        app/client/base.SourceClient.get_children. None means no enabled
        client could answer for this ratingKey at all (distinct from an
        empty list: "found, but no children")."""
        return self._first_hit(
            f"get_children({rating_key!r})",
            lambda client: client.get_children(rating_key, episode_order=episode_order),
        )

    def _first_hit(self, what: str, call: Callable[[SourceClient], T | None]) -> T | None:
        """Tries each client in order and returns the first non-None result.
        A client raising SourceUnavailableError is skipped; if no later
        client answers, that error is re-raised rather than reported as
        "not found"."""
        pending_error: SourceUnavailableError | None = None

        for client in self.clients:
            try:
                result = call(client)
            except SourceUnavailableError as exc:
                pending_error = exc
                logger.info("%s errored via source %r (%s) - trying next source", what, client.name, exc)
                continue
            if result is not None:
                return result

        if pending_error is not None:
            raise pending_error
        return None
