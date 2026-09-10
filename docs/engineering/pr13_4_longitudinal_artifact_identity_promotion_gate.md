# PR13.4 — Longitudinal Artifact Identity Promotion Gate

## Question

PR13.1 proved explicit product-owned `file_id` identity and short-term stability across two independent fresh-client reads. PR13.2 proved that the exact identity is bound to the generated-file resolver. PR13.3 proved that the resolver returns the exact expected artifact bytes by both size and SHA-256.

One deliberate blocker remains before revising the frozen PR10.1 status:

```text
stable_product_identity_proven = false
```

PR13.4 asks whether the same product-owned identity observed in the authenticated PR13.1 R2 evidence remains the same after a materially later interval and still resolves to the same exact bytes.

## Baseline

The longitudinal baseline is the authenticated PR13.1 R2 evidence recorded by commit:

```text
eebd48cef2896a285331896741e3ba0225e5cc5f
```

That commit was created after the R2 live observation, so its commit timestamp is a conservative lower bound: if the current gate is at least four hours newer than the evidence commit, the actual interval since the R2 observation is at least as long.

The gate requires:

```text
MIN_LONGITUDINAL_AGE_SECONDS = 14400
```

and verifies that the baseline evidence commit is an ancestor of the current exact head.

## Private identity commitment

PR13.1 R2 intentionally did not commit the raw `file_id` or its SHA-256 fingerprint into repository evidence. PR13.4 preserves that privacy boundary.

The caller supplies the `first_identity_fingerprint` from the original authenticated R2 terminal output as:

```text
--baseline-identity-fingerprint <sha256>
```

The value is consumed only in memory. The report exports neither the expected fingerprint nor the currently observed fingerprint. It exposes only:

```text
identity_fingerprint_matches = true|false
```

The raw `file_id` is also never exported.

## Gate

After exact-head, tracked-clean, ancestry and elapsed-time checks, PR13.4 performs at most three authenticated/read-only product requests through one fresh client:

1. discover the target's current explicit `file_id` from `/backend-api/conversations/{conversation_id}/files`;
2. compare SHA-256 of that identity against the private PR13.1 R2 baseline commitment;
3. only on exact identity match, resolve the current `file_id` through the already-proven PR13.2 resolver and fetch its returned locator once into memory;
4. require the same known fixture bytes as PR13.3: 45 bytes and exact SHA-256 `d0bb354d72fad3715f5348740d75dd644435165f68034f54f7974c834cbe9f1d`.

The filename `cwa_pr13_1_identity_probe.txt` is used only to select the unique conversation-file record. It is not treated as identity and is not sufficient for promotion.

## Positive characterization

A positive result is:

```text
LONGITUDINAL_ARTIFACT_IDENTITY_PROMOTION_PROVEN
```

and requires all of the following:

- exact expected git head and tracked-clean checkout;
- baseline PR13.1 evidence commit is an ancestor;
- at least four hours have elapsed since that evidence commit;
- current explicit identity key is still `file_id`;
- current `file_id` SHA-256 matches the private R2 baseline fingerprint;
- resolver remains successful for that exact identity;
- returned locator remains within PR13.3's fail-closed origin policy;
- one locator GET returns the exact known bytes by both size and SHA-256.

Only then may the experiment report:

```text
longitudinal_identity_stability_proven = true
stable_product_identity_proven = true
stability_scope = KNOWN_GENERATED_ARTIFACT_ACROSS_AT_LEAST_4H
```

It deliberately retains:

```text
indefinite_identity_stability_proven = false
download_authority_granted = false
production_handoff_promoted = false
artifact_disk_write_attempted = false
materialization_attempted = false
write_attempted = false
```

The meaning is intentionally bounded: the known generated artifact has preserved the same product-owned identity across a multi-hour interval and that same identity still resolves to the same exact artifact. PR13.4 does not claim indefinite retention across arbitrary months, account migrations, deletion, expiry or product lifecycle changes.

## Authenticated live result — 2026-09-10

A clean exact-head run against the same saved conversation and generated text artifact used by PR13.1–PR13.3 returned:

