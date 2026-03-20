import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from "react";
import { api, type OutputEntry, type RunSummary } from "./api";

interface PipelineState {
  running: boolean;
  output: OutputEntry[];
  runSummary: RunSummary | null;
  progress: number;
  addOutput: (type: OutputEntry["type"], message: string) => void;
  clearOutput: () => void;
  setRunSummary: (summary: RunSummary | null) => void;
  runTransformAll: (force?: boolean) => Promise<void>;
  runStream: (name: string, force?: boolean) => Promise<void>;
  cancelPipeline: () => Promise<void>;
  runLint: (fix?: boolean) => Promise<void>;
  runCurrentScript: (scriptPath: string) => Promise<void>;
  runSingleModel: (modelName: string) => Promise<void>;
  runContracts: () => Promise<void>;
}

const PipelineContext = createContext<PipelineState | null>(null);

interface PipelineProviderProps {
  children: React.ReactNode;
  onTablesChanged: () => void;
  onPipelineComplete?: () => void;
}

interface EventProcessor {
  (event: string, data: Record<string, unknown>): void;
  gotComplete: () => boolean;
}

/**
 * Process SSE events from the pipeline event stream.
 * Shared between runStream and reconnect-on-mount.
 */
function createEventProcessor(
  addOutput: (type: OutputEntry["type"], message: string, serverTs?: number) => void,
  setProgress: React.Dispatch<React.SetStateAction<number>>,
  setRunning: React.Dispatch<React.SetStateAction<boolean>>,
  setRunSummary: React.Dispatch<React.SetStateAction<RunSummary | null>>,
  onTablesChanged: () => void,
  onPipelineComplete?: () => void,
  firstLineMsgRef?: React.MutableRefObject<string>,
  resolve?: () => void,
) {
  const models: { name: string; result: string }[] = [];
  let totalRows = 0;
  let hasError = false;
  let totalItems = 0;
  let completedItems = 0;
  let gotComplete = false;
  const nodeStartTimes: Record<string, number> = {};

  const processor = (event: string, data: Record<string, unknown>) => {
    const serverTs = data.ts as number | undefined;
    switch (event) {
      case "start": {
        totalItems = (data.total as number) || 0;
        const opLabel = data.label as string || data.stream as string || "Running";
        addOutput("info", `${opLabel}...`, serverTs);
        if (firstLineMsgRef) firstLineMsgRef.current = `${opLabel}...`;
        break;
      }
      case "step_start":
        break;
      case "model_start": {
        const action = data.action as string;
        const mName = data.name as string;
        const num = data.num as number || 0;
        const verb = action === "ingest" ? "Ingesting" : action === "export" ? "Exporting" : "Building";
        const prefix = totalItems && num ? `(${num}/${totalItems}) ` : "";
        nodeStartTimes[mName] = Date.now();
        const displayName = (action === "ingest" || action === "export") && !mName.includes("/") ? `${action}/${mName}` : mName;
        addOutput("log", `${prefix}${verb} ${displayName}...`, serverTs);
        break;
      }
      case "model_end": {
        completedItems++;
        if (totalItems > 0) setProgress(completedItems / totalItems);
        const status = data.status as string;
        const mName = data.name as string;
        const action = data.action as string;
        const dur = data.duration_ms as number | undefined;
        const rows = data.row_count as number | undefined;
        const rowsAff = data.rows_affected as number | undefined;
        const err = data.error as string | undefined;
        const num = data.num as number || 0;

        const prefix = totalItems && num ? `(${num}/${totalItems}) ` : "";
        let msg = "";
        const wallMs = nodeStartTimes[mName] ? Date.now() - nodeStartTimes[mName] : dur;
        const durVal = wallMs || dur || 0;
        const durStr = durVal ? `(${(durVal / 1000).toFixed(1)}s)` : "";
        const verb = action === "ingest" ? "Ingested" : action === "export" ? "Exported" : "Built";
        const rowCount = rows || rowsAff || 0;
        const rowStr = rowCount ? `${rowCount.toLocaleString()} rows` : "";
        const details = [rowStr, durStr].filter(Boolean).join(" ");
        const displayName = (action === "ingest" || action === "export") && !mName.includes("/") ? `${action}/${mName}` : mName;

        if (status === "skipped") {
          msg = `${prefix}Skipped ${displayName} (no changes)`;
        } else if (status === "error" || status === "assertion_failed") {
          const cleanErr = err?.replace(/[\x00]/g, ".").replace(/\.+/g, ".") || "";
          msg = `${prefix}Failed ${displayName}${cleanErr ? ` — ${cleanErr}` : ""}`;
        } else {
          msg = `${prefix}${verb} ${displayName}${details ? ` — ${details}` : ""}`;
        }

        if (rowCount) totalRows += rowCount;
        const level = status === "error" || status === "assertion_failed" ? "error"
          : status === "skipped" ? "log"
          : "success";
        addOutput(level as OutputEntry["type"], msg, serverTs);

        if (status !== "skipped") {
          models.push({ name: mName, result: status });
        }
        if (status === "error" || status === "assertion_failed") hasError = true;
        break;
      }
      case "lint_violation": {
        const file = data.file as string;
        const line = data.line as number;
        const col = data.col as number;
        const code = data.code as string;
        const desc = data.description as string;
        const fixable = data.fixable as boolean;
        const tag = fixable === false ? " (unfixable)" : "";
        addOutput("warn", `${file}:${line}:${col} [${code}] ${desc}${tag}`, serverTs);
        break;
      }
      case "contract_result": {
        const cr = data as Record<string, unknown>;
        const contractName = cr.contract_name as string;
        const model = cr.model as string;
        const passed = cr.passed as boolean;
        const assertions = (cr.assertions || []) as { expression: string; passed: boolean; detail: string }[];
        const ruleCount = assertions.length;
        const failedCount = assertions.filter(a => !a.passed).length;
        const durMs = cr.duration_ms as number;
        const crError = cr.error as string | undefined;

        if (crError && ruleCount === 0) {
          addOutput("warn", `SKIP  ${contractName} on ${model} — ${crError}`, serverTs);
        } else if (passed) {
          addOutput("info", `PASS  ${contractName} on ${model} -- ${ruleCount} rule${ruleCount !== 1 ? "s" : ""} passed [${durMs}ms]`, serverTs);
        } else {
          addOutput("error", `FAIL  ${contractName} on ${model} -- ${failedCount} of ${ruleCount} rule${ruleCount !== 1 ? "s" : ""} failed [${durMs}ms]`, serverTs);
          for (const a of assertions) {
            if (!a.passed) {
              addOutput("error", `       ${a.expression}  -->  ${a.detail || "failed"}`, serverTs);
            }
          }
          if (crError) addOutput("error", `       Error: ${crError}`, serverTs);
        }
        if (!passed) hasError = true;
        break;
      }
      case "script_output": {
        addOutput("log", data.line as string, serverTs);
        break;
      }
      case "complete": {
        gotComplete = true;
        const durS = (data.duration_seconds as number) || 0;
        const pipelineStatus = data.status as string;
        const op = (data.operation as string) || "stream";
        const isCancelled = pipelineStatus === "cancelled";
        const level = isCancelled ? "warn" : pipelineStatus === "failed" || pipelineStatus === "error" ? "error" : "info";

        // Operation-specific completion messages
        if (op === "lint") {
          const lintErr = data.error as string | undefined;
          if (lintErr) {
            addOutput("error", lintErr, serverTs);
          } else {
            const count = data.count as number || 0;
            const fixed = data.fixed as number || 0;
            const isFix = data.fix as boolean;
            if (isFix) {
              const parts: string[] = [];
              if (fixed > 0) parts.push(`${fixed} fixed`);
              if (count > 0) parts.push(`${count} violation(s) remain (unfixable by SQLFluff)`);
              addOutput("info", parts.length > 0 ? parts.join(", ") + "." : "All fixable violations resolved.", serverTs);
            } else {
              addOutput("info", count === 0 ? "No lint violations found." : `${count} violation(s) found.`, serverTs);
            }
          }
          addOutput(level as OutputEntry["type"], `Lint completed in ${durS}s`, serverTs);
        } else if (op === "contracts") {
          const contractErr = data.error as string | undefined;
          if (contractErr) {
            addOutput("error", contractErr, serverTs);
          } else {
            const total = data.total as number || 0;
            const passed = data.passed as number || 0;
            const failed = data.failed as number || 0;
            if (total === 0) {
              addOutput("warn", "No contracts found. Create YAML files in contracts/ to get started.", serverTs);
            } else if (failed === 0) {
              addOutput("info", `Done: ${passed} contract${passed !== 1 ? "s" : ""} passed`, serverTs);
            } else {
              addOutput("error", `Done: ${failed} contract${failed !== 1 ? "s" : ""} failed, ${passed} passed`, serverTs);
            }
          }
          addOutput(level as OutputEntry["type"], `Contracts completed in ${durS}s`, serverTs);
        } else if (op === "script") {
          const scriptPath = data.script_path as string || "";
          const scriptErr = data.error as string;
          if (scriptErr) addOutput("error", scriptErr, serverTs);
          addOutput(level as OutputEntry["type"], `${scriptPath} ${pipelineStatus} in ${durS}s`, serverTs);
        } else {
          const genericErr = data.error as string | undefined;
          if (genericErr) addOutput("error", genericErr, serverTs);
          const preposition = isCancelled ? "after" : "in";
          const opLabel = op === "transform" ? "Transform" : "Pipeline";
          addOutput(level as OutputEntry["type"], `${opLabel} ${pipelineStatus} ${preposition} ${durS}s`, serverTs);
        }

        setProgress(1);
        if (firstLineMsgRef) firstLineMsgRef.current = "";
        if (op === "transform" || op === "stream") onTablesChanged();

        const summaryType = (op === "stream" || op === "transform" || op === "lint" || op === "script" || op === "contracts") ? op : "stream";
        const summary: RunSummary = {
          type: summaryType as RunSummary["type"],
          status: isCancelled ? "failed" : hasError ? "failed" : pipelineStatus === "failed" ? "failed" : "success",
          models,
          totalRows,
          duration: Math.round(durS * 1000),
          errors: models.filter((m) => m.result === "error").length,
        };
        setRunSummary(summary);
        if (!hasError && !isCancelled) onPipelineComplete?.();
        setRunning(false);
        resolve?.();
        break;
      }
      case "error": {
        const errMsg = data.message as string;
        if (firstLineMsgRef) firstLineMsgRef.current = "";
        if (errMsg && (errMsg.includes("network error") || errMsg.includes("Failed to fetch") || errMsg.includes("AbortError"))) {
          addOutput("warn", "Connection to server lost. The server may have been restarted.");
        } else {
          addOutput("error", errMsg || "An unknown error occurred");
        }
        setRunning(false);
        resolve?.();
        break;
      }
    }
  };

  // Expose gotComplete flag for post-stream checking
  (processor as EventProcessor).gotComplete = () => gotComplete;
  return processor as EventProcessor;
}

