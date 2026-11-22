"""Web tools. Offline mock by default; real access requires an opt-in connector.

The bundled corpus is deliberately labelled and uses reserved ``.invalid``
hosts. Summaries are extractive: every claim stays attached to the source text
it came from, so the assistant cannot invent facts or citations.
"""

from __future__ import annotations

import re
from typing import Any

from ...safety.redaction import redact_value
from .base import CoreTool, CoreToolError, truncate

MOCK_PAGES: tuple[dict[str, str], ...] = (
    {
        "url": "https://omen-local.invalid/guides/local-first",
        "title": "Local-first assistant design notes (offline mock)",
        "text": (
            "Local-first assistants keep notes, memory and transcripts on the user's computer. "
            "Cloud features stay off until the user opts in. Omen uses a local policy engine to "
            "approve actions outside the model. Approval tokens are single use and expire after "
            "a short time. Audit events are appended to a hash-chained log on disk."
        ),
    },
    {
        "url": "https://omen-local.invalid/guides/tool-safety",
        "title": "Tool safety and permissions (offline mock)",
        "text": (
            "Every tool declares a permission class before it runs. Read-only tools are allowed "
            "by policy, while writes require explicit approval. Payment execution and credential "
            "sharing are always blocked. A global kill switch stops further actions. Sandboxed "
            "shell commands run without a shell interpreter and are checked against an allowlist."
        ),
    },
    {
        "url": "https://omen-local.invalid/research/memory-notes",
        "title": "Research notes on memory budgets (offline mock)",
        "text": (
            "The memory budget for embeddings is 0.3 GB on this machine. The total assistant "
            "budget is 8 GB with a hard ceiling of 9 GB. Background indexing pauses when resource "
            "pressure reaches the degrade level. Search combines keyword and vector ranking."
        ),
    },
)

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_TERMS = re.compile(r"[A-Za-z0-9_]{3,}")
_NUMERIC = re.compile(r"\b([A-Za-z][A-Za-z ]{2,24}?)\s*(?:is|=|:)\s*(\d+(?:\.\d+)?)\s*(%|gb|ms|mb)?", re.I)


