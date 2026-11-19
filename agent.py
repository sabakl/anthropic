"""Larkspur disruption agent. This is the file you build.

It runs right now, and it is wrong in four places. The trace shows each one
before the code does, so read the trace first:

    python3 run.py K7PQ2M --trace

Where you edit:   grep -n '✏' agent.py   (six marks, one per place)
Steps and gates:  https://anthropicpartnerbasecamp.bts.com/
"""
from __future__ import annotations
from typing import Any, Dict, List
from support import (MODEL, SYSTEM_PROMPT, call_local, execute_tool, mcp_client,
                     new_session, next_available_day, record_tool_result,
                     runtime_preamble)
import json
import re
from datetime import datetime
from support import mock_backend as backend
from support.tools import check_policy as _check_policy

MAX_TOOL_CALLS = 8  # Larkspur's own build capped the loop here; then a human takes over.

TONE_ADDENDUM = ""                       # ✏️ Build 4, step 4.1, intelligence goal
def cause_in_plain_words(cause_code: str, language: str = "en") -> Dict[str, Any]:
    """Why was my flight cancelled or delayed, in words I can repeat? Reads the
    customer-facing name of a cause from disruption_policy.json (cause_labels_customer)."""
    labels = backend.load_policy()["cause_labels_customer"]
    code = (cause_code or "").strip().upper()
    lang = (language or "en").strip().lower()
    if code not in labels:
        return {"error": f"Unknown cause_code {cause_code!r}. Valid codes: {', '.join(labels)}."}
    if lang not in labels[code]:
        return {"error": f"No customer wording in language {language!r}. Available: "
                         f"{', '.join(labels[code])}. For any other language, hand off to a human."}
    return {"cause_code": code, "language": lang, "label": labels[code][lang]}


# ── Shared helpers for the tools below ───────────────────────────────────────
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_HANDBOOK: Dict[str, Dict[str, str]] = {}


def _now() -> datetime:
    """The one 'now' of the sample data: fixture_clock in bookings.json."""
    return datetime.fromisoformat(backend.load_bookings()["fixture_clock"])


def _handbook_sections() -> Dict[str, Dict[str, str]]:
    """fare_rules_excerpt.md split into its numbered sections: {"6": {title, text}}."""
    if not _HANDBOOK:
        text = (backend.DATA_DIR / "fare_rules_excerpt.md").read_text(encoding="utf-8")
        for chunk in re.split(r"^### ", text, flags=re.MULTILINE)[1:]:
            m = re.match(r"(\d+)\.\s+(.*)", chunk)
            if m:
                _HANDBOOK[m.group(1)] = {"title": m.group(2).strip(), "text": "### " + chunk.strip()}
    return _HANDBOOK


def _flight_row(flight_no: str, date: str):
    fn = (flight_no or "").strip().upper()
    return next((r for r in backend.load_flights() if r["flight_no"] == fn and r["date"] == date), None)


# ── The tools ────────────────────────────────────────────────────────────────
def reopen_stats(intent_label: str = "", cause_code: str = "") -> Dict[str, Any]:
    """How often each kind of problem came back within 72 hours, from the sample transcripts."""
    path = backend.DATA_DIR / "transcripts_sample.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    matched = [r for r in rows
               if (not intent_label or r.get("intent_label") == intent_label)
               and (not cause_code or (r.get("disruption") or {}).get("cause_code") == cause_code.upper())]
    if not matched:
        labels = sorted({r.get("intent_label") for r in rows})
        return {"error": "No conversation matches that filter.", "intent_labels_available": labels,
                "cause_codes_available": sorted({(r.get("disruption") or {}).get("cause_code") for r in rows})}
    reopened = [r for r in matched if r.get("reopened_within_72h")]
    return {
        "sample_note": f"Based on only {len(rows)} conversations handled by people. Say 'in the "
                       "sample', never give it as a rate or a prediction.",
        "conversations_matched": len(matched),
        "reopened_within_72h": len(reopened),
        "conversations": [{"id": r["id"], "intent_label": r["intent_label"],
                           "cause_code": (r.get("disruption") or {}).get("cause_code"),
                           "reopened_within_72h": bool(r.get("reopened_within_72h")),
                           "reopen_reason": r.get("reopen_reason", "")} for r in matched],
    }


def care_entitlements(pnr: str, cause_code: str, delay_minutes: int, status: str,
                      wait_minutes_for_alternative: int = None) -> Dict[str, Any]:
    """Meal / hotel / ground care for this booking, with the Handbook's care section."""
    kwargs = {} if wait_minutes_for_alternative is None else {
        "wait_minutes_for_alternative": wait_minutes_for_alternative}
    policy = _check_policy(pnr, cause_code, delay_minutes, status, **kwargs)
    if "error" in policy:
        return policy
    return {"policy_row_id": policy["policy_row_id"], "care": policy["care"],
            "handbook_section_6": _handbook_sections()["6"]["text"]}


