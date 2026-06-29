from __future__ import annotations

ADMIN_KB_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Knowledge Base Admin</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --surface: #ffffff;
      --line: #d9dee7;
      --text: #1f2937;
      --muted: #667085;
      --accent: #137c8b;
      --accent-dark: #0f6170;
      --danger: #b42318;
      --ok: #027a48;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
    }
    main {
      display: grid;
      grid-template-columns: minmax(420px, 1fr) minmax(460px, 1.2fr);
      gap: 16px;
      padding: 16px;
    }
    section {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 6px;
      min-width: 0;
    }
    .toolbar, .actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      padding: 12px;
      border-bottom: 1px solid var(--line);
    }
    .stack { padding: 12px; }
    input, select, textarea, button {
      font: inherit;
      border-radius: 4px;
    }
    input, select, textarea {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      padding: 8px 9px;
    }
    input:focus, select:focus, textarea:focus {
      outline: 2px solid rgba(19, 124, 139, 0.2);
      border-color: var(--accent);
    }
    button {
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      padding: 8px 10px;
      cursor: pointer;
    }
    button.secondary {
      background: #fff;
      color: var(--accent-dark);
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    #tokenInput { width: 260px; }
    #searchInput { min-width: 240px; flex: 1; }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 9px 10px;
      vertical-align: top;
      text-align: left;
      word-break: break-word;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      background: #fbfcfe;
    }
    tr:hover td { background: #f8fbfc; }
    tr.selected td { background: #eaf5f7; }
    .mono {
      font-family: Consolas, monospace;
      font-size: 12px;
    }
    .muted { color: var(--muted); }
    .status {
      min-height: 20px;
      color: var(--muted);
    }
    .status.error { color: var(--danger); }
    .status.ok { color: var(--ok); }
    #textClean {
      width: 100%;
      min-height: 280px;
      resize: vertical;
      line-height: 1.45;
    }
    pre {
      max-height: 360px;
      overflow: auto;
      background: #0f172a;
      color: #e5e7eb;
      padding: 12px;
      border-radius: 6px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .field-grid {
      display: grid;
      grid-template-columns: 120px 1fr;
      gap: 8px 10px;
      align-items: center;
      margin-bottom: 10px;
    }
    .detail-title {
      margin: 0 0 10px;
      font-size: 16px;
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      header { align-items: flex-start; flex-direction: column; }
      #tokenInput { width: min(100%, 360px); }
    }
  </style>
</head>
<body>
  <header>
    <h1>Knowledge Base Admin</h1>
    <div class="toolbar" style="border: 0; padding: 0;">
      <input id="tokenInput" type="password" autocomplete="off" placeholder="ADMIN_AUTH_TOKEN">
      <button id="saveTokenButton" type="button">Сохранить токен</button>
      <button id="validateButton" class="secondary" type="button">Validation</button>
      <button id="qualityButton" class="secondary" type="button">Quality check</button>
    </div>
  </header>
  <main>
    <section>
      <div class="toolbar">
        <input id="searchInput" type="search" placeholder="Поиск по чанкам">
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
        <button id="saveChunkButton" type="button" disabled>Сохранить</button>
        <button id="relatedCasesButton" class="secondary" type="button" disabled>Eval cases</button>
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
          <td>${item.status || ""}</td>
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
        setStatus("listStatus", `Найдено: ${data.total}`, "ok");
      } catch (error) {
        setStatus("listStatus", error.message, "error");
      }
    }
    async function loadChunk(chunkId, row) {
      try {
        selectedChunkId = chunkId;
        document.querySelectorAll("tr.selected").forEach((el) => el.classList.remove("selected"));
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
        const data = await requestJson("/admin/kb/chunks/" + encodeURIComponent(selectedChunkId), {
          method: "PATCH",
          body: JSON.stringify({
            status: document.getElementById("chunkStatus").value,
            text_clean: document.getElementById("textClean").value,
          }),
        });
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        setStatus("detailStatus", "Сохранено", "ok");
        await loadChunks();
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
    document.getElementById("relatedCasesButton").addEventListener("click", showRelatedCases);
    document.getElementById("validateButton").addEventListener("click", showValidation);
    document.getElementById("qualityButton").addEventListener("click", showQualityCheck);
  </script>
</body>
</html>
"""
