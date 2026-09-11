from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

AUXILIARY_PROCESS_TOKENS = (
    "WKChatGPTAuthority",
    "WebKit.WebContent",
    "WebKit.Networking",
    "WebKit.GPU",
)

CHILD_PROGRAM = r"""
import json
import sys

from chatgpt_web_adapter import assemble_product_runtime

marker = sys.argv[1]
auth_file = sys.argv[2]
conversation = None if sys.argv[3] == "-" else sys.argv[3]
profile = sys.argv[4]

runtime = assemble_product_runtime(auth_file=auth_file)
observed = {}

def on_event(event):
    if event.get("type") == "browser_native_write_completed":
        for key in (
            "canonical_read_transport",
            "canonical_read_fallback_reason",
            "phase_a_transport",
            "phase_a_gate_wait_ms",
            "phase_b_transport",
            "phase_b_fallback_reason",
        ):
            observed[key] = event.get(key)

response = runtime.send(
    f"Reply exactly {marker} and nothing else.",
    conversation=conversation,
    timeout=180,
    model_profile=profile,
    on_token=lambda _token: None,
    on_event=on_event,
)
print(
    json.dumps(
        {
            "marker": marker,
            "text": response.text,
            "conversation_id": response.conversation.conversation_id,
            **observed,
        },
        sort_keys=True,
    )
)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the WKWebView promotion multi-process release load gate."
    )
    parser.add_argument(
        "--auth-file",
        type=Path,
        default=Path.home() / ".local/share/gptty/profiles/chatgpt-web/auth_data.json",
    )
    parser.add_argument(
        "--mode",
        choices=("new", "continuation", "mixed"),
        required=True,
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--conversation",
        action="append",
        default=[],
        help="Conversation id for continuation/mixed inputs; repeat as needed.",
    )
    parser.add_argument("--marker-prefix", default="WK_MAIN_LOAD")
    parser.add_argument(
        "--profile",
        choices=("FAST", "BALANCED", "DEEP"),
        default="FAST",
        help="Model profile used for load turns; transport/resource gate defaults to FAST/INSTANT.",
    )
    parser.add_argument("--sample-interval", type=float, default=0.05)
    return parser.parse_args()


def _ps_rows() -> dict[int, dict[str, Any]]:
    completed = subprocess.run(
        [
            "ps",
            "-axo",
            "pid=,ppid=,rss=,%cpu=,comm=,command=",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rows: dict[int, dict[str, Any]] = {}
    for raw in completed.stdout.splitlines():
        parts = raw.strip().split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss_kb = int(parts[2])
            cpu_percent = float(parts[3])
        except ValueError:
            continue
        rows[pid] = {
            "ppid": ppid,
            "rss_kb": rss_kb,
            "cpu_percent": cpu_percent,
            "comm": parts[4],
            "command": parts[5],
        }
    return rows


def _is_wk_helper(row: dict[str, Any]) -> bool:
    command = str(row.get("command") or "")
    return (
        "WKChatGPTAuthority.app/Contents/MacOS/WKChatGPTAuthority" in command
        and not command.startswith(("/bin/", "/usr/bin/"))
    )


def _is_webkit_auxiliary(row: dict[str, Any]) -> bool:
    command = str(row.get("command") or "")
    return command.startswith("/System/Library/Frameworks/WebKit.framework/")


def _auxiliary_pids(rows: dict[int, dict[str, Any]]) -> set[int]:
    return {
        pid
        for pid, row in rows.items()
        if _is_wk_helper(row) or _is_webkit_auxiliary(row)
    }


def _descendants(rows: dict[int, dict[str, Any]], roots: set[int]) -> set[int]:
    result = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, row in rows.items():
            if pid in result:
                continue
            if row["ppid"] in result:
                result.add(pid)
                changed = True
    return result


def _specs(args: argparse.Namespace) -> list[tuple[str, str]]:
    workers = max(1, int(args.workers))
    conversations = [
        str(item).strip() for item in args.conversation if str(item).strip()
    ]
    if args.mode == "continuation" and len(conversations) < workers:
        raise SystemExit("continuation mode requires one --conversation per worker")
    if args.mode == "mixed" and len(conversations) < workers // 2:
        raise SystemExit(
            "mixed mode requires --conversation values for half the workers"
        )

    result: list[tuple[str, str]] = []
    continuation_index = 0
    for index in range(1, workers + 1):
        use_continuation = args.mode == "continuation" or (
            args.mode == "mixed" and index > (workers + 1) // 2
        )
        conversation = "-"
        kind = "NEW"
        if use_continuation:
            conversation = conversations[continuation_index]
            continuation_index += 1
            kind = "CONT"
        marker = f"{args.marker_prefix}_{args.mode.upper()}_{kind}_{index}_OK_20260911"
        result.append((marker, conversation))
    return result


def main() -> int:
    args = parse_args()
    auth_file = args.auth_file.expanduser().resolve()
    if not auth_file.is_file():
        raise SystemExit(f"auth file not found: {auth_file}")

    specs = _specs(args)
    baseline_rows = _ps_rows()
    baseline_aux = _auxiliary_pids(baseline_rows)
    started = time.monotonic()

    processes: list[subprocess.Popen[str]] = []
    for marker, conversation in specs:
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    CHILD_PROGRAM,
                    marker,
                    str(auth_file),
                    conversation,
                    args.profile,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        )

    root_pids = {process.pid for process in processes}
    max_wk_helpers = 0
    peak_rss_kb = 0
    peak_cpu_percent = 0.0
    observed_aux_names: set[str] = set()

    while any(process.poll() is None for process in processes):
        rows = _ps_rows()
        new_aux = _auxiliary_pids(rows) - baseline_aux
        tracked = _descendants(rows, root_pids) | new_aux

        helper_count = 0
        rss_kb = 0
        cpu_percent = 0.0
        for pid in tracked:
            row = rows.get(pid)
            if row is None:
                continue
            rss_kb += int(row["rss_kb"])
            cpu_percent += float(row["cpu_percent"])
            if _is_wk_helper(row):
                helper_count += 1
            if pid in new_aux:
                if _is_wk_helper(row):
                    observed_aux_names.add("WKChatGPTAuthority")
                elif _is_webkit_auxiliary(row):
                    observed_aux_names.add("WebKit")

        max_wk_helpers = max(max_wk_helpers, helper_count)
        peak_rss_kb = max(peak_rss_kb, rss_kb)
        peak_cpu_percent = max(peak_cpu_percent, cpu_percent)
        time.sleep(max(0.01, float(args.sample_interval)))

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=5)
        lines = [line for line in stdout.splitlines() if line.strip()]
        try:
            result = json.loads(lines[-1]) if lines else {"missing_output": True}
        except json.JSONDecodeError:
            result = {"unparsed_stdout": stdout[-2000:]}
        results.append(result)
        if process.returncode != 0 or stderr.strip():
            errors.append(
                {
                    "returncode": process.returncode,
                    "stderr": stderr[-2000:],
                }
            )

    exact = all(
        isinstance(item.get("marker"), str)
        and isinstance(item.get("text"), str)
        and item["text"] == item["marker"]
        for item in results
    )
    lightweight = all(
        item.get("phase_b_transport")
        in {
            "curl_cffi_websocket",
            "phase_one_terminal",
            "canonical_message_id_recovery",
        }
        and not item.get("phase_b_fallback_reason")
        for item in results
    )
    recovery_count = sum(
        item.get("phase_b_transport") == "canonical_message_id_recovery"
        for item in results
    )
    continuation_reads_lightweight = all(
        item.get("canonical_read_transport") in {None, "curl_cffi", "cache"}
        for item in results
    )
    phase_a_serialized = max_wk_helpers <= 1

    payload = {
        "ok": (
            not errors
            and exact
            and lightweight
            and continuation_reads_lightweight
            and phase_a_serialized
        ),
        "mode": args.mode,
        "profile": args.profile,
        "workers": len(processes),
        "results": results,
        "errors": errors,
        "exact_markers": exact,
        "lightweight_phase_b": lightweight,
        "recovery_count": recovery_count,
        "continuation_reads_lightweight": continuation_reads_lightweight,
        "max_phase_a_helpers": max_wk_helpers,
        "phase_a_serialized": phase_a_serialized,
        "peak_rss_mb": round(peak_rss_kb / 1024.0, 1),
        "peak_cpu_percent": round(peak_cpu_percent, 1),
        "elapsed_s": round(time.monotonic() - started, 2),
        "auxiliary_process_names": sorted(observed_aux_names),
        "conversation_ids": [
            item.get("conversation_id")
            for item in results
            if isinstance(item.get("conversation_id"), str)
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
