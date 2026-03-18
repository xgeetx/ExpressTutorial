"""
CLI entry point for jsonlExtract.

Usage:
    python -m jsonlExtract scan [--dir PATH]
    python -m jsonlExtract extract <session_id_or_path> [--summarize]
    python -m jsonlExtract extract-all [--dir PATH] [--summarize]
    python -m jsonlExtract search <query>
    python -m jsonlExtract narrative <session_id_or_path>
    python -m jsonlExtract summarize <session_id_or_path>
    python -m jsonlExtract graph <session_id_or_path> [--format dot|json]
    python -m jsonlExtract conversation <slug>
"""

import argparse
import json
import sys
from pathlib import Path

from .parser import parse_jsonl_file, find_session_files, find_conversation_files
from .extractor import extract_session, extract_to_dict
from .graph import build_activity_graph, get_session_narrative
from .summarizer import summarize_session, generate_search_keywords
from .index import SessionIndex, IndexEntry


def resolve_session_path(session_ref: str, claude_dir: str | None = None) -> Path | None:
    """Resolve a session ID or file path to an actual JSONL file."""
    # Direct path
    p = Path(session_ref)
    if p.exists() and p.suffix == ".jsonl":
        return p

    # Search by session ID or slug
    files = find_session_files(claude_dir)
    for f in files:
        if session_ref in f.stem:
            return f

    # Try matching by slug
    for f in files:
        session = parse_jsonl_file(f)
        if session.slug and session_ref in session.slug:
            return f

    return None


def _build_index_entry(extract, graph, narrative, keywords, summary=""):
    """Build an IndexEntry from extracted data."""
    usage = extract.total_usage
    return IndexEntry(
        session_id=extract.session_id,
        slug=extract.slug,
        user_query=extract.user_query,
        start_time=extract.start_time,
        end_time=extract.end_time,
        cwd=extract.cwd,
        git_branch=extract.git_branch,
        topics=extract.topics,
        keywords=keywords or [],
        files_touched=list(extract.files.keys()),
        tools_used=dict(extract.tools_used),
        narrative=narrative,
        summary=summary,
        stats={
            "message_count": extract.message_count,
            "user_message_count": extract.user_message_count,
            "assistant_message_count": extract.assistant_message_count,
            "tool_call_count": extract.tool_call_count,
            "error_count": extract.error_count,
            "files_touched": len(extract.files),
            "segment_count": extract.segment_count,
        },
        token_usage={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_tokens": usage.cache_creation_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "total_tokens": usage.total_tokens,
        },
        models_used=dict(extract.models_used),
        graph=graph.to_dict(),
        extract=extract_to_dict(extract),
    )


def cmd_scan(args):
    """List all available session files."""
    files = find_session_files(args.dir)
    if not files:
        print("No session files found.")
        return

    print(f"Found {len(files)} session(s):\n")
    for f in files:
        session = parse_jsonl_file(f)
        query_preview = session.user_query[:80] if session.user_query else "(no user query)"
        msg_count = len(session.messages)
        usage = session.total_usage

        print(f"  {session.session_id}")
        if session.slug:
            print(f"    Slug:     {session.slug}")
        print(f"    Time:     {session.start_time or 'unknown'}")
        print(f"    Branch:   {session.git_branch or 'unknown'}")
        print(f"    Messages: {msg_count}")
        if session.segment_count > 1:
            print(f"    Segments: {session.segment_count} (compacted {session.segment_count - 1}x)")
        if session.compactions:
            print(f"    Compactions: {len(session.compactions)} ({', '.join(c.trigger for c in session.compactions)})")
        if usage.total_tokens > 0:
            print(f"    Tokens:   {usage.total_tokens:,} (in:{usage.input_tokens:,} out:{usage.output_tokens:,} cache:{usage.cache_read_tokens:,})")
        print(f"    Query:    {query_preview}")
        print()


