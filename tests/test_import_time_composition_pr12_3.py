from __future__ import annotations

import ast
import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "chatgpt_web_adapter"


def _assert_no_top_level_runtime_mutation(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            assert all(not isinstance(target, ast.Attribute) for target in node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            assert not isinstance(node.target, ast.Attribute)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            function = node.value.func
            if isinstance(function, ast.Name):
                assert not function.id.startswith("install_")


def _run_isolated_python(source: str) -> None:
    subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        check=True,
        cwd=ROOT,
    )


def test_package_root_is_export_only_without_class_mutation() -> None:
    _assert_no_top_level_runtime_mutation(PACKAGE / "__init__.py")
    source = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    for token in (
        "ChatGPTWebClient.send =",
        "ChatGPTProductRuntime.send_text_observed =",
        "_BrowserlessRequestTransport.__init__ =",
        "_BrowserlessRequestTransport._execute =",
    ):
        assert token not in source


def test_observation_gate_import_is_side_effect_free() -> None:
    _run_isolated_python(
        """
        import importlib

        import chatgpt_web_adapter.product_observations as product_observations
        import chatgpt_web_adapter.product_runtime_observation_gate as observation_gate

        current_runtime = importlib.import_module("chatgpt_web_adapter.product_runtime")
        current_browser_owned = importlib.import_module(
            "chatgpt_web_adapter.browser_owned_product_transport"
        )

        before = (
            current_runtime.ChatGPTProductRuntime.send_text_observed,
            current_browser_owned.BrowserOwnedProductTransport.capabilities,
            product_observations._activity_observation_kind,
        )

        importlib.reload(observation_gate)

        after = (
            current_runtime.ChatGPTProductRuntime.send_text_observed,
            current_browser_owned.BrowserOwnedProductTransport.capabilities,
            product_observations._activity_observation_kind,
        )
        assert after == before
        """
    )
    _assert_no_top_level_runtime_mutation(
        PACKAGE / "product_runtime_observation_gate.py"
    )


def test_package_reload_preserves_composed_class_and_method_identity() -> None:
    _run_isolated_python(
        """
        import importlib

        import chatgpt_web_adapter

        current_client = importlib.import_module(
            "chatgpt_web_adapter.client"
        ).ChatGPTWebClient
        current_browserless = importlib.import_module(
            "chatgpt_web_adapter.browserless_request_transport"
        ).BrowserlessRequestTransport
        current_browser_owned = importlib.import_module(
            "chatgpt_web_adapter.browser_owned_product_transport"
        ).BrowserOwnedProductTransport
        current_runtime = importlib.import_module(
            "chatgpt_web_adapter.product_runtime"
        ).ChatGPTProductRuntime

        classes_before = (
            current_client,
            current_browserless,
            current_browser_owned,
            current_runtime,
        )
        methods_before = (
            current_client.send,
            current_browserless._execute,
            current_browser_owned.capabilities,
            current_runtime.send_text_observed,
            current_runtime.submit,
            current_runtime.await_final,
            current_runtime.observe_ui_liveness,
        )

        reloaded = importlib.reload(chatgpt_web_adapter)

        assert reloaded.ChatGPTWebClient is current_client
        assert reloaded.WebChatClient is current_client
        assert reloaded.ChatGPTProductRuntime is current_runtime
        assert (
            importlib.import_module("chatgpt_web_adapter.client").ChatGPTWebClient,
            importlib.import_module(
                "chatgpt_web_adapter.browserless_request_transport"
            ).BrowserlessRequestTransport,
            importlib.import_module(
                "chatgpt_web_adapter.browser_owned_product_transport"
            ).BrowserOwnedProductTransport,
            importlib.import_module(
                "chatgpt_web_adapter.product_runtime"
            ).ChatGPTProductRuntime,
        ) == classes_before
        assert (
            current_client.send,
            current_browserless._execute,
            current_browser_owned.capabilities,
            current_runtime.send_text_observed,
            current_runtime.submit,
            current_runtime.await_final,
            current_runtime.observe_ui_liveness,
        ) == methods_before
        """
    )


def test_public_runtime_and_transports_own_static_composition_points() -> None:
    current_client = importlib.import_module("chatgpt_web_adapter.client")
    current_browserless = importlib.import_module(
        "chatgpt_web_adapter.browserless_request_transport"
    )
    current_browser_owned = importlib.import_module(
        "chatgpt_web_adapter.browser_owned_product_transport"
    )
    current_runtime = importlib.import_module("chatgpt_web_adapter.product_runtime")

    assert "send" in current_client.ChatGPTWebClient.__dict__
    assert "_poll_conversation_after_prepare" in current_client.ChatGPTWebClient.__dict__
    assert "__init__" in current_browserless.BrowserlessRequestTransport.__dict__
    assert "_execute" in current_browserless.BrowserlessRequestTransport.__dict__
    assert "capabilities" in current_browser_owned.BrowserOwnedProductTransport.__dict__

    for name in (
        "__init__",
        "send_text_observed",
        "submit",
        "await_final",
        "submission_lifecycle_snapshot",
        "observe_ui_liveness",
        "governance",
    ):
        method = current_runtime.ChatGPTProductRuntime.__dict__.get(name)
        assert callable(method)
        assert method.__module__ == "chatgpt_web_adapter.product_runtime"
