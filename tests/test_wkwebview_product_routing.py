from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from chatgpt_web_adapter.browser_native_provider import BrowserNativeTurnResult
from chatgpt_web_adapter.product_capabilities import (
    FILES,
    MULTIMODAL_CONTINUATION,
    PRODUCT_CAPABILITY_NAMES,
    CapabilityState,
)
from chatgpt_web_adapter.product_runtime import assemble_product_runtime
from chatgpt_web_adapter.wkwebview_provider import WKWebViewTurnProvider


def _turn(*, conversation_id: str) -> BrowserNativeTurnResult:
    return BrowserNativeTurnResult(
        conversation_id=conversation_id,
        turn_exchange_id="wk-turn",
        response_status=200,
        response_mime_type="text/event-stream",
        final_url=f"https://chatgpt.com/c/{conversation_id}",
        tab_id=None,
        tab_was_active=False,
        elapsed_ms=12,
        passive_observer_armed=True,
    )


def test_explicit_model_slug_stays_on_native_wk(monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    seen: dict[str, Any] = {}

    def fake_native(text: str, **kwargs: Any) -> BrowserNativeTurnResult:
        seen.update(kwargs)
        return _turn(conversation_id="wk-model-conversation")

    monkeypatch.setattr(provider, "_send_text_impl", fake_native)

    result = provider.send_text("hello", model_slug="gpt-test-exact")

    assert result.conversation_id == "wk-model-conversation"
    assert seen["model_slug"] == "gpt-test-exact"


def test_general_file_stays_on_native_wk(tmp_path: Path, monkeypatch) -> None:
    provider = WKWebViewTurnProvider()
    notes = tmp_path / "notes.txt"
    notes.write_text("payload", encoding="utf-8")
    seen: dict[str, Any] = {}

    def fake_native(text: str, **kwargs: Any) -> BrowserNativeTurnResult:
        seen.update(kwargs)
        return _turn(conversation_id="wk-file-conversation")

    monkeypatch.setattr(provider, "_send_text_impl", fake_native)

    result = provider.send_text("read file", attachment_paths=[notes])

    assert result.conversation_id == "wk-file-conversation"
    assert seen["attachment_paths"] == [notes]


class _CanonicalSource:
    def get_status(self, conversation: Any) -> SimpleNamespace:
        return SimpleNamespace(status="completed")

    def get_messages(self, conversation: Any) -> list[Any]:
        return []

    def attach_conversation(self, conversation: Any) -> SimpleNamespace:
        return SimpleNamespace(conversation_id=str(conversation))


def test_wk_runtime_keeps_model_file_and_temporary_on_native_provider() -> None:
    runtime = assemble_product_runtime(
        client=_CanonicalSource(),
        browser_authority_backend="wkwebview",
        browser_authority_policy="TURN_SCOPED",
    )
    provider = runtime.write_transport.provider
    governance = runtime.governance()

    assert isinstance(provider, WKWebViewTurnProvider)
    assert provider.supports_model_slug is True
    assert provider.temporary_chat_supported is True
    assert runtime.write_transport._temporary_runtime.provider is provider
    assert governance["model_slug_product_runtime_selection_supported"] is True
    assert governance["temporary_chat_product_runtime_selection_supported"] is True


def test_wk_capability_states_match_chrome_native_surface() -> None:
    chrome = assemble_product_runtime(
        client=_CanonicalSource(),
        browser_authority_backend="chrome-native",
        browser_authority_policy="TURN_SCOPED",
    )
    wk = assemble_product_runtime(
        client=_CanonicalSource(),
        browser_authority_backend="wkwebview",
        browser_authority_policy="TURN_SCOPED",
    )

    chrome_capabilities = chrome.capabilities()
    wk_capabilities = wk.capabilities()
    chrome_states = {
        name: chrome_capabilities.state(name) for name in PRODUCT_CAPABILITY_NAMES
    }
    wk_states = {name: wk_capabilities.state(name) for name in PRODUCT_CAPABILITY_NAMES}

    assert wk_states == chrome_states
    assert wk_capabilities.state(FILES) is CapabilityState.AVAILABLE
    assert wk_capabilities.state(MULTIMODAL_CONTINUATION) is CapabilityState.AVAILABLE
