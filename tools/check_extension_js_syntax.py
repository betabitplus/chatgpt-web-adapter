from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "chatgpt_web_adapter"
EXTENSION = PACKAGE / "browser_native_extension"
WK_HELPER = PACKAGE / "wkwebview_helper"


def javascript_files() -> list[Path]:
    files = sorted(EXTENSION.glob("*.js"))
    minimal_shell = WK_HELPER / "minimal_security_shell.js"
    if minimal_shell.is_file():
        files.append(minimal_shell)
    return files


def main() -> int:
    node = shutil.which("node")
    if node is None:
        raise SystemExit("Node.js is required for package JavaScript syntax validation")

    files = javascript_files()
    if not files:
        raise SystemExit("no package JavaScript files found")

    for path in files:
        subprocess.run(
            [node, "--check", str(path)],
            check=True,
            cwd=ROOT,
        )

    print(f"validated JavaScript syntax for {len(files)} packaged files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
