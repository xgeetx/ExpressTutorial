"""
Parse Claude Code JSONL session logs into structured data.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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


@dataclass
class Session:
    session_id: str
    messages: list[Message] = field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    cwd: str = ""
    git_branch: str = ""
    version: str = ""
    user_query: str = ""


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


def parse_jsonl_file(filepath: str | Path) -> Session:
    """Parse a single JSONL session file into a Session object."""
    filepath = Path(filepath)
    session = Session(session_id=filepath.stem)
    lines = filepath.read_text().strip().split("\n")

    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        record_type = record.get("type", "")
        timestamp = record.get("timestamp", "")

        # Skip queue operations and other non-message records
        if record_type not in ("user", "assistant"):
            continue

        uuid = record.get("uuid", "")
        parent_uuid = record.get("parentUuid")
        is_sidechain = record.get("isSidechain", False)
        cwd = record.get("cwd", "")
        git_branch = record.get("gitBranch", "")
        version = record.get("version", "")
        session_id = record.get("sessionId", "")

        msg_data = record.get("message", {})
        role = msg_data.get("role", record_type)
        content = msg_data.get("content", "")

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
            session_id=session_id,
        )
        session.messages.append(msg)

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

        # Capture first user message as the session query
        if role == "user" and not session.user_query and text_blocks:
            session.user_query = text_blocks[0][:500]

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