def departures_in_window(airport: str, date: str, from_time: str = "00:00",
                         to_time: str = "23:59") -> Dict[str, Any]:
    """Flights still leaving an airport in a time window, by estimated departure."""
    ap = (airport or "").strip().upper()
    if len(ap) != 3:
        return {"error": f"airport must be a three-letter code like DEN, got {airport!r}."}
    if not _ISO_DATE.match(date or ""):
        return {"error": f"date must be YYYY-MM-DD, got {date!r}."}
    if not (_HHMM.match(from_time or "") and _HHMM.match(to_time or "")):
        return {"error": "from_time and to_time must be 24-hour HH:MM, e.g. 12:00 and 18:00."}
    leaving, cancelled = [], 0
    for r in backend.load_flights():
        if r["origin"] != ap or r["date"] != date:
            continue
        est = backend._add_minutes(r["sched_dep_local"], r["delay_min"])
        if not (from_time <= est <= to_time):
            continue
        if r["status"] == "CANCELLED":
            cancelled += 1
            continue
        leaving.append({"flight_no": r["flight_no"], "dest": r["dest"], "status": r["status"],
                        "sched_dep_local": r["sched_dep_local"], "est_dep_local": est,
                        "delay_min": r["delay_min"], "seats_left_Y": r["seats_left_Y"],
                        "seats_left_J": r["seats_left_J"], "remarks": r["remarks"]})
    leaving.sort(key=lambda f: f["est_dep_local"])
    return {"airport": ap, "date": date, "window": f"{from_time}-{to_time}",
            "as_of": _now().isoformat(), "cancelled_in_window": cancelled, "departures": leaving}


def seats_left(flight_no: str, date: str, party_size: int = None) -> Dict[str, Any]:
    """Free seats in each cabin on one flight, and whether a party fits."""
    if not _ISO_DATE.match(date or ""):
        return {"error": f"date must be YYYY-MM-DD, got {date!r}."}
    r = _flight_row(flight_no, date)
    if r is None:
        return {"error": f"No flight {flight_no} on {date} in the schedule. It covers "
                         f"{backend.DATA_HORIZON_START} to {backend.DATA_HORIZON_END}."}
    out = {"flight_no": r["flight_no"], "date": date, "status": r["status"],
           "seats_left_Y": r["seats_left_Y"], "seats_left_J": r["seats_left_J"]}
    if party_size is not None:
        out["party_size"] = party_size
        out["fits_in_Y"] = r["seats_left_Y"] >= party_size
        out["fits_in_J"] = r["seats_left_J"] >= party_size
    return out


def ground_stop_status(airport: str) -> Dict[str, Any]:
    """Whether an airport is under a ground stop right now, and until when, from flight remarks."""
    ap = (airport or "").strip().upper()
    if len(ap) != 3:
        return {"error": f"airport must be a three-letter code like DEN, got {airport!r}."}
    now = _now()
    now_hhmm = now.strftime("%H:%M")
    evidence, windows = [], []
    for r in backend.load_flights():
        rem = r["remarks"] or ""
        if r["date"] == now.strftime("%Y-%m-%d") and re.search(rf"\b{ap}\b.*(ground stop|GDP)", rem, re.I):
            evidence.append({"flight_no": r["flight_no"], "remarks": rem})
            windows += re.findall(r"(\d\d:\d\d)-(\d\d:\d\d)", rem)
    base = {"airport": ap, "as_of": now.isoformat(), "evidence": evidence}
    if not evidence:
        return {**base, "ground_stop": False, "note": f"No ground stop for {ap} in the flight remarks."}
    if not windows:
        return {**base, "ground_stop": True, "until_local": None,
                "note": "A ground stop is mentioned but no end time is in the data. Say you do not know when it ends."}
    start, end = min(w[0] for w in windows), max(w[1] for w in windows)
    return {**base, "ground_stop": start <= now_hhmm <= end, "window_local": f"{start}-{end}",
            "until_local": end, "now_local": now_hhmm,
            "note": "Times are local (MDT). The end time is the data's, not a promise it will lift then."}


