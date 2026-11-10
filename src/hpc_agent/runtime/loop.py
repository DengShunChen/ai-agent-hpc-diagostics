"""The harness loop shared by every LLM-driven agent.

    model -> tool calls? -> harness runs them (run_call) -> results back to the model -> ...
          -> no tool calls: the reply should be the final JSON answer

The model never executes anything; it only asks. `run_call` decides what happens to each
request. The loop stops after `max_rounds`; if the last reply is not usable JSON, it asks
once more for the JSON alone (without tools) and gives up after that.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from hpc_agent.llm.provider import ChatLLM
from hpc_agent.runtime.trace import Emit, no_trace

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_json(text: str) -> dict | None:
    match = _JSON_OBJECT.search(text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def run_agent(llm: ChatLLM, system: str, user: str, tools: list[dict], max_rounds: int,
              run_call: Callable[[str, dict], str], emit: Emit = no_trace) -> dict | None:
    """Run the tool loop; return the model's final JSON answer, or None if it gave none."""
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    emit("prompt", system=system, user=user, tools=[t.get("function", {}).get("name") for t in tools])
    reply: dict = {"content": "", "tool_calls": []}
    for round_ in range(1, max_rounds + 1):
        reply = llm.chat(messages, tools=tools)
        emit("model_reply", round=round_, content=reply["content"],
             tool_calls=[{"name": c["name"], "arguments": c["arguments"]} for c in reply["tool_calls"]])
        if not reply["tool_calls"]:
            break
        assistant: dict = {"role": "assistant", "content": reply["content"],
                           "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                          for c in reply["tool_calls"]]}
        if reply.get("provider_state") is not None:
            assistant["provider_state"] = reply["provider_state"]  # passed back unchanged (e.g. Claude)
        messages.append(assistant)
        for call in reply["tool_calls"]:
            result: dict = {"role": "tool", "tool_name": call["name"],
                            "content": run_call(call["name"], call["arguments"])}
            if call.get("id"):
                result["tool_call_id"] = call["id"]
            messages.append(result)
    answer = parse_json(reply["content"]) if not reply["tool_calls"] else None
    if answer is None:
        # Tools stay declared (some providers require it once tool calls are in the history);
        # the Ollama provider drops them for this JSON-only turn.
        messages.append({"role": "user", "content": "Answer now with the JSON object only."})
        final = llm.chat(messages, tools=tools, json_output=True)["content"]
        emit("model_reply", round="final-json", content=final, tool_calls=[])
        answer = parse_json(final)
    return answer


def ask_json(llm: ChatLLM, system: str, user: str, emit: Emit = no_trace) -> dict | None:
    """One turn without tools, JSON output (used by triage)."""
    emit("prompt", system=system, user=user, tools=[])
    reply = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_output=True)
    emit("model_reply", round=1, content=reply["content"], tool_calls=[])
    return parse_json(reply["content"])
