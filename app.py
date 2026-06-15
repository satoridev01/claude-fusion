#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["textual", "httpx"]
# ///
# app.py: claude fusion tui (textual).
#   modes: panel (parallel), relay (sequential), debate (multi-round).
#   each mode can feed an optional judge and a final synthesizer.
import argparse
import datetime
import re
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, Input, Markdown

from config import MODELS, DEFAULT_PANEL, DEFAULT_JUDGE, DEFAULT_SYNTHESIZER, ANTHROPIC_API_KEY
from fusion import run_panel, run_relay, run_debate, run_judge, run_synth, label_results, err_detail


def _slug(text: str, maxlen: int) -> str:
    """lowercase a string into a filename-safe slug."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen].strip("-") or "untitled"


class Pane(VerticalScroll):
    """a non-prompt pane; focusable so q (handled by the app) can quit while it has focus."""

    can_focus = True
    content = ""  # latest markdown rendered, used by the ctrl+o export

    async def set(self, md: str) -> None:
        self.content = md
        await self.query_one(Markdown).update(md)


class PanelCard(Pane):
    """one scrollable card per panel participant."""

    def __init__(self, idx: int, key: str) -> None:
        super().__init__(id=f"card-{idx}")
        self.idx = idx
        self.border_title = MODELS[key]["label"]

    def compose(self) -> ComposeResult:
        yield Markdown("*(waiting)*", id=f"md-{self.idx}")


class SaveScreen(ModalScreen[str | None]):
    """ask for a filename; returns it on enter, None on escape."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    SaveScreen { align: center middle; background: $surface 70%; }
    SaveScreen > Input { width: 80%; border: round $accent; }
    """

    def __init__(self, default_name: str) -> None:
        super().__init__()
        self._default = default_name

    def compose(self) -> ComposeResult:
        inp = Input(value=self._default, id="savename")
        inp.border_title = "save to file (enter to confirm, esc to cancel)"
        yield inp

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # don't let it bubble to the main app's submit handler
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ClaudeFusion(App):
    """fusion tui: panel/relay/debate row, optional judge, final synthesis."""

    TITLE = "Claude Fusion · the head of the Kwik-E-Mart corporation"
    ENABLE_COMMAND_PALETTE = False  # drop the "palette" hint in the footer

    CSS = """
    Screen { layout: vertical; }
    #panel-row { height: 33%; }
    PanelCard { border: round $primary; margin: 0 1; padding: 0 1; }
    PanelCard:focus { border: heavy $primary; background: $boost; }
    #judge { height: 1fr; border: round $accent; margin: 1 1 0 1; padding: 0 1; }
    #judge:focus { border: heavy $accent; background: $boost; }
    #final { height: 1fr; border: round $success; margin: 1 1 0 1; padding: 0 1; }
    #final:focus { border: heavy $success; background: $boost; }
    Input { dock: bottom; margin: 1; border: round $primary; }
    Input:focus { border: heavy $primary; }
    """
    # ctrl+q / ctrl+c quit from anywhere (priority, so they fire even in the
    # input). plain q quits too, but only when the input is not focused (see
    # on_key) so you can still type the letter q in the prompt.
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+c", "quit", show=False, priority=True),
        Binding("ctrl+o", "save", "Save"),        # export the focused pane to a .md file
        Binding("ctrl+t", "cycle_mode", "Mode"),  # not ctrl+m: terminals fold ctrl+m into Enter
        Binding("ctrl+l", "clear", "Clear"),
        Binding("escape", "stop", "Stop"),        # interrupt the running analysis
    ]

    def __init__(self, panel, judge, synth, mode, use_judge, rounds) -> None:
        super().__init__()
        self.panel = panel
        self.judge = judge
        self.synth = synth
        self.mode = mode
        self.use_judge = use_judge
        self.rounds = rounds
        self._last_query = ""  # for the export filename
        self._cost = 0.0       # running usd cost shown on the prompt's bottom border

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="panel-row"):
            for i, key in enumerate(self.panel):
                yield PanelCard(i, key)
        judge = Pane(id="judge")
        judge.border_title = f"Judge · {MODELS[self.judge]['label']}" if self.use_judge else "Judge · disabled"
        with judge:
            yield Markdown("*(the judge analyzes after the panel)*", id="md-judge")
        final = Pane(id="final")
        final.border_title = f"Final fusion · {MODELS[self.synth]['label']}"
        with final:
            yield Markdown("*(fused answer appears at the end)*", id="md-final")
        prompt = Input(placeholder="type a query and press enter…", id="q")
        prompt.border_title = self._mode_bar()
        prompt.border_subtitle = f"${self._cost:.4f}"  # live cost meter, bottom-right of the prompt
        yield prompt
        yield Footer()

    def on_mount(self) -> None:
        """start with the focus on the query input so the user can type immediately."""
        self.query_one("#q", Input).focus()
        self._apply_mode_layout()

    def _apply_mode_layout(self) -> None:
        """hide the judge pane in debate (there is no judge) so Final fusion gets the space."""
        self.query_one("#judge").display = self.mode != "debate"

    def on_key(self, event: events.Key) -> None:
        """q quits whenever the focus is not the prompt input (there q is just a character)."""
        if event.key == "q" and not isinstance(self.focused, Input):
            event.stop()
            self.exit()

    def action_stop(self) -> None:
        """escape: interrupt the running analysis."""
        if self.workers.cancel_group(self, "fuse"):
            self.notify("stopped", timeout=2)

    def action_save(self) -> None:
        """ctrl+o: write the focused pane's output to a .md file, asking for the name."""
        pane = self.focused
        if not isinstance(pane, Pane):
            self.notify("Select a pane with Tab first, then Ctrl+O to save.", severity="warning", timeout=3)
            return
        date = datetime.date.today().isoformat()
        box = str(pane.border_title or pane.id or "pane")
        default = f"{date}-{_slug(box, 40)}-{_slug(self._last_query, 60)}.md"

        def write(name: str | None) -> None:
            if not name:
                return
            try:
                Path(name).write_text(pane.content, encoding="utf-8")
                self.notify(f"saved → {name}")
            except Exception as e:
                self.notify(f"save failed: {err_detail(e)}", severity="error", timeout=5)

        self.push_screen(SaveScreen(default), write)

    async def action_cycle_mode(self) -> None:
        """ctrl+t: cycle panel -> relay -> debate, cancelling any running analysis and resetting the panes."""
        self.workers.cancel_group(self, "fuse")  # stop the current analysis, if any
        order = ("panel", "relay", "debate")
        self.mode = order[(order.index(self.mode) + 1) % len(order)]
        self.query_one("#q", Input).border_title = self._mode_bar()
        self._apply_mode_layout()
        await self.action_clear()
        self.notify(f"mode: {self.mode}", timeout=2)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "q":  # ignore submits from other inputs (e.g. the save dialog)
            return
        q = event.value.strip()
        if not q:
            return
        inp = self.query_one("#q", Input)
        inp.value = ""
        if q.lower() in ("/quit", "/exit"):
            self.exit()
            return
        inp.placeholder = q  # keep the last query visible (light gray) until the user types again
        self._last_query = q
        # run as a worker so ctrl+t can cancel it; exclusive cancels a previous run
        self.run_worker(self.fuse(q), exclusive=True, group="fuse")

    def _mode_bar(self) -> str:
        """'mode:' label with the active mode highlighted, for the input border title."""
        parts = [f"[b reverse] {m} [/]" if m == self.mode else f"[dim]{m}[/]" for m in ("panel", "relay", "debate")]
        return "mode: " + " ".join(parts)

    async def _run_mode(self, query: str, on_update, on_result, on_cost) -> list[dict]:
        """dispatch to the configured collaboration mode."""
        if self.mode == "relay":
            return await run_relay(self.panel, query, on_update=on_update, on_result=on_result, on_cost=on_cost)
        if self.mode == "debate":
            return await run_debate(self.panel, query, self.rounds, on_update=on_update, on_result=on_result, on_cost=on_cost)
        return await run_panel(self.panel, query, on_update=on_update, on_result=on_result, on_cost=on_cost)

    async def fuse(self, query: str) -> None:
        """run the selected mode, then the optional judge, then the synthesizer (moderator in debate)."""
        cards = {c.idx: c for c in self.query(PanelCard)}
        debate = self.mode == "debate"
        transcript: dict[int, list[str]] = {}  # answers per card, one entry per debate round
        labels: dict[int, str] = {}
        for c in cards.values():
            c.border_subtitle = ""  # clear any round marker from a prior run

        def card_md(idx: int, status: str | None = None) -> str:
            head = f"*({labels[idx]})*\n\n" if idx in labels else ""
            entries = transcript.get(idx, [])
            if not entries:
                return head + (f"*{status}*" if status else "*(waiting)*")
            if len(entries) == 1:
                body = entries[0]
            else:  # debate: stack the rounds instead of replacing
                body = "\n\n---\n\n".join(f"**Round {i + 1}**\n\n{e}" for i, e in enumerate(entries))
            return head + body + (f"\n\n*{status}*" if status else "")

        async def on_update(idx: int, status: str) -> None:
            if debate:  # round this model is working on, shown bottom-right of its box
                cards[idx].border_subtitle = f"round {len(transcript.get(idx, [])) + 1}/{self.rounds}"
            await cards[idx].set(card_md(idx, status))

        async def on_result(r: dict) -> None:  # accumulate so debate rounds stack, keeping prior rounds visible
            label = f"temp {r['temp']}" if r["temp"] is not None else "default sampling"
            if r.get("no_tools"):
                label += " · ⚠️ no web tools"
            labels[r["idx"]] = label
            transcript.setdefault(r["idx"], []).append(r["text"])
            if debate:
                cards[r["idx"]].border_subtitle = f"round {len(transcript[r['idx']])}/{self.rounds}"
            await cards[r["idx"]].set(card_md(r["idx"]))

        async def on_cost(delta: float) -> None:  # live cost meter on the prompt's bottom border
            self._cost += delta
            self.query_one("#q", Input).border_subtitle = f"${self._cost:.4f}"

        results = sorted(await self._run_mode(query, on_update, on_result, on_cost), key=lambda r: r["idx"])

        # debate skips the judge: the panelists already critique each other, and the
        # final synthesis acts as the moderator writing the consensus.
        judge_pane = self.query_one("#judge", Pane)
        if self.use_judge and not debate:
            await judge_pane.set("*analyzing and ranking…*")
            try:
                analysis, labeled = await run_judge(self.judge, query, results, on_cost=on_cost)
                await judge_pane.set(analysis)
            except Exception as e:
                await judge_pane.set(f"⚠️ judge error: {err_detail(e)}")
                return
        else:
            await judge_pane.set("*(debate: the panelists critique each other)*" if debate else "*(judge disabled)*")
            analysis, labeled = "", label_results(results)

        final_pane = self.query_one("#final", Pane)
        await final_pane.set("*writing the consensus…*" if debate else "*fusing the final answer…*")
        try:
            final = await run_synth(self.synth, query, labeled, analysis, moderator=debate, on_cost=on_cost)
            await final_pane.set(final)
        except Exception as e:
            await final_pane.set(f"⚠️ synthesis error: {err_detail(e)}")

    async def action_clear(self) -> None:
        for c in self.query(PanelCard):
            c.border_subtitle = ""
            await c.set("*(waiting)*")
        await self.query_one("#judge", Pane).set("*(cleared)*")
        await self.query_one("#final", Pane).set("*(cleared)*")