export function PipelineProvider({ children, onTablesChanged, onPipelineComplete }: PipelineProviderProps) {
  const [running, setRunning] = useState(false);
  const [output, setOutput] = useState<OutputEntry[]>(() => {
    try {
      const saved = sessionStorage.getItem('havn_pipeline_output');
      return saved ? JSON.parse(saved) : [];
    } catch { return []; }
  });
  const [runSummary, setRunSummary] = useState<RunSummary | null>(() => {
    try {
      const saved = sessionStorage.getItem('havn_run_summary');
      return saved ? JSON.parse(saved) : null;
    } catch { return null; }
  });
  const [progress, setProgress] = useState(0);

  const elapsedRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef<number>(0);
  const firstLineMsgRef = useRef<string>("");

  // Persist output to sessionStorage
  const outputRef = useRef(output);
  outputRef.current = output;
  const runSummaryRef = useRef(runSummary);
  runSummaryRef.current = runSummary;

  useEffect(() => {
    try {
      const toSave = output.length > 500 ? output.slice(-500) : output;
      sessionStorage.setItem('havn_pipeline_output', JSON.stringify(toSave));
    } catch { /* storage full - ignore */ }
  }, [output]);

  // Persist runSummary to sessionStorage
  useEffect(() => {
    try {
      if (runSummary) sessionStorage.setItem('havn_run_summary', JSON.stringify(runSummary));
      else sessionStorage.removeItem('havn_run_summary');
    } catch { /* ignore */ }
  }, [runSummary]);

  // Force-persist on page unload (useEffect may not fire before unload)
  useEffect(() => {
    const handler = () => {
      try {
        const toSave = outputRef.current.length > 500 ? outputRef.current.slice(-500) : outputRef.current;
        sessionStorage.setItem('havn_pipeline_output', JSON.stringify(toSave));
        if (runSummaryRef.current) sessionStorage.setItem('havn_run_summary', JSON.stringify(runSummaryRef.current));
      } catch { /* ignore */ }
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, []);

  // Clear output if server has restarted (boot time changed)
  const bootCheckDone = useRef(false);
  useEffect(() => {
    if (bootCheckDone.current) return;
    bootCheckDone.current = true;
    fetch("/api/health").then(r => r.json()).then(d => {
      const currentBoot = String(d.boot || "");
      const lastBoot = sessionStorage.getItem("havn_server_boot");
      if (currentBoot) sessionStorage.setItem("havn_server_boot", currentBoot);
      if (lastBoot && lastBoot !== currentBoot) {
        // Server restarted — clear all persisted output
        setOutput([]);
        setRunSummary(null);
        sessionStorage.removeItem("havn_pipeline_output");
        sessionStorage.removeItem("havn_run_summary");
      }
    }).catch(() => {});
  }, []);

  // Reconnect to active operation on mount (handles page refresh)
  const reconnectAttempted = useRef(false);
  useEffect(() => {
    if (reconnectAttempted.current) return;
    reconnectAttempted.current = true;

    api.getActiveStream().then((data) => {
      // If no operation is running or has finished with events, nothing to do
      if (!data.running && !(data.finished && data.total_events > 0)) return;

      // If operation already finished and we have saved output, keep it
      if (!data.running && data.finished && output.length > 0) return;

      if (data.running) {
        setRunning(true);
      }
      setRunSummary(null);

      // Clear existing output -- the fresh replay from the server replaces it
      setOutput([]);
      sessionStorage.removeItem('havn_pipeline_output');
      sessionStorage.removeItem('havn_run_summary');

      // No "Reconnecting" message -- seamless replay
      const processor = createEventProcessor(
        addOutput,
        setProgress,
        setRunning,
        setRunSummary,
        onTablesChanged,
        onPipelineComplete,
      );

      // Connect from event 0 -- replay the entire buffer
      const { done } = api.connectToStreamEvents(0, processor);

      // After SSE stream ends, check if we got the complete event.
      // If not, the pipeline may have finished while we were disconnected.
      done.then(() => {
        if (!processor.gotComplete()) {
          api.getActiveStream().then((active) => {
            if (active.finished && !active.running) {
              // Pipeline finished but we missed the complete event — synthesize it
              const durS = active.duration_seconds ?? 0;
              const status = active.status ?? "success";
              processor("complete", {
                operation: active.operation ?? "stream",
                stream: active.stream_name,
                status,
                duration_seconds: durS,
                ts: Date.now() / 1000,
              });
            }
          }).catch(() => { /* ignore */ });
        }
      });
    }).catch(() => {
      // Server not reachable or auth issue -- ignore
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Elapsed timer: update the first output line every second while running
  useEffect(() => {
    if (running && firstLineMsgRef.current) {
      startTimeRef.current = Date.now();
      elapsedRef.current = setInterval(() => {
        const elapsed = Math.floor((Date.now() - startTimeRef.current) / 1000);
        setOutput((prev) => {
          if (prev.length === 0) return prev;
          const first = prev[0];
          const updated = { ...first, message: `${firstLineMsgRef.current} ${elapsed}s` };
          return [updated, ...prev.slice(1)];
        });
      }, 1000);
    }
    return () => {
      if (elapsedRef.current) {
        clearInterval(elapsedRef.current);
        elapsedRef.current = null;
      }
    };
  }, [running]);

  const addOutput = useCallback((type: OutputEntry["type"], message: string, serverTs?: number) => {
    const ts = serverTs
      ? new Date(serverTs * 1000).toLocaleTimeString()
      : new Date().toLocaleTimeString();
    setOutput((prev) => [...prev, { type, message, ts }]);
  }, []);

  const clearOutput = useCallback(() => {
    setOutput([]);
    sessionStorage.removeItem('havn_pipeline_output');
    sessionStorage.removeItem('havn_run_summary');
  }, []);

  // Shared helper: start a background operation and connect to SSE
  const startAndConnect = useCallback((
    startFn: () => Promise<{ status: string }>,
    _label: string,
  ): Promise<void> => {
    setRunning(true);
    setRunSummary(null);
    setProgress(0);

    return new Promise<void>(async (resolve) => {
      try {
        const startResult = await startFn();
        if (startResult.status === "already_running") {
          addOutput("warn", "An operation is already running.");
          setRunning(false);
          resolve();
          return;
        }

        const processor = createEventProcessor(
          addOutput, setProgress, setRunning, setRunSummary,
          onTablesChanged, onPipelineComplete, firstLineMsgRef, resolve,
        );

        const { done } = api.connectToStreamEvents(0, processor);
        done.then(() => {
          if (!processor.gotComplete()) {
            api.getActiveStream().then((active) => {
              if (active.finished && !active.running) {
                processor("complete", {
                  operation: active.operation ?? "stream",
                  stream: active.stream_name,
                  status: active.status ?? "success",
                  duration_seconds: active.duration_seconds ?? 0,
                  ts: Date.now() / 1000,
                });
              }
            }).catch(() => { setRunning(false); resolve(); });
          }
        });
      } catch (e: unknown) {
        addOutput("error", (e as Error).message);
        firstLineMsgRef.current = "";
        setRunning(false);
        resolve();
      }
    });
  }, [addOutput, onTablesChanged, onPipelineComplete]);

  const runTransformAll = useCallback((force: boolean = false) =>
    startAndConnect(
      () => api.startTransform(null, force),
      `Running transform${force ? " (force)" : ""}...`,
    ),
  [startAndConnect]);

  const runStream = useCallback((name: string, force: boolean = false) =>
    startAndConnect(
      () => api.startStream(name, force),
      `Running pipeline ${name}${force ? " (full refresh)" : ""}...`,
    ),
  [startAndConnect]);

  const cancelPipeline = useCallback(async () => {
    try {
      await api.cancelStream();
      addOutput("warn", "Cancellation requested...");
    } catch (e: unknown) {
      addOutput("error", (e as Error).message);
    }
  }, [addOutput]);

  const runLint = useCallback((fix: boolean = false) =>
    startAndConnect(
      () => api.startLint(fix),
      fix ? "Running SQLFluff lint --fix..." : "Running SQLFluff lint...",
    ),
  [startAndConnect]);

  const runCurrentScript = useCallback((scriptPath: string) =>
    startAndConnect(
      () => api.startScript(scriptPath),
      `Running script ${scriptPath}...`,
    ),
  [startAndConnect]);

  const runSingleModel = useCallback((modelName: string) =>
    startAndConnect(
      () => api.startTransform([modelName], true),
      `Running transform for ${modelName}...`,
    ),
  [startAndConnect]);

  const runContracts = useCallback(() =>
    startAndConnect(
      () => api.startContracts(),
      "Running data contracts...",
    ),
  [startAndConnect]);

  return (
    <PipelineContext.Provider
      value={{
        running: !!running,
        output,
        runSummary,
        progress,
        addOutput,
        clearOutput,
        setRunSummary,
        runTransformAll,
        runStream,
        cancelPipeline,
        runLint,
        runCurrentScript,
        runSingleModel,
        runContracts,
      }}
    >
      {children}
    </PipelineContext.Provider>
  );
}

export function usePipeline(): PipelineState {
  const ctx = useContext(PipelineContext);
  if (!ctx) throw new Error("usePipeline must be used within PipelineProvider");
  return ctx;
}
