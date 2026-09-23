// PR12.0 observation-domain assembly.
//
// Terminal-outcome extraction augments the existing safe response metadata before
// connector characterization becomes the terminal executeNativeTurn wrapper.
// UI liveness then wraps Native Messaging observation after that turn surface is
// assembled and grants no write, retry, or canonical-finality authority.

importScripts("service_worker_terminal_outcome.js");
importScripts("service_worker_connector_support_pr10_0.js");
importScripts("service_worker_ui_liveness.js");
