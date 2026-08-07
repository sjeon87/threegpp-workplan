/* 3GPP Work Plan Explorer — frontend logic */
"use strict";

const $ = (sel) => document.querySelector(sel);

const els = {
  snapshotSelect: $("#snapshotSelect"),
  momentInput: $("#momentInput"),
  applyMoment: $("#applyMoment"),
  clearMoment: $("#clearMoment"),
  snapshotBadge: $("#snapshotBadge"),
  searchBox: $("#searchBox"),
  releaseTrigger: $("#releaseTrigger"),
  releasePanel: $("#releasePanel"),
  releaseMulti: $("#releaseMulti"),
  compMin: $("#compMin"),
  compMax: $("#compMax"),
  sortList: $("#sortList"),
  addSortBtn: $("#addSortBtn"),
  resetBtn: $("#resetBtn"),
  resultInfo: $("#resultInfo"),
  itemsBody: $("#itemsBody"),
  pagination: $("#pagination"),
  drawerOverlay: $("#drawerOverlay"),
  historyDrawer: $("#historyDrawer"),
  drawerHandle: $("#drawerHandle"),
  drawerTitle: $("#drawerTitle"),
  drawerSub: $("#drawerSub"),
  drawerBody: $("#drawerBody"),
  drawerGrid: $("#drawerGrid"),
  historyBody: $("#historyBody"),
  timeline: $("#timeline"),
  drawerClose: $("#drawerClose"),
  toast: $("#toast"),
};

const SORT_OPTIONS = [
  ["id", "Default"],
  ["name", "Name (A–Z)"],
  ["acronym", "Acronym"],
  ["item_id", "Item ID"],
  ["unique_id", "Unique ID"],
  ["release", "Release"],
  ["outline_level", "Outline level"],
  ["completion", "Completion %"],
  ["start_date", "Start date"],
  ["finish_date", "Finish date"],
];

const PAGE_SIZE = 25;

// ---------- state ----------
const state = {
  snapshots: [],
  currentSnapshot: null,   // resolved snapshot object
  q: "",
  filters: { release: [] },
  compMin: "", compMax: "",
  sorts: [{ col: "id", order: "asc" }],
  offset: 0,
};

// ---------- helpers ----------
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(msg) {
  els.toast.textContent = msg;
  els.toast.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => els.toast.classList.add("hidden"), 2600);
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return res.json();
}

// ---------- snapshot dropdown ----------
function renderSnapshotDropdown() {
  const sel = els.snapshotSelect;
  sel.innerHTML = "";
  state.snapshots.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.file_date;
    opt.textContent = `${s.file_date}`;
    sel.appendChild(opt);
  });
}

// ---------- facets ----------
function updateReleaseTrigger() {
  const n = state.filters.release.length;
  els.releaseTrigger.textContent = n
    ? (n === 1 ? "1 release" : `${n} releases`) + " ▾"
    : "All releases ▾";
}

async function loadFacets() {
  const snap = state.currentSnapshot ? `?snapshot=${state.currentSnapshot.file_date}` : "";
  const f = await getJSON(`/api/facets${snap}`);
  els.releasePanel.innerHTML = (f.releases || []).map((r) => `
    <label class="multi-opt">
      <input type="checkbox" class="release-opt" value="${esc(r)}" />
      <span>${esc(r)}</span>
    </label>`).join("");
  // Restore any prior selection.
  state.filters.release.forEach((v) => {
    [...els.releasePanel.querySelectorAll(".release-opt")].forEach((cb) => {
      if (cb.value === v) cb.checked = true;
    });
  });
  updateReleaseTrigger();
}

// ---------- badge ----------
function renderBadge() {
  const s = state.currentSnapshot;
  if (!s) { els.snapshotBadge.innerHTML = ""; return; }
  const clamped = s.clamped_early
    ? ' <span>· before earliest snapshot, clamped</span>' : "";
  const n = (s.item_count != null) ? s.item_count.toLocaleString()
    : (s.row_count != null ? s.row_count.toLocaleString() + " (raw)" : "…");
  els.snapshotBadge.className = "badge" + (s.clamped_early ? " clamped" : "");
  els.snapshotBadge.innerHTML =
    `Snapshot as of <b>${esc(s.file_date)}</b> &nbsp;·&nbsp; ` +
    `<span class="source">${esc(s.source_file)}</span> ` +
    `· <b>${n}</b> items (all latest versions as of this moment)${clamped}`;
  if (els.snapshotSelect.value !== s.file_date) {
    els.snapshotSelect.value = s.file_date;
  }
}

