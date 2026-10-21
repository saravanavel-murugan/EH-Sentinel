"""Phase 6 — Streamlit UI: end-to-end multi-agent trace from signal to closure.

A rich operator console over the Phase 6 ``SentinelSession``. It shows, for the
whole multi-agent workflow:

  * a multi-turn chat (paste a signal JSON or ask a follow-up),
  * the explicit PLAN (task decomposition) with live step status,
  * each named agent's role + output (A1 Signal Validation ... A8 Recovery
    Verification), the agent trace, and retrieval/RAG evidence,
  * short-term conversation state and long-term incident memory (with recurrence),
  * reset (short-term) vs. explicit forget-all (long-term) controls.

Run it:
    streamlit run ui/streamlit_app.py           # from capstone_agent/

Headless import safety: ALL Streamlit calls live inside ``main()`` / helpers that
are only invoked under ``streamlit run`` (or ``__main__``). Importing this module
(``import ui.streamlit_app``) therefore has no side effects and needs no running
Streamlit server — which keeps the offline verification check green.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the package root (capstone_agent/) is importable whether launched via
# `streamlit run ui/streamlit_app.py` or imported as `ui.streamlit_app`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SAMPLE_SIGNAL = {
    "signal_id": "sig-ui-1",
    "source": "control_center",
    "entity": {"type": "topic", "id": "payments.settlement"},
    "metric_or_event": "consumer_lag",
    "value": 60000,
    "unit": "messages",
    "environment": "prod",
}

_STATUS_GLYPH = {
    "pending": "⬜", "running": "🔄", "done": "✅",
    "skipped": "⏭️", "failed": "❌",
}

# Human-facing agent metadata: name, icon, one-line purpose, and the key output.
# Keyed by the lowercase state id (a1..a8). Names mirror contracts.AGENT_NAMES.
_AGENT_META = {
    "a1": {"icon": "🔎", "name": "Signal Validation / Anomaly Triage",
           "purpose": "Decides whether a signal is a real anomaly vs transient/noise, "
                      "detects duplicates, and asks for more evidence if needed.",
           "produces": "classification, anomaly score, missing data"},
    "a2": {"icon": "🔗", "name": "Correlation & Topology",
           "purpose": "Groups related symptoms into one incident candidate and maps "
                      "the impacted dependency path.",
           "produces": "incident candidate, correlated signals, dependency path"},
    "a3": {"icon": "📊", "name": "Severity & Business Impact",
           "purpose": "Assigns P1–P4 priority and the business impact, using the "
                      "entity's criticality tier.",
           "produces": "severity, business impact, affected services"},
    "a4": {"icon": "🧩", "name": "Root-Cause Hypothesis",
           "purpose": "Produces ranked root-cause hypotheses with evidence for/against "
                      "and the next diagnostic check — grounded in runbooks when "
                      "available, else LLM-inferred.",
           "produces": "ranked hypotheses, observed facts, next checks"},
    "a5": {"icon": "🛠️", "name": "Remediation Recommendation",
           "purpose": "Enumerates safe, reversible, approval-gated actions (diagnostics "
                      "first). Read-only by default; never acts automatically.",
           "produces": "recommended actions (all require approval)"},
    "a6": {"icon": "📝", "name": "Incident Summary & Ticket",
           "purpose": "Consolidates everything into one 7-section incident record with "
                      "a narrative summary and the knowledge-reference trail.",
           "produces": "incident record, closure document, ticket payload"},
    "a7": {"icon": "📣", "name": "Stakeholder Communication & Escalation",
           "purpose": "Resolves recipients ONLY from the approved directory and drafts "
                      "an audience-appropriate message. Never auto-sends.",
           "produces": "recipients by role, message, channel (not sent)"},
    "a8": {"icon": "✅", "name": "Recovery Verification & Feedback",
           "purpose": "Verifies recovery against explicit metric criteria and records "
                      "feedback. Alert disappearance alone is not proof of recovery.",
           "produces": "recovery status, closure recommendation"},
}

_AGENT_ORDER = ["a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8"]


def agent_label(aid: str) -> str:
    """Human label for an agent id, e.g. 'A4 · Root-Cause Hypothesis' (pure helper)."""
    meta = _AGENT_META.get(aid.lower())
    if not meta:
        return aid.upper()
    return f"{aid.upper()} · {meta['name']}"


def trace_labels(trace: list) -> str:
    """Render a trace like ['A1','A2'] as readable agent names (pure helper)."""
    return "  →  ".join(agent_label(a) for a in trace) if trace else "—"

# Modern SLATE + TEAL dark-mode theme injected as CSS. A deep slate-navy canvas
# (not pure black) with a teal/cyan primary accent, amber highlights, and soft
# off-white text — a clean, professional "ops dashboard" look.
_TEAL = "#2DD4BF"          # primary accent (teal)
_TEAL_BRIGHT = "#5EEAD4"   # brighter teal (hover / highlights)
_CYAN = "#38BDF8"          # secondary accent (links, headings)
_AMBER = "#FBBF24"         # warm accent (warnings / attention)
_BG = "#0B1220"            # deep slate-navy canvas
_PANEL = "#141D2E"         # slate panels / sidebar
_PANEL_2 = "#1C2740"       # lighter slate (table stripes, hover)
_LINE = "#263247"          # subtle slate borders
_TEXT = "#E5E7EB"          # soft off-white text
_MUTED = "#94A3B8"         # muted slate text (captions)
_INK = "#06111F"           # near-black ink for text on teal buttons

_THEME_CSS = f"""
<style>
  /* deep slate canvas */
  .stApp {{ background-color: {_BG}; color: {_TEXT}; }}
  .main .block-container {{ background-color: {_BG}; }}

  /* headings in cyan; body soft off-white; muted captions */
  h1, h2, h3, h4, h5 {{ color: {_CYAN} !important; letter-spacing: 0.2px; }}
  .stApp a {{ color: {_TEAL}; }}
  .stApp, .stMarkdown, p, span, label {{ color: {_TEXT}; }}
  .stCaption, [data-testid="stCaptionContainer"] {{ color: {_MUTED} !important; }}

  /* slate sidebar with a teal edge */
  section[data-testid="stSidebar"] {{
      background-color: {_PANEL};
      border-right: 1px solid {_LINE};
  }}
  section[data-testid="stSidebar"] h1,
  section[data-testid="stSidebar"] h2,
  section[data-testid="stSidebar"] h3 {{ color: {_TEAL} !important; }}
  section[data-testid="stSidebar"] * {{ color: {_TEXT}; }}

  /* buttons: teal fill, dark ink text, subtle lift on hover */
  .stButton > button {{
      background-color: {_TEAL};
      color: {_INK};
      border: 0;
      border-radius: 8px;
      font-weight: 700;
      transition: background-color 120ms ease, transform 80ms ease;
  }}
  .stButton > button:hover {{ background-color: {_TEAL_BRIGHT}; color: {_INK};
      transform: translateY(-1px); }}

  /* progress bar fill */
  .stProgress > div > div > div > div {{ background-color: {_TEAL}; }}

  /* chat bubbles: slate panel with a teal left border */
  div[data-testid="stChatMessage"] {{
      background-color: {_PANEL};
      border: 1px solid {_LINE};
      border-left: 4px solid {_TEAL};
      border-radius: 12px;
      padding: 0.6rem 0.85rem;
  }}

  /* expanders + status panels */
  div[data-testid="stExpander"] {{
      border: 1px solid {_LINE};
      border-radius: 12px;
      background-color: {_PANEL};
  }}
  div[data-testid="stExpander"] summary {{ color: {_CYAN}; }}

  /* tables: teal header, slate striped rows */
  table {{ color: {_TEXT}; border-collapse: collapse; }}
  table thead tr th {{
      background-color: {_PANEL_2};
      color: {_TEAL} !important;
      border-bottom: 2px solid {_TEAL};
  }}
  table tbody tr {{ background-color: {_PANEL}; }}
  table tbody tr:nth-child(even) {{ background-color: {_PANEL_2}; }}

  /* inputs */
  div[data-testid="stChatInput"] textarea {{
      border-color: {_LINE};
      background-color: {_PANEL};
      color: {_TEXT};
  }}
  div[data-testid="stChatInput"] textarea:focus {{ border-color: {_TEAL}; }}

  /* code / json blocks: slate panel with a teal edge */
  .stCode, pre, code {{
      background-color: {_PANEL} !important;
      border-left: 3px solid {_CYAN};
      color: {_TEXT};
  }}

  /* alert tints that fit the palette (info=cyan, success=teal, warning=amber) */
  div[data-testid="stAlert"] {{ border-radius: 10px; }}
