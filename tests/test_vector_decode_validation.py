"""Vector decoding keeps strict JSON numeric validation with less Python work."""

from array import array
import json
import math
import random
import struct

import pytest

from serein.recall import typed_surface


@pytest.fixture(params=['stdlib', 'orjson'])
def vector_decoder_backend(request, monkeypatch):
    backend = None if request.param == 'stdlib' else pytest.importorskip('orjson')
    monkeypatch.setattr(typed_surface, '_vector_json', backend)
    typed_surface._VECTOR_CACHE.clear()
    yield request.param
    typed_surface._VECTOR_CACHE.clear()


def previous_decode(raw):
    values = json.loads(str(raw))
    if not isinstance(values, list) or any(type(value) not in (int, float) or not math.isfinite(value)
                                          for value in values):
        raise ValueError('Stored embedding is not a finite JSON number array')
    return array('d', values)


@pytest.mark.parametrize('raw', [
    '[]', '[1,2]', '[0,-0.0,2e-3,1e308]', '[1e308,1e308]',
    ' [2, 3.14, -4e-5] ', '[1\n,2\t]', '[9007199254740993,-9007199254740993]',
    '[true]', '[false]', '[null]', '["2"]', '["true"]', '["\\u0074rue"]',
    '[[]]', '[{}]', '{}', '0', 'null', 'true', '[NaN]', '[Infinity]', '[-Infinity]',
    '[1e999]', '[[true]]', '[1,true,0]', '[1,,2]', '[0x1]', '[+1]', '[01]', '[1_0]',
    '[1e309,-1e309]', '[' + '9' * 400 + ']',
])
def test_decoding_matches_previous_acceptance_values_and_exception_types(raw, vector_decoder_backend):
    typed_surface._VECTOR_CACHE.clear()
    try:
        expected = previous_decode(raw)
    except Exception as exc:
        with pytest.raises(type(exc)):
            typed_surface._decode_vector(raw)
    else:
        assert typed_surface._decode_vector(raw).tobytes() == expected.tobytes()


def test_random_numeric_vectors_keep_every_stored_double_and_cache_key(vector_decoder_backend):
    rng = random.Random(901109)
    typed_surface._VECTOR_CACHE.clear()
    for length in (0, 1, 2, 9, 64, 4096):
        values = [rng.uniform(-1, 1) for _ in range(length)]
        values += [rng.randrange(-2 ** 60, 2 ** 60), -0.0]
        raw = json.dumps(values, separators=(',', ':'))
        expected = previous_decode(raw)
        decoded = typed_surface._decode_vector(raw)
        assert decoded.tobytes() == expected.tobytes()
        assert typed_surface._decode_vector(raw) is decoded
        changed = json.dumps(values + [1])
        assert typed_surface._decode_vector(changed).tobytes() == previous_decode(changed).tobytes()


def test_rejected_vectors_do_not_populate_cache(vector_decoder_backend):
    typed_surface._VECTOR_CACHE.clear()
    for raw in ('[true]', '[false]', '[null]', '["2"]', '[NaN]', '[Infinity]', '[[]]'):
        with pytest.raises(ValueError):
            typed_surface._decode_vector(raw)
        assert raw not in typed_surface._VECTOR_CACHE


def test_random_ieee_doubles_preserve_exact_bytes_with_both_parsers(vector_decoder_backend):
    rng = random.Random(901109)
    values = [0., -0., 5e-324, -5e-324, 1.7976931348623157e308]
    for _ in range(20000):
        value = struct.unpack('!d', rng.getrandbits(64).to_bytes(8, 'big'))[0]
        if math.isfinite(value):
            values.append(value)
    for start in range(0, len(values), 256):
        raw = json.dumps(values[start:start + 256], separators=(',', ':'))
        assert typed_surface._decode_vector(raw).tobytes() == previous_decode(raw).tobytes()


def test_big_json_integers_preserve_conversion_and_overflow(vector_decoder_backend):
    rng = random.Random(901109)
    for bits in (53, 54, 63, 64, 65, 80, 100, 512, 1023, 1024, 1025, 2048):
        for _ in range(25):
            number = rng.getrandbits(bits)
            raw = json.dumps([number, -number])
            try:
                expected = previous_decode(raw)
            except Exception as exc:
                with pytest.raises(type(exc)):
                    typed_surface._decode_vector(raw)
            else:
                assert typed_surface._decode_vector(raw).tobytes() == expected.tobytes()


def test_fast_parser_errors_fall_back_to_stdlib_without_changing_failures(monkeypatch):
    fast = pytest.importorskip('orjson')

    class RejectingParser:
        JSONDecodeError = fast.JSONDecodeError
        calls = []

        @classmethod
        def loads(cls, raw):
            cls.calls.append(raw)
            raise cls.JSONDecodeError('synthetic parser rejection', raw, 0)

    monkeypatch.setattr(typed_surface, '_vector_json', RejectingParser)
    typed_surface._VECTOR_CACHE.clear()
    assert typed_surface._decode_vector('[1,-0.0]').tobytes() == previous_decode('[1,-0.0]').tobytes()
    with pytest.raises(json.JSONDecodeError):
        typed_surface._decode_vector('[invalid]')
    with pytest.raises(ValueError, match='finite JSON number array'):
        typed_surface._decode_vector('[NaN]')
    assert RejectingParser.calls == ['[1,-0.0]', '[invalid]', '[NaN]']


def test_fast_numeric_payload_does_not_use_stdlib_json(monkeypatch):
    fast = pytest.importorskip('orjson')
    monkeypatch.setattr(typed_surface, '_vector_json', fast)
    typed_surface._VECTOR_CACHE.clear()
    monkeypatch.setattr(typed_surface.json, 'loads', lambda *_args, **_kwargs: pytest.fail('slow vector parser'))
    assert typed_surface._decode_vector('[0.25,-0.5]').tolist() == [0.25, -0.5]
