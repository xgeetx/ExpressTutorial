"""
Parse Claude Code JSONL session logs into structured data.

Handles:
- Single session files
- Session continuations (context clears, plan->execution transitions)
  linked via slug fields and compact_boundary records
- Token usage extraction from message.usage fields
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TokenUsage:
    """Token usage for a single API call."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_tokens + self.cache_read_tokens)

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass
class ToolUse:
    tool_name: str
    tool_id: str
    input_data: dict
    timestamp: str
    uuid: str


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool
    timestamp: str
    uuid: str


@dataclass
class Message:
    role: str  # "user" or "assistant"
    uuid: str
    parent_uuid: Optional[str]
    timestamp: str
    text_blocks: list[str] = field(default_factory=list)
    tool_uses: list[ToolUse] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    thinking_blocks: list[str] = field(default_factory=list)
    is_sidechain: bool = False
    cwd: str = ""
    git_branch: str = ""
    session_id: str = ""
    segment_index: int = 0  # Which continuation segment this belongs to
    usage: Optional[TokenUsage] = None
    model: str = ""


@dataclass
class SessionSegment:
    """A single continuous context window within a conversation.
    A conversation may have multiple segments due to context clears."""
    segment_index: int
    session_id: str  # The file-level session ID for this segment
    messages: list[Message] = field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    user_query: str = ""  # First user message in this segment
    is_continuation: bool = False


@dataclass
class Session:
    """A full conversation, potentially spanning multiple context clears."""
    session_id: str  # Primary session ID (from the file)
    slug: str = ""  # Human-readable conversation identifier, persists across continuations
    segments: list[SessionSegment] = field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    cwd: str = ""
    git_branch: str = ""
    version: str = ""
    user_query: str = ""  # First user message across all segments
    total_usage: TokenUsage = field(default_factory=TokenUsage)

    @property
    def messages(self) -> list[Message]:
        """All messages across all segments, in order."""
        msgs = []
        for seg in self.segments:
            msgs.extend(seg.messages)
        return msgs

    @property
    def segment_count(self) -> int:
        return len(self.segments)


