"""Reflect engine: generate a structured reflection of the user's Claude activity.

Fully data-driven — no hardcoded topics. Extracts themes, patterns, and insights
from whatever the user's memory files, transcripts, and history actually contain.

Uses the same memory schema as ``stores.read_memory()``:
  - ``claude_md``: user/project CLAUDE.md files
  - ``auto_memory``: per-project auto-memory files
  - ``agent_memory``: per-agent memory files
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths, scanner, stores


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset("""
a an the and or but if then else when at by for with about into through during before after
above below to from up down in out on off over under again further once here there all any
both each few more most other some such no nor not only own same so than too very can will
just should now i me my we our you your he him his she her it its they them their this that
these those am is are was were be been being have has had having do does did doing would
could ought as of until while because until against between through during before after
above below to from up down in out on off over under again further once here there
""".split())

_MIN_FREQ = 3
_MIN_LEN = 3


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase word tokens."""
    return re.findall(r"[a-z][a-z0-9\-_]{2,}", text.lower())


def _extract_terms(text: str, top_n: int = 40) -> list[tuple[str, int]]:
    """Extract the most frequent meaningful terms from text."""
    tokens = _tokenize(text)
    filtered = [t for t in tokens if t not in _STOPWORDS and len(t) >= _MIN_LEN]
    counts = Counter(filtered)
    return [(term, freq) for term, freq in counts.most_common(top_n * 3) if freq >= _MIN_FREQ][:top_n]


def _extract_phrases(text: str, top_n: int = 20) -> list[tuple[str, int]]:
    """Extract frequent bigrams (two-word phrases) from text."""
    tokens = _tokenize(text)
    filtered = [t for t in tokens if t not in _STOPWORDS and len(t) >= _MIN_LEN]
    bigrams = [f"{filtered[i]} {filtered[i+1]}" for i in range(len(filtered) - 1)]
    counts = Counter(bigrams)
    return [(phrase, freq) for phrase, freq in counts.most_common(top_n * 2) if freq >= _MIN_FREQ][:top_n]


# ---------------------------------------------------------------------------
# Topic clustering
# ---------------------------------------------------------------------------


def _cluster_topics(terms: list[tuple[str, int]], phrases: list[tuple[str, int]]) -> list[dict]:
    """Cluster extracted terms and phrases into coherent topics."""
    if not terms and not phrases:
        return []

    term_cooccurrence: dict[str, Counter] = {}
    for phrase, freq in phrases:
        words = phrase.split()
        for i, w1 in enumerate(words):
            if w1 not in term_cooccurrence:
                term_cooccurrence[w1] = Counter()
            for w2 in words[i+1:]:
                term_cooccurrence[w1][w2] += freq

    topics = []
    used_terms: set[str] = set()

    for term, freq in terms:
        if term in used_terms:
            continue
        related = []
        if term in term_cooccurrence:
            for other, co_freq in term_cooccurrence[term].most_common(5):
                if other not in used_terms and co_freq >= _MIN_FREQ:
                    related.append(other)
                    used_terms.add(other)

        if related:
            topic_name = _title_case(term)
            if related[0] != term:
                topic_name = f"{_title_case(term)} / {_title_case(related[0])}"
        else:
            topic_name = _title_case(term)

        topics.append({
            "name": topic_name,
            "terms": [term] + related[:4],
            "frequency": freq,
            "description": _describe_topic(term, related[:3], phrases),
        })
        used_terms.add(term)

        if len(topics) >= 8:
            break

    if len(topics) < 3:
        for phrase, freq in phrases:
            words = phrase.split()
            if all(w not in used_terms for w in words):
                topics.append({
                    "name": _title_case(phrase),
                    "terms": words[:3],
                    "frequency": freq,
                    "description": _describe_topic(words[0], words[1:3], phrases),
                })
                used_terms.update(words)
                if len(topics) >= 5:
                    break

    return topics


def _title_case(text: str) -> str:
    """Convert a term to title case."""
    return " ".join(w.capitalize() for w in text.replace("-", " ").replace("/", " ").split())


def _describe_topic(seed: str, related: list[str], phrases: list[tuple[str, int]]) -> str:
    """Generate a short description for a topic from its terms and phrases."""
    relevant = [p for p, _ in phrases if seed in p.split()]
    if relevant:
        return f"Centered on '{relevant[0]}'"
    if related:
        return f"Involves {', '.join(related[:3])}"
    return ""


# ---------------------------------------------------------------------------
# Insight generation
# ---------------------------------------------------------------------------


