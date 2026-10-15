(function () {
  "use strict";
  var body = document.body;

  // ---------------------------------------------------------------- القائمة الجانبية (الجوال)
  var toggles = document.querySelectorAll("[data-nav-toggle]");
  function setNav(open) {
    body.classList.toggle("nav-open", open);
    toggles.forEach(function (b) { b.setAttribute("aria-expanded", open ? "true" : "false"); });
  }
  toggles.forEach(function (b) {
    b.addEventListener("click", function () { setNav(!body.classList.contains("nav-open")); });
  });
  document.querySelectorAll("[data-nav-close], .sb-nav a").forEach(function (el) {
    el.addEventListener("click", function () { setNav(false); });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && body.classList.contains("nav-open")) setNav(false);
  });

  // ---------------------------------------------------------------- عدّاد الفلاتر الإضافية
  document.querySelectorAll("details.more-filters").forEach(function (d) {
    var badge = d.querySelector(".fcount");
    if (!badge) return;
    function update() {
      var n = 0;
      d.querySelectorAll("input, select").forEach(function (f) {
        if (f.type !== "checkbox" && f.type !== "radio" && f.value) n++;
      });
      badge.textContent = n;
      badge.hidden = n === 0;
      if (n && !d.dataset.touched) d.open = true;
    }
    d.addEventListener("toggle", function () { d.dataset.touched = "1"; });
    d.addEventListener("input", update);
    d.addEventListener("change", update);
    update();
  });

  // ---------------------------------------------------------------- جداول متجاوبة (بطاقات على الجوال)
  document.querySelectorAll(".table-wrap table").forEach(function (table) {
    var heads = Array.prototype.map.call(table.querySelectorAll("thead th"), function (th) {
      return th.textContent.trim();
    });
    if (!heads.length) return;
    table.classList.add("rtable");
    table.querySelectorAll("tbody tr").forEach(function (tr) {
      Array.prototype.forEach.call(tr.children, function (td, i) {
        if (td.hasAttribute("colspan")) { td.classList.add("wide"); return; }
        if (i === 0) td.classList.add("lead");
        if (heads[i] && !td.hasAttribute("data-label")) td.setAttribute("data-label", heads[i]);
      });
    });
  });

  // ---------------------------------------------------------------- حجم الملفات قبل الرفع
  var maxMb = parseFloat(body.getAttribute("data-max-upload-mb") || "0");
  function totalFileBytes(form) {
    var total = 0;
    form.querySelectorAll('input[type="file"]').forEach(function (inp) {
      Array.prototype.forEach.call(inp.files || [], function (f) { total += f.size; });
    });
    return total;
  }
  function sizeMessage(bytes) {
    return "حجم الملفات المختارة " + (bytes / 1048576).toFixed(1) + " ميغابايت، والحد الأقصى للرفع " + maxMb +
      " ميغابايت. قسّم الملف إلى ملفات أصغر أو احذف الأعمدة والأوراق غير اللازمة.";
  }
  document.querySelectorAll('input[type="file"]').forEach(function (inp) {
    inp.addEventListener("change", function () {
      if (!maxMb || !inp.form) return;
      var bytes = totalFileBytes(inp.form);
      inp.setCustomValidity(bytes > maxMb * 1048576 ? sizeMessage(bytes) : "");
      inp.reportValidity();
    });
  });

  // ---------------------------------------------------------------- منع الإرسال المزدوج
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    if (maxMb && form.enctype === "multipart/form-data") {
      var bytes = totalFileBytes(form);
      if (bytes > maxMb * 1048576) {
        e.preventDefault();
        window.alert(sizeMessage(bytes));
        return;
      }
    }
    var buttons = form.querySelectorAll('button[type="submit"]:not([data-keep]), input[type="submit"]:not([data-keep])');
    setTimeout(function () {
      buttons.forEach(function (b) { b.disabled = true; b.setAttribute("aria-busy", "true"); });
      form.setAttribute("aria-busy", "true");
    }, 0);
  });
  window.addEventListener("pageshow", function () {
    document.querySelectorAll('[aria-busy="true"]').forEach(function (el) {
      el.removeAttribute("aria-busy");
      if ("disabled" in el && el.tagName !== "FORM") el.disabled = false;
    });
  });
})();
