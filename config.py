# config.py: claude fusion. panel of N claude models plus judge plus synthesizer.
# inspired by openrouter fusion, but entirely within the claude family.
import os

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
ANTHROPIC_VERSION = "2023-06-01"

# catalog of claude models available for the panel, judge and synthesizer.
# "temperature": whether the model accepts the temperature param. opus 4.7/4.8
# and fable reject it with a 400, so we omit it for those and let default
# sampling variance drive diversity instead.
# "price": (input, output) usd per million tokens, for the live cost meter.
MODELS = {
    "sonnet": {"model": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "temperature": True, "price": (3.0, 15.0)},
    "opus45": {"model": "claude-opus-4-5", "label": "Claude Opus 4.5", "temperature": True, "price": (5.0, 25.0)},
    "opus46": {"model": "claude-opus-4-6", "label": "Claude Opus 4.6", "temperature": True, "price": (5.0, 25.0)},
    "opus47": {"model": "claude-opus-4-7", "label": "Claude Opus 4.7", "temperature": False, "price": (5.0, 25.0)},
    "opus48": {"model": "claude-opus-4-8", "label": "Claude Opus 4.8", "temperature": False, "price": (5.0, 25.0)},
}

# panel: the models that answer in parallel. they can repeat (self fusion).
DEFAULT_PANEL = ["sonnet", "opus46", "opus47"]

# judge: analyzes the panel answers (ranking plus critique).
DEFAULT_JUDGE = "opus47"

# synthesizer: writes the final answer using the judge analysis.
DEFAULT_SYNTHESIZER = "opus48"

# anti brand bias: hide from the judge which model produced each answer.
BLIND_JUDGING = True

# different temperature per run to encourage diversity in self fusion.
PANEL_TEMPERATURES = [0.3, 0.7, 1.0]  # cycled across the panel

# server-side web tools so the answering models can read URLs and search the
# web (they run on anthropic's side). set to False for plain, tool-free prompts.
ENABLE_WEB_TOOLS = True
WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]

MAX_TOKENS = 4096
TIMEOUT_S = 300
