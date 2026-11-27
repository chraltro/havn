import React, { createContext, useContext, useState, useCallback, useEffect, useRef } from "react";
import { api, type TableInfo, type FileEntry, type StreamConfig } from "./api";

interface WarehouseState {
  tables: TableInfo[];
  files: FileEntry[];
  streams: Record<string, StreamConfig>;
  /** Why the last load of each list failed, if it did. An empty list is
   * otherwise indistinguishable from "nothing there yet". */
  loadErrors: { tables?: string; files?: string };
  loadTables: () => Promise<void>;
  loadFiles: () => Promise<void>;
  loadStreams: () => Promise<void>;
  refreshAll: () => void;
}

const WarehouseContext = createContext<WarehouseState | null>(null);

export function WarehouseProvider({ children, enabled }: { children: React.ReactNode; enabled: boolean }) {
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [streams, setStreams] = useState<Record<string, StreamConfig>>({});
  const [loadErrors, setLoadErrors] = useState<{ tables?: string; files?: string }>({});
  const setLoadError = useCallback((key: "tables" | "files", message: string | undefined) => {
    setLoadErrors((prev) => (prev[key] === message ? prev : { ...prev, [key]: message }));
  }, []);

  // Track if initial load has happened to avoid duplicate fetches
  const initialLoadDone = useRef(false);

  const loadTables = useCallback(async () => {
    try {
      const data = await api.listTables();
      setTables(data);
      setLoadError("tables", undefined);
    } catch (e: unknown) {
      console.warn("Failed to load tables:", (e as Error).message);
      setLoadError("tables", (e as Error).message || "Failed to load tables");
    }
  }, [setLoadError]);

  const loadFiles = useCallback(async () => {
    try {
      const data = await api.listFiles();
      setFiles(
        data.map((f: FileEntry | string) =>
          typeof f === "string"
            ? ({ path: f.replace(/\\/g, "/"), type: "file" } as FileEntry)
            : { ...f, path: f.path?.replace(/\\/g, "/") }
        )
      );
      setLoadError("files", undefined);
    } catch (e: unknown) {
      console.warn("Failed to load files:", (e as Error).message);
      setLoadError("files", (e as Error).message || "Failed to load files");
    }
  }, [setLoadError]);

  const loadStreams = useCallback(async () => {
    try {
      const data = await api.listStreams();
      setStreams(data);
    } catch (e: unknown) {
      console.warn("Failed to load streams:", (e as Error).message);
    }
  }, []);

  const refreshAll = useCallback(() => {
    loadFiles();
    loadTables();
    loadStreams();
  }, [loadFiles, loadTables, loadStreams]);

  useEffect(() => {
    if (enabled && !initialLoadDone.current) {
      initialLoadDone.current = true;
      refreshAll();
    }
  }, [enabled, refreshAll]);

  // Reset when disabled (logout)
  useEffect(() => {
    if (!enabled) {
      initialLoadDone.current = false;
    }
  }, [enabled]);

  return (
    <WarehouseContext.Provider value={{ tables, files, streams, loadErrors, loadTables, loadFiles, loadStreams, refreshAll }}>
      {children}
    </WarehouseContext.Provider>
  );
}

export function useWarehouse(): WarehouseState {
  const ctx = useContext(WarehouseContext);
  if (!ctx) throw new Error("useWarehouse must be used within WarehouseProvider");
  return ctx;
}
