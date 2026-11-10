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
              run_call: Callable[[str, dict], str]) -> dict | None:
    """Run the tool loop; return the model's final JSON answer, or None if it gave none."""
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    reply: dict = {"content": "", "tool_calls": []}
    for _ in range(max_rounds):
        reply = llm.chat(messages, tools=tools)
        if not reply["tool_calls"]:
            break
        messages.append({"role": "assistant", "content": reply["content"],
                         "tool_calls": [{"function": c} for c in reply["tool_calls"]]})
        for call in reply["tool_calls"]:
            messages.append({"role": "tool", "tool_name": call["name"],
                             "content": run_call(call["name"], call["arguments"])})
    answer = parse_json(reply["content"]) if not reply["tool_calls"] else None
    if answer is None:
        messages.append({"role": "user", "content": "Answer now with the JSON object only."})
        answer = parse_json(llm.chat(messages, json_output=True)["content"])
    return answer


def ask_json(llm: ChatLLM, system: str, user: str) -> dict | None:
    """One turn without tools, JSON output (used by triage)."""
    reply = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_output=True)
    return parse_json(reply["content"])