def _generate_insights(memory: dict, history: dict, transcripts: list, topics: list[dict]) -> list[str]:
    """Generate insights from observed patterns in the data."""
    insights = []

    claude_md_count = len(memory.get("claude_md", []))
    auto_memory_count = sum(len(p.get("files", [])) for p in memory.get("auto_memory", []))
    agent_memory_count = sum(len(a.get("files", [])) for a in memory.get("agent_memory", []))

    if claude_md_count > 0:
        scopes = [item.get("scope", "unknown") for item in memory.get("claude_md", [])]
        insights.append(f"Maintains {claude_md_count} CLAUDE.md file(s) across {len(set(scopes))} scope(s).")
    if auto_memory_count > 0:
        proj_count = len(memory.get("auto_memory", []))
        insights.append(f"Auto-memory captured {auto_memory_count} file(s) across {proj_count} project(s).")
    if agent_memory_count > 0:
        agent_count = len(memory.get("agent_memory", []))
        insights.append(f"Agent memory holds {agent_memory_count} file(s) across {agent_count} agent(s).")

    if len(topics) >= 5:
        insights.append(f"Work spans {len(topics)} distinct themes — from {topics[0]['name'].lower()} to {topics[-1]['name'].lower()}.")
    elif len(topics) >= 3:
        names = ", ".join(t["name"] for t in topics[:3])
        insights.append(f"Work concentrates on {len(topics)} main areas: {names}.")

    total_entries = history.get("total", 0)
    if total_entries > 100:
        insights.append(f"Made {total_entries:,} prompt entries — a high-velocity workflow.")
    elif total_entries > 0:
        insights.append(f"Made {total_entries} prompt entries in the recorded period.")

    if len(transcripts) > 50:
        insights.append(f"{len(transcripts)} session transcripts found — extensive conversation history.")
    elif len(transcripts) > 0:
        insights.append(f"{len(transcripts)} session transcripts found.")

    projects = set()
    for t in transcripts:
        if t.project_dir:
            projects.add(t.project_dir)
    if len(projects) > 5:
        insights.append(f"Work spans {len(projects)} distinct project directories.")
    elif len(projects) > 0:
        insights.append(f"Work spans {len(projects)} project directory(ies).")

    pasted = history.get("pasted", 0)
    if pasted > 0:
        insights.append(f"{pasted} entr{'y has' if pasted == 1 else 'ies have'} pasted content — large context inputs.")

    return insights


