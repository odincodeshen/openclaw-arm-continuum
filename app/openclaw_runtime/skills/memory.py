import re
import time
import uuid
from datetime import date, timedelta

from openclaw_runtime.categories import registry_entries, resolve_category
from openclaw_runtime.config import Settings
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.http_client import is_reachable
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skills.base import SkillResult


# Accept the half-width "#" and the full-width "＃" (common from CJK IMEs),
# and half/full-width brackets for multi-word names.
_CATEGORY_PREFIX_RE = re.compile(
    r"^[#＃]+[ \t]*(?:[\[［]([^\]］]+)[\]］]"
    r"|[\{｛]([^\}｝]+)[\}｝]"
    r"|(\S+))(?:\s+(.*))?$",
    re.DOTALL,
)
_ALL_CATEGORIES_TOKEN = "\x00all\x00"

# A leading tag:<word> prefix on a /rag query, e.g. "tag:work how much...".
_RAG_TAG_PREFIX_RE = re.compile(r"^tag:(\S+)\s+(.*)$", re.IGNORECASE | re.DOTALL)

# due:YYYY-MM-DD / tag:<word> anywhere in a /mem write, as standalone tokens
# (bounded by whitespace or string edges, so they don't match mid-word).
_DUE_TOKEN_RE = re.compile(r"(?<!\S)due:(\S+)(?!\S)", re.IGNORECASE)
_TAG_TOKEN_RE = re.compile(r"(?<!\S)tag:(\S+)(?!\S)", re.IGNORECASE)

# Relative snooze targets like "3d" or "1w" for /mem snooze.
_SNOOZE_RELATIVE_RE = re.compile(r"^(\d+)([dw])$", re.IGNORECASE)

# A single tag:<word> token, matched per whitespace-split token (so no
# boundary lookaround needed) for /mem list and /mem digest scoping.
_TAG_ARG_TOKEN_RE = re.compile(r"^tag:(\S+)$", re.IGNORECASE)


def _parse_list_or_digest_args(arg: str) -> tuple[str, str | None]:
    """Parse /mem list|digest args: the literal "done" (list only) selects
    the done bucket, and tag:<word> scopes results to that exact tag."""
    status = "active"
    tag: str | None = None
    for token in arg.split():
        if token.lower() == "done":
            status = "done"
            continue
        match = _TAG_ARG_TOKEN_RE.match(token)
        if match:
            tag = match.group(1)
    return status, tag


def parse_memory_metadata(content: str) -> tuple[str, str | None, list[str]]:
    """Pull ``due:YYYY-MM-DD`` and ``tag:<word>`` tokens out of free text.

    Returns ``(clean_text, due_or_None, tags)``. Every ``tag:`` token is
    consumed. Only the first ``due:`` token that parses as a real ISO date is
    consumed as the due date; anything that doesn't parse (or a second due:)
    is left in the text untouched -- it likely wasn't meant as metadata.
    """
    tags: list[str] = []

    def _take_tag(match: re.Match) -> str:
        tags.append(match.group(1))
        return ""

    working = _TAG_TOKEN_RE.sub(_take_tag, content)

    due: str | None = None

    def _take_due(match: re.Match) -> str:
        nonlocal due
        if due is not None:
            return match.group(0)
        value = match.group(1)
        try:
            date.fromisoformat(value)
        except ValueError:
            return match.group(0)
        due = value
        return ""

    working = _DUE_TOKEN_RE.sub(_take_due, working)
    clean = " ".join(working.split())
    return clean, due, tags


def split_category_prefix(query: str) -> tuple[str | None, str]:
    """Pull a leading ``#category`` / ``#[multi word]`` / ``#all`` off a query.

    Returns ``(category_token_or_None, remaining_query)``. ``#all`` yields the
    internal all-categories sentinel. Also accepts the full-width ``＃``.
    """
    match = _CATEGORY_PREFIX_RE.match(query.strip())
    if not match:
        return None, query.strip()
    token = (match.group(1) or match.group(2) or match.group(3) or "").strip()
    rest = (match.group(4) or "").strip()
    if token.casefold() in {"all", "*", "全部", "所有"}:
        return _ALL_CATEGORIES_TOKEN, rest
    return token, rest


