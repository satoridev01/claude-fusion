#!/usr/bin/env python3
# fusion.py: claude fusion engine.
#   panel:  N models answer in parallel, blind to each other (diverse).
#   relay:  N models answer sequentially, each reading the prior answers.
#   debate: N models answer, then critique and revise over several rounds.
# any mode can feed an optional judge and a final synthesizer.
import asyncio
import random
import string
import httpx

from config import MODELS, MAX_TOKENS, TIMEOUT_S, ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, ANTHROPIC_VERSION, BLIND_JUDGING, PANEL_TEMPERATURES, ENABLE_WEB_TOOLS, WEB_TOOLS

# web tools handed to every model call (panel, judge and synthesizer) so they
# can verify claims and read urls instead of trusting a stale training cutoff.
TOOLS = WEB_TOOLS if ENABLE_WEB_TOOLS else None


def _api_error(r) -> str:
    """pull the api's own error message out of a non-2xx response."""
    try:
        return f"api {r.status_code}: {r.json()['error']['message']}"
    except Exception:
        return f"api {r.status_code}: {r.text[:200]}"


def err_detail(e: Exception) -> str:
    """format an exception with its type, so empty-message errors (timeouts, cancellations) are still legible."""
    detail = str(e)
    return f"{type(e).__name__}: {detail}" if detail else type(e).__name__


def _cost(key: str, usage: dict) -> float:
    """usd cost of one response from its usage block (web-search billing not included)."""
    in_rate, out_rate = MODELS[key].get("price", (0.0, 0.0))
    inp = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) * 1.25 + usage.get("cache_read_input_tokens", 0) * 0.1
    return (inp * in_rate + usage.get("output_tokens", 0) * out_rate) / 1_000_000