// ---------- items ----------
function buildQuery() {
  const p = new URLSearchParams();
  if (state.currentSnapshot) p.set("snapshot", state.currentSnapshot.file_date);
  if (state.q) p.set("q", state.q);
  if (state.filters.release && state.filters.release.length) {
    p.set("release", state.filters.release.join(","));
  }
  if (state.compMin !== "") p.set("completion_min", state.compMin);
  if (state.compMax !== "") p.set("completion_max", state.compMax);
  p.set("sort", state.sorts.map((s) => s.col).join(",") || "id");
  p.set("order", state.sorts.map((s) => s.order).join(",") || "asc");
  p.set("limit", String(PAGE_SIZE));
  p.set("offset", String(state.offset));
  return p.toString();
}

// ---------- multi-sort builder ----------
function renderSortList() {
  const options = SORT_OPTIONS.map(
    ([k, label]) => `<option value="${k}">${label}</option>`).join("");
  els.sortList.innerHTML = state.sorts.map((s, idx) => `
    <div class="sort-criterion" data-idx="${idx}">
      <button type="button" class="sort-move" data-action="up"
              title="Primary (move up)" ${idx === 0 ? "disabled" : ""}>↑</button>
      <button type="button" class="sort-move" data-action="down"
              title="Lower priority (move down)" ${idx === state.sorts.length - 1 ? "disabled" : ""}>↓</button>
      <select class="sort-col" data-idx="${idx}">
        ${options}
      </select>
      <button type="button" class="sort-dir" data-action="dir"
              title="Toggle direction">${s.order === "asc" ? "Asc ↑" : "Desc ↓"}</button>
      <button type="button" class="sort-remove" data-action="remove" title="Remove">×</button>
    </div>`).join("");
  state.sorts.forEach((s, idx) => {
    const sel = els.sortList.querySelector(`.sort-col[data-idx="${idx}"]`);
    if (sel) sel.value = s.col;
  });
}

function renderRows(items) {
  if (!items.length) {
    els.itemsBody.innerHTML =
      '<tr><td colspan="11" class="empty">No work plan items match your criteria.</td></tr>';
    return;
  }
  els.itemsBody.innerHTML = items.map((it) => `
    <tr data-uid="${esc(it.unique_id)}" title="Click to view history">
      <td>${esc(it.unique_id)}</td>
      <td class="name">${esc(it.name)}</td>
      <td class="acronym">${esc(it.acronym)}</td>
      <td class="wid">${esc(it.hyperlink)}</td>
      <td class="status">${esc(it.status_report)}</td>
      <td>${esc(it.release)}</td>
      <td>${it.outline_level == null ? "" : esc(it.outline_level)}</td>
      <td>${it.completion == null ? "" : esc(it.completion)}</td>
      <td>${esc(it.start_date)}</td>
      <td>${esc(it.finish_date)}</td>
      <td>${esc(it.rapporteur_name)}</td>
    </tr>`).join("");
}

let _curTotal = 0;
function renderPagination(total) {
  _curTotal = total;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const cur = Math.floor(state.offset / PAGE_SIZE) + 1;
  const start = state.offset + 1;
  const end = Math.min(total, state.offset + PAGE_SIZE);
  els.resultInfo.innerHTML = `<b>${total.toLocaleString()}</b> result${total === 1 ? "" : "s"} ·
    showing <b>${start.toLocaleString()}–${end.toLocaleString()}</b>`;

  let html = `<button data-page="first" ${state.offset === 0 ? "disabled" : ""}>«</button>`;
  html += `<button data-page="prev" ${state.offset === 0 ? "disabled" : ""}>‹</button>`;
  html += `<span class="pageinfo">Page ${cur} / ${pages}</span>`;
  html += `<button data-page="next" ${end >= total ? "disabled" : ""}>›</button>`;
  html += `<button data-page="last" ${end >= total ? "disabled" : ""}>»</button>`;
  els.pagination.innerHTML = html;
}

async function loadItems() {
  if (!state.currentSnapshot) return;
  const data = await getJSON(`/api/items?${buildQuery()}`);
  renderRows(data.items);
  renderPagination(data.total);
}

// ---------- load everything ----------
async function applySnapshot(snapshot) {
  state.currentSnapshot = snapshot;
  state.offset = 0;
  renderBadge();
  await loadFacets();
  await loadItems();
}

async function init() {
  const data = await getJSON("/api/snapshots");
  state.snapshots = data.snapshots;
  renderSnapshotDropdown();

  renderSortList();

  state.currentSnapshot = await getJSON("/api/resolve-snapshot");
  renderBadge();
  els.snapshotSelect.value = state.currentSnapshot.file_date;
  await loadFacets();
  await loadItems();
  els.searchBox.focus();
}

// ---------- events ----------
els.snapshotSelect.addEventListener("change", async () => {
  const fd = els.snapshotSelect.value;
  if (!fd) return;
  try {
    // Resolve through the API so we also get the as-of item count.
    const snap = await getJSON(`/api/resolve-snapshot?datetime=${encodeURIComponent(fd)}`);
    await applySnapshot(snap);
  } catch (e) { toast(e.message); }
});

