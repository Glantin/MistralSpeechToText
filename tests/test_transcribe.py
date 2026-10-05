"""Unit tests for the pure error-classification logic in transcribe.py.

These assert on the STABLE `kind` returned by classify_error (english keys) and
on the retriable/permanent decision, never on the human message text (which may
be localized), so they survive UI wording changes.
"""

import transcribe


class _HTTPExc(Exception):
    """Minimal stand-in for a mistralai/httpx error carrying a status code."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def test_http_status_reads_status_code():
    assert transcribe.http_status(_HTTPExc("boom", 429)) == 429


def test_http_status_none_when_absent_or_out_of_range():
    assert transcribe.http_status(Exception("no status here 400")) is None
    assert transcribe.http_status(_HTTPExc("weird", 999)) is None


def test_is_retriable_transient_status_codes():
    for code in (408, 429, 500, 502, 503, 599):
        assert transcribe.is_retriable(_HTTPExc("x", code)) is True, code


def test_is_retriable_permanent_4xx():
    for code in (400, 401, 403, 404, 422):
        assert transcribe.is_retriable(_HTTPExc("x", code)) is False, code


def test_is_retriable_network_without_status_is_transient():
    assert transcribe.is_retriable(Exception("connection timed out")) is True


def test_is_retriable_ssl_is_permanent():
    assert transcribe.is_retriable(Exception("certificate verify failed")) is False


def test_classify_error_ssl_beats_auth():
    # A proxy TLS failure must NOT be misread as a rejected key.
    kind, _ = transcribe.classify_error(Exception("SSL: CERTIFICATE_VERIFY_FAILED self signed"))
    assert kind == "ssl"


def test_classify_error_auth():
    assert transcribe.classify_error(Exception("401 Unauthorized"))[0] == "auth"
    assert transcribe.classify_error(Exception("Invalid API key"))[0] == "auth"


def test_classify_error_auth_by_status_code():
    # A rejected/forbidden key must be classified as auth even if the message
    # text carries no HTTP number (we read the status code).
    assert transcribe.classify_error(_HTTPExc("nope", 401))[0] == "auth"
    assert transcribe.classify_error(_HTTPExc("nope", 403))[0] == "auth"


def test_classify_error_forbidden_mentions_access_not_app():
    # 403 = key lacks Voxtral access: the message must make clear it's a key
    # issue, not the app.
    _, msg = transcribe.classify_error(_HTTPExc("Forbidden", 403))
    assert "403" in msg


def test_classify_error_network():
    assert transcribe.classify_error(Exception("Connection timeout"))[0] == "network"


def test_classify_error_other_is_fallback():
    assert transcribe.classify_error(Exception("totally unexpected"))[0] == "other"


def test_valid_bias_term_accepts_single_token():
    assert transcribe._valid_bias_term("Voxtral") == "Voxtral"
    assert transcribe._valid_bias_term("  kubectl  ") == "kubectl"


def test_valid_bias_term_rejects_spaces_and_internal_commas():
    assert transcribe._valid_bias_term("Mistral AI") is None
    assert transcribe._valid_bias_term("a,b") is None
    assert transcribe._valid_bias_term("") is None
    assert transcribe._valid_bias_term("   ") is None


def test_valid_bias_term_strips_edge_commas():
    assert transcribe._valid_bias_term("Mistral,") == "Mistral"


def test_request_timeout_scales_with_take_length(tmp_path):
    import config

    bps = config.SAMPLE_RATE * config.CHANNELS * 2
    short = tmp_path / "short.wav"
    short.write_bytes(b"\0" * (bps * 5))  # 5 s
    medium = tmp_path / "medium.wav"
    medium.write_bytes(b"\0" * (bps * 120))  # 2 min
    long_ = tmp_path / "long.wav"
    long_.write_bytes(b"\0" * (bps * 600))  # 10 min

    assert transcribe.request_timeout_seconds(str(short)) == config.HTTP_READ_TIMEOUT
    assert transcribe.request_timeout_seconds(str(medium)) == 120
    assert transcribe.request_timeout_seconds(str(long_)) == config.HTTP_READ_TIMEOUT_MAX
