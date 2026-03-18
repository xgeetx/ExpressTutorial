"""
Optional Gemini Flash summarizer for session extracts.

Only called when heuristic narrative isn't enough — generates a concise
"what did I do" summary using Gemini 2.0 Flash (cheap, fast).

Requires: GEMINI_API_KEY environment variable.
"""

import os
from typing import Optional

from .extractor import SessionExtract, extract_to_dict
from .graph import ActivityGraph, get_session_narrative


def _get_gemini_client():
    """Lazy-load the Gemini client."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        return genai.GenerativeModel("gemini-2.0-flash")
    except ImportError:
        print("Warning: google-generativeai not installed. Run: pip install google-generativeai")
        return None


def summarize_session(
    extract: SessionExtract,
    graph: ActivityGraph,
    style: str = "brief",
) -> Optional[str]:
    """
    Generate an LLM summary of the session using Gemini Flash.

    Args:
        extract: The heuristic extraction
        graph: The activity graph
        style: "brief" (2-3 sentences), "detailed" (paragraph), or "bullets"

    Returns:
        Summary string, or None if Gemini is unavailable
    """
    model = _get_gemini_client()
    if model is None:
        return None

    # Build context from the heuristic narrative (cheaper than sending raw logs)
    narrative = get_session_narrative(extract, graph)

    style_instructions = {
        "brief": "Respond with 2-3 sentences summarizing what was accomplished.",
        "detailed": "Respond with a short paragraph covering the goal, key actions, and outcome.",
        "bullets": "Respond with 3-7 bullet points covering the main activities and outcomes.",
    }

    prompt = f"""You are summarizing a Claude Code AI assistant session log.
Given the extracted session data below, {style_instructions.get(style, style_instructions["brief"])}

Focus on:
- What the user wanted to accomplish
- What was actually done (files created/modified, commands run)
- Whether it succeeded or had issues
- Key technical topics/decisions

Session data:
{narrative}

User intents (chronological):
{chr(10).join(f'- {i.text[:150]}' for i in extract.user_intents[:10])}

Summary:"""

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"[Gemini summarization failed: {e}]"


def generate_search_keywords(extract: SessionExtract) -> Optional[list[str]]:
    """
    Use Gemini to generate search keywords for indexing this session.
    Falls back to heuristic keywords if Gemini is unavailable.
    """
    # Heuristic fallback keywords (always available)
    keywords = list(extract.topics)
    if extract.git_branch:
        keywords.append(extract.git_branch)
    for intent in extract.user_intents:
        # Extract key nouns/verbs heuristically
        words = intent.text.lower().split()
        skip = {"i", "a", "the", "to", "and", "or", "is", "it", "in", "on", "for",
                "with", "this", "that", "my", "me", "do", "can", "you", "we",
                "want", "need", "please", "just", "some", "here", "there"}
        keywords.extend(w for w in words if len(w) > 3 and w not in skip)

    # Dedupe and limit
    seen = set()
    unique = []
    for kw in keywords:
        if kw.lower() not in seen:
            seen.add(kw.lower())
            unique.append(kw)
    heuristic_keywords = unique[:30]

    # Try Gemini enhancement
    model = _get_gemini_client()
    if model is None:
        return heuristic_keywords

    prompt = f"""Given this coding session summary, generate 10-20 search keywords
that someone might use to find this session later. Include technical terms,
action verbs, and concepts. Return one keyword per line, nothing else.

Session: {extract.user_query[:200]}
Topics: {', '.join(extract.topics[:15])}
Files: {', '.join(list(extract.files.keys())[:10])}
Tools: {', '.join(f'{k}({v})' for k, v in extract.tools_used.most_common(5))}"""

    try:
        response = model.generate_content(prompt)
        llm_keywords = [
            line.strip().strip("- ").lower()
            for line in response.text.strip().split("\n")
            if line.strip()
        ]
        # Merge with heuristic keywords
        for kw in llm_keywords:
            if kw and kw not in seen:
                seen.add(kw)
                heuristic_keywords.append(kw)
        return heuristic_keywords[:40]
    except Exception:
        return heuristic_keywords
