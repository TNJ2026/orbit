import { el } from "./dom.js";

let sequence = 0;

export function syncCustomSelect(select) {
  if (!select) return;
  const wrapper = select.closest(".custom-select");
  if (!wrapper) return;
  const selected = select.selectedOptions[0];
  const trigger = wrapper.querySelector(".custom-select-trigger");
  if (select.getAttribute("aria-label")) {
    select.dataset.customSelectLabel = select.getAttribute("aria-label");
  }
  trigger.setAttribute("aria-label", select.dataset.customSelectLabel || "");
  select.removeAttribute("aria-label");
  trigger.querySelector(".custom-select-value").textContent = selected?.textContent || "";
  trigger.disabled = select.disabled;
  for (const option of wrapper.querySelectorAll(".custom-select-option")) {
    const active = option.dataset.value === select.value;
    option.setAttribute("aria-selected", String(active));
    option.classList.toggle("selected", active);
  }
}

function enhanceSelect(select) {
  if (select.dataset.customSelect === "true") return;
  select.dataset.customSelect = "true";
  const label = select.getAttribute("aria-label")
    || select.labels?.[0]?.textContent.trim() || "";
  select.dataset.customSelectLabel = label;
  const listId = `custom-select-${sequence += 1}`;
  const wrapper = el("span", { class: "custom-select" });
  const trigger = el("button", {
    type: "button", class: "button custom-select-trigger",
    role: "combobox", "aria-haspopup": "listbox", "aria-expanded": "false",
    "aria-controls": listId, "aria-label": label,
  }, [
    el("span", { class: "custom-select-value" }),
    el("span", { class: "custom-select-chevron", "aria-hidden": "true" }),
  ]);
  const list = el("span", {
    class: "custom-select-options", id: listId, role: "listbox",
    ...(label ? { "aria-label": label } : {}), hidden: "hidden",
  });

  const close = (restoreFocus = false) => {
    list.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    wrapper.classList.remove("open");
    if (restoreFocus) trigger.focus();
  };
  const open = (focusSelected = false) => {
    if (trigger.disabled) return;
    for (const other of document.querySelectorAll(".custom-select.open")) {
      if (other !== wrapper) {
        other.querySelector(".custom-select-options").hidden = true;
        other.querySelector(".custom-select-trigger").setAttribute("aria-expanded", "false");
        other.classList.remove("open");
      }
    }
    list.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    wrapper.classList.add("open");
    if (focusSelected) {
      (list.querySelector(".custom-select-option.selected") || list.firstElementChild)?.focus();
    }
  };

  for (const nativeOption of select.options) {
    const option = el("button", {
      type: "button", class: "custom-select-option", role: "option",
      "data-value": nativeOption.value,
      "aria-selected": String(nativeOption.selected),
      ...(nativeOption.disabled ? { disabled: "disabled" } : {}),
      text: nativeOption.textContent,
    });
    option.addEventListener("click", () => {
      select.value = option.dataset.value;
      close(true);
      syncCustomSelect(select);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    option.addEventListener("keydown", (event) => {
      const options = [...list.querySelectorAll(".custom-select-option:not(:disabled)")];
      const index = options.indexOf(option);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const delta = event.key === "ArrowDown" ? 1 : -1;
        options[(index + delta + options.length) % options.length]?.focus();
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        options[event.key === "Home" ? 0 : options.length - 1]?.focus();
      } else if (event.key === "Escape" || event.key === "Tab") {
        close(event.key === "Escape");
      }
    });
    list.append(option);
  }

  trigger.addEventListener("click", () => {
    if (list.hidden) open(false); else close(false);
  });
  trigger.addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      open(true);
    } else if (event.key === "Escape") close(false);
  });
  document.addEventListener("pointerdown", (event) => {
    if (!wrapper.contains(event.target)) close(false);
  });

  select.parentNode.insertBefore(wrapper, select);
  wrapper.append(select, trigger, list);
  select.classList.add("custom-select-native");
  select.hidden = true;
  select.setAttribute("tabindex", "-1");
  select.setAttribute("aria-hidden", "true");
  select.addEventListener("change", () => syncCustomSelect(select));
  syncCustomSelect(select);
}

export function installCustomSelects() {
  document.querySelectorAll("select").forEach(enhanceSelect);
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (!(node instanceof Element)) continue;
        if (node.matches("select")) enhanceSelect(node);
        node.querySelectorAll?.("select").forEach(enhanceSelect);
      }
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}
