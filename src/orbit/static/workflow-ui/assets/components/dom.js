/** Small DOM constructors shared by the framework-free UI modules. */

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

export function svgEl(tag, props = {}, children = []) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

/**
 * The way out of an overlay, in the corner, as a mark rather than a word.
 *
 * One constructor because there is one control: the run sheet, the goal
 * sheet, the Agent picker, the artifact dialog, the workflow page and the
 * editor each drew their own, half of them as a labelled button reading
 * "Close" and half as this cross, so the same gesture looked like a different
 * thing depending on which overlay you had opened.
 *
 * The word survives where it was always doing the work — `aria-label` and the
 * tooltip — so a reader who cannot see the mark still gets the sentence, and
 * a translation still has somewhere to land. What goes is only the printed
 * label, which said in a button what the position and the cross already say.
 */
export function closeButton(el, { label, onClose, id = null, className = "" }) {
  return el("button", {
    class: `icon-close${className ? ` ${className}` : ""}`,
    type: "button",
    ...(id ? { id } : {}),
    "aria-label": label,
    title: label,
    onclick: onClose,
  }, [
    svgEl("svg", {
      viewBox: "0 0 24 24", width: "16", height: "16",
      "aria-hidden": "true", fill: "none", stroke: "currentColor",
      "stroke-width": "1.8", "stroke-linecap": "round",
    }, [
      svgEl("path", { d: "M6 6l12 12" }),
      svgEl("path", { d: "M18 6L6 18" }),
    ]),
  ]);
}
