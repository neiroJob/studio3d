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
    const openDlg = e.target.closest("[data-open-dialog]");
    if (openDlg) {
      const d = document.getElementById(openDlg.dataset.openDialog);
      if (d && d.showModal) d.showModal();
    }
    const closeDlg = e.target.closest("[data-close-dialog]");
    if (closeDlg) {
      const d = closeDlg.closest("dialog");
      if (d) d.close();
    }
    document.querySelectorAll("details.menu-wrap[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
    const renameBtn = e.target.closest("[data-rename-btn]");
    if (renameBtn) {
      const current = renameBtn.getAttribute("data-rename-btn");
      const name = (prompt("Новое имя файла:", current) || "").trim();
      if (name && name !== current) {
        const form = renameBtn.closest("form");
        form.querySelector('input[name="new_name"]').value = name;
        form.requestSubmit();
      }
    }
  });
  document.addEventListener("DOMContentLoaded", function () { apply(current()); });

  // Меню «⋯» (переименовать/удалить файл) — position:fixed, а не absolute: иначе таблица
  // файлов, у которой включён overflow-x:auto (широкие строки), обрезает выпадающее меню или
  // сама обзаводится лишней полосой прокрутки, когда меню открыто (меню входит в прокручиваемую
  // область родителя). Координаты считаем от кнопки при каждом открытии.
  document.addEventListener("toggle", function (e) {
    const d = e.target;
    if (!(d instanceof HTMLDetailsElement) || !d.classList.contains("menu-wrap") || !d.open) return;
    const menu = d.querySelector(".menu");
    const btn = d.querySelector("summary");
    if (!menu || !btn) return;
    const r = btn.getBoundingClientRect();
    menu.style.top = r.bottom + 4 + "px";
    menu.style.left = "auto";
    menu.style.right = window.innerWidth - r.right + "px";
  }, true);
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

// Камера принтера во фрейме (webrtc_iframe, K1C): страница принтера рассчитана на весь экран
// браузера, а не на маленький блок карточки — при обычном 100%-размере её вёрстка не помещается
// и либо обрезается, либо появляются свои полосы прокрутки. Поэтому фрейм рендерится в полный
// размер (ширина/высота заданы в CSS, .cam iframe), а здесь просто уменьшается масштабом под
// размер блока — видно всю страницу целиком, без обрезки и без прокрутки.
(function () {
  function fit(iframe) {
    const box = iframe.parentElement;
    if (!box || !iframe.offsetWidth) return;
    iframe.style.transform = "scale(" + box.clientWidth / iframe.offsetWidth + ")";
  }
  function fitAll() {
    document.querySelectorAll("iframe[data-cam-fit]").forEach(fit);
  }
  document.addEventListener("DOMContentLoaded", fitAll);
  window.addEventListener("resize", fitAll);
})();

