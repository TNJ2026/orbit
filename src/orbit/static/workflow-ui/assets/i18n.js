/* Message catalogs and locale-aware formatting.
 *
 * There is no default string baked into a component: a key with no translation
 * renders as the key itself, loudly, so a missing entry fails a review instead
 * of quietly shipping one language into the other locale.
 */

export const LOCALES = ["zh-CN", "en-US"];
const STORAGE_KEY = "orbit.locale";

export function preferredLocale(
  stored = localStorage.getItem(STORAGE_KEY),
  languages = navigator.languages || [navigator.language || ""],
) {
  if (LOCALES.includes(stored)) return stored;
  for (const tag of languages) {
    const exact = LOCALES.find((locale) => locale.toLowerCase() === tag.toLowerCase());
    if (exact) return exact;
    const base = LOCALES.find((locale) => locale.split("-")[0] === tag.split("-")[0]);
    if (base) return base;
  }
  return "en-US";
}

export class I18n {
  constructor(locale, messages) {
    this.locale = locale;
    this.messages = messages;
    this.missing = new Set();
  }

  static async load(locale) {
    const response = await fetch(`assets/i18n.${locale}.json`);
    if (!response.ok) throw new Error(`missing catalog for ${locale}`);
    return new I18n(locale, await response.json());
  }

  /** Translate `key`, substituting {placeholders} from `values`. */
  t(key, values = {}) {
    const template = this.messages[key];
    if (template === undefined) {
      this.missing.add(key);
      return key;
    }
    return template.replace(/\{(\w+)\}/g, (match, name) =>
      Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match,
    );
  }

  /** A status pill's text, falling back to the raw server value. */
  status(value) {
    const key = `status.${value}`;
    return this.messages[key] === undefined ? value : this.messages[key];
  }

  /** A server-advertised command's label, keyed by command id. */
  command(allowed) {
    const key = `command.${allowed.command}`;
    // The server's own label is the fallback: a new command must still be
    // clickable before its translation lands.
    return this.messages[key] === undefined ? allowed.label : this.messages[key];
  }

  number(value) {
    return new Intl.NumberFormat(this.locale).format(value);
  }

  dateTime(value) {
    if (!value) return "";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return new Intl.DateTimeFormat(this.locale, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(parsed);
  }

  persist() {
    // A UI preference, deliberately client-side: locale is not Runtime state.
    localStorage.setItem(STORAGE_KEY, this.locale);
  }
}
