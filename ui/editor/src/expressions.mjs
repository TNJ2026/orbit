/**
 * Edge conditions and mappings, in the form an author writes them.
 *
 * The thing to be clear about: the `{op: ...}` tree is what the *compiler
 * produces*, not what a document holds. A DSL source writes a condition as a
 * string in Python expression syntax (`compile_condition` hands it to
 * `ast.parse`), or as a boolean, or — legally but rarely — as an already
 * compiled AST. A mapping is `{schema_id, value}` where `value` is ordinary
 * JSON except that a string beginning with `$` is a reference.
 *
 * So this module edits the authored form, and offers one convenience: an
 * expression that arrives as an AST is rendered back to the text it came from,
 * so an author who opens such an edge sees `source.value > 5` rather than a
 * blob they cannot touch.
 *
 * No React, no xyflow, for the same reason as `dsl-graph.mjs`: this decides
 * what goes into somebody's workflow, so it has to be testable on its own.
 */

// Python syntax, because `ast.parse` is what reads these. `true` would be a
// name lookup and fail; `True` is the literal.
const LITERALS = new Map([
  [true, "True"],
  [false, "False"],
  [null, "None"],
]);

const COMPARISONS = new Map([
  ["eq", "=="], ["ne", "!="], ["lt", "<"], ["lte", "<="],
  ["gt", ">"], ["gte", ">="], ["in", "in"], ["not_in", "not in"],
]);

// Binding tightness, used to decide where parentheses are needed. Mirrors
// Python's: `or` is loosest, then `and`, then `not`, then comparison.
const PRECEDENCE = { or: 1, and: 2, not: 3, comparison: 4, atom: 5 };

function precedenceOf(node) {
  if (node.op === "or" || node.op === "and") return PRECEDENCE[node.op];
  if (node.op === "not") return PRECEDENCE.not;
  if (COMPARISONS.has(node.op)) return PRECEDENCE.comparison;
  return PRECEDENCE.atom;
}

function literalText(value) {
  if (LITERALS.has(value)) return LITERALS.get(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    // A negative number has no text form in this grammar. `ast.parse("-7")`
    // yields a UnaryOp(USub), and the compiler admits only Constant — so `-7`
    // would render, look right, and then be refused as "expression syntax
    // UnaryOp is not allowed". A non-finite number is worse: `Infinity` parses
    // as a name lookup and would mean something else entirely. Such an AST is
    // reachable only through the structured form, and the honest answer is to
    // admit it cannot be shown as text rather than to show a lie.
    return Number.isFinite(value) && value >= 0 ? String(value) : null;
  }
  return null;
}

function wrap(node, limit) {
  const text = astToText(node);
  if (text === null) return null;
  return precedenceOf(node) < limit ? `(${text})` : text;
}

function joinAll(parts, render) {
  const rendered = parts.map(render);
  return rendered.some((item) => item === null) ? null : rendered;
}

/** An expression AST rendered back to the source text it was compiled from.
 *
 * `null` when the AST holds something this grammar cannot express as text.
 * Callers must treat that as "not editable here", never as an empty condition.
 */
export function astToText(node) {
  if (!node || typeof node !== "object") return null;
  switch (node.op) {
    case "literal":
      return literalText(node.value);
    case "ref":
      return typeof node.path === "string" && node.path ? node.path : null;
    case "and":
    case "or": {
      const parts = joinAll(node.args ?? [], (arg) => wrap(arg, PRECEDENCE[node.op] + 1));
      return parts === null ? null : parts.join(` ${node.op} `);
    }
    case "not": {
      const arg = wrap(node.arg, PRECEDENCE.not + 1);
      return arg === null ? null : `not ${arg}`;
    }
    case "call": {
      const arg = astToText(node.args?.[0]);
      return arg === null ? null : `${node.name}(${arg})`;
    }
    case "list": {
      const parts = joinAll(node.items ?? [], astToText);
      return parts === null ? null : `[${parts.join(", ")}]`;
    }
    default: {
      const operator = COMPARISONS.get(node.op);
      if (!operator) return null;
      const limit = PRECEDENCE.comparison + 1;
      const left = wrap(node.left, limit);
      const right = wrap(node.right, limit);
      return left === null || right === null ? null : `${left} ${operator} ${right}`;
    }
  }
}

