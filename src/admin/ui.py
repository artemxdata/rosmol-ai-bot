from __future__ import annotations

import base64
from pathlib import Path


def _logo_data_uri() -> str:
    path = Path(__file__).with_name("static") / "codepulse-logo.png"
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:image/png;base64,{encoded}"


_HTML_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Импульс кода | Knowledge Base Admin</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7fb;
      --surface: #ffffff;
      --surface-soft: #f8fafc;
      --line: #dbe3ee;
      --text: #091a2f;
      --muted: #65758b;
      --brand: #061d39;
      --accent: #087b8f;
      --accent-soft: #e5f6f8;
      --danger: #b42318;
      --ok: #027a48;
      --shadow: 0 10px 30px rgba(7, 29, 57, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.94);
      position: sticky;
      top: 0;
      z-index: 3;
      backdrop-filter: blur(12px);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 260px;
    }
    .brand img {
      width: 138px;
      height: auto;
      display: block;
    }
    .brand-title {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    h1 {
      margin: 0;
      color: var(--brand);
      font-size: 18px;
      font-weight: 800;
      letter-spacing: 0;
    }
    .subtitle {
      color: var(--muted);
      font-size: 12px;
    }
    main {
      display: grid;
      grid-template-columns: minmax(440px, 0.95fr) minmax(520px, 1.25fr);
      gap: 18px;
      padding: 18px;
    }
    section {
      min-width: 0;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .toolbar, .actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      padding: 14px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }
    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .stack { padding: 14px; }
    input, select, textarea, button {
      font: inherit;
      border-radius: 6px;
    }
    input, select, textarea {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      padding: 10px 11px;
    }
    input:focus, select:focus, textarea:focus {
      outline: 3px solid rgba(8, 123, 143, 0.16);
      border-color: var(--accent);
    }
    button {
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      padding: 10px 13px;
      cursor: pointer;
      font-weight: 700;
    }
    button.secondary {
      background: #fff;
      color: var(--accent);
    }
    button.ghost {
      border-color: var(--line);
      background: #fff;
      color: var(--brand);
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    label.toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      user-select: none;
    }
    label.toggle input { width: 16px; height: 16px; }
    #tokenInput { width: 310px; }
    #searchInput { min-width: 260px; flex: 1; }
    #forumFilter, #categoryFilter { width: 180px; }
    .status {
      min-height: 22px;
      color: var(--muted);
    }
    .status.error { color: var(--danger); }
    .status.ok { color: var(--ok); }
    .metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      padding: 14px;
      background: var(--surface-soft);
      border-bottom: 1px solid var(--line);
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fff;
    }
    .metric-value {
      display: block;
      font-size: 20px;
      font-weight: 800;
      color: var(--brand);
      margin-bottom: 3px;
    }
    .metric-label {
      color: var(--muted);
      font-size: 12px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      vertical-align: top;
      text-align: left;
      word-break: break-word;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      background: var(--surface-soft);
    }
    tbody tr { cursor: pointer; }
    tr:hover td { background: #f2fbfd; }
    tr.selected td {
      background: var(--accent-soft);
      border-bottom-color: #bde8ee;
    }
    .mono {
      font-family: Consolas, monospace;
      font-size: 12px;
    }
    .muted { color: var(--muted); }
    #textClean {
      width: 100%;
      min-height: 320px;
      resize: vertical;
      line-height: 1.5;
      font-size: 15px;
    }
    pre {
      max-height: 320px;
      overflow: auto;
      margin: 14px 0 0;
      background: #07152a;
      color: #e7eef8;
      padding: 14px;
      border-radius: 8px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .field-grid {
      display: grid;
      grid-template-columns: 120px 1fr;
      gap: 9px 12px;
      align-items: center;
      margin-bottom: 12px;
    }
    .detail-title {
      margin: 0 0 12px;
      color: var(--brand);
      font-size: 17px;
      font-weight: 800;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 800;
    }
    @media (max-width: 1060px) {
      main { grid-template-columns: 1fr; }
      header { align-items: flex-start; flex-direction: column; }
      .header-actions { justify-content: flex-start; }
      #tokenInput { width: min(100%, 360px); }
      .metrics { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <img src="__LOGO_DATA_URI__" alt="Импульс кода">
      <div class="brand-title">
        <h1>Админка знаний</h1>
        <span class="subtitle">RAG-корпус Росмолодёжи · Knowledge Base Admin</span>
      </div>
    </div>
    <div class="header-actions">
      <input id="tokenInput" type="password" autocomplete="off" placeholder="ADMIN_AUTH_TOKEN">
      <button id="saveTokenButton" type="button">Сохранить токен</button>
      <button id="validateButton" class="secondary" type="button">Validation</button>
      <button id="qualityButton" class="secondary" type="button">Quality check</button>
    </div>
  </header>
  <main>
    <section>
      <div class="toolbar">
        <input id="searchInput" type="search" placeholder="Поиск: Амур, проезд, сертификат">
        <select id="statusFilter">
          <option value="">Все статусы</option>
          <option value="published">published</option>
          <option value="draft">draft</option>
          <option value="archived">archived</option>
        </select>
        <input id="forumFilter" type="text" placeholder="forum">
        <input id="categoryFilter" type="text" placeholder="category">
        <button id="loadButton" type="button">Найти</button>
      </div>
      <div class="metrics">
        <div class="metric">
          <span id="metricFound" class="metric-value">-</span>
          <span class="metric-label">найдено</span>
        </div>
        <div class="metric">
          <span id="metricValid" class="metric-value">-</span>
          <span class="metric-label">валидных чанков</span>
        </div>
        <div class="metric">
          <span id="metricEval" class="metric-value">-</span>
          <span class="metric-label">quality report</span>
        </div>
      </div>
      <div class="stack">
        <div id="listStatus" class="status"></div>
      </div>
      <table>
        <thead>
          <tr>
            <th style="width: 28%;">chunk_id</th>
            <th style="width: 14%;">status</th>
            <th style="width: 18%;">forum</th>
            <th>preview</th>
          </tr>
        </thead>
        <tbody id="chunksTable"></tbody>
      </table>
    </section>
    <section>
      <div class="actions">
        <button id="saveChunkButton" type="button" disabled>
          Сохранить и обновить индекс
        </button>
        <button id="reindexButton" class="secondary" type="button" disabled>
          Обновить индекс
        </button>
        <button id="relatedCasesButton" class="ghost" type="button" disabled>
          Eval cases
        </button>
        <label class="toggle">
          <input id="reindexToggle" type="checkbox" checked>
          сразу обновлять RAG
        </label>
        <span id="detailStatus" class="status"></span>
      </div>
      <div class="stack">
        <h2 id="detailTitle" class="detail-title muted">Чанк не выбран</h2>
        <div class="field-grid">
          <label for="chunkStatus">Статус</label>
          <select id="chunkStatus" disabled>
            <option value="published">published</option>
            <option value="draft">draft</option>
            <option value="archived">archived</option>
          </select>
          <div class="muted">Forum</div>
          <div id="chunkForum" class="mono"></div>
          <div class="muted">Topic</div>
          <div id="chunkTopic" class="mono"></div>
          <div class="muted">Source</div>
          <div id="chunkSource" class="mono"></div>
        </div>
        <textarea id="textClean" disabled></textarea>
        <pre id="reportOutput"></pre>
      </div>
    </section>
  </main>
  <script>
    let selectedChunkId = "";
    const tokenInput = document.getElementById("tokenInput");
    tokenInput.value = sessionStorage.getItem("adminToken") || "";

    function token() {
      return tokenInput.value.trim();
    }
    function headers(json = true) {
      const result = {"X-Admin-Token": token()};
      if (json) result["Content-Type"] = "application/json";
      return result;
    }
    function setStatus(id, message, cls = "") {
      const el = document.getElementById(id);
      el.className = "status " + cls;
      el.textContent = message;
    }
    function setMetric(id, value) {
      document.getElementById(id).textContent = String(value);
    }
    async function requestJson(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: {...headers(options.body !== undefined), ...(options.headers || {})},
      });
      const text = await response.text();
      let payload = {};
      if (text) {
        try { payload = JSON.parse(text); } catch { payload = {raw: text}; }
      }
      if (!response.ok) {
        const detail = payload.detail || payload.raw || response.statusText;
        throw new Error(String(detail));
      }
      return payload;
    }
    function queryString(params) {
      const search = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) {
        if (value) search.set(key, value);
      }
      return search.toString();
    }
    function renderRows(items) {
      const tbody = document.getElementById("chunksTable");
      tbody.innerHTML = "";
      for (const item of items) {
        const tr = document.createElement("tr");
        tr.dataset.chunkId = item.chunk_id;
        tr.innerHTML = `
          <td class="mono">${item.chunk_id || ""}</td>
          <td><span class="badge">${item.status || ""}</span></td>
          <td>${item.forum_normalized || ""}</td>
          <td>${item.text_preview || ""}</td>
        `;
        tr.addEventListener("click", () => loadChunk(item.chunk_id, tr));
        tbody.appendChild(tr);
      }
    }
    async function loadChunks() {
      try {
        setStatus("listStatus", "Загрузка...");
        const params = queryString({
          q: document.getElementById("searchInput").value.trim(),
          status: document.getElementById("statusFilter").value,
          forum: document.getElementById("forumFilter").value.trim(),
          category: document.getElementById("categoryFilter").value.trim(),
          limit: "100",
        });
        const data = await requestJson("/admin/kb/chunks?" + params, {method: "GET"});
        renderRows(data.items || []);
        setMetric("metricFound", data.total);
        setStatus("listStatus", `Найдено: ${data.total}`, "ok");
      } catch (error) {
        setStatus("listStatus", error.message, "error");
      }
    }
    async function loadChunk(chunkId, row) {
      try {
        selectedChunkId = chunkId;
        document.querySelectorAll("tr.selected").forEach((el) => {
          el.classList.remove("selected");
        });
        if (row) row.classList.add("selected");
        setStatus("detailStatus", "Загрузка...");
        const data = await requestJson(
          "/admin/kb/chunks/" + encodeURIComponent(chunkId),
          {method: "GET"}
        );
        document.getElementById("detailTitle").textContent = data.chunk_id || "";
        document.getElementById("chunkStatus").value = data.status || "published";
        document.getElementById("chunkStatus").disabled = false;
        document.getElementById("chunkForum").textContent = data.forum_normalized || "";
        document.getElementById("chunkTopic").textContent = data.topic || "";
        document.getElementById("chunkSource").textContent = data.source_type || "";
        document.getElementById("textClean").value = data.text_clean || data.text || "";
        document.getElementById("textClean").disabled = false;
        document.getElementById("saveChunkButton").disabled = false;
        document.getElementById("reindexButton").disabled = false;
        document.getElementById("relatedCasesButton").disabled = false;
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        setStatus("detailStatus", "Готово", "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function saveChunk() {
      if (!selectedChunkId) return;
      try {
        setStatus("detailStatus", "Сохранение...");
        const data = await requestJson(
          "/admin/kb/chunks/" + encodeURIComponent(selectedChunkId),
          {
            method: "PATCH",
            body: JSON.stringify({
              status: document.getElementById("chunkStatus").value,
              text_clean: document.getElementById("textClean").value,
              reindex: document.getElementById("reindexToggle").checked,
            }),
          }
        );
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        const reindex = data.reindex && data.reindex.ok ? " RAG обновлён." : "";
        setStatus("detailStatus", "Сохранено." + reindex, "ok");
        await loadChunks();
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function reindexChunk() {
      if (!selectedChunkId) return;
      try {
        setStatus("detailStatus", "Обновление индекса...");
        const data = await requestJson(
          "/admin/kb/chunks/" + encodeURIComponent(selectedChunkId) + "/reindex",
          {method: "POST", body: "{}"}
        );
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        setStatus("detailStatus", "RAG-индекс обновлён", "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function showRelatedCases() {
      if (!selectedChunkId) return;
      try {
        setStatus("detailStatus", "Загрузка eval cases...");
        const data = await requestJson(
          "/admin/kb/chunks/" + encodeURIComponent(selectedChunkId) + "/eval-cases",
          {method: "GET"}
        );
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        setStatus("detailStatus", `Eval cases: ${data.total}`, "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function showValidation() {
      try {
        const data = await requestJson("/admin/kb/validate", {method: "POST", body: "{}"});
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        setMetric("metricValid", data.valid_records);
        setStatus("detailStatus", `KB valid: ${data.valid_records}`, "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function showQualityCheck() {
      try {
        const data = await requestJson("/admin/kb/quality-check", {
          method: "POST",
          body: JSON.stringify({include_latest_eval_report: true}),
        });
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        const report = data.latest_eval_report_exists ? "loaded" : "missing";
        setMetric("metricEval", report);
        setStatus("detailStatus", "Quality check loaded", "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }

    document.getElementById("saveTokenButton").addEventListener("click", () => {
      sessionStorage.setItem("adminToken", token());
      setStatus("listStatus", "Токен сохранён", "ok");
    });
    document.getElementById("loadButton").addEventListener("click", loadChunks);
    document.getElementById("saveChunkButton").addEventListener("click", saveChunk);
    document.getElementById("reindexButton").addEventListener("click", reindexChunk);
    document.getElementById("relatedCasesButton").addEventListener("click", showRelatedCases);
    document.getElementById("validateButton").addEventListener("click", showValidation);
    document.getElementById("qualityButton").addEventListener("click", showQualityCheck);
  </script>
</body>
</html>
"""


ADMIN_KB_HTML = _HTML_TEMPLATE.replace("__LOGO_DATA_URI__", _logo_data_uri())
