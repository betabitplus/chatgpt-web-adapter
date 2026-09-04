# Downstream maintenance

This checkout is the maintained downstream fork used by Betabit.

- `origin`: `https://github.com/betabitplus/chatgpt-web-adapter.git` — writable downstream fork.
- `upstream`: `https://github.com/kymuco/chatgpt-web-adapter.git` — original repository; fetch only.
- `main`: the maintained downstream branch. It contains upstream plus the minimal Betabit patches still needed in production.

## Updating from upstream

```bash
git fetch upstream
git switch main
git merge upstream/main
python -m pytest -q
python tools/check_extension_js_syntax.py
git push origin main
```

If upstream adds or changes quality gates, satisfy the new upstream gate before pushing. Do not keep a local workaround when upstream now provides the same behavior: remove the downstream patch and use upstream.

## When ChatGPT Web changes

Keep browser/product drift isolated here. Reproduce the smallest failing browser-owned turn, add or update a regression test/stream fixture, fix the lowest stable boundary, then run the full suite and a real live smoke.

The preferred live architecture is:

```text
submit proof -> debugger detach -> passive page fetch/WebSocket stream -> terminal event -> short settle -> canonical final reconcile
```

Normal live observation must not poll ChatGPT. Observe the page's existing conversation `fetch` response and any `subscribe_ws_topic` handoff on the page's own WebSocket; never create a second stream connection. Polling is only a bounded fallback if passive transport cannot be observed or ends without a terminal event. Avoid DOM scraping as the primary path and never move browser transport work into `gptty`.

The dedicated CWA browser profile is CWA-owned. Its steady state is exactly two ChatGPT tabs: the runtime tab and a minimal same-origin canonical-read tab at `https://chatgpt.com/robots.txt`. The read tab deliberately avoids loading a second ChatGPT application while retaining authenticated same-origin canonical fetches. Its access token is cached in-page for at most 60 seconds and refreshed on authentication failure so canonical reads do not fetch `/api/auth/session` every time. Canonical reconcile prunes orphaned `chatgpt.com` tabs left by browser session restore or previous runs.

After reinstalling changed extension assets, explicitly reload the unpacked extension before live verification. A Chrome process restart alone can retain stale MV3 imported-worker code.

## Adding features

Create a short-lived branch from `main`. Put ChatGPT/browser/protocol/session/finality behavior in this repository. Put only terminal UX/rendering/commands in `gptty`. Merge to downstream `main` only after tests and the relevant live smoke pass.

Generic fixes should be proposed upstream when practical. The long-term goal is to keep the downstream diff as small as possible.
