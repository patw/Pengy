"""Detect, seed and announce a skills directory.

A Pengy skill is a folder of plain files::

    ~/skills/
    ├── skill_index.md          # the table of contents Pengy reads
    ├── README.md               # the tutorial
    └── weather/
        └── weather_skill.md    # instructions, plus optional scripts

Nothing in Pengy's *code* reads that tree.  The **system message** is what
tells the model the index exists and to consult it before reaching for a
built-in tool, so a skills folder with a system message that never mentions it
is a very quiet dead end — which is exactly what a new user hits.

This module is the one place that knows how to

* resolve the folder (:func:`skills_root`, configurable via ``skills_dir``),
* report whether it is actually usable (:func:`scan`),
* copy the bundled starter skills in without clobbering anything
  (:func:`install`), and
* add the pointer sentence to a system message, idempotently
  (:func:`with_reference`).

It is Qt-free so the desktop Settings dialog, the CLI and the web UI can share
it.  Every path it touches is under the user's home directory; nothing is
installed system-wide.
"""
from __future__ import annotations

import filecmp
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

#: Config key holding the skills folder.  ``~/skills`` is the documented
#: default in docs/skills.md and skills/README.md, and it is what PengyOS
#: images seed through ``/etc/skel/skills``.
SKILLS_DIR_KEY = "skills_dir"
DEFAULT_SKILLS_DIR = "~/skills"

INDEX_NAME = "skill_index.md"
README_NAME = "README.md"
SKILL_SUFFIX = "_skill.md"

#: The one starter skill that is not finished by installing it — it needs an
#: interview, so the UI can point at it as the natural next step.
PROFILE_SKILL = "user_profile"
PROFILE_FILE = "profile.md"
PROFILE_TODO = "<!-- TODO -->"

#: The sentence the shipped starter skills need in the system message.  It is
#: deliberately phrased as a warning: an agent that skips the index re-solves
#: problems a skill already documents.
POINTER = (
    "WARNING: Always look at {index} before running web search, bash, running "
    "code or fetch_url! Skills should be used over tools!"
)


# ── locations ────────────────────────────────────────────────────────────────

def default_skills_source() -> Path:
    """Return the bundled starter-skill tree.

    Shipped as package data (``pengy/assets/skills``) so a plain
    ``pip install pengy`` carries it.  Raises :class:`RuntimeError` rather than
    silently installing nothing if the data is missing from the build.
    """
    source = Path(__file__).resolve().parent.parent / "assets" / "skills"
    if not source.is_dir():
        raise RuntimeError(f"Starter skills are missing from this install: {source}")
    return source


