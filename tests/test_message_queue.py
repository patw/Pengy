"""The composer's message queue: messages typed while a turn is running.

No Qt needed -- ``pengy.core.message_queue`` is deliberately plain Python, so these
run on every machine in the fleet (the GUI test files self-skip without PySide6).
"""

from pengy.core.message_queue import MessageQueue, QueuedMessage


def test_new_queue_is_empty():
    q = MessageQueue()
    assert q.is_empty()
    assert len(q) == 0
    assert q.previews() == []
    assert q.held is False
    # Taking from an empty queue must be harmless, not an IndexError.
    assert q.take_head().text == ""
    assert q.take_newest().text == ""
    assert q.take_oldest().text == ""


def test_order_and_the_three_ways_out():
    q = MessageQueue()
    for text in ("first", "second", "third"):
        q.enqueue(text)
    assert len(q) == 3
    assert q.items[0].text == "first"
    assert q.take_head().text == "first"      # FIFO: what a finished turn sends
    assert q.take_newest().text == "third"    # LIFO: what Up recalls
    assert q.take_oldest().text == "second"   # Down
    assert q.is_empty()


def test_blank_payloads_are_not_queue_entries():
    q = MessageQueue()
    q.enqueue("")
    q.enqueue("   \n ")
    assert q.is_empty()
    q.enqueue("", [object()])
    assert len(q) == 1
    assert q.items[0].text == ""
    assert len(q.items[0].images) == 1


def test_held_flag_and_clear():
    q = MessageQueue()
    q.enqueue("parked")
    q.held = True
    assert q.held is True
    q.held = False
    assert q.held is False
    q.clear()
    assert q.is_empty() and q.held is False


def test_previews():
    q = MessageQueue()
    q.enqueue("read the logs\nand then summarise them")
    q.enqueue("", ["shot.png"])
    q.enqueue("", ["a.png", "b.png"])
    previews = q.previews()
    assert previews[0] == "read the logs"          # first line only
    assert "summarise" not in previews[0]
    assert previews[1] == "[1 image]"
    assert previews[2] == "[2 images]"


def test_preview_elision_is_bounded_and_marked():
    q = MessageQueue()
    q.enqueue("x" * 200)
    one = q.previews(20)[0]
    assert len(one) == 20
    assert one.endswith("\u2026")
    assert len(q.previews(0)[0]) == 200            # max_chars 0 disables elision


def test_drained_queue_stops_advertising_previews():
    q = MessageQueue()
    q.enqueue("only")
    q.take_head()
    assert q.previews() == []


def test_items_is_a_copy():
    """The window renders items() while draining: mutating the returned list must
    not corrupt the queue."""
    q = MessageQueue()
    q.enqueue("keep me")
    items = q.items
    items.clear()
    assert len(q) == 1
    assert isinstance(items[0] if items else QueuedMessage(), QueuedMessage)
