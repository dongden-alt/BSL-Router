"""Native functionResponse image delivery, using independently generated pixels."""
import base64
import copy
import json
import struct
import zlib

from app.compat.adapters.gemini import gemini_request_to_openai
from app.codex_adapter import openai_to_responses
from app.models import ChatCompletionRequest
from app.scouts.vision import _extract_image_urls


def png_bytes(rgb=(13, 79, 151)):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0' + bytes(rgb))) + chunk(b'IEND', b''))


def media(rgb=(13, 79, 151)):
    return {'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(png_bytes(rgb)).decode()}}


def native(parts=None):
    return {'contents': [
        {'role': 'model', 'parts': [{'functionCall': {'name': 'read_file', 'args': {}, 'id': 'read-1'},
                                    'thoughtSignature': 'call-signature'}]},
        {'role': 'user', 'parts': [{'functionResponse': {'name': 'read_file', 'id': 'read-1',
            'response': {'result': 'Image read successfully'}, 'parts': [media()] if parts is None else parts},
            'thoughtSignature': 'result-signature'}]},
    ]}


def test_observed_function_response_parts_exact_shape():
    body = gemini_request_to_openai(native(), 'target')
    hits = _extract_image_urls(ChatCompletionRequest(**body))
    assert len(hits) == 1
    assert base64.b64decode(hits[0][2]['url'].split(',', 1)[1]) == png_bytes()
    assert [m['role'] for m in body['messages']] == ['assistant', 'tool', 'user']
    tool = body['messages'][1]
    assert tool['content'] == json.dumps('Image read successfully')
    assert tool['tool_call_id'] == 'read-1'
    assert tool['thought_signature'] == 'result-signature'
    assert body['messages'][0]['tool_calls'][0]['thought_signature'] == 'call-signature'
    assert 'read-1' in body['messages'][2]['content'][0]['text']
    assert media()['inlineData']['data'] not in tool['content']


def test_responses_preserves_user_image_content():
    image = media()['inlineData']
    body = openai_to_responses({'model': 'target', 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'pixels'},
        {'type': 'image_url', 'image_url': {'url': f"data:image/png;base64,{image['data']}"}},
    ]}]})
    assert body['input'][0]['content'] == [
        {'type': 'input_text', 'text': 'pixels'},
        {'type': 'input_image', 'image_url': f"data:image/png;base64,{image['data']}"},
    ]


def test_user_image_baseline_and_no_image_legacy_exact():
    expected = [
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'read-1', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': '{}'}, 'thought_signature': 'call-signature'}]},
        {'role': 'tool', 'tool_call_id': 'read-1', 'content': '"Image read successfully"',
         'thought_signature': 'result-signature'},
    ]
    assert gemini_request_to_openai(native([]), 'target')['messages'] == expected
    source = native([])
    source['contents'].append({'role': 'user', 'parts': [{'text': 'existing'}, media()]})
    body = gemini_request_to_openai(source, 'target')
    assert body['messages'][:2] == expected
    assert body['messages'][-1]['content'] == [
        {'type': 'text', 'text': 'existing'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + media()['inlineData']['data']}},
    ]


def test_parallel_split_results_explicit_ids_order_and_missing_result():
    source = native()
    calls = source['contents'][0]['parts']
    calls.append({'functionCall': {'name': 'read_file', 'args': {}, 'id': 'read-2'}})
    calls.append({'functionCall': {'name': 'other', 'args': {}, 'id': 'skipped'}})
    result = copy.deepcopy(source['contents'][1])
    result['parts'][0]['functionResponse']['id'] = 'read-2'
    result['parts'].append({'text': 'followup after second result'})
    source['contents'].insert(1, result)
    body = gemini_request_to_openai(source, 'target')
    assert [m['role'] for m in body['messages']] == ['assistant', 'tool', 'tool', 'tool', 'user', 'user', 'user']
    assert [m['tool_call_id'] for m in body['messages'][1:4]] == ['read-2', 'read-1', 'skipped']
    assert 'read-2' in body['messages'][4]['content'][0]['text']
    assert body['messages'][5]['content'] == 'followup after second result'
    assert 'read-1' in body['messages'][6]['content'][0]['text']


