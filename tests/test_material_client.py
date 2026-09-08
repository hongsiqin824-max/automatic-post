from __future__ import annotations

from app.services.material_client import MaterialClient


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        return self.responses.pop(0)


def test_fetch_all_uses_contract_pagination_and_retries_429(monkeypatch):
    session = _Session([
        _Response({}, status_code=429),
        _Response({"code": 0, "msg": "ok", "data": {"items": [{"translate_title": "标题一", "translate_body": "<p>正文一</p>", "source": "marca", "source_url": "https://example.com/1", "archive_id": 0, "dqd_litpic": "", "channels": [1]}], "total": 2, "has_more": False}}),
        _Response({"code": 0, "msg": "ok", "data": {"items": [{"translate_title": "标题二", "translate_body": "<p>正文二</p>", "source": "marca", "source_url": "https://example.com/2", "archive_id": 0, "dqd_litpic": "", "channels": []}], "total": 2, "has_more": False}}),
    ])
    monkeypatch.setattr("app.services.material_client.time.sleep", lambda seconds: None)
    client = MaterialClient("https://example.test", "sk", "caller", session=session, max_retries=1)
    result = client.fetch_all(["marca"], hours=6, limit=1)

    assert len(result.items) == 2
    assert [call["offset"] for call in session.calls] == [0, 0, 1]
    assert session.calls[1]["source"] == "marca"
    assert session.calls[1]["hours"] == 6


def test_normalize_item_filters_blacklisted_channels_without_changing_raw_payload():
    from app.services.material_client import normalize_item

    raw = {
        "translate_title": "标签过滤测试",
        "translate_body": "<p>正文</p>",
        "source": "marca",
        "source_url": "https://example.com/channel-filter",
        "channels": [89, 700000001, 93, 189],
    }

    normalized = normalize_item(raw)

    assert normalized["channels"] == [700000001, 189]
    assert normalized["raw_payload"]["channels"] == [89, 700000001, 93, 189]
