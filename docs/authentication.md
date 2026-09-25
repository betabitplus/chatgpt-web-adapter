# Authentication and Session Lifecycle

`chatgpt-web-adapter` uses an existing ChatGPT web account session. It does not
use an OpenAI API key and does not implement an alternative login protocol.

## Install Auth Extras

Install the browser extra required by the legacy interactive login flow, and add the secret-store extra on desktop systems where reusable credentials should live in the OS credential store:

```bash
python -m pip install "chatgpt-web-adapter[browser,secret-store]"
```

`secret-store` installs the mature Python `keyring` abstraction (macOS Keychain, Windows Credential Manager, and supported Linux Secret Service backends). CWA still supports an explicit owner-only file fallback for portable/headless environments.

## First Login

```bash
chatgpt-web-adapter auth login --auth-file auth_data.json
```

The command opens the SDK's persistent Chromium profile. Complete the normal
ChatGPT login in that window and leave it open until the command reports that
authorization was saved.

Default profile locations:

- Windows: `%LOCALAPPDATA%\chatgpt-web-adapter\browser-profile`
- macOS: `~/Library/Application Support/chatgpt-web-adapter/browser-profile`
- Linux: `$XDG_STATE_HOME/chatgpt-web-adapter/browser-profile`, or
  `~/.local/state/chatgpt-web-adapter/browser-profile`

Set `CHATGPT_WEB_ADAPTER_PROFILE_DIR` or pass `--profile-dir` to override it.

## What Is Stored

The login command creates two related pieces of reusable state. With a working `keyring` backend, the access/session tokens, cookies, structured `browserCookies`, reusable request headers, and compatible legacy fields are stored as one JSON credential blob in the OS credential store. `auth_data.json` remains as an owner-only metadata pointer containing the backend/account identifier and non-secret expiry/timestamp hints. If keyring support is unavailable, `auto` retains the same data in the existing owner-only file backend instead.

The persistent browser profile remains separate browser-side session state. `browserCookies` preserves cookie domain, path, expiry, SameSite, priority, and source metadata. Neither `proof_token` nor `turnstile_token` is persisted by the reusable auth store; those values are short-lived and memory-only.

Backend rules are intentionally asymmetric for safety: an existing plaintext file may migrate to keyring only after the keyring write is verified, and the metadata file is replaced only afterward. Once metadata says the profile is keyring-backed, a temporary keyring outage fails closed rather than silently writing bearer-equivalent credentials back to plaintext.

Treat a secret-bearing fallback file and the browser profile as secrets. The keyring metadata file itself contains no reusable credential values, but should still remain local and owner-only.

## Normal Startup

Recommended client for an application that sends messages:

```python
from chatgpt_web_adapter import ChatGPTWebClient

client = ChatGPTWebClient(
    auth_file="auth_data.json",
    auto_login=True,
    auto_sentinel=True,
    sentinel_headless=True,
)
```

- `auto_login=True` opens the persistent profile only if auth is missing or
  `/api/auth/session` refresh fails.
- `auto_sentinel=True` obtains a fresh official-page Sentinel bundle for a
  protected write.
- `sentinel_headless=True` runs Chromium without a visible window after the
  first interactive login.

Headless is not browserless: Chromium is still running as the browser engine.

## Refresh Behavior

On client construction, a missing or near-expiry access token is refreshed
through `GET https://chatgpt.com/api/auth/session`. The refresh uses the saved
session cookies, updates access/session metadata, preserves the structured
browser cookie jar, and atomically rewrites `auth_data.json`. It does not launch
Chromium.

Refresh explicitly with:

```bash
chatgpt-web-adapter auth refresh --auth-file auth_data.json
```

or with `client.refresh_auth()`.

## Status Without Exposing Secrets

```bash
chatgpt-web-adapter auth status --auth-file auth_data.json
```

The command reports token/session expiry, structured cookie count, persistent profile location, credential backend provenance, and whether the profile exists. It does not print credential values.

## Credential Backend Migration and Logout

`auto` is the default write policy: use keyring when a usable OS backend is installed, otherwise use the hardened file fallback. For new or already file-backed auth, choose the portable backend explicitly during login/refresh with `--credential-store file`, or require the OS backend with `--credential-store keyring`. An already keyring-backed profile is never silently downgraded by refresh/login; use the explicit `auth migrate --backend file` path so the old OS-store copy is removed safely.

Existing authorization can be moved explicitly without re-login:

```bash
chatgpt-web-adapter auth migrate --auth-file auth_data.json --backend keyring
chatgpt-web-adapter auth migrate --auth-file auth_data.json --backend file
```

Migration never deletes the last usable credential set before the target copy exists. `auth migrate --backend file` writes the private file first and then deletes the OS-store item; a deletion failure is reported instead of being hidden.

Remove reusable local authorization with:

```bash
chatgpt-web-adapter auth logout --auth-file auth_data.json
```

For a keyring-backed profile, logout deletes the OS credential first and removes the metadata file only after that succeeds. It does not claim to revoke the server-side ChatGPT session or delete the separate browser profile.

## Forced Reauthentication

Use a forced login when ChatGPT rejects the saved session or the persistent
profile belongs to the wrong account:

```bash
chatgpt-web-adapter auth login --force --auth-file auth_data.json
```

`--force` deletes ChatGPT session-cookie variants from the SDK profile before
opening the login page. It does not delete unrelated browser data.

## Concurrent Processes

Only one process can use the same persistent Chromium profile at a time. The SDK
uses a cross-process lock around login and Sentinel capture. A second process
waits briefly and then raises a clear profile-busy error instead of corrupting
the profile.

Use separate `browser_profile_dir` values if truly independent concurrent
browser sessions are required.

## Linux Without a Desktop

The supported sequence is:

1. Perform the first interactive login where Chromium can display a window, or
   through a temporary desktop/X session on the target machine.
2. Preserve the SDK profile and `auth_data.json` on that same trusted machine.
3. Run later writes with `sentinel_headless=True`.

Copying only `auth_data.json` to a clean server is not guaranteed to reproduce
the browser session. A completely Chromium-free protected-write mode is not
currently supported.
