# PR13.3 — Identity-Bound Artifact Bytes & Integrity Probe

## Question

PR13.1 established an explicit product-owned `file_id` for one generated artifact and short-term stability across two independent reads. PR13.2 established that the real `file_id` resolves through a product-owned resolver while a deliberately changed identity is rejected.

PR13.3 asks one narrower remaining question:

> If we follow the locator returned for that exact product-owned identity, do we receive the exact artifact bytes we expect?

This is still a characterization experiment. It does not add production download/materialization support.

## Gate

The live gate performs at most three read-only requests:

1. `GET /backend-api/conversations/{conversation_id}/files` to discover the exact `file_id` for one caller-named generated artifact.
2. `GET /backend-api/files/download/{file_id}?conversation_id={conversation_id}&inline=false` to obtain the product resolver payload.
3. One GET against the returned locator, into memory only.

The third request is the first step in PR13.x that intentionally follows the returned locator.

## Integrity oracle

The caller supplies two independent expectations for the known fixture:

- exact byte size;
- exact SHA-256 digest.

A positive result therefore requires both:

```text
observed_size_bytes == expected_size_bytes
observed_sha256 == expected_sha256
```

The filename is used only to select one record from the conversation-scoped files collection. It is not used as identity and is not used to validate bytes.

For the PR13.1 text fixture created in the live ChatGPT conversation:

```text
filename = cwa_pr13_1_identity_probe.txt
size = 45
sha256 = d0bb354d72fad3715f5348740d75dd644435165f68034f54f7974c834cbe9f1d
```

## Locator policy

PR13.3 does not treat an arbitrary URL from a product response as safe to fetch.

Allowed locator classes are:

- `https://chatgpt.com/...`
- `https://*.oaiusercontent.com/...`
- `https://oaiusercontent.com/...`

Additional restrictions:

- HTTPS only;
- no URL userinfo;
- no fragment;
- no non-default port;
- no unrecognized origin;
- redirects are not followed.

Credential handling is origin-sensitive:

- same-origin `chatgpt.com` locator: use the authenticated CWA headers because current estuary content can require ChatGPT auth;
- `oaiusercontent.com`: send no ChatGPT Authorization or Cookie headers.

This prevents an observed locator from becoming a credential-forwarding primitive.

## Positive characterization

PR13.3 reports:

```text
IDENTITY_BOUND_ARTIFACT_BYTES_INTEGRITY_OBSERVED
```

only when:

- the target record has explicit product identity;
- the identity resolver returns an allowed locator;
- one locator GET returns HTTP 2xx;
- returned bytes exactly match both expected size and expected SHA-256.

A positive result may set:

```text
artifact_bytes_proven = true
artifact_integrity_proven = true
identity_bound_byte_retrieval_proven = true
```

but deliberately keeps:

```text
stable_product_identity_proven = false
download_authority_granted = false
artifact_disk_write_attempted = false
materialization_attempted = false
write_attempted = false
```

The distinction matters: proving that bytes can be fetched and match a known fixture does not yet define a safe public API, destination policy, overwrite semantics, retry authority, or longitudinal identity guarantee.

## Privacy boundary

The final report never exports:

- the real `file_id`;
- the raw locator or signed query parameters;
- Authorization/Cookie values;
- raw artifact bytes;
- raw resolver response bodies.

It may report the observed SHA-256 digest and byte count because those are integrity evidence, not locator or credential material.

The underlying generic CWA HTTP helper may use temporary transport/header files internally, but PR13.3 never writes the artifact body to a user-visible or persistent destination.

## Fail-closed outcomes

Separate characterizations are retained for:

- exact-head or tracked-clean failure;
- missing explicit target identity;
- resolver failure;
- missing resolver locator;
- rejected locator origin;
- locator redirect;
- locator HTTP failure;
- byte-size or SHA-256 mismatch.

There is no endpoint fallback and no locator-origin fallback.

## Relationship to the frozen PR10.1 status

Even a positive PR13.3 result does not directly modify:

```text
ARTIFACT_DOWNLOAD_HANDOFF_UNSUPPORTED_WITHOUT_STABLE_PRODUCT_IDENTITY
```

PR13.3 would remove the separate uncertainty around identity-bound byte retrieval and integrity. A later promotion step can then decide whether the accumulated PR13.1–PR13.3 evidence is sufficient to revise the product capability contract, or whether one longitudinal identity gate is still required first.

## Running the live gate

From an exact clean checkout:

```powershell
python tools/pr13_3_artifact_bytes_integrity_probe.py `
  --conversation "https://chatgpt.com/c/<conversation-id>" `
  --expected-filename cwa_pr13_1_identity_probe.txt `
  --expected-size 45 `
  --expected-sha256 d0bb354d72fad3715f5348740d75dd644435165f68034f54f7974c834cbe9f1d `
  --expected-head <exact-head-sha>
```
