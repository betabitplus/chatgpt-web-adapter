from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools import (
    check_extension_js_syntax,
    installed_wheel_smoke,
    release_gate,
)

VERSION = "0.3.0"
REPOSITORY_VERSION = "0.3.1"


def _root(tmp_path: Path, *, changelog: str = "# Changelog\n\n## Unreleased\n") -> Path:
    root = tmp_path / "repo"
    extension = root / "src" / "chatgpt_web_adapter" / "browser_native_extension"
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text("{}\n", encoding="utf-8")
    (extension / "service_worker.js").write_text("// worker\n", encoding="utf-8")
    (extension / "extra.js").write_text("// extra\n", encoding="utf-8")
    wk_helper = root / "src" / "chatgpt_web_adapter" / "wkwebview_helper"
    wk_helper.mkdir(parents=True)
    (wk_helper / "WKChatGPTAuthority.m").write_text("// objc\n", encoding="utf-8")
    (wk_helper / "Info.plist").write_text("<plist/>\n", encoding="utf-8")
    (wk_helper / "minimal_security_shell.js").write_text("// shell\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "chatgpt-web-adapter"\nversion = "0.3.0"\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return root


def _entry_points_text(*, broken: bool = False) -> str:
    target = (
        "chatgpt_web_adapter.cli:main" if broken else "chatgpt_web_adapter.cli_v02:main"
    )
    return (
        "[console_scripts]\n"
        f"cwa = {target}\n"
        f"chatgpt-web-adapter = {target}\n"
        "chatgpt-web-adapter-native-host = chatgpt_web_adapter.browser_native_host:main\n"
    )


def _write_dist(
    root: Path,
    *,
    broken_entry_points: bool = False,
    omit_extra_js: bool = False,
    omit_wk_shell: bool = False,
    wheel_extra: tuple[str, str] | None = None,
    sdist_extra: tuple[str, str] | None = None,
) -> Path:
    dist = root / "dist"
    dist.mkdir()
    wheel = dist / f"chatgpt_web_adapter-{VERSION}-py3-none-any.whl"
    dist_info = f"chatgpt_web_adapter-{VERSION}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA", f"Name: chatgpt-web-adapter\nVersion: {VERSION}\n"
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            _entry_points_text(broken=broken_entry_points),
        )
        for required in release_gate.REQUIRED_WHEEL_FILES:
            if omit_wk_shell and required.endswith("/minimal_security_shell.js"):
                continue
            archive.writestr(required, "x\n")
        if not omit_extra_js:
            archive.writestr(
                "chatgpt_web_adapter/browser_native_extension/extra.js", "x\n"
            )
        if wheel_extra is not None:
            archive.writestr(*wheel_extra)
    sdist = dist / f"chatgpt_web_adapter-{VERSION}.tar.gz"
    root_name = f"chatgpt_web_adapter-{VERSION}"
    with tarfile.open(sdist, "w:gz") as archive:
        for suffix in release_gate.REQUIRED_SDIST_SUFFIXES:
            info = tarfile.TarInfo(root_name + suffix)
            payload = b"x\n"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if sdist_extra is not None:
            relative_name, text = sdist_extra
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root_name}/{relative_name.lstrip('/')}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return dist


def test_repository_package_version_is_staged_for_0_3_1_candidate() -> None:
    repo = Path(__file__).resolve().parents[1]
    assert release_gate.project_version(repo / "pyproject.toml") == REPOSITORY_VERSION


