"""Tests for chat search: the core scan, the web route + sidebar, the CLI
/search command, and the Qt sidebar's search box.

Run with:  python -m pytest tests/test_chat_search.py -v
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from pengy.core import chat_search
from pengy.core.chat_manager import create_chat, save_chat
from pengy.core.chat_search import make_snippet, match_chat, parse_query, search_chats
from pengy.core.config import get_config_dir, set_config_dir


@pytest.fixture
def tmp_cfg():
    with tempfile.TemporaryDirectory(prefix="pengy-search-") as cfg_dir:
        set_config_dir(cfg_dir)
        assert get_config_dir() != Path.home() / ".config" / "pengy"
        yield Path(cfg_dir)
    set_config_dir(None)


def make_chat(title, *messages):
    chat = create_chat()
    chat["title"] = title
    chat["messages"] = list(messages)
    save_chat(chat)
    time.sleep(0.002)  # distinct created_at, so newest-first order is defined
    return chat


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def results(query, **kw):
    return [e for e in search_chats(query, **kw) if e["type"] == "result"]


# ── core ────────────────────────────────────────────────────────────────────────

class TestParseQuery:
    @pytest.mark.parametrize("query, terms", [
        ("NGINX proxy", ["nginx", "proxy"]),
        ('  "Reverse   Proxy"  nginx ', ["reverse proxy", "nginx"]),
        ("a a A", ["a"]),
        ('""', []),
        ("", []),
    ])
    def test_terms(self, query, terms):
        assert parse_query(query) == terms


class TestMatchChat:
    def test_words_may_be_in_different_messages(self):
        chat = {"title": "t", "messages": [user("set up nginx"), assistant("use a proxy_pass")]}
        hits = match_chat(chat, ["nginx", "proxy"])
        assert [h["index"] for h in hits] == [0, 1]

    def test_every_word_must_appear(self):
        chat = {"title": "t", "messages": [user("set up nginx")]}
        assert match_chat(chat, ["nginx", "apache"]) is None

    def test_title_counts_toward_the_words(self):
        chat = {"title": "Nginx notes", "messages": [user("proxy_pass is the key")]}
        assert match_chat(chat, ["nginx", "proxy"]) is not None

    def test_tool_traffic_is_not_searched(self):
        chat = {"title": "t", "messages": [
            user("read my config"),
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "1", "function": {"name": "read_file",
                                                     "arguments": '{"path": "nginx.conf"}'}}]},
            {"role": "tool", "tool_call_id": "1", "content": "server { listen 80; nginx }"},
        ]}
        assert match_chat(chat, ["nginx"]) is None

    def test_list_content_is_searched(self):
        chat = {"title": "t", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "what is in this image"},
                                         {"type": "image_url", "image_url": {}}]}]}
        assert match_chat(chat, ["image"])

    def test_at_most_three_hits_preferring_messages_with_more_words(self):
        msgs = [user("nginx")] * 4 + [assistant("nginx proxy here")]
        hits = match_chat({"title": "t", "messages": msgs}, ["nginx", "proxy"])
        assert len(hits) == chat_search.MAX_HITS
        assert 4 in [h["index"] for h in hits]


class TestSnippet:
    def test_window_around_the_match_with_spans(self):
        text = "x" * 200 + " the NGINX config " + "y" * 200
        snippet, spans = make_snippet(text, ["nginx"])
        assert snippet.startswith("…") and snippet.endswith("…")
        start, end = spans[0]
        assert snippet[start:end] == "NGINX"

    def test_markdown_and_whitespace_are_flattened(self):
        snippet, spans = make_snippet("**Use** `nginx`\n\n```bash\nreload\n```", ["nginx"])
        assert snippet == "Use nginx reload"  # the fence language tag goes too
        assert snippet[spans[0][0]:spans[0][1]] == "nginx"


class TestSearchChats:
    def test_title_matches_first_then_newest_first(self, tmp_cfg):
        old = make_chat("Old", user("nginx setup"))
        titled = make_chat("Nginx notes", user("hello"))
        new = make_chat("New", assistant("more NGINX"))
        found = results("nginx")
        assert [r["id"] for r in found] == [titled["id"], new["id"], old["id"]]
        assert found[0]["title_match"] and found[0]["hits"] == []
        assert found[1]["hits"][0]["snippet"] == "more NGINX"

    def test_non_ascii_words_match_escaped_json(self, tmp_cfg):
        # The Python edition writes non-ASCII as \\uXXXX, so the raw-bytes
        # pre-check must not reject a word it can't see in the file.
        chat = make_chat("t", user("Un CAFÉ crème, s'il vous plaît"))
        assert "\\u00c9" in (tmp_cfg / "chats" / f"{chat['id']}.json").read_text()
        assert [r["id"] for r in results("café")] == [chat["id"]]

    def test_quotes_and_backslashes_in_words(self, tmp_cfg):
        chat = make_chat("t", user('run "make all" in C:\\src'))
        assert results('c:\\src') and results('"make all"')
        assert results('"make all"')[0]["id"] == chat["id"]

    def test_limit_marks_truncated(self, tmp_cfg):
        for i in range(5):
            make_chat(f"c{i}", user("needle"))
        events = list(search_chats("needle", limit=3))
        assert len([e for e in events if e["type"] == "result"]) == 3
        assert events[-1] == {"type": "done", "results": 3, "truncated": True, "cancelled": False}

    def test_cancel_stops_between_chats(self, tmp_cfg):
        for i in range(3):
            make_chat(f"c{i}", user("needle"))
        events = list(search_chats("needle", cancelled=lambda: True))
        assert events[-1]["cancelled"] and not [e for e in events if e["type"] == "result"]

    def test_progress_and_empty_query(self, tmp_cfg):
        make_chat("a", user("x"))
        make_chat("b", user("y"))
        events = list(search_chats("zzz"))
        assert [e["scanned"] for e in events if e["type"] == "progress"] == [1, 2]
        assert list(search_chats("   ")) == [{"type": "done", "results": 0,
                                               "truncated": False, "cancelled": False}]

    def test_empty_chats_are_not_read(self, tmp_cfg, monkeypatch):
        make_chat("New Chat")
        monkeypatch.setattr(chat_search, "_could_match", lambda *a: pytest.fail("read"))
        assert results("anything") == []


# ── web ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_cfg):
    from pengy.web.app import app
    return app.test_client()


class TestWebSearch:
    def test_route_returns_results(self, client):
        chat = make_chat("Nginx notes", user("proxy_pass"))
        other = make_chat("Other", assistant("reverse proxy via nginx"))
        data = client.get("/search?q=nginx proxy").get_json()
        assert [r["id"] for r in data["results"]] == [other["id"], chat["id"]]
        assert data["truncated"] is False

    def test_messages_carry_their_chat_index(self, client):
        chat = make_chat("t", user("read it"),
                         {"role": "assistant", "content": "Reading.",
                          "tool_calls": [{"id": "c1", "type": "function",
                                          "function": {"name": "glob", "arguments": "{}"}}]},
                         # no tool result: the page repairs it with a placeholder
                         assistant("the nginx answer"))
        html = client.get(f"/chat/{chat['id']}").get_data(as_text=True)
        assert 'id="msg-0"' in html and 'id="msg-1"' in html
        # Index 2 still points at "the nginx answer", not at the placeholder.
        assert html.index('id="msg-2"') < html.index("the nginx answer")
        assert html.index('id="msg-1"') < html.index("Reading.") < html.index('id="msg-2"')

    def test_sidebar_has_search_box(self, client):
        chat = make_chat("t", user("hi"))
        html = client.get(f"/chat/{chat['id']}").get_data(as_text=True)
        assert 'id="chatSearch"' in html and 'id="searchResults"' in html
        assert 'id="chatList"' in html


# ── CLI ─────────────────────────────────────────────────────────────────────────

class TestCliSearch:
    def test_numbers_match_list_and_load(self, tmp_cfg, capsys):
        from pengy.cli.main import PengyCLI

        target = make_chat("Nginx notes", user("proxy_pass"))
        make_chat("Newest", user("unrelated"))
        cli = PengyCLI(no_save=True)
        cli._handle_slash("/search nginx")
        out = capsys.readouterr().out
        assert "Nginx notes" in out and "1 chat found" in out
        # "Newest" is #1 in /list, so the hit is #2 -- and /load 2 opens it.
        assert "   2  Nginx notes" in out
        cli._handle_slash("/load 2")
        assert cli.current_chat["id"] == target["id"]

    def test_snippets_number_messages_and_load_prints_one(self, tmp_cfg, capsys):
        from pengy.cli.main import PengyCLI

        make_chat("t", user("hello"), assistant("hi"), user("how do I set up nginx"),
                  assistant("Install **nginx** and edit the config."))
        cli = PengyCLI(no_save=True)
        cli._handle_slash("/search nginx")
        out = capsys.readouterr().out
        assert "#3    You:" in out and "#4    AI:" in out
        cli._handle_slash("/load 1 #4")
        out = capsys.readouterr().out
        assert "#4 Assistant" in out and "Install **nginx** and edit the config." in out
        assert "/show 1 to read on" in out and "Conversation:" not in out
        cli._handle_slash("/load 1 9")
        assert "No message #9; this chat has 4." in capsys.readouterr().out

    def test_usage_and_no_match(self, tmp_cfg, capsys):
        from pengy.cli.main import PengyCLI

        cli = PengyCLI(no_save=True)
        cli._handle_slash("/search")
        assert "Usage: /search" in capsys.readouterr().out
        cli._handle_slash("/search zzz")
        assert "No chats match" in capsys.readouterr().out


# ── Qt ──────────────────────────────────────────────────────────────────────────

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6", reason="PySide6 not installed")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def sidebar(tmp_cfg, qapp):
    from pengy.core.chat_manager import load_index
    from pengy.ui.chat_history import ChatHistoryWidget

    make_chat("Nginx notes", user("proxy_pass"))
    make_chat("Grocery list", assistant("milk and an nginx joke"))
    make_chat("Plan a trip", user("Banff"))
    widget = ChatHistoryWidget()
    widget.load_chats(load_index())
    yield widget
    widget._cancel_search()
    widget.deleteLater()
    qapp.processEvents()


def _visible_titles(widget):
    return [widget._item_title(widget.chat_list.item(i))
            for i in range(widget.chat_list.count()) if not widget.chat_list.isRowHidden(i)]


def _wait_done(qapp, widget):
    deadline = time.time() + 5
    while "searching" in widget.search_status.text().lower() or not widget.search_status.text():
        qapp.processEvents()
        assert time.time() < deadline, widget.search_status.text()
        time.sleep(0.01)


class TestQtSearch:
    def test_typing_filters_titles_without_reading_files(self, sidebar, monkeypatch):
        monkeypatch.setattr(chat_search, "_could_match", lambda *a: pytest.fail("read"))
        sidebar.search_input.setText("NGINX")
        assert _visible_titles(sidebar) == ["Nginx notes"]
        sidebar.search_input.clear()
        assert len(_visible_titles(sidebar)) == 3

    def test_enter_shows_message_results_and_click_opens_chat(self, sidebar, qapp):
        sidebar.search_input.setText("nginx")
        sidebar.run_search()
        assert sidebar.list_stack.currentIndex() == 1
        _wait_done(qapp, sidebar)
        ids = [sidebar.results_list.item(i).data(0x0100)
               for i in range(sidebar.results_list.count())]
        assert len(ids) == 2
        assert "2 chats found" in sidebar.search_status.text()

        opened = []
        sidebar.search_hit_selected.connect(lambda *args: opened.append(args))
        sidebar._on_result_clicked(sidebar.results_list.item(1))
        assert opened == [(ids[1], [0], ["nginx"])]
        # The title match ("Nginx notes") comes first and has no hits to jump to.
        sidebar._on_result_clicked(sidebar.results_list.item(0))
        assert opened[-1] == (ids[0], [], ["nginx"])

    def test_editing_or_escape_returns_to_the_list(self, sidebar, qapp):
        sidebar.search_input.setText("nginx")
        sidebar.run_search()
        _wait_done(qapp, sidebar)
        sidebar.search_input.setText("nginx p")
        assert sidebar.list_stack.currentIndex() == 0
        assert _visible_titles(sidebar) == []
        sidebar.search_input.clear()
        assert sidebar.list_stack.currentIndex() == 0

    def test_stale_search_events_are_dropped(self, sidebar):
        sidebar.search_input.setText("nginx")
        sidebar.run_search()
        stale = sidebar._search_generation - 1
        sidebar._on_search_event(stale, {"type": "result", "id": "x", "title": "Stale", "hits": []})
        assert all(sidebar.results_list.item(i).data(0x0100) != "x"
                   for i in range(sidebar.results_list.count()))


class TestQtJumpToHit:
    def test_mark_terms_skips_tags_and_matches_entities(self, qapp):
        from pengy.ui.chat_view import mark_terms
        out = mark_terms('<a href="nginx.org">NGINX</a> &amp; nginx', ["nginx", "&"])
        assert 'href="nginx.org"' in out
        assert out.count('class="search-hit"') == 3
        assert '<span class="search-hit">NGINX</span>' in out
        assert '<span class="search-hit">&amp;</span>' in out
        assert mark_terms('say &quot;hi&quot;', ['"hi"']).count("search-hit") == 1

    def test_reveal_marks_the_hit_messages_and_scrolls_to_the_first(self, qapp):
        from pengy.ui.chat_view import ChatView
        view = ChatView()
        view.resize(500, 300)
        filler = "\n\n".join(f"paragraph {i}" for i in range(60))
        view.append_message("user", "first question", render=False, source=0)
        view.append_message("assistant", filler, render=False, source=1)
        view.append_message("tool_request", {"tool_call_id": "t", "name": "glob", "args": {}},
                            render=False)
        view.append_message("assistant", "the NGINX answer", render=False, source=3)
        view.append_message("user", "thanks " + filler, render=False, source=5)
        view.render_now()
        view.show()

        assert view.reveal_hits([3], ["nginx"])
        assert set(view._hit_marks) == {3}
        html_out = "".join(view._html_cache)
        assert html_out.count('class="search-hit"') == 1
        assert '<a name="pengy-hit"></a>' in view._html_cache[3]
        deadline = time.time() + 2
        while view.verticalScrollBar().value() in (0, view.verticalScrollBar().maximum()):
            qapp.processEvents()
            assert time.time() < deadline, "did not scroll to the hit"
        assert not view._auto_scroll

        # A message index no longer in the view (redacted) is reported.
        assert not view.reveal_hits([42], ["nginx"])
        assert view._hit_marks == {}
        view.deleteLater()

    def test_sending_a_message_clears_marks_and_follows_again(self, qapp):
        from pengy.ui.chat_view import ChatView
        view = ChatView()
        view.append_message("assistant", "nginx", render=False, source=0)
        view.render_now()
        view.reveal_hits([0], ["nginx"])
        view.append_message("user", "next", source=1)
        assert view._hit_marks == {} and view._auto_scroll
        assert "search-hit" not in "".join(view._html_cache)
        view.deleteLater()
