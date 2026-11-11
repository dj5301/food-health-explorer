"""Side-by-side runs for the README: the full agent, a plain run_sql agent, and the bare model.

    uv run compare.py "Which New York counties have the highest adult obesity rate?"
    uv run compare.py --mode baseline "..."     # run_sql only: no mappings, glossary, guards or analysis tools
    uv run compare.py --mode notools "..."      # same model, no tools, no system prompt
    uv run compare.py --mode agent "..."        # the deployed agent (default when --mode is omitted: all three)

Each run starts a fresh session and prints a Markdown trace (tool calls, then the answer) to paste
into the README's "plain run_sql vs this agent" and "model without tools" sections.
"""

import argparse
import json
import sys

import app
from tools import TOOLS, new_state, run_tool

BASELINE_PROMPT = """You are a data analyst. Answer the user's question by querying a DuckDB database with the run_sql tool.
Tables (one row per US county, joined on fips): counties, food_access, food_stores, restaurants, food_assistance, food_insecurity, food_taxes, local_foods, food_health, socioeconomic, health_rankings, health_rankings_state.
Use DESCRIBE <table> or SELECT * ... LIMIT 3 to discover columns. Answer in plain English with the numbers you found."""

RUN_SQL_ONLY = [t for t in TOOLS if t["function"]["name"] == "run_sql"]


def run_plain(messages: list[dict], tools: list | None) -> tuple[str, list[dict]]:
    """The simplest loop: call tools until the model answers. No harness checks, no context tools."""
    calls = []
    state = new_state() | {"plain_sql": True}  # the baseline gets run_sql without the state-level warning
    for _ in range(app.MAX_TOOL_ROUNDS):
        reply = app.complete(messages, tools=tools)
        messages += [reply.model_dump()]
        if not reply.tool_calls:
            return reply.content or "", calls
        for call in reply.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            result = run_tool(call.function.name, args, state)
            calls += [{"name": call.function.name, "args": args, "result": result}]
            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]
    return "(tool-call limit reached)", calls


def trace_md(title: str, calls: list[dict], answer: str) -> str:
    out = [f"#### {title}", ""]
    for i, c in enumerate(calls, 1):
        if c["name"] == "run_sql":
            out += [f"{i}. `run_sql`", "", "   ```sql", "   " + c["args"].get("query", "").replace("\n", "\n   "), "   ```"]
        else:
            out += [f"{i}. `{c['name']}({json.dumps(c['args'])})`"]
        result = c["result"]
        out += ["", f"   → `{result[:300]}{'…' if len(result) > 300 else ''}`", ""]
    out += ["**Answer:**", "", answer.strip(), ""]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("question")
    p.add_argument("--mode", choices=["agent", "baseline", "notools", "all"], default="all")
    a = p.parse_args()
    modes = ["notools", "baseline", "agent"] if a.mode == "all" else [a.mode]

    for mode in modes:
        if mode == "notools":
            reply = app.complete([{"role": "user", "content": a.question}], tools=None)
            print(trace_md(f"Same model, no tools, no system prompt ({app.MODEL})", [], reply.content or ""))
        elif mode == "baseline":
            msgs = [{"role": "system", "content": BASELINE_PROMPT}, {"role": "user", "content": a.question}]
            answer, calls = run_plain(msgs, RUN_SQL_ONLY)
            print(trace_md("Plain run_sql agent (no mappings, glossary, guards or analysis tools)", calls, answer))
        else:
            session = app.get_session("compare")
            session["messages"] += [{"role": "user", "content": a.question}]
            answer, calls = app.run_agent(session["messages"], session["state"])
            print(trace_md("This agent", calls, answer))
        print("\n---\n")


if __name__ == "__main__":
    sys.exit(main())
