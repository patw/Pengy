"""Regression tests for tool sharp edges found by the cross-edition audit.

Each test pins one behaviour a model would *assume* without being told: a
preview never writes, a failed write never damages the file, an edit keeps
line endings / permissions / symlinks, a search covers every text file, and a
malformed or misplaced argument is reported instead of silently defaulted.
Mirror these in the Rust and C++ editions.
"""

import http.server
import json
import os
import stat
import threading

import pytest

from pengy.core import tools
from pengy.core.tool_args import normalize_arguments, parse_raw_arguments
from pengy.core.tools import TOOLS, execute_tool

PARAMS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}


def _replace(path, old, new):
    return {"path": str(path), "operations": [{"kind": "replace", "old": old, "new": new}]}


# ── argument normalization ────────────────────────────────────────────────────

class TestNormalizeArguments:
    def test_unambiguous_slips_are_coerced(self):
        args, err = normalize_arguments("read_file", {"path": "f", "offset": "3", "limit": 2.0},
                                        PARAMS["read_file"])
        assert err is None
        assert args == {"path": "f", "offset": 3, "limit": 2}

    def test_string_booleans(self):
        for raw, want in (("true", True), ("False", False), (1, True), (0, False)):
            args, err = normalize_arguments("apply_changes", {
                "changes": [_replace("f", "a", "b")], "dry_run": raw}, PARAMS["apply_changes"])
            assert err is None and args["dry_run"] is want

    def test_single_value_and_json_string_become_arrays(self):
        args, err = normalize_arguments("read_multiple_files", {"paths": "ab"},
                                        PARAMS["read_multiple_files"])
        assert err is None and args["paths"] == ["ab"]
        args, err = normalize_arguments("read_multiple_files", {"paths": '["a", "b"]'},
                                        PARAMS["read_multiple_files"])
        assert err is None and args["paths"] == ["a", "b"]

    def test_null_is_absent(self):
        args, err = normalize_arguments("read_file", {"path": "f", "offset": None},
                                        PARAMS["read_file"])
        assert err is None and args == {"path": "f"}

    def test_enum_case_is_folded(self):
        args, err = normalize_arguments("todowrite", {"todos": [
            {"content": "x", "status": "In_Progress"}]}, PARAMS["todowrite"])
        assert err is None and args["todos"][0]["status"] == "in_progress"

    def test_unknown_key_is_rejected_with_valid_keys(self):
        _, err = normalize_arguments("write_file", {"path": "f", "content": "x", "append": True},
                                     PARAMS["write_file"])
        assert "append: unknown key (valid keys: content, path)" in err
        assert "nothing was run" in err

    def test_misplaced_top_level_key_is_named(self):
        _, err = normalize_arguments("apply_changes", {
            "changes": [dict(_replace("f", "a", "b"), dry_run=True)]}, PARAMS["apply_changes"])
        assert "changes[0].dry_run: not allowed here — dry_run is a top-level argument" in err

    def test_missing_required_key(self):
        _, err = normalize_arguments("replace_in_file", {"path": "f", "old_str": "a"},
                                     PARAMS["replace_in_file"])
        assert "new_str: missing required key" in err

    def test_uncoercible_value(self):
        _, err = normalize_arguments("read_file", {"path": "f", "offset": "three"},
                                     PARAMS["read_file"])
        assert 'offset: expected an integer, got string "three"' in err

    def test_raw_argument_parsing(self):
        assert parse_raw_arguments("") == {}
        assert parse_raw_arguments('{"a": 1}') == {"a": 1}
        assert parse_raw_arguments(json.dumps('{"a": 1}')) == {"a": 1}
        assert parse_raw_arguments("{oops") == {"_raw_arguments": "{oops"}
        _, err = normalize_arguments("read_file", {"_raw_arguments": "{oops"}, PARAMS["read_file"])
        assert "were not valid JSON" in err


# ── apply_changes / dry_run ───────────────────────────────────────────────────

