from __future__ import annotations

from typing import Any

CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND = "chrome-native"
WKWEBVIEW_BROWSER_AUTHORITY_BACKEND = "wkwebview"
DEFAULT_BROWSER_AUTHORITY_BACKEND = CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND
SUPPORTED_BROWSER_AUTHORITY_BACKENDS: tuple[str, ...] = (
    CHROME_NATIVE_BROWSER_AUTHORITY_BACKEND,
    WKWEBVIEW_BROWSER_AUTHORITY_BACKEND,
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
]
