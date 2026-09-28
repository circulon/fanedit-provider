"""
Holds an ordered list of enabled source clients and resolves a single
ratingKey's full metadata by trying each client in turn.

app/services.py builds one SearchService per source category (for
type-scoped matching, see app/service/match.py) plus one combined
SearchService over all enabled categories (for ratingKey lookups, which
carry no type information - see app/service/metadata.py).
"""
import logging

from app.client.base import SourceMetadata, SourceClient, SourceUnavailableError

logger = logging.getLogger(__name__)


class SearchService:
    def __init__(self, clients: list[SourceClient]):
        #: Enabled source clients, in priority order.
        self.clients = clients

    def get_entry(self, rating_key: str) -> SourceMetadata | None:
        """Full metadata for a single ratingKey. Tries each client in
        order, returning the first hit. If a client errors and no later
        client recovers the entry, that error is re-raised rather than
        reported as "not found"."""
        pending_error: SourceUnavailableError | None = None

        for client in self.clients:
            try:
                entry = client.get_entry(rating_key)
            except SourceUnavailableError as exc:
                pending_error = exc
                logger.info(
                    "get_entry(%r) errored via source %r (%s) - trying next source",
                    rating_key, client.name, exc,
                )
                continue

            if entry is not None:
                return entry

        if pending_error is not None:
            raise pending_error

        return None

    def get_children(self, rating_key: str, episode_order: str | None = None) -> list[SourceMetadata] | None:
        """Direct children (Seasons for a Show, Episodes for a Season) for
        a single ratingKey - see app/client/base.SourceClient.get_children.
        Tries each client in order (a client without child support simply
        returns None, per CachingSourceClient.get_children), returning the
        first non-None result. Returns None if no enabled client could
        answer for this ratingKey at all (distinct from an empty list,
        which means "found, but no children")."""
        pending_error: SourceUnavailableError | None = None

        for client in self.clients:
            try:
                children = client.get_children(rating_key, episode_order=episode_order)
            except SourceUnavailableError as exc:
                pending_error = exc
                logger.info(
                    "get_children(%r) errored via source %r (%s) - trying next source",
                    rating_key, client.name, exc,
                )
                continue

            if children is not None:
                return children

        if pending_error is not None:
            raise pending_error

        return None
