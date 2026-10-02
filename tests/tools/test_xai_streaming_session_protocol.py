"""Offline contract for the xAI streaming handshake URL.

The wire protocol and the incremental bridge are upstream's and are covered by
``test_tts_streaming.py``. This file pins the one local addition: optional
tuning keys from the ``tts.xai`` config section reach the query string.
"""
from urllib.parse import parse_qs, urlsplit

from tools import tts_streaming


def _query(section):
    return parse_qs(urlsplit(tts_streaming.XAIStreamer({}, section)._session_url()).query)


def test_xai_session_url_carries_the_required_parameters():
    assert _query({"voice_id": "leo", "language": "en"}) == {
        "voice": ["leo"], "language": ["en"], "codec": ["pcm"], "sample_rate": ["24000"]
    }


def test_xai_session_url_carries_optional_tuning_keys():
    query = _query({
        "voice_id": "eve",
        "optimize_streaming_latency": 2,
        "speed": 1.2,
        "text_normalization": True,
    })
    assert query["optimize_streaming_latency"] == ["2"]
    assert query["speed"] == ["1.2"]
    assert query["text_normalization"] == ["true"]


def test_xai_session_url_omits_default_speed_and_keeps_an_existing_query():
    url = tts_streaming.XAIStreamer(
        {}, {"speed": "1.0", "streaming_url": "wss://example.test/v1/tts?region=us"}
    )._session_url()
    query = parse_qs(urlsplit(url).query)
    assert "speed" not in query
    assert query["region"] == ["us"]
    assert query["codec"] == ["pcm"]
