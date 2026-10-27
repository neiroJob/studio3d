// Тема: авто → светлая → тёмная. Выбор хранится в браузере.
(function () {
  const order = ["auto", "light", "dark"];
  const svg = function (body) {
    return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + "</svg>";
  };
  const labels = {
    auto: svg('<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>'),
    light: svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4' +
      'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
    dark: svg('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'),
  };
  const titles = { auto: "Тема: как в системе", light: "Тема: светлая", dark: "Тема: тёмная" };

  function current() {
    try { return localStorage.getItem("theme") || "auto"; } catch (e) { return "auto"; }
  }
  function apply(theme) {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.innerHTML = labels[theme];
      b.title = titles[theme];
    });
  }
  document.addEventListener("click", function (e) {
    const themeBtn = e.target.closest("[data-theme-toggle]");
    if (themeBtn) {
      const next = order[(order.indexOf(current()) + 1) % order.length];
      try { localStorage.setItem("theme", next); } catch (err) { /* нет хранилища */ }
      apply(next);
    }
    const passBtn = e.target.closest("[data-show-pass]");
    if (passBtn) {
      const input = passBtn.parentElement.querySelector("input");
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      passBtn.textContent = show ? "скрыть" : "показать";
    }
    if (e.target.closest("[data-menu]")) document.body.classList.toggle("nav-open");
    else if (document.body.classList.contains("nav-open") && !e.target.closest(".side")) {
      document.body.classList.remove("nav-open");
    }
  });
  document.addEventListener("DOMContentLoaded", function () { apply(current()); });
})();

// Превью камер: обновление раз в N секунд, только когда вкладка видна.
(function () {
  function refresh() {
    if (document.hidden) return;
    document.querySelectorAll("img[data-refresh]").forEach(function (img) {
      const base = img.getAttribute("src").split("?")[0];
      const next = new Image();
      next.onload = function () { img.src = next.src; };
      next.src = base + "?t=" + Date.now();
    });
  }
  document.addEventListener("DOMContentLoaded", function () {
    const sec = parseInt(document.body.dataset.cameraRefresh || "10", 10);
    setInterval(refresh, Math.max(sec, 2) * 1000);
  });
})();

// Форма принтера: производитель → модель; порт и мощность из профиля; наработка из проверки связи.
(function () {
  function setup() {
    const man = document.getElementById("f-man");
    const dataEl = document.getElementById("profiles-data");
    if (!man || !dataEl) return;
    const data = JSON.parse(dataEl.textContent);
    const model = document.getElementById(man.dataset.modelsFor);
    const selected = model.value;
    let byId = {};
    Object.keys(data).forEach(function (m) {
      man.add(new Option(m, m));
      data[m].forEach(function (p) { byId[p.id] = Object.assign({ man: m }, p); });
    });
    function fillModels(keep) {
      const list = data[man.value] || [];
      model.innerHTML = "";
      model.add(new Option(man.value ? "Выберите" : "Сначала производитель", ""));
      list.forEach(function (p) { model.add(new Option(p.model + (p.verified ? "" : " (не проверено)"), p.id)); });
      model.disabled = !man.value;
      if (keep) model.value = keep;
    }
    function applyProfile() {
      const p = byId[model.value];
      if (!p) return;
      const port = document.getElementById("id_port");
      const power = document.getElementById("id_power_w");
      if (port) port.placeholder = String(p.port);
      if (power && !power.value && p.power_w) power.value = p.power_w;
    }
    if (selected && byId[selected]) man.value = byId[selected].man;
    else if (Object.keys(data).length === 1) man.value = Object.keys(data)[0];
    fillModels(selected);
    applyProfile();
    man.addEventListener("change", function () { fillModels(""); });
    model.addEventListener("change", applyProfile);
  }
  document.addEventListener("DOMContentLoaded", setup);
  document.addEventListener("htmx:afterSwap", function (e) {
    const box = e.target.querySelector("[data-fill-hours]");
    const field = document.getElementById("id_print_hours_base");
    if (box && field && !field.value) field.value = box.dataset.fillHours.replace(",", ".");
  });
})();