def hold_time_left(hold_id: str) -> Dict[str, Any]:
    """Minutes left on a seat hold, against the sample data's 'now'."""
    hid = (hold_id or "").strip().upper()
    hold = backend._holds.get(hid)
    if hold is None:
        if hid in backend._confirmed:
            return {"hold_id": hid, "status": "already_confirmed"}
        return {"error": f"No active hold {hold_id!r}. Holds come from hold_seat in this conversation."}
    now = _now()
    left = (hold["expires_at"] - now).total_seconds() / 60
    return {"hold_id": hid, "option_id": hold["option_id"], "expires_at": hold["expires_at"].isoformat(),
            "as_of": now.isoformat(), "minutes_left": max(0, round(left, 1)), "expired": left <= 0}


def what_automation_will_not_do() -> Dict[str, Any]:
    """Handbook section 7: what the chat is not allowed to do."""
    s = _handbook_sections()["7"]
    return {"title": s["title"], "text": s["text"]}


EXTRA_TOOLS: List[Dict[str, Any]] = [   # ✏️ Build 2, step 2.1: schemas for the tools you add
    {
        "name": "cause_in_plain_words",
        "description": (
            "Turn a disruption cause code into the customer-facing words Larkspur uses "
            "for it, so you can answer 'why was my flight cancelled or delayed?' in "
            "words the customer can repeat. Call it whenever you are about to tell a "
            "customer the cause, with the cause_code that get_flight_status returned, "
            "and say the label it returns, not the code and not your own wording. It "
            "gives the name of the cause only; it does not explain it, assign blame, or "
            "say what the customer is owed (that is check_policy)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"],
                               "description": "Cause code from get_flight_status for the disrupted flight"},
                "language": {"type": "string", "enum": ["en", "es"],
                             "description": "Language to reply in, from the booking's language. Defaults to en."},
            },
            "required": ["cause_code"],
        },
    },
    {
        "name": "reopen_stats",
        "description": (
            "Answer 'is this problem going to come back?' from eight real conversations "
            "handled by people: how many of a kind of problem were reopened within 72 hours, "
            "and why. It is a tiny sample, so say 'in the sample', never a rate or a promise. "
            "Filter by intent_label (e.g. rebook_after_cancellation, refund_vs_credit) or "
            "cause_code; with no filter it covers all eight. An unmatched filter lists the valid ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_label": {"type": "string", "description": "Kind of problem, e.g. rebook_after_cancellation. Optional."},
                "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"], "description": "Disruption cause. Optional."},
            },
            "required": [],
        },
    },
    {
        "name": "care_entitlements",
        "description": (
            "Answer 'do I get a meal or a hotel while I wait?' for this booking: the meal, "
            "hotel and ground-transport amounts and conditions. Pass what get_flight_status "
            "told you. It says WHAT this customer gets, not WHY: for 'why do I not get a "
            "hotel?' or the reason behind any rule, call fare_rules. It covers care only; for "
            "refunds, rebooking and goodwill use check_policy, and cite its policy_row_id "
            "before issuing any voucher."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pnr": {"type": "string", "description": "Confirmation code of the booking"},
                "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"], "description": "Cause code from get_flight_status"},
                "delay_minutes": {"type": "integer", "description": "Delay in minutes from get_flight_status; 0 if cancelled"},
                "status": {"type": "string", "enum": ["ON_TIME", "DELAYED", "CANCELLED", "DIVERTED"], "description": "Flight status from get_flight_status"},
                "wait_minutes_for_alternative": {"type": "integer", "description": "Minutes until the customer's next flight, if known. Optional."},
            },
            "required": ["pnr", "cause_code", "delay_minutes", "status"],
        },
    },
    {
        "name": "departures_in_window",
        "description": (
            "List what is still leaving an airport in a time window, by estimated departure "
            "(cancelled flights are counted, not listed), with status and seats left. Use for "
            "'what is still leaving Denver this afternoon?'. For the customer's own booking "
            "use search_alternatives. Dates run 2025-05-07 to 2025-05-09."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airport": {"type": "string", "description": "Departure airport code, e.g. DEN"},
                "date": {"type": "string", "description": "Local date, ISO format YYYY-MM-DD, e.g. 2025-05-08"},
                "from_time": {"type": "string", "description": "Window start, 24-hour HH:MM local. Defaults to 00:00. 'Afternoon' is 12:00 to 18:00."},
                "to_time": {"type": "string", "description": "Window end, 24-hour HH:MM local. Defaults to 23:59."},
            },
            "required": ["airport", "date"],
        },
    },
    {
        "name": "seats_left",
        "description": (
            "Check whether one named flight has room: free seats in economy (Y) and business "
            "(J), and whether a party of a given size fits. Use for 'is there room on that "
            "flight for the three of us?'. Pass party_size so you do not have to guess. It "
            "checks one flight only; it holds nothing (that is hold_seat)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_no": {"type": "string", "description": "Flight number, e.g. LK105"},
                "date": {"type": "string", "description": "Local departure date, ISO format YYYY-MM-DD"},
                "party_size": {"type": "integer", "description": "Number of passengers travelling together. Optional."},
            },
            "required": ["flight_no", "date"],
        },
    },
    {
        "name": "ground_stop_status",
        "description": (
            "Say whether an airport is under an air-traffic ground stop right now, and until "
            "when, from flight remarks and the current time. Use for 'is Denver still stopped, "
            "and until when?'. If it gives no end time, say you do not know; never promise "
            "when a stop lifts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"airport": {"type": "string", "description": "Airport code, e.g. DEN"}},
            "required": ["airport"],
        },
    },
    {
        "name": "hold_time_left",
        "description": (
            "Tell the customer how long they have before a held seat is released. Pass the "
            "hold_id that hold_seat returned. Use for 'how long do I have?'. It reads; it "
            "never extends or confirms a hold."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"hold_id": {"type": "string", "description": "hold_id returned by hold_seat, e.g. HOLD-AB12CD"}},
            "required": ["hold_id"],
        },
    },
    {
        "name": "what_automation_will_not_do",
        "description": (
            "Return Handbook section 7, the list of things this chat is not allowed to do "
            "(refunds, hotel approval, groups of 10 or more, unaccompanied minors, partner "
            "flights and more). Call it when a customer asks you to do something like 'just "
            "cancel the whole trip for me', so you can say plainly what a person must handle, "
            "then escalate_to_human."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]
# next_available_day and fare_rules are served by the MCP server (step 2.2): one owner each.
LOCAL_TOOLS: Dict[str, Any] = {          # ✏️ Build 2, step 2.1: the functions behind them
    "cause_in_plain_words": cause_in_plain_words,
    "reopen_stats": reopen_stats,
    "care_entitlements": care_entitlements,
    "departures_in_window": departures_in_window,
    "seats_left": seats_left,
    "ground_stop_status": ground_stop_status,
    "hold_time_left": hold_time_left,
    "what_automation_will_not_do": what_automation_will_not_do,
}


def text_of(response) -> str:
    """Given. The last non-empty text block, never content[0]."""
    texts = [b.text for b in response.content if getattr(b, "type", None) == "text" and b.text]
    return texts[-1] if texts else ""


def tool_results(response) -> List[Dict[str, Any]]:
    """Given. Runs every tool_use block and packages the results the way the
    API expects them back. A tool can live in three places: the MCP server,
    LOCAL_TOOLS, or support/tools.py."""
    # three branches, no try/except in this file: mcp_client.call_remote() and
    # support.call_local() answer with an error dict instead of raising, and both
    # record what came back on the trace
    results = []
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if block.name in mcp_client.tool_names:
            output = mcp_client.call_remote(block.name, block.input)
        elif block.name in LOCAL_TOOLS:
            output = call_local(LOCAL_TOOLS[block.name], block.name, block.input)
        else:
            output = execute_tool(block.name, block.input)
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": str(output),
        })
    return results


