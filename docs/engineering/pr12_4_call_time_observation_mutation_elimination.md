# PR12.4 — Call-Time Observation Mutation Elimination

PR12.4 closes the observation-topology cleanup deliberately deferred by PR12.3. Canonical source/citation observation is now composed into the browser-native client when its functions are defined instead of being installed lazily by the first observed product execution.

## Static ownership

`browser_native_client.py` owns the two canonical observation composition points:

- `_wait_for_new_final_assistant` is wrapped by `_gate_wait_for_new_final_assistant` at definition time;
- `send_browser_native` is wrapped by `_gate_send_browser_native` at definition time.

Modules and classes that import `send_browser_native` therefore receive the already-composed function. `product_runtime_observation_gate.py` only collects the standardized event stream and no longer imports or invokes the historical compatibility installer while a turn is running.

The historical `install_canonical_product_observation_gate()` entry point remains for compatibility. Because the intrinsic browser-native functions are already marked and composed, invoking that installer is identity-stable and does not replace the active functions.

## Runtime topology invariant

An observed execution must not change the identities of:

- `browser_native_client._wait_for_new_final_assistant`;
- `browser_native_client.send_browser_native`;
- `browser_owned_write_runtime.send_browser_native`;
- `ChatGPTWebClient.send_browser_native`.

PR12.4 adds a regression test for this invariant and also proves that the runtime observation gate contains no lazy canonical installer call.

## Preserved contracts

PR12.4 does not change:

- Browser Authority or lease semantics;
- submission acceptance or canonical-finality authority;
- automatic retry/replay policy after ambiguous writes;
- ordinary-text conversation identity authority;
- Temporary Chat or rich-input behavior;
- source/citation normalization, privacy filtering, or stream/canonical deduplication;
- observation/approval separation;
- browserless support or transport semantics.

The change is topology-only: canonical observation behavior remains the same, but its ownership is explicit and stable before any product turn executes.
