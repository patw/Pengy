"""Precise image rejection recovery, against actual SDK HTTP error handling."""
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from openai import BadRequestError
from PIL import Image

from pengy.core import tools
from pengy.core.attachments import import_image, resolve_history
from pengy.core.config import set_config_dir
from pengy.core.llm_client import LLMClient, _is_image_input_error
from tests.test_context_recovery import overflow
from tests.test_llm_loop import collect_auto, completion, tool_call


LEGACY = "Only text content parts are supported by this upstream format"
PROXY = {"error": {"source": "openai-proxy", "code": "unsupported_content_type",
                    "content_type": "image_url", "message": "Cannot translate pictures"}}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        status, body = self.server.replies.pop(0) if self.server.replies else (500, {"error": "exhausted"})
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


@pytest.fixture
def wire():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.requests, server.replies = [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = LLMClient(f'http://127.0.0.1:{server.server_port}/v1', 'test-key', 'stub-model')
    yield server, client
    client.client.close()
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize('message,expected', [
    (LEGACY, True), ('This model does not support image inputs', True),
    ('Image inputs are not supported by this endpoint', True),
    ('Unsupported parameter: temperature', False), ('Invalid image URL', False),
    ('Unsupported image format', False), ('Unsupported image detail', False),
    ('context length exceeded', False)])
def test_image_error_classification(message, expected):
    assert _is_image_input_error(overflow(message)) is expected


@pytest.mark.parametrize('wrapped', [True, False])
@pytest.mark.parametrize('code,kind,expected', [
    ('unsupported_content_type', 'image_url', True),
    ('unsupported_content_type', 'input_audio', False),
    ('invalid_chat_request', 'image_url', False),
    ('unsupported_content_role', 'image_url', False),
    ('unsupported_image_detail', 'image_url', False)])
def test_structured_error_overrides_keywords(wrapped, code, kind, expected):
    exc = overflow('This model does not support image inputs')
    detail = {'source': 'openai-proxy', 'code': code, 'content_type': kind,
              'message': 'This model does not support image inputs'}
    exc.body = {'error': detail} if wrapped else detail
    assert _is_image_input_error(exc) is expected
    exc.status_code = 500
    assert not _is_image_input_error(exc)


@pytest.mark.parametrize('error', [PROXY, {'error': LEGACY},
    {'error': {'message': 'This model does not support image inputs'}}])
def test_read_image_recovery_keeps_tool_reasoning_state_and_does_not_rerun(wire, tmp_path, error):
    server, client = wire
    image = tmp_path / 'shot.png'
    Image.new('RGB', (48, 32), 'blue').save(image)
    envelope = {'format': 'openai-proxy/reasoning-v1', 'proxy_model': 'stub-model',
                'model': 'upstream-stub', 'provider': 'openai_responses',
                'blocks': [{'type': 'reasoning', 'encrypted_content': 'opaque'}]}
    server.replies = [
        (200, completion(tool_calls=[tool_call('tc1', 'read_image', {'path': str(image)})],
                         reasoning_details=envelope)),
        (400, error), (200, completion(content='Image not inspected'))]
    history = [{'role': 'user', 'content': 'look'}]
    original = copy.deepcopy(history)
    ctx = tools.ToolContext()
    events = collect_auto(client.chat(history, tool_confirmation='all', tool_context=ctx))
    assert [e['type'] for e in events] == ['assistant_tool_calls', 'tool_request', 'tool_result', 'final_response']
    assert history == original
    assert len(server.requests) == 3
    before, after = [r['messages'] for r in server.requests[1:]]
    assert before[-1]['content'][1]['type'] == 'image_url'
    assert after[-2]['content'].startswith('Image loaded by read_image:')
    assert after[1]['reasoning_details'] == envelope
    assert after[2]['tool_call_id'] == 'tc1'
    notice = after[-1]['content']
    assert 'Do not claim to have inspected' in notice
    assert ('proxy adapter' in notice) == (error in [PROXY, {'error': LEGACY}])
    assert events[-1]['content'] == 'Image not inspected'


@pytest.mark.parametrize('error', [
    {'error': {'message': 'Invalid image URL'}},
    {'error': {'message': 'Unsupported image format'}},
    {'error': {'message': 'Unsupported parameter: temperature'}},
    {'error': {**PROXY['error'], 'code': 'invalid_chat_request'}},
    {'error': {**PROXY['error'], 'content_type': 'input_audio'}},
])
def test_unrelated_bad_request_with_image_is_not_retried(wire, error):
    server, client = wire
    server.replies = [(400, error)]
    message = {'role': 'user', 'content': [{'type': 'text', 'text': 'look'},
               {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aW1hZ2U='}}]}
    original = copy.deepcopy(message)
    with pytest.raises(BadRequestError):
        collect_auto(client.chat([message]))
    assert len(server.requests) == 1
    assert message == original


def test_attachment_recovery_is_request_only_and_bounded(wire, tmp_path):
    server, client = wire
    set_config_dir(str(tmp_path / 'config'))
    image = tmp_path / 'attached.png'
    Image.new('RGB', (32, 32), 'red').save(image)
    ref = import_image(image)
    history = [{'role': 'user', 'content': 'attachment', 'attachments': [ref]}]
    original = copy.deepcopy(history)
    outgoing = resolve_history(history)
    server.replies = [(400, PROXY), (400, PROXY)]
    with pytest.raises(BadRequestError, match='Cannot translate pictures'):
        collect_auto(client.chat(outgoing))
    assert len(server.requests) == 2
    assert 'attachments' not in server.requests[0]['messages'][0]
    assert server.requests[0]['messages'][0]['content'][0]['type'] == 'image_url'
    assert server.requests[1]['messages'][0]['content'] == 'attachment'
    assert history == original
    assert outgoing[0]['content'][0]['type'] == 'image_url'


def test_image_retry_retains_prior_context_reduction(wire):
    server, client = wire
    history = [{'role': 'user', 'content': [{'type': 'image_url',
        'image_url': {'url': 'data:image/png;base64,aW1hZ2U='}}]},
        {'role': 'assistant', 'content': '', 'tool_calls': [tool_call('tc1', 'read_file', {'path': 'x'})]},
        {'role': 'tool', 'tool_call_id': 'tc1', 'content': 'data' * 10000}]
    original = copy.deepcopy(history)
    server.replies = [(400, {'error': {'code': 'context_length_exceeded', 'message': 'context length exceeded'}}),
                      (400, PROXY), (200, completion(content='done'))]
    events = collect_auto(client.chat(history))
    assert events[0]['type'] == 'context_compacted'
    assert len(server.requests[2]['messages'][2]['content']) < 4000
    assert server.requests[2]['messages'][2] == server.requests[1]['messages'][2]
    assert history == original


def test_rejected_images_stay_omitted_in_later_tool_rounds(wire, tmp_path):
    server, client = wire
    note = tmp_path / 'note.txt'
    note.write_text('metadata')
    history = [{'role': 'user', 'content': [{'type': 'text', 'text': 'look'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aW1hZ2U='}}]}]
    original = copy.deepcopy(history)
    server.replies = [(400, PROXY),
        (200, completion(tool_calls=[tool_call('tc1', 'read_file', {'path': str(note)})])),
        (200, completion(content='done'))]
    events = collect_auto(client.chat(history, tool_confirmation='all'))
    assert events[-1]['content'] == 'done'
    assert len(server.requests) == 3
    for request in server.requests[1:]:
        assert request['messages'][0]['content'] == 'look'
        assert 'not evidence' in request['messages'][-1]['content']
    assert server.requests[-1]['messages'][-2]['tool_call_id'] == 'tc1'
    assert history == original
