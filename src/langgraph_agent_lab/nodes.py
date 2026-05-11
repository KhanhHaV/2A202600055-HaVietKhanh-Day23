"""Node skeletons for the LangGraph workflow.

Each function should be small, testable, and return a partial state update. Avoid mutating the
input state in place.
"""

from __future__ import annotations

from .state import AgentState, ApprovalDecision, Route, make_event


def intake_node(state: AgentState) -> dict:
    """Normalize raw query into state fields and mask PII."""
    import re

    query = state.get("query", "").strip()
    # Simple PII mask for emails
    masked_query = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", "[EMAIL]", query)
    
    return {
        "query": masked_query,
        "messages": [f"intake: {masked_query[:40]}..."],
        "events": [make_event("intake", "completed", "query normalized and PII masked")],
    }


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route using keyword-based heuristics.

    Priority: RISKY > TOOL > MISSING_INFO > ERROR > SIMPLE
    """
    import re

    query = state.get("query", "").lower()
    
    # Define keywords
    risky_keywords = {"refund", "delete", "send", "cancel", "remove", "revoke"}
    tool_keywords = {"status", "order", "lookup", "check", "track", "find", "search"}
    error_keywords = {"timeout", "fail", "failure", "error", "crash", "unavailable"}
    
    # Word boundary check helper
    def contains_any(text, keywords):
        for k in keywords:
            if re.search(rf"\b{k}\b", text):
                return True
        return False

    route = Route.SIMPLE
    risk_level = "low"

    # 1. RISKY (Highest Priority)
    if contains_any(query, risky_keywords):
        route = Route.RISKY
        risk_level = "high"
    # 2. TOOL
    elif contains_any(query, tool_keywords):
        route = Route.TOOL
    # 3. MISSING_INFO
    # Heuristic: < 5 words AND contains pronouns like "it"
    else:
        words = re.findall(rf"\b\w+\b", query)
        if len(words) < 5 and "it" in words:
            route = Route.MISSING_INFO
        # 4. ERROR
        elif contains_any(query, error_keywords):
            route = Route.ERROR
    
    return {
        "route": route.value,
        "risk_level": risk_level,
        "events": [make_event("classify", "completed", f"route={route.value}")],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information instead of hallucinating."""
    query = state.get("query", "")
    if len(query.split()) < 3:
        question = "I'm sorry, your request is too brief. Could you please provide more details about what you need help with?"
    else:
        question = "I understand you need help with something specific, but I need more details (like an order ID or specific error message) to proceed."
    
    return {
        "pending_question": question,
        "final_answer": question,
        "events": [make_event("clarify", "completed", "missing information requested")],
    }


def tool_node(state: AgentState) -> dict:
    """Call a mock tool.

    Simulates transient failures for error-route scenarios to demonstrate retry loops.
    TODO(student): implement idempotent tool execution and structured tool results.
    """
    attempt = int(state.get("attempt", 0))
    if state.get("route") == Route.ERROR.value and attempt < 2:
        result = f"ERROR: transient failure attempt={attempt} scenario={state.get('scenario_id', 'unknown')}"
    else:
        result = f"mock-tool-result for scenario={state.get('scenario_id', 'unknown')}"
    return {
        "tool_results": [result],
        "events": [make_event("tool", "completed", f"tool executed attempt={attempt}")],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a risky action for approval."""
    query = state.get("query", "")
    risk_level = state.get("risk_level", "high")
    
    proposed_action = f"Execute high-impact action: {query}"
    evidence = "Action involves sensitive financial or account data (refund/delete/send)."
    
    return {
        "proposed_action": f"{proposed_action}. Reason: {evidence}",
        "events": [make_event("risky_action", "pending_approval", f"approval required for {risk_level} risk")],
    }


def approval_node(state: AgentState) -> dict:
    """Human approval step with optional LangGraph interrupt().

    Set LANGGRAPH_INTERRUPT=true to use real interrupt() for HITL demos.
    Default uses mock decision so tests and CI run offline.

    TODO(student): implement reject/edit decisions and timeout escalation.
    """
    import os

    if os.getenv("LANGGRAPH_INTERRUPT", "").lower() == "true":
        from langgraph.types import interrupt

        value = interrupt({
            "proposed_action": state.get("proposed_action"),
            "risk_level": state.get("risk_level"),
        })
        if isinstance(value, dict):
            decision = ApprovalDecision(**value)
        else:
            decision = ApprovalDecision(approved=bool(value))
    else:
        decision = ApprovalDecision(approved=True, comment="mock approval for lab")
    return {
        "approval": decision.model_dump(),
        "events": [make_event("approval", "completed", f"approved={decision.approved}")],
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Record a retry attempt or fallback decision.

    TODO(student): implement bounded retry, exponential backoff metadata, and fallback route.
    """
    attempt = int(state.get("attempt", 0)) + 1
    errors = [f"transient failure attempt={attempt}"]
    return {
        "attempt": attempt,
        "errors": errors,
        "events": [make_event("retry", "completed", "retry attempt recorded", attempt=attempt)],
    }


def answer_node(state: AgentState) -> dict:
    """Produce a final response grounded in tool results."""
    tool_results = state.get("tool_results", [])
    approval = state.get("approval")
    
    if tool_results:
        latest_result = tool_results[-1]
        answer = f"Based on our internal tools, here is the result: {latest_result}"
        if approval and approval.get("approved"):
            answer += " (Action was approved by an administrator)"
    elif state.get("pending_question"):
        answer = state["pending_question"]
    else:
        answer = "I've processed your request successfully."
        
    return {
        "final_answer": answer,
        "events": [make_event("answer", "completed", "answer generated")],
    }


def evaluate_node(state: AgentState) -> dict:
    """Evaluate tool results to determine if a retry is needed."""
    tool_results = state.get("tool_results", [])
    latest = tool_results[-1] if tool_results else ""
    
    # Check for various error markers
    error_markers = ["ERROR", "TIMEOUT", "FAILURE", "FAILED"]
    needs_retry = any(marker in latest.upper() for marker in error_markers)
    
    if needs_retry:
        return {
            "evaluation_result": "needs_retry",
            "events": [make_event("evaluate", "completed", "tool result indicates failure, retry needed")],
        }
    return {
        "evaluation_result": "success",
        "events": [make_event("evaluate", "completed", "tool result satisfactory")],
    }


def dead_letter_node(state: AgentState) -> dict:
    """Log unresolvable failures for manual review."""
    attempt = state.get("attempt", 0)
    scenario_id = state.get("scenario_id", "unknown")
    
    final_msg = f"CRITICAL: Request {scenario_id} failed after {attempt} attempts. Escalating to manual support."
    
    return {
        "final_answer": final_msg,
        "events": [make_event("dead_letter", "completed", f"max retries exceeded, attempt={attempt}")],
    }


def finalize_node(state: AgentState) -> dict:
    """Finalize the run and emit a final audit event."""
    return {"events": [make_event("finalize", "completed", "workflow finished")]}