def main() -> None:
    """parse args and launch the tui."""
    ap = argparse.ArgumentParser(description="claude fusion (panel / relay / debate + judge + synthesis)")
    ap.add_argument("--mode", default="panel", choices=["panel", "relay", "debate"], help="how the panel collaborates")
    ap.add_argument("--panel", nargs="+", default=DEFAULT_PANEL, help=f"panel models, repeatable (from {list(MODELS)})")
    ap.add_argument("--judge", default=DEFAULT_JUDGE, choices=list(MODELS))
    ap.add_argument("--synth", default=DEFAULT_SYNTHESIZER, choices=list(MODELS))
    ap.add_argument("--no-judge", dest="use_judge", action="store_false", help="skip the judge; synthesize from the raw answers")
    ap.add_argument("--rounds", type=int, default=3, help="debate rounds (mode=debate only)")
    args = ap.parse_args()
    if not ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY is not set. Export it first:\n  export ANTHROPIC_API_KEY=sk-ant-...")
    for k in (*args.panel, args.judge, args.synth):
        if k not in MODELS:
            raise SystemExit(f"unknown model: {k}. options: {list(MODELS)}")
    ClaudeFusion(panel=args.panel, judge=args.judge, synth=args.synth, mode=args.mode, use_judge=args.use_judge, rounds=args.rounds).run()


if __name__ == "__main__":
    main()