def cmd_extract(args):
    """Extract and index a single session."""
    path = resolve_session_path(args.session, args.dir)
    if not path:
        print(f"Session not found: {args.session}", file=sys.stderr)
        sys.exit(1)

    session = parse_jsonl_file(path)
    extract = extract_session(session)
    graph = build_activity_graph(extract)
    narrative = get_session_narrative(extract, graph)
    keywords = generate_search_keywords(extract)

    summary = ""
    if args.summarize:
        result = summarize_session(extract, graph, style="brief")
        if result:
            summary = result

    # Index it
    index = SessionIndex()
    index.add(_build_index_entry(extract, graph, narrative, keywords, summary))

    if args.json:
        print(json.dumps(extract_to_dict(extract), indent=2))
    else:
        print(narrative)
        if summary:
            print(f"\n--- Gemini Summary ---\n{summary}")
        print(f"\nIndexed. Total sessions in index: {index.count}")


def cmd_extract_all(args):
    """Extract and index all sessions."""
    files = find_session_files(args.dir)
    if not files:
        print("No session files found.")
        return

    index = SessionIndex()
    for i, path in enumerate(files, 1):
        try:
            session = parse_jsonl_file(path)
            extract = extract_session(session)
            graph = build_activity_graph(extract)
            narrative = get_session_narrative(extract, graph)
            keywords = generate_search_keywords(extract)

            summary = ""
            if args.summarize:
                result = summarize_session(extract, graph, style="brief")
                if result:
                    summary = result

            index.add(_build_index_entry(extract, graph, narrative, keywords, summary))
            query_preview = extract.user_query[:60] if extract.user_query else "(no query)"
            print(f"  [{i}/{len(files)}] {extract.session_id[:20]}... - {query_preview}")
        except Exception as e:
            print(f"  [{i}/{len(files)}] ERROR: {path.name} - {e}", file=sys.stderr)

    print(f"\nDone. Indexed {index.count} sessions.")


def cmd_search(args):
    """Search indexed sessions."""
    index = SessionIndex()
    if index.count == 0:
        print("No sessions indexed yet. Run 'extract-all' first.")
        return

    results = index.search(args.query, max_results=args.limit)
    if not results:
        print(f"No results for: {args.query}")
        return

    print(f"Found {len(results)} result(s) for '{args.query}':\n")
    for r in results:
        print(f"  {r['session_id']}")
        if r.get("slug"):
            print(f"    Slug:    {r['slug']}")
        print(f"    Time:    {r.get('start_time', 'unknown')}")
        print(f"    Branch:  {r.get('git_branch', 'unknown')}")
        print(f"    Query:   {r.get('user_query', '')[:80]}")
        if r.get("summary"):
            print(f"    Summary: {r['summary'][:120]}")
        tu = r.get("token_usage", {})
        if tu.get("total_tokens"):
            print(f"    Tokens:  {tu['total_tokens']:,}")
        print(f"    Topics:  {', '.join(r.get('topics', []))}")
        print()


def cmd_narrative(args):
    """Print the heuristic narrative for a session."""
    # Try index first
    index = SessionIndex()
    entry = index.get(args.session)
    if entry and entry.get("narrative"):
        print(entry["narrative"])
        return

    # Fall back to parsing
    path = resolve_session_path(args.session, args.dir)
    if not path:
        print(f"Session not found: {args.session}", file=sys.stderr)
        sys.exit(1)

    session = parse_jsonl_file(path)
    extract = extract_session(session)
    graph = build_activity_graph(extract)
    print(get_session_narrative(extract, graph))


def cmd_summarize(args):
    """Generate a Gemini Flash summary for a session."""
    path = resolve_session_path(args.session, args.dir)
    if not path:
        print(f"Session not found: {args.session}", file=sys.stderr)
        sys.exit(1)

    session = parse_jsonl_file(path)
    extract = extract_session(session)
    graph = build_activity_graph(extract)

    summary = summarize_session(extract, graph, style=args.style)
    if summary:
        print(summary)
    else:
        print("Gemini unavailable. Set GEMINI_API_KEY or install google-generativeai.")
        print("\nFalling back to heuristic narrative:\n")
        print(get_session_narrative(extract, graph))


