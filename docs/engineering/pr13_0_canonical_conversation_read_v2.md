# PR13.0 — Canonical Conversation Read v2

PR13.0 repairs the canonical read plane for the current ChatGPT conversation
history surface while retaining a bounded compatibility path for cohorts that
still expose the legacy conversation endpoint.

## Drift evidence

Independent dated product measurement in August/September 2026 observed the
ordinary saved-conversation read move from:

```text
GET /backend-api/conversation/{id}
```

with a `mapping` tree to:

```text
GET /backend-api/conversations/{id}?include_has_versions=true&num_turns=100
```

with a flat `messages[]` current branch and `page_info` pagination. Older pages
are requested with `before=<start_cursor>`. The observed endpoint rejects an
unbounded `num_turns`; PR13.0 therefore uses the observed 100-turn page size and
bounds total pagination separately.

The external observation is treated as a drift signal, not as runtime authority.
CWA preserves its own fail-closed identity, finality, retry and Browser Authority
contracts.

## Wire-shape normalization

CWA consumers continue to operate on one canonical current-branch contract.

- a legacy payload that already contains `mapping` remains unchanged;
- a current payload containing ordered `messages[]` is normalized into a
  deterministic linear `mapping` keyed by product-owned message ids;
- parent/children links represent only the ordered current branch supplied by the
  product;
- no sibling/version tree is invented;
- duplicate message ids or an unbound `current_node` fail closed.

This keeps `status`, `attach`, canonical finality and existing message parsing
independent of the upstream wire format.

## Read policy

The normal canonical read used by finality/status fetches one current page only.
It must not paginate the full history during every finality poll.

Full pagination is a separate path used when a consumer requests unbounded or
large message history. Pagination:

- follows `page_info.has_previous_page` through `start_cursor`;
- sends `before=<cursor>`;
- rejects repeated cursors;
- is bounded to 100 pages;
- rejects conversation-id disagreement across pages;
- deduplicates stable message ids at page boundaries.

## Compatibility fallback

The current plural endpoint is always attempted first. A legacy singular read is
authorized only by a current-endpoint HTTP 404. Authentication failures,
challenges, validation failures and server errors do not trigger fallback.

This keeps cohort compatibility without turning arbitrary current-endpoint
failures into hidden transport substitution.

## Browser-owned read plane

`service_worker_canonical_read_v2.js` owns the active authenticated browser-context
read. It preserves the PR11.2 authority lane and SHA-256 sealed chunk transfer.
The active read-domain assembly imports v2 explicitly; the old PR11.2 file remains
historical evidence and is not modified in place.

For a one-page read the exact successful response bytes are transferred. When
full history is requested, the browser assembles the bounded paginated payload,
serializes that assembled object, and seals the resulting bytes before crossing
the local bridge.

## Authority invariants

PR13.0 does not change:

```text
streaming != canonical finality
observation != write authority
ambiguous write -> reconciliation -> no automatic retry
```

It also does not touch ordinary-text write identity (#79/#80), rich-input request
binding, Temporary Chat identity, connector authorization or generated-artifact
handoff.

## Follow-up experiment

After PR13.0 stabilizes, the next bounded research gate is the newly observed
conversation-owned files surface:

```text
GET /backend-api/conversations/{id}/files
```

That probe will test one narrow question: whether the product now exposes stable
conversation-owned generated-artifact identity sufficient to reconsider the
currently frozen artifact-download handoff. It is intentionally not part of
PR13.0.
