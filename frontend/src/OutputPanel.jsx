import React, { useEffect, useRef, useMemo } from "react";

// Match file:line:col at start of message (lint output, e.g. "transform\silver\foo.sql:19:20 ...")
const FILE_LINE_RE = /^([\w./\\-]+\.\w+):(\d+):(\d+)/;
// Match identifier with a dot followed by ": " (model or script reference, e.g. "silver.earthquake_daily: skipped")
const REF_RE = /^([\w/\\-]+\.[\w./\\-]+): /;

function renderMessage(message, style, onOpenFile) {
  if (!onOpenFile) return <span style={style}>{message}</span>;

  // Try file:line:col first (lint output)
  const lineMatch = message.match(FILE_LINE_RE);
  if (lineMatch) {
    const [fullMatch, filePath, lineStr, colStr] = lineMatch;
    const line = parseInt(lineStr, 10);
    const col = parseInt(colStr, 10);
    const rest = message.slice(fullMatch.length);
    return (
      <span style={style}>
        <span
          style={styles.fileLink}
          onClick={() => onOpenFile(filePath, line, col)}
          onMouseEnter={(e) => { e.currentTarget.style.color = "var(--havn-accent)"; e.currentTarget.style.textDecoration = "underline"; }}
          onMouseLeave={(e) => { e.currentTarget.style.color = ""; e.currentTarget.style.textDecoration = ""; }}
          title={`Open ${filePath} at line ${line}`}
        >
          {fullMatch}
        </span>
        {rest}
      </span>
    );
  }

  // Try identifier: status (model or script reference)
  const refMatch = message.match(REF_RE);
  if (refMatch) {
    const ref = refMatch[1];
    const rest = message.slice(ref.length);
    return (
      <span style={style}>
        <span
          style={styles.fileLink}
          onClick={() => onOpenFile(ref, 1, 1)}
          onMouseEnter={(e) => { e.currentTarget.style.color = "var(--havn-accent)"; e.currentTarget.style.textDecoration = "underline"; }}
          onMouseLeave={(e) => { e.currentTarget.style.color = ""; e.currentTarget.style.textDecoration = ""; }}
          title={`Open ${ref}`}
        >
          {ref}
        </span>
        {rest}
      </span>
    );
  }

  // Try inline file paths anywhere in the message (e.g. "Exported 31 rows to output/file.csv")
  const INLINE_PATH_RE = /([\w-]+[/\\][\w./\\-]+\.\w+)/g;
  const parts = [];
  let lastIndex = 0;
  let m;
  while ((m = INLINE_PATH_RE.exec(message)) !== null) {
    if (m.index > lastIndex) parts.push({ text: message.slice(lastIndex, m.index) });
    parts.push({ text: m[1], isLink: true });
    lastIndex = m.index + m[0].length;
  }
  if (parts.length > 0) {
    if (lastIndex < message.length) parts.push({ text: message.slice(lastIndex) });
    return (
      <span style={style}>
        {parts.map((p, i) => p.isLink ? (
          <span
            key={i}
            style={styles.fileLink}
            onClick={() => onOpenFile(p.text, 1, 1)}
            onMouseEnter={(e) => { e.currentTarget.style.color = "var(--havn-accent)"; e.currentTarget.style.textDecoration = "underline"; }}
            onMouseLeave={(e) => { e.currentTarget.style.color = ""; e.currentTarget.style.textDecoration = ""; }}
            title={`Open ${p.text}`}
          >
            {p.text}
          </span>
        ) : p.text)}
      </span>
    );
  }

  return <span style={style}>{message}</span>;
}

// Extract active task names from output messages
const START_RE = /(?:Ingesting|Building|Exporting) (.+)\.\.\./;
const END_RE = /(?:Ingested|Built|Skipped|Failed) ([^\s]+)/;