els.applyMoment.addEventListener("click", async () => {
  const val = els.momentInput.value;
  if (!val) { toast("Choose a date/time first."); return; }
  try {
    const snap = await getJSON(`/api/resolve-snapshot?datetime=${encodeURIComponent(val)}`);
    await applySnapshot(snap);
    toast(`Applying snapshot as of ${snap.file_date}`);
  } catch (e) { toast(e.message); }
});

els.clearMoment.addEventListener("click", async () => {
  els.momentInput.value = "";
  const latest = await getJSON("/api/resolve-snapshot");
  state.currentSnapshot = latest;
  els.snapshotSelect.value = latest.file_date;
  renderBadge();
  await loadFacets();
  await loadItems();
});


let searchTimer;
els.searchBox.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = els.searchBox.value.trim();
    state.offset = 0;
    loadItems().catch((e) => toast(e.message));
  }, 250);
});

function onFilterChange() {
  state.compMin = els.compMin.value;
  state.compMax = els.compMax.value;
  state.offset = 0;
  loadItems().catch((e) => toast(e.message));
}
["compMin", "compMax"].forEach((id) => els[id].addEventListener("change", onFilterChange));

// Release multi-select
els.releaseTrigger.addEventListener("click", (e) => {
  e.stopPropagation();
  els.releasePanel.classList.toggle("hidden");
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#releaseMulti")) els.releasePanel.classList.add("hidden");
});
els.releasePanel.addEventListener("change", (e) => {
  if (!e.target.classList.contains("release-opt")) return;
  state.filters.release = [...els.releasePanel.querySelectorAll(".release-opt:checked")]
    .map((cb) => cb.value);
  updateReleaseTrigger();
  state.offset = 0;
  loadItems().catch((err) => toast(err.message));
});

els.addSortBtn.addEventListener("click", () => {
  const used = new Set(state.sorts.map((s) => s.col));
  const next = SORT_OPTIONS.find(([k]) => !used.has(k));
  state.sorts.push({
    col: next ? next[0] : SORT_OPTIONS[0][0],
    order: "asc",
  });
  renderSortList();
  state.offset = 0;
  loadItems().catch((e) => toast(e.message));
});

els.sortList.addEventListener("change", (e) => {
  const sel = e.target.closest("select.sort-col");
  if (!sel) return;
  const idx = Number(sel.dataset.idx);
  if (Number.isNaN(idx)) return;
  state.sorts[idx].col = sel.value;
  state.offset = 0;
  loadItems().catch((e) => toast(e.message));
});

els.sortList.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const row = btn.closest(".sort-criterion");
  if (!row) return;
  const idx = Number(row.dataset.idx);
  const action = btn.dataset.action;
  if (!Number.isInteger(idx)) return;

  if (action === "dir") {
    state.sorts[idx].order = state.sorts[idx].order === "asc" ? "desc" : "asc";
  } else if (action === "remove") {
    state.sorts.splice(idx, 1);
    if (!state.sorts.length) state.sorts.push({ col: "id", order: "asc" });
  } else if (action === "up" && idx > 0) {
    [state.sorts[idx - 1], state.sorts[idx]] = [state.sorts[idx], state.sorts[idx - 1]];
  } else if (action === "down" && idx < state.sorts.length - 1) {
    [state.sorts[idx + 1], state.sorts[idx]] = [state.sorts[idx], state.sorts[idx + 1]];
  }
  renderSortList();
  state.offset = 0;
  loadItems().catch((e) => toast(e.message));
});

els.resetBtn.addEventListener("click", () => {
  els.searchBox.value = "";
  els.compMin.value = ""; els.compMax.value = "";
  els.momentInput.value = "";
  state.q = ""; state.compMin = ""; state.compMax = "";
  state.filters.release = [];
  els.releasePanel.querySelectorAll(".release-opt").forEach((cb) => (cb.checked = false));
  state.offset = 0; state.sorts = [{ col: "id", order: "asc" }];
  renderSortList();
  updateReleaseTrigger();
  loadFacets().then(loadItems);
});

els.pagination.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-page]");
  if (!btn || btn.disabled) return;
  const act = btn.dataset.page;
  const lastOffset = Math.max(0, Math.floor((_curTotal - 1) / PAGE_SIZE) * PAGE_SIZE);
  if (act === "first") state.offset = 0;
  else if (act === "prev") state.offset = Math.max(0, state.offset - PAGE_SIZE);
  else if (act === "next") state.offset += PAGE_SIZE;
  else if (act === "last") state.offset = lastOffset;
  loadItems().catch((e) => toast(e.message));
});