class WebProvider:
    """Resolve pages from an MCP-style connector or the offline mock corpus."""

    def __init__(self, config: Any, connectors: Any, budget: Any | None = None) -> None:
        self.config = config
        self.connectors = connectors
        self.budget = budget
        self._budget_warning: str | None = None
        self.max_results = int(config.get("core_tools.web.max_results", 5))
        self.max_page_chars = int(config.get("core_tools.web.max_page_chars", 12000))
        self.search_connector_name = config.get("core_tools.web.search_connector")
        self.read_connector_name = config.get("core_tools.web.read_connector")

    def _connector(self, name: Any, kind: str) -> Any:
        self._budget_warning = None
        if name:
            connector = self.connectors.get(kind)
            if connector is None or connector.name != str(name):
                raise CoreToolError(f"Configured {kind} connector {name!r} is not available")
        else:
            connector = self.connectors.get(kind)
        if connector is not None and connector.external:
            if self.budget is None:
                raise CoreToolError("External connector has no budget controller (BLOCKED-EXTERNAL)")
            decision = self.budget.authorize(provider=connector.name, local=False)
            if not decision.allowed:
                raise CoreToolError(
                    f"External connector blocked by cost policy: {decision.reason}; "
                    f"fallback: {decision.fallback or 'offline mock'}"
                )
            self._budget_warning = decision.warning
        return connector

    def search(self, query: str, limit: int | None = None) -> dict[str, Any]:
        connector = self._connector(self.search_connector_name, "web_search")
        count = max(1, min(int(limit or self.max_results), self.max_results))
        if connector is not None:
            response = connector.call("search", {"query": query, "limit": count})
            results = list(response.get("results") or [])
            return {
                "query": query,
                "results": redact_value(results[:count]),
                "source": connector.name,
                "external": bool(connector.external),
                "mode": "connector",
                "budget_warning": self._budget_warning,
            }
        terms = set(_TERMS.findall(query.lower()))
        scored: list[tuple[int, dict[str, Any]]] = []
        for page in MOCK_PAGES:
            haystack = f"{page['title']} {page['text']}".lower()
            score = sum(1 for term in terms if term in haystack)
            if terms and score == 0:
                continue
            scored.append(
                (
                    score,
                    {
                        "title": page["title"],
                        "url": page["url"],
                        "snippet": truncate(page["text"][:260], 280),
                        "score": score,
                    },
                )
            )
        scored.sort(key=lambda item: (-item[0], item[1]["url"]))
        return {
            "query": query,
            "results": redact_value([item for _, item in scored[:count]]),
            "source": "mock_offline_corpus",
            "external": False,
            "blocked_external": True,
            "mode": "mock",
        }

    def read(self, url: str) -> dict[str, Any]:
        connector = self._connector(self.read_connector_name, "web_read")
        if connector is not None:
            response = connector.call("read", {"url": url})
            text = truncate(str(response.get("text") or ""), self.max_page_chars)
            return {
                "url": url,
                "title": str(response.get("title") or url),
                "text": redact_value(text),
                "source": connector.name,
                "external": bool(connector.external),
                "mode": "connector",
                "budget_warning": self._budget_warning,
            }
        for page in MOCK_PAGES:
            if page["url"] == url:
                return {
                    "url": page["url"],
                    "title": page["title"],
                    "text": redact_value(truncate(page["text"], self.max_page_chars)),
                    "source": "mock_offline_corpus",
                    "external": False,
                    "blocked_external": True,
                    "mode": "mock",
                }
        raise CoreToolError(f"Page is not in the offline mock corpus: {url}")

    @staticmethod
    def sentences(text: str) -> list[str]:
        return [part.strip() for part in _SENTENCE.split(text) if part.strip()]

    def summarise(self, url: str, query: str = "", max_sentences: int = 3) -> dict[str, Any]:
        page = self.read(url)
        sentences = self.sentences(str(page["text"]))
        if not sentences:
            raise CoreToolError(f"No extractable text in page: {url}")
        terms = set(_TERMS.findall(query.lower()))
        ranked = sorted(
            range(len(sentences)),
            key=lambda index: (
                -sum(1 for term in terms if term in sentences[index].lower()),
                index,
            ),
        )
        chosen = sorted(ranked[: max(1, min(int(max_sentences), len(sentences)))])
        citations = [
            {"number": number, "title": page["title"], "url": page["url"]}
            for number in range(1, len(chosen) + 1)
        ]
        body = " ".join(f"{sentence} [{number}]" for number, sentence in enumerate(chosen, 1))
        return {
            "url": page["url"],
            "title": page["title"],
            "summary": redact_value(body),
            "citations": redact_value(citations),
            "source": page["source"],
            "external": page["external"],
            "blocked_external": page.get("blocked_external", False),
            "method": "extractive_sentences",
        }

    def compare(self, urls: list[str], query: str = "") -> dict[str, Any]:
        if len(urls) < 2:
            raise CoreToolError("At least two URLs are required to compare sources")
        pages = [self.read(url) for url in urls]
        terms = set(_TERMS.findall(query.lower()))
        claims: list[dict[str, Any]] = []
        values: dict[str, list[tuple[str, str, str]]] = {}
        for index, page in enumerate(pages, 1):
            for sentence in self.sentences(str(page["text"])):
                if terms and not any(term in sentence.lower() for term in terms):
                    continue
                claims.append(
                    {
                        "number": len(claims) + 1,
                        "text": redact_value(sentence),
                        "url": page["url"],
                        "source_index": index,
                    }
                )
                for match in _NUMERIC.finditer(sentence):
                    label = " ".join(match.group(1).lower().split())
                    values.setdefault(label, []).append(
                        (f"{match.group(2)}{match.group(3) or ''}", page["url"], sentence)
                    )
        contradictions: list[dict[str, Any]] = []
        for label, seen in sorted(values.items()):
            distinct_urls = {item[1] for item in seen}
            distinct_values = {item[0] for item in seen}
            if len(distinct_urls) > 1 and len(distinct_values) > 1:
                contradictions.append(
                    {
                        "metric": label,
                        "values": redact_value(
                            [{"value": value, "url": url} for value, url, _ in seen]
                        ),
                    }
                )
        return {
            "claims": claims,
            "contradictions": contradictions,
            "note": "Contradictions are only reported when two sources give different numeric values for the same labelled metric.",
            "sources": [{"number": i, "url": p["url"], "title": p["title"]} for i, p in enumerate(pages, 1)],
        }


