"""
Session index — stores extracted data for fast retrieval and search.

Uses a simple JSON file as the index store. Each session gets an entry
with its extract, narrative, keywords, and optional LLM summary.
"""

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


INDEX_FILENAME = "session_index.json"


@dataclass
class IndexEntry:
    session_id: str
    user_query: str
    start_time: str
    end_time: str
    cwd: str
    git_branch: str
    topics: list[str]
    keywords: list[str]
    files_touched: list[str]
    tools_used: dict[str, int]
    narrative: str
    summary: str = ""  # LLM summary if available
    stats: dict = field(default_factory=dict)
    graph: dict = field(default_factory=dict)
    extract: dict = field(default_factory=dict)


class SessionIndex:
    """Manages the session index file."""

    def __init__(self, index_dir: str | Path | None = None):
        if index_dir is None:
            index_dir = Path.home() / ".claude" / "jsonl_extract"
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.index_dir / INDEX_FILENAME
        self._entries: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.index_path.exists():
            try:
                self._entries = json.loads(self.index_path.read_text())
            except (json.JSONDecodeError, OSError):
                self._entries = {}

    def _save(self):
        self.index_path.write_text(json.dumps(self._entries, indent=2))

    def add(self, entry: IndexEntry):
        self._entries[entry.session_id] = {
            "session_id": entry.session_id,
            "user_query": entry.user_query,
            "start_time": entry.start_time,
            "end_time": entry.end_time,
            "cwd": entry.cwd,
            "git_branch": entry.git_branch,
            "topics": entry.topics,
            "keywords": entry.keywords,
            "files_touched": entry.files_touched,
            "tools_used": entry.tools_used,
            "narrative": entry.narrative,
            "summary": entry.summary,
            "stats": entry.stats,
            "graph": entry.graph,
            "extract": entry.extract,
        }
        self._save()

    def get(self, session_id: str) -> Optional[dict]:
        return self._entries.get(session_id)

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """
        Search indexed sessions by keyword matching.
        Scores each session by how many query terms match its keywords,
        topics, user_query, narrative, and file paths.
        """
        query_terms = query.lower().split()
        scored = []

        for sid, entry in self._entries.items():
            score = 0
            searchable = " ".join([
                entry.get("user_query", ""),
                " ".join(entry.get("keywords", [])),
                " ".join(entry.get("topics", [])),
                entry.get("narrative", ""),
                " ".join(entry.get("files_touched", [])),
                entry.get("git_branch", ""),
                entry.get("summary", ""),
            ]).lower()

            for term in query_terms:
                if term in searchable:
                    score += searchable.count(term)

            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:max_results]]

    def list_sessions(self) -> list[dict]:
        """List all indexed sessions, sorted by start time (newest first)."""
        sessions = list(self._entries.values())
        sessions.sort(key=lambda s: s.get("start_time", ""), reverse=True)
        return sessions

    @property
    def count(self) -> int:
        return len(self._entries)
