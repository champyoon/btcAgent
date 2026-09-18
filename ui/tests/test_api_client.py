"""api_client.py의 HTTP 결과 정규화 검증 — 실제 네트워크 호출 없이 requests.post/get을
몽키패치해서 성공/404/409/타임아웃/연결 실패를 각각 확인한다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_client


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, json_error: bool = False):
        self.status_code = status_code
        self._json_body = json_body
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._json_body


def test_query_success_returns_body_as_is():
    fake = _FakeResponse(200, {"answer": "hello", "trace": []})
    with patch("api_client.requests.post", return_value=fake) as mock_post:
        result = api_client.query("http://localhost:8000", "질문")
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "http://localhost:8000/query"
    assert kwargs["json"] == {
        "question": "질문", "proceed_with_stale_data": False, "awaiting_input_token": "",
    }
    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["body"] == {"answer": "hello", "trace": []}
    assert result["error_kind"] is None


def test_query_strips_trailing_slash_from_base_url():
    fake = _FakeResponse(200, {"answer": "ok"})
    with patch("api_client.requests.post", return_value=fake) as mock_post:
        api_client.query("http://localhost:8000/", "질문")
    assert mock_post.call_args[0][0] == "http://localhost:8000/query"


def test_query_forwards_awaiting_input_token_when_given():
    fake = _FakeResponse(200, {"answer": "ok"})
    with patch("api_client.requests.post", return_value=fake) as mock_post:
        api_client.query("http://localhost:8000", "200만원", awaiting_input_token="tok-await-1")
    assert mock_post.call_args[1]["json"]["awaiting_input_token"] == "tok-await-1"


def test_confirm_budget_change_404_maps_to_http_error_with_detail():
    fake = _FakeResponse(404, {"detail": "존재하지 않는 확인 토큰입니다."})
    with patch("api_client.requests.post", return_value=fake):
        result = api_client.confirm_budget_change("http://localhost:8000", "tok-123")
    assert result["ok"] is False
    assert result["status_code"] == 404
    assert result["error_kind"] == "http_error"
    assert result["detail"] == "존재하지 않는 확인 토큰입니다."


def test_confirm_budget_change_409_maps_to_http_error_with_detail():
    fake = _FakeResponse(409, {"detail": "이미 처리된 확인 토큰입니다."})
    with patch("api_client.requests.post", return_value=fake):
        result = api_client.confirm_budget_change("http://localhost:8000", "tok-123")
    assert result["status_code"] == 409
    assert result["error_kind"] == "http_error"
    assert result["detail"] == "이미 처리된 확인 토큰입니다."


def test_timeout_is_reported_as_timeout_not_generic_error():
    with patch("api_client.requests.post", side_effect=requests.exceptions.Timeout("timed out")):
        result = api_client.approve("http://localhost:8000", "app-1")
    assert result["ok"] is False
    assert result["error_kind"] == "timeout"
    assert result["status_code"] is None


def test_connection_error_is_reported_distinctly_from_timeout():
    with patch("api_client.requests.post", side_effect=requests.exceptions.ConnectionError("refused")):
        result = api_client.reject("http://localhost:8000", "app-1")
    assert result["ok"] is False
    assert result["error_kind"] == "connection_error"
    assert result["status_code"] is None


def test_non_json_body_does_not_raise():
    fake = _FakeResponse(200, json_error=True)
    with patch("api_client.requests.post", return_value=fake):
        result = api_client.query("http://localhost:8000", "질문")
    assert result["ok"] is True
    assert result["body"] is None


def test_health_ok():
    fake = _FakeResponse(200, {"status": "ok"})
    with patch("api_client.requests.get", return_value=fake):
        result = api_client.health("http://localhost:8000")
    assert result["ok"] is True


def test_health_connection_failure_does_not_raise():
    with patch("api_client.requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
        result = api_client.health("http://localhost:8000")
    assert result["ok"] is False
    assert result["status_code"] is None