// Действия с принтером (пауза, свет, запуск печати, «снял изделие» и т.п.) — обычная отправка
// формы с полной перезагрузкой страницы, без какой-либо обратной связи до её конца: пользователь
// не видит, что команда выполняется, и может решить, что ничего не произошло (особенно если
// принтер отвечает с задержкой — например, K1C долго ставит файл на печать). Помечаем форму
// data-busy="текст", чтобы при отправке сразу заблокировать кнопку и показать спиннер с этим
// текстом — до тех пор, пока браузер не уйдёт на следующую страницу (успех) или не перерисует
// эту же (ошибка), и JS-состояние само не сбросится.
(function () {
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return; // отменено confirm()/другим обработчиком — команда не ушла
    const form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-busy")) return;
    const btn = form.querySelector('button[type="submit"]') || form.querySelector("button");
    if (!btn || btn.disabled) return;
    btn.textContent = form.getAttribute("data-busy") || "Выполняется…";
    btn.disabled = true;
    btn.classList.add("busy");
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

// Загрузка G-code: прогресс «браузер → сервер» по событиям XHR, затем «сервер → принтер» опросом.
(function () {
  function mb(n) { return (n / 1048576).toFixed(1).replace(".", ",") + " МБ"; }
  function setup(form) {
    const box = form.querySelector(".upload-progress");
    const bar = form.querySelector("[data-up-bar]");
    const stage = form.querySelector("[data-up-stage]");
    const text = form.querySelector("[data-up-text]");
    const hint = form.querySelector("[data-up-hint]");
    const cancel = form.querySelector("[data-up-cancel]");
    const submit = form.querySelector("button[type=submit]");
    let xhr = null, poll = null;

    function show(title, sent, total) {
      stage.textContent = title;
      if (total) {
        const pct = Math.min(100, Math.round(sent / total * 100));
        bar.style.width = pct + "%";
        text.textContent = pct + " % · " + mb(sent) + " из " + mb(total);
        box.classList.remove("indeterminate");
      } else {
        text.textContent = "";
        box.classList.add("indeterminate");
      }
    }
    function finish() {
      if (poll) clearInterval(poll);
      poll = null; xhr = null;
      submit.disabled = false;
      cancel.hidden = false;
    }

    const warnBox = form.querySelector(".upload-warn");
    const warnList = form.querySelector("[data-warn-list]");
    let pendingFile = null;

    function readSlice(blob) {
      return new Promise(function (resolve) {
        const r = new FileReader();
        r.onload = function () { resolve(r.result || ""); };
        r.onerror = function () { resolve(""); };
        r.readAsText(blob);
      });
    }
    function precheck(file) {
      // Проверка G-code до загрузки: начало и конец файла, без отправки всего файла.
      const head = file.slice(0, 512 * 1024);
      const tail = file.slice(Math.max(file.size - 128 * 1024, 0));
      return Promise.all([readSlice(head), readSlice(tail)]).then(function (parts) {
        const fd = new FormData();
        fd.append("csrfmiddlewaretoken", form.querySelector("[name=csrfmiddlewaretoken]").value);
        fd.append("head", parts[0]);
        fd.append("tail", parts[1]);
        return fetch(form.dataset.checkUrl, { method: "POST", body: fd, credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (j) { return j.warnings || []; })
          .catch(function () { return []; });
      });
    }

    form.addEventListener("submit", function (e) {
      const file = form.querySelector("input[type=file]").files[0];
      if (!file || !window.XMLHttpRequest || !window.FormData || !window.fetch) return; // обычная отправка формы
      e.preventDefault();
      warnBox.hidden = true;
      submit.disabled = true;
      precheck(file).then(function (warnings) {
        if (warnings.length) {
          warnList.innerHTML = "";
          warnings.forEach(function (w) { const li = document.createElement("li"); li.textContent = w; warnList.appendChild(li); });
          pendingFile = file;
          warnBox.hidden = false;
          submit.disabled = false;
          return;
        }
        upload(file, false);
      });
    });
    form.querySelector("[data-warn-go]").addEventListener("click", function () {
      warnBox.hidden = true;
      if (pendingFile) upload(pendingFile, true);
    });
    form.querySelector("[data-warn-cancel]").addEventListener("click", function () {
      warnBox.hidden = true;
      pendingFile = null;
    });

    function upload(file, confirmed) {
      const id = Math.random().toString(36).slice(2) + Date.now().toString(36);
      const data = new FormData(form);
      data.append("upload_id", id);
      if (confirmed) data.append("confirm_warnings", "1");
      xhr = new XMLHttpRequest();
      xhr.open("POST", form.action);
      xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
      box.hidden = false;
      submit.disabled = true;
      hint.textContent = "Не закрывайте страницу до окончания загрузки.";
      show("Загрузка на сервер", 0, file.size);

      xhr.upload.onprogress = function (ev) {
        if (ev.lengthComputable) show("Загрузка на сервер", ev.loaded, ev.total);
      };
      xhr.upload.onload = function () {
        // Файл на сервере — теперь сервер передаёт его на принтер.
        show("Передача на принтер", 0, 0);
        cancel.hidden = true; // отменить передачу на принтер уже нельзя
        const url = form.dataset.progressUrl.replace("ID", id);
        poll = setInterval(function () {
          fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (p) {
            if (p.total && xhr) show("Передача на принтер", p.sent, p.total);
          }).catch(function () {});
        }, 700);
      };
      xhr.onload = function () {
        let target = null;
        try { target = JSON.parse(xhr.responseText).redirect; } catch (err) { /* не JSON */ }
        finish();
        if (target) { show("Готово", 1, 1); window.location.href = target; }
        else { stage.textContent = "Ошибка"; hint.textContent = "Сервер ответил ошибкой (HTTP " + xhr.status + ")."; }
      };
      xhr.onerror = function () {
        finish();
        stage.textContent = "Ошибка";
        hint.textContent = "Связь с сервером прервалась. Попробуйте ещё раз.";
      };
      xhr.onabort = function () {
        finish();
        box.hidden = true;
      };
      xhr.send(data);
    }

    cancel.addEventListener("click", function () { if (xhr) xhr.abort(); });
  }
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-upload]").forEach(setup);
  });
})();
