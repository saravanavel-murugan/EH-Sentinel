"""EventHub Ops Sentinel — Phase 6 planning, memory & context package.

Adds an explicit planning/task-decomposition layer, short-term conversation
memory, long-term persisted incident memory (with reset/retention rules), and a
multi-turn session that orchestrates the existing (unchanged) A1-A8 pipeline and
Phase 5 tools into one end-to-end, inspectable workflow.

Public surface:
    SentinelSession      - multi-turn orchestrator (planning + memory + agents)
    IncidentPlan         - deterministic task decomposition with step status
    ConversationMemory   - bounded short-term conversation state
    IncidentMemoryStore  - durable long-term incident memory (JSON)
    IncidentRecord       - one stored incident
"""

from __future__ import annotations

from .memory import ConversationMemory, IncidentMemoryStore, IncidentRecord, Turn
from .planner import IncidentPlan, PlanStep
from .session import SentinelSession

__all__ = [
    "SentinelSession",
    "IncidentPlan",
    "PlanStep",
    "ConversationMemory",
    "IncidentMemoryStore",
    "IncidentRecord",
    "Turn",
]