def test_duplicate_names_fifo_historical_replay_and_multiple_images():
    source = native([{'text': 'first'}, media(), {'text': 'between'}, media((92, 17, 3))])
    for content in source['contents']:
        for p in content['parts']:
            (p.get('functionCall') or p.get('functionResponse')).pop('id')
    source['contents'][0]['parts'].append(copy.deepcopy(source['contents'][0]['parts'][0]))
    source['contents'][1]['parts'].append(copy.deepcopy(source['contents'][1]['parts'][0]))
    source['contents'] *= 2
    body = gemini_request_to_openai(source, 'target')
    calls = [tc['id'] for m in body['messages'] for tc in m.get('tool_calls', [])]
    results = [m['tool_call_id'] for m in body['messages'] if m['role'] == 'tool']
    assert len(set(calls)) == 4
    assert calls == results
    assert body == gemini_request_to_openai(source, 'target')
    assert len(_extract_image_urls(ChatCompletionRequest(**body))) == 8
    companion = body['messages'][3]['content']
    assert [p['text'] for p in companion if p['type'] == 'text'][::2] == ['first', 'between']
    assert [p['image_url']['url'] for p in companion if p['type'] == 'image_url'] == [
        'data:image/png;base64,' + media()['inlineData']['data'],
        'data:image/png;base64,' + media((92, 17, 3))['inlineData']['data'],
    ]


def test_nested_response_text_preserved_without_base64_in_text():
    source = native([{'text': 'before'}, media(), {'text': 'after'}])
    source['contents'][1]['parts'][0]['functionResponse']['response'] = {'result': {'output': '  original\ntext  '}}
    body = gemini_request_to_openai(source, 'target')
    assert body['messages'][1]['content'] == json.dumps({'output': '  original\ntext  '})
    parts = body['messages'][-1]['content']
    assert parts[0] == {'type': 'text', 'text': 'before'}
    assert parts[-1] == {'type': 'text', 'text': 'after'}
    assert not any(media()['inlineData']['data'] in p['text'] for p in parts if p['type'] == 'text')


import pytest


@pytest.mark.parametrize('parts', [None, {}, 'bad', [None, 7, {}, {'inlineData': []}],
    [{'inlineData': {'mimeType': 'text/plain', 'data': 'aGVsbG8='}}],
    [{'inlineData': {'mimeType': 'image/png', 'data': '%%%'}}],
    [{'inlineData': {'mimeType': 'image/png', 'data': 'aGVsbG8='}}],
    [{'inlineData': {'mimeType': 'image/png', 'data': ''}}],
    [{'inlineData': {'mimeType': [], 'data': 'YQ=='}}],
    [{'fileData': {'fileUri': 'file:///not-read.png'}}],
])
def test_malformed_absent_and_nonimage_parts_do_not_claim_success(parts):
    source = native([])
    source['contents'][1]['parts'][0]['functionResponse']['parts'] = parts
    assert gemini_request_to_openai(source, 'target') == gemini_request_to_openai(native([]), 'target')


def test_decoded_budget_is_bounded_per_request(monkeypatch):
    import app.compat.adapters.gemini as adapter
    monkeypatch.setattr(adapter, 'MAX_TOOL_IMAGE_BYTES', len(png_bytes()))
    body = gemini_request_to_openai(native([media(), media()]), 'target')
    assert len(_extract_image_urls(ChatCompletionRequest(**body))) == 1
    monkeypatch.setattr(adapter, 'MAX_TOOL_IMAGE_BYTES', len(png_bytes()) - 1)
    assert not _extract_image_urls(ChatCompletionRequest(**gemini_request_to_openai(native(), 'target')))


def test_snake_case_inline_data():
    image = media()['inlineData']
    body = gemini_request_to_openai(native([{'inline_data': {'mime_type': image['mimeType'], 'data': image['data']}}]), 'target')
    assert len(_extract_image_urls(ChatCompletionRequest(**body))) == 1


