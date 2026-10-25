from __future__ import annotations

from types import SimpleNamespace

from chatgpt_web_adapter.browser_cookies import (
    flatten_browser_cookies,
    serialize_browser_cookies,
)


def test_browser_cookie_domain_validation_rejects_lookalikes() -> None:
    records = serialize_browser_cookies(
        [
            SimpleNamespace(name="exact", value="ok", domain="chatgpt.com"),
            SimpleNamespace(name="subdomain", value="ok", domain=".auth.chatgpt.com"),
            SimpleNamespace(
                name="prefix-lookalike", value="bad", domain="evilchatgpt.com"
            ),
            SimpleNamespace(
                name="suffix-lookalike",
                value="bad",
                domain="chatgpt.com.evil.example",
            ),
        ]
    )

    assert [record["name"] for record in records] == ["exact", "subdomain"]
    assert flatten_browser_cookies(
        [
            {"name": "exact", "value": "ok", "domain": "chatgpt.com"},
            {"name": "subdomain", "value": "ok", "domain": ".auth.chatgpt.com"},
            {"name": "prefix-lookalike", "value": "bad", "domain": "evilchatgpt.com"},
            {
                "name": "suffix-lookalike",
                "value": "bad",
                "domain": "chatgpt.com.evil.example",
            },
        ]
    ) == {"exact": "ok", "subdomain": "ok"}
