"""Shared About info — edition name, links, and blurb for all three frontends.

Keep the description/links here in sync with README.md and the Rust/C++ editions'
own About screens (PengyR, PengyCPP) so all editions present the same facts.
"""
from datetime import datetime

from pengy import __version__

GITHUB_URL = "https://github.com/patw/Pengy"
WEBSITE_URL = "https://pengy.catbee.ca"
LICENSE_URL = f"{GITHUB_URL}/blob/main/LICENSE"
LICENSE_NAME = "MIT License"

# The year Pengy was first published — kept in sync with LICENSE's copyright year.
_FOUNDING_YEAR = 2026

DESCRIPTION = (
    "Pengy is a local-first AI agent that connects to any OpenAI-compatible API "
    "(OpenAI, Ollama, vLLM, Groq, OpenRouter, or a local endpoint) and gives the "
    "model tools to operate on your filesystem, run code, search the web, and "
    "more — all with your approval."
)

CATBEE_URL = "https://catbee.ca"
CATBEE_BLURB = (
    "Pengy is part of Catbee — a collection of open-source, self-hosted AI tools "
    "for hyper-personal computing, designed to be self-hosted, fully controllable, "
    "and yours to own."
)


def edition_line(edition: str) -> str:
    """e.g. edition_line('Python') -> 'Pengy Python - 1.8.1'"""
    return f"Pengy {edition} - {__version__}"


def copyright_line() -> str:
    """e.g. 'Copyright © 2026 Pat Wendorf (dungeons@gmail.com)', ranged once the year rolls over."""
    year = datetime.now().year
    year_str = str(_FOUNDING_YEAR) if year <= _FOUNDING_YEAR else f"{_FOUNDING_YEAR}–{year}"
    return f"Copyright © {year_str} Pat Wendorf (dungeons@gmail.com)"
