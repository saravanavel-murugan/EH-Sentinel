"""Phase 6 — SentinelSession: planning + memory + multi-turn orchestration.

This is the Phase 6 layer that sits ON TOP OF the existing, unchanged pieces:

    * MultiAgentSentinel (A1-A8 LangGraph pipeline)   — Phase 3b, unchanged
    * ToolAgent (allow-listed tools over MCP/registry) — Phase 5, unchanged

What Phase 6 adds:

    1. PLANNING — every signal is decomposed into an explicit IncidentPlan whose
       steps are marked running/done/skipped as the A1-A8 pipeline executes, so
       the multi-step reasoning is inspectable end to end (signal -> closure).

    2. MEMORY — a short-term ConversationMemory (turns + working set within the
       session) and a long-term IncidentMemoryStore (incidents persisted across
       sessions). Before triaging, the session RECALLS prior incidents for the
       entity (recurrence awareness); after closing, it PERSISTS the record.

    3. MULTI-TURN — one session handles a sequence of turns. A turn may be a
       normalized-signal JSON (full incident pipeline) OR a free-text operator
       follow-up (answered from memory + the Phase 5 ToolAgent). Context carries
       across turns (the "current entity/incident in focus").

The session never performs real production actions: everything inherits the
read-only / human-approval guardrails of the underlying agents.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Optional

from ..core_agent import BaselineAgent
from .memory import ConversationMemory, IncidentMemoryStore, IncidentRecord
from .planner import IncidentPlan

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MEMORY_PATH = _REPO_ROOT / "logs" / "incident_memory.json"


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class SentinelSession:
    """A stateful, multi-turn conversation over the Sentinel multi-agent system.

    Construct once per operator conversation. Call ``handle_turn`` for each input;
    the session keeps short-term state and reads/writes long-term incident memory.
    """

    version = "phase6-session-0.1.0"

    def __init__(
        self,
        chat_model: Any = None,
        use_retrieval: bool = True,
        memory_path: Path | str | None = None,
        max_turns: int = 20,
        max_records: int = 200,
        multi_agent: Any = None,
        tool_agent: Any = None,
    ):
        # Reuse the existing agents unchanged. Import lazily so Phase 2 stays
        # LLM-free and tests can inject fakes.
        from ..agents import MultiAgentSentinel
        from ..llm import provider_name
        from ..tool_agent import ToolAgent

        self.provider = provider_name()
        self._ma = multi_agent or MultiAgentSentinel(
            chat_model=chat_model, use_retrieval=use_retrieval
        )
        self._tools = tool_agent or ToolAgent(chat_model=chat_model)
        self.retrieval_enabled = getattr(self._ma, "retrieval_enabled", False)

        self.conversation = ConversationMemory(max_turns=max_turns)
        self.memory = IncidentMemoryStore(
            memory_path or _DEFAULT_MEMORY_PATH, max_records=max_records
        )
        self.started_at = _utc_now()

    # ----------------------------------------------------------------------
    # Public entry point
    # ----------------------------------------------------------------------

    def handle_turn(self, user_input: str) -> dict[str, Any]:
        """Route one turn: signal JSON -> full pipeline; free text -> follow-up."""
        text = (user_input or "").strip()
        self.conversation.add_turn("user", text)

        if text.lower() in {"reset", "/reset", "new incident"}:
            return self._handle_reset()

        signal = BaselineAgent._try_parse_signal(text)
        if signal is not None:
            return self._handle_signal_turn(signal)
        return self._handle_followup_turn(text)

    # ----------------------------------------------------------------------
    # Signal turn: plan + A1-A8 + memory, with full end-to-end trace
    # ----------------------------------------------------------------------

    def _handle_signal_turn(self, signal: dict[str, Any]) -> dict[str, Any]:
        plan = IncidentPlan.build_default(signal)
        entity = self._entity_key(signal)
        metric = signal.get("metric_or_event", "n/a")

        # --- STEP: recall prior context (long-term memory) ---
        plan.start("recall")
        prior = self.memory.for_entity(entity)
        recurrence = self.memory.recurrence_count(entity, metric)
        if prior:
            last = prior[0]
            plan.complete(
                "recall",
                f"{recurrence} prior incident(s) for {entity}; last was "
                f"{last.incident_id} ({last.severity}).",
            )
        else:
            plan.complete("recall", f"no prior incidents for {entity}.")

        # --- STEPS A1-A8: run the unchanged multi-agent pipeline ---
        # The pipeline runs as one bounded graph; we reflect its outcome onto the
        # plan steps (the orchestrator itself is not modified).
        for aid in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"):
            plan.start(aid)
        result = self._ma.run_signal(signal)
        trace = result.get("trace", []) or []
        agents = result.get("agents", {}) or {}
        gated = bool(result.get("stop_reason"))

        self._reflect_pipeline_on_plan(plan, trace, agents, gated,
                                       result.get("stop_reason"))

        # --- STEP: persist to long-term memory ---
        record: Optional[IncidentRecord] = None
        if not gated:
            record = self._persist_incident(result, entity, metric)
            plan.complete(
                "persist",
                f"stored {record.incident_id} (now {self.memory.recurrence_count(entity, metric)} "
                f"for {entity}/{metric}).",
            )
        else:
            plan.skip("persist", "gated at A1 (watch state) — nothing to persist.")

        # --- update short-term working set (carries across turns) ---
        self.conversation.remember("current_entity", entity)
        self.conversation.remember("current_metric", metric)
        if record is not None:
            self.conversation.remember("current_incident_id", record.incident_id)

        response = self._render_signal_turn(result, plan, prior, recurrence)
        self.conversation.add_turn("agent", response,
                                   meta={"kind": "signal", "entity": entity,
                                         "trace": trace})

        return {
            "kind": "signal",
            "turn": self.conversation.turn_count,
            "provider": self.provider,
            "entity": entity,
            "entity_known": result.get("entity_known"),
            "recurrence_count": recurrence,
            "gated": gated,
            "stop_reason": result.get("stop_reason"),
            "trace": trace,
            "plan": plan.to_dict(),
            "retrieval": result.get("retrieval", {}),
            "agents": agents,
            "incident_id": record.incident_id if record else None,
            "pipeline_response": result.get("response", ""),
            "response": response,
        }

    def _reflect_pipeline_on_plan(self, plan: IncidentPlan, trace: list[str],
                                  agents: dict[str, Any], gated: bool,
                                  stop_reason: Optional[str]) -> None:
        """Mark plan steps done/skipped from the pipeline's actual trace."""
        for aid in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"):
            if aid in trace:
                plan.complete(aid, self._agent_summary(aid, agents))
        if gated:
            # A1 ran and gated; the rest did not execute.
            plan.complete("A1", self._agent_summary("A1", agents))
            for aid in ("A2", "A3", "A4", "A5", "A6", "A7", "A8"):
                if aid not in trace:
                    plan.skip(aid, stop_reason or "skipped (A1 gate).")

    @staticmethod
    def _agent_summary(aid: str, agents: dict[str, Any]) -> str:
        a = agents.get(aid.lower(), {}) or {}
        if aid == "A1":
            return f"classification={a.get('classification')} score={a.get('anomaly_score')}"
        if aid == "A2":
            return f"candidate={a.get('incident_candidate_id')}"
        if aid == "A3":
            return f"severity={a.get('severity')}"
        if aid == "A4":
            return f"{len(a.get('hypotheses', []) or [])} hypothesis(es)"
        if aid == "A5":
            return f"{len(a.get('actions', []) or [])} action(s), read_only={a.get('read_only')}"
        if aid == "A6":
            return f"incident_id={a.get('incident_id')}"
        if aid == "A7":
            return f"channel={a.get('channel')} delivery={a.get('delivery_status')}"
        if aid == "A8":
            return f"recovery={a.get('recovery_status')} closure={a.get('closure_recommendation')}"
        return ""

    def _persist_incident(self, result: dict[str, Any], entity: str,
                          metric: str) -> IncidentRecord:
        agents = result.get("agents", {}) or {}
        a1 = agents.get("a1", {}) or {}
        a3 = agents.get("a3", {}) or {}
        a6 = agents.get("a6", {}) or {}
        a8 = agents.get("a8", {}) or {}
        record = IncidentRecord(
            incident_id=a6.get("incident_id") or f"inc-{entity}",
            entity=entity,
            metric=metric,
            classification=a1.get("classification", ""),
            severity=a6.get("severity") or a3.get("severity"),
            summary=a6.get("summary") or a6.get("issue", ""),
            closure_recommendation=a8.get("closure_recommendation", "continue_monitoring"),
            knowledge_references=a6.get("knowledge_references", []) or [],
        )
        self.memory.add(record)
        return record

    # ----------------------------------------------------------------------
    # Free-text follow-up turn: answer from memory + Phase 5 tools
    # ----------------------------------------------------------------------

    def _handle_followup_turn(self, text: str) -> dict[str, Any]:
        entity = self.conversation.recall("current_entity")
        incident_id = self.conversation.recall("current_incident_id")

        # Memory-grounded context about what we've already done this session.
        memory_note = self._memory_note(entity)

        # Let the Phase 5 ToolAgent handle the operator's follow-up (metric
        # lookup / knowledge search / escalation) with its own guardrails.
        tool_out = self._tools.handle(text)

        response = self._render_followup(text, memory_note, tool_out)
        self.conversation.add_turn("agent", response,
                                   meta={"kind": "followup",
                                         "entity": entity,
                                         "tools": tool_out.get("n_steps")})

        return {
            "kind": "followup",
            "turn": self.conversation.turn_count,
            "provider": self.provider,
            "focus_entity": entity,
            "focus_incident": incident_id,
            "memory_note": memory_note,
            "tool_transport": tool_out.get("transport"),
            "tool_steps": tool_out.get("steps", []),
            "tool_response": tool_out.get("response", ""),
            "response": response,
        }

    def _memory_note(self, entity: Optional[str]) -> str:
        if not entity:
            return "No incident is in focus yet this session. Paste a signal JSON to start."
        prior = self.memory.for_entity(entity)
        if not prior:
            return f"In focus: {entity}. No incidents recorded for it yet."
        last = prior[0]
        return (f"In focus: {entity}. {len(prior)} incident(s) on record; most "
                f"recent {last.incident_id} — {last.severity}, "
                f"closure: {last.closure_recommendation}.")

    # ----------------------------------------------------------------------
    # Reset: short-term only (long-term memory is retained by design)
    # ----------------------------------------------------------------------

    def _handle_reset(self) -> dict[str, Any]:
        kept = self.memory.count
        self.conversation.reset()
        response = (
            "Short-term conversation reset. Starting a fresh conversation.\n"
            f"Long-term incident memory RETAINED ({kept} record(s)); use "
            "clear_long_term_memory() to forget those too."
        )
        # Record the reset as the first turn of the new conversation.
        self.conversation.add_turn("system", response, meta={"kind": "reset"})
        return {
            "kind": "reset",
            "turn": self.conversation.turn_count,
            "long_term_retained": kept,
            "response": response,
        }

    def clear_long_term_memory(self) -> int:
        """Explicit forget-all for durable memory. Returns how many were cleared."""
        n = self.memory.count
        self.memory.reset()
        return n

    # ----------------------------------------------------------------------
    # Rendering
    # ----------------------------------------------------------------------

    def _render_signal_turn(self, result: dict[str, Any], plan: IncidentPlan,
                            prior: list[IncidentRecord], recurrence: int) -> str:
        entity = result.get("entity")
        lines = ["=== SENTINEL SESSION — SIGNAL (end-to-end) ==="]
        lines.append(f"Turn   : {self.conversation.turn_count}  ·  provider: {self.provider}")
        lines.append(f"Entity : {entity} (known={result.get('entity_known')})")
        if recurrence:
            last = prior[0]
            lines.append(f"Memory : RECURRING — {recurrence} prior incident(s); "
                         f"last {last.incident_id} ({last.severity}).")
        else:
            lines.append("Memory : first time this entity is seen (no prior incidents).")
        lines.append("")
        lines.append(plan.render())
        lines.append("")
        lines.append("--- Multi-agent pipeline output ---")
        lines.append(result.get("response", ""))
        return "\n".join(lines)

    def _render_followup(self, text: str, memory_note: str,
                         tool_out: dict[str, Any]) -> str:
        lines = ["=== SENTINEL SESSION — FOLLOW-UP ==="]
        lines.append(f"Turn   : {self.conversation.turn_count}  ·  provider: {self.provider}")
        lines.append(f"Memory : {memory_note}")
        lines.append("")
        lines.append(tool_out.get("response", "(no tool output)"))
        return "\n".join(lines)

    # ----------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------

    @staticmethod
    def _entity_key(signal: dict[str, Any]) -> str:
        entity = signal.get("entity", {}) or {}
        return f"{entity.get('type', 'n/a')}:{entity.get('id', 'n/a')}"

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serializable view of the whole session (for UI/trace/debug)."""
        return {
            "version": self.version,
            "provider": self.provider,
            "started_at": self.started_at,
            "retrieval_enabled": self.retrieval_enabled,
            "conversation": self.conversation.to_dict(),
            "long_term_memory": {
                "count": self.memory.count,
                "records": [r.to_dict() for r in self.memory.all()],
            },
        }
