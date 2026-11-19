# agent.py: changes so far (before → after)

Baseline = last commit (`de05b64`). Compared against the current working copy.
Gates cleared with these changes: 1.2, 1.3, 1.4.

## 1. Step 1.2: `run_agent` loop (3 changes)

**Why:** Turn 2 failed with a 400: `tool_result` had no matching `tool_use` in the
previous message. The assistant turn was saved as plain text, which dropped the
`tool_use` block, and the returned answer came from the turn that asked for tools.

### 1a. Send the whole assistant turn back

Before:
```python
messages.append({"role": "assistant", "content": text_of(response)})
```
After:
```python
messages.append({"role": "assistant", "content": response.content})
```

### 1b. Drop the stale `answer` variable

Before:
```python
answer = ""
...
answer = text_of(response)   # inside the loop
```
After: both lines removed.

### 1c. Return the final response's text

Before:
```python
return answer
```
After:
```python
return text_of(response)
```

## 2. Step 1.3: tool descriptions in `build_tools` (2 tools)

**Why:** `get_flight_status` told Claude to send `MM/DD/YYYY`, but the tool rejects
anything but `YYYY-MM-DD` (turn 2 of the first good trace showed the error and a retry).
`search_alternatives` had a one-word description, so nothing told Claude when to use it.

### 2a. `get_flight_status` field descriptions

Before:
```python
"flight_no": {"type": "string"},
"date": {"type": "string", "description": "MM/DD/YYYY"},
```
After:
```python
"flight_no": {"type": "string", "description": "Flight number from the booking segment, e.g. LK255"},
"date": {"type": "string", "description": "Local departure date of that segment in ISO format YYYY-MM-DD, e.g. 2025-05-08. Not MM/DD/YYYY."},
```

### 2b. `search_alternatives` description and `pnr` field

Before:
```python
"description": "search",
...
"properties": {"pnr": {"type": "string"}},
```
After:
```python
"description": (
    "Find alternative flights for the disrupted segment on this booking: nonstop "
    "options on the same route, with option_id, date, times, cabin and seats "
    "available. Use when the customer wants to be rebooked or the flight is "
    "cancelled or badly delayed. The route, date, cabin and party size come from "
    "the booking, so you only pass the PNR. Pass an option_id to hold_seat to "
    "hold one."
),
...
"properties": {"pnr": {"type": "string", "description": "Confirmation code of the booking to rebook"}},
```

## 3. Step 1.4

No code change. The gate passed on the first run.

## Not touched yet

- `TONE_ADDENDUM`, `EXTRA_TOOLS`, `LOCAL_TOOLS` (Builds 2 and 4)
- `tool_list` (step 2.2)
- Tool descriptions other than the two above

## Raw diff

```diff
@@ -65,19 +65,17 @@ def run_agent(pnr: str, last_name: str, message: str) -> str:
         thinking={"type": "adaptive"}, tools=tools, messages=messages,
     )
 
-    answer = ""
     turns = 1
     while response.stop_reason == "tool_use" and turns < MAX_TOOL_CALLS:
-        messages.append({"role": "assistant", "content": text_of(response)})
+        messages.append({"role": "assistant", "content": response.content})
         messages.append({"role": "user", "content": tool_results(response)})
-        answer = text_of(response)
         response = client.messages.create(
             model=MODEL, max_tokens=4096, system=runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
             thinking={"type": "adaptive"}, tools=tools, messages=messages,
         )
         turns += 1
 
-    return answer
+    return text_of(response)
 
@@ -118,18 +116,25 @@ def build_tools():
-                    "flight_no": {"type": "string"},
-                    "date": {"type": "string", "description": "MM/DD/YYYY"},
+                    "flight_no": {"type": "string", "description": "Flight number from the booking segment, e.g. LK255"},
+                    "date": {"type": "string", "description": "Local departure date of that segment in ISO format YYYY-MM-DD, e.g. 2025-05-08. Not MM/DD/YYYY."},
@@
-            "description": "search",
+            "description": (
+                "Find alternative flights for the disrupted segment on this booking: nonstop "
+                "options on the same route, with option_id, date, times, cabin and seats "
+                "available. Use when the customer wants to be rebooked or the flight is "
+                "cancelled or badly delayed. The route, date, cabin and party size come from "
+                "the booking, so you only pass the PNR. Pass an option_id to hold_seat to "
+                "hold one."
+            ),
-                "properties": {"pnr": {"type": "string"}},
+                "properties": {"pnr": {"type": "string", "description": "Confirmation code of the booking to rebook"}},
```
