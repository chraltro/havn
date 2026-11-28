// havn's SQL completion and hover providers are registered with Monaco when
// the Editor module loads (they share its schema caches). The editor page is
// its own chunk, so a Monaco editor elsewhere -- the model notebook opened
// from the DAG or Tables, the notebook panel -- had no SQL completion until
// the idle preload happened to fetch that chunk. Mounting one of those calls
// this to fetch it now. Monaco applies providers registered after an editor
// mounted to that editor as well.
let pending = null;

export function ensureSqlLanguageSupport(load = () => import("./Editor")) {
  if (!pending) {
    pending = Promise.resolve()
      .then(load)
      .catch(() => {
        // A failed fetch is tried again by the next mount.
        pending = null;
      });
  }
  return pending;
}

export function _resetSqlLanguageSupportForTests() {
  pending = null;
}
