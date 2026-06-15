# Claude Fusion

Local take on OpenRouter's **Fusion** pattern, within the Claude family. A panel of N Claude models answers a query, an optional **judge** ranks them, and a **synthesizer** writes the final fused answer. The panel can work three ways: **panel** (parallel), **relay** (sequential), **debate** (multi-round).

```
┌──────────┬──────────┬──────────┐
│ Sonnet   │ Opus 4.6 │ Opus 4.7 │   ← panel mode (parallel, blind)
│ temp 0.3 │ temp 0.7 │ sampling │   ← Opus 4.7 ignores temperature
├──────────┴──────────┴──────────┤
│ Judge · Opus 4.7 (optional)    │   ← analysis + ranking, blind by default
├────────────────────────────────┤
│ Final fusion · Opus 4.8        │   ← fused answer, ready to use
└────────────────────────────────┘
```

## Install

Set your key (read from the environment):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

```bash
# uv, without installing
uvx --from git+https://github.com/satoridev01/claude-fusion.git claude-fusion

# uv or pip, installs the `claude-fusion` command
uv tool install git+https://github.com/satoridev01/claude-fusion.git
pip install git+https://github.com/satoridev01/claude-fusion.git

# local checkout
uv run app.py
```

## Run

```bash
claude-fusion                                                    # panel haiku, sonnet, opus
claude-fusion --mode relay                                       # sequential, each model reads the prior answers
claude-fusion --mode debate --rounds 3                           # multi-round critique then converge
claude-fusion --panel sonnet sonnet opus                         # self fusion of sonnet
claude-fusion --no-judge                                         # skip the judge; synthesize from raw answers
claude-fusion --panel haiku sonnet --judge opus --synth sonnet   # mixed roles
```

Keys: **Enter** runs, **Esc** interrupts a running analysis, **Ctrl+L** clears, **Ctrl+T** cycles the mode, **Ctrl+O** saves the focused pane to a `.md` file (Tab to focus a pane first). Quit with **Ctrl+C** or `/quit` from the prompt, or **q** when any other pane has focus.

## Modes

| Mode | Flow | Character |
|------|------|-----------|
| `panel` | all models answer in parallel, blind to each other | diverse, independent |
| `relay` | models answer one after another, each reading the prior answers | cumulative, builds up |
| `debate` | opening answers, then `--rounds` of mutual critique and revision (3 rounds by default) | adversarial, converges |

In `debate` the judge is skipped — the panelists already critique each other — and the final synthesis acts as the moderator writing the consensus. `panel` and `relay` use the judge unless `--no-judge`.

Every model gets Anthropic's server-side `web_search` / `web_fetch`, so they can read URLs you paste and verify claims instead of trusting their training cutoff.

## Configure

Defaults live in `config.py`:

- `DEFAULT_PANEL`, `DEFAULT_JUDGE`, `DEFAULT_SYNTHESIZER`: models used when the flags are omitted.
- `BLIND_JUDGING`: anonymize answers for the judge (on by default).
- `ENABLE_WEB_TOOLS`: give every model web search/fetch to verify claims (on by default).
- `PANEL_TEMPERATURES`: temperatures cycled across the panel (ignored by models that reject the param, like Opus 4.8).
- `MAX_TOKENS`, `TIMEOUT_S`: per-call output cap and HTTP timeout.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`: read from the environment.