export default function OutputPanel({ output, onClear, height = 180, onOpenFile, running, progress }) {
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [output]);

  // Compute in-flight tasks from output messages
  const activeTasks = useMemo(() => {
    if (!running) return [];
    const started = new Set();
    const finished = new Set();
    for (const entry of output) {
      const msg = entry.message || "";
      const sm = msg.match(START_RE);
      if (sm) started.add(sm[1]);
      const em = msg.match(END_RE);
      if (em) finished.add(em[1]);
    }
    return [...started].filter(t => !finished.has(t));
  }, [output, running]);

  const pctText = running && progress > 0 && progress < 1 ? `${Math.round(progress * 100)}%` : "";

  return (
    <div style={{ ...styles.container, height }}>
      <div style={styles.header}>
        <span style={styles.headerTitle}>Output</span>
        {running && <span style={styles.runningBadge}>{pctText || "running"}</span>}
        <span style={styles.count}>{output.length > 0 ? `${output.length} entries` : ""}</span>
        <button onClick={onClear} style={styles.clearBtn}>
          Clear
        </button>
      </div>
      <div style={styles.log}>
        {output.length === 0 && (
          <div style={styles.placeholder}>Run a script or transform to see output here.</div>
        )}
        {output.map((entry, i) => (
          <div key={i} style={styles.entry}>
            <span style={styles.ts}>{entry.ts}</span>
            <span style={{
              ...styles.indicator,
              background: entry.type === "error" ? "var(--havn-red)"
                : entry.type === "warn" ? "var(--havn-yellow)"
                : entry.type === "success" ? "var(--havn-accent)"
                : "var(--havn-accent)",
            }} />
            {renderMessage(entry.message, typeStyles[entry.type] || typeStyles.info, onOpenFile)}
          </div>
        ))}
        {running && activeTasks.length > 0 && (
          <div style={styles.statusLine}>
            <span style={styles.statusSpinner} />
            <span>Waiting for {activeTasks.length === 1 ? activeTasks[0] : `${activeTasks.length} tasks`}</span>
            {activeTasks.length > 1 && activeTasks.length <= 4 && (
              <span style={styles.statusDetail}>{activeTasks.join(", ")}</span>
            )}
          </div>
        )}
        {running && activeTasks.length === 0 && output.length > 0 && (
          <div style={styles.statusLine}>
            <span style={styles.statusSpinner} />
            <span>Running...</span>
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}

const typeStyles = {
  info: { color: "var(--havn-text)" },
  success: { color: "var(--havn-accent)" },
  error: { color: "var(--havn-red)" },
  warn: { color: "var(--havn-yellow)" },
  log: { color: "var(--havn-text-secondary)" },
};

const styles = {
  container: { borderTop: "1px solid var(--havn-border)", display: "flex", flexDirection: "column", background: "var(--havn-bg-tertiary)" },
  header: { display: "flex", alignItems: "center", gap: "8px", padding: "4px 12px", fontSize: "12px", fontWeight: "600", color: "var(--havn-text-secondary)", borderBottom: "1px solid var(--havn-border)" },
  headerTitle: { fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px" },
  count: { fontSize: "10px", color: "var(--havn-text-dim)", flex: 1 },
  clearBtn: { background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "11px" },
  log: { flex: 1, overflow: "auto", padding: "4px 12px", fontFamily: "var(--havn-font-mono)", fontSize: "12px", lineHeight: "1.7" },
  placeholder: { color: "var(--havn-text-dim)", fontStyle: "italic", padding: "8px 0", fontSize: "12px" },
  entry: { display: "flex", gap: "8px", alignItems: "baseline" },
  ts: { color: "var(--havn-text-dim)", flexShrink: 0, fontSize: "11px" },
  indicator: { width: "4px", height: "4px", borderRadius: "50%", flexShrink: 0, marginTop: "2px" },
  fileLink: { cursor: "pointer", transition: "color 0.15s" },
  runningBadge: { fontSize: "10px", padding: "1px 6px", borderRadius: "4px", background: "color-mix(in srgb, var(--havn-accent) 15%, transparent)", color: "var(--havn-accent)", fontWeight: 600, letterSpacing: "0.3px" },
  statusLine: { display: "flex", gap: "8px", alignItems: "center", padding: "6px 0", color: "var(--havn-text-dim)", fontSize: "12px", fontStyle: "italic" },
  statusSpinner: { width: "8px", height: "8px", borderRadius: "50%", border: "2px solid var(--havn-accent)", borderTopColor: "transparent", animation: "havn-spin 0.8s linear infinite", flexShrink: 0 },
  statusDetail: { color: "var(--havn-text-dim)", fontSize: "11px" },
};
