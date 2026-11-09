(() => {
  const form = document.querySelector("[data-maintenance]");
  if (!form) return;
  const actions = JSON.parse(document.getElementById("maintenance-actions").textContent);
  const stock = JSON.parse(document.getElementById("maintenance-stock").textContent);
  const count = form.elements["work-TOTAL_FORMS"];
  const addButton = form.querySelector("[data-add-work]");

  function update() {
    const deductions = [];
    for (const line of form.querySelectorAll("[data-work-line]")) {
      const action = actions[line.querySelector('[name$="-action"]').value];
      const item = line.querySelector('[name$="-item"]');
      const quantity = line.querySelector('[name$="-quantity"]');
      line.querySelector("[data-work-material]").hidden = !action?.stock;
      for (const option of item.options) {
        option.disabled = !!(option.value && action?.group && stock[option.value] !== action.group);
      }
      if (!action?.stock || item.selectedOptions[0]?.disabled) item.value = "";
      if (action?.stock && item.value && quantity.value && !line.querySelector('[name$="-DELETE"]').checked) {
        deductions.push(item.selectedOptions[0].textContent.split(" · ").slice(0, -1).join(" · ") + " × " + quantity.value);
      }
    }
    form.querySelector("[data-work-preview]").textContent = deductions.length
      ? "К списанию · " + deductions.join("; ")
      : "Расходники списываются при сохранении работ.";
    addButton.disabled = Number(count.value) >= Number(form.elements["work-MAX_NUM_FORMS"].value);
  }

  addButton.addEventListener("click", () => {
    const template = form.querySelector("[data-work-template]");
    form.querySelector("[data-work-lines]").insertAdjacentHTML(
      "beforeend", template.innerHTML.replaceAll("__prefix__", count.value),
    );
    count.value = Number(count.value) + 1;
    update();
  });
  form.addEventListener("input", update);
  update();
})();