def _generate_skills(memory: dict, history: dict, transcripts: list, topics: list[dict]) -> list[dict]:
    """Generate skills/fluency observations based on observed data patterns."""
    skills = []

    projects = set()
    for t in transcripts:
        if t.project_dir:
            projects.add(t.project_dir)
    if len(projects) >= 3:
        skills.append({
            "name": "Multi-project fluency",
            "statement": "Work is distributed across multiple projects and domains.",
            "evidence": f"Active work in {len(projects)} distinct project directories, spanning {len(topics)} theme(s).",
        })

    auto_memory_count = sum(len(p.get("files", [])) for p in memory.get("auto_memory", []))
    if auto_memory_count > 5:
        skills.append({
            "name": "Context-rich prompting",
            "statement": "Prompts carry rich domain context.",
            "evidence": f"{auto_memory_count} auto-memory files capture specialised project knowledge.",
        })

    if len(topics) >= 4:
        skills.append({
            "name": "Cross-domain thinking",
            "statement": "Work spans diverse domains and can context-switch.",
            "evidence": f"Themes range from {topics[0]['name']} to {topics[-1]['name']}.",
        })

    total_entries = history.get("total", 0)
    if total_entries > 50 or len(transcripts) > 20:
        skills.append({
            "name": "Sustained engagement",
            "statement": "Extensive, detailed work over time.",
            "evidence": f"{len(transcripts)} transcripts and {total_entries:,} history entries show sustained engagement.",
        })

    tool_users = 0
    for t in transcripts[:20]:
        try:
            with open(t.path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "tool_use" in line or "tool_result" in line:
                        tool_users += 1
                        break
        except OSError:
            pass
    if tool_users > 10:
        skills.append({
            "name": "Tool-augmented workflow",
            "statement": "Heavy use of tools and automation alongside conversation.",
            "evidence": f"Tool calls observed in {tool_users} of the first 20 transcripts examined.",
        })

    return skills


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_reflection(config_dir: Path) -> dict[str, Any]:
    """Generate a structured reflection of the user's Claude activity.

    Returns a dict with:
      - generated_at: ISO timestamp
      - period: human-readable period string
      - activity: {total_conversations, total_sessions, most_active_day, peak_hour, date_range}
      - topics: [{name, terms, frequency, description}]
      - skills: [{name, statement, evidence}]
      - insights: [str]
      - memory_files: {claude_md, auto_memory, agent_memory}
    """
    config_dir = Path(config_dir)
    # `Path("")` is `.`, which would read the launch directory instead of ~/.claude.
    if str(config_dir) in ("", "."):
        config_dir = scanner.detect_default_source()
    now = datetime.now(timezone.utc)

    memory = stores.read_memory(config_dir)
    history = stores.read_history(config_dir)
    transcripts = scanner.discover_code_transcripts(config_dir)

    total_sessions = len(transcripts)
    total_conversations = history.get("total", 0)

    day_counts: Counter = Counter()
    hour_counts: Counter = Counter()
    for entry in history.get("entries", []):
        ts = entry.get("timestamp")
        if isinstance(ts, (int, float)):
            try:
                dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, timezone.utc)
                day_counts[dt.strftime("%A")] += 1
                hour_counts[dt.hour] += 1
            except (OSError, ValueError, OverflowError):
                pass

    most_active_day = day_counts.most_common(1)[0][0] if day_counts else "—"
    peak_hour = f"{hour_counts.most_common(1)[0][0]:02d}:00" if hour_counts else "—"

    if transcripts:
        mtimes = [t.mtime for t in transcripts if t.mtime > 0]
        if mtimes:
            lo = datetime.fromtimestamp(min(mtimes), timezone.utc).strftime("%Y-%m-%d")
            hi = datetime.fromtimestamp(max(mtimes), timezone.utc).strftime("%Y-%m-%d")
            date_range = f"{lo} → {hi}"
            days = (max(mtimes) - min(mtimes)) / 86400
            if days <= 1:
                period = "Today"
            elif days <= 7:
                period = "Past week"
            elif days <= 31:
                period = "Past month"
            elif days <= 90:
                period = "Past quarter"
            else:
                period = f"Past {int(days)} days"
        else:
            date_range = "—"
            period = "Unknown"
    else:
        date_range = "—"
        period = "No data"

    # --- extract terms and phrases from all text sources ---
    all_text = ""
    for item in memory.get("claude_md", []):
        all_text += item.get("text", "") + "\n"
    for proj in memory.get("auto_memory", []):
        for f in proj.get("files", []):
            all_text += f.get("text", "") + "\n"

    for t in transcripts[:30]:
        try:
            with open(t.path, "r", encoding="utf-8", errors="replace") as fh:
                all_text += fh.read(8192) + "\n"
        except OSError:
            pass

    terms = _extract_terms(all_text)
    phrases = _extract_phrases(all_text)
    topics = _cluster_topics(terms, phrases)

    insights = _generate_insights(memory, history, transcripts, topics)
    skills = _generate_skills(memory, history, transcripts, topics)

    memory_files = {
        "claude_md": len(memory.get("claude_md", [])),
        "auto_memory": sum(len(p.get("files", [])) for p in memory.get("auto_memory", [])),
        "agent_memory": sum(len(a.get("files", [])) for a in memory.get("agent_memory", [])),
    }

    return {
        "generated_at": now.isoformat(),
        "period": period,
        "activity": {
            "total_conversations": total_conversations,
            "total_sessions": total_sessions,
            "most_active_day": most_active_day,
            "peak_hour": peak_hour,
            "date_range": date_range,
        },
        "topics": topics,
        "skills": skills,
        "insights": insights,
        "memory_files": memory_files,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(reflection: dict) -> str:
    """Render the reflection as a Markdown string."""
    lines = []
    lines.append(f"# Reflect — {reflection['period']}")
    lines.append("")
    lines.append(f"Generated: {reflection['generated_at'][:19].replace('T', ' ')}")
    lines.append("")

    act = reflection["activity"]
    lines.append("## Activity")
    lines.append("")
    lines.append(f"- **Conversations:** {act['total_conversations']:,}")
    lines.append(f"- **Sessions:** {act['total_sessions']:,}")
    lines.append(f"- **Most active day:** {act['most_active_day']}")
    lines.append(f"- **Peak hour:** {act['peak_hour']}")
    lines.append(f"- **Date range:** {act['date_range']}")
    lines.append("")

    if reflection["topics"]:
        lines.append("## What your conversations were about")
        lines.append("")
        for t in reflection["topics"]:
            lines.append(f"- **{t['name']}** — {t['description']}")
            if t.get("terms"):
                lines.append(f"  - Key terms: {', '.join(t['terms'][:5])}")
        lines.append("")

    if reflection["skills"]:
        lines.append("## Skills")
        lines.append("")
        for s in reflection["skills"]:
            lines.append(f"### {s['name']}")
            lines.append("")
            lines.append(f"**{s['statement']}**")
            lines.append("")
            lines.append(s["evidence"])
            lines.append("")

    if reflection["insights"]:
        lines.append("## Insights")
        lines.append("")
        for insight in reflection["insights"]:
            lines.append(f"- {insight}")
        lines.append("")

    mf = reflection["memory_files"]
    lines.append("## Memory files")
    lines.append("")
    lines.append(f"- CLAUDE.md files: {mf['claude_md']}")
    lines.append(f"- Auto-memory files: {mf['auto_memory']}")
    lines.append(f"- Agent memory files: {mf['agent_memory']}")
    lines.append("")

    return "\n".join(lines)
