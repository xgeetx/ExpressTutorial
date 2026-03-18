"""
Build an activity graph linking concepts, files, and actions from a session.

The graph connects:
  - User intents -> files they caused to be touched
  - Files -> other files co-accessed in the same action sequence
  - Actions -> outcomes (errors, git ops, results)
  - Topics -> files and intents they appear in

This creates a lightweight "what happened and why" map without needing an LLM.
"""

import networkx as nx
from collections import defaultdict
from dataclasses import dataclass, field

from .extractor import SessionExtract


@dataclass
class ActivityNode:
    id: str
    label: str
    node_type: str  # intent, file, tool, error, git_op, topic
    timestamp: str = ""
    detail: str = ""


@dataclass
class ActivityEdge:
    source: str
    target: str
    relation: str  # caused, touched, co_accessed, resolved_by, tagged
    weight: float = 1.0


@dataclass
class ActivityGraph:
    nodes: dict[str, ActivityNode] = field(default_factory=dict)
    edges: list[ActivityEdge] = field(default_factory=list)

    def add_node(self, node: ActivityNode):
        self.nodes[node.id] = node

    def add_edge(self, edge: ActivityEdge):
        self.edges.append(edge)

    def to_networkx(self) -> nx.DiGraph:
        G = nx.DiGraph()
        for nid, node in self.nodes.items():
            G.add_node(nid, **{
                "label": node.label,
                "type": node.node_type,
                "timestamp": node.timestamp,
                "detail": node.detail,
            })
        for edge in self.edges:
            G.add_edge(edge.source, edge.target,
                       relation=edge.relation, weight=edge.weight)
        return G

    def to_dict(self) -> dict:
        return {
            "nodes": [
                {
                    "id": n.id,
                    "label": n.label,
                    "type": n.node_type,
                    "timestamp": n.timestamp,
                    "detail": n.detail[:200] if n.detail else "",
                }
                for n in self.nodes.values()
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "relation": e.relation,
                    "weight": e.weight,
                }
                for e in self.edges
            ],
        }


def build_activity_graph(extract: SessionExtract) -> ActivityGraph:
    """
    Build an activity graph from extracted session data.

    Strategy:
    1. Each user intent becomes a node
    2. Files become nodes, linked to the intent that triggered them
    3. Errors link back to the file/command that caused them
    4. Git operations link to the files they affected
    5. Topics link to intents and files they appear in
    6. Co-accessed files (within a time window) get co_accessed edges
    """
    graph = ActivityGraph()

    # --- Intent nodes ---
    intent_nodes = []
    for i, intent in enumerate(extract.user_intents):
        nid = f"intent_{i}"
        graph.add_node(ActivityNode(
            id=nid,
            label=intent.text[:80],
            node_type="intent",
            timestamp=intent.timestamp,
            detail=intent.text,
        ))
        intent_nodes.append((nid, intent.timestamp))

    # --- File nodes and intent->file edges ---
    # Assign each file to the nearest preceding intent
    file_to_intent = {}
    for path, fa in extract.files.items():
        fid = f"file:{path}"
        short_name = path.rsplit("/", 1)[-1] if "/" in path else path
        graph.add_node(ActivityNode(
            id=fid,
            label=short_name,
            node_type="file",
            timestamp=fa.first_seen,
            detail=f"ops: {', '.join(fa.operations)}",
        ))

        # Find the nearest preceding intent
        best_intent = None
        for intent_id, intent_ts in intent_nodes:
            if intent_ts <= fa.first_seen:
                best_intent = intent_id
            else:
                break
        if best_intent:
            graph.add_edge(ActivityEdge(
                source=best_intent,
                target=fid,
                relation="touched",
            ))
            file_to_intent[path] = best_intent

    # --- Co-accessed file edges ---
    # Files whose operations overlap in time are likely related
    file_list = list(extract.files.items())
    for i, (path_a, fa_a) in enumerate(file_list):
        for path_b, fa_b in file_list[i + 1:]:
            # If they share an intent, they're co-accessed
            if (file_to_intent.get(path_a) == file_to_intent.get(path_b)
                    and file_to_intent.get(path_a) is not None):
                graph.add_edge(ActivityEdge(
                    source=f"file:{path_a}",
                    target=f"file:{path_b}",
                    relation="co_accessed",
                    weight=0.5,
                ))

    # --- Error nodes ---
    for i, error in enumerate(extract.errors):
        eid = f"error_{i}"
        graph.add_node(ActivityNode(
            id=eid,
            label=error.message[:60],
            node_type="error",
            timestamp=error.timestamp,
            detail=error.message,
        ))
        # Link error to the nearest file or intent
        best_intent = None
        for intent_id, intent_ts in intent_nodes:
            if intent_ts <= error.timestamp:
                best_intent = intent_id
            else:
                break
        if best_intent:
            graph.add_edge(ActivityEdge(
                source=best_intent,
                target=eid,
                relation="caused",
            ))

    # --- Git operation nodes ---
    for i, git_op in enumerate(extract.git_operations):
        gid = f"git_{i}"
        label = f"git {git_op.op_type}"
        if git_op.detail:
            label += f": {git_op.detail[:40]}"
        graph.add_node(ActivityNode(
            id=gid,
            label=label,
            node_type="git_op",
            timestamp=git_op.timestamp,
            detail=git_op.detail,
        ))
        # Link to nearest intent
        best_intent = None
        for intent_id, intent_ts in intent_nodes:
            if intent_ts <= git_op.timestamp:
                best_intent = intent_id
            else:
                break
        if best_intent:
            graph.add_edge(ActivityEdge(
                source=best_intent,
                target=gid,
                relation="caused",
            ))

    # --- Topic nodes ---
    # Link topics to intents and files
    for topic in extract.topics:
        tid = f"topic:{topic}"
        graph.add_node(ActivityNode(
            id=tid,
            label=topic,
            node_type="topic",
        ))
        # Link to intents that mention this topic
        topic_lower = topic.lower()
        for i, intent in enumerate(extract.user_intents):
            if topic_lower in intent.text.lower():
                graph.add_edge(ActivityEdge(
                    source=f"intent_{i}",
                    target=tid,
                    relation="tagged",
                    weight=0.3,
                ))
        # Link to files whose paths suggest this topic
        for path in extract.files:
            if topic_lower in path.lower():
                graph.add_edge(ActivityEdge(
                    source=f"file:{path}",
                    target=tid,
                    relation="tagged",
                    weight=0.3,
                ))

    return graph


