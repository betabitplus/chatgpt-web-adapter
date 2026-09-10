# PR13.1 — Conversation Files Identity Probe

## Question

Does the authenticated ChatGPT product expose an explicit conversation-scoped file identity surface at:

```text
GET /backend-api/conversations/{conversation_id}/files
```

that is materially stronger than PR10.1's point-in-time generated-artifact observation?

This is a characterization experiment, not a production capability promotion.

## R1 boundaries

The R1 identity probe performs exactly one authenticated GET for one caller-supplied saved-conversation identity.

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

## R1 characterizations

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

## Authenticated R1 evidence — 2026-09-10

A clean exact-head authenticated run against a saved ChatGPT conversation containing one generated text artifact returned HTTP 200 and:

```text
collection_key = items
record_count = 1
explicit_identity_key = file_id
filename_key = file_name
media_type_key = mime_type
media_type = text/plain
locator_field_present = false
conversation_id_field_present = false
message_id_field_present = false
characterization = EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED
```

The observed opaque identifier value is intentionally not recorded in repository evidence. The result establishes that the product endpoint exposes an explicit conversation-scoped `file_id` candidate for the generated artifact. It does not establish longitudinal stability or resolution/download semantics.

## R2 — independent short-term stability

R2 performs exactly two read-only authenticated requests through two fresh `ChatGPTWebClient` instances. A caller-supplied expected filename is used only as an anchor to select exactly one record from each response; filename equality is never treated as identity.

R2 requires:

- exactly one filename-anchored target in each read;
- an explicit identity field in both reads;
- the same explicit identity value in both reads;
- the same identity key in both reads.

The raw identity value is not exported by R2. The report contains only SHA-256 fingerprints plus equality booleans and identity-key names.

A positive R2 characterization is:

```text
SAME_EXPLICIT_IDENTITY_ACROSS_INDEPENDENT_READS
```

and may set:

```text
short_term_identity_stability_proven = true
```

R2 deliberately continues to keep:

```text
stable_product_identity_proven = false
resolution_surface_proven = false
download_authority_granted = false
```

Two immediate independent reads are evidence of short-term identity stability, not proof that the identifier survives later sessions, regeneration, account transitions, or that it can be safely resolved into downloadable bytes.

## Authenticated R2 evidence — 2026-09-10

A clean exact-head authenticated run against the same saved conversation and generated text artifact performed two reads through two fresh clients and returned:

```text
first_characterization = EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED
second_characterization = EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED
first_identity_key = file_id
second_identity_key = file_id
first_target_record_present = true
second_target_record_present = true
same_explicit_identity = true
same_identity_key = true
short_term_identity_stability_proven = true
characterization = SAME_EXPLICIT_IDENTITY_ACROSS_INDEPENDENT_READS
```

The raw identifier and its SHA-256 fingerprint are intentionally not copied into repository evidence. This result establishes that the same filename-anchored generated artifact exposed the same product-owned `file_id` across two independent immediate reads through fresh clients.

This is sufficient to close PR13.1's short-term identity question. It is not sufficient to promote generated-artifact download support because longitudinal/session stability and an identity-bound resolution surface remain unproven.

## Relationship to PR10.1

PR10.1 correctly froze generated-artifact download handoff as:

```text
ARTIFACT_DOWNLOAD_HANDOFF_UNSUPPORTED_WITHOUT_STABLE_PRODUCT_IDENTITY
```

PR13.1 does not change that support status. It has now established two narrower facts: the product exposes an explicit conversation-scoped `file_id` for the generated artifact, and that identity is stable across two independent immediate reads. Resolution-path research belongs to a separate follow-up gate.

## Running R1

From an exact clean checkout with an authenticated CWA session available:

```bash
python tools/pr13_1_conversation_files_live_gate.py \
  --conversation "https://chatgpt.com/c/<conversation-id>" \
  --expected-head <exact-head-sha>
```

## Running R2

```bash
python tools/pr13_1_conversation_files_stability_gate.py \
  --conversation "https://chatgpt.com/c/<conversation-id>" \
  --expected-filename cwa_pr13_1_identity_probe.txt \
  --expected-head <exact-head-sha>
```

Both gates are characterization tools. Neither grants artifact resolution, download, filesystem-write, retry, or overwrite authority.
