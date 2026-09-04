from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "src" / "chatgpt_web_adapter" / "browser_native_extension"
MANIFEST = EXTENSION / "manifest.json"
TEXT_HARDENING = EXTENSION / "service_worker_text_submit_commit_hardening_pr11_3.js"
RECOVERY = EXTENSION / "service_worker_recovery.js"
CANONICAL = EXTENSION / "service_worker_canonical_read.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_long_turn_fix_preserves_manifest_identity_and_pr12_entrypoint() -> None:
    manifest = json.loads(_read(MANIFEST))
    assert manifest["version"] == "0.1.13"
    assert manifest["background"]["service_worker"] == (
        "service_worker_browser_runtime_v2.js"
    )


def test_pr113_marks_only_ordinary_text_page_turns_for_commit_detach() -> None:
    source = _read(TEXT_HARDENING)
    assert 'PR113_COMMIT_DETACH_FLAG = "__cwaPr113OrdinaryTextCommitDetachActive"' in source
    assert "_pr113ExecuteOfficialTextWithCommitDetachSignal" in source
    assert "_pr92ActiveRichInputContext" in source
    assert "_pr813TemporaryTurnContext" in source
    assert "return _pr113PriorExecuteOfficialPageTurn(args);" in source
    assert "globalThis[PR113_COMMIT_DETACH_FLAG] = true" in source
    assert "delete globalThis[PR113_COMMIT_DETACH_FLAG]" in source


def test_core_detaches_after_http_200_headers_before_stream_completion() -> None:
    source = _read(RECOVERY)
    assert "__cwaPr113OrdinaryTextCommitDetachActive" in source
    assert "responseSeen" in source
    assert 'kind: "response_headers"' in source
    assert "diagnostics.responseStatus === 200" in source
    assert "CHATGPT_SUBMISSION_COMMITTED_DEBUGGER_DETACH_FAILED" in source
    assert "CHATGPT_SUBMISSION_COMMITTED_CONVERSATION_ID_UNRESOLVED" in source
    assert '!routeConversationId.startsWith("WEB:")' in source
    assert 'diagnostics.completionBoundary = "response_headers_commit"' in source

    commit_start = source.index("diagnostics.submissionCommitDetachRequested =")
    fallback_start = source.index("const earlySignalPromise", commit_start)
    commit = source[commit_start:fallback_start]
    response_wait = commit.index("responseSeen.then")
    status_proof = commit.index("diagnostics.responseStatus === 200")
    detach = commit.index("await chrome.debugger.detach(debuggee)")
    route_wait = commit.index("_cwaWaitForCommittedConversationRoute(")
    assert response_wait < status_proof < detach < route_wait
    assert "Network.loadingFinished" not in commit
    assert "Network.getResponseBody" not in commit
    assert "waitForComposerReady(" not in commit


def test_canonical_reads_use_separate_persistent_tab() -> None:
    source = _read(CANONICAL)
    start = source.index("async function _cwaCanonicalRuntimeTab()")
    end = source.index("\nasync function _cwaCanonicalFetch", start)
    block = source[start:end]

    assert 'CWA_CANONICAL_READ_TAB_KEY = "browserNativeCanonicalReadTabIdV1"' in source
    assert "chrome.storage.local.get(CWA_CANONICAL_READ_TAB_KEY)" in block
    assert "chrome.storage.local.set({ [CWA_CANONICAL_READ_TAB_KEY]: tab.id })" in block
    assert "storedRuntimeTabId" not in block
    assert "storeRuntimeTabId" not in block
    assert "chrome.tabs.create" in block
    assert "active: false" in block
