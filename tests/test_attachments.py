import json
from pathlib import Path

from PIL import Image

from pengy.core.attachments import (
    attachment_label, derivative_path, import_image, message_to_api, object_path,
    scan_references, storage_report,
)
from pengy.core.config import set_config_dir
FIXTURES = Path(__file__).parent / "fixtures" / "attachments"


def test_image_import_is_content_addressed_and_request_only(tmp_path):
    set_config_dir(str(tmp_path / "config"))
    source = tmp_path / "screen.png"
    Image.new("RGBA", (32, 24), (20, 30, 40, 128)).save(source)
    ref = import_image(source, "Screen shot.png")
    assert ref["id"].startswith("sha256:")
    assert ref["kind"] == "image"
    assert object_path(ref["id"]).is_file()
    assert derivative_path(ref["id"], "thumbnail-256-v1.jpg").is_file()
    persisted = {"role": "user", "content": "What is this?", "attachments": [ref]}
    serialized = json.dumps(persisted)
    assert "data:image/" not in serialized
    api = message_to_api(persisted)
    assert api["content"][0]["type"] == "image_url"
    assert api["content"][-1] == {"type": "text", "text": "What is this?"}
    assert "attachments" not in api
    assert "32×24" in attachment_label(ref)


def test_excluded_attachment_metadata_is_removed_from_provider_message():

    message = {
        "role": "user",
        "content": "older image",
        "attachments": [{"kind": "image", "id": "sha256:" + "a" * 64}],
    }
    assert message_to_api(message, include_attachments=False) == {
        "role": "user", "content": "older image"
    }


def test_shared_fixtures_preserve_legacy_unknown_and_missing_states(tmp_path):
    legacy = json.loads((FIXTURES / "legacy-placeholder-chat.json").read_text())
    assert legacy["messages"][0].get("attachments") is None
    unknown = json.loads((FIXTURES / "unknown-kind-v1-chat.json").read_text())
    message = unknown["messages"][0]
    round_trip = json.loads(json.dumps(unknown))
    assert round_trip == unknown
    assert message["attachments"][0]["future"]["preserve"] is True
    assert message["future_message_field"]["keep"] is True
    missing = json.loads((FIXTURES / "missing-object-v1-chat.json").read_text())
    set_config_dir(str(tmp_path / "config"))
    ref = missing["messages"][0]["attachments"][0]
    assert not object_path(ref["id"]).exists()
    assert message_to_api(missing["messages"][0])["content"] == [{"type": "text", "text": "Missing"}]


def test_invalid_and_corrupt_objects_never_resolve(tmp_path):
    set_config_dir(str(tmp_path / "config"))
    bad = {"role": "user", "content": "look", "attachments": [{"kind": "image", "id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "bad.png"}]}
    assert message_to_api(bad)["content"] == [{"type": "text", "text": "look"}]
    source = tmp_path / "corrupt"
    source.write_bytes(b"not an image")
    corrupt_ref = {"id": "sha256:" + "c" * 64, "kind": "image"}
    object_path(corrupt_ref["id"]).parent.mkdir(parents=True, exist_ok=True)
    object_path(corrupt_ref["id"]).write_bytes(source.read_bytes())
    assert message_to_api({"role": "user", "content": "x", "attachments": [corrupt_ref]})["content"] == [{"type": "text", "text": "x"}]


def test_storage_report_is_dry_run_and_tracks_references(tmp_path):
    set_config_dir(str(tmp_path / "config"))
    source = tmp_path / "one.png"
    Image.new("RGB", (2, 2), "red").save(source)
    ref = import_image(source, "one.png")
    chat = {"messages": [{"role": "user", "content": "x", "attachments": [ref]}]}
    report = storage_report([chat])
    assert report["objects"] == 1
    assert report["referenced"] == 1
    assert report["reclaimable_objects"] == 0
    assert report["delete_performed"] is False
    assert scan_references([chat]) == {ref["id"]}
