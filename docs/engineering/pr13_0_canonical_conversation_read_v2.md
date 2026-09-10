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
GET /backend-api/conversations/{id}?include_has_versions=true&num_turns=<hint>
```

with a flat `messages[]` current branch and `page_info` pagination. Older pages
are requested with `before=<start_cursor>`.

Initial external drift measurements commonly showed `num_turns=100`. An
authenticated PR13.0 live gate on 2026-09-10 then reproduced a long-conversation
failure on the same current plural surface: `num_turns=100` returned HTTP 500
after about 30 seconds. The same conversation with `num_turns=20` returned HTTP
200 with a flat `messages[]` response, `page_info.has_previous_page=true`, and a
valid `start_cursor`. The response contained 1107 message records despite the
query value 20. The singular `/backend-api/conversation/{id}` surface returned
HTTP 500 for that conversation as well.

PR13.0 therefore treats `num_turns=20` as a product-observed server query hint,
not as a client-side page-size or message-count guarantee. Pagination completeness
continues to be governed exclusively by `page_info`, while bounded `get_messages`
requests decide whether another page is needed from the number of actually parsed
messages rather than from the query hint.

The external observation and authenticated live gate are treated as drift/evidence
signals, not as runtime authority. CWA preserves its own fail-closed identity,
finality, retry and Browser Authority contracts.

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

The normal canonical read used by finality/status fetches one current response
only. It must not paginate the full history during every finality poll.

For `get_messages(limit=N)`, CWA first parses one bounded current response. If that
response already supplies at least `N` matching messages, it stops there. If the
response is insufficient and `page_info.has_previous_page=true`, it uses the full
reader. This deliberately avoids assuming that the `num_turns` query hint equals a
message count.

Unbounded message history uses full pagination directly. Pagination:

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

The long-chat live gate showed that the singular endpoint can itself return HTTP
500, so PR13.0 deliberately does not hide current-endpoint server failures behind
an unproven singular recovery path.

This keeps cohort compatibility without turning arbitrary current-endpoint
failures into hidden transport substitution.

## Browser-owned read plane

`service_worker_canonical_read_v2.js` owns the active authenticated browser-context
read. It preserves the PR11.2 authority lane and SHA-256 sealed chunk transfer.
The active read-domain assembly imports v2 explicitly; the old PR11.2 file remains
historical evidence and is not modified in place.

The browser-owned path uses the same `num_turns=20` server query hint and the same
`page_info`/`before` completeness contract as the direct canonical reader.

For a one-response read the exact successful response bytes are transferred. When
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

The conversation-owned files surface was investigated separately in PR13.1–13.4
and is not part of this canonical conversation-read change.
