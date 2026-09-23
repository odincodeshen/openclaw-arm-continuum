from openclaw_runtime.qdrant_client import QdrantClient


def write_owned_point(
    qdrant: QdrantClient,
    collection: str,
    owner: str,
    text: str,
    vector: list[float],
    metadata: dict,
    *,
    point_id: str | None = None,
) -> str:
    """Write a per-user record. ``owner`` is mandatory and never defaulted --
    a per-user record written without it would be indistinguishable from
    shared content on read, silently leaking one family member's data into
    another's queries the moment anyone forgets to pass it."""
    if not owner:
        raise ValueError("owner is required -- refusing to write a per-user record without it")
    payload = {"owner": owner, **metadata}
    return qdrant.upsert_text(collection, text, vector, payload, point_id=point_id)


def read_owned_points(
    qdrant: QdrantClient,
    collection: str,
    owner: str,
    extra_filters: dict | None = None,
    limit: int = 64,
) -> list[dict]:
    """Read only ``owner``'s records. Same mandatory-owner rule as
    write_owned_point -- omitting it here would scroll every family member's
    records back as one merged result."""
    if not owner:
        raise ValueError("owner is required -- refusing to read per-user records without it")
    filters = {"owner": owner, **(extra_filters or {})}
    return qdrant.scroll_by_filters(collection, filters, limit=limit)