```text
head_matches = true
tracked_clean = true
baseline_age_requirement_met = true
baseline_age_seconds = 19553
minimum_longitudinal_age_seconds = 14400
identity_discovery_characterization = EXPLICIT_PRODUCT_IDENTITY_CANDIDATES_OBSERVED
identity_key = file_id
identity_fingerprint_matches = true
target_record_present = true
resolution_status_code = 200
resolution_locator_field_present = true
resolution_locator_key = download_url
locator_origin_class = CHATGPT_SAME_ORIGIN
locator_fetch_status_code = 200
observed_size_bytes = 45
expected_size_bytes = 45
size_matches = true
observed_artifact_sha256 = d0bb354d72fad3715f5348740d75dd644435165f68034f54f7974c834cbe9f1d
expected_artifact_sha256 = d0bb354d72fad3715f5348740d75dd644435165f68034f54f7974c834cbe9f1d
sha256_matches = true
artifact_bytes_proven = true
artifact_integrity_proven = true
identity_bound_byte_retrieval_proven = true
longitudinal_identity_stability_proven = true
stable_product_identity_proven = true
stability_scope = KNOWN_GENERATED_ARTIFACT_ACROSS_AT_LEAST_4H
characterization = LONGITUDINAL_ARTIFACT_IDENTITY_PROMOTION_PROVEN
```

Request accounting remained bounded at exactly three reads through one fresh client: one identity discovery, one resolution request and one locator byte fetch.

The private baseline fingerprint, current raw `file_id`, locator/signed query, resolver response body and artifact bytes are intentionally not copied into repository evidence.

The gate also preserved the authority boundary:

```text
indefinite_identity_stability_proven = false
download_authority_granted = false
production_handoff_promoted = false
artifact_disk_write_attempted = false
materialization_attempted = false
write_attempted = false
identity_values_exported = false
locator_values_exported = false
artifact_bytes_exported = false
```

## Result

PR13.4 closes the longitudinal identity blocker positively for the bounded scope represented by this known generated artifact. The accumulated evidence chain is now:

```text
saved conversation
  -> explicit product-owned file_id
  -> same file_id across independent immediate reads
  -> same file_id fingerprint after at least 4 hours
  -> identity-bound product resolver
  -> locator-bearing resolution response
  -> locator byte fetch
  -> exact byte-count match
  -> exact SHA-256 match
```

Within that scope, `stable_product_identity_proven = true` is now supported by authenticated product evidence rather than inferred from short-term reads.

This does not claim indefinite retention or survival across deletion, account migration, lifecycle expiry or future product changes. Those are operational lifecycle questions, not blockers for promoting the proven generated-artifact handoff path into a governed production contract.

## Failure boundaries

The gate stops without resolver/byte requests if the longitudinal interval is too short, git evidence ancestry is invalid, the current target has no explicit identity, the identity key changed, or the current identity fingerprint differs from the R2 baseline.

A matching identity is still insufficient if current resolver or byte integrity fails. This avoids promoting a stable-but-dead identifier.

## Relationship to production handoff

The positive PR13.4 result closes the specific empirical blocker named by PR10.1: lack of stable product-owned generated-artifact identity.

It still does not itself add a public download API or local materialization behavior. The next milestone should be a production design/promotion PR defining governed destination selection, overwrite policy, atomic materialization, retry authority, integrity verification and failure semantics.

In other words, the PR13 research series should stop here. The next work should be the governed production artifact handoff.

## Running

From the exact clean PR13.4 checkout, use the same saved conversation and the private `first_identity_fingerprint` from the PR13.1 R2 terminal output:

```powershell
python tools/pr13_4_longitudinal_artifact_identity_promotion_gate.py `
  --conversation "https://chatgpt.com/c/<conversation-id>" `
  --baseline-identity-fingerprint <PR13.1-R2-first_identity_fingerprint> `
  --expected-head <exact-head-sha>
```

The fingerprint should not be pasted into GitHub issues, PR descriptions, documentation or committed files.
