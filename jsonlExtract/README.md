# jsonlExtract

Scan, extract, and summarize Claude Code session logs (JSONL format) for indexing and later retrieval.

## Design Philosophy

- **Heuristics first**: Extracts files, tools, git ops, errors, topics, and user intents without any LLM calls
- **Gemini Flash optional**: Only used for summary generation and keyword enhancement when `GEMINI_API_KEY` is set
- **Activity graph**: Links user intents to files, errors, git operations, and topics — a lightweight causality map
- **Searchable index**: All extracted data is stored in a JSON index for fast keyword search

## Install

```bash
pip install networkx
# Optional, for LLM summaries:
pip install google-generativeai
export GEMINI_API_KEY=your_key_here
```

## Usage

```bash
# List all sessions found in ~/.claude/projects/
python -m jsonlExtract scan

# Extract and index a single session (by ID prefix or full path)
python -m jsonlExtract extract 7d5726b8
python -m jsonlExtract extract /path/to/session.jsonl --json

# Extract all sessions at once
python -m jsonlExtract extract-all

# Search indexed sessions
python -m jsonlExtract search "authentication bug"
python -m jsonlExtract search "react component" --limit 5

# View heuristic narrative (no LLM needed)
python -m jsonlExtract narrative 7d5726b8

# Generate Gemini Flash summary (requires API key)
python -m jsonlExtract summarize 7d5726b8 --style bullets

# Export activity graph
python -m jsonlExtract graph 7d5726b8 --format json
python -m jsonlExtract graph 7d5726b8 --format dot > session.dot
```

## What Gets Extracted (Heuristics Only)

| Category | Details |
|----------|---------|
| **User intents** | Each user message, timestamped |
| **Files touched** | Path, operations (read/write/edit/glob/grep), first/last seen |
| **Shell commands** | Full command, error status, output snippet |
| **Git operations** | Commits, pushes, checkouts, diffs, etc. |
| **Errors** | Error messages from tool results, with source |
| **Tools used** | Frequency count of each tool (Bash, Read, Write, etc.) |
| **URLs** | All URLs referenced in the session |
| **Topics** | Inferred from text content and file paths |

## Activity Graph

The graph connects concepts for "what did I do" queries:

```
[User Intent] --touched--> [File]
[User Intent] --caused-->  [Error]
[User Intent] --caused-->  [Git Op]
[User Intent] --tagged-->  [Topic]
[File]        --co_accessed--> [File]
[File]        --tagged-->  [Topic]
```

Export as DOT for Graphviz visualization, or JSON for programmatic use.

## Index

Extracted data is stored at `~/.claude/jsonl_extract/session_index.json`. Each session entry includes the full extract, narrative, keywords, activity graph, and optional LLM summary.

## Architecture

```
jsonlExtract/
  parser.py      # JSONL -> structured Session/Message objects
  extractor.py   # Heuristic extraction of files, tools, errors, topics
  graph.py       # Activity graph builder + narrative generator
  summarizer.py  # Optional Gemini Flash summarization
  index.py       # JSON-based session index with keyword search
  __main__.py    # CLI entry point
```
