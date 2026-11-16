const state = {
  jobs: [],
  categories: [],
  sort: "created_at",
  direction: "desc",
  rows: new Map(),
};

const $ = (sel) => document.querySelector(sel);

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".panel").forEach((p) => p.classList.add("hidden"));
    $("#" + btn.dataset.tab).classList.remove("hidden");
  });
});

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = "خطا";
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_) { /* keep generic */ }
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  if (!type.includes("json")) return response;
  return response.json();
}

function fillCategories(list) {
  const select = $("#category");
  const q = $("#category-search").value.trim();
  const filtered = list.filter((item) => !q || item.name.includes(q) || item.slug.includes(q));
  select.innerHTML = filtered.slice(0, 400).map((item) => {
    const pad = "— ".repeat(Math.max(0, item.depth - 1));
    return `<option value="${item.slug}">${pad}${item.name}</option>`;
  }).join("");
  const beauty = [...select.options].find((opt) => opt.value === "beauty-and-haircare");
  if (beauty) select.value = beauty.value;
}

async function loadCities(q) {
  const cities = await api("/api/cities?q=" + encodeURIComponent(q || ""));
  const select = $("#city");
  const current = select.value;
  select.innerHTML = cities.map((city) => `<option value="${city.id}">${city.name}</option>`).join("");
  if ([...select.options].some((opt) => opt.value === current)) select.value = current;
  else if ([...select.options].some((opt) => opt.value === "1")) select.value = "1";
}

function renderJobs() {
  const box = $("#job-list");
  const filter = $("#filter-job");
  const selected = filter.value;
  filter.innerHTML = `<option value="">همه کارها</option>` + state.jobs.map(
    (job) => `<option value="${job.id}">${job.name}</option>`
  ).join("");
  filter.value = selected;
  box.innerHTML = state.jobs.map((job) => {
    const counts = job.counts || {};
    return `<article class="job">
      <div>
        <strong>${escapeHtml(job.name)}</strong>
        <span class="status ${job.status}">${statusLabel(job.status)}</span>
        <div class="meta">${escapeHtml(job.category)} · ${escapeHtml(job.city_name)} · صفحه ${job.page}/${job.max_pages}
          · آگهی ${counts.listings || 0} · توضیح ${counts.with_description || 0} · شماره ${counts.with_phone || 0}</div>
        ${job.error ? `<div class="meta">${escapeHtml(job.error)}</div>` : ""}
      </div>
      <div class="row">
        <button data-act="start" data-id="${job.id}">شروع / ادامه</button>
        <button class="ghost" data-act="pause" data-id="${job.id}">مکث</button>
        <button class="ghost" data-act="stop" data-id="${job.id}">توقف</button>
      </div>
    </article>`;
  }).join("") || `<p class="muted">هنوز کاری نیست.</p>`;
}

function phoneStatusLabel(status) {
  return {
    pending: "در صف شماره",
    working: "در حال گرفتن شماره",
    skipped: "رد شد",
    empty: "بدون شماره",
  }[status] || "";
}

function statusLabel(status) {
  return { running: "در حال اجرا", paused: "متوقف موقت", stopped: "متوقف", done: "تمام", error: "خطا", limited: "محدود امروز", expired: "منقضی", active: "فعال" }[status] || status;
}

function browserStatusLabel(status) {
  return {
    active: "وصل شده",
    needs_login: "نیاز به لاگین",
    captcha: "نیاز به کپچا",
    limited: "سقف امروز",
    error: "خطا",
  }[status] || statusLabel(status);
}

