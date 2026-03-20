import React, { useState, useMemo, useRef, useEffect } from "react";
import { schemaCompare } from "./schemaOrder";

function FileNode({ node, depth, onSelect, activeFile, onNewFile, onDeleteFile, onMoveFile }) {
  // Auto-expand directories that contain the active file
  const _af = activeFile?.replace(/\\/g, "/");
  const containsActive = _af && node.type === "dir" && node.children?.some(function check(n) {
    return n.path?.replace(/\\/g, "/") === _af || (n.children && n.children.some(check));
  });
  const [expanded, setExpanded] = useState(depth < 2 || !!containsActive);

  // Expand when active file changes to be inside this dir
  useEffect(() => {
    if (containsActive && !expanded) setExpanded(true);
  }, [containsActive]);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [hovered, setHovered] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const activeRef = useRef(null);
  const isActive = activeFile && node.path && activeFile.replace(/\\/g, "/") === node.path.replace(/\\/g, "/");

  // Scroll into view when this node becomes active
  useEffect(() => {
    if (isActive && activeRef.current) {
      activeRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [isActive]);

  if (node.type === "dir") {
    return (
      <div role="treeitem" aria-expanded={expanded} aria-label={node.name}>
        <div
          data-havn-file=""
          style={{
            ...styles.item,
            paddingLeft: 8 + depth * 16,
            background: dragOver ? "color-mix(in srgb, var(--havn-accent) 12%, transparent)" : "transparent",
          }}
          onClick={() => setExpanded(!expanded)}
          onMouseEnter={() => setHovered(true)}
          onMouseLeave={() => setHovered(false)}
          onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); setDragOver(true); }}
          onDragLeave={(e) => { e.stopPropagation(); setDragOver(false); }}
          onDrop={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setDragOver(false);
            const srcPath = e.dataTransfer.getData("text/plain");
            if (srcPath && onMoveFile) {
              const fileName = srcPath.split("/").pop();
              const dest = `${node.path}/${fileName}`;
              if (dest !== srcPath) onMoveFile(srcPath, dest);
            }
          }}
        >
          <span style={{ ...styles.icon, transform: expanded ? "rotate(0deg)" : "rotate(-90deg)" }}>
            {"\u25BE"}
          </span>
          <span style={styles.dirName}>{node.name}</span>
          <button
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(true);
              setCreating(true);
              setNewName("");
            }}
            style={{ ...styles.addBtn, opacity: hovered ? 1 : 0, pointerEvents: hovered ? "auto" : "none" }}
            title={`New file in ${node.name}/`}
            aria-label={`New file in ${node.name}`}
          >+</button>
        </div>
        {expanded && creating && (
          <div style={{ ...styles.newFileRow, paddingLeft: 24 + depth * 16 }}>
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="filename.sql"
              style={styles.newFileInput}
              autoFocus
              onKeyDown={(e) => {
                if (e.key === "Enter" && newName.trim()) {
                  onNewFile(`${node.path}/${newName.trim()}`);
                  setCreating(false);
                  setNewName("");
                }
                if (e.key === "Escape") { setCreating(false); setNewName(""); }
              }}
              onBlur={() => { setCreating(false); setNewName(""); }}
            />
          </div>
        )}
        {expanded &&
          (node.path === "transform" ? [...(node.children || [])].sort((a, b) => a.type === "dir" && b.type === "dir" ? schemaCompare(a.name, b.name) : 0) : node.children)?.map((child) => (
            <FileNode
              key={child.path}
              node={child}
              depth={depth + 1}
              onSelect={onSelect}
              activeFile={activeFile}
              onNewFile={onNewFile}
              onDeleteFile={onDeleteFile}
              onMoveFile={onMoveFile}
            />
          ))}
      </div>
    );
  }

  const ext = node.name.split(".").pop();
  const iconColor = ext === "sql" ? "var(--havn-accent)" : ext === "py" ? "var(--havn-green)" : "var(--havn-text-secondary)";

  return (
    <div
      ref={isActive ? activeRef : undefined}
      data-havn-file=""
      role="treeitem"
      aria-selected={isActive}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData("text/plain", node.path);
        e.dataTransfer.effectAllowed = "move";
      }}
      style={{
        ...styles.item,
        paddingLeft: 8 + depth * 16,
        background: isActive ? "var(--havn-bg-secondary)" : "transparent",
        borderLeft: isActive ? "2px solid var(--havn-accent)" : "2px solid transparent",
      }}
      onClick={() => onSelect(node.path)}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <span style={{ ...styles.dot, background: iconColor }} />
      <span style={isActive ? styles.activeFileName : styles.fileName}>{node.name}</span>
      {onDeleteFile && (
        <button
          onClick={(e) => { e.stopPropagation(); onDeleteFile(node.path); }}
          style={{ ...styles.deleteBtn, opacity: hovered ? 1 : 0, pointerEvents: hovered ? "auto" : "none" }}
          title={`Delete ${node.name}`}
          aria-label={`Delete ${node.name}`}
        >&times;</button>
      )}
    </div>
  );
}

