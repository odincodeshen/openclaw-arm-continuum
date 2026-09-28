from openclaw_runtime.owned_records import read_owned_points, write_owned_point
from openclaw_runtime.qdrant_client import QdrantClient


def day_tag(week_number: int, day: str) -> str:
    return f"eng_wk{week_number}_day{day}"


def mark_task_pushed(
    qdrant: QdrantClient,
    collection: str,
    week_number: int,
    day: str,
    owner: str,
    vector: list[float],
) -> str:
    """Record that today's task was pushed to this user, starting in the
    not-yet-completed state. Call right after sending that day's task
    message.

    One record per user per day: pushing the same day again (a restart, a
    forced re-push while testing) resets the existing record to "pushed,
    not completed" and removes any extra copies left from before, instead of
    adding another one -- the latest push is what counts."""
    tag = day_tag(week_number, day)
    existing = list(read_owned_points(qdrant, collection, owner, {"tag": tag, "kind": "daily_task"}, limit=64))
    if existing:
        keep, extra = existing[0], existing[1:]
        qdrant.set_payload(collection, keep["id"], {"completed": False, "skipped": False})
        if extra:
            qdrant.delete_points(collection, [point["id"] for point in extra])
        return keep["id"]
    return write_owned_point(
        qdrant,
        collection,
        owner,
        f"{tag} task pushed",
        vector,
        {"tag": tag, "kind": "daily_task", "completed": False, "skipped": False},
    )


def mark_task_completed(
    qdrant: QdrantClient, collection: str, week_number: int, day: str, owner: str
) -> bool:
    """Flip today's task record to completed when the user replies. Returns
    False if no pushed-task record exists yet (nothing to mark)."""
    tag = day_tag(week_number, day)
    points = read_owned_points(qdrant, collection, owner, {"tag": tag, "kind": "daily_task"}, limit=4)
    if not points:
        return False
    for point in points:
        qdrant.set_payload(collection, point["id"], {"completed": True})
    return True


def sweep_incomplete_to_skipped(
    qdrant: QdrantClient, collection: str, week_number: int, day: str, owners: list[str]
) -> list[str]:
    """Meant for the 21:00 daily sweep: mark skipped=True for every owner
    whose task record for this day is still not completed. Returns the list
    of owners actually marked skipped."""
    tag = day_tag(week_number, day)
    swept: list[str] = []
    for owner in owners:
        points = read_owned_points(qdrant, collection, owner, {"tag": tag, "kind": "daily_task"}, limit=4)
        for point in points:
            payload = point.get("payload") or {}
            if not payload.get("completed"):
                qdrant.set_payload(collection, point["id"], {"skipped": True})
                swept.append(owner)
    return swept
