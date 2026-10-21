"""Decision-oriented Council rounds, with bounded clarification and option ballots."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from novacode_cli.council import PERSONAS, _content_text, run_council


def _json(text: str) -> dict:
    try:
        value = json.loads(
            text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        )
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


async def run_decision(
    topic: str,
    model: Any,
    *,
    options: list[str],
    criteria: str,
    ask,
    personas=None,
    history=None,
    member_models=None,
):
    """Clarify, independently advise, then vote for actual options, never inventing votes."""
    members = personas or PERSONAS
    models = member_models or {}
    brief = (
        f"{topic}\nDecision options: {json.dumps(options)}\nCriteria and constraints: {criteria}"
    )
    clarifications = []
    for member in members:
        try:
            response = await asyncio.wait_for(
                models.get(member.id, model).ainvoke(
                    [
                        (
                            "system",
                            member.system
                            + '\nAsk at most one essential question that could change your recommendation. Return JSON {"question":"...", "choices":["...","..."]}. Return {} if none. Do not answer the decision yet.',
                        ),
                        ("human", brief + "\nAlready clarified: " + json.dumps(clarifications)),
                    ]
                ),
                60,
            )
            question = _json(_content_text(response))
        except Exception:
            question = {}
        if isinstance(question.get("question"), str) and question["question"].strip():
            choices = question.get("choices", [])
            choices = (
                [c[:200] for c in choices[:6] if isinstance(c, str)]
                if isinstance(choices, list)
                else []
            )
            event = {
                "type": "question",
                "id": member.id,
                "name": member.name,
                "question": question["question"][:1000],
                "choices": choices,
            }
            yield event
            answer = await ask(event)
            clarifications.append({"question": event["question"], "answer": answer})
            yield {"type": "question_answered", "id": member.id, "answer": answer}
    brief += "\nUser clarifications: " + json.dumps(clarifications)
    brief += "\nCompare the options against the criteria. Include benefits, risks, assumptions, your preferred option, a concrete next step, and what would change your mind."
    transcript = []
    async for event in run_council(
        brief, model, personas=members, history=history, member_models=models, advice_only=True
    ):
        if event["type"] == "agent_done" and event.get("text", "").strip():
            transcript.append(event)
        if event["type"] in ("vote_start", "vote", "verdict", "done"):
            continue
        yield event
    if not transcript:
        yield {"type": "council_error", "message": "No advisor produced an answer. Check the configured models."}
        yield {"type": "done"}
        return
    yield {"type": "vote_start", "options": options}
    tally = {option: 0 for option in options}

    async def ballot(member):
        try:
            response = await asyncio.wait_for(
                models.get(member.id, model).ainvoke(
                    [
                        (
                            "system",
                            member.system
                            + '\nVote for one exact option, or abstain. Return JSON {"choice":"exact option", "reason":"tradeoffs and criteria behind your vote"}.',
                        ),
                        ("human", brief + "\nIndependent opinions: " + json.dumps(transcript)),
                    ]
                ),
                60,
            )
            vote = _json(_content_text(response))
        except Exception:
            vote = {}
        choice = vote.get("choice")
        if not isinstance(choice, str):
            choice = None
        return {
            "type": "vote",
            "voter": member.id,
            "voter_name": member.name,
            "choice": choice if choice in tally else None,
            "choice_name": choice if choice in tally else "Abstain",
            "reason": str(vote.get("reason", "No valid ballot received."))[:2000],
        }

    for vote in await asyncio.gather(
        *(ballot(m) for m in members if any(t["id"] == m.id for t in transcript))
    ):
        if vote["choice"]:
            tally[vote["choice"]] += 1
        yield vote
    top = max(tally.values())
    leaders = [o for o in options if tally[o] == top] if top else []
    winner = leaders[0] if len(leaders) == 1 else "No consensus"
    answer = f"Recommendation: {winner}.\n\n" + "\n".join(
        f"{o}: {n} vote(s)" for o, n in tally.items()
    )
    if len(leaders) > 1:
        answer += (
            "\n\nTied options: "
            + ", ".join(leaders)
            + ". Resolve the trade-offs before committing."
        )
    answer += "\n\nReview the advisors’ risks and assumptions above. The final choice is yours."
    yield {
        "type": "verdict",
        "winner_id": "",
        "winner_name": winner,
        "tally": {},
        "option_tally": tally,
        "votes": sum(tally.values()),
        "tied": len(leaders) > 1,
        "answer": answer,
    }
    yield {"type": "done"}