def test_transport_matrix_actual_serializers():
    from app.normalizer import UniversalNormalizer
    from app.compat.adapters.antigravity_upstream import openai_to_cloudcode_envelope
    from app.normalizer_v2 import to_canonical, from_canonical, DIALECT_OPENAI_CHAT, DIALECT_RESPONSES, DIALECT_GEMINI

    body = gemini_request_to_openai(native(), 'target')
    request = UniversalNormalizer.normalize_to_openai(copy.deepcopy(body))
    chat = request.model_dump(exclude_none=True)
    assert _extract_image_urls(ChatCompletionRequest(**chat))[0][2]['url'].endswith(media()['inlineData']['data'])
    responses = openai_to_responses(copy.deepcopy(chat))
    assert [p.get('type') for p in responses['input'][:2]] == ['function_call', 'function_call_output']
    assert responses['input'][1]['output'] == json.dumps('Image read successfully')
    image = responses['input'][2]['content'][1]
    assert image['type'] == 'input_image'
    assert base64.b64decode(image['image_url'].split(',', 1)[1]) == png_bytes()
    anthropic = UniversalNormalizer.normalize_to_anthropic(request)
    assert anthropic['messages'][1]['content'][0]['tool_use_id'] == 'read-1'
    image = anthropic['messages'][2]['content'][1]
    assert image['type'] == 'image'
    assert base64.b64decode(image['source']['data']) == png_bytes()
    gemini = openai_to_cloudcode_envelope(chat, 'gemini-3.1-pro', project='offline-test')['request']
    parts = [p for c in gemini['contents'] for p in c['parts']]
    assert [p['thoughtSignature'] for p in parts if 'thoughtSignature' in p] == ['call-signature', 'result-signature']
    assert base64.b64decode(next(p['inlineData']['data'] for p in parts if 'inlineData' in p)) == png_bytes()
    canonical = to_canonical(DIALECT_OPENAI_CHAT, chat)
    for dialect in (DIALECT_OPENAI_CHAT, DIALECT_RESPONSES, DIALECT_GEMINI):
        wire = from_canonical(dialect, canonical)
        assert media()['inlineData']['data'] in json.dumps(wire)
    for wire in (chat, responses, anthropic, gemini):
        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('text', 'output') and isinstance(item, str):
                        assert media()['inlineData']['data'] not in item
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(wire)


def test_responses_legacy_tool_output_strings_unchanged():
    body = {'model': 'target', 'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'x', 'function': {'name': 'f', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'x', 'content': [{'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'next'}]},
    ]}
    output = openai_to_responses(body)['input']
    assert output[1] == {'type': 'function_call_output', 'call_id': 'x', 'output': 'one\ntwo'}
    assert output[2] == {'role': 'user', 'content': 'next'}


def test_scout_recovered_images_grounded_fallback_and_honest_failure(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.scouts import vision

    config = {'tools': {'vision_bridge_enabled': True}}
    monkeypatch.setattr(vision, '_resolve_vision_candidates', lambda *a: [('connection', 'vision', 'provider')])
    describe = AsyncMock(return_value='Independent scout description')
    monkeypatch.setattr(vision, '_describe_image', describe)
    request = ChatCompletionRequest(**gemini_request_to_openai(native(), 'text-only-target'))
    out = asyncio.run(vision.polyfill_vision(request, None, config))
    assert describe.call_args.args[0]['url'].endswith(media()['inlineData']['data'])
    assert not _extract_image_urls(out)
    assert 'Independent scout description' in json.dumps(out.model_dump())
    assert out.messages[1].tool_call_id == 'read-1'
    describe.side_effect = RuntimeError('scout unavailable')
    out = asyncio.run(vision.polyfill_vision(ChatCompletionRequest(**gemini_request_to_openai(native(), 'text-only-target')), None, config))
    assert vision.PLACEHOLDER_UNREADABLE in json.dumps(out.model_dump())
    assert 'Independent scout description' not in json.dumps(out.model_dump())
    # Exercise the real wall-clock timeout and its propagated exception.
    async def slow(*a, **kw):
        await asyncio.sleep(0.05)
    monkeypatch.setattr(vision, '_describe_image', slow)
    config['tools']['vision_total_budget_s'] = 0.001
    with pytest.raises(vision.VisionPolyfillFailed, match='budget'):
        asyncio.run(vision.polyfill_vision(ChatCompletionRequest(**gemini_request_to_openai(native(), 'text-only-target')), None, config))