def _as_path(raw: str) -> Path:
    """Expand ``~`` and anchor a relative value at home (never the CWD)."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path.home() / path


def skills_root(config: dict | None = None) -> Path:
    """Resolve the skills folder, in this order:

    1. ``skills_dir`` from config — an explicit choice always wins;
    2. the folder named by the system message, if it points at an index that
       exists on disk (a fresh Syncthing fleet, or a kit moved after setup,
       otherwise looks like "no skills here");
    3. ``~/skills``, the documented default and what PengyOS seeds.
    """
    raw = ""
    if config:
        raw = str(config.get(SKILLS_DIR_KEY) or "").strip()
    if raw:
        return _as_path(raw)
    referenced = referenced_index_path((config or {}).get("system_message", ""))
    if referenced is not None and referenced.parent.is_dir():
        return referenced.parent
    return Path(DEFAULT_SKILLS_DIR).expanduser()


_INDEX_IN_TEXT = re.compile(
    r"(?:~|\$HOME|/)[^\s'\"`()<>\[\]]*" + re.escape(INDEX_NAME))


def referenced_index_path(message: str) -> Path | None:
    """Return the skill index a system message points at, or ``None``."""
    match = _INDEX_IN_TEXT.search(message or "")
    if not match:
        return None
    raw = match.group(0).replace("$HOME", str(Path.home()))
    try:
        return _as_path(raw)
    except (OSError, ValueError):  # pragma: no cover - defensive
        return None


def display_path(path: Path) -> str:
    """Abbreviate a path under ``$HOME`` to ``~/…`` for prose and prompts."""
    try:
        return "~/" + str(Path(path).expanduser().resolve().relative_to(Path.home()))
    except (ValueError, OSError):
        return str(path)


def pointer(index_path: Path | str) -> str:
    """The pointer sentence, naming *index_path* the way a human would."""
    return POINTER.format(index=display_path(Path(index_path)))


# ── detection ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SkillsStatus:
    """What is (or is not) on disk at the configured skills folder."""

    path: Path
    exists: bool
    has_index: bool
    skills: tuple[str, ...] = ()

    @property
    def index_path(self) -> Path:
        return self.path / INDEX_NAME

    @property
    def installed(self) -> bool:
        """True when the folder can actually be used: present *and* indexed."""
        return self.exists and self.has_index

    @property
    def count(self) -> int:
        return len(self.skills)

    def summary(self) -> str:
        """One sentence for a status line, without markup."""
        if not self.exists:
            return f"No skills folder at {display_path(self.path)}"
        if not self.has_index:
            return (f"{display_path(self.path)} exists but has no {INDEX_NAME} "
                    f"— Pengy has nothing to read")
        if not self.skills:
            return f"{display_path(self.path)} has an index but no skills yet"
        noun = "skill" if self.count == 1 else "skills"
        return f"{self.count} {noun} at {display_path(self.path)}"


def scan(config: dict | None = None) -> SkillsStatus:
    """Inspect the configured skills folder (read-only, never raises)."""
    root = skills_root(config)
    exists = root.is_dir()
    has_index = (root / INDEX_NAME).is_file() if exists else False
    names: list[str] = []
    if exists:
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith(".") \
                    and (child / f"{child.name}{SKILL_SUFFIX}").is_file():
                names.append(child.name)
    return SkillsStatus(path=root, exists=exists, has_index=has_index,
                        skills=tuple(names))


def profile_needs_interview(status: SkillsStatus) -> bool:
    """True when the profile skill is installed but never filled in.

    Marker-based on purpose: the file belongs to the user, so the only reliable
    signal that nobody has answered anything is the template still being
    untouched. A hand-edited profile that keeps the marker is theirs to keep.
    """
    if PROFILE_SKILL not in status.skills:
        return False
    try:
        text = (status.path / PROFILE_SKILL / PROFILE_FILE).read_text(encoding="utf-8")
    except OSError:
        return False
    return PROFILE_TODO in text


# ── installation ─────────────────────────────────────────────────────────────

@dataclass
class InstallReport:
    """What :func:`install` wrote, skipped or replaced."""

    target: Path
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.replaced)

    def summary(self) -> str:
        parts = []
        if self.created:
            parts.append(f"added {len(self.created)} file(s)")
        if self.replaced:
            parts.append(f"updated {len(self.replaced)} file(s)")
        if self.skipped:
            parts.append(f"left {len(self.skipped)} existing file(s) alone")
        return ", ".join(parts) or "nothing to do"


def install(config: dict | None = None, *, overwrite: bool = False,
            source: Path | None = None) -> InstallReport:
    """Copy the bundled starter skills into the configured folder.

    Non-destructive by default: a file that already exists is left alone, so
    running this on a folder the user has been editing adds what is missing and
    touches nothing else.  ``overwrite=True`` refreshes existing files from the
    bundle (what a "Restore starter skills" action would want).

    Returns an :class:`InstallReport`; raises :class:`RuntimeError` only when
    the bundle itself is missing.
    """
    source = Path(source) if source is not None else default_skills_source()
    if not source.is_dir():
        raise RuntimeError(f"Starter skills are missing from this install: {source}")

    target = skills_root(config)
    report = InstallReport(target=target)
    target.mkdir(parents=True, exist_ok=True)

    for src in sorted(source.rglob("*")):
        if not src.is_file():
            continue
        if src.name.startswith(".") or "__pycache__" in src.parts \
                or src.suffix == ".pyc":
            continue
        rel = src.relative_to(source)
        dst = target / rel
        if dst.is_file():
            if not overwrite or filecmp.cmp(src, dst, shallow=False):
                report.skipped.append(str(rel))
                continue
            report.replaced.append(str(rel))
        elif dst.exists():
            # A directory where the bundle has a file: never fight over it.
            report.skipped.append(str(rel))
            continue
        else:
            report.created.append(str(rel))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        # A wheel does not carry permission bits, so a helper script that is
        # executable in the repo arrives as 0644 and `./script.py` fails for
        # anyone who installed from PyPI. Make the documented form work.
        if dst.suffix in (".py", ".sh"):
            dst.chmod(dst.stat().st_mode | 0o111)

    return report


# ── system message ───────────────────────────────────────────────────────────

def references_skills(message: str) -> bool:
    """True when a system message already points at a skill index.

    Matches the bare filename rather than one spelling of the folder, so a
    hand-written pointer (``~/Personal/skills/skill_index.md`` on a Syncthing
    fleet, say) counts and is never duplicated.
    """
    return INDEX_NAME in (message or "")


def with_reference(message: str, index_path: Path | str) -> str:
    """Return *message* with the skills pointer appended, once.

    Idempotent: a message that already mentions the index is returned
    unchanged, so pressing the Settings button twice cannot stack sentences.
    """
    message = message or ""
    if references_skills(message):
        return message
    sentence = pointer(index_path)
    base = message.strip()
    return f"{base} {sentence}" if base else sentence
