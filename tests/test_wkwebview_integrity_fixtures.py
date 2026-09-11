from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "src" / "chatgpt_web_adapter" / "wkwebview_helper" / "minimal_security_shell.js"
FIXTURES = ROOT / "tests" / "fixtures" / "wkwebview_integrity"


def _node() -> str:
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node.js is required for minimal-shell integrity fixture tests")
    return executable


def _discover(fixture: Path, *, streaming: bool) -> dict[str, str]:
    script = r"""
const fs = require("fs");
const shell = fs.readFileSync(process.argv[1], "utf8");
const fixture = fs.readFileSync(process.argv[2], "utf8");
const start = shell.indexOf("  const matchingBrace =");
const end = shell.indexOf("  const decodeAttachmentDescriptors =");
if (start < 0 || end <= start) throw new Error("production discovery block not found");
const block = shell.slice(start, end);
const hooks = new Function(`${block}\nreturn { discoverIntegrityExports, discoverIntegrityExportsStreaming };`)();

async function run() {
  let result;
  if (process.argv[3] === "streaming") {
    const encoded = new TextEncoder().encode(fixture);
    const chunkSizes = [7, 11, 5, 23, 13, 3, 19];
    let offset = 0;
    let chunkIndex = 0;
    const response = {
      body: {
        getReader() {
          return {
            async read() {
              if (offset >= encoded.length) return { value: undefined, done: true };
              const size = chunkSizes[chunkIndex % chunkSizes.length];
              chunkIndex += 1;
              const value = encoded.slice(offset, Math.min(encoded.length, offset + size));
              offset += value.length;
              return { value, done: false };
            },
            async cancel() {
              offset = encoded.length;
            },
          };
        },
      },
      async text() {
        return fixture;
      },
    };
    result = await hooks.discoverIntegrityExportsStreaming(response);
  } else {
    result = hooks.discoverIntegrityExports(fixture);
  }
  process.stdout.write(JSON.stringify(result));
}

run().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""
    mode = "streaming" if streaming else "direct"
    completed = subprocess.run(
        [_node(), "-e", script, str(SHELL), str(fixture), mode],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=10,
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        (
            "conversation-small-deploy-a.js",
            {"helperExport": "hA", "initializerExport": "iA"},
        ),
        (
            "conversation-small-deploy-b.js",
            {
                "helperExport": "renamedHelperB",
                "initializerExport": "renamedInitializerB",
            },
        ),
    ],
)
def test_minimal_shell_discovers_sanitized_known_integrity_layouts(
    fixture_name: str,
    expected: dict[str, str],
) -> None:
    fixture = FIXTURES / fixture_name

    assert _discover(fixture, streaming=False) == expected
    assert _discover(fixture, streaming=True) == expected


def test_integrity_fixtures_remain_sanitized() -> None:
    for fixture in sorted(FIXTURES.glob("conversation-small-*.js")):
        text = fixture.read_text(encoding="utf-8")
        assert "Bearer " not in text
        assert "accessToken" not in text
        assert "resume-secret" not in text
        assert "authorization" not in text.lower()