def parse_usage(usage_data: dict) -> TokenUsage:
    """Extract token usage from a message's usage field."""
    if not usage_data:
        return TokenUsage()
    return TokenUsage(
        input_tokens=usage_data.get("input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0),
        cache_creation_tokens=(
            usage_data.get("cache_creation_input_tokens", 0)
            or usage_data.get("cacheCreationTokens", 0)
        ),
        cache_read_tokens=(
            usage_data.get("cache_read_input_tokens", 0)
            or usage_data.get("cacheReadTokens", 0)
        ),
    )


def parse_content_blocks(content, timestamp: str, uuid: str):
    """Extract typed content blocks from a message's content field."""
    text_blocks = []
    tool_uses = []
    tool_results = []
    thinking_blocks = []

    if isinstance(content, str):
        text_blocks.append(content)
        return text_blocks, tool_uses, tool_results, thinking_blocks

    if not isinstance(content, list):
        return text_blocks, tool_uses, tool_results, thinking_blocks

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")

        if block_type == "text":
            text_blocks.append(block.get("text", ""))

        elif block_type == "tool_use":
            tool_uses.append(ToolUse(
                tool_name=block.get("name", ""),
                tool_id=block.get("id", ""),
                input_data=block.get("input", {}),
                timestamp=timestamp,
                uuid=uuid,
            ))

        elif block_type == "tool_result":
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                parts = []
                for part in result_content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text", ""))
                result_content = "\n".join(parts)
            elif not isinstance(result_content, str):
                result_content = str(result_content)

            tool_results.append(ToolResult(
                tool_use_id=block.get("tool_use_id", ""),
                content=result_content[:2000],  # Truncate large results
                is_error=block.get("is_error", False),
                timestamp=timestamp,
                uuid=uuid,
            ))

        elif block_type == "thinking":
            thinking_blocks.append(block.get("thinking", ""))

    return text_blocks, tool_uses, tool_results, thinking_blocks


def _detect_segments(records: list[dict], file_session_id: str) -> list[tuple[int, str, bool]]:
    """
    Detect context clear boundaries in a list of parsed JSONL records.

    Returns a list of (start_record_index, segment_session_id, is_continuation).

    Detection strategy:
    1. compact_boundary records explicitly mark segment boundaries.
    2. A change in sessionId within the records indicates a continuation
       prefix (records before the switch are from the parent session).
    3. If neither is found, it's a single-segment session.
    """
    boundaries = []
    seen_session_ids = []
    prev_session_id = None

    for i, record in enumerate(records):
        record_type = record.get("type", "")

        # Explicit compact_boundary marker
        if record_type == "compact_boundary":
            sid = record.get("sessionId", file_session_id)
            boundaries.append((i, sid, True))
            continue

        sid = record.get("sessionId", "")
        if not sid:
            continue

        # Track session ID changes (continuation detection)
        if sid != prev_session_id and prev_session_id is not None:
            if sid not in seen_session_ids:
                boundaries.append((i, sid, True))

        if sid not in seen_session_ids:
            seen_session_ids.append(sid)
        prev_session_id = sid

    # If no boundaries detected, single segment starting at 0
    if not boundaries:
        return [(0, file_session_id, False)]

    # Ensure we have the initial segment
    if boundaries[0][0] > 0:
        first_sid = seen_session_ids[0] if seen_session_ids else file_session_id
        boundaries.insert(0, (0, first_sid, False))

    return boundaries


def parse_jsonl_file(filepath: str | Path) -> Session:
    """Parse a single JSONL session file into a Session object.

    Handles session continuations by detecting segment boundaries
    (compact_boundary records or sessionId changes) and grouping
    messages into segments.
    """
    filepath = Path(filepath)
    file_session_id = filepath.stem

    # Parse all records first
    records = []
    lines = filepath.read_text().strip().split("\n")
    for line in lines:
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Extract slug (conversation-level identifier)
    slug = ""
    for record in records:
        s = record.get("slug", "")
        if s:
            slug = s
            break

    # Detect segment boundaries
    seg_boundaries = _detect_segments(records, file_session_id)

    session = Session(session_id=file_session_id, slug=slug)
    total_usage = TokenUsage()

    # Process each segment
    for seg_idx, (start_rec, seg_session_id, is_continuation) in enumerate(seg_boundaries):
        # Determine end of this segment
        if seg_idx + 1 < len(seg_boundaries):
            end_rec = seg_boundaries[seg_idx + 1][0]
        else:
            end_rec = len(records)

        segment = SessionSegment(
            segment_index=seg_idx,
            session_id=seg_session_id,
            is_continuation=is_continuation,
        )

        for record in records[start_rec:end_rec]:
            record_type = record.get("type", "")
            timestamp = record.get("timestamp", "")

            # Skip non-message records
            if record_type not in ("user", "assistant"):
                continue

            # Skip compact summary records (synthetic, not real conversation)
            if record.get("isCompactSummary", False):
                continue

            uuid = record.get("uuid", "")
            parent_uuid = record.get("parentUuid")
            is_sidechain = record.get("isSidechain", False)
            cwd = record.get("cwd", "")
            git_branch = record.get("gitBranch", "")
            version = record.get("version", "")
            rec_session_id = record.get("sessionId", "")

            msg_data = record.get("message", {})
            role = msg_data.get("role", record_type)
            content = msg_data.get("content", "")
            model = msg_data.get("model", "")

            # Token usage
            usage_data = msg_data.get("usage", {})
            usage = parse_usage(usage_data) if usage_data else None
            if usage:
                total_usage = total_usage + usage

            text_blocks, tool_uses, tool_results, thinking_blocks = parse_content_blocks(
                content, timestamp, uuid
            )

            msg = Message(
                role=role,
                uuid=uuid,
                parent_uuid=parent_uuid,
                timestamp=timestamp,
                text_blocks=text_blocks,
                tool_uses=tool_uses,
                tool_results=tool_results,
                thinking_blocks=thinking_blocks,
                is_sidechain=is_sidechain,
                cwd=cwd,
                git_branch=git_branch,
                session_id=rec_session_id,
                segment_index=seg_idx,
                usage=usage,
                model=model,
            )
            segment.messages.append(msg)

            # Update segment metadata
            if not segment.start_time or timestamp < segment.start_time:
                segment.start_time = timestamp
            if not segment.end_time or timestamp > segment.end_time:
                segment.end_time = timestamp

            # Capture first user message as segment query
            if role == "user" and not segment.user_query and text_blocks:
                segment.user_query = text_blocks[0][:500]

            # Update session metadata
            if not session.start_time or timestamp < session.start_time:
                session.start_time = timestamp
            if not session.end_time or timestamp > session.end_time:
                session.end_time = timestamp
            if cwd:
                session.cwd = cwd
            if git_branch:
                session.git_branch = git_branch
            if version:
                session.version = version

        session.segments.append(segment)

    # First user query across all segments
    for seg in session.segments:
        if seg.user_query:
            session.user_query = seg.user_query
            break

    session.total_usage = total_usage
    return session


def find_session_files(claude_dir: str | Path | None = None) -> list[Path]:
    """Find all JSONL session files in the Claude config directory."""
    if claude_dir is None:
        claude_dir = Path.home() / ".claude" / "projects"
    else:
        claude_dir = Path(claude_dir)

    if not claude_dir.exists():
        return []

    files = []
    for jsonl_file in claude_dir.rglob("*.jsonl"):
        # Skip subagent files
        if "subagents" in jsonl_file.parts:
            continue
        files.append(jsonl_file)

    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def find_conversation_files(slug: str, claude_dir: str | Path | None = None) -> list[Path]:
    """Find all JSONL files belonging to the same conversation (by slug).

    When context is cleared, a new session file is created but shares
    the same slug. This finds all files in that conversation chain.
    """
    all_files = find_session_files(claude_dir)
    matching = []

    for f in all_files:
        # Quick scan for slug without fully parsing
        with open(f) as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                    if record.get("slug") == slug:
                        matching.append(f)
                        break
                except (json.JSONDecodeError, KeyError):
                    continue

    return sorted(matching, key=lambda f: f.stat().st_mtime)
