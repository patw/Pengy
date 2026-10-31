"""Tests for the starter-skills installer (no Qt needed).

The core module is what the Settings → Extras tab drives, but it is
deliberately Qt-free so the CLI and web UI can grow the same action later.
These tests pin the two promises a user actually relies on: installing never
destroys a file that is already there, and it never adds the system-message
pointer twice.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pengy.core import skills_install as si
from pengy.core.config import DEFAULTS


# ── locations ────────────────────────────────────────────────────────────────

def test_bundled_starter_tree_is_packaged():
    source = si.default_skills_source()
    assert source.is_dir()
    assert (source / si.INDEX_NAME).is_file()
    assert (source / si.README_NAME).is_file()
    for name in ("user_profile", "memory", "pengy_bio", "botskills", "sysinfo"):
        assert (source / name / f"{name}{si.SKILL_SUFFIX}").is_file()


def test_bundled_index_lists_exactly_the_bundled_skills():
    """An index row without a folder (or the reverse) is a silent dead end.

    The bundle is deliberately *not* the same set as the repo's `skills/`
    tutorial (decided 2026-10-31: that folder teaches the patterns, this one is
    a curated starter set), so nothing pins them together.
    """
    source = si.default_skills_source()
    on_disk = {d.name for d in source.iterdir()
               if d.is_dir() and (d / f"{d.name}{si.SKILL_SUFFIX}").is_file()}
    index = (source / si.INDEX_NAME).read_text(encoding="utf-8")
    listed = set()
    for line in index.splitlines():
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1].strip()
        if cell and not set(cell) <= {"-"}:
            listed.add(cell.strip("/"))
    listed.discard("Dir")
    assert on_disk == listed
    # …and the scan sees all of them, so "Install Skills" cannot report success
    # on a folder it only half understands.
    status = si.scan({si.SKILLS_DIR_KEY: str(source)})
    assert set(status.skills) == on_disk


def test_skills_root_prefers_config_then_message_then_default(tmp_path):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    assert si.skills_root({si.SKILLS_DIR_KEY: str(explicit)}) == explicit

    # No skills_dir: the folder the system message names is used, but only if
    # it really exists — a stale pointer must not send an install to nowhere.
    pointed = tmp_path / "synced"
    pointed.mkdir()
    message = f"You are Pengy. Read {si.display_path(pointed)}/{si.INDEX_NAME} first."
    assert si.skills_root({"system_message": message}) == pointed

    stale = tmp_path / "gone" / si.INDEX_NAME
    assert si.skills_root({"system_message": f"Read {stale} first."}) \
        == Path(si.DEFAULT_SKILLS_DIR).expanduser()

    assert si.skills_root({}) == Path(si.DEFAULT_SKILLS_DIR).expanduser()
    assert si.skills_root(DEFAULTS) == Path(si.DEFAULT_SKILLS_DIR).expanduser()


def test_skills_root_anchors_a_relative_value_at_home():
    assert si.skills_root({si.SKILLS_DIR_KEY: "skills"}) == Path.home() / "skills"
    assert si.skills_root({si.SKILLS_DIR_KEY: "~/x"}) == Path.home() / "x"


def test_report_summary_counts_what_happened(tmp_path):
    target = tmp_path / "skills"
    report = si.install({si.SKILLS_DIR_KEY: str(target)})
    assert report.changed
    assert "added" in report.summary()
    quiet = si.install({si.SKILLS_DIR_KEY: str(target)})
    assert not quiet.changed
    assert quiet.summary().startswith("left ") and "alone" in quiet.summary()


def test_display_path_abbreviates_home_only(tmp_path):
    assert si.display_path(Path.home() / "skills") == "~/skills"
    assert si.display_path(tmp_path) == str(tmp_path)


# ── scan ─────────────────────────────────────────────────────────────────────

def test_scan_reports_missing_folder(tmp_path):
    status = si.scan({si.SKILLS_DIR_KEY: str(tmp_path / "absent")})
    assert not status.exists
    assert not status.installed
    assert status.count == 0
    assert "No skills folder" in status.summary()


def test_scan_needs_an_index_to_count_as_installed(tmp_path):
    (tmp_path / "weather").mkdir()
    (tmp_path / "weather" / "weather_skill.md").write_text("# Weather\n")
    status = si.scan({si.SKILLS_DIR_KEY: str(tmp_path)})
    assert status.exists and not status.has_index
    assert not status.installed
    assert status.skills == ("weather",)  # found, but unusable without an index
    assert si.INDEX_NAME in status.summary()


def test_scan_ignores_folders_without_a_skill_markdown(tmp_path):
    (tmp_path / si.INDEX_NAME).write_text("# Skills Index\n")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "real_skill.md").write_text("# Real\n")
    (tmp_path / "notes").mkdir()          # documentation, not a skill
    (tmp_path / ".hidden").mkdir()        # never a skill
    (tmp_path / ".hidden" / ".hidden_skill.md").write_text("x")
    status = si.scan({si.SKILLS_DIR_KEY: str(tmp_path)})
    assert status.installed
    assert status.skills == ("real",)


# ── install ──────────────────────────────────────────────────────────────────

def test_install_writes_starter_tree_into_a_missing_folder(tmp_path):
    target = tmp_path / "skills"
    report = si.install({si.SKILLS_DIR_KEY: str(target)})

    assert target.is_dir()
    assert (target / si.INDEX_NAME).is_file()
    assert report.created and not report.skipped and not report.replaced
    assert "skill_index.md" in report.created
    assert si.scan({si.SKILLS_DIR_KEY: str(target)}).installed
    # a skill may ship data files next to its instructions, and they come too
    assert (target / "user_profile" / "profile.md").is_file()


def test_install_never_clobbers_a_file_the_user_has_edited(tmp_path):
    target = tmp_path / "skills"
    target.mkdir()
    mine = target / si.INDEX_NAME
    mine.write_text("# My own index\n")

    report = si.install({si.SKILLS_DIR_KEY: str(target)})

    assert mine.read_text() == "# My own index\n"
    assert "skill_index.md" in report.skipped
    assert (target / "botskills" / "botskills_skill.md").is_file()  # still added what was missing


def test_install_overwrite_refreshes_changed_files_only(tmp_path):
    target = tmp_path / "skills"
    si.install({si.SKILLS_DIR_KEY: str(target)})
    index = target / si.INDEX_NAME
    index.write_text("# Edited\n")

    report = si.install({si.SKILLS_DIR_KEY: str(target)}, overwrite=True)

    assert index.read_text() == (si.default_skills_source() / si.INDEX_NAME).read_text()
    assert "skill_index.md" in report.replaced
    # unchanged files are not rewritten
    assert "botskills/botskills_skill.md" in report.skipped
    assert not report.created


def test_profile_skill_starts_as_an_unanswered_template(tmp_path):
    target = tmp_path / "skills"
    si.install({si.SKILLS_DIR_KEY: str(target)})
    status = si.scan({si.SKILLS_DIR_KEY: str(target)})
    assert si.profile_needs_interview(status)

    # Once it is filled in, the tab stops advertising the interview.
    profile = target / si.PROFILE_SKILL / si.PROFILE_FILE
    profile.write_text(profile.read_text(encoding="utf-8").replace(
        si.PROFILE_TODO, "Prefers terse answers."), encoding="utf-8")
    assert not si.profile_needs_interview(
        si.scan({si.SKILLS_DIR_KEY: str(target)}))


def test_profile_needs_interview_is_false_without_the_skill(tmp_path):
    (tmp_path / si.INDEX_NAME).write_text("# Skills Index\n", encoding="utf-8")
    assert not si.profile_needs_interview(
        si.scan({si.SKILLS_DIR_KEY: str(tmp_path)}))


def test_install_rerun_is_a_no_op(tmp_path):
    target = tmp_path / "skills"
    si.install({si.SKILLS_DIR_KEY: str(target)})
    report = si.install({si.SKILLS_DIR_KEY: str(target)})
    assert not report.changed
    assert not report.created and not report.replaced


def test_install_survives_a_directory_where_the_bundle_has_a_file(tmp_path):
    target = tmp_path / "skills"
    (target / "README.md").mkdir(parents=True)  # user made a folder with that name
    report = si.install({si.SKILLS_DIR_KEY: str(target)})
    assert (target / "README.md").is_dir()
    assert "README.md" in report.skipped
    assert (target / si.INDEX_NAME).is_file()


def test_install_raises_a_clear_error_when_the_bundle_is_missing(tmp_path):
    with pytest.raises(RuntimeError, match="missing from this install"):
        si.install({si.SKILLS_DIR_KEY: str(tmp_path / "skills")},
                   source=tmp_path / "nope")


# ── system message ───────────────────────────────────────────────────────────

def test_pointer_names_the_index_the_way_a_human_would(tmp_path):
    text = si.pointer(Path.home() / "skills" / si.INDEX_NAME)
    assert "~/skills/skill_index.md" in text
    assert "Skills should be used over tools" in text


def test_with_reference_appends_once_and_is_idempotent(tmp_path):
    index = tmp_path / si.INDEX_NAME
    base = "You are Pengy. The current date is {date}."
    once = si.with_reference(base, index)
    assert once.startswith(base)
    assert "skill_index.md" in once
    assert si.with_reference(once, index) == once   # pressing twice cannot stack


def test_with_reference_fills_an_empty_message(tmp_path):
    assert si.with_reference("", tmp_path / si.INDEX_NAME).startswith("WARNING:")


def test_with_reference_respects_a_hand_written_pointer(tmp_path):
    custom = "Read ~/Personal/skills/skill_index.md before bash."
    assert si.with_reference(custom, tmp_path / si.INDEX_NAME) == custom


def test_references_skills_matches_any_index_spelling():
    assert si.references_skills("see ~/skills/skill_index.md")
    assert si.references_skills("see /opt/x/skill_index.md")
    assert not si.references_skills("You are a helpful assistant.")


# ── the bundled scripts actually run ─────────────────────────────────────────

def _memory(*args, env_store, exec_bit=True):
    import os
    import subprocess
    import sys

    script = si.default_skills_source() / "memory" / "memory.py"
    env = dict(os.environ, PENGY_MEMORY_FILE=str(env_store))
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, text=True, env=env, check=False)


def test_memory_skill_add_search_forget(tmp_path):
    import json

    store = tmp_path / "memory.jsonl"

    assert _memory("add", "--text", "Tests need QT_QPA_PLATFORM=offscreen",
                   "--subject", "pengy", "--tags", "tests,qt",
                   env_store=store).returncode == 0
    assert _memory("add", "--text", "Pat prefers terse answers",
                   env_store=store).returncode == 0

    found = _memory("search", "--q", "offscreen", env_store=store)
    assert found.returncode == 0
    assert "QT_QPA_PLATFORM" in found.stdout

    # every term must match; a miss exits non-zero so `search && act` is safe
    assert _memory("search", "--q", "offscreen nomatch",
                   env_store=store).returncode == 1

    # --json works in BOTH positions (the house CLI convention)
    before = _memory("--json", "list", env_store=store)
    after = _memory("list", "--json", env_store=store)
    assert [r["text"] for r in json.loads(before.stdout)] == \
           [r["text"] for r in json.loads(after.stdout)]

    # destructive command refuses without --yes, then works
    assert _memory("forget", "--id", "1", env_store=store).returncode == 2
    assert _memory("forget", "--id", "1", "--yes", env_store=store).returncode == 0
    stats = json.loads(_memory("stats", "--json", env_store=store).stdout)
    assert stats["notes"] == 1

    # one fact per line, so a corrupt line cannot take the file down
    store.write_text(store.read_text() + "{not json}\n", encoding="utf-8")
    assert _memory("stats", "--json", env_store=store).returncode == 0


def test_install_restores_the_execute_bit_on_scripts(tmp_path):
    """Wheels drop permission bits; the skill docs promise `./memory.py`."""
    source = tmp_path / "bundle"
    (source / "demo").mkdir(parents=True)
    (source / si.INDEX_NAME).write_text("# Skills Index\n", encoding="utf-8")
    script = source / "demo" / "demo.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    script.chmod(0o644)                       # what a wheel gives you
    (source / "demo" / "demo_skill.md").write_text("# Demo\n", encoding="utf-8")

    target = tmp_path / "skills"
    si.install({si.SKILLS_DIR_KEY: str(target)}, source=source)

    assert (target / "demo" / "demo.py").stat().st_mode & 0o111