def test_candidate_gate_allows_changelog_finalization_to_remain_separate(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    report = release_gate.run_release_gate(root=root)
    assert report["ok"] is True
    assert report["version"] == VERSION
    assert report["tag"] is None
    assert report["changelog_release_date"] is None


def test_tagged_gate_requires_dated_changelog(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(release_gate.ReleaseGateError, match="CHANGELOG"):
        release_gate.run_release_gate(root=root, tag="v0.3.0")


def test_tagged_gate_requires_exact_version_match(tmp_path: Path) -> None:
    root = _root(tmp_path, changelog="# Changelog\n\n## 0.3.0 - 2026-09-01\n")
    with pytest.raises(release_gate.ReleaseGateError, match="tag/version mismatch"):
        release_gate.run_release_gate(root=root, tag="v0.3.1")
    report = release_gate.run_release_gate(root=root, tag="refs/tags/v0.3.0")
    assert report["tag"] == VERSION
    assert report["changelog_release_date"] == "2026-09-01"


def test_installed_smoke_normalizes_release_tag_versions() -> None:
    assert installed_wheel_smoke.normalize_expected_version("0.3.0") == VERSION
    assert installed_wheel_smoke.normalize_expected_version("v0.3.0") == VERSION
    assert (
        installed_wheel_smoke.normalize_expected_version("refs/tags/v0.3.0") == VERSION
    )


def test_installed_smoke_can_derive_expected_version_from_source_pyproject(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    assert (
        installed_wheel_smoke.source_project_version(root / "pyproject.toml") == VERSION
    )


def test_installed_smoke_source_version_parser_is_project_scoped(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        'version = "9.9.9"\n\n'
        "[project]\n"
        'name = "chatgpt-web-adapter"\n'
        'version = "0.3.0"\n\n'
        "[tool.example]\n"
        'version = "8.8.8"\n',
        encoding="utf-8",
    )
    assert installed_wheel_smoke.source_project_version(pyproject) == VERSION


def test_release_gate_accepts_complete_wheel_and_sdist(tmp_path: Path) -> None:
    root = _root(tmp_path)
    dist = _write_dist(root)
    report = release_gate.run_release_gate(root=root, dist_dir=dist)
    assert report["artifacts"]["wheel"]["filename"].endswith(".whl")
    assert report["artifacts"]["wheel"]["extension_files"] == 3
    assert report["artifacts"]["sdist"]["filename"].endswith(".tar.gz")


def test_release_gate_rejects_omitted_extension_package_data(tmp_path: Path) -> None:
    root = _root(tmp_path)
    dist = _write_dist(root, omit_extra_js=True)
    with pytest.raises(
        release_gate.ReleaseGateError, match="omitted packaged browser extension"
    ):
        release_gate.run_release_gate(root=root, dist_dir=dist)


def test_release_gate_rejects_omitted_wk_helper_package_data(tmp_path: Path) -> None:
    root = _root(tmp_path)
    dist = _write_dist(root, omit_wk_shell=True)
    with pytest.raises(release_gate.ReleaseGateError, match="missing required files"):
        release_gate.run_release_gate(root=root, dist_dir=dist)


def test_release_gate_rejects_forbidden_wheel_temp_artifact(tmp_path: Path) -> None:
    root = _root(tmp_path)
    dist = _write_dist(
        root,
        wheel_extra=("chatgpt_web_adapter/cwa-wk-resume-orphan", "x\n"),
    )
    with pytest.raises(
        release_gate.ReleaseGateError, match="forbidden repository artifacts"
    ):
        release_gate.run_release_gate(root=root, dist_dir=dist)


def test_release_gate_rejects_experiment_file_in_sdist(tmp_path: Path) -> None:
    root = _root(tmp_path)
    dist = _write_dist(
        root,
        sdist_extra=("experiments/wkwebview_authority/probe.txt", "x\n"),
    )
    with pytest.raises(
        release_gate.ReleaseGateError, match="forbidden repository artifacts"
    ):
        release_gate.run_release_gate(root=root, dist_dir=dist)


def test_release_gate_rejects_local_checkout_path_in_artifact_text(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    dist = _write_dist(
        root,
        wheel_extra=("chatgpt_web_adapter/local_path_probe.py", str(root)),
    )
    with pytest.raises(release_gate.ReleaseGateError, match="local checkout path"):
        release_gate.run_release_gate(root=root, dist_dir=dist)


def test_release_gate_rejects_removed_wk_broker_runtime_marker(tmp_path: Path) -> None:
    root = _root(tmp_path)
    dist = _write_dist(
        root,
        sdist_extra=(
            "src/chatgpt_web_adapter/wkwebview_helper/legacy_probe.m",
            "RunResumeBroker\n",
        ),
    )
    with pytest.raises(
        release_gate.ReleaseGateError, match="removed WK broker runtime"
    ):
        release_gate.run_release_gate(root=root, dist_dir=dist)


def test_release_gate_rejects_console_entrypoint_drift(tmp_path: Path) -> None:
    root = _root(tmp_path)
    dist = _write_dist(root, broken_entry_points=True)
    with pytest.raises(
        release_gate.ReleaseGateError, match="console entry points mismatch"
    ):
        release_gate.run_release_gate(root=root, dist_dir=dist)


def test_release_gate_rejects_ambiguous_distribution_set(tmp_path: Path) -> None:
    root = _root(tmp_path)
    dist = _write_dist(root)
    (dist / "extra.whl").write_bytes(b"not-a-wheel")
    with pytest.raises(release_gate.ReleaseGateError, match="exactly one wheel"):
        release_gate.run_release_gate(root=root, dist_dir=dist)


def test_pyproject_freezes_console_scripts_and_extension_package_data() -> None:
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'version = "0.3.1"' in text
    assert 'cwa = "chatgpt_web_adapter.cli_v02:main"' in text
    assert 'chatgpt-web-adapter = "chatgpt_web_adapter.cli_v02:main"' in text
    assert (
        'chatgpt-web-adapter-native-host = "chatgpt_web_adapter.browser_native_host:main"'
        in text
    )
    assert '"browser_native_extension/*.json"' in text
    assert '"browser_native_extension/*.js"' in text
    assert '"wkwebview_helper/*.m"' in text
    assert '"wkwebview_helper/*.plist"' in text
    assert '"wkwebview_helper/*.js"' in text


def test_package_javascript_syntax_gate_includes_wk_minimal_shell() -> None:
    files = check_extension_js_syntax.javascript_files()
    assert any(path.name == "minimal_security_shell.js" for path in files)


def test_ci_builds_once_then_smokes_exact_wheel_on_linux_and_windows() -> None:
    text = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    assert "python tools/release_gate.py --dist-dir dist" in text
    assert "actions/upload-artifact@v4" in text
    assert "actions/download-artifact@v4" in text
    assert "installed-wheel-smoke:" in text
    assert "ubuntu-latest" in text and "windows-latest" in text
    assert '"3.10"' in text and '"3.14"' in text
    assert "python tools/installed_wheel_smoke.py --wheel-dir dist" in text
    assert "--expected-version 0.2.0" not in text
    assert "--expected-version 0.3.0" not in text


def test_ci_has_blocking_macos_wk_release_coverage() -> None:
    text = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "macos-wk:" in text
    assert "runs-on: macos-latest" in text
    assert "-Wall -Wextra -Werror" in text
    assert "tests/test_wkwebview_backend.py" in text
    assert "tests/test_wkwebview_lightweight_transport.py" in text
    assert "tests/test_wkwebview_temporary.py" in text
    assert "tests/test_wkwebview_stream_observation.py" in text
    assert "python tools/check_extension_js_syntax.py" in text
    assert "python tools/release_gate.py --dist-dir dist" in text
    assert "python tools/installed_wheel_smoke.py --wheel-dir dist" in text
    assert "      - macos-wk" in text


def test_publish_workflow_gates_tag_and_exact_wheel_before_upload() -> None:
    text = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")
    macos_job = text.index("  macos-wk-tagged:")
    publish_job = text.index("  publish:")
    tag_gate = text.index("python tools/release_gate.py")
    wheel_smoke = text.index("python tools/installed_wheel_smoke.py")
    publish = text.index("pypa/gh-action-pypi-publish@release/v1")
    assert macos_job < publish_job
    assert "runs-on: macos-latest" in text
    assert "      - macos-wk-tagged" in text
    assert "tests/test_wkwebview_backend.py" in text
    assert "tests/test_wkwebview_integrity_fixtures.py" in text
    assert "-Wall -Wextra -Werror" in text
    assert "github.event.release.tag_name" in text
    assert "ref: ${{ github.event.release.tag_name }}" in text
    assert '--expected-version "${{ github.event.release.tag_name }}"' in text
    assert tag_gate < wheel_smoke < publish


def test_readme_and_release_checklist_present_0_3_user_and_release_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    checklist = (root / "docs" / "release_checklist.md").read_text(encoding="utf-8")
    for command in (
        "cwa doctor",
        "cwa status",
        "cwa capabilities",
        "cwa send",
        "cwa messages",
        "cwa snapshot",
        "cwa export",
    ):
        assert command in readme
    assert (
        "GitHub tag version == pyproject package version == dated CHANGELOG release heading"
        in readme
    )
    assert "installed-wheel smoke" in checklist
    assert "Post-publish verification" in checklist
    assert "v0.3.1" in checklist
    assert "tagged macOS WK verification" in checklist