/** The editable text for whatever form an edge's condition arrived in.
 *
 * Absent means "always", which is what the compiler substitutes, so it shows
 * as empty rather than as `True` — an author should not have to delete
 * something they never wrote.
 *
 * `null` for a structured AST that has no text form. The caller shows it
 * read-only instead of handing over a field whose contents cannot be saved.
 */
export function conditionText(condition) {
  if (condition === undefined || condition === null) return "";
  if (typeof condition === "boolean") return condition ? "True" : "False";
  if (typeof condition === "string") return condition;
  if (typeof condition === "object") return astToText(condition);
  return null;
}

/** Whether this condition can be edited as text at all. */
export const conditionEditable = (condition) => conditionText(condition) !== null;

/** What to store for edited condition text, or a problem to show.
 *
 * `original` is returned untouched when the text has not moved off it, so
 * opening an edge and closing it again cannot rewrite how the condition was
 * written — an AST stays an AST, a boolean stays a boolean.
 */
export function conditionValue(text, original) {
  const trimmed = text.trim();
  const current = conditionText(original);
  if (current === null) {
    // Nothing was offered for editing, so nothing may be saved over it.
    return { problem: "this condition has no text form and cannot be edited here" };
  }
  if (trimmed === current) return { value: original, changed: false };
  if (!trimmed) return { value: undefined, changed: true };
  if (trimmed === "True") return { value: true, changed: true };
  if (trimmed === "False") return { value: false, changed: true };
  if (trimmed.length > 4096) {
    return { problem: "a condition may be at most 4096 characters" };
  }
  // Everything else goes as the string it is. The compiler parses it, and its
  // diagnostic is a better answer than a guess made here.
  return { value: trimmed, changed: true };
}

/** Reference paths an edge may use, in the order they are worth offering.
 *
 * The compiler restricts an edge's references to its own source port and the
 * workflow inputs (`DSL_REFERENCE_NOT_FOUND` otherwise). Listing them is help,
 * not validation: what is written still goes to the compiler to judge.
 */
export function availableReferences(document, edge) {
  if (!edge) return [];
  const port = edge.from?.port ?? edge.source_port;
  const references = port ? [`source.${port}`] : [];
  for (const input of document?.inputs ?? []) {
    references.push(`workflow.inputs.${input.id}`);
  }
  return references;
}

/** The `{schema_id, value}` of a mapping as editable text.
 *
 * An absent mapping is the identity — the source port carried across — so it
 * shows as empty, not as a fabricated object.
 */
export function mappingText(mapping) {
  if (!mapping || Object.keys(mapping).length === 0) {
    return { schemaId: "", value: "" };
  }
  return {
    schemaId: mapping.schema_id ?? "",
    value: mapping.value === undefined ? "" : JSON.stringify(mapping.value, null, 2),
  };
}

/** What to store for an edited mapping, or a problem to show.
 *
 * Both halves are required together: `compile_mapping` refuses a mapping
 * without a `schema_id` and one without a `value`, so a half-filled form is
 * stopped here rather than sent to be refused.
 */
export function mappingValue({ schemaId, value }, original) {
  const current = mappingText(original);
  if (schemaId.trim() === current.schemaId && value.trim() === current.value.trim()) {
    return { value: original, changed: false };
  }
  if (!schemaId.trim() && !value.trim()) return { value: undefined, changed: true };
  if (!schemaId.trim()) return { problem: "a mapping needs a schema_id" };
  if (!value.trim()) return { problem: "a mapping needs a value" };
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch (error) {
    return { problem: `value is not JSON: ${error.message}` };
  }
  return { value: { schema_id: schemaId.trim(), value: parsed }, changed: true };
}

/** Parse an object-valued field, or say why it could not be.
 *
 * Used for a node's `config`, which the DSL requires to be an object and whose
 * shape belongs to the Handler, not to this editor.
 */
export function objectValue(text, original) {
  const current = original === undefined || Object.keys(original ?? {}).length === 0
    ? ""
    : JSON.stringify(original, null, 2);
  if (text.trim() === current.trim()) return { value: original, changed: false };
  if (!text.trim()) return { value: undefined, changed: true };
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    return { problem: `not JSON: ${error.message}` };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { problem: "must be a JSON object" };
  }
  return { value: parsed, changed: true };
}

export function objectText(value) {
  if (!value || Object.keys(value).length === 0) return "";
  return JSON.stringify(value, null, 2);
}