function renderBrowserAccounts(list) {
  $("#browser-account-list").innerHTML = list.map((account) => `
    <article class="account ${account.status === "captcha" ? "account-captcha" : ""}">
      <div>
        <strong>${escapeHtml(account.label || ("اکانت " + account.id))}</strong>
        <span class="status ${account.status}">${browserStatusLabel(account.status)}</span>
        <div class="meta">
          ${account.phone ? `<span dir="ltr">${escapeHtml(account.phone)}</span> · ` : "شماره هنوز خوانده نشده · "}
          امروز ${account.views_today} از ${account.daily_cap}
          · تأخیر ${account.delay_min}–${account.delay_max} ثانیه
          ${account.last_error ? "· " + escapeHtml(account.last_error) : ""}
        </div>
        <div class="row account-edit">
          <label>سقف روزانه
            <input type="number" min="1" data-cap-input="${account.id}" value="${Number(account.daily_cap) || 50}" dir="ltr" />
          </label>
          <label>تأخیر از
            <input type="number" min="0" step="1" data-delay-min-input="${account.id}" value="${Number(account.delay_min) || 20}" dir="ltr" />
          </label>
          <label>تا
            <input type="number" min="0" step="1" data-delay-max-input="${account.id}" value="${Number(account.delay_max) || 40}" dir="ltr" />
          </label>
          <button type="button" data-save="${account.id}">ذخیره سقف</button>
        </div>
      </div>
      <div class="row">
        <a class="button-link" href="https://divar2.pixlink.ir/vnc.html" target="_blank" rel="noopener">VNC</a>
        ${account.status === "captcha" ? `<button type="button" data-focus="${account.id}">نمایش کپچا</button>` : ""}
        <button type="button" data-login="${account.id}">لاگین این اکانت</button>
        <button type="button" data-verify="${account.id}">تأیید اتصال</button>
        <button class="ghost" type="button" data-del="${account.id}">حذف</button>
      </div>
    </article>`).join("") || `<p class="muted">هنوز اکانتی نیست. حداقل یکی اضافه و لاگین کن تا گرفتن شماره شروع شود.</p>`;
}

function upsertRow(row) {
  state.rows.set(row.job_id + ":" + row.token, row);
  paintRows();
}

function paintRows() {
  const q = $("#filter").value.trim();
  const job = $("#filter-job").value;
  let rows = [...state.rows.values()];
  if (job) rows = rows.filter((row) => String(row.job_id) === job);
  if (q) {
    rows = rows.filter((row) => [row.title, row.district, row.phone, row.snippet, row.description].join(" ").includes(q));
  }
  const dir = state.direction === "asc" ? 1 : -1;
  rows.sort((a, b) => String(a[state.sort] || "").localeCompare(String(b[state.sort] || ""), "fa") * dir);
  $("#rows").innerHTML = rows.slice(0, 500).map((row) => `<tr>
    <td>${escapeHtml(row.title || "")}</td>
    <td><a href="${row.link}" target="_blank" rel="noopener">آگهی</a></td>
    <td>${escapeHtml(row.district || "")}</td>
    <td>${escapeHtml(row.price_text || "")}</td>
    <td class="snip">${escapeHtml(row.snippet || "")}</td>
    <td dir="ltr">${escapeHtml(row.phone || phoneStatusLabel(row.phone_status))}</td>
    <td>${escapeHtml(row.job_name || "")}</td>
    <td>${escapeHtml((row.created_at || "").replace("T", " ").slice(0, 16))}</td>
  </tr>`).join("");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function appendLog(entry) {
  const line = `[${(entry.created_at || "").slice(11, 19)}] ${entry.message}\n`;
  const log = $("#log");
  log.textContent += line;
  log.scrollTop = log.scrollHeight;
}

function appendTraffic(entry) {
  const time = (entry.created_at || "").slice(11, 19);
  const note = entry.note ? ` · ${entry.note}` : "";
  const line = `[${time}] ${entry.method} ${entry.status} ${entry.elapsed_ms}ms ${entry.url}${note}\n`;
  const log = $("#traffic-log");
  log.textContent += line;
  log.scrollTop = log.scrollHeight;
}

$("#clear-traffic").addEventListener("click", async () => {
  await api("/api/traffic", { method: "DELETE" });
  $("#traffic-log").textContent = "";
});

$("#url-job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const msg = $("#url-job-msg");
  msg.textContent = "در حال خواندن لینک و ساخت کار…";
  try {
    const job = await api("/api/jobs/from-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: form.get("url"),
        name: form.get("name"),
        max_pages: Number(form.get("max_pages") || 5),
        fetch_phones: form.get("fetch_phones") === "on",
        fetch_details: form.get("fetch_details") === "on",
        start: true,
      }),
    });
    msg.textContent = `شروع شد: ${job.name} · ${job.city_name} · ${job.category}`;
    await refreshJobs();
  } catch (err) {
    msg.textContent = err.message;
  }
});

$("#job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const free = String(form.get("category_free") || "").trim();
  const body = {
    name: form.get("name"),
    category: free || form.get("category"),
    keywords: [form.get("keywords")].filter(Boolean),
    city_id: form.get("city_id"),
    title_filter: [form.get("title_filter")].filter(Boolean),
    desc_filter: [form.get("desc_filter")].filter(Boolean),
    max_pages: Number(form.get("max_pages") || 2),
    fetch_phones: form.get("fetch_phones") === "on",
    fetch_details: form.get("fetch_details") === "on",
  };
  try {
    await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    event.target.reset();
    await refreshJobs();
  } catch (err) {
    alert(err.message);
  }
});

