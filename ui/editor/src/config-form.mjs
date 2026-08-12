/**
 * A Handler's `config_schema`, turned into fields to render.
 *
 * The schema is the Handler's own — it is what validates the config, and it
 * arrives from `/api/v1/handler-catalog` already carrying the bounds and the
 * prose the Handler's author wrote. Asking an author to hand-type JSON that
 * the Runtime could describe to them is asking them to guess at something
 * they were already told.
 *
 * This renders the part of JSON Schema that Handlers actually use, and is
 * deliberate about the rest: a schema it cannot express fully is reported as
 * such so the caller keeps the raw editor rather than quietly offering a form
 * that would drop what it does not understand. Nothing here may lose a key.
 */

const SUPPORTED = new Set(["string", "integer", "number", "boolean"]);

/** A single-line input is right up to here; past it a value wants room. */
const SHORT_TEXT = 120;

function describeOne(name, property, required) {
  if (!property || typeof property !== "object") return null;
  const { type, description, enum: choices } = property;
  const base = {
    name,
    required,
    description: typeof description === "string" ? description : null,
    default: property.default,
  };
  if (Array.isArray(choices) && choices.length) {
    return { ...base, control: "select", choices };
  }
  if (!SUPPORTED.has(type)) return null;
  if (type === "boolean") return { ...base, control: "checkbox" };
  if (type === "integer" || type === "number") {
    return {
      ...base,
      control: "number",
      integer: type === "integer",
      minimum: typeof property.minimum === "number" ? property.minimum : null,
      maximum: typeof property.maximum === "number" ? property.maximum : null,
    };
  }
  const maxLength = property.maxLength;
  return {
    ...base,
    control: typeof maxLength === "number" && maxLength <= SHORT_TEXT ? "text" : "textarea",
    maxLength: typeof maxLength === "number" ? maxLength : null,
  };
}

/** The fields a form can offer for this schema, or none.
 *
 * `null` when there is nothing to render from — a schema with no properties
 * describes a Handler that takes whatever it likes, and a form with no fields
 * would say the opposite.
 */
export function describeFields(schema) {
  const properties = schema?.properties;
  if (!properties || typeof properties !== "object") return null;
  const required = new Set(Array.isArray(schema.required) ? schema.required : []);
  const fields = [];
  for (const name of Object.keys(properties)) {
    const field = describeOne(name, properties[name], required.has(name));
    // One property this cannot draw makes the whole form untrustworthy: an
    // author would edit around a field they cannot see.
    if (field === null) return null;
    fields.push(field);
  }
  return fields.length ? fields : null;
}

/** Keys held in the config that the schema does not describe.
 *
 * A schema that closes itself with `additionalProperties: false` cannot have
 * any, so a form covers the whole config. An open one can, and those keys have
 * to survive being edited beside — they are shown, and never dropped.
 */
export function unknownKeys(config, schema) {
  const known = new Set(Object.keys(schema?.properties ?? {}));
  return Object.keys(config ?? {}).filter((key) => !known.has(key)).sort();
}

/** Whether a form alone can hold everything this config may contain. */
export const isClosed = (schema) => schema?.additionalProperties === false;

/** The value to show for a field: what is set, else the schema's default. */
export function fieldValue(config, field) {
  const current = config?.[field.name];
  return current === undefined ? field.default : current;
}

/**
 * One field changed, as a new config.
 *
 * An emptied field removes the key rather than writing `""` or `null`: absent
 * and blank are different to a Handler, and the schema's own default applies
 * only to the absent one. Returns `{value}` or `{problem}`.
 */
export function applyField(config, field, raw) {
  const next = { ...(config ?? {}) };
  const blank = raw === "" || raw === null || raw === undefined;

  if (field.control === "checkbox") {
    if (raw) next[field.name] = true;
    else delete next[field.name];
    return { value: next };
  }
  if (blank) {
    delete next[field.name];
    return { value: next };
  }
  if (field.control === "number") {
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return { problem: `${field.name} must be a number` };
    if (field.integer && !Number.isInteger(parsed)) {
      return { problem: `${field.name} must be a whole number` };
    }
    if (field.minimum !== null && parsed < field.minimum) {
      return { problem: `${field.name} must be at least ${field.minimum}` };
    }
    if (field.maximum !== null && parsed > field.maximum) {
      return { problem: `${field.name} must be at most ${field.maximum}` };
    }
    next[field.name] = parsed;
    return { value: next };
  }
  if (field.maxLength !== null && field.maxLength !== undefined
      && String(raw).length > field.maxLength) {
    return { problem: `${field.name} may be at most ${field.maxLength} characters` };
  }
  next[field.name] = String(raw);
  return { value: next };
}

/** An empty config is absent, not `{}` — the DSL gives it that default. */
export const emptied = (config) =>
  config && Object.keys(config).length ? config : undefined;
