let bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

function findToken() {
  try {
    for (const key of Object.keys(localStorage)) {
      if (!key.toLowerCase().includes("token")) continue;
      const raw = localStorage.getItem(key) || "";
      let value = raw;
      try {
        const parsed = JSON.parse(raw);
        if (typeof parsed === "string") value = parsed;
        else if (parsed && typeof parsed === "object") {
          value = parsed.token || parsed.access_token || parsed.jwt || "";
        }
      } catch (err) {
        value = raw.replace(/^"|"$/g, "");
      }
      if (value && String(value).startsWith("ey")) return String(value);
    }
  } catch (err) {
    /* ignore */
  }
  return "";
}

function waitBridge(timeoutMs = 2500) {
  return new Promise((resolve) => {
    const started = Date.now();
    const timer = setInterval(() => {
      if (window.AstrBotPluginPage) {
        clearInterval(timer);
        bridge = window.AstrBotPluginPage;
        resolve(bridge);
      } else if (Date.now() - started > timeoutMs) {
        clearInterval(timer);
        resolve(null);
      }
    }, 80);
  });
}

function directUrl(path, params) {
  const qs = new URLSearchParams(params || {}).toString();
  return `/api/plug/memory5/${path}${qs ? `?${qs}` : ""}`;
}

async function directGet(path, params) {
  const tk = findToken();
  const res = await fetch(directUrl(path, params), {
    credentials: "include",
    headers: tk ? { Authorization: `Bearer ${tk}` } : {},
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function directPost(path, payload) {
  const tk = findToken();
  const res = await fetch(directUrl(path), {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(tk ? { Authorization: `Bearer ${tk}` } : {}),
    },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

const state = {
  scopes: [], days: [], dates: {}, tab: "core", trash: [],
  rows: [], shown: 0, selected: new Set(), query: "", lastTab: "", meta: { page: 1, pages: 1, total: 0, page_size: 10 }, renderFn: null, fetchPage: null,
};
const PAGE_SIZE = 10;

// 服务端分页：页面只拿到当前页，翻页重新请求

function setPage(payload, renderFn, fetchPage) {
  state.rows = payload.items || payload.tasks || [];
  state.meta = {
    page: payload.page || 1,
    pages: payload.pages || 1,
    total: payload.total ?? state.rows.length,
    page_size: payload.page_size || PAGE_SIZE,
  };
  state.renderFn = renderFn;
  state.fetchPage = fetchPage;
  renderPage();
}

const CHUNK = 30;

function renderPage() {
  const form = document.getElementById("addForm");
  const formParent = form ? form.parentElement : null;
  if (form) form.remove();
  state.shown = Math.min(state.rows.length, CHUNK);
  if (state.renderFn) state.renderFn(state.rows.slice(0, state.shown));
  if (form && formParent) formParent.prepend(form);
  const { page, pages, total } = state.meta;
  $("pager").classList.toggle("hidden", total === 0);
  $("pageInfo").textContent = `共 ${pages} 页 · 第 ${page}/${pages} 页 · 共 ${total} 条（每页 ${state.meta.page_size}）`;
  $("prevPage").disabled = page <= 1;
  $("nextPage").disabled = page >= pages;
  scheduleHealth();
}

async function goPage(delta) {
  if (!state.fetchPage) return;
  const next = state.meta.page + delta;
  if (next < 1 || next > state.meta.pages) return;
  await state.fetchPage(next);
}

let healthTimer = 0;
function resetSelection() {
  state.selected.clear();
  const all = $("bulkAll");
  if (all) all.checked = false;
  updateBulkBar();
}


function scheduleHealth() {
  clearTimeout(healthTimer);
  healthTimer = setTimeout(() => {
    const card = $("healthCard");
    if (card && !card.classList.contains("hidden")) loadHealth(false);
  }, 800);
}

function updateBulkBar() {
  const count = state.selected.size;
  const el = $("bulkCount");
  if (el) el.textContent = `已选 ${count} 条`;
}

async function runBulk(action, anchor) {
  const scope = $("scope").value;
  const tab = state.tab;
  const day = currentDay();
  const keys = [...state.selected];
  if (!keys.length) {
    tipAt(anchor, "先勾选条目");
    return;
  }
  const items = keys.map((k) => {
    const parts = k.split(":");
    return { day: parts[1] || "", seq: parts[2] };
  });
  const source = tab === "core" ? "core" : (tab === "important" || tab === "remind" ? tab : "daily");
  const payload = { scope, source, day, items, action };
  if (action === "tag") {
    const picked = document.querySelector("input[name=bulkLevel]:checked");
    payload.tag = picked ? picked.value : "";
  }
  const res = await apiPost("bulk", payload);
  if (okOf(res)) {
    const data = (res && res.data) || {};
    tipAt(anchor, action === "delete"
      ? `已删除 ${data.removed || seqs.length} 条`
      : `已调整为 ${payload.tag}（${data.moved || seqs.length} 条）`);
    resetSelection();
    refresh();
  } else {
    tipAt(anchor, "操作失败");
  }
}

function confirmAction(btn, tip, action) {
  if (!btn.dataset.armed) {
    btn.dataset.armed = "1";
    btn.dataset.label = btn.textContent;
    btn.textContent = tip;
    tipAt(btn, tip);
    setTimeout(() => {
      if (btn.dataset.armed) {
        delete btn.dataset.armed;
        btn.textContent = btn.dataset.label;
      }
    }, 4000);
    return;
  }
  delete btn.dataset.armed;
  btn.disabled = true;
  action();
}

function setStatus(msg) {
  $("status").textContent = msg || "";
}

function unwrap(res) {
  if (Array.isArray(res)) return res;
  if (res && typeof res === "object" && "data" in res) return res.data;
  return res;
}

async function apiGet(path, params = {}) {
  if (bridge && bridge.apiGet) {
    try {
      return unwrap(await bridge.apiGet(path, params));
    } catch (err) {
      state.lastError = err && err.message ? err.message : String(err);
      throw err;
    }
  }
  try {
    return unwrap(await directGet(path, params));
  } catch (err) {
    state.lastError = err && err.message ? err.message : String(err);
    throw err;
  }
}

async function apiPost(path, payload) {
  if (bridge && bridge.apiPost) {
    try {
      return unwrap(await bridge.apiPost(path, payload || {}));
    } catch (err) {
      state.lastError = err && err.message ? err.message : String(err);
      throw err;
    }
  }
  try {
    return unwrap(await directPost(path, payload || {}));
  } catch (err) {
    state.lastError = err && err.message ? err.message : String(err);
    throw err;
  }
}

async function loadDays(scope) {
  const res = await apiGet("dates", { scope });
  state.days = unwrap(res) || [];
  return fillDaySelect(state.days);
}

function defaultDay(days, now = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const prev = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const yesterday = `${prev.getFullYear()}-${pad(prev.getMonth() + 1)}-${pad(prev.getDate())}`;
  return days && days.includes(today) ? today : yesterday;
}

function fillDaySelect(days) {
  const input = $("day");
  const all = $("allDates");
  const today = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const max = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
  if (input) {
    input.max = max;
    input.value = defaultDay(days, today);
  }
  if (input && all) input.disabled = all.checked;
  return currentDay();
}

function currentDay() {
  const all = $("allDates");
  if (all && all.checked) return "";
  return $("day") ? $("day").value : "";
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"\']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "\'": "&#39;",
  }[ch]));
}

function tagClass(tag) {
  return ["长期", "重要", "提醒", "低密度"].includes(tag) ? tag : "";
}

function renderEntries(rows, source) {
  const box = $("list");
  box.innerHTML = "";
  if (!rows.length) {
    box.innerHTML = '<div class="card empty">这一项还没有记录。</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const row of rows) {
    const card = document.createElement("div");
    card.className = "card";
    const tag = document.createElement("span");
    tag.className = `tag tag-corner ${tagClass(row.tag)}`;
    tag.textContent = row.tag || "";
    if (row.tag) card.appendChild(tag);
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML =
      `<span>#${esc(row.seq ?? "")}</span>` +
      `<span>${esc(row.subject || "")}</span>` +
      `<span>${esc(row.topic || "")}</span>` +
      `<span>${esc(row.date || "")}</span>` +
      (row.due ? `<span>到期 ${esc(row.due)}</span>` : "");
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = row.text || "";
    const actions = document.createElement("div");
    actions.className = "actions";
    const edit = document.createElement("button");
    edit.textContent = "编辑";
    edit.onclick = () => startEdit(card, row, source);
    const del = document.createElement("button");
    del.className = "danger";
    del.textContent = "删除";
    del.onclick = () => confirmAction(del, "确认删除？", () => removeEntry(row, source, card));
    actions.append(edit, del);
    if (state.bulkAllowed && ["core", "important", "remind", "normal", "low"].includes(state.tab)) {
      const pick = document.createElement("input");
      pick.type = "checkbox";
      pick.className = "pick";
      pick.checked = state.selected.has(`${state.tab}:${row.date || ""}:${row.seq}`);
      pick.onchange = () => {
        const key = `${state.tab}:${row.date || ""}:${row.seq}`;
        if (pick.checked) state.selected.add(key);
        else state.selected.delete(key);
        updateBulkBar();
      };
      actions.appendChild(pick);
    }
    card.append(meta, text, actions);
    frag.appendChild(card);
  }
  box.appendChild(frag);
}

function startEdit(card, row, source) {
  if (card.querySelector("textarea")) return;
  const area = document.createElement("textarea");
  area.value = row.text || "";
  const actions = document.createElement("div");
  actions.className = "actions";
  const save = document.createElement("button");
  save.textContent = "保存";
  save.onclick = async () => {
    save.disabled = true;
    let res;
    try {
      res = await apiPost("update", {
        scope: $("scope").value,
        source: source,
        day: row.date,
        seq: row.seq,
        index: row.seq,
        text: area.value.trim(),
      });
    } catch (err) {
      res = { status: "error", message: err.message };
    }
    save.disabled = false;
    if (okOf(res)) {
      setStatus("已保存");
      refresh();
    } else {
      setStatus((res && res.message) || "保存失败");
    }
  };
  const cancel = document.createElement("button");
  cancel.textContent = "取消";
  cancel.onclick = () => card.querySelector("textarea").remove();
  actions.append(save, cancel);
  card.append(area, actions);
}

function okOf(res) {
  return !(res && typeof res === "object" && res.status === "error");
}

function showUndo(message, onUndo) {
  const el = $("status");
  el.textContent = "";
  const span = document.createElement("span");
  span.textContent = message + " ";
  const btn = document.createElement("button");
  btn.textContent = "撤销";
  btn.onclick = async () => {
    try {
      await onUndo();
      el.textContent = "已撤销";
    } catch (err) {
      el.textContent = "撤销失败：" + (err && err.message ? err.message : err);
    }
  };
  el.append(span, btn);
}

async function removeEntry(row, source, card) {
  const parent = card.parentNode;
  const next = card.nextSibling;
  card.remove(); // 先移除，失败再插回去，避免等网络
  const res = await apiPost("delete", {
    scope: $("scope").value,
    source: source,
    day: row.date,
    seq: row.seq,
    index: row.seq,
  }).catch((err) => ({ status: "error", message: err.message }));

  if (!okOf(res)) {
    parent.insertBefore(card, next);
    setStatus((res && res.message) || "删除失败");
    return;
  }
  const id = (res && res.id) || (res && res.data && res.data.id);
  if (id) {
    showUndo("已移入回收站", async () => {
      await apiPost("restore", { id });
      refresh();
    });
  } else {
    setStatus("已移入回收站");
  }
}

async function renderTrash() {
  const box = $("trashBox");
  $("list").classList.add("hidden");
  box.classList.remove("hidden");
  const load = async (page) => {
    const res = await apiGet("trash", { page });
    const payload = unwrap(res) || {};
    if (!payload.total) {
      box.innerHTML = '<div class="card empty">回收站里没有内容。</div>';
      $("pager").classList.add("hidden");
      return;
    }
    setPage(payload, renderTrashCards, load);
    const bar = document.createElement("div");
    bar.className = "card";
    const clear = document.createElement("button");
    clear.className = "danger";
    clear.textContent = "清空回收站";
    clear.onclick = async () => {
      if (!clear.dataset.armed) {
        clear.dataset.armed = "1";
        clear.textContent = "确认清空全部？";
        setTimeout(() => {
          if (clear.dataset.armed) {
            delete clear.dataset.armed;
            clear.textContent = "清空回收站";
          }
        }, 4000);
        return;
      }
      delete clear.dataset.armed;
      clear.disabled = true;
      const res = await apiPost("trash-clear", {});
      toast(okOf(res) ? `已清空回收站（${(res && res.data && res.data.removed) || 0} 项）` : "清空失败");
      await load(1);
    };
    bar.appendChild(clear);
    $("trashBox").prepend(bar);
  };
  await load(1);
}

function renderTrashCards(rows) {
  const box = $("trashBox");
  box.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const item of rows) {
    const card = document.createElement("div");
    card.className = "card";
    const tagVal = (item.entry && item.entry.tag) || item.tag || "";
    if (tagVal) {
      const tag = document.createElement("span");
      tag.className = `tag tag-corner ${tagClass(tagVal)}`;
      tag.textContent = tagVal;
      card.appendChild(tag);
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${item.scope} · ${item.source} · ${item.day || ""}`;
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = (item.entry && item.entry.text) || "";
    const actions = document.createElement("div");
    actions.className = "actions";
    const restore = document.createElement("button");
    restore.textContent = "恢复";
    restore.onclick = async () => {
      await apiPost("restore", { id: item.id });
      setStatus("已恢复");
      refresh();
    };
    const purge = document.createElement("button");
    purge.className = "danger";
    purge.textContent = "彻底删除";
    purge.onclick = async () => {
      if (!purge.dataset.armed) {
        purge.dataset.armed = "1";
        purge.textContent = "确认彻底删除？";
        setTimeout(() => {
          if (purge.dataset.armed) {
            delete purge.dataset.armed;
            purge.textContent = "彻底删除";
          }
        }, 4000);
        return;
      }
      delete purge.dataset.armed;
      purge.textContent = "彻底删除";
      await apiPost("purge", { id: item.id });
      setStatus("已彻底删除");
      refresh();
    };
    actions.append(restore, purge);
    card.append(meta, text, actions);
    frag.appendChild(card);
  }
  box.appendChild(frag);
}

function field(labelText, type, placeholder) {
  const wrap = document.createElement("label");
  wrap.textContent = labelText;
  const input = document.createElement(type === "textarea" ? "textarea" : "input");
  if (type !== "textarea") input.type = type;
  if (placeholder) input.placeholder = placeholder;
  wrap.appendChild(input);
  return { wrap, input };
}

async function renderTasks() {
  const box = $("trashBox");
  $("list").classList.add("hidden");
  box.classList.remove("hidden");
  const load = async (page) => {
    const res = await apiGet("tasks", { page });
    const payload = unwrap(res) || {};
    box.innerHTML = "";
    if (payload.usage && payload.limits) {
      const u = payload.usage;
      const l = payload.limits;
      const bar = document.createElement("div");
      bar.className = "card meta";
      bar.textContent =
        `已用容量：文字 ${u.text || 0}/${l.text}MB　图片 ${u.image || 0}/${l.image}MB　`
        + `语音 ${u.voice || 0}/${l.voice}MB　视频 ${u.video || 0}/${l.video}MB`;
      box.appendChild(bar);
    }
    if (!payload.total) {
      const empty = document.createElement("div");
      empty.className = "card empty";
      empty.textContent = "还没有定时任务，点右上角「新增」。";
      box.appendChild(empty);
      $("pager").classList.add("hidden");
      return;
    }
    setPage(payload, renderTaskCards, load);
  };
  await load(1);
}

function renderTaskCards(tasks) {
  const box = $("trashBox");
  const old = box.querySelectorAll(".card:not(.meta)");
  for (const card of old) card.remove();
  const frag = document.createDocumentFragment();
  for (const task of tasks) {
    const card = document.createElement("div");
    card.className = "card";
    const meta = document.createElement("div");
    meta.className = "meta";
    const kindName = { once: "一次性", daily: "每天", weekly: "每周" }[task.kind] || task.kind;
    const msgName = { text: "文字", image: "图片", voice: "语音", video: "视频" }[task.msg_type || "text"];
    const when = task.kind === "once" ? `${task.date} ${task.at}` : task.at;
    const repeat = task.kind === "daily" && task.repeat_days > 0 ? `连续 ${task.repeat_days} 天` : "";
    meta.innerHTML =
      `<span class="tag">${kindName}</span>` +
      `<span class="tag">${msgName}</span>` +
      `<span>${when}</span>` +
      (repeat ? `<span>${repeat}</span>` : "") +
      `<span>${task.scope}</span>` +
      `<span>${task.enabled ? "启用" : "停用"}</span>`;
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = `${task.name}：${task.message}`;
    const actions = document.createElement("div");
    actions.className = "actions";
    const edit = document.createElement("button");
    edit.textContent = "修改";
    edit.onclick = () => showTaskForm(task);
    const toggle = document.createElement("button");
    toggle.textContent = task.enabled ? "停用" : "启用";
    toggle.onclick = async () => {
      await apiPost("task-toggle", { id: task.id, enabled: !task.enabled });
      setStatus(task.enabled ? "已停用" : "已启用");
      refresh();
    };
    const del = document.createElement("button");
    del.className = "danger";
    del.textContent = "删除";
    del.onclick = async () => {
      if (!del.dataset.armed) {
        del.dataset.armed = "1";
        del.textContent = "确认删除？";
        setTimeout(() => {
          if (del.dataset.armed) {
            delete del.dataset.armed;
            del.textContent = "删除";
          }
        }, 4000);
        return;
      }
      await apiPost("task-delete", { id: task.id });
      setStatus("已删除任务");
      refresh();
    };
    actions.append(edit, toggle, del);
    card.append(meta, text, actions);
    frag.appendChild(card);
  }
  box.appendChild(frag);
}

function showTaskForm(editing) {
  const existing = document.getElementById("addForm");
  if (existing && !editing) return;
  if (existing) existing.remove();
  document.querySelectorAll(".empty").forEach((el) => el.remove());
  const card = document.createElement("div");
  card.className = "card";
  card.id = "addForm";
  const title = document.createElement("div");
  title.className = "meta";
  title.textContent = editing ? `修改任务：${editing.name}` : "新增定时任务";
  card.appendChild(title);

  const name = field("名称", "text", "例如 早睡提醒");
  const scope = field("目标会话", "text", "group:群号 或 private:QQ号");
  const message = field("内容", "textarea", "到点发送的文字（发媒体时可留空）");
  const msgTypeWrap = document.createElement("label");
  msgTypeWrap.textContent = "消息类型";
  const msgType = document.createElement("select");
  msgType.innerHTML = '<option value="text">纯文字</option><option value="image">图片</option>'
    + '<option value="voice">语音</option><option value="video">视频</option>';
  msgTypeWrap.appendChild(msgType);
  const mediaWrap = document.createElement("label");
  mediaWrap.textContent = editing && editing.media ? `媒体文件（当前：${editing.media}，不选则保留）` : "媒体文件";
  const mediaInput = document.createElement("input");
  mediaInput.type = "file";
  mediaInput.accept = "image/*,audio/*,video/*";
  mediaWrap.appendChild(mediaInput);
  mediaWrap.classList.add("hidden");
  msgType.onchange = () => {
    mediaWrap.classList.toggle("hidden", msgType.value === "text");
  };
  const at = field("时间", "time");
  const kindWrap = document.createElement("label");
  kindWrap.textContent = "类型";
  const kind = document.createElement("select");
  kind.innerHTML = '<option value="daily">每天</option><option value="once">一次性</option>'
    + '<option value="weekly">每周</option>';
  kindWrap.appendChild(kind);
  const date = field("起始日期", "date");
  const repeatDays = field("连续天数（0=不限）", "number", "0");
  const weekdays = field("星期（1=周一…7=周日）", "text", "1,3,5");

  card.append(name.wrap, scope.wrap, message.wrap, msgTypeWrap, mediaWrap, kindWrap,
              at.wrap, date.wrap, repeatDays.wrap, weekdays.wrap);
  date.wrap.classList.add("hidden");
  repeatDays.wrap.classList.add("hidden");
  weekdays.wrap.classList.add("hidden");
  kind.onchange = () => {
    date.wrap.classList.toggle("hidden", kind.value === "weekly");
    repeatDays.wrap.classList.toggle("hidden", kind.value !== "daily");
    weekdays.wrap.classList.toggle("hidden", kind.value !== "weekly");
  };

  if (editing) {
    name.input.value = editing.name || "";
    scope.input.value = editing.scope || "";
    message.input.value = editing.message || "";
    msgType.value = editing.msg_type || "text";
    at.input.value = editing.at || "";
    kind.value = editing.kind || "daily";
    date.input.value = editing.date || "";
    repeatDays.input.value = editing.repeat_days || 0;
    weekdays.input.value = (editing.weekdays || []).join(",");
    kind.onchange();
    if (msgType.value !== "text") mediaWrap.classList.remove("hidden");
  }

  const actions = document.createElement("div");
  actions.className = "actions";
  const save = document.createElement("button");
  save.textContent = "保存";
  save.onclick = async () => {
    if (save.disabled) return;
    save.disabled = true;
    save.textContent = "保存中…";
    const payload = {
      name: name.input.value.trim(),
      scope: scope.input.value.trim() || $("scope").value,
      message: message.input.value.trim(),
      kind: kind.value,
      at: at.input.value,
      date: date.input.value,
      repeat_days: Number(repeatDays.input.value || 0),
      weekdays: weekdays.input.value.split(/[,，\s]+/).filter(Boolean),
      msg_type: msgType.value,
      media: (editing && editing.media) || "",
    };
    if (editing) payload.id = editing.id;
    try {
      const file = mediaInput.files[0];
      if (msgType.value !== "text") {
        if (!file) throw new Error("选了媒体类型但没选文件");
        if (!bridge || !bridge.upload) throw new Error("面板桥接不可用，无法上传");
        const up = unwrap(await bridge.upload("task-media", file));
        if (!okOf(up)) throw new Error((up && up.message) || "上传失败");
        payload.media = (up && up.media) || (up && up.data && up.data.media) || "";
      }
      const res = await apiPost("task-save", payload);
      if (okOf(res)) {
        setStatus("已新增定时任务");
        card.remove();
        refresh();
      } else {
        setStatus((res && res.message) || "保存失败");
      }
    } catch (err) {
      setStatus(`保存失败：${err && err.message ? err.message : err}`);
    } finally {
      save.disabled = false;
      save.textContent = "保存";
    }
  };
  const cancel = document.createElement("button");
  cancel.textContent = "取消";
  cancel.onclick = () => card.remove();
  actions.append(save, cancel);
  card.appendChild(actions);
  // 定时页签的内容在 trashBox 里，表单必须加到这里，否则会被隐藏的 list 挡住
  $("list").classList.add("hidden");
  $("trashBox").classList.remove("hidden");
  $("trashBox").prepend(card);
}

function showAddForm() {
  const tab = state.tab;
  if (tab === "tasks") {
    showTaskForm();
    return;
  }
  if (!["core", "important", "remind"].includes(tab)) return;
  if (document.getElementById("addForm")) return;
  document.querySelectorAll(".empty").forEach((el) => el.remove());
  const names = { core: "长期记忆", important: "重要记忆", remind: "提醒" };
  const card = document.createElement("div");
  card.className = "card";
  card.id = "addForm";
  const title = document.createElement("div");
  title.className = "meta";
  title.textContent = `新增${names[tab]}`;
  card.appendChild(title);

  const qq = field("主体 QQ", "text", "填对方的 QQ 号");
  const topic = field("主题", "text", "2~6 个字，例如 称呼");
  const body = field("内容", "textarea", "一句话，主谓宾完整");
  card.append(qq.wrap, topic.wrap, body.wrap);

  let due = null;
  let fallback = null;
  let repeatSel = null;
  if (tab === "remind") {
    due = field("到期时间", "datetime-local");
    card.appendChild(due.wrap);
    const repeatWrap = document.createElement("label");
    repeatWrap.textContent = "重复";
    const repeat = document.createElement("select");
    repeat.innerHTML = '<option value="">不重复</option><option value="daily">每天</option>'
      + '<option value="weekly">每周</option><option value="weekdays">工作日</option>';
    repeatWrap.appendChild(repeat);
    card.appendChild(repeatWrap);
    const fbWrap = document.createElement("label");
    fbWrap.textContent = "过期后";
    const fb = document.createElement("select");
    fb.innerHTML = '<option value="important">回落到重要</option>'
      + '<option value="normal">回落到普通</option>'
      + '<option value="delete">直接删除</option>';
    fbWrap.appendChild(fb);
    fallback = fb;
    card.appendChild(fbWrap);
    repeatSel = repeat;
  }

  const actions = document.createElement("div");
  actions.className = "actions";
  const save = document.createElement("button");
  save.textContent = "保存";
  save.onclick = async () => {
    if (save.disabled) return;
    save.disabled = true;
    save.textContent = "保存中…";
    const payload = {
      scope: $("scope").value,
      source: tab,
      subject: qq.input.value.trim(),
      topic: topic.input.value.trim(),
      text: body.input.value.trim(),
    };
    if (due) payload.due = due.input.value;
    if (fallback) payload.fallback = fallback.value;
    if (repeatSel) payload.repeat = repeatSel.value;
    let res;
    try {
      res = await apiPost("add", payload);
    } catch (err) {
      res = { status: "error", message: err.message };
    }
    save.disabled = false;
    save.textContent = "保存";
    if (okOf(res)) {
      setStatus("已新增");
      card.remove();
      refresh();
    } else {
      setStatus((res && res.message) || "新增失败");
    }
  };
  const cancel = document.createElement("button");
  cancel.textContent = "取消";
  cancel.onclick = () => card.remove();
  actions.append(save, cancel);
  card.appendChild(actions);
  $("list").prepend(card);
}

async function refresh() {
  const scope = $("scope").value;
  if (!scope) {
    setStatus("还没有任何会话");
    return;
  }
  const tab = state.tab;
  const day = currentDay();
  const hideDay = tab === "trash" || tab === "tasks";
  $("day").disabled = hideDay || $("allDates").checked;
  $("allDates").parentElement.classList.toggle("hidden", hideDay);
  // 定时/回收站页签用不到日期，直接把整个日期控件藏起来，免得灰着难看
  if ($("day").parentElement) {
    $("day").parentElement.classList.toggle("hidden", hideDay);
  }
  const memoryTab = ["core", "important", "remind", "normal", "low"].includes(tab);
  if (state.lastTab !== tab) {
    resetSelection();
    state.lastTab = tab;
  }
  const bulkAllowed = memoryTab;
  state.bulkAllowed = bulkAllowed;
  $("searchRow").classList.toggle("hidden", !memoryTab);
  $("bulkTop").classList.toggle("hidden", !bulkAllowed);
  $("bulkBottom").classList.toggle("hidden", !bulkAllowed);
  if (!bulkAllowed) $("bulkPanel").classList.add("hidden");
  updateBulkBar();
  const canAdd = ["core", "important", "remind", "tasks"].includes(tab);
  $("addBtn").classList.toggle("hidden", !canAdd);
  const openForm = document.getElementById("addForm");
  if (openForm && !canAdd) openForm.remove();
  const sideBox = tab === "trash" || tab === "tasks";
  $("list").classList.toggle("hidden", sideBox);
  $("trashBox").classList.toggle("hidden", !sideBox);

  if (tab === "tasks") {
    await renderTasks();
    return;
  }
  if (tab === "aliases") {
    await renderAliases(scope);
    return;
  }
  if (tab === "exports") {
    await renderExportHistory();
    return;
  }
  if (tab === "trash") {
    await renderTrash();
    return;
  }
  if (["core", "important", "remind", "normal", "low"].includes(tab)) {
    const load = async (page) => {
      const res = await apiGet("page", {
        scope, tab, day: day || "", page, q: state.query,
      });
      const payload = unwrap(res) || {};
      if (Array.isArray(payload.dates)) {
        const known = state.dates[scope] || [];
        if (JSON.stringify(payload.dates) !== JSON.stringify(known)) {
          state.dates[scope] = payload.dates;
          fillDaySelect(payload.dates);
        }
      }
      if (!payload.total) {
        const text = tab === "core" ? "长期记忆还没有内容。" : "这一项还没有记录。";
        $("list").innerHTML = `<div class="card empty">${text}</div>`;
        $("pager").classList.add("hidden");
        return;
      }
      const source = tab === "core" ? "core"
        : (tab === "normal" || tab === "low" ? "daily" : tab);
      setPage(payload, (rows) => renderEntries(rows, source), load);
    };
    await load(1);
    return;
  }
}

async function boot() {
  // iframe 是 sandbox（无 allow-same-origin），只能走桥接；直接打开则走直连
  const b = await waitBridge(3000);
  bridge = b;
  if (b) {
    try {
      await Promise.race([
        b.ready(),
        new Promise((_, reject) => setTimeout(() => reject(new Error("bridge timeout")), 3000)),
      ]);
    } catch (err) {
      setStatus("面板桥接未就绪，尝试直连…");
      bridge = null;
    }
  }
  setStatus("加载中…");
  loadHealth(false);
  // 首屏静默同步运行状态按钮（是否可用），不展开面板
  const boot = unwrap(await apiGet("bootstrap")) || {};
  const scopes = boot.scopes || [];
  if (!scopes.length) {
    setStatus(`没有读到记忆数据${state.lastError ? "：" + state.lastError : ""}`);
    return;
  }
  state.scopes = scopes;
  const sel = $("scope");
  sel.innerHTML = "";
  for (const item of scopes) {
    const opt = document.createElement("option");
    opt.value = item.scope;
    opt.textContent = `${item.scope}（${item.days} 天）`;
    sel.appendChild(opt);
  }
  const first = scopes[0].scope;
  sel.value = first;
  state.dates[first] = boot.dates?.[first] || [];
  fillDaySelect(state.dates[first]);
  await refresh();
  setStatus("");

  const debounce = (fn, wait = 120) => {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  };

  sel.onchange = debounce(async () => {
    const scope = sel.value;
    if (!state.dates[scope]) {
      setStatus("加载日期…");
      const days = await loadDays(scope);
      state.dates[scope] = state.days;
      fillDaySelect(days);
    } else {
      fillDaySelect(state.dates[scope]);
    }
    await refresh();
    setStatus("");
  });
  $("day").onchange = debounce(() => { resetSelection(); refresh(); });
  $("allDates").addEventListener("change", () => {
    $("day").disabled = $("allDates").checked || $("day").parentElement.classList.contains("hidden");
    resetSelection();
    refresh();
  });
  $("reload").onclick = async () => {
    state.dates = {};
    const res = unwrap(await apiGet("bootstrap")) || {};
    state.dates[sel.value] = res.dates?.[sel.value] || [];
    fillDaySelect(state.dates[sel.value]);
    await refresh();
  };
  $("addBtn").onclick = showAddForm;
  $("exportBtn").onclick = doExport;
  $("exportHistoryBtn").onclick = async () => {
    state.tab = "exports";
    for (const other of document.querySelectorAll(".tab")) other.classList.remove("active");
    await refresh();
  };
  $("query").oninput = () => {
    const value = $("query").value.trim();
    state.query = value;
    resetSelection();
    clearTimeout($("query")._timer);
    $("query")._timer = setTimeout(() => refresh(), 250);
  };
  $("bulkAll").onchange = () => {
    for (const box of document.querySelectorAll("#list .pick")) {
      box.checked = $("bulkAll").checked;
      box.onchange();
    }
  };
  $("bulkAdjust").onclick = () => {
    const panel = $("bulkPanel");
    if (!panel.classList.contains("hidden")) {
      panel.classList.add("hidden");
      return;
    }
    if (!state.selected.size) {
      tipAt($("bulkAdjust"), "先勾选条目");
      return;
    }
    $("bulkPanelCount").textContent = `已选 ${state.selected.size} 条`;
    panel.classList.remove("hidden");
    $("bulkConfirm").disabled = true;
    $("bulkConfirm").textContent = "请选择等级";
    for (const r of document.querySelectorAll("input[name=bulkLevel]")) r.checked = false;
  };
  for (const radio of document.querySelectorAll("input[name=bulkLevel]")) {
    radio.onchange = () => {
      $("bulkConfirm").disabled = false;
      $("bulkConfirm").textContent = `确认调整为 ${radio.value}`;
    };
  }
  $("bulkCancel").onclick = () => $("bulkPanel").classList.add("hidden");
  $("bulkConfirm").onclick = () => {
    const btn = $("bulkConfirm");
    $("bulkPanel").classList.add("hidden");
    $("bulkAll").checked = false;
    runBulk("tag", btn);
  };
  $("bulkDelete").onclick = () => {
    const btn = $("bulkDelete");
    if (!btn.dataset.armed) {
      btn.dataset.armed = "1";
      btn.textContent = "确认删除？";
      setTimeout(() => {
        if (btn.dataset.armed) {
          delete btn.dataset.armed;
          btn.textContent = "删除";
        }
      }, 4000);
      return;
    }
    delete btn.dataset.armed;
    btn.textContent = "删除";
    runBulk("delete", btn);
  };
  $("selfcheckBtn").onclick = async () => {
    $("selfcheckOut").textContent = "自检中…";
    const res = await apiPost("selfcheck", {});
    const data = unwrap(res) || {};
    const lines = (data.lines || []).join("；");
    $("selfcheckOut").textContent = `${data.ok ? "自检通过" : "自检异常"} · ${data.path || ""} · ${lines}`;
  };
  $("healthBtn").onclick = async () => {
    const card = $("healthCard");
    if (!card) return;
    if (card.classList.contains("hidden")) {
      await loadHealth(true);
      card.scrollIntoView({ behavior: "smooth", block: "end" });
    } else {
      card.classList.add("hidden");
    }
  };
  window.addEventListener("scroll", () => {
    if (!state.renderFn || state.shown >= state.rows.length) return;
    const nearBottom =
      window.innerHeight + window.scrollY >= document.body.offsetHeight - 240;
    if (!nearBottom) return;
    state.shown = Math.min(state.rows.length, state.shown + CHUNK);
    state.renderFn(state.rows.slice(0, state.shown));
  });
  loadHealth(false);
  setInterval(() => {
    if (document.hidden) return;
    const card = $("healthCard");
    if (card && !card.classList.contains("hidden")) loadHealth(false);
  }, 120000);
  $("prevPage").onclick = () => goPage(-1);
  $("nextPage").onclick = () => goPage(1);
  for (const btn of document.querySelectorAll(".tab")) {
    btn.onclick = async () => {
      for (const other of document.querySelectorAll(".tab")) other.classList.remove("active");
      btn.classList.add("active");
      state.tab = btn.dataset.tab;
      await refresh();
    };
  }
}


// ------------------------------------------------------------ 健康 / 导出 / 别名

async function loadHealth(show = false) {
  const card = $("healthCard");
  const box = $("healthBody");
  const btn = $("healthBtn");
  if (!card || !box) return;
  let data = {};
  try {
    data = unwrap(await apiGet("health")) || {};
    try {
      const latest = unwrap(await apiGet("selfcheck/latest")) || {};
      if (latest.path && $("selfcheckOut")) $("selfcheckOut").textContent = `上次自检：${latest.path}`;
    } catch (_) {}

  } catch (err) {
    if (btn) btn.disabled = false;
    if (show) {
      card.classList.remove("hidden");
      box.innerHTML = '<div class="item"><b>状态</b><span>读取失败</span></div>';
    }
    return;
  }
  if (data.panel_enabled === false) {
    card.classList.add("hidden");
    if (btn) {
      btn.disabled = true;
      btn.title = "设置里已关闭运行状态";
    }
    return;
  }
  if (btn) {
    btn.disabled = false;
    btn.title = "显示运行状态";
  }
  if (show) card.classList.remove("hidden");

  const hb = data.heartbeat || {};
  const hbText = hb.age == null ? "暂无心跳" : `${hb.age}s 前 · 延迟 ${hb.lag || 0}s`;
  const enc = data.encryption || {};
  const cache = data.cache || {};
  const backup = data.backup || {};
  const verify = backup.last_verify || {};
  const policy = backup.policy || {};
  const scopes = data.scopes || [];
  const keyAge = enc.key_age_hours == null ? "未知" : `${enc.key_age_hours} 小时`;
  const verifyText = verify.checked_at
    ? `${verify.ok ? "通过" : "异常"} · ${verify.checked_at} · ${verify.file || ""}`
    : "暂无记录";
  const policyText = policy.enabled === false ? "已关闭" : `每 ${policy.period_days || "-"} 天`;

  const totals = scopes.reduce(
    (acc, s) => {
      acc.core += s.core || 0;
      acc.important += s.important || 0;
      acc.remind += s.remind || 0;
      acc.daily += s.daily || 0;
      return acc;
    },
    { core: 0, important: 0, remind: 0, daily: 0 },
  );

  const scopeText = scopes.length
    ? scopes.map((s) => s.scope).join("<br>")
    : "暂无会话";
  const cacheByScope = (cache.by_scope || [])
    .map((b) => `${b.scope} · ${b.files} 个文件 / ${Math.max(1, Math.round(b.bytes / 1024))}KB`)
    .join("<br>") || "缓存为空";

  box.innerHTML =
    `<div class="item"><b>版本</b><span>v${data.version || "-"} · ${hbText}</span></div>`
    + `<div class="item"><b>会话</b><span>${scopes.length} 个（群/私聊）</span>`
    + `<span class="muted">${scopeText}</span></div>`
    + `<div class="item"><b>加密</b><span>${enc.mode || "-"}（${enc.format || "-"}）</span>`
    + `<span class="muted">密钥已用 ${keyAge}</span></div>`
    + `<div class="item"><b>缓存（按会话）</b><span>${cache.files || 0} 个文件 / ${Math.round((cache.bytes || 0) / 1024)}KB</span>`
    + `<span class="muted">${cacheByScope}</span></div>`
    + `<div class="item"><b>条目</b><span>长期 ${totals.core} · 重要 ${totals.important}</span>`
    + `<span class="muted">提醒 ${totals.remind}</span></div>`
    + `<div class="item"><b>备份校验</b><span>${policyText}</span>`
    + `<span class="muted">${verifyText}</span></div>`
    ;
}

function tipAt(anchor, msg, ms = 2200) {
  if (!anchor || !anchor.getBoundingClientRect) {
    toast(msg, ms);
    return;
  }
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.textContent = msg;
  document.body.appendChild(tip);
  const rect = anchor.getBoundingClientRect();
  const width = Math.min(240, window.innerWidth - 16);
  tip.style.maxWidth = `${width}px`;
  tip.style.left = `${Math.max(8, Math.min(window.innerWidth - width - 8, rect.left))}px`;
  tip.style.top = `${rect.bottom + 6}px`;
  setTimeout(() => tip.remove(), ms);
}

function toast(msg, ms = 2500) {
  setStatus(msg);
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => setStatus(""), ms);
}

function deviceId() {
  try {
    let id = localStorage.getItem("memory5_device_id");
    if (!id) {
      id = "PC-" + Math.random().toString(16).slice(2, 10);
      localStorage.setItem("memory5_device_id", id);
    }
    return id;
  } catch (err) {
    return "PC-unknown";
  }
}

async function doExport() {
  const scope = $("scope").value;
  if (!scope) return;
  try {
    const data = unwrap(await apiGet("export", { scope, format: "json", device: deviceId() })) || {};
    const blob = new Blob([data.content || ""], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = data.filename || "memory5-export.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast("已导出（记录在运行状态里）");
    loadHealth(false);
  } catch (err) {
    toast("导出失败：" + (err && err.message ? err.message : err), 4000);
  }
}

async function renderAliases(scope) {
  const box = $("list");
  box.innerHTML = "";
  $("trashBox").classList.add("hidden");
  const groups = unwrap(await apiGet("aliases", { scope })) || [];

  const addCard = document.createElement("div");
  addCard.className = "card";
  const addTitle = document.createElement("div");
  addTitle.className = "meta";
  addTitle.textContent = "新增别名";
  const addRow = document.createElement("div");
  addRow.className = "actions";
  const qqInput = document.createElement("input");
  qqInput.type = "text";
  qqInput.placeholder = "QQ号";
  qqInput.style.maxWidth = "140px";
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.placeholder = "别名，最多12字";
  nameInput.maxLength = 12;
  nameInput.style.maxWidth = "160px";
  const confInput = document.createElement("input");
  confInput.type = "number";
  confInput.step = "0.1";
  confInput.min = "0";
  confInput.max = "1";
  confInput.value = "1";
  confInput.style.maxWidth = "80px";
  const addBtn = document.createElement("button");
  addBtn.textContent = "新增";
  addBtn.onclick = async () => {
    const qq = qqInput.value.trim();
    const alias = nameInput.value.trim();
    const confidence = Number(confInput.value || 1);
    if (!qq || !alias) {
      toast("请填写 QQ 和别名", 3000);
      return;
    }
    const res = await apiPost("aliases-add", { scope, qq, new: alias, confidence });
    toast(okOf(res) ? "已新增" : "新增失败");
    if (okOf(res)) {
      qqInput.value = "";
      nameInput.value = "";
      confInput.value = "1";
      renderAliases(scope);
    }
  };
  addRow.append(qqInput, nameInput, confInput, addBtn);
  addCard.append(addTitle, addRow);
  box.appendChild(addCard);

  if (!groups.length) {
    const empty = document.createElement("div");
    empty.className = "card empty";
    empty.textContent = "还没有别名记录。";
    box.appendChild(empty);
    $("pager").classList.add("hidden");
    return;
  }

  const search = document.createElement("div");
  search.className = "card";
  const searchInput = document.createElement("input");
  searchInput.type = "search";
  searchInput.placeholder = "按 QQ、昵称或别名搜索";
  const searchInfo = document.createElement("span");
  searchInfo.className = "muted";
  searchInfo.style.marginLeft = "8px";
  search.appendChild(searchInput);
  search.appendChild(searchInfo);
  box.appendChild(search);

  const listBox = document.createElement("div");
  box.appendChild(listBox);

  const PAGE = 10;
  let page = 1;
  let filtered = groups.slice();

  const draw = () => {
    const pages = Math.max(1, Math.ceil(filtered.length / PAGE));
    page = Math.min(Math.max(1, page), pages);
    const slice = filtered.slice((page - 1) * PAGE, page * PAGE);
    const frag = document.createDocumentFragment();
    for (const group of slice) {
      const card = document.createElement("div");
      card.className = "card";
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${group.qq} · ${group.nickname || "（未记录昵称）"} · ${group.aliases.length} 个别名`;
      card.appendChild(meta);
      for (const item of group.aliases) {
        const row = document.createElement("div");
        row.className = "actions alias-row";
        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.value = item.alias;
        nameInput.style.maxWidth = "140px";
        const confInput = document.createElement("input");
        confInput.type = "number";
        confInput.step = "0.1";
        confInput.min = "0";
        confInput.max = "1";
        confInput.value = item.confidence;
        confInput.style.maxWidth = "80px";
        const save = document.createElement("button");
        save.textContent = "保存";
        save.onclick = async () => {
          const res = await apiPost("aliases-update", {
            scope,
            qq: group.qq,
            old: item.alias,
            new: nameInput.value.trim(),
            confidence: Number(confInput.value || 0),
          });
          toast(okOf(res) ? "已保存" : "保存失败");
          if (okOf(res)) renderAliases(scope);
        };
        const del = document.createElement("button");
        del.className = "danger";
        del.textContent = "删除";
        del.onclick = () => confirmAction(del, "确认删除？", async () => {
          const res = await apiPost("aliases-delete", { scope, qq: group.qq, alias: item.alias });
          toast(okOf(res) ? "已删除" : "删除失败");
          if (okOf(res)) renderAliases(scope);
        });
        row.append(nameInput, confInput, save, del);
        card.appendChild(row);
      }
      frag.appendChild(card);
    }
    listBox.innerHTML = "";
    listBox.appendChild(frag);
    searchInfo.textContent = `显示 ${filtered.length}/${groups.length} 个 QQ · 第 ${page}/${pages} 页`;
    $("pager").classList.remove("hidden");
    $("pageInfo").textContent = `别名 ${filtered.length} 个 QQ · 第 ${page}/${pages} 页（每页 ${PAGE}）`;
    $("prevPage").disabled = page <= 1;
    $("nextPage").disabled = page >= pages;
    state.meta = { page, pages, total: filtered.length, page_size: PAGE };
    state.fetchPage = async (next) => {
      page = next;
      draw();
    };
  };

  searchInput.oninput = () => {
    const q = searchInput.value.trim();
    filtered = groups.filter(
      (g) =>
        `${g.qq}`.includes(q)
        || (g.nickname || "").includes(q)
        || g.aliases.some((a) => a.alias.includes(q)),
    );
    page = 1;
    draw();
  };

  state.meta = { page: 1, pages: 1, total: filtered.length, page_size: PAGE };
  state.rows = [];
  state.renderFn = null;
  page = 1;
  draw();
}

async function renderExportHistory() {
  const box = $("list");
  box.innerHTML = "";
  $("trashBox").classList.add("hidden");
  $("pager").classList.add("hidden");
  const rows = unwrap(await apiGet("export-history")) || [];
  const header = document.createElement("div");
  header.className = "card";
  const title = document.createElement("div");
  title.className = "meta";
  title.textContent = `导出历史 · 共 ${rows.length} 次`;
  header.appendChild(title);
  if (rows.length) {
    const clear = document.createElement("button");
    clear.className = "danger";
    clear.textContent = "清空记录";
    clear.onclick = async () => {
      if (!clear.dataset.armed) {
        clear.dataset.armed = "1";
        clear.textContent = "确认清空？";
        setTimeout(() => {
          if (clear.dataset.armed) {
            delete clear.dataset.armed;
            clear.textContent = "清空记录";
          }
        }, 4000);
        return;
      }
      delete clear.dataset.armed;
      clear.disabled = true;
      const res = await apiPost("export-history-clear", {});
      toast(okOf(res) ? "已清空导出历史" : "清空失败");
      await renderExportHistory();
    };
    header.appendChild(clear);
  }
  box.appendChild(header);
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "card empty";
    empty.textContent = "还没有导出记录。";
    box.appendChild(empty);
    return;
  }
  const frag = document.createDocumentFragment();
  for (const row of rows) {
    const card = document.createElement("div");
    card.className = "card";
    if (state.bulkAllowed && ["core", "important", "remind", "normal", "low"].includes(state.tab)) {
      const pick = document.createElement("input");
      pick.type = "checkbox";
      pick.className = "pick";
      pick.checked = state.selected.has(`${state.tab}:${row.date || ""}:${row.seq}`);
      pick.onchange = () => {
        const key = `${state.tab}:${row.date || ""}:${row.seq}`;
        if (pick.checked) state.selected.add(key);
        else state.selected.delete(key);
        updateBulkBar();
      };
      card.appendChild(pick);
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${row.time.replace("T", " ")} · ${row.device || "未知设备"} · ${Math.max(1, Math.round((row.bytes || 0) / 1024))}KB`;
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = `${row.scope} · ${row.filename}`;
    card.append(meta, text);
    frag.appendChild(card);
  }
  box.appendChild(frag);
}

boot().catch((err) => setStatus(`加载失败：${err && err.message ? err.message : err}`));