</style>
"""


# ==========================================================================
# Session plumbing (no Streamlit imports at module scope)
# ==========================================================================

def _get_session(st):
    """Return the SentinelSession stored in st.session_state, creating it once."""
    from agent.env_config import load_env
    from agent.session import SentinelSession

    if "sentinel_session" not in st.session_state:
        load_env(verbose=False)
        # Retrieval is optional; disable gracefully if no FAISS index is present.
        try:
            st.session_state.sentinel_session = SentinelSession(use_retrieval=True)
        except Exception:  # noqa: BLE001
            st.session_state.sentinel_session = SentinelSession(use_retrieval=False)
        st.session_state.history = []  # list of {role, text, result}
    return st.session_state.sentinel_session


# ==========================================================================
# Rendering helpers (take `st` so the module imports without streamlit)
# ==========================================================================

def _render_plan(st, plan: dict) -> None:
    st.markdown(f"**Plan objective:** {plan.get('objective', '')}")
    steps = plan.get("steps", [])
    done = sum(1 for s in steps if s.get("status") == "done")
    total = len(steps) or 1
    st.progress(done / total, text=plan.get("progress", f"{done}/{total}"))
    for i, s in enumerate(steps, 1):
        glyph = _STATUS_GLYPH.get(s.get("status"), "⬜")
        summary = f" — {s['result_summary']}" if s.get("result_summary") else ""
        st.markdown(f"{glyph} **{i}. {s['title']}** `({s['owner']})`{summary}")


def _render_agents(st, agents: dict) -> None:
    for aid in _AGENT_ORDER:
        a = agents.get(aid)
        if not a:
            continue
        meta = _AGENT_META.get(aid, {})
        icon = meta.get("icon", "•")
        name = meta.get("name", aid.upper())
        with st.expander(f"{icon}  {aid.upper()} · {name}", expanded=(aid == "a6")):
            if meta.get("purpose"):
                st.caption(f"**Purpose:** {meta['purpose']}")
                st.caption(f"**Produces:** {meta.get('produces', '')}")
            st.json(a)


def _render_about_agents(st) -> None:
    """A reference panel describing what each of the 8 agents does."""
    st.markdown(
        "The pipeline is **8 cooperating agents** (LangGraph nodes, each driven by a "
        "LangChain chat model). A signal flows through them in order; A1 can gate the "
        "flow to a watch state for noise/insufficient evidence."
    )
    for aid in _AGENT_ORDER:
        m = _AGENT_META[aid]
        st.markdown(
            f"**{m['icon']} {aid.upper()} · {m['name']}**  \n"
            f"{m['purpose']}  \n"
            f"<span style='color:#94A3B8'>Produces: {m['produces']}</span>",
            unsafe_allow_html=True,
        )


def analysis_source_of(agents: dict) -> str:
    """Return 'knowledge_base' or 'llm_inference' from the A4 output (pure helper)."""
    a4 = agents.get("a4") or {}
    return a4.get("analysis_source", "knowledge_base")


def _render_analysis_source(st, agents: dict) -> None:
    """Prominent badge showing whether this incident was runbook-grounded or
    LLM-inferred (signal not covered by the knowledge base)."""
    src = analysis_source_of(agents)
    if src == "llm_inference":
        st.warning(
            "🧠 **Analysis source: LLM inference** — this signal is **not covered "
            "by the knowledge base**. Root cause & remediation were derived by the "
            "LLM from domain reasoning and must be human-validated before any action."
        )
    else:
        st.success(
            "📚 **Analysis source: knowledge base** — root cause & remediation are "
            "grounded in retrieved runbooks/FAQ (see Retrieval evidence below)."
        )


def _render_retrieval(st, retrieval: dict) -> None:
    if not retrieval.get("enabled"):
        st.info("Retrieval (RAG) disabled — no FAISS index wired for this run.")
        return
    if not retrieval.get("found"):
        st.warning(f"No relevant knowledge matched (best score "
                   f"{retrieval.get('best_score')}).")
        return
    st.caption(f"embedder `{retrieval.get('embedder')}` · query "
               f"`{retrieval.get('query')}`")
    hits = retrieval.get("hits", [])
    if hits:
        st.table([
            {"source": h.get("source"), "heading": h.get("heading"),
             "score": round(float(h.get("score", 0.0)), 3)}
            for h in hits
        ])


def _render_memory(st, session) -> None:
    snap = session.snapshot()
    conv = snap["conversation"]
    ltm = snap["long_term_memory"]

    st.subheader("Short-term (this conversation)")
    st.caption(f"{conv['turn_count']} turn(s); window={conv['window']}")
    if conv["working_set"]:
        st.json(conv["working_set"])
    else:
        st.write("_working set empty_")

    st.subheader("Long-term (persisted incidents)")
    st.caption(f"{ltm['count']} record(s) retained across sessions")
    if ltm["records"]:
        st.table([
            {"incident": r["incident_id"], "entity": r["entity"],
             "metric": r["metric"], "severity": r["severity"],
             "class": r["classification"]}
            for r in ltm["records"]
        ])
    else:
        st.write("_no incidents recorded yet_")


def _render_turn_result(st, result: dict) -> None:
    """Render one agent turn result (signal or follow-up) in the chat stream."""
    kind = result.get("kind")
    if kind == "signal":
        if result.get("recurrence_count"):
            st.warning(f"🔁 Recurring: {result['recurrence_count']} prior "
                       f"incident(s) for {result.get('entity')}.")
        if result.get("gated"):
            st.info(f"Gated at **{agent_label('a1')}**: {result.get('stop_reason')}")
        # Provenance badge: was the analysis grounded in the knowledge base or
        # inferred by the LLM for a signal the knowledge base does not cover?
        _render_analysis_source(st, result.get("agents", {}))
        st.markdown("##### Plan (task decomposition)")
        _render_plan(st, result.get("plan", {}))
        st.markdown(f"**Agent trace:** {trace_labels(result.get('trace', []))}")
        st.markdown("##### Retrieval evidence (RAG)")
        _render_retrieval(st, result.get("retrieval", {}))
        st.markdown("##### Agent reasoning — each agent's role & output")
        _render_agents(st, result.get("agents", {}))
        with st.expander("Raw pipeline text", expanded=False):
            st.code(result.get("pipeline_response", ""), language="text")
    elif kind == "followup":
        st.caption(result.get("memory_note", ""))
        st.code(result.get("tool_response", ""), language="text")
    else:  # reset / other
        st.code(result.get("response", ""), language="text")


# ==========================================================================
# Main app (only called under `streamlit run` / __main__)
# ==========================================================================

def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="EventHub Ops Sentinel — Phase 6",
                       page_icon="🛰️", layout="wide")
    st.markdown(_THEME_CSS, unsafe_allow_html=True)  # purple-and-white theme
    session = _get_session(st)

    st.title("🛰️ EventHub Ops Sentinel — multi-agent console")
    st.caption(
        f"Phase 6 · planning + memory · provider `{session.provider}` · "
        f"retrieval `{session.retrieval_enabled}` · "
        "advisory only — production actions require human approval."
    )
    with st.expander("ℹ️ About the 8 agents — what each one does", expanded=False):
        _render_about_agents(st)

    # ---- sidebar: controls + live memory -----------------------------
    with st.sidebar:
        st.header("Session")
        if st.button("Load sample signal"):
            st.session_state.pending_input = json.dumps(_SAMPLE_SIGNAL)
        if st.button("Reset conversation (short-term)"):
            res = session.handle_turn("reset")
            st.session_state.history.append(
                {"role": "assistant", "text": res["response"], "result": res})
            st.rerun()
        if st.button("Forget long-term memory"):
            n = session.clear_long_term_memory()
            st.session_state.history.append(
                {"role": "assistant",
                 "text": f"Cleared {n} long-term incident record(s).",
                 "result": {"kind": "reset", "response": ""}})
            st.rerun()
        st.divider()
        _render_memory(st, session)

    # ---- replay chat history -----------------------------------------
    for msg in st.session_state.history:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.code(msg["text"], language="json"
                        if msg["text"].strip().startswith("{") else "text")
            else:
                st.markdown(msg["text"].splitlines()[0] if msg["text"] else "")
                if msg.get("result"):
                    _render_turn_result(st, msg["result"])

    # ---- input (chat box or injected sample) -------------------------
    injected = st.session_state.pop("pending_input", None)
    typed = st.chat_input("Paste a signal JSON, ask a follow-up, or type 'reset'…")
    user_input = injected or typed

    if user_input:
        st.session_state.history.append({"role": "user", "text": user_input})
        with st.chat_message("user"):
            st.code(user_input, language="json"
                    if user_input.strip().startswith("{") else "text")
        with st.chat_message("assistant"):
            with st.status("Running multi-agent workflow…", expanded=True) as status:
                result = session.handle_turn(user_input)
                status.update(label=f"Done · turn {result.get('turn')}",
                              state="complete")
            headline = (result.get("response") or "").splitlines()
            st.markdown(headline[0] if headline else "")
            _render_turn_result(st, result)
        st.session_state.history.append(
            {"role": "assistant",
             "text": (result.get("response") or "").splitlines()[0]
             if result.get("response") else "",
             "result": result})
        st.rerun()


if __name__ == "__main__":
    main()
