# Downstream maintenance

This checkout is the maintained Betabit fork, used in production by `gptty`.

- `origin`: `https://github.com/betabitplus/chatgpt-web-adapter.git` — writable fork.
- `upstream`: `https://github.com/kymuco/chatgpt-web-adapter.git` — the author's repository; fetch only.
- `main`: the one maintained branch. Work happens here; a short-lived branch only for a large or risky change.
- Branches cleaned up on 2026-10-24 (all fully in `main`) are listed with their commits in
  `docs/maintenance/deleted-branches-2026-10-24.txt`; restore one with
  `git push origin <sha>:refs/heads/<name>`.
- `.ai-bridge/` (not in git) keeps the working notes of past sessions: why things were done.

Our line is the WKWebView backend: macOS WebKit, no Chrome, lighter than the author's browser
extension. It is the default on macOS 12+.

## The author's repository: a reference, not a merge source

Since 2026-10-24 we do not merge `upstream/main`. The two lines have gone different ways (the author:
Chrome extension and Direct HTTP; we: WKWebView), and a merge costs more than it gives. Instead we
read what the author added and port by hand only what we need.

Look there first when:
- ChatGPT Web changes (a selector, a control, a stream event) — the author may already have fixed
  it on their side;
- a fix does not come out after a few tries — their solution may show the way;
- periodically, to see whether something new is worth having.

```bash
git fetch upstream
git log --oneline <reviewed>..upstream/main                 # what is new since the last review
git log --oneline <reviewed>..upstream/main -- src/chatgpt_web_adapter/browser_native_extension
git log --oneline <reviewed>..upstream/main -i --grep composer   # by topic
git show <sha>
```

When porting, write the change in our own code and name the source in the commit message
(`ported from upstream <sha>`). Then move the marker below.

- Reviewed up to: `1a0240c8` (2026-09-11, the last merge of upstream into the fork).
- Upstream head when this policy was set: `bfefe051` (2026-10-24), not yet reviewed.
- Worth a look: composer detection for the new ChatGPT composer (`52113a5d`, 2026-10-10); Direct
  HTTP without a browser tab (`b1b1f347`, `bfefe051`).

## The Chrome variant: conserved

The `chrome-native` backend and `browser_native_extension/` stay in `main` as they are and are not
developed. They are used only when asked for explicitly (`gptty --backend chrome-native`). Against
the current ChatGPT they do not work: the extension does not find the composer (`composer_missing`,
the ChatGPT frontend changed around 2026-09-25). Keep their tests passing; do not fix or delete
them until we decide what to do with them.

Return point: tag `chrome-baseline` — here `524f400` (2026-09-07), in `gptty` `fcd409b` (2026-09-06).
It is the author's code as of `fb8cc07` (2026-09-03) plus 20 of our patches that `gptty` needs (long
turns, passive stream, stop, resume, canonical reads), before the WKWebView work. That pair was
`main` in both repositories at the time. To get a Chrome version on the author's fresh code:
branch from the tag in both repositories, merge `upstream/main` into the adapter branch, keep our
20 patches where the author has no equivalent, and run `gptty` from its tag against it.

Notes that apply only to the Chrome variant:
- The dedicated CWA browser profile keeps exactly two ChatGPT tabs: the runtime tab and a minimal
  same-origin canonical-read tab at `https://chatgpt.com/robots.txt`.
- After reinstalling changed extension assets, reload the unpacked extension before live
  verification; a Chrome restart alone can keep stale MV3 worker code.

## When ChatGPT Web changes

Reproduce the smallest failing turn, add or update a regression test or stream fixture, fix the
lowest stable boundary, then run the suite and a real live smoke (for example one Temporary turn
and a gpt-research agent). Check the author's repository for the same change first (see above).

Live observation follows the page's own response stream: submit proof → passive page stream →
terminal event → canonical final reconcile. Normal observation must not poll ChatGPT; polling is
only a bounded fallback. Never move browser transport work into `gptty`.

## Adding features

Put ChatGPT/browser/protocol/session/finality behavior in this repository and only terminal
UX/rendering/commands in `gptty`. Merge into `main` after tests and the relevant live smoke pass.