class TestDryRunNeverWrites:
    def test_per_file_dry_run_is_rejected_and_file_untouched(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("alpha\nbeta\n")
        result = execute_tool("apply_changes", {
            "changes": [dict(_replace(f, "beta", "BETA"), dry_run=True)]})
        assert "dry_run is a top-level argument" in result
        assert f.read_text() == "alpha\nbeta\n"

    def test_string_true_is_a_dry_run(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("alpha\nbeta\n")
        result = execute_tool("apply_changes", {
            "changes": [_replace(f, "beta", "BETA")], "dry_run": "true"})
        assert result.startswith("Dry run: no changes applied.")
        assert "-beta" in result and "+BETA" in result
        assert f.read_text() == "alpha\nbeta\n"


# ── safe writes ───────────────────────────────────────────────────────────────

def _fail_fsync(monkeypatch):
    def boom(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(tools.os, "fsync", boom)


@pytest.mark.parametrize("tool,make_args", [
    ("write_file", lambda f: {"path": str(f), "content": "NEW"}),
    ("replace_in_file", lambda f: {"path": str(f), "old_str": "ORIGINAL", "new_str": "NEW"}),
    ("apply_changes", lambda f: {"changes": [_replace(f, "ORIGINAL", "NEW")]}),
])
def test_failed_write_leaves_original_intact(tmp_path, monkeypatch, tool, make_args):
    f = tmp_path / "target.txt"
    f.write_text("ORIGINAL content\n")
    _fail_fsync(monkeypatch)
    result = execute_tool(tool, make_args(f))
    assert "No space left" in result
    assert f.read_text() == "ORIGINAL content\n"
    assert os.listdir(tmp_path) == ["target.txt"]  # no stray temp file


def test_failed_rename_leaves_original_intact(tmp_path, monkeypatch):
    f = tmp_path / "target.txt"
    f.write_text("ORIGINAL\n")

    def boom(src, dst):
        raise OSError(18, "Invalid cross-device link")
    monkeypatch.setattr(tools.os, "replace", boom)
    result = execute_tool("replace_in_file", {"path": str(f), "old_str": "ORIGINAL", "new_str": "NEW"})
    assert "Error" in result
    assert f.read_text() == "ORIGINAL\n"
    assert os.listdir(tmp_path) == ["target.txt"]


@pytest.mark.parametrize("tool,make_args", [
    ("write_file", lambda f: {"path": str(f), "content": "echo NEW\n"}),
    ("replace_in_file", lambda f: {"path": str(f), "old_str": "hi", "new_str": "HI"}),
    ("apply_changes", lambda f: {"changes": [_replace(f, "hi", "HI")]}),
])
def test_edit_keeps_permissions(tmp_path, tool, make_args):
    f = tmp_path / "run.sh"
    f.write_text("echo hi\n")
    f.chmod(0o750)
    execute_tool(tool, make_args(f))
    assert stat.S_IMODE(f.stat().st_mode) == 0o750


@pytest.mark.parametrize("tool,make_args", [
    ("write_file", lambda f: {"path": str(f), "content": "NEW\n"}),
    ("replace_in_file", lambda f: {"path": str(f), "old_str": "OLD", "new_str": "NEW"}),
    ("apply_changes", lambda f: {"changes": [_replace(f, "OLD", "NEW")]}),
])
def test_edit_through_symlink_keeps_link(tmp_path, tool, make_args):
    real = tmp_path / "real.txt"
    real.write_text("OLD\n")
    link = tmp_path / "link.txt"
    link.symlink_to("real.txt")
    execute_tool(tool, make_args(link))
    assert link.is_symlink()
    assert real.read_text() == "NEW\n"


def test_symlink_and_target_are_one_file_for_apply_changes(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("a\nb\n")
    (tmp_path / "link.txt").symlink_to("real.txt")
    result = execute_tool("apply_changes", {"changes": [
        _replace(real, "a", "A"), _replace(tmp_path / "link.txt", "b", "B")]})
    assert "duplicate path" in result
    assert real.read_text() == "a\nb\n"


@pytest.mark.parametrize("tool,make_args", [
    ("replace_in_file", lambda f: {"path": str(f), "old_str": "one\ntwo", "new_str": "ONE\nTWO"}),
    ("apply_changes", lambda f: {"changes": [_replace(f, "one\ntwo", "ONE\nTWO")]}),
])
def test_edit_keeps_crlf(tmp_path, tool, make_args):
    f = tmp_path / "win.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = execute_tool(tool, make_args(f))
    assert "Error" not in result
    assert f.read_bytes() == b"ONE\r\nTWO\r\nthree\r\n"


# ── download_file never destroys an existing file ─────────────────────────────

@pytest.fixture
def http_base():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/big":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * (2 * 1024 * 1024))
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.parametrize("url_path,extra", [("/missing", {}), ("/big", {"max_size_mb": 1})])
def test_failed_download_keeps_existing_file(tmp_path, http_base, url_path, extra):
    existing = tmp_path / "keep.bin"
    existing.write_text("PRECIOUS")
    result = execute_tool("download_file", dict(
        url=http_base + url_path, dir=str(tmp_path), filename="keep.bin", **extra))
    assert result.startswith("Error")
    assert existing.read_text() == "PRECIOUS"
    assert os.listdir(tmp_path) == ["keep.bin"]


# ── search_content covers every text file ─────────────────────────────────────

class TestSearchCoverage:
    def test_text_files_found_regardless_of_extension(self, tmp_path):
        for name in ("Program.cs", "app.vue", "main.tf", "deploy", "server.log", "data.csv"):
            (tmp_path / name).write_text("needle\n")
        (tmp_path / "blob.dat").write_bytes(b"needle\x00\x01")
        result = execute_tool("search_content", {"pattern": "needle", "path": str(tmp_path)})
        for name in ("Program.cs", "app.vue", "main.tf", "deploy", "server.log", "data.csv"):
            assert name in result
        assert "blob.dat" not in result

    def test_no_match_reports_skipped_binaries(self, tmp_path):
        (tmp_path / "a.txt").write_text("hay\n")
        (tmp_path / "b.dat").write_bytes(b"\x00needle")
        result = execute_tool("search_content", {"pattern": "needle", "path": str(tmp_path)})
        assert "(searched 1 files; skipped 1 binary)" in result

    def test_file_glob_with_directory(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("needle\n")
        (tmp_path / "b.py").write_text("needle\n")
        result = execute_tool("search_content", {
            "pattern": "needle", "path": str(tmp_path), "file_glob": "src/*.py"})
        assert "src/a.py" in result and "b.py:" not in result.replace("src/a.py", "")

    def test_file_glob_matching_nothing_is_reported(self, tmp_path):
        (tmp_path / "a.txt").write_text("needle\n")
        result = execute_tool("search_content", {
            "pattern": "needle", "path": str(tmp_path), "file_glob": "*.rs"})
        assert "No file matched file_glob '*.rs'" in result

    def test_hidden_dirs_searched_noise_dirs_skipped(self, tmp_path):
        for rel in (".github/workflows/ci.yml", ".env", "app/auth.py",
                    ".git/config", "node_modules/x/index.js", ".venv/lib/site.py"):
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text("needle\n")
        result = execute_tool("search_content", {"pattern": "needle", "path": str(tmp_path)})
        for rel in (".github/workflows/ci.yml", ".env", "app/auth.py"):
            assert rel in result
        for rel in (".git/", "node_modules", ".venv"):
            assert rel not in result

    def test_hidden_or_noise_named_root_is_searched(self, tmp_path):
        for name in (".config", "build", "target"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "f.txt").write_text("needle\n")
            result = execute_tool("search_content", {"pattern": "needle", "path": str(tmp_path / name)})
            assert "Found 1 match" in result, name


def test_glob_skips_hidden_dirs_unless_named(tmp_path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.txt").write_text("x")
    (tmp_path / "y.txt").write_text("y")
    assert ".hidden" not in execute_tool("glob", {"pattern": "**/*.txt", "path": str(tmp_path)})
    assert "x.txt" in execute_tool("glob", {"pattern": ".hidden/*.txt", "path": str(tmp_path)})


@pytest.mark.parametrize("tool,make_args", [
    ("write_file", lambda f: {"path": str(f), "content": "NEW"}),
    ("replace_in_file", lambda f: {"path": str(f), "old_str": "ORIGINAL", "new_str": "NEW"}),
    ("apply_changes", lambda f: {"changes": [_replace(f, "ORIGINAL", "NEW")]}),
])
def test_read_only_file_is_not_replaced(tmp_path, tool, make_args):
    f = tmp_path / "locked.txt"
    f.write_text("ORIGINAL\n")
    f.chmod(0o444)
    result = execute_tool(tool, make_args(f))
    assert "Permission denied" in result
    assert f.read_text() == "ORIGINAL\n"
    assert os.listdir(tmp_path) == ["locked.txt"]


def test_replace_all_and_hint(tmp_path):
    f = tmp_path / "h.py"
    f.write_text("client_v1.a()\nclient_v1.b()\n")
    result = execute_tool("replace_in_file", {"path": str(f), "old_str": "client_v1", "new_str": "client_v2"})
    assert "set replace_all=true" in result
    result = execute_tool("replace_in_file", {"path": str(f), "old_str": "client_v1",
                                              "new_str": "client_v2", "replace_all": True})
    assert result == f"✅ Replaced 2 occurrence(s) in {f}."
    assert f.read_text() == "client_v2.a()\nclient_v2.b()\n"


def test_search_ignore_case(tmp_path):
    (tmp_path / "a.txt").write_text("Needle\n")
    assert "No matches" in execute_tool("search_content", {"pattern": "needle", "path": str(tmp_path)})
    assert "Found 1 match" in execute_tool("search_content", {"pattern": "needle", "path": str(tmp_path),
                                                              "ignore_case": True})
