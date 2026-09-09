from __future__ import annotations

import inspect

import chatgpt_web_adapter.browser_native_client as browser_native_client
import chatgpt_web_adapter.browser_owned_write_runtime as browser_owned_write_runtime
from chatgpt_web_adapter.canonical_product_observation_gate_pr9_3 import (
    _PR93_CANONICAL_OBSERVATION_GATE_MARKER,
    install_canonical_product_observation_gate,
)
from chatgpt_web_adapter.client import ChatGPTWebClient
from chatgpt_web_adapter.product_runtime_observation_gate import (
    gate_product_runtime_send_text_observed,
)
from chatgpt_web_adapter.product_transport import ProductRuntimeExecution
from chatgpt_web_adapter.types import ChatResponse


def _topology_snapshot() -> tuple[object, object, object, object]:
    return (
        browser_native_client._wait_for_new_final_assistant,
        browser_native_client.send_browser_native,
        browser_owned_write_runtime.send_browser_native,
        ChatGPTWebClient.send_browser_native,
    )


def test_canonical_observation_gates_are_statically_composed() -> None:
    assert getattr(
        browser_native_client._wait_for_new_final_assistant,
        _PR93_CANONICAL_OBSERVATION_GATE_MARKER,
        False,
    )
    assert getattr(
        browser_native_client.send_browser_native,
        _PR93_CANONICAL_OBSERVATION_GATE_MARKER,
        False,
    )
    assert (
        browser_owned_write_runtime.send_browser_native
        is browser_native_client.send_browser_native
    )
    assert ChatGPTWebClient.send_browser_native is browser_native_client.send_browser_native


def test_legacy_installer_is_identity_stable_after_static_composition() -> None:
    before = _topology_snapshot()

    install_canonical_product_observation_gate()

    assert _topology_snapshot() == before


def test_observed_execution_does_not_install_or_replace_browser_native_functions() -> None:
    before = _topology_snapshot()

    def fake_send_text_observed(self, text, *args, **kwargs):
        assert text == "hello"
        assert callable(kwargs.get("on_event"))
        return ProductRuntimeExecution(
            transport="test",
            response=ChatResponse(text="ok"),
            observation=None,
        )

    gated = gate_product_runtime_send_text_observed(fake_send_text_observed)
    result = gated(object(), "hello")

    assert result.response.text == "ok"
    assert _topology_snapshot() == before


def test_runtime_observation_gate_contains_no_lazy_canonical_installer() -> None:
    source = inspect.getsource(gate_product_runtime_send_text_observed)

    assert "install_canonical_product_observation_gate" not in source