$("#job-list").addEventListener("click", async (event) => {
  const btn = event.target.closest("button");
  if (!btn) return;
  await api(`/api/jobs/${btn.dataset.id}/${btn.dataset.act}`, { method: "POST" });
  await refreshJobs();
});

$("#browser-account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  $("#browser-account-msg").textContent = "در حال افزودن…";
  try {
    await api("/api/browser-accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: form.get("label"),
        daily_cap: Number(form.get("daily_cap")),
        delay_min: Number(form.get("delay_min")),
        delay_max: Number(form.get("delay_max")),
      }),
    });
    event.target.reset();
    $("#browser-account-msg").textContent = "اکانت اضافه شد. لاگین و بعد تأیید اتصال را بزن.";
    renderBrowserAccounts(await api("/api/browser-accounts"));
  } catch (err) {
    $("#browser-account-msg").textContent = err.message;
  }
});

$("#browser-account-list").addEventListener("click", async (event) => {
  const btn = event.target.closest("button");
  if (!btn) return;
  const msg = $("#browser-account-msg");
  try {
    if (btn.dataset.save) {
      const id = btn.dataset.save;
      const cap = Number(document.querySelector(`[data-cap-input="${id}"]`)?.value);
      const delayMin = Number(document.querySelector(`[data-delay-min-input="${id}"]`)?.value);
      const delayMax = Number(document.querySelector(`[data-delay-max-input="${id}"]`)?.value);
      msg.textContent = "در حال ذخیره…";
      try {
        const updated = await api(`/api/browser-accounts/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            daily_cap: cap,
            delay_min: delayMin,
            delay_max: delayMax,
          }),
        });
        msg.textContent = `سقف اکانت ذخیره شد: ${updated.daily_cap} در روز`;
        renderBrowserAccounts(await api("/api/browser-accounts"));
      } catch (err) {
        msg.textContent = err.message;
      }
      return;
    }
    if (btn.dataset.focus) {
      msg.textContent = "در حال نمایش اکانت روی VNC…";
      const result = await api(`/api/browser-accounts/${btn.dataset.focus}/focus`, { method: "POST" });
      if (result.vnc_url) window.open(result.vnc_url, "_blank", "noopener");
      msg.textContent = result.message || "آماده.";
      return;
    }
    if (btn.dataset.login) {
      msg.textContent = "در حال باز کردن Chromium این اکانت…";
      const result = await api(`/api/browser-accounts/${btn.dataset.login}/open-login`, { method: "POST" });
      if (result.vnc_url) window.open(result.vnc_url, "_blank", "noopener");
      msg.textContent = result.message || "باز شد.";
      renderBrowserAccounts(await api("/api/browser-accounts"));
      return;
    }
    if (btn.dataset.verify) {
      msg.textContent = "در حال بررسی لاگین…";
      const result = await api(`/api/browser-accounts/${btn.dataset.verify}/verify`, { method: "POST" });
      if (result.needs_captcha) {
        showCaptchaBanner(result);
        msg.textContent = "کپچا آمد؛ در VNC حل کن و دوباره تأیید بزن.";
      } else if (result.ok) {
        msg.textContent = result.account.phone
          ? `وصل شد: ${result.account.phone}`
          : "وصل شد.";
      } else {
        msg.textContent = "هنوز لاگین نیست؛ در VNC وارد شو و دوباره تأیید کن.";
      }
      renderBrowserAccounts(await api("/api/browser-accounts"));
      return;
    }
    if (btn.dataset.del) {
      await api("/api/browser-accounts/" + btn.dataset.del, { method: "DELETE" });
      renderBrowserAccounts(await api("/api/browser-accounts"));
    }
  } catch (err) {
    msg.textContent = err.message;
  }
});

state.captchaAccountId = null;

function showCaptchaBanner(payload) {
  const banner = $("#captcha-banner");
  const link = $("#captcha-vnc");
  const title = $("#captcha-title");
  const detail = $("#captcha-detail");
  const active = Boolean(payload && payload.active);
  banner.classList.toggle("hidden", !active);
  if (!payload) return;
  if (payload.vnc_url) link.href = payload.vnc_url;
  state.captchaAccountId = payload.account_id || null;
  const who = payload.account_phone || payload.account_label || (payload.account_id ? ("اکانت " + payload.account_id) : "");
  if (active && who) {
    title.textContent = `کپچا روی اکانت ${who}`;
    detail.textContent = "VNC را باز کن، کپچای همین اکانت را حل کن، بعد «کپچا حل شد» را بزن. بقیه اکانت‌ها می‌توانند همزمان کار کنند.";
  } else {
    title.textContent = "کپچا آمد";
    detail.textContent = "اول VNC را باز کن و کپچا را روی همان اکانت حل کن، بعد «کپچا حل شد» را بزن.";
  }
}

$("#captcha-focus").addEventListener("click", async () => {
  if (!state.captchaAccountId) return;
  try {
    const result = await api(`/api/browser-accounts/${state.captchaAccountId}/focus`, { method: "POST" });
    if (result.vnc_url) window.open(result.vnc_url, "_blank", "noopener");
  } catch (err) {
    appendLog({ level: "error", message: err.message, created_at: new Date().toISOString() });
  }
});

$("#captcha-done").addEventListener("click", async () => {
  try {
    const result = await api("/api/browser/captcha/resolved", { method: "POST" });
    showCaptchaBanner(result);
    renderBrowserAccounts(await api("/api/browser-accounts"));
  } catch (err) {
    appendLog({ level: "error", message: err.message, created_at: new Date().toISOString() });
  }
});

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const body = Object.fromEntries([...form.entries()].map(([key, value]) => [key, Number(value)]));
  try {
    await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#settings-msg").textContent = "ذخیره شد.";
  } catch (err) {
    $("#settings-msg").textContent = err.message;
  }
});

$("#refresh-cities").addEventListener("click", async () => {
  const result = await api("/api/cities/refresh", { method: "POST" });
  $("#settings-msg").textContent = result.updated
    ? `فهرست شهر به‌روز شد (${result.count}).`
    : `دیوار فهرست تازه‌ای نداد؛ همان فهرست محلی (${result.count}) استفاده می‌شود.`;
  await loadCities($("#city-search").value);
});

$("#category-search").addEventListener("input", () => fillCategories(state.categories));
let cityTimer;
$("#city-search").addEventListener("input", () => {
  clearTimeout(cityTimer);
  cityTimer = setTimeout(() => loadCities($("#city-search").value), 200);
});
$("#filter").addEventListener("input", paintRows);
$("#filter-job").addEventListener("change", paintRows);
document.querySelectorAll("th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (state.sort === key) state.direction = state.direction === "asc" ? "desc" : "asc";
    else { state.sort = key; state.direction = "asc"; }
    paintRows();
  });
});

function exportUrl(ext) {
  const params = new URLSearchParams();
  if ($("#filter-job").value) params.set("job_id", $("#filter-job").value);
  if ($("#filter").value) params.set("q", $("#filter").value);
  return `/api/export.${ext}?` + params.toString();
}
$("#export-csv").addEventListener("click", () => { window.location = exportUrl("csv"); });
$("#export-xlsx").addEventListener("click", () => { window.location = exportUrl("xlsx"); });

async function refreshJobs() {
  state.jobs = await api("/api/jobs");
  renderJobs();
}

function connect() {
  const source = new EventSource("/api/events");
  source.addEventListener("listing", (event) => upsertRow(JSON.parse(event.data)));
  source.addEventListener("log", (event) => appendLog(JSON.parse(event.data)));
  source.addEventListener("traffic", (event) => appendTraffic(JSON.parse(event.data)));
  source.addEventListener("job", (event) => {
    const job = JSON.parse(event.data);
    const index = state.jobs.findIndex((item) => item.id === job.id);
    if (index >= 0) state.jobs[index] = job;
    else state.jobs.unshift(job);
    renderJobs();
  });
  source.addEventListener("browser_account", () => api("/api/browser-accounts").then(renderBrowserAccounts));
  source.addEventListener("captcha", (event) => showCaptchaBanner(JSON.parse(event.data)));
  source.onerror = () => { source.close(); setTimeout(connect, 2000); };
}

async function boot() {
  state.categories = await api("/api/categories");
  fillCategories(state.categories);
  await loadCities("");
  const settings = await api("/api/settings");
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(settings)) {
    if (form.elements[key]) form.elements[key].value = value;
  }
  await refreshJobs();
  const existing = await api("/api/listings?limit=300");
  existing.forEach((row) => state.rows.set(row.job_id + ":" + row.token, row));
  paintRows();
  (await api("/api/logs")).forEach(appendLog);
  (await api("/api/traffic")).forEach(appendTraffic);
  renderBrowserAccounts(await api("/api/browser-accounts"));
  showCaptchaBanner(await api("/api/browser/captcha"));
  connect();
  const tab = (location.hash || "").replace("#", "");
  if (tab) {
    const button = document.querySelector(`.tab[data-tab="${tab}"]`);
    if (button) button.click();
  }
}

boot();
