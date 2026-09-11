from __future__ import annotations

import platform
import sys
from typing import Any

CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND = "chrome-native"
WKWEBVIEW_BROWSER_AUTHORITY_BACKEND = "wkwebview"
SUPPORTED_BROWSER_AUTHORITY_BACKENDS: tuple[str, ...] = (
    CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND,
    WKWEBVIEW_BROWSER_AUTHORITY_BACKEND,
)
_MINIMUM_WK_DEFAULT_MACOS = (12, 0)


def _select_default_browser_authority_backend(
    *,
    platform_name: str,
    macos_version: tuple[int, int] | None,
) -> str:
    if (
        platform_name == "darwin"
        and macos_version is not None
        and macos_version >= _MINIMUM_WK_DEFAULT_MACOS
    ):
        return WKWEBVIEW_BROWSER_AUTHORITY_BACKEND
    return CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND


def _detected_macos_version() -> tuple[int, int] | None:
    if sys.platform != "darwin":
        return None
    raw = platform.mac_ver()[0]
    try:
        parts = tuple(int(part) for part in raw.split(".")[:2])
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


DEFAULT_BROWSER_AUTHORITY_BACKEND = _select_default_browser_authority_backend(
    platform_name=sys.platform,
    macos_version=_detected_macos_version(),
)


def normalize_browser_authority_backend(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("browser authority backend must be a string")
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_BROWSER_AUTHORITY_BACKENDS:
        supported = ", ".join(SUPPORTED_BROWSER_AUTHORITY_BACKENDS)
        raise ValueError(
            f"unsupported browser authority backend {value!r}; expected one of: {supported}"
        )
    return normalized


def resolve_browser_authority_backend(value: str | None) -> str:
    if value is None:
        return DEFAULT_BROWSER_AUTHORITY_BACKEND
    return normalize_browser_authority_backend(value)


def assemble_browser_authority_provider(value: str) -> Any | None:
    normalized = normalize_browser_authority_backend(value)
    if normalized == CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND:
        # BrowserOwnedProductTransport owns the existing production provider
        # assembly when no explicit provider is injected.
        return None
    if normalized == WKWEBVIEW_BROWSER_AUTHORITY_BACKEND:
        from .wkwebview_provider import WKWebViewTurnProvider

        return WKWebViewTurnProvider()
    raise AssertionError(f"unhandled browser authority backend: {normalized}")


__all__ = [
    "CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND",
    "DEFAULT_BROWSER_AUTHORITY_BACKEND",
    "SUPPORTED_BROWSER_AUTHORITY_BACKENDS",
    "WKWEBVIEW_BROWSER_AUTHORITY_BACKEND",
    "assemble_browser_authority_provider",
    "normalize_browser_authority_backend",
    "resolve_browser_authority_backend",
]
