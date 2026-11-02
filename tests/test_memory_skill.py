"""The bundled memory skill: does a new user's memory actually work?

These drive ``pengy/assets/skills/memory/memory.py`` as a subprocess, because
that is how an agent drives it. The interesting cases are the ones that made the
first shipped version produce nothing: a question phrased in the user's own
words, a note corrected later, and two writes landing at the same moment.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from pengy.core import skills_install as si


def run(store, *args, env_extra=None):
    script = si.default_skills_source() / "memory" / "memory.py"
    env = dict(os.environ, PENGY_MEMORY_FILE=str(store), **(env_extra or {}))
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, text=True, env=env, check=False)


def add(store, text, *extra):
    result = run(store, "add", "--text", text, *extra)
    assert result.returncode == 0, result.stderr
    return result


def texts(store, *args):
    out = json.loads(run(store, "--json", *args).stdout)
    return [record["text"] for record in out]


def test_a_question_in_the_users_own_words_finds_the_note(tmp_path):
    """The whole point: the agent should not need to remember the exact wording."""
    store = tmp_path / "memory.jsonl"
    add(store, "HTTPS uses truststore because certifi kept a revoked root",
        "--subject", "pengy", "--tags", "tls")
    add(store, "The printer bed needs glue stick on PEI at 60C",
        "--subject", "workshop", "--tags", "3dprint")

    found = run(store, "search", "--q", "why does https keep failing on a revoked ca")
    assert found.returncode == 0, found.stderr
    assert "truststore" in found.stdout
    assert "printer" not in found.stdout


def test_plural_and_tense_do_not_have_to_match(tmp_path):
    store = tmp_path / "memory.jsonl"
    add(store, "The fleet SSH key must be refreshed after a reflash")

    for query in ("ssh keys", "reflashed machine", "refresh the key"):
        assert run(store, "search", "--q", query).returncode == 0, query


def test_more_matching_words_outrank_fewer(tmp_path):
    store = tmp_path / "memory.jsonl"
    add(store, "The deploy script takes about eleven minutes",
        "--subject", "garden")
    add(store, "Deploy is git pull plus a service restart; there is no CI",
        "--subject", "garden")

    out = json.loads(run(store, "--json", "search", "--q",
                         "deploy restart service", "--limit", "2").stdout)
    assert "git pull plus a service restart" in out[0]["text"]
    assert out[0]["score"] > out[1]["score"]


def test_superseded_note_is_hidden_from_search_but_kept(tmp_path):
    store = tmp_path / "memory.jsonl"
    add(store, "UI tests are broken under xvfb; use a real display")
    add(store, "UI tests need QT_QPA_PLATFORM=offscreen", "--supersedes", "1")

    live = run(store, "search", "--q", "how do the gui tests run")
    assert "xvfb" not in live.stdout          # the retired advice is not offered

    every = run(store, "search", "--q", "how do the gui tests run", "--all")
    assert "xvfb" in every.stdout             # but it is still there to audit
    assert "superseded by #2" in every.stdout

    stats = json.loads(run(store, "stats", "--json").stdout)
    assert stats["notes"] == 2 and stats["superseded"] == 1


def test_superseding_does_not_lose_the_new_note(tmp_path):
    """The rewrite after the append must see the note it is linking from."""
    store = tmp_path / "memory.jsonl"
    add(store, "the old way")
    add(store, "the new way", "--supersedes", "1")
    assert len(texts(store, "list", "--all")) == 2


def test_an_identical_note_is_not_stored_twice(tmp_path):
    store = tmp_path / "memory.jsonl"
    add(store, "Pat prefers terse answers")

    again = run(store, "add", "--text", "pat prefers   Terse answers")
    assert again.returncode == 0
    assert "Already stored as #1" in again.stdout
    assert len(texts(store, "list")) == 1

    forced = json.loads(run(store, "--json", "add", "--text",
                            "Pat prefers terse answers", "--force").stdout)
    assert forced["stored"]["id"] == 2


def test_parallel_writers_get_unique_ids(tmp_path):
    """Agents issue tool calls in parallel; the ids must not collide."""
    store = tmp_path / "memory.jsonl"
    script = si.default_skills_source() / "memory" / "memory.py"
    env = dict(os.environ, PENGY_MEMORY_FILE=str(store))
    procs = [
        subprocess.Popen([sys.executable, str(script), "add", "--text", f"writer {i}"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for i in range(10)
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0

    records = [json.loads(line) for line in store.read_text().splitlines() if line.strip()]
    ids = [record["id"] for record in records]
    assert len(records) == 10
    assert len(set(ids)) == 10


def test_forget_needs_yes_deletes_one_note_and_leaves_a_backup(tmp_path):
    store = tmp_path / "memory.jsonl"
    add(store, "keep me")
    add(store, "delete me")

    assert run(store, "forget", "--id", "2").returncode == 2
    assert len(texts(store, "list")) == 2

    assert run(store, "forget", "--id", "2", "--yes").returncode == 0
    assert texts(store, "list") == ["keep me"]
    assert (tmp_path / "memory.jsonl.bak").is_file()


def test_forget_does_not_take_out_a_note_that_merely_shares_an_id(tmp_path):
    """A store written by an older copy can hold two notes with one id."""
    store = tmp_path / "memory.jsonl"
    store.write_text(
        json.dumps({"id": 7, "ts": "2026-11-01T00:00:00+00:00",
                    "text": "the DNS token lives in ~/.secrets", "subject": "a", "tags": []}) + "\n"
        + json.dumps({"id": 7, "ts": "2026-11-02T00:00:00+00:00",
                      "text": "the printer needs glue stick", "subject": "b", "tags": []}) + "\n",
        encoding="utf-8")

    result = run(store, "forget", "--id", "7", "--yes")
    assert result.returncode == 0
    assert "appeared 2 times" in result.stderr
    assert texts(store, "list", "--all") == ["the DNS token lives in ~/.secrets"]


def test_a_corrupt_line_survives_a_rewrite(tmp_path):
    store = tmp_path / "memory.jsonl"
    add(store, "keep me")
    add(store, "delete me")
    with store.open("a", encoding="utf-8") as handle:
        handle.write("{this line is a hand-edit that does not parse}\n")

    assert run(store, "forget", "--id", "2", "--yes").returncode == 0
    assert "{this line is a hand-edit that does not parse}" in store.read_text()


def test_exit_codes_are_usable_in_a_shell_chain(tmp_path):
    store = tmp_path / "memory.jsonl"
    add(store, "the kaypro floppy drive needs a new belt")

    assert run(store, "search", "--q", "zeppelin airship").returncode == 1
    # a stopword-only query is a mistake, not an empty store
    assert run(store, "search", "--q", "how do the").returncode == 2


def test_json_works_in_both_positions(tmp_path):
    store = tmp_path / "memory.jsonl"
    add(store, "one fact worth keeping", "--subject", "s", "--tags", "a,b")

    before = run(store, "--json", "list")
    after = run(store, "list", "--json")
    assert json.loads(before.stdout) == json.loads(after.stdout)

    stats = json.loads(run(store, "stats", "--json").stdout)
    assert stats["notes"] == 1 and stats["subjects"] == {"s": 1}


def test_store_can_be_relocated(tmp_path):
    home_store = tmp_path / "elsewhere" / "notes.jsonl"
    result = run(tmp_path / "unused.jsonl", "add", "--text", "note",
                 "--store", str(home_store))
    assert result.returncode == 0
    assert home_store.is_file()
    assert not (tmp_path / "unused.jsonl").exists()