// drawer
function openDrawer() {
  els.historyDrawer.classList.add("open");
  els.drawerOverlay.classList.remove("hidden");
}
function closeDrawer() {
  els.historyDrawer.classList.remove("open");
  els.drawerOverlay.classList.add("hidden");
}
els.drawerClose.addEventListener("click", closeDrawer);
els.drawerOverlay.addEventListener("click", closeDrawer);

// Resizable drawer — drag the left edge handle to change its width.
const DRAWER_MIN_W = 460;
(function initDrawerResize() {
  const handle = els.drawerHandle;
  const drawer = els.historyDrawer;
  let startX = 0;
  let startW = 0;
  let dragging = false;

  handle.addEventListener("pointerdown", (e) => {
    dragging = true;
    startX = e.clientX;
    startW = drawer.getBoundingClientRect().width;
    drawer.classList.add("resizing");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
  });

  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    // Drawer is anchored to the right edge; width grows as the handle moves left.
    const dx = startX - e.clientX;
    const maxW = window.innerWidth - 40;
    const w = Math.min(maxW, Math.max(DRAWER_MIN_W, startW + dx));
    drawer.style.width = w + "px";
  });

  const stop = (e) => {
    if (!dragging) return;
    dragging = false;
    drawer.classList.remove("resizing");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    if (handle.hasPointerCapture(e && e.pointerId)) {
      handle.releasePointerCapture(e.pointerId);
    }
  };
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
})();
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

document.addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-uid]");
  if (tr) showHistory(tr.dataset.uid);
});

async function showHistory(uid) {
  try {
    const data = await getJSON(`/api/items/${encodeURIComponent(uid)}`);
    els.drawerTitle.textContent = data.name || "Work item";
    els.drawerSub.textContent =
      `${data.unique_id}${data.acronym ? " — " + data.acronym : ""}`;

    const d = data.latest || {};
    els.drawerBody.innerHTML = `
      <div class="detail-row"><strong>${esc(data.name)}</strong></div>
      ${d.hyperlink ? `<div class="detail-row"><span class="muted">WID:</span> <b>${esc(d.hyperlink)}</b></div>` : ""}
      ${d.status_report ? `<div class="detail-row"><span class="muted">Status report:</span> ${esc(d.status_report)}</div>` : ""}
      ${d.notes ? `<div class="detail-row"><span class="muted">Notes:</span> ${esc(d.notes)}</div>` : ""}
      ${d.impacted_specs ? `<div class="detail-row"><span class="muted">Impacted specs:</span> ${esc(d.impacted_specs)}</div>` : ""}
    `;

    const kv = (k, v) => v ? `<div class="kv"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>` : "";
    const parts = [
      kv("WID", d.hyperlink),
      kv("Status report", d.status_report),
      kv("Release", d.release),
      kv("Outline level", d.outline_level),
      kv("Completion %", d.completion),
      kv("Start", d.start_date),
      kv("Finish", d.finish_date),
      kv("Rapporteur", d.rapporteur_name),
      kv("Email", d.rapporteur_email),
      kv("TSG approved", d.tsg_approved),
      kv("PCG approved", d.pcg_approved),
      kv("Created", d.created),
      kv("Last modified", d.last_modified),
    ];
    els.drawerGrid.innerHTML = parts.join("") || '<div class="muted">No additional details.</div>';
    els.historyDrawer.dataset.uid = uid;

    const hist = data.history || [];
    els.historyBody.innerHTML = hist.map((h) => `
      <tr data-file="${esc(h.file_date)}">
        <td>${esc(h.file_date)}</td>
        <td>${esc(h.source_file)}</td>
        <td>${esc(h.name)}</td>
        <td class="wid">${esc(h.hyperlink)}</td>
        <td>${esc(h.release)}</td>
        <td>${h.outline_level == null ? "" : esc(h.outline_level)}</td>
        <td>${h.completion == null ? "" : esc(h.completion)}</td>
        <td>${esc(h.start_date)}</td>
        <td>${esc(h.finish_date)}</td>
        <td>${esc(h.status_report)}</td>
      </tr>`).join("");

    const sortedHist = hist.slice().sort((a, b) => a.file_date.localeCompare(b.file_date));
    els.timeline.innerHTML = sortedHist.map((h) => `
      <div class="tl-row">
        <span class="tl-date">${esc(h.file_date)}</span>
        <div class="tl-bar-wrap"><div class="tl-bar" style="width:${Math.max(0, Math.min(100, h.completion || 0))}%"></div></div>
        <span class="tl-pct">${h.completion == null ? "—" : esc(h.completion) + "%"}</span>
      </div>`).join("");

    openDrawer();
  } catch (e) { toast(e.message); }
}

init().catch((e) => toast(`Init error: ${e.message}`));


