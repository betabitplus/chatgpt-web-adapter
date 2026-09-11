from __future__ import annotations

from pathlib import Path

EXTENSION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "chatgpt_web_adapter"
    / "browser_native_extension"
)


def test_browser_native_turn_timeout_is_not_capped_at_five_minutes() -> None:
    worker = (EXTENSION / "service_worker.js").read_text(encoding="utf-8")
    rich_input = (EXTENSION / "service_worker_rich_input_pr9_2.js").read_text(
        encoding="utf-8"
    )
    host = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "chatgpt_web_adapter"
        / "browser_native_host.py"
    ).read_text(encoding="utf-8")

    assert "Math.min(Number(message.timeoutMs), 300_000)" not in worker
    assert "Math.min(timeoutMs, 300_000)" not in rich_input
    assert "min(float(timeout_ms or default_timeout_ms) / 1000.0, 300.0)" not in host
    assert "Math.max(10_000, Number(message.timeoutMs))" in worker
    assert "return timeoutMs;" in rich_input
    assert "float(timeout_ms or default_timeout_ms) / 1000.0" in host