def get_session_narrative(extract: SessionExtract, graph: ActivityGraph) -> str:
    """
    Generate a plain-text narrative of what happened in the session,
    purely from heuristics (no LLM needed).
    """
    lines = []

    # Opening
    lines.append(f"Session: {extract.session_id}")
    if extract.start_time:
        lines.append(f"Time: {extract.start_time} -> {extract.end_time}")
    if extract.cwd:
        lines.append(f"Working directory: {extract.cwd}")
    if extract.git_branch:
        lines.append(f"Branch: {extract.git_branch}")
    lines.append("")

    # User's goal
    if extract.user_query:
        lines.append(f"Initial request: {extract.user_query[:200]}")
        lines.append("")

    # Activity summary
    if extract.user_intents:
        lines.append("User requests:")
        for i, intent in enumerate(extract.user_intents, 1):
            lines.append(f"  {i}. {intent.text[:120]}")
        lines.append("")

    # Files touched
    if extract.files:
        write_files = [p for p, f in extract.files.items()
                       if any(op in ("write", "edit", "notebook_edit") for op in f.operations)]
        read_files = [p for p, f in extract.files.items()
                      if all(op == "read" for op in f.operations)]
        if write_files:
            lines.append(f"Files modified ({len(write_files)}):")
            for f in write_files[:15]:
                short = f.rsplit("/", 1)[-1]
                ops = extract.files[f].operations
                lines.append(f"  - {short} ({', '.join(set(ops))})")
            lines.append("")
        if read_files:
            lines.append(f"Files read-only ({len(read_files)}):")
            for f in read_files[:10]:
                short = f.rsplit("/", 1)[-1]
                lines.append(f"  - {short}")
            lines.append("")

    # Git operations
    if extract.git_operations:
        lines.append("Git operations:")
        for op in extract.git_operations:
            detail = f" - {op.detail}" if op.detail else ""
            lines.append(f"  - {op.op_type}{detail}")
        lines.append("")

    # Errors
    if extract.errors:
        lines.append(f"Errors encountered ({extract.error_count}):")
        for err in extract.errors[:5]:
            lines.append(f"  - {err.message[:100]}")
        lines.append("")

    # Topics
    if extract.topics:
        lines.append(f"Topics: {', '.join(extract.topics)}")
        lines.append("")

    # Stats
    lines.append("Stats:")
    lines.append(f"  Messages: {extract.message_count} ({extract.user_message_count} user, {extract.assistant_message_count} assistant)")
    lines.append(f"  Tool calls: {extract.tool_call_count}")
    lines.append(f"  Files touched: {len(extract.files)}")
    if extract.tools_used:
        top_tools = extract.tools_used.most_common(5)
        lines.append(f"  Top tools: {', '.join(f'{t}({n})' for t, n in top_tools)}")

    return "\n".join(lines)
