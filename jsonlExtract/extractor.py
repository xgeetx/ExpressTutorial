"""
Heuristic-based extraction of structured activities from parsed sessions.

Extracts: files touched, tools used, shell commands, git operations,
errors, topics/concepts, and user intent — all without LLM calls.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .parser import Message, Session, TokenUsage, ToolUse, ToolResult


# --- Patterns for heuristic extraction ---

FILE_PATH_RE = re.compile(r'(?:/[\w.\-]+)+(?:\.\w+)?')
GIT_COMMIT_RE = re.compile(r'git\s+commit\s+-m\s+["\'](.+?)["\']', re.IGNORECASE)
GIT_BRANCH_RE = re.compile(r'git\s+(?:checkout|switch)\s+(?:-[bB]\s+)?(\S+)', re.IGNORECASE)
GIT_PUSH_RE = re.compile(r'git\s+push\s+(\S+)\s+(\S+)', re.IGNORECASE)
NPM_CMD_RE = re.compile(r'npm\s+(install|run|test|build|start)\s*(.*)', re.IGNORECASE)
ERROR_KEYWORDS = re.compile(
    r'\b(error|exception|traceback|failed|failure|cannot|unable to|'
    r'not found|permission denied|ENOENT|EACCES|SyntaxError|TypeError|'
    r'ReferenceError|ModuleNotFoundError|ImportError)\b',
    re.IGNORECASE
)
URL_RE = re.compile(r'https?://[^\s\'"<>]+')


@dataclass
class FileActivity:
    path: str
    operations: list[str] = field(default_factory=list)  # read, write, edit, glob, grep
    first_seen: str = ""
    last_seen: str = ""


@dataclass
class ShellCommand:
    command: str
    timestamp: str
    had_error: bool = False
    output_snippet: str = ""


@dataclass
class GitOperation:
    op_type: str  # commit, push, checkout, branch, etc.
    detail: str = ""
    timestamp: str = ""


@dataclass
class ErrorEvent:
    message: str
    source: str  # tool name or context
    timestamp: str = ""
    resolved: bool = False


@dataclass
class UserIntent:
    """A discrete user request/question extracted from user messages."""
    text: str
    timestamp: str
    message_uuid: str


@dataclass
class SegmentSummary:
    """Summary of a single context segment within a session."""
    segment_index: int
    session_id: str
    is_continuation: bool
    start_time: str = ""
    end_time: str = ""
    user_query: str = ""
    message_count: int = 0
    tool_call_count: int = 0
    files_touched: list[str] = field(default_factory=list)


@dataclass
class SessionExtract:
    """All heuristic-extracted information from a session."""
    session_id: str
    slug: str
    user_query: str
    start_time: str
    end_time: str
    cwd: str
    git_branch: str

    # Extracted data
    user_intents: list[UserIntent] = field(default_factory=list)
    files: dict[str, FileActivity] = field(default_factory=dict)
    shell_commands: list[ShellCommand] = field(default_factory=list)
    git_operations: list[GitOperation] = field(default_factory=list)
    errors: list[ErrorEvent] = field(default_factory=list)
    tools_used: Counter = field(default_factory=Counter)
    urls_referenced: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    models_used: Counter = field(default_factory=Counter)

    # Segments (context clears / continuations)
    segments: list[SegmentSummary] = field(default_factory=list)

    # Token usage
    total_usage: TokenUsage = field(default_factory=TokenUsage)

    # Stats
    message_count: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    tool_call_count: int = 0
    error_count: int = 0
    segment_count: int = 0


def extract_file_from_tool(tool: ToolUse) -> Optional[tuple[str, str]]:
    """Extract file path and operation type from a tool use."""
    inp = tool.input_data
    name = tool.tool_name

    if name == "Read":
        path = inp.get("file_path", "")
        return (path, "read") if path else None
    elif name == "Write":
        path = inp.get("file_path", "")
        return (path, "write") if path else None
    elif name == "Edit":
        path = inp.get("file_path", "")
        return (path, "edit") if path else None
    elif name == "Glob":
        pattern = inp.get("pattern", "")
        return (pattern, "glob_search") if pattern else None
    elif name == "Grep":
        path = inp.get("path", "")
        pattern = inp.get("pattern", "")
        return (path or pattern, "grep_search") if (path or pattern) else None
    elif name == "NotebookEdit":
        path = inp.get("notebook_path", "")
        return (path, "notebook_edit") if path else None
    return None


def extract_shell_command(tool: ToolUse) -> Optional[str]:
    """Extract command string from a Bash tool use."""
    if tool.tool_name == "Bash":
        return tool.input_data.get("command", "")
    return None


def extract_git_ops(command: str, timestamp: str) -> list[GitOperation]:
    """Extract git operations from a shell command."""
    ops = []
    if not command or "git" not in command.lower():
        return ops

    m = GIT_COMMIT_RE.search(command)
    if m:
        ops.append(GitOperation("commit", m.group(1), timestamp))

    m = GIT_BRANCH_RE.search(command)
    if m:
        ops.append(GitOperation("checkout", m.group(1), timestamp))

    m = GIT_PUSH_RE.search(command)
    if m:
        ops.append(GitOperation("push", f"{m.group(1)} {m.group(2)}", timestamp))

    if "git status" in command:
        ops.append(GitOperation("status", "", timestamp))

    if "git diff" in command:
        ops.append(GitOperation("diff", "", timestamp))

    if "git log" in command:
        ops.append(GitOperation("log", "", timestamp))

    return ops


def extract_topics_from_text(text: str) -> list[str]:
    """
    Heuristic topic extraction from text content.
    Finds technical terms, framework names, and concepts.
    """
    # Known tech terms to look for
    tech_terms = {
        "react", "vue", "angular", "express", "fastapi", "django", "flask",
        "typescript", "javascript", "python", "rust", "golang", "java",
        "docker", "kubernetes", "aws", "gcp", "azure", "terraform",
        "postgresql", "mysql", "mongodb", "redis", "sqlite",
        "graphql", "rest", "api", "websocket", "grpc",
        "authentication", "authorization", "oauth", "jwt", "cors",
        "testing", "unittest", "pytest", "jest", "cypress", "playwright",
        "ci/cd", "github actions", "deployment", "migration",
        "refactor", "debug", "optimize", "performance",
        "component", "middleware", "route", "endpoint", "model", "schema",
        "webpack", "vite", "rollup", "esbuild", "nextjs", "nuxt",
        "tailwind", "css", "html", "sass", "styled-components",
        "state management", "redux", "zustand", "context",
        "database", "query", "index", "transaction",
        "error handling", "logging", "monitoring",
        "security", "encryption", "hashing", "sanitization",
        "gemini", "openai", "anthropic", "llm", "embedding",
    }

    text_lower = text.lower()
    found = []
    for term in tech_terms:
        if term in text_lower:
            found.append(term)
    return found


def extract_topics_from_paths(paths: list[str]) -> list[str]:
    """Infer topics from file paths."""
    topics = []
    path_indicators = {
        "test": "testing",
        "spec": "testing",
        "__test__": "testing",
        "migration": "database migration",
        "middleware": "middleware",
        "route": "routing",
        "controller": "routing",
        "model": "data modeling",
        "schema": "data modeling",
        "component": "ui components",
        "hook": "react hooks",
        "store": "state management",
        "redux": "state management",
        "style": "styling",
        "css": "styling",
        "docker": "containerization",
        "ci": "ci/cd",
        "deploy": "deployment",
        "auth": "authentication",
        "config": "configuration",
        "util": "utilities",
        "helper": "utilities",
        "api": "api",
    }

    for path in paths:
        path_lower = path.lower()
        for indicator, topic in path_indicators.items():
            if indicator in path_lower and topic not in topics:
                topics.append(topic)

    # Infer language from extensions
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "react", ".tsx": "react", ".vue": "vue",
        ".rs": "rust", ".go": "golang", ".java": "java",
        ".rb": "ruby", ".php": "php", ".swift": "swift",
        ".sql": "sql", ".graphql": "graphql",
    }
    for path in paths:
        for ext, lang in ext_map.items():
            if path.endswith(ext) and lang not in topics:
                topics.append(lang)
                break

    return topics


def build_tool_result_map(messages: list[Message]) -> dict[str, ToolResult]:
    """Build a lookup from tool_use_id to its ToolResult."""
    result_map = {}
    for msg in messages:
        for tr in msg.tool_results:
            result_map[tr.tool_use_id] = tr
    return result_map


def extract_session(session: Session) -> SessionExtract:
    """Run all heuristic extractors on a parsed session."""
    extract = SessionExtract(
        session_id=session.session_id,
        slug=session.slug,
        user_query=session.user_query,
        start_time=session.start_time or "",
        end_time=session.end_time or "",
        cwd=session.cwd,
        git_branch=session.git_branch,
        total_usage=session.total_usage,
        segment_count=session.segment_count,
    )

    # Build segment summaries
    for seg in session.segments:
        seg_files = []
        seg_tool_count = 0
        for msg in seg.messages:
            if not msg.is_sidechain:
                for tool in msg.tool_uses:
                    seg_tool_count += 1
                    fi = extract_file_from_tool(tool)
                    if fi and fi[0].startswith("/"):
                        seg_files.append(fi[0])

        extract.segments.append(SegmentSummary(
            segment_index=seg.segment_index,
            session_id=seg.session_id,
            is_continuation=seg.is_continuation,
            start_time=seg.start_time or "",
            end_time=seg.end_time or "",
            user_query=seg.user_query,
            message_count=len(seg.messages),
            tool_call_count=seg_tool_count,
            files_touched=list(dict.fromkeys(seg_files)),
        ))

    result_map = build_tool_result_map(session.messages)
    all_text = []
    all_file_paths = []

    for msg in session.messages:
        # Skip sidechain (subagent) messages for the main extract
        if msg.is_sidechain:
            continue

        extract.message_count += 1
        if msg.role == "user":
            extract.user_message_count += 1
            for text in msg.text_blocks:
                if text.strip():
                    extract.user_intents.append(UserIntent(
                        text=text[:300],
                        timestamp=msg.timestamp,
                        message_uuid=msg.uuid,
                    ))
                    all_text.append(text)
        else:
            extract.assistant_message_count += 1
            for text in msg.text_blocks:
                all_text.append(text)
            # Track model usage
            if msg.model:
                extract.models_used[msg.model] += 1

        # Process tool uses
        for tool in msg.tool_uses:
            extract.tools_used[tool.tool_name] += 1
            extract.tool_call_count += 1

            # File tracking
            file_info = extract_file_from_tool(tool)
            if file_info:
                path, op = file_info
                if path and path.startswith("/"):
                    all_file_paths.append(path)
                    if path not in extract.files:
                        extract.files[path] = FileActivity(
                            path=path, first_seen=tool.timestamp
                        )
                    extract.files[path].operations.append(op)
                    extract.files[path].last_seen = tool.timestamp

            # Shell commands
            cmd = extract_shell_command(tool)
            if cmd:
                # Check if the result was an error
                result = result_map.get(tool.tool_id)
                had_error = False
                output_snippet = ""
                if result:
                    had_error = result.is_error
                    output_snippet = result.content[:200]

                extract.shell_commands.append(ShellCommand(
                    command=cmd,
                    timestamp=tool.timestamp,
                    had_error=had_error,
                    output_snippet=output_snippet,
                ))

                # Git operations
                git_ops = extract_git_ops(cmd, tool.timestamp)
                extract.git_operations.extend(git_ops)

        # Error detection in tool results
        for tr in msg.tool_results:
            if tr.is_error or ERROR_KEYWORDS.search(tr.content[:500]):
                extract.errors.append(ErrorEvent(
                    message=tr.content[:300],
                    source=tr.tool_use_id,
                    timestamp=tr.timestamp,
                ))
                extract.error_count += 1

    # Extract URLs from all text
    combined_text = " ".join(all_text)
    urls = URL_RE.findall(combined_text)
    extract.urls_referenced = list(dict.fromkeys(urls))[:50]  # Dedupe, limit

    # Topic extraction
    text_topics = extract_topics_from_text(combined_text)
    path_topics = extract_topics_from_paths(all_file_paths)
    all_topics = list(dict.fromkeys(text_topics + path_topics))
    extract.topics = all_topics

    return extract


def extract_to_dict(extract: SessionExtract) -> dict:
    """Serialize a SessionExtract to a JSON-friendly dict."""
    usage = extract.total_usage
    return {
        "session_id": extract.session_id,
        "slug": extract.slug,
        "user_query": extract.user_query,
        "start_time": extract.start_time,
        "end_time": extract.end_time,
        "cwd": extract.cwd,
        "git_branch": extract.git_branch,
        "user_intents": [
            {"text": i.text, "timestamp": i.timestamp}
            for i in extract.user_intents
        ],
        "files": {
            path: {
                "operations": fa.operations,
                "first_seen": fa.first_seen,
                "last_seen": fa.last_seen,
            }
            for path, fa in extract.files.items()
        },
        "shell_commands": [
            {
                "command": sc.command,
                "timestamp": sc.timestamp,
                "had_error": sc.had_error,
            }
            for sc in extract.shell_commands
        ],
        "git_operations": [
            {"type": go.op_type, "detail": go.detail, "timestamp": go.timestamp}
            for go in extract.git_operations
        ],
        "errors": [
            {"message": e.message, "source": e.source, "timestamp": e.timestamp}
            for e in extract.errors
        ],
        "tools_used": dict(extract.tools_used),
        "models_used": dict(extract.models_used),
        "urls_referenced": extract.urls_referenced,
        "topics": extract.topics,
        "segments": [
            {
                "segment_index": s.segment_index,
                "session_id": s.session_id,
                "is_continuation": s.is_continuation,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "user_query": s.user_query,
                "message_count": s.message_count,
                "tool_call_count": s.tool_call_count,
                "files_touched": s.files_touched,
            }
            for s in extract.segments
        ],
        "token_usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_tokens": usage.cache_creation_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "total_tokens": usage.total_tokens,
        },
        "stats": {
            "message_count": extract.message_count,
            "user_message_count": extract.user_message_count,
            "assistant_message_count": extract.assistant_message_count,
            "tool_call_count": extract.tool_call_count,
            "error_count": extract.error_count,
            "files_touched": len(extract.files),
            "segment_count": extract.segment_count,
        },
    }
