const tablist = document.querySelector("[data-editor-tabs]");

if (tablist) {
  const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
  const modeInput = document.getElementById("editor-mode");

  function activateTab(tab, moveFocus = false) {
    for (const item of tabs) {
      const selected = item === tab;
      const panel = document.getElementById(item.getAttribute("aria-controls"));
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      if (panel) {
        panel.hidden = !selected;
        panel.querySelectorAll("input, select, textarea").forEach((control) => {
          control.disabled = !selected;
        });
      }
    }
    if (modeInput) modeInput.value = tab.dataset.editorMode || "fields";
    if (moveFocus) tab.focus();
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => activateTab(tab));
  }

  tablist.addEventListener("keydown", (event) => {
    const currentIndex = tabs.indexOf(event.target);
    if (currentIndex < 0) return;

    let nextIndex;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (currentIndex + 1) % tabs.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    } else {
      return;
    }

    event.preventDefault();
    activateTab(tabs[nextIndex], true);
  });

  activateTab(tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0]);
}

for (const group of document.querySelectorAll("[data-repeat-group]")) {
  const items = group.querySelector("[data-repeat-items]");
  const template = document.getElementById(group.dataset.repeatTemplate);
  if (!items || !template) continue;

  function updateAddButton() {
    const addButton = group.querySelector("[data-add-item]");
    if (addButton) addButton.disabled = items.querySelectorAll("[data-repeat-item]").length >= 20;
  }

  group.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;

    if (event.target.closest("[data-add-item]")) {
      if (items.querySelectorAll("[data-repeat-item]").length >= 20) return;
      const row = template.content.firstElementChild.cloneNode(true);
      items.append(row);
      updateAddButton();
      row.querySelector("input")?.focus();
      return;
    }

    const removeButton = event.target.closest("[data-remove-item]");
    if (!removeButton) return;

    const rows = items.querySelectorAll("[data-repeat-item]");
    const row = removeButton.closest("[data-repeat-item]");
    if (!row) return;
    if (rows.length > 1) {
      row.remove();
    } else {
      row.querySelectorAll("input").forEach((input) => {
        input.value = "";
      });
    }
    updateAddButton();
    items.querySelector("input")?.focus();
  });

  updateAddButton();
}