async def _call(key: str, prompt: str, temperature: float | None, tools: list | None = None, on_cost=None) -> tuple[str, bool]:
    """call the messages api and run the server-side tool loop. returns (text, tools_dropped): if the model rejects the tools with a 400 we retry without them and flag it, so a dead web-tools feature is visible instead of silent."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("missing ANTHROPIC_API_KEY in the environment.")
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    messages = [{"role": "user", "content": prompt}]
    use_tools = tools
    dropped = False
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        for _ in range(8):  # bound the pause_turn continuation and the one-shot tool fallback
            payload = {"model": MODELS[key]["model"], "max_tokens": MAX_TOKENS, "messages": messages}
            if temperature is not None:  # None means omit the param; see _effective_temp for the per-model rule
                payload["temperature"] = temperature
            if use_tools:
                payload["tools"] = use_tools
            r = await client.post(f"{ANTHROPIC_BASE_URL}/v1/messages", json=payload, headers=headers)
            if r.status_code == 400 and use_tools:  # model can't use the tools; retry without them, but remember it
                use_tools = None
                dropped = True
                messages = [{"role": "user", "content": prompt}]
                continue
            if r.status_code >= 400:
                raise RuntimeError(_api_error(r))
            data = r.json()
            if on_cost and data.get("usage"):  # report cost as each response lands, so the meter is live
                await on_cost(_cost(key, data["usage"]))
            if data.get("stop_reason") == "pause_turn":  # server tools hit the iteration cap; resend to resume
                messages = messages + [{"role": "assistant", "content": data["content"]}]
                continue
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
            return text, dropped
    return "⚠️ tool loop did not converge", dropped


def _result(idx: int, key: str, temp: float | None, text: str) -> dict:
    """build one panelist result dict; temp is None when the model ignores temperature."""
    return {"idx": idx, "key": key, "label": MODELS[key]["label"], "temp": temp, "text": text}


def _effective_temp(key: str, temperature: float) -> float | None:
    """single source of truth for the temperature actually sent: None when the model rejects the param (opus 4.8 / fable 400)."""
    return temperature if MODELS[key].get("temperature") else None


def _panel_temp(idx: int) -> float:
    """configured temperature for panelist idx, cycled across PANEL_TEMPERATURES."""
    return PANEL_TEMPERATURES[idx % len(PANEL_TEMPERATURES)]


# panel: parallel, blind
async def run_panel(panel: list[str], query: str, on_update=None, on_result=None, on_cost=None) -> list[dict]:
    """run the panel in parallel and return one dict per answer."""
    async def one(idx: int, key: str) -> dict:
        temp = _effective_temp(key, _panel_temp(idx))
        if on_update:
            await on_update(idx, "generating…")
        try:
            text, dropped = await _call(key, query, temp, tools=TOOLS, on_cost=on_cost)
        except Exception as e:
            text, dropped = f"⚠️ error: {err_detail(e)}", False
        r = _result(idx, key, temp, text)
        r["no_tools"] = bool(TOOLS) and dropped  # tools were on but this model couldn't use them
        if on_result:  # hand the finished answer back so the card shows it right away
            await on_result(r)
        return r

    return await asyncio.gather(*(one(i, k) for i, k in enumerate(panel)))


# relay: sequential, each panelist reads the prior answers
def build_relay_prompt(query: str, prior: list[dict]) -> str:
    """build a relay prompt that includes the answers given so far."""
    if not prior:
        return query
    block = "\n".join(f"### {r['label']}\n{r['text']}\n" for r in prior)
    return f"You are part of a sequential panel. Earlier panelists answered the query below, and their answers follow. Build on what is correct, fix what is wrong, and add anything missing. Give your own complete answer, not a critique.\n\n## QUERY\n{query}\n\n## EARLIER ANSWERS\n{block}\n## YOUR ANSWER\n"


async def run_relay(panel: list[str], query: str, on_update=None, on_result=None, on_cost=None) -> list[dict]:
    """run the panel sequentially; each panelist sees the prior answers."""
    results: list[dict] = []
    for idx, key in enumerate(panel):
        temp = _effective_temp(key, _panel_temp(idx))
        if on_update:
            await on_update(idx, "generating…")
        try:
            text, dropped = await _call(key, build_relay_prompt(query, results), temp, tools=TOOLS, on_cost=on_cost)
        except Exception as e:
            text, dropped = f"⚠️ error: {err_detail(e)}", False
        r = _result(idx, key, temp, text)
        r["no_tools"] = bool(TOOLS) and dropped
        if on_result:  # show this answer immediately, not after the whole relay finishes
            await on_result(r)
        results.append(r)
    return results


# debate: parallel rounds of critique and revision
def build_debate_prompt(query: str, mine: str, others: list[dict], round_no: int) -> str:
    """build a debate prompt with the panelist's own answer and the others'."""
    block = "\n".join(f"### {o['label']}\n{o['text']}\n" for o in others)
    return f"You are in a multi-round debate (round {round_no}). The query is below, followed by your own latest answer and the other panelists' latest answers. Critique the others where they are wrong, defend or revise your own position in light of theirs, and produce your updated best answer.\n\n## QUERY\n{query}\n\n## YOUR LATEST ANSWER\n{mine}\n\n## OTHER PANELISTS\n{block}\n## YOUR UPDATED ANSWER\n"


async def run_debate(panel: list[str], query: str, rounds: int, on_update=None, on_result=None, on_cost=None) -> list[dict]:
    """run an opening parallel round, then `rounds - 1` revision rounds."""
    current = await run_panel(panel, query, on_update, on_result, on_cost)
    for rnd in range(1, rounds):
        async def revise(r: dict) -> dict:
            others = [o for o in current if o["idx"] != r["idx"]]
            if on_update:
                await on_update(r["idx"], f"round {rnd + 1}…")
            try:
                text, dropped = await _call(r["key"], build_debate_prompt(query, r["text"], others, rnd + 1), r["temp"], tools=TOOLS, on_cost=on_cost)
            except Exception as e:
                text, dropped = f"⚠️ error: {err_detail(e)}", False
            nr = _result(r["idx"], r["key"], r["temp"], text)
            nr["no_tools"] = bool(TOOLS) and dropped
            if on_result:  # update the card after each round's revision
                await on_result(nr)
            return nr

        current = await asyncio.gather(*(revise(r) for r in current))
    return current


# canonical labeling (shared judge ↔ synthesizer)
def label_results(results: list[dict]) -> list[tuple[str, dict]]:
    """assign A/B/C tags to the answers, shuffling order when blind judging is on."""
    ordered = results[:]
    if BLIND_JUDGING:
        random.shuffle(ordered)
    letters = string.ascii_uppercase
    return [(letters[i], r) for i, r in enumerate(ordered)]


def _render_answers(labeled: list[tuple[str, dict]], show_model: bool) -> str:
    """render the labeled answers into a markdown block for a prompt."""
    block = []
    for tag, r in labeled:
        head = f"### Answer {tag} ({r['label']})" if show_model else f"### Answer {tag}"
        block.append(f"{head}\n{r['text']}\n")
    return "\n".join(block)


# judge
def build_judge_prompt(query: str, labeled: list[tuple[str, dict]]) -> str:
    """build the judge prompt from the labeled answers."""
    if BLIND_JUDGING:
        ident = "The answers are anonymized (A, B, C). Judge purely on quality, without speculating about which model wrote each one."
        answers_block = _render_answers(labeled, show_model=False)
    else:
        ident = ""
        answers_block = _render_answers(labeled, show_model=True)
    return f"You are a rigorous, impartial technical judge. I am giving you a query and several candidate answers. {ident}\n\nFor each answer assess correctness, completeness, and risks. Then rank them from best to worst and explain why. Do not favor an answer just because it is longer.\n\nYou have web_search and web_fetch tools. Your training has a cutoff and may be out of date, so when an answer makes a factual claim you are unsure about (recent events, product or model names, versions, dates, prices), VERIFY it with the tools before calling it wrong. Do not dismiss a claim as fabricated just because you do not recognize it.\n\n## QUERY\n{query}\n\n## ANSWERS\n{answers_block}\n## YOUR ANALYSIS\nDeliver: (1) a brief evaluation of each one, (2) the final ranking, (3) the concrete elements the ideal answer should contain."


async def run_judge(judge_key: str, query: str, results: list[dict], on_cost=None) -> tuple[str, list[tuple[str, dict]]]:
    """run the judge and return (analysis, labeled) so the synthesizer reuses the same tags."""
    labeled = label_results(results)
    prompt = build_judge_prompt(query, labeled)
    analysis, dropped = await _call(judge_key, prompt, _effective_temp(judge_key, 0.2), tools=TOOLS, on_cost=on_cost)
    if bool(TOOLS) and dropped:  # surface that the judge ranked without web access
        analysis = "> ⚠️ this judge model could not use web tools — judged from training only.\n\n" + analysis
    return analysis, labeled


# synthesizer
def build_synth_prompt(query: str, labeled: list[tuple[str, dict]], judge_analysis: str, moderator: bool = False) -> str:
    """build the synthesizer prompt; moderator=True frames it as a debate moderator writing the consensus."""
    answers_block = _render_answers(labeled, show_model=False)
    judge_section = f"## JUDGE ANALYSIS\n{judge_analysis}\n\n" if judge_analysis else ""
    tools_note = "You have web_search and web_fetch tools. Your training has a cutoff, so verify any uncertain or time-sensitive fact (recent events, product or model names, versions, dates, prices) with the tools rather than relying on memory; prefer current information over your training."
    if moderator:
        intro = f"You are the moderator of a debate. The panelists argued and below are their final positions. Write the single best consensus answer: state what they agree on, resolve their disagreements on the merits, and fill any gaps. {tools_note}"
        body = f"## QUERY\n{query}\n\n## FINAL POSITIONS\n{answers_block}\n{judge_section}## CONSENSUS ANSWER\n"
    else:
        intro = f"You are the final synthesizer. You have the query, the candidate answers, and (when present) a judge analysis. Write the best possible answer: take what is correct from the candidates, fix what was flagged as deficient, and fill the gaps. If a candidate was already optimal you may build on it; if none was, build something better. Deliver a polished final answer ready to use, with no meta commentary. {tools_note} Do not drop a candidate's correct up-to-date claim just because the judge or your training disagrees."
        body = f"## QUERY\n{query}\n\n## CANDIDATES\n{answers_block}\n{judge_section}## FINAL FUSED ANSWER\n"
    return f"{intro}\n\n{body}"


async def run_synth(synth_key: str, query: str, labeled: list[tuple[str, dict]], judge_analysis: str, moderator: bool = False, on_cost=None) -> str:
    """run the synthesizer (or debate moderator) and return the final answer."""
    prompt = build_synth_prompt(query, labeled, judge_analysis, moderator=moderator)
    final, dropped = await _call(synth_key, prompt, _effective_temp(synth_key, 0.4), tools=TOOLS, on_cost=on_cost)
    if bool(TOOLS) and dropped:  # surface that the final answer was written without web access
        final = "> ⚠️ this synthesizer model could not use web tools — synthesized from training only.\n\n" + final
    return final