def run_agent(pnr: str, last_name: str, message: str) -> str:            # ✏️ Build 1, step 1.2
    """Run the tool loop until Claude stops asking for tools. Return its final text."""
    client, tracer = new_session()
    tools = tool_list()
    messages = [
        {"role": "user", "content": f"PNR {pnr}, last name {last_name}. {message}"},
    ]

    response = client.messages.create(
        model=MODEL, max_tokens=4096, system=runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
        thinking={"type": "adaptive"}, tools=tools, messages=messages,
    )

    turns = 1
    while response.stop_reason == "tool_use" and turns < MAX_TOOL_CALLS:
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results(response)})
        response = client.messages.create(
            model=MODEL, max_tokens=4096, system=runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
            thinking={"type": "adaptive"}, tools=tools, messages=messages,
        )
        turns += 1

    return text_of(response)


def tool_list() -> List[Dict[str, Any]]:                   # ✏️ Build 2, step 2.2
    """Given. Exactly what Claude is offered on every turn; run.py --show-tools
    prints this list."""
    return build_tools() + EXTRA_TOOLS + mcp_client.discover()


# ──────────────────────────────────────────────────────────────────────────────
# Below this line: what Claude is told about each tool. Step 1.3.
# The functions these describe are written and correct, in support/tools.py.
# ──────────────────────────────────────────────────────────────────────────────
def build_tools() -> List[Dict[str, Any]]:                 # ✏️ Build 1, step 1.3
    """Anthropic-shaped schemas: name, description, input_schema. What Claude is
    told about each of the nine tools, and all it is ever told."""
    return [
        {
            "name": "lookup_booking",
            "description": (
                "Retrieve a Larkspur reservation from Altura by confirmation code (PNR) "
                "and the passenger's last name. Both are required to prevent a lookup on "
                "a guessed PNR. Returns fare family, loyalty tier, the segment that needs "
                "attention, and any group/partner/minor/SSR flags relevant to scope."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}, "last_name": {"type": "string"}},
                "required": ["pnr", "last_name"],
            },
        },
        {
            "name": "get_flight_status",
            "description": (
                "Look up a Larkspur or Larkspur Link flight's current OpsFeed status for "
                "one local date: status, delay minutes, and cause. Use this before telling "
                "a customer anything about a flight's timing; never state it from memory."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "flight_no": {"type": "string", "description": "Flight number from the booking segment, e.g. LK255"},
                    "date": {"type": "string", "description": "Local departure date of that segment in ISO format YYYY-MM-DD, e.g. 2025-05-08. Not MM/DD/YYYY."},
                },
                "required": ["flight_no", "date"],
            },
        },
        {
            "name": "search_alternatives",
            "description": (
                "Find alternative flights for the disrupted segment on this booking: nonstop "
                "options on the same route, with option_id, date, times, cabin and seats "
                "available. Use when the customer wants to be rebooked or the flight is "
                "cancelled or badly delayed. The route, date, cabin and party size come from "
                "the booking, so you only pass the PNR. Pass an option_id to hold_seat to "
                "hold one."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string", "description": "Confirmation code of the booking to rebook"}},
                "required": ["pnr"],
            },
        },
        {
            "name": "check_policy",
            "description": (
                "Resolve what Larkspur owes this customer for the disruption: rebooking "
                "waiver, refund path, meal/hotel/ground care, goodwill eligibility and cap, "
                "and any escalation triggers. cause_code, delay_minutes and status describe "
                "what get_flight_status told you; fare_family, loyalty_tier and whether this "
                "is overnight are looked up from the booking, not asked of you. Every "
                "response carries a policy_row_id. Cite it if you reference this decision "
                "again."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"},
                    "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"]},
                    "delay_minutes": {"type": "integer"},
                    "status": {"type": "string", "enum": ["ON_TIME", "DELAYED", "CANCELLED", "DIVERTED"]},
                    "wait_minutes_for_alternative": {"type": "integer"},
                    "chosen_option_id": {"type": "string"},
                },
                "required": ["pnr", "cause_code", "delay_minutes", "status"],
            },
        },
        {
            "name": "hold_seat",
            "description": "Place a 15-minute hold on one alternative. Reversible. It simply expires.",
            "input_schema": {
                "type": "object",
                "properties": {"option_id": {"type": "string"}, "pnr": {"type": "string"}},
                "required": ["option_id", "pnr"],
            },
        },
        {
            "name": "confirm_rebooking",
            "description": (
                "Finalize a held seat. Irreversible. Requires a confirmation_token that "
                "only the customer's own Confirm-click can produce. You cannot supply it "
                "yourself, and 'the customer said yes' in chat does not substitute for it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"hold_id": {"type": "string"}, "confirmation_token": {"type": "string"}},
                "required": ["hold_id", "confirmation_token"],
            },
        },
        {
            "name": "issue_voucher",
            "description": (
                "Issue a meal, ground, hotel, or goodwill voucher. Auto-approves within the "
                "policy's threshold for that type; above it, returns a pending status for a "
                "human. It does not fail. Always pass the policy_row_id that made it eligible."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "voucher_type": {"type": "string", "enum": ["meal", "ground", "hotel", "goodwill"]},
                    "amount_usd": {"type": "number"},
                    "pnr": {"type": "string"},
                    "policy_row_id": {"type": "string"},
                },
                "required": ["voucher_type", "amount_usd", "pnr", "policy_row_id"],
            },
        },
        {
            "name": "escalate_to_human",
            "description": (
                "Hand this conversation to a human, with your reasoning attached. Use for "
                "groups, partner segments, unaccompanied minors, refunds, or anything else "
                "out of scope. This is the correct outcome for those cases, not a failure."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"}, "reason": {"type": "string"},
                    "summary_for_human": {"type": "string"}, "queue": {"type": "string"},
                },
                "required": ["pnr", "reason", "summary_for_human"],
            },
        },
        {
            "name": "send_confirmation",
            "description": "Send the customer a written confirmation of what was just done. Benign.",
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}, "message": {"type": "string"}},
                "required": ["pnr", "message"],
            },
        },
    ]
