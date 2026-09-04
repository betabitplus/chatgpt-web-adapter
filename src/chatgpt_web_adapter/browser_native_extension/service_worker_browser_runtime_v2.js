// Browser runtime v2 production entrypoint.
//
// A distinct top-level worker URL is intentional: Chromium may retain compiled
// importScripts() children across unpacked-extension restarts. Loading the stable
// PR12 runtime first and the passive stream observer second makes observation the
// terminal layer while forcing deterministic activation of this runtime revision.

importScripts("service_worker_runtime.js");
importScripts("service_worker_passive_stream_observer.js");
