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
submit proof -> debugger detach -> passive existing Web response stream -> terminal event -> canonical final reconcile
```

Normal live observation must not poll ChatGPT. Polling is only a bounded fallback if the passive stream cannot be observed. Avoid DOM scraping as the primary path and never move browser transport work into `gptty`.

The dedicated CWA browser profile is CWA-owned. Its steady state is exactly two ChatGPT tabs: the runtime tab and the canonical-read tab. Canonical reconcile prunes orphaned `chatgpt.com` tabs left by browser session restore or previous runs.

After reinstalling changed extension assets, explicitly reload the unpacked extension before live verification. A Chrome process restart alone can retain stale MV3 imported-worker code.

## Adding features

Create a short-lived branch from `main`. Put ChatGPT/browser/protocol/session/finality behavior in this repository. Put only terminal UX/rendering/commands in `gptty`. Merge to downstream `main` only after tests and the relevant live smoke pass.

Generic fixes should be proposed upstream when practical. The long-term goal is to keep the downstream diff as small as possible.
