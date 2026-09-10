# PR13.2 — Identity-Bound Artifact Resolution Probe

## Question

Given the explicit conversation-scoped `file_id` established by PR13.1, does ChatGPT expose a product-owned resolution surface that is actually bound to that identity?

The generated-artifact candidate under test is:

```text
GET /backend-api/files/download/{file_id}?conversation_id={conversation_id}&inline=false
```

PR13.2 is a characterization experiment. It does not follow or export the returned locator and does not download artifact bytes.

## Why this surface

Recent browser-observed implementations for generated `file_...` / sediment artifacts use the `files/download/{file_id}` route with the owning conversation id. PR13.2 intentionally tests only that exact generated-file route rather than fuzzing multiple endpoint shapes.

## Gate

The live gate performs at most three authenticated read-only GET requests:

1. Read `GET /backend-api/conversations/{conversation_id}/files` and select exactly one caller-named generated artifact.
2. Resolve the product-owned `file_id` returned for that artifact through the generated-file resolution endpoint.
3. Only if the real identity resolves to a recognized locator-bearing response, repeat the same resolution request with one deterministic nonmatching identity control.

The filename is used only to select one record from the conversation-scoped files collection. It is never used as artifact identity and is never substituted into the resolution endpoint.

## Positive characterization

PR13.2 reports:

```text
IDENTITY_BOUND_RESOLUTION_SURFACE_OBSERVED
```

only when all of the following hold:

- conversation-files exposes exactly one target record with an explicit product identity;
- the real identity returns an HTTP 2xx response containing a recognized locator field;
- the nonmatching control identity is rejected with a non-rate-limit, non-server-error response;
- neither identity value nor any locator value is exported in the final report.

A positive result may set:

```text
identity_bound_resolution_surface_proven = true
```

but deliberately continues to keep:

```text
stable_product_identity_proven = false
artifact_bytes_proven = false
download_authority_granted = false
```

The distinction matters: observing a product-owned resolver that emits a locator is not yet proof that the locator can be safely followed, that returned bytes are the expected artifact, or that the identity is longitudinally stable across later sessions.

## Fail-closed characterizations

The gate keeps separate outcomes for:

- exact-head or tracked-clean preflight failure;
- inability to establish the target explicit identity;
- real-ID resolution request failure;
- real-ID response without a recognized locator;
- negative-control request failure;
- negative control unexpectedly resolving;
- negative-control rate-limit or server failure, which is inconclusive rather than positive evidence.

No fallback endpoint is attempted.

## Privacy and authority boundary

The report does not contain:

- the real `file_id` value;
- the negative-control identity value;
- signed URLs or other locator values;
- raw resolver response bodies;
- cookies, authorization material, tokens, or credentials.

The probe never follows a locator, downloads bytes, writes a product mutation, writes an artifact to disk, grants retry authority, or changes write/finality authority.

## Relationship to PR13.1 and PR10.1

PR13.1 established that one generated artifact exposed an explicit product-owned `file_id` and that the same `file_id` survived two independent immediate reads through fresh clients.

PR13.2 asks only whether that product identity is accepted by a product-owned artifact resolver and rejected when the identity is deliberately changed.

Even a positive PR13.2 result does not by itself change the public frozen status:

```text
ARTIFACT_DOWNLOAD_HANDOFF_UNSUPPORTED_WITHOUT_STABLE_PRODUCT_IDENTITY
```

A later gate would still need to prove safe identity-bound byte retrieval and artifact integrity before production download/materialization can be considered.

## Running the live gate

From an exact clean checkout with an authenticated CWA session available:

```powershell
python tools/pr13_2_identity_bound_artifact_resolution_probe.py --conversation "https://chatgpt.com/c/<conversation-id>" --expected-filename cwa_pr13_1_identity_probe.txt --expected-head <exact-head-sha>
```