def cmd_graph(args):
    """Output the activity graph."""
    path = resolve_session_path(args.session, args.dir)
    if not path:
        print(f"Session not found: {args.session}", file=sys.stderr)
        sys.exit(1)

    session = parse_jsonl_file(path)
    extract = extract_session(session)
    graph = build_activity_graph(extract)

    if args.format == "json":
        print(json.dumps(graph.to_dict(), indent=2))
    elif args.format == "dot":
        # DOT format for Graphviz
        print("digraph session {")
        print("  rankdir=LR;")
        type_colors = {
            "intent": "#4CAF50", "file": "#2196F3", "error": "#F44336",
            "git_op": "#FF9800", "topic": "#9C27B0", "tool": "#607D8B",
        }
        for node in graph.nodes.values():
            color = type_colors.get(node.node_type, "#999")
            label = node.label.replace('"', '\\"')[:50]
            print(f'  "{node.id}" [label="{label}" style=filled fillcolor="{color}" fontcolor=white];')
        for edge in graph.edges:
            print(f'  "{edge.source}" -> "{edge.target}" [label="{edge.relation}"];')
        print("}")
    else:
        # Simple text format
        print(f"Nodes ({len(graph.nodes)}):")
        for n in graph.nodes.values():
            print(f"  [{n.node_type}] {n.label}")
        print(f"\nEdges ({len(graph.edges)}):")
        for e in graph.edges:
            print(f"  {e.source} --{e.relation}--> {e.target}")


def cmd_conversation(args):
    """Show all sessions belonging to the same conversation (by slug)."""
    files = find_conversation_files(args.slug, args.dir)
    if not files:
        print(f"No sessions found for conversation slug: {args.slug}")
        return

    print(f"Conversation '{args.slug}' spans {len(files)} session file(s):\n")
    total_tokens = 0
    total_messages = 0

    for f in files:
        session = parse_jsonl_file(f)
        extract = extract_session(session)
        query_preview = extract.user_query[:80] if extract.user_query else "(no query)"
        usage = session.total_usage
        total_tokens += usage.total_tokens
        total_messages += extract.message_count

        print(f"  {session.session_id}")
        print(f"    Time:     {session.start_time} -> {session.end_time}")
        print(f"    Segments: {session.segment_count}")
        print(f"    Messages: {extract.message_count}")
        if usage.total_tokens > 0:
            print(f"    Tokens:   {usage.total_tokens:,}")
        print(f"    Query:    {query_preview}")
        print()

    print(f"Total across conversation: {total_messages} messages, {total_tokens:,} tokens")


def main():
    parser = argparse.ArgumentParser(
        prog="jsonlExtract",
        description="Scan, extract, and summarize Claude Code session logs.",
    )
    parser.add_argument("--dir", default=None, help="Claude projects directory (default: ~/.claude/projects)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan
    subparsers.add_parser("scan", help="List all available session files")

    # extract
    p_extract = subparsers.add_parser("extract", help="Extract and index a single session")
    p_extract.add_argument("session", help="Session ID, slug, or path to JSONL file")
    p_extract.add_argument("--summarize", action="store_true", help="Generate Gemini summary")
    p_extract.add_argument("--json", action="store_true", help="Output raw JSON extract")

    # extract-all
    p_all = subparsers.add_parser("extract-all", help="Extract and index all sessions")
    p_all.add_argument("--summarize", action="store_true", help="Generate Gemini summaries")

    # search
    p_search = subparsers.add_parser("search", help="Search indexed sessions")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10, help="Max results")

    # narrative
    p_narr = subparsers.add_parser("narrative", help="Print heuristic narrative for a session")
    p_narr.add_argument("session", help="Session ID, slug, or path to JSONL file")

    # summarize
    p_sum = subparsers.add_parser("summarize", help="Generate Gemini Flash summary")
    p_sum.add_argument("session", help="Session ID, slug, or path to JSONL file")
    p_sum.add_argument("--style", choices=["brief", "detailed", "bullets"], default="brief")

    # graph
    p_graph = subparsers.add_parser("graph", help="Output activity graph")
    p_graph.add_argument("session", help="Session ID, slug, or path to JSONL file")
    p_graph.add_argument("--format", choices=["json", "dot", "text"], default="text")

    # conversation
    p_conv = subparsers.add_parser("conversation", help="Show all sessions in a conversation (by slug)")
    p_conv.add_argument("slug", help="Conversation slug (e.g., 'zesty-singing-newell')")

    args = parser.parse_args()

    commands = {
        "scan": cmd_scan,
        "extract": cmd_extract,
        "extract-all": cmd_extract_all,
        "search": cmd_search,
        "narrative": cmd_narrative,
        "summarize": cmd_summarize,
        "graph": cmd_graph,
        "conversation": cmd_conversation,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