/**
 * Collect all file names (leaf nodes) in a tree, returning a Set of paths whose
 * file name matches the query (case-insensitive substring).
 */
function collectMatchingPaths(nodes, query) {
  const matches = new Set();
  const lower = query.toLowerCase();
  function walk(node) {
    if (node.type === "file") {
      if (node.name.toLowerCase().includes(lower)) {
        // Normalize backslashes to forward slashes for consistent path comparison
        matches.add((node.path || "").replace(/\\/g, "/"));
      }
    }
    if (node.children) node.children.forEach(walk);
  }
  nodes.forEach(walk);
  return matches;
}

/**
 * Given a set of matching file paths, return a set of all ancestor directory paths
 * that need to be visible (expanded) so the matching files are shown.
 */
function ancestorPaths(matchingPaths) {
  const ancestors = new Set();
  for (const p of matchingPaths) {
    // Handle both forward slashes and backslashes (Windows paths from API)
    const normalized = p.replace(/\\/g, "/");
    const parts = normalized.split("/");
    for (let i = 1; i < parts.length; i++) {
      ancestors.add(parts.slice(0, i).join("/"));
    }
  }
  return ancestors;
}

function FilteredFileNode({ node, depth, onSelect, activeFile, onNewFile, onDeleteFile, onMoveFile, matchingFiles, visibleDirs }) {
  const normalizedPath = (node.path || "").replace(/\\/g, "/");
  if (node.type === "file") {
    if (!matchingFiles.has(normalizedPath)) return null;
    return (
      <FileNode node={node} depth={depth} onSelect={onSelect} activeFile={activeFile} onNewFile={onNewFile} onDeleteFile={onDeleteFile} onMoveFile={onMoveFile} />
    );
  }
  // Directory: only show if it's an ancestor of a matching file
  if (!visibleDirs.has(normalizedPath)) return null;
  const children = (node.path === "transform"
    ? [...(node.children || [])].sort((a, b) => a.type === "dir" && b.type === "dir" ? schemaCompare(a.name, b.name) : 0)
    : node.children || []);
  return (
    <div>
      <div data-havn-file="" style={{ ...styles.item, paddingLeft: 8 + depth * 16 }}>
        <span style={{ ...styles.icon, transform: "rotate(0deg)" }}>{"\u25BE"}</span>
        <span style={styles.dirName}>{node.name}</span>
      </div>
      {children.map((child) => (
        <FilteredFileNode
          key={child.path}
          node={child}
          depth={depth + 1}
          onSelect={onSelect}
          activeFile={activeFile}
          onNewFile={onNewFile}
          onDeleteFile={onDeleteFile}
          onMoveFile={onMoveFile}
          matchingFiles={matchingFiles}
          visibleDirs={visibleDirs}
        />
      ))}
    </div>
  );
}

