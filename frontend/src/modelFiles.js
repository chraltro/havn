/*
 * Which files in the project are transformation models.
 *
 * A model is a .sql file under transform/, or a .py file there that defines
 * a model function: one decorated with @model (or @havn.model), or a plain
 * `def model`. Other .py files under transform/ are helper modules the
 * models import; by convention they start with "_". The backend makes the
 * same call from the file's syntax tree (engine/transform/python_models.py);
 * this is the cheap version for deciding what the editor shows.
 */

const PY_MODEL_RE = /^\s*(?:@(?:\w+\.)?model\b|def\s+model\s*\()/m;

function norm(path) {
  return (path || "").replace(/\\/g, "/");
}

function baseName(path) {
  const parts = norm(path).split("/");
  return parts[parts.length - 1] || "";
}

/** True when `content` reads like a Python model file. */
export function isPythonModelSource(content) {
  return PY_MODEL_RE.test(content || "");
}

/** True for a .py file under transform/ that is not a `_` helper. */
export function isPythonModelPath(path) {
  const p = norm(path);
  return p.startsWith("transform/") && p.endsWith(".py") && !baseName(p).startsWith("_");
}

/**
 * Whether the file at `path` is a model. `content` decides for a .py file;
 * without it, any non-helper .py under transform/ counts.
 */
export function isModelFile(path, content) {
  const p = norm(path);
  if (!p.startsWith("transform/")) return false;
  if (p.endsWith(".sql")) return true;
  if (!isPythonModelPath(p)) return false;
  return content === undefined ? true : isPythonModelSource(content);
}

/**
 * "schema.name" by the folder convention: transform/silver/x.py -> silver.x.
 * Only the default; @config schema= / @model(schema=...) can move a model,
 * which is why the workbench passes the real name when it has it.
 */
export function modelNameFromPath(path) {
  return norm(path)
    .replace(/^transform\//, "")
    .replace(/\.(sql|py)$/, "")
    .replace(/\//g, ".");
}

/** Starter content for a new Python model file created from the tree. */
export function pythonModelTemplate(path) {
  const name = baseName(path).replace(/\.py$/, "") || "model";
  return (
    `"""${modelNameFromPath(path)}: describe what this model builds."""\n` +
    "from havn import model\n\n\n" +
    '@model(materialized="table")\n' +
    `def ${name}(db, ref):\n` +
    '    # ref("schema.table") returns a DuckDB relation and makes it a dependency.\n' +
    '    return db.sql("SELECT 1 AS placeholder")\n'
  );
}
