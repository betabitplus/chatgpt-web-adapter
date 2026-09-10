# PR13.1 — Conversation Files Identity Probe

## Question

Does the authenticated ChatGPT product expose an explicit conversation-scoped file identity surface at:

```text
GET /backend-api/conversations/{conversation_id}/files
```

that is materially stronger than PR10.1's point-in-time generated-artifact observation?

This is a characterization experiment, not a production capability promotion.

## Boundaries

The probe performs exactly one authenticated GET for one caller-supplied saved-conversation identity.

It does not:

- submit a ChatGPT turn;
- retry or fall back to another endpoint;
- download an artifact;
- follow a URL or signed locator;
- write to the local filesystem other than normal process output;
- grant download, overwrite, retry, write, or finality authority;
- expose the raw response body;
- export URL, signed URL, token, cookie, credential, or authorization values.

The report may export only bounded identity/metadata candidates such as an opaque explicit ID, basename-like filename, MIME type, non-negative size, and booleans describing whether conversation/message identity fields or locator fields were present.

## Characterizations

The probe distinguishes:

- `AUTHENTICATION_REQUIRED` — HTTP 401;
- `ACCESS_CHALLENGED` — HTTP 403;
- `ENDPOINT_ABSENT_OR_NOT_VISIBLE` — HTTP 404;
- `FILES_ENDPOINT_HTTP_ERROR` — other HTTP failure;
- `UNRECOGNIZED_FILES_RESPONSE_SHAPE` — successful response without a recognized list shape;
- `EMPTY_FILE_COLLECTION_OBSERVED` — recognized empty collection;
- `NON_OBJECT_FILE_RECORD_OBSERVED` — a collection contains a non-object item;
- `FILE_RECORDS_WITHOUT_EXPLICIT_IDENTITY` — at least one file record has no safe explicit identity field;
- `DUPLICATE_EXPLICIT_IDENTITY_CANDIDATE_OBSERVED` — explicit identities are not unique within the response;
- `EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED` — every file record exposes a safe explicit identity candidate and candidates are unique within that one response.

## What a positive result means

`EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED` is stronger evidence than a DOM-adjacent label or an inferred filename because the candidate originates from a conversation-scoped product response field.

It still does **not** prove that the identifier is stable across independent reads, sessions, regenerated artifacts, or account transitions. Therefore every PR13.1 report keeps:

```text
stable_product_identity_proven = false
download_authority_granted = false
```

A later promotion gate would require at least independent repeated observation of the same artifact identity plus a browser-owned resolution path bound to that identity. Neither is part of PR13.1.

## Relationship to PR10.1

PR10.1 correctly froze generated-artifact download handoff as:

```text
ARTIFACT_DOWNLOAD_HANDOFF_UNSUPPORTED_WITHOUT_STABLE_PRODUCT_IDENTITY
```

PR13.1 does not change that support status. It only tests whether a newer product endpoint supplies a stronger identity primitive worth investigating.

## Running the probe

From an exact clean checkout with an authenticated CWA session available:

```bash
python tools/pr13_1_conversation_files_identity_probe.py \
  --conversation-id <conversation-id> \
  --expected-head <exact-head-sha>
```

The tool emits one sanitized JSON report. A completed characterization is not equivalent to capability support.