class WebSearch(CoreTool):
    name = "web.search"
    description = "Search the web (offline mock unless a connector is enabled) with cited results."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, provider: WebProvider) -> None:
        self.provider = provider

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "query")
        return self.provider.search(self.text(kwargs.get("query")), kwargs.get("limit"))


class WebRead(CoreTool):
    name = "web.read"
    description = "Read and extract page text (offline mock unless a connector is enabled)."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 2000}},
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(self, provider: WebProvider) -> None:
        self.provider = provider

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "url")
        return self.provider.read(self.text(kwargs.get("url")))


class WebSummarise(CoreTool):
    name = "web.summarise"
    description = "Extractively summarise a page and attach numbered source citations."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 1, "maxLength": 2000},
            "query": {"type": "string", "maxLength": 500},
            "max_sentences": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(self, provider: WebProvider) -> None:
        self.provider = provider

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "url")
        return self.provider.summarise(
            self.text(kwargs.get("url")),
            self.text(kwargs.get("query")),
            int(kwargs.get("max_sentences") or 3),
        )


class WebCompare(CoreTool):
    name = "web.compare"
    description = "Compare cited claims from several pages and flag differing numeric metrics."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {
            "urls": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6},
            "query": {"type": "string", "maxLength": 500},
        },
        "required": ["urls"],
        "additionalProperties": False,
    }

    def __init__(self, provider: WebProvider) -> None:
        self.provider = provider

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        urls = kwargs.get("urls")
        if not isinstance(urls, (list, tuple)):
            raise CoreToolError("urls must be a list")
        return self.provider.compare([self.text(url) for url in urls], self.text(kwargs.get("query")))


class WebSaveNote(CoreTool):
    name = "web.save_note"
    description = "Save source-linked research into the local notes store."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "url": {"type": "string", "minLength": 1, "maxLength": 2000},
            "query": {"type": "string", "maxLength": 500},
        },
        "required": ["title", "url"],
        "additionalProperties": False,
    }

    def __init__(self, provider: WebProvider, notes: Any, rollback: Any) -> None:
        self.provider = provider
        self.notes = notes
        self.rollback = rollback

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "tool": self.name,
            "dry_run": True,
            "would_create": "source-linked local note",
            "title": self.text(kwargs.get("title")),
            "url": self.text(kwargs.get("url")),
            "side_effects": "none",
        }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "title", "url")
        summary = self.provider.summarise(self.text(kwargs.get("url")), self.text(kwargs.get("query")))
        record = self.notes.create(
            "notes",
            {
                "title": self.text(kwargs.get("title")),
                "body": summary["summary"],
                "sources": summary["citations"],
            },
        )
        plan_id = self.rollback.record(
            "record_remove",
            self.name,
            record["id"],
            {"domain": "notes", "id": record["id"]},
        )
        return {
            "note": record,
            "citations": summary["citations"],
            "source": summary["source"],
            "rollback_plan_id": plan_id,
            "rollback": "undo removes the locally created note",
        }
