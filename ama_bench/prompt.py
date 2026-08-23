"""Batch prompt builder for answering questions from NovaCode memory.

Mirrors AMA-Bench's ``longcontext`` batch format (``Answer[i]:`` slots) while
providing the full structured task/tool state (including Macro Timeline,
Findings, Decisions, Key Sequences) along with per-question evidence retrieval.
The final answer line is extracted by :func:`ama_bench.extract.extract_final_answer`.
"""
from __future__ import annotations

from .memory import NovaCodeMemory
from .retrieve import render_evidence, score_candidates

_MAX_PROMPT_CHARS = 24_000


def render_structured_state(memory: NovaCodeMemory) -> str:
    """Format the full TaskState (Macro Timeline, Findings, Decisions, Sequences) and ToolState."""
    lines: list[str] = []
    task = memory.task_state

    # 1. Macro Timeline
    timeline_entries = [t for t in task.macro_timeline if t.status == "valid"]
    if timeline_entries:
        lines.append("<macro_timeline>")
        for t in timeline_entries:
            range_str = f" [Steps {t.step_range}]" if t.step_range else ""
            lines.append(f"-{range_str} {t.summary} (Outcome: {t.outcome})")
        lines.append("</macro_timeline>")
    elif task.completed:
        lines.append("<macro_timeline>")
        for p in task.completed:
            step_str = f" [Step {p.completed_step}]" if p.completed_step else ""
            if p.text:
                lines.append(f"-{step_str} {p.text}")
        lines.append("</macro_timeline>")

    # 2. Structured Task State
    task_lines: list[str] = []
    if task.key_findings:
        task_lines.append("Key Findings:")
        for f in task.key_findings:
            if f.status == "valid":
                step_info = f" (step {f.updated_step})" if f.updated_step else ""
                task_lines.append(f"- {f.fact}{step_info}")

    if task.decisions:
        task_lines.append("Decisions:")
        for d in task.decisions:
            if d.status == "valid":
                reason = f" - Reason: {d.reason}" if d.reason else ""
                task_lines.append(f"- {d.decision}{reason}")

    if task.key_sequences:
        task_lines.append("Key Sequences / Action Patterns:")
        for s in task.key_sequences:
            if s.status == "valid":
                range_str = f" (steps {s.step_range})" if s.step_range else ""
                intent = f" -> Intent: {s.intent}" if s.intent else ""
                task_lines.append(f"- {s.pattern}{range_str}{intent}")

    if task.completed and timeline_entries:
        progress_items = [p for p in task.completed if p.text]
        if progress_items:
            task_lines.append("Milestones:")
            for p in progress_items:
                task_lines.append(f"- {p.text}")

    if task.unresolved:
        blocked = [u for u in task.unresolved if u.blocked]
        if blocked:
            task_lines.append("Blocked Items:")
            for u in blocked:
                task_lines.append(f"- {u.text}")

    if task_lines:
        lines.append("<task_state>")
        lines.extend(task_lines)
        lines.append("</task_state>")

    # 3. Tool experience (if any)
    tool_lines: list[str] = []
    for profile_name, slots in memory.tool_state.profiles.items():
        for slot_name, entries in slots.items():
            for entry in entries:
                if entry.status == "valid":
                    val = entry.value if isinstance(entry.value, dict) else {}
                    txt = val.get("text") or val.get("command") or val.get("path") or str(entry.value)
                    if txt:
                        tool_lines.append(f"- [{profile_name}.{slot_name}] {txt}")
    if tool_lines:
        lines.append("<tool_experience>")
        lines.extend(tool_lines[:15])
        lines.append("</tool_experience>")

    if not lines:
        return ""
    return "<structured_state>\n" + "\n".join(lines) + "\n</structured_state>"


def build_batch_prompt(
    memory: NovaCodeMemory,
    questions: list[str],
    *,
    mcq_mode: bool = False,
    max_total_chars: int = _MAX_PROMPT_CHARS,
) -> str:
    """Build one prompt that answers every question with global structured state and local evidence."""
    objective = (memory.task_state.objective or "").strip()
    lines: list[str] = []
    lines.append("You are answering questions about an agent trajectory.")
    if objective:
        lines.append(f"Episode task: {objective}")

    structured_block = render_structured_state(memory)
    if structured_block:
        lines.append("")
        lines.append(structured_block)

    budget = max_total_chars - len("\n".join(lines))
    per_q_budget = max(1_500, budget // max(1, len(questions)))
    evidence_budget = min(3_000 if len(questions) <= 1 else 2_000, per_q_budget)

    for index, question in enumerate(questions, start=1):
        candidates = score_candidates(memory, question)
        evidence = render_evidence(candidates, max_total_chars=evidence_budget)
        if len(evidence) > budget:
            evidence = evidence[:budget]
        lines.append("")
        lines.append(f"Question {index}: {question}")
        lines.append("Relevant trajectory evidence:")
        lines.append(evidence)
        if mcq_mode:
            lines.append(f"Answer[{index}]: [select all correct options, e.g. (A) or (A)(C)]")
        else:
            lines.append(f"Answer[{index}]: [your answer here]")
        budget -= len(evidence) + 200
        if budget <= 0:
            lines.append("(context budget exhausted; remaining questions omitted)")
            break
    return "\n".join(lines)
