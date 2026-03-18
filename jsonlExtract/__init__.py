"""
jsonlExtract — Scan, extract, and summarize Claude Code session logs.

Usage:
    python -m jsonlExtract scan                    # List all sessions
    python -m jsonlExtract extract <session_id>    # Extract one session
    python -m jsonlExtract extract-all             # Extract all sessions
    python -m jsonlExtract search <query>          # Search indexed sessions
    python -m jsonlExtract narrative <session_id>  # Print session narrative
    python -m jsonlExtract summarize <session_id>  # Gemini Flash summary
"""
