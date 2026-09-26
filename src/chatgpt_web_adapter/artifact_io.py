from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_private_artifact_text(path: str | Path, text: str) -> Path:
    """Create one artifact file privately without replacing an existing target.

    The payload is fully written and fsynced to an owner-only temporary file first.
    On POSIX the final basename is then published atomically with a hard link.  The
    bundle manifest is written last by callers and is the artifact completion marker.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)

        if os.name != "nt":
            os.chmod(temporary, 0o600)

        try:
            os.link(temporary, target)
        except FileExistsError:
            raise
        except OSError:
            # Some non-POSIX filesystems do not support hard links. Preserve the
            # no-overwrite contract with O_EXCL; the manifest-last bundle contract
            # still prevents a partial file from being treated as complete.
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(target, flags, 0o600)
            try:
                payload = temporary.read_bytes()
                with os.fdopen(fd, "wb", closefd=True) as output:
                    fd = -1
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                if fd >= 0:
                    os.close(fd)

        if os.name != "nt":
            os.chmod(target, 0o600)
        return target
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