def split_tag_prefix(query: str) -> tuple[str | None, str]:
    """Pull a leading ``tag:<word>`` off a /rag query, e.g.
    ``tag:work what did I save about the deadline?``. Only recognized
    before any ``#<category>`` prefix -- tags only exist on tracker_memory
    items (from /mem due:/tag:), so this only affects the default
    (no-category) search path; see ``RagRetrieveSkill._run_default``.
    """
    match = _RAG_TAG_PREFIX_RE.match(query)
    if not match:
        return None, query
    return match.group(1), match.group(2).strip()


class MemoryWriteSkill:
    name = "memory_write"

    def __init__(self, settings: Settings, config: dict, embeddings: EmbeddingClient, qdrant: QdrantClient) -> None:
        self.settings = settings
        self.keywords = tuple(config.get("keywords", ["/mem", "/remember", "mem:", "remember:"]))
        self.embeddings = embeddings
        self.qdrant = qdrant

    def can_handle(self, text: str) -> bool:
        return any(text.startswith(keyword) for keyword in self.keywords)

    def health_check(self) -> str:
        if is_reachable(f"{self.settings.qdrant_base_url}/collections"):
            return "ready"
        return "error: Qdrant unreachable"

    def run(self, text: str) -> SkillResult:
        content = self._strip_command(text)
        if not content:
            return SkillResult(
                self.name,
                "Add the content to save after /mem, or use:\n"
                "/mem list [done] [tag:<word>]\n/mem done <id>\n/mem rm <id>\n"
                "/mem digest [tag:<word>]\n"
                "/mem snooze <id> <3d|1w|YYYY-MM-DD>\n/mem edit <id> <new text>\n"
                "Add due:YYYY-MM-DD and/or tag:<word> anywhere in the text to save them.",
            )
        stripped = content.strip()
        first_word, _, rest = stripped.partition(" ")
        keyword = first_word.lower()
        if keyword == "list":
            return self._list(rest.strip())
        if keyword == "done":
            return self._mark_done(rest.strip())
        if keyword in ("rm", "delete"):
            return self._remove(rest.strip())
        if keyword == "digest":
            return self._digest(rest.strip())
        if keyword == "snooze":
            return self._snooze(rest.strip())
        if keyword == "edit":
            return self._edit(rest.strip())
        return self._write(content)

    def _write(self, content: str) -> SkillResult:
        clean_text, due, tags = parse_memory_metadata(content)
        if not clean_text:
            return SkillResult(self.name, "Add some content to remember, not just due:/tag: metadata.")

        point_id = str(uuid.uuid4())
        short_id = point_id.split("-")[0]
        now = int(time.time())
        metadata = {
            "source": "telegram",
            "kind": "tracker_memory",
            "short_id": short_id,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        if due:
            metadata["due"] = due
        if tags:
            metadata["tags"] = tags

        vector = self.embeddings.embed(clean_text)
        self.qdrant.upsert_text(
            self.settings.tracker_collection, clean_text, vector, metadata, point_id=point_id
        )

        bits = []
        if due:
            bits.append(f"due {due}")
        if tags:
            bits.append("tags: " + ", ".join(tags))
        suffix = f" ({'; '.join(bits)})" if bits else ""
        return SkillResult(self.name, f"Saved to {self.settings.tracker_collection}. Memory ID: {short_id}{suffix}")

    def _edit(self, rest: str) -> SkillResult:
        short_id, _, new_content = rest.partition(" ")
        new_content = new_content.strip()
        if not short_id or not new_content:
            return SkillResult(self.name, "Usage: /mem edit <id> <new text>")
        point = self._find_by_short_id(short_id)
        if not point:
            return SkillResult(self.name, f'No memory item with ID "{short_id}".')

        clean_text, due, tags = parse_memory_metadata(new_content)
        if not clean_text:
            return SkillResult(self.name, "Add some content to remember, not just due:/tag: metadata.")

        payload = point.get("payload") or {}
        # due:/tag: in the new text override the stored ones; omitting them
        # keeps whatever was already there instead of wiping it out.
        effective_due = due or payload.get("due")
        effective_tags = tags or payload.get("tags")
        metadata = {
            "source": payload.get("source", "telegram"),
            "kind": "tracker_memory",
            "short_id": short_id,
            "status": payload.get("status", "active"),
            "created_at": payload.get("created_at", int(time.time())),
            "updated_at": int(time.time()),
        }
        if effective_due:
            metadata["due"] = effective_due
        if effective_tags:
            metadata["tags"] = effective_tags

        vector = self.embeddings.embed(clean_text)
        self.qdrant.upsert_text(
            self.settings.tracker_collection, clean_text, vector, metadata, point_id=point["id"]
        )

        bits = []
        if effective_due:
            bits.append(f"due {effective_due}")
        if effective_tags:
            bits.append("tags: " + ", ".join(effective_tags))
        suffix = f" ({'; '.join(bits)})" if bits else ""
        return SkillResult(self.name, f"Updated #{short_id}: {clean_text}{suffix}")

    def _list(self, arg: str) -> SkillResult:
        status, tag = _parse_list_or_digest_args(arg)
        filters = {"kind": "tracker_memory", "status": status}
        if tag:
            filters["tags"] = tag
        hits = self.qdrant.scroll_by_filters(self.settings.tracker_collection, filters, limit=200)
        tag_suffix = f' tagged "{tag}"' if tag else ""
        if not hits:
            return SkillResult(
                self.name, f"No {'completed' if status == 'done' else 'active'} memory items{tag_suffix}."
            )

        def sort_key(hit: dict) -> tuple:
            payload = hit.get("payload") or {}
            return (payload.get("due") or "9999-99-99", -(payload.get("created_at") or 0))

        hits.sort(key=sort_key)
        label = "Completed" if status == "done" else "Active"
        lines = [f"{label} memory{tag_suffix} ({len(hits)}):"]
        for hit in hits[:50]:
            payload = hit.get("payload") or {}
            short_id = payload.get("short_id", "?")
            item_text = str(payload.get("text") or "").strip()
            bits = []
            if payload.get("due"):
                bits.append(f"due {payload['due']}")
            tags = payload.get("tags") or []
            if tags:
                bits.append("tags: " + ", ".join(tags))
            suffix = f" ({'; '.join(bits)})" if bits else ""
            lines.append(f"#{short_id} {item_text}{suffix}")
        if len(hits) > 50:
            lines.append(f"... and {len(hits) - 50} more.")
        return SkillResult(self.name, "\n".join(lines))

    def _mark_done(self, short_id: str) -> SkillResult:
        if not short_id:
            return SkillResult(self.name, "Usage: /mem done <id>")
        point = self._find_by_short_id(short_id)
        if not point:
            return SkillResult(self.name, f'No memory item with ID "{short_id}".')
        self.qdrant.set_payload(
            self.settings.tracker_collection,
            point["id"],
            {"status": "done", "updated_at": int(time.time())},
        )
        return SkillResult(self.name, f"Marked #{short_id} as done.")

    def _snooze(self, rest: str) -> SkillResult:
        short_id, _, amount = rest.partition(" ")
        amount = amount.strip()
        if not short_id or not amount:
            return SkillResult(self.name, "Usage: /mem snooze <id> <3d|1w|YYYY-MM-DD>")
        new_due = self._parse_snooze_target(amount)
        if new_due is None:
            return SkillResult(
                self.name, f'Could not parse "{amount}" as a snooze target. Use 3d, 1w, or YYYY-MM-DD.'
            )
        point = self._find_by_short_id(short_id)
        if not point:
            return SkillResult(self.name, f'No memory item with ID "{short_id}".')
        new_due_str = new_due.isoformat()
        self.qdrant.set_payload(
            self.settings.tracker_collection,
            point["id"],
            # Snoozing implies "not done yet" -- reactivate a done item too.
            {"due": new_due_str, "status": "active", "updated_at": int(time.time())},
        )
        item_text = str((point.get("payload") or {}).get("text") or "").strip()
        suffix = f": {item_text}" if item_text else ""
        return SkillResult(self.name, f"Snoozed #{short_id} to {new_due_str}{suffix}")

    @staticmethod
    def _parse_snooze_target(amount: str) -> date | None:
        match = _SNOOZE_RELATIVE_RE.match(amount)
        if match:
            count = int(match.group(1))
            unit = match.group(2).lower()
            days = count if unit == "d" else count * 7
            return date.today() + timedelta(days=days)
        try:
            return date.fromisoformat(amount)
        except ValueError:
            return None

    def _remove(self, short_id: str) -> SkillResult:
        if not short_id:
            return SkillResult(self.name, "Usage: /mem rm <id>")
        point = self._find_by_short_id(short_id)
        if not point:
            return SkillResult(self.name, f'No memory item with ID "{short_id}".')
        self.qdrant.delete_points(self.settings.tracker_collection, [point["id"]])
        return SkillResult(self.name, f"Deleted #{short_id}.")

    def _find_by_short_id(self, short_id: str) -> dict | None:
        hits = self.qdrant.scroll_by_filters(
            self.settings.tracker_collection,
            {"kind": "tracker_memory", "short_id": short_id},
            limit=1,
        )
        return hits[0] if hits else None

    def _digest(self, arg: str = "") -> SkillResult:
        """A proactive-reminder summary: overdue items, items due soon, and
        stale undated items. Meant to be run interactively (/mem digest) or
        on a cron schedule -- see docs/TRACKER_MEMORY.md.

        Overdue and due-soon items are repeated every call on purpose (that
        is what a reminder is for). Stale items get a cooldown
        (mem_digest_remind_cooldown_days) via `last_reminded_at` so an
        undated item doesn't get nagged about on every single run.

        An optional tag:<word> arg scopes the whole digest to that tag --
        handy for a per-topic cron job (e.g. `/mem digest tag:work`).
        """
        _, tag = _parse_list_or_digest_args(arg)
        filters = {"kind": "tracker_memory", "status": "active"}
        if tag:
            filters["tags"] = tag
        hits = self.qdrant.scroll_by_filters(self.settings.tracker_collection, filters, limit=500)
        today = date.today()
        due_soon_cutoff = today + timedelta(days=self.settings.mem_digest_due_soon_days)
        stale_cutoff = time.time() - self.settings.mem_digest_stale_days * 86400
        cooldown_cutoff = time.time() - self.settings.mem_digest_remind_cooldown_days * 86400

        overdue: list[dict] = []
        due_soon: list[dict] = []
        stale: list[dict] = []
        remind_ids: list[str] = []

        for hit in hits:
            payload = hit.get("payload") or {}
            due_value = payload.get("due")
            due_date = self._parse_due(due_value)
            if due_date is not None:
                if due_date < today:
                    overdue.append(hit)
                elif due_date <= due_soon_cutoff:
                    due_soon.append(hit)
                continue
            updated_at = payload.get("updated_at") or payload.get("created_at") or 0
            if updated_at > stale_cutoff:
                continue
            last_reminded_at = payload.get("last_reminded_at") or 0
            if last_reminded_at > cooldown_cutoff:
                continue
            stale.append(hit)
            remind_ids.append(hit["id"])

        overdue.sort(key=lambda h: h["payload"]["due"])
        due_soon.sort(key=lambda h: h["payload"]["due"])

        if not overdue and not due_soon and not stale:
            tag_suffix = f' tagged "{tag}"' if tag else ""
            return SkillResult(
                self.name,
                f"You're all caught up{tag_suffix} -- nothing overdue, due soon, or stale.",
                suppress_if_routine=True,
            )

        if remind_ids:
            now = int(time.time())
            for point_id in remind_ids:
                self.qdrant.set_payload(
                    self.settings.tracker_collection, point_id, {"last_reminded_at": now}
                )

        sections = []
        if overdue:
            sections.append(self._digest_section("Overdue", overdue))
        if due_soon:
            sections.append(
                self._digest_section(f"Due in the next {self.settings.mem_digest_due_soon_days} days", due_soon)
            )
        if stale:
            sections.append(
                self._digest_section(
                    f"Stale (untouched {self.settings.mem_digest_stale_days}+ days, no due date)", stale
                )
            )
        return SkillResult(self.name, "\n\n".join(sections))

    @staticmethod
    def _digest_section(title: str, hits: list[dict]) -> str:
        lines = [f"{title} ({len(hits)}):"]
        for hit in hits:
            payload = hit.get("payload") or {}
            short_id = payload.get("short_id", "?")
            text = str(payload.get("text") or "").strip()
            due = payload.get("due")
            suffix = f" (due {due})" if due else ""
            lines.append(f"#{short_id} {text}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _parse_due(value) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _strip_command(text: str) -> str:
        for prefix in ("/mem ", "/remember ", "mem:", "remember:"):
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        if text in {"/mem", "/remember"}:
            return ""
        return text.strip()


class RagRetrieveSkill:
    name = "rag_retrieve"

    def __init__(
        self,
        settings: Settings,
        config: dict,
        embeddings: EmbeddingClient,
        qdrant: QdrantClient,
        llm: LlmClient,
    ) -> None:
        self.settings = settings
        self.keywords = tuple(config.get("keywords", ["/rag", "rag:"]))
        self.embeddings = embeddings
        self.qdrant = qdrant
        self.llm = llm
        self.model_policy = str(config.get("model_policy") or "").strip() or "local_default"

    @property
    def endpoint_id(self):
        return getattr(self.llm, "endpoint_id", None)

    def can_handle(self, text: str) -> bool:
        return any(keyword in text for keyword in self.keywords)

    def health_check(self) -> str:
        if not is_reachable(f"{self.settings.qdrant_base_url}/collections"):
            return "error: Qdrant unreachable"
        if not self.llm.is_reachable():
            return "error: vLLM unreachable"
        return "ready"

    def run(self, text: str) -> SkillResult:
        raw_query = self._strip_command(text)
        tag, raw_query = split_tag_prefix(raw_query)
        category_token, query = split_category_prefix(raw_query)
        if not query:
            return SkillResult(self.name, "Add the question to look up after /rag.")

        if self.settings.category_rag_enabled and category_token == _ALL_CATEGORIES_TOKEN:
            return self._run_all_categories(query)
        if self.settings.category_rag_enabled and category_token:
            return self._run_single_category(category_token, query)
        return self._run_default(query, tag)

    def _run_default(self, query: str, tag: str | None = None) -> SkillResult:
        file_hits = self._file_hits(
            query, [self.settings.knowledge_collection, self.settings.tracker_collection]
        )
        vector = self.embeddings.embed(query)
        # tags only ever live on tracker_memory items (from /mem due:/tag:),
        # so a tag filter scopes tracker search and skips knowledge search
        # entirely rather than returning knowledge hits no filter applies to.
        tracker_filters = {"tags": tag} if tag else None
        tracker_hits = self.qdrant.search(self.settings.tracker_collection, vector, filters=tracker_filters)
        sections = [("filename_match", file_hits), (self.settings.tracker_collection, tracker_hits)]
        if not tag:
            knowledge_hits = self.qdrant.search(self.settings.knowledge_collection, vector)
            sections.append((self.settings.knowledge_collection, knowledge_hits))
        answer = self._answer_from(query, sections)
        if answer is None:
            tag_suffix = f' tagged "{tag}"' if tag else ""
            return SkillResult(self.name, f"No relevant memory was found in either Qdrant collection{tag_suffix}.")
        return SkillResult(self.name, answer)

    def _run_single_category(self, token: str, query: str) -> SkillResult:
        entry = resolve_category(self.settings, token)
        if not entry or not entry.get("known"):
            names = ", ".join(item["display"] for item in registry_entries(self.settings)) or "(none yet)"
            return SkillResult(
                self.name,
                f"No category named \"{token}\". Existing categories: {names}\n"
                "Query with /rag #<category>, or create one by captioning an upload #<category>.",
            )
        collection = entry["collection"]
        vector = self.embeddings.embed(query)
        try:
            hits = self.qdrant.search(collection, vector)
        except Exception:
            hits = []
        file_hits = self._file_hits(query, [collection])
        answer = self._answer_from(
            query, [("filename_match", file_hits), (f"category:{entry['display']}", hits)]
        )
        if answer is None:
            return SkillResult(
                self.name,
                f"Category \"{entry['display']}\" has no indexed content yet "
                "(if you just uploaded, give the indexer ~10 seconds).",
            )
        return SkillResult(self.name, answer)

    def _run_all_categories(self, query: str) -> SkillResult:
        entries = registry_entries(self.settings)
        if not entries:
            return SkillResult(self.name, "No categories have been created yet.")
        vector = self.embeddings.embed(query)
        sections = []
        for entry in entries:
            try:
                hits = self.qdrant.search(entry["collection"], vector, limit=3)
            except Exception:
                hits = []
            if hits:
                sections.append((f"category:{entry['display']}", hits))
        answer = self._answer_from(query, sections)
        if answer is None:
            return SkillResult(self.name, "No relevant content was found in any category.")
        return SkillResult(self.name, answer)

    def _answer_from(self, query: str, labelled_hits: list[tuple[str, list[dict]]]) -> str | None:
        context = self._format_context(labelled_hits)
        if not context:
            return None
        prompt = (
            "You are OpenClaw's local RAG assistant. Answer using only the Context below; "
            "if the Context is insufficient, say so explicitly. When you use a fact, name "
            "the source document it came from.\n\n"
            f"Question: {query}\n\n"
            f"Context:\n{context}"
        )
        answer = self.llm.chat(prompt, max_tokens=360)
        sources = self._collect_sources(labelled_hits)
        if sources:
            answer = f"{answer}\n\nSources: {', '.join(sources)}"
        return answer

    @staticmethod
    def _source_name(payload: dict) -> str | None:
        for key in ("original_file_name", "doc_title", "file_name"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return None

    def _collect_sources(self, labelled_hits: list[tuple[str, list[dict]]]) -> list[str]:
        seen: list[str] = []
        for _label, hits in labelled_hits:
            for hit in hits:
                payload = hit.get("payload") or {}
                if not str(payload.get("text") or "").strip():
                    continue
                name = self._source_name(payload)
                if name and name not in seen:
                    seen.append(name)
        return seen

    def _file_hits(self, query: str, collections: list[str]) -> list[dict]:
        file_names = re.findall(r"[\w.+-]+\.(?:pdf|odf|md|txt|log|json|csv|tsv)", query, flags=re.IGNORECASE)
        hits = []
        seen = set()
        for file_name in file_names:
            normalized = file_name.strip()
            for candidate in (normalized, normalized.replace(".odf", ".pdf")):
                if candidate in seen:
                    continue
                seen.add(candidate)
                for collection in collections:
                    try:
                        hits.extend(self.qdrant.scroll_by_file_name(collection, candidate, limit=10))
                    except Exception:
                        continue
        return hits

    def _format_context(self, labelled_hits: list[tuple[str, list[dict]]]) -> str:
        parts = []
        for label, hits in labelled_hits:
            for index, hit in enumerate(hits, start=1):
                payload = hit.get("payload") or {}
                text = str(payload.get("text") or "").strip()
                if not text:
                    continue
                score = hit.get("score", 0)
                source = self._source_name(payload)
                tag = f"{label} · {source}" if source else label
                parts.append(f"[{tag} #{index} score={score:.3f}] {text}")
        return "\n".join(parts)

    @staticmethod
    def _strip_command(text: str) -> str:
        for prefix in ("/rag ", "rag:"):
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        return text.strip()