export default function FileTree({ files, onSelect, activeFile, onNewFile, onDeleteFile, onMoveFile, filter: externalFilter }) {
  // Support both external filter (from sidebar) and internal filter (standalone)
  const [internalFilter, setInternalFilter] = useState("");
  const filter = externalFilter !== undefined ? externalFilter : internalFilter;

  const matchingFiles = useMemo(() => filter.trim() ? collectMatchingPaths(files, filter.trim()) : null, [files, filter]);
  const visibleDirs = useMemo(() => matchingFiles ? ancestorPaths(matchingFiles) : null, [matchingFiles]);

  const isFiltering = filter.trim().length > 0;

  return (
    <div role="tree" aria-label="Project files">
      {externalFilter === undefined && (
        <div style={styles.filterRow}>
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="var(--havn-text-dim)" strokeWidth="1.5" style={{ flexShrink: 0 }}>
            <circle cx="6.5" cy="6.5" r="5"/>
            <path d="M10.5 10.5L14.5 14.5"/>
          </svg>
          <input
            value={internalFilter}
            onChange={(e) => setInternalFilter(e.target.value)}
            placeholder="Filter files..."
            style={styles.filterInput}
            aria-label="Filter files"
          />
          {internalFilter && (
            <button onClick={() => setInternalFilter("")} style={styles.filterClear} title="Clear filter" aria-label="Clear filter">&times;</button>
          )}
        </div>
      )}
      {files.length === 0 && !isFiltering && (
        <div style={styles.empty}>No files found</div>
      )}
      {isFiltering && matchingFiles && matchingFiles.size === 0 && (
        <div style={styles.empty}>No files matching '{filter}'</div>
      )}
      {isFiltering && matchingFiles && visibleDirs ? (
        files.map((f) => (
          <FilteredFileNode
            key={f.path}
            node={f}
            depth={0}
            onSelect={onSelect}
            activeFile={activeFile}
            onNewFile={onNewFile}
            onDeleteFile={onDeleteFile}
            onMoveFile={onMoveFile}
            matchingFiles={matchingFiles}
            visibleDirs={visibleDirs}
          />
        ))
      ) : (
        !isFiltering && files.map((f) => (
          <FileNode key={f.path} node={f} depth={0} onSelect={onSelect} activeFile={activeFile} onNewFile={onNewFile} onDeleteFile={onDeleteFile} onMoveFile={onMoveFile} />
        ))
      )}
    </div>
  );
}

const styles = {
  filterRow: { display: "flex", alignItems: "center", gap: "6px", padding: "4px 8px", margin: "4px 4px 2px" },
  filterInput: { flex: 1, padding: "3px 6px", background: "var(--havn-bg)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontSize: "11px", fontFamily: "var(--havn-font-mono)", outline: "none", minWidth: 0 },
  filterClear: { background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "14px", padding: "0 2px", lineHeight: 1, flexShrink: 0 },
  item: { display: "flex", alignItems: "center", gap: "6px", padding: "4px 8px", cursor: "pointer", fontSize: "13px", whiteSpace: "nowrap", margin: "0 4px", borderRadius: "3px" },
  icon: { fontSize: "10px", color: "var(--havn-text-secondary)", width: "10px", display: "inline-block", transition: "transform 0.12s ease" },
  dirName: { color: "var(--havn-text)", fontWeight: 500, fontFamily: "var(--havn-font-mono)" },
  addBtn: { marginLeft: "auto", width: "18px", height: "18px", background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "14px", lineHeight: "18px", textAlign: "center", padding: 0, flexShrink: 0 },
  newFileRow: { display: "flex", padding: "2px 8px 4px", margin: "0 4px" },
  newFileInput: { flex: 1, padding: "3px 6px", background: "var(--havn-bg)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontSize: "11px", fontFamily: "var(--havn-font-mono)", outline: "none" },
  fileName: { color: "var(--havn-text)", fontFamily: "var(--havn-font-mono)", fontSize: "12px" },
  activeFileName: { color: "var(--havn-accent)", fontWeight: 500, fontFamily: "var(--havn-font-mono)", fontSize: "12px" },
  dot: { width: "6px", height: "6px", borderRadius: "50%", flexShrink: 0 },
  deleteBtn: { marginLeft: "auto", width: "18px", height: "18px", background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "14px", lineHeight: "18px", textAlign: "center", padding: 0, flexShrink: 0 },
  empty: { padding: "12px", color: "var(--havn-text-dim)", fontSize: "12px", textAlign: "center" },
};
