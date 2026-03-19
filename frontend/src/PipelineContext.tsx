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

  // Reconnect to active pipeline on mount (handles page refresh)
  const reconnectAttempted = useRef(false);
  useEffect(() => {
    if (reconnectAttempted.current) return;
    reconnectAttempted.current = true;

    api.getActiveStream().then((data) => {
      if (!data.running) return;

      setRunning(true);
      setRunSummary(null);
      const firstMsg = "Reconnecting to running pipeline...";
      firstLineMsgRef.current = firstMsg;
      setOutput((prev) => {
        // Keep existing output from sessionStorage, add reconnect message
        return [...prev, { type: "info" as const, message: firstMsg, ts: new Date().toLocaleTimeString() }];
      });

      // Skip replaying old events — sessionStorage already has them.
      // Use the server's buffered count to start from the current position.
      const fromEvent = (data as any).buffered_events || 0;

      const models: { name: string; result: string }[] = [];
      let totalRows = 0;
      let hasError = false;
      let totalItems = 0;
      let completedItems = 0;

      api.reconnectStreamSSE(fromEvent, (event, data) => {
        switch (event) {
          case "start":
            totalItems = (data.total as number) || 0;
            break;
          case "model_start": {
            const action = data.action as string;
            const mName = data.name as string;
            const num = data.num as number || 0;
            const verb = action === "ingest" ? "Ingesting" : action === "export" ? "Exporting" : "Building";
            const prefix = totalItems && num ? `(${num}/${totalItems}) ` : "";
            const displayName = (action === "ingest" || action === "export") && !mName.includes("/") ? `${action}/${mName}` : mName;
            addOutput("log", `${prefix}${verb} ${displayName}...`);
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
            const verb = action === "ingest" ? "Ingested" : action === "export" ? "Exported" : "Built";
            const rowCount = rows || rowsAff || 0;
            const durStr = dur ? `(${(dur / 1000).toFixed(1)}s)` : "";
            const rowStr = rowCount ? `${rowCount.toLocaleString()} rows` : "";
            const details = [rowStr, durStr].filter(Boolean).join(" ");
            const displayName = (action === "ingest" || action === "export") && !mName.includes("/") ? `${action}/${mName}` : mName;

            let msg = "";
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
            addOutput(level as OutputEntry["type"], msg);

            if (status !== "skipped") {
              models.push({ name: mName, result: status });
            }
            if (status === "error" || status === "assertion_failed") hasError = true;
            break;
          }
          case "complete": {
            const durS = (data.duration_seconds as number) || 0;
            const pipelineStatus = data.status as string;
            const isCancelled = pipelineStatus === "cancelled";
            const level = isCancelled ? "warn" : "info";
            addOutput(level as OutputEntry["type"], `Pipeline ${pipelineStatus} in ${durS}s`);
            setProgress(1);
            firstLineMsgRef.current = "";
            onTablesChanged();

            const summary: RunSummary = {
              type: "stream",
              status: isCancelled ? "failed" : hasError ? "failed" : "success",
              models,
              totalRows,
              duration: Math.round(durS * 1000),
              errors: models.filter((m) => m.result === "error").length,
            };
            setRunSummary(summary);
            if (!hasError && !isCancelled) onPipelineComplete?.();
            setRunning(false);
            break;
          }
          case "error": {
            const errMsg = data.message as string;
            firstLineMsgRef.current = "";
            // 404 means pipeline finished between our check and reconnect — not an error
            if (errMsg && errMsg.includes("404")) {
              setRunning(false);
              return;
            }
            if (errMsg && (errMsg.includes("network error") || errMsg.includes("Failed to fetch"))) {
              addOutput("warn", "Connection to server lost.");
            } else {
              addOutput("error", errMsg || "An unknown error occurred");
            }
            setRunning(false);
            break;
          }
        }
      });
    }).catch(() => {
      // Server not reachable or auth issue — ignore
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

  const addOutput = useCallback((type: OutputEntry["type"], message: string) => {
    const ts = new Date().toLocaleTimeString();
    setOutput((prev) => [...prev, { type, message, ts }]);
  }, []);

  const clearOutput = useCallback(() => {
    setOutput([]);
    sessionStorage.removeItem('havn_pipeline_output');
    sessionStorage.removeItem('havn_run_summary');
  }, []);

  const runTransformAll = useCallback(async (force: boolean = false) => {
    setRunning(true);
    setRunSummary(null);
    const firstMsg = `Running transform (force=${force})...`;
    firstLineMsgRef.current = firstMsg;
    addOutput("info", firstMsg);
    try {
      const data = await api.runTransform(null, force);
      const models: { name: string; result: string }[] = [];
      for (const [model, status] of Object.entries(data.results || {})) {
        addOutput(status === "error" ? "error" : "info", `${model}: ${status}`);
        models.push({ name: model, result: status });
      }
      onTablesChanged();

      const summary: RunSummary = {
        type: "transform",
        status: models.some((m) => m.result === "error") ? "failed" : "success",
        models,
        totalRows: 0,
        duration: 0,
        errors: models.filter((m) => m.result === "error").length,
      };
      setRunSummary(summary);
      if (summary.status === "success") onPipelineComplete?.();
    } catch (e: unknown) {
      addOutput("error", (e as Error).message);
    } finally {
      firstLineMsgRef.current = "";
      setRunning(false);
    }
  }, [addOutput, onTablesChanged, onPipelineComplete]);

  const runStream = useCallback(async (name: string, force: boolean = false) => {
    setRunning(true);
    setRunSummary(null);
    setProgress(0);
    const firstMsg = `Running pipeline${force ? " (full refresh)" : ""}...`;
    firstLineMsgRef.current = firstMsg;
    addOutput("info", firstMsg);

    const models: { name: string; result: string }[] = [];
    let totalRows = 0;
    let hasError = false;
    let totalItems = 0;
    let completedItems = 0;
    const nodeStartTimes: Record<string, number> = {};

    return new Promise<void>((resolve) => {
      const { abort } = api.runStreamSSE(name, force, (event, data) => {
        switch (event) {
          case "start":
            totalItems = (data.total as number) || 0;
            break;
          case "step_start":
            break;
          case "model_start": {
            const action = data.action as string;
            const mName = data.name as string;
            const num = data.num as number || 0;
            const verb = action === "ingest" ? "Ingesting" : action === "export" ? "Exporting" : "Building";
            const prefix = totalItems && num ? `(${num}/${totalItems}) ` : "";
            nodeStartTimes[mName] = Date.now();
            // Use full path for ingest/export scripts so they're clickable in output
            const displayName = (action === "ingest" || action === "export") && !mName.includes("/") ? `${action}/${mName}` : mName;
            addOutput("log", `${prefix}${verb} ${displayName}...`);
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
            // Use wall-clock time from when "Building..." was shown
            const wallMs = nodeStartTimes[mName] ? Date.now() - nodeStartTimes[mName] : dur;
            const durVal = wallMs || dur || 0;
            const durStr = durVal ? `(${(durVal / 1000).toFixed(1)}s)` : "";
            const verb = action === "ingest" ? "Ingested" : action === "export" ? "Exported" : "Built";
            const rowCount = rows || rowsAff || 0;
            const rowStr = rowCount ? `${rowCount.toLocaleString()} rows` : "";
            const details = [rowStr, durStr].filter(Boolean).join(" ");
            // Use full path for ingest/export scripts so they're clickable in output
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
            addOutput(level as OutputEntry["type"], msg);

            if (status !== "skipped") {
              models.push({ name: mName, result: status });
            }
            if (status === "error" || status === "assertion_failed") hasError = true;
            break;
          }
          case "complete": {
            const durS = (data.duration_seconds as number) || 0;
            const pipelineStatus = data.status as string;
            const isCancelled = pipelineStatus === "cancelled";
            const level = isCancelled ? "warn" : "info";
            addOutput(level as OutputEntry["type"], `Pipeline ${pipelineStatus} in ${durS}s`);
            setProgress(1);
            firstLineMsgRef.current = "";
            onTablesChanged();

            const summary: RunSummary = {
              type: "stream",
              status: isCancelled ? "failed" : hasError ? "failed" : "success",
              models,
              totalRows,
              duration: Math.round(durS * 1000),
              errors: models.filter((m) => m.result === "error").length,
            };
            setRunSummary(summary);
            if (!hasError && !isCancelled) onPipelineComplete?.();
            setRunning(false);
            resolve();
            break;
          }
          case "error": {
            const errMsg = data.message as string;
            firstLineMsgRef.current = "";
            if (errMsg && (errMsg.includes("network error") || errMsg.includes("Failed to fetch") || errMsg.includes("AbortError"))) {
              addOutput("warn", "Connection to server lost. The server may have been restarted.");
            } else {
              addOutput("error", errMsg || "An unknown error occurred");
            }
            setRunning(false);
            resolve();
            break;
          }
        }
      });

      // Store abort for potential cancellation
      void abort;
    });
  }, [addOutput, onTablesChanged, onPipelineComplete]);

  const cancelPipeline = useCallback(async () => {
    try {
      await api.cancelStream();
      addOutput("warn", "Cancellation requested...");
    } catch (e: unknown) {
      addOutput("error", (e as Error).message);
    }
  }, [addOutput]);

  const runLint = useCallback(async (fix: boolean = false) => {
    setRunning(true);
    addOutput("info", fix ? "Fixing SQL..." : "Linting SQL...");
    try {
      const data = await api.runLint(fix);
      for (const v of data.violations || []) {
        const tag = fix && !v.fixable ? " (unfixable)" : "";
        addOutput("warn", `${v.file}:${v.line}:${v.col} [${v.code}] ${v.description}${tag}`);
      }
      if (fix) {
        const fixed = data.fixed ?? 0;
        const remaining = data.count;
        const parts: string[] = [];
        if (fixed > 0) parts.push(`${fixed} fixed`);
        if (remaining > 0) parts.push(`${remaining} violation(s) remain (unfixable by SQLFluff)`);
        addOutput("info", parts.length > 0 ? parts.join(", ") + "." : "All fixable violations resolved.");
      } else {
        addOutput("info", data.count === 0 ? "No lint violations found." : `${data.count} violation(s) found.`);
      }
    } catch (e: unknown) {
      addOutput("error", (e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [addOutput]);

  const runCurrentScript = useCallback(async (scriptPath: string) => {
    setRunning(true);
    addOutput("info", `Running ${scriptPath}...`);
    try {
      const data = await api.runScript(scriptPath);
      addOutput(data.status === "error" ? "error" : "info", `${scriptPath}: ${data.status} (${data.duration_ms}ms)`);
      if (data.log_output) data.log_output.split("\n").filter((l: string) => l.trim()).forEach((l: string) => addOutput("log", l));
      if (data.error) addOutput("error", data.error);
    } catch (e: unknown) {
      addOutput("error", (e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [addOutput]);

  const runSingleModel = useCallback(async (modelName: string) => {
    setRunning(true);
    setRunSummary(null);
    addOutput("info", `Running transform for ${modelName}...`);
    try {
      const data = await api.runTransform([modelName], true);
      const models: { name: string; result: string }[] = [];
      for (const [model, status] of Object.entries(data.results || {})) {
        addOutput(status === "error" ? "error" : "info", `${model}: ${status}`);
        models.push({ name: model, result: status });
      }
      onTablesChanged();
      setRunSummary({
        type: "transform",
        status: models.some((m) => m.result === "error") ? "failed" : "success",
        models,
        totalRows: 0,
        duration: 0,
        errors: models.filter((m) => m.result === "error").length,
      });
    } catch (e: unknown) {
      addOutput("error", (e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [addOutput, onTablesChanged]);

  const runContracts = useCallback(async () => {
    setRunning(true);
    addOutput("info", "Running contracts...");
    try {
      const data = await api.runContracts() as {
        total: number;
        passed: number;
        failed: number;
        results: {
          contract_name: string;
          model: string;
          passed: boolean;
          severity: string;
          duration_ms: number;
          error?: string;
          assertions: { expression: string; passed: boolean; detail: string }[];
        }[];
      };

      if (data.total === 0) {
        addOutput("warn", "No contracts found. Create YAML files in contracts/ to get started.");
        return;
      }

      for (const cr of data.results) {
        const ruleCount = (cr.assertions || []).length;
        const failedCount = (cr.assertions || []).filter(a => !a.passed).length;
        const level = cr.passed ? "info" : "error";

        if (cr.passed) {
          addOutput(level as OutputEntry["type"], `PASS  ${cr.contract_name} on ${cr.model} -- ${ruleCount} rule${ruleCount !== 1 ? "s" : ""} passed [${cr.duration_ms}ms]`);
        } else {
          addOutput(level as OutputEntry["type"], `FAIL  ${cr.contract_name} on ${cr.model} -- ${failedCount} of ${ruleCount} rule${ruleCount !== 1 ? "s" : ""} failed [${cr.duration_ms}ms]`);
          for (const a of cr.assertions || []) {
            if (!a.passed) {
              addOutput("error", `       ${a.expression}  -->  ${a.detail || "failed"}`);
            }
          }
        }
        if (cr.error) addOutput("error", `       Error: ${cr.error}`);
      }

      addOutput("info", "");
      const totalRules = data.results.reduce((sum, cr) => sum + (cr.assertions || []).length, 0);
      if (data.failed === 0) {
        addOutput("info", `Done: ${data.passed} contract${data.passed !== 1 ? "s" : ""} passed (${totalRules} rules total)`);
      } else {
        addOutput("error", `Done: ${data.failed} contract${data.failed !== 1 ? "s" : ""} failed, ${data.passed} passed (${totalRules} rules total)`);
      }
    } catch (e: unknown) {
      addOutput("error", (e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [addOutput]);

  return (
    <PipelineContext.Provider
      value={{
        running,
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
