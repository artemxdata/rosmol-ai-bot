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
  <title>Импульс кода | Админка знаний</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #061d39;
      --ink-2: #0b315f;
      --bg: #eef3f8;
      --surface: #ffffff;
      --surface-2: #f7fafc;
      --line: #d7e1ec;
      --muted: #64748b;
      --text: #102033;
      --accent: #fbdb24;
      --accent-2: #0b7f96;
      --accent-soft: #e6f7fa;
      --danger: #b42318;
      --ok: #027a48;
      --shadow: 0 18px 45px rgba(6, 29, 57, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(180deg, rgba(6, 29, 57, 0.08), transparent 260px),
        var(--bg);
      color: var(--text);
      font-family: Inter, Arial, sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    button, input, select, textarea {
      font: inherit;
      letter-spacing: 0;
    }
    button {
      min-height: 40px;
      border: 1px solid transparent;
      border-radius: 7px;
      padding: 9px 13px;
      background: var(--ink);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }
    button:hover { filter: brightness(1.04); }
    button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    button.primary {
      background: var(--accent);
      color: var(--ink);
    }
    button.secondary {
      background: #fff;
      border-color: var(--line);
      color: var(--ink);
    }
    button.danger {
      background: #fff;
      border-color: #f2b8b5;
      color: var(--danger);
    }
    input, select, textarea {
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      color: var(--text);
      padding: 10px 12px;
      min-height: 40px;
    }
    input:focus, select:focus, textarea:focus {
      outline: 3px solid rgba(11, 127, 150, 0.16);
      border-color: var(--accent-2);
    }
    .topbar {
      min-height: 78px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 18px;
      padding: 16px 22px;
      background: var(--ink);
      color: #fff;
      position: sticky;
      top: 0;
      z-index: 5;
      box-shadow: 0 12px 30px rgba(6, 29, 57, 0.2);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 16px;
      min-width: 0;
    }
    .logo-mark {
      display: grid;
      place-items: center;
      width: 218px;
      height: 58px;
      flex: 0 0 auto;
      border-radius: 8px;
      background: #fff;
      padding: 8px 14px;
    }
    .logo-mark img {
      width: 100%;
      max-width: 184px;
      max-height: 40px;
      height: 100%;
      display: block;
      object-fit: contain;
    }
    .brand-copy {
      display: flex;
      flex-direction: column;
      gap: 3px;
      min-width: 0;
    }
    h1 {
      margin: 0;
      font-size: 19px;
      line-height: 1.1;
      font-weight: 900;
    }
    .subtitle {
      color: #bfd1e6;
      font-size: 12px;
      line-height: 1.3;
      max-width: 460px;
    }
    .top-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
      max-width: 720px;
    }
    .top-actions button.secondary {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.18);
      color: #fff;
    }
    .top-actions button.primary {
      box-shadow: 0 0 0 1px rgba(251, 219, 36, 0.25);
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 34px;
      border-radius: 999px;
      padding: 6px 11px;
      background: rgba(255, 255, 255, 0.1);
      color: #e8f1fb;
      font-size: 12px;
      font-weight: 800;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent);
    }
    .auth {
      max-width: 560px;
      margin: 58px auto;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .auth-head {
      padding: 24px;
      background: linear-gradient(135deg, var(--ink), var(--ink-2));
      color: #fff;
    }
    .auth-head h2 {
      margin: 0 0 8px;
      font-size: 24px;
    }
    .auth-head p {
      margin: 0;
      color: #c9d8e8;
      line-height: 1.45;
    }
    .auth-body {
      padding: 22px;
      display: grid;
      gap: 12px;
    }
    .auth-body input { width: 100%; }
    .layout {
      display: grid;
      grid-template-columns: minmax(420px, 0.88fr) minmax(560px, 1.18fr);
      gap: 18px;
      padding: 18px;
    }
    .panel {
      min-width: 0;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }
    .panel-title {
      margin: 0;
      color: var(--ink);
      font-size: 15px;
      font-weight: 900;
    }
    .search-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(168px, 1fr));
      gap: 10px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--surface-2);
    }
    .search-grid input,
    .search-grid select,
    .search-grid button {
      width: 100%;
      min-width: 0;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .metric {
      min-height: 78px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background:
        linear-gradient(135deg, rgba(251, 219, 36, 0.16), transparent 70%),
        var(--surface-2);
    }
    .metric-value {
      display: block;
      color: var(--ink);
      font-size: 23px;
      font-weight: 900;
      line-height: 1;
      margin-bottom: 8px;
    }
    .metric-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .status {
      min-height: 22px;
      color: var(--muted);
      font-size: 13px;
    }
    .status.error { color: var(--danger); }
    .status.warn { color: #8a6500; }
    .status.ok { color: var(--ok); }
    .table-wrap {
      max-height: calc(100vh - 324px);
      min-height: 360px;
      overflow: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 11px 12px;
      vertical-align: top;
      text-align: left;
      word-break: break-word;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 2;
      background: #f2f6fa;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
    }
    tbody tr { cursor: pointer; }
    tbody tr:hover td { background: #f5fbfc; }
    tr.selected td {
      background: var(--accent-soft);
      border-bottom-color: #bce5ec;
    }
    .mono {
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
    }
    .muted { color: var(--muted); }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 5px 10px;
      background: var(--accent-soft);
      color: var(--accent-2);
      font-size: 12px;
      font-weight: 900;
      max-width: 100%;
      white-space: nowrap;
    }
    .badge.status-draft {
      background: #fff8db;
      color: #8a6500;
    }
    .badge.status-archived {
      background: #f1f5f9;
      color: #64748b;
    }
    .editor-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .editor-body {
      padding: 16px;
    }
    .field-grid {
      display: grid;
      grid-template-columns: 110px 1fr;
      gap: 10px 12px;
      align-items: center;
      margin-bottom: 14px;
    }
    .detail-title {
      margin: 0 0 14px;
      color: var(--ink);
      font-size: 18px;
      font-weight: 900;
    }
    #textClean {
      width: 100%;
      min-height: 360px;
      resize: vertical;
      line-height: 1.55;
      font-size: 15px;
    }
    pre {
      max-height: 300px;
      overflow: auto;
      margin: 14px 0 0;
      background: #07152a;
      color: #e7eef8;
      padding: 14px;
      border-radius: 8px;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 12px;
    }
    .ops-dashboard {
      display: grid;
      gap: 12px;
      margin: 14px 0;
    }
    .quality-dashboard {
      display: grid;
      gap: 12px;
      margin: 14px 0;
    }
    .ops-kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .ops-section {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .ops-section h3 {
      margin: 0;
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      background: #f7fafc;
      color: var(--ink);
      font-size: 13px;
      font-weight: 900;
    }
    .ops-list {
      display: grid;
      gap: 0;
    }
    .ops-item {
      display: grid;
      gap: 4px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
    }
    .ops-item:last-child { border-bottom: 0; }
    .ops-line {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--ink);
      font-weight: 800;
    }
    .ops-meta {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .ops-preview {
      color: var(--text);
      font-size: 13px;
      line-height: 1.4;
    }
    .quality-note {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
      color: var(--text);
      line-height: 1.45;
    }
    .quality-ok {
      color: var(--ok);
      font-weight: 900;
    }
    .quality-bad {
      color: var(--danger);
      font-weight: 900;
    }
    .toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-weight: 700;
      user-select: none;
    }
    .toggle input {
      width: 16px;
      height: 16px;
      min-height: 16px;
    }
    .hidden { display: none !important; }
    @media (max-width: 1180px) {
      .topbar {
        grid-template-columns: 1fr;
        align-items: start;
      }
      .top-actions {
        justify-content: start;
        max-width: none;
      }
      .layout { grid-template-columns: 1fr; }
      .table-wrap { max-height: none; }
      .search-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      .topbar { padding: 14px; }
      .top-actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
        width: 100%;
      }
      .top-actions .status-pill {
        grid-column: 1 / -1;
        justify-content: center;
      }
      .top-actions button { width: 100%; }
      .brand {
        align-items: center;
        gap: 12px;
      }
      .logo-mark {
        width: 190px;
        height: 54px;
      }
      .logo-mark img {
        max-width: 160px;
        max-height: 36px;
      }
      .layout { padding: 10px; }
      .search-grid, .metrics, .field-grid, .ops-kpis { grid-template-columns: 1fr; }
    }
    @media (max-width: 640px) {
      .table-wrap {
        overflow: visible;
        min-height: 0;
      }
      table, thead, tbody, tr, th, td {
        display: block;
        width: 100%;
      }
      thead { display: none; }
      tbody {
        display: grid;
        gap: 10px;
        padding: 12px;
      }
      tbody tr {
        border: 1px solid var(--line);
        border-radius: 9px;
        background: #fff;
        overflow: hidden;
      }
      td {
        border-bottom: 0;
        padding: 8px 12px;
      }
      td::before {
        content: attr(data-label);
        display: block;
        margin-bottom: 3px;
        color: var(--muted);
        font-size: 11px;
        font-weight: 900;
        text-transform: uppercase;
      }
    }
    @media (max-width: 520px) {
      .brand {
        align-items: flex-start;
        flex-direction: column;
      }
      .brand-copy h1 { font-size: 22px; }
      .logo-mark {
        width: 220px;
        max-width: 100%;
      }
      .top-actions {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <div class="logo-mark">
        <img src="__LOGO_DATA_URI__" alt="Импульс кода">
      </div>
      <div class="brand-copy">
        <h1>Админка знаний</h1>
        <span class="subtitle">RAG-корпус Росмолодёжи · правка чанков · обновление индекса</span>
      </div>
    </div>
    <div class="top-actions">
      <span class="status-pill">
        <span class="status-dot"></span>
        <span id="authState">проверка доступа</span>
      </span>
      <button id="validateButton" class="secondary" type="button">Проверка базы</button>
      <button id="qualityButton" class="secondary" type="button">Отчёт качества</button>
      <button id="opsButton" class="secondary" type="button">Работа бота</button>
      <button id="logoutButton" class="danger" type="button">Выйти</button>
    </div>
  </header>

  <section id="authPanel" class="auth hidden">
    <div class="auth-head">
      <h2>Вход в админку</h2>
      <p>
        Токен вводится один раз. Сервер сохранит безопасную HttpOnly-cookie,
        сам токен не попадёт в HTML и не будет храниться в JavaScript.
      </p>
    </div>
    <div class="auth-body">
      <input id="tokenInput" type="password" autocomplete="off" placeholder="ADMIN_AUTH_TOKEN">
      <button id="loginButton" class="primary" type="button">Войти</button>
      <div id="authStatus" class="status"></div>
    </div>
  </section>

  <main id="appShell" class="layout hidden">
    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">База знаний</h2>
        <div id="listStatus" class="status"></div>
      </div>
      <div class="search-grid">
        <input id="searchInput" type="search" placeholder="Поиск: Амур, проезд, сертификат">
        <select id="statusFilter">
          <option value="">Все статусы</option>
          <option value="published">Опубликованные</option>
          <option value="draft">Черновики</option>
          <option value="archived">Архивные</option>
        </select>
        <input id="forumFilter" type="text" placeholder="Форум">
        <input id="categoryFilter" type="text" placeholder="Категория">
        <button id="loadButton" type="button">Найти</button>
      </div>
      <div class="metrics">
        <div class="metric">
          <span id="metricFound" class="metric-value">-</span>
          <span class="metric-label">найдено по фильтру</span>
        </div>
        <div class="metric">
          <span id="metricValid" class="metric-value">-</span>
          <span class="metric-label">валидных чанков</span>
        </div>
        <div class="metric">
          <span id="metricEval" class="metric-value">-</span>
          <span class="metric-label">отчёт качества</span>
        </div>
        <div class="metric">
          <span id="metricOps" class="metric-value">-</span>
          <span class="metric-label">запросов за 7 дней</span>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width: 26%;">ID чанка</th>
              <th style="width: 16%;">статус</th>
              <th style="width: 18%;">форум</th>
              <th>текст</th>
            </tr>
          </thead>
          <tbody id="chunksTable"></tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">Редактор чанка</h2>
        <div class="editor-actions">
          <button id="saveChunkButton" class="primary" type="button" disabled>
            Сохранить и обновить RAG
          </button>
          <button id="reindexButton" class="secondary" type="button" disabled>
            Только обновить индекс
          </button>
          <button id="relatedCasesButton" class="secondary" type="button" disabled>
            Тест-кейсы
          </button>
        </div>
      </div>
      <div class="editor-body">
        <div class="editor-actions" style="margin-bottom: 12px;">
          <label class="toggle">
            <input id="reindexToggle" type="checkbox" checked>
            сразу обновлять Qdrant и сбрасывать семантический кэш
          </label>
          <span id="detailStatus" class="status"></span>
        </div>
        <h2 id="detailTitle" class="detail-title muted">Чанк не выбран</h2>
        <div class="field-grid">
          <label for="chunkStatus">Статус</label>
          <select id="chunkStatus" disabled>
            <option value="published">Опубликован</option>
            <option value="draft">Черновик</option>
            <option value="archived">Архив</option>
          </select>
          <div class="muted">Форум</div>
          <div id="chunkForum" class="mono"></div>
          <div class="muted">Тема</div>
          <div id="chunkTopic" class="mono"></div>
          <div class="muted">Источник</div>
          <div id="chunkSource" class="mono"></div>
        </div>
        <textarea
          id="textClean"
          disabled
          placeholder="Выбери чанк слева, чтобы редактировать текст ответа."
        ></textarea>
        <div id="qualityDashboard" class="quality-dashboard hidden"></div>
        <div id="opsDashboard" class="ops-dashboard hidden"></div>
        <pre id="reportOutput"></pre>
      </div>
    </section>
  </main>

  <script>
    let selectedChunkId = "";

    function setStatus(id, message, cls = "") {
      const el = document.getElementById(id);
      el.className = "status " + cls;
      el.textContent = message;
    }
    function setMetric(id, value) {
      document.getElementById(id).textContent = String(value);
    }
    function setAuthenticated(isAuthenticated) {
      document.getElementById("authPanel").classList.toggle("hidden", isAuthenticated);
      document.getElementById("appShell").classList.toggle("hidden", !isAuthenticated);
      document.getElementById("authState").textContent = isAuthenticated
        ? "доступ открыт"
        : "нужен вход";
    }
    function adminErrorMessage(status, detail) {
      const raw = String(detail || "").trim();
      const normalized = raw.toLowerCase();
      if (status === 503) {
        if (
          normalized.includes("service unavailable") ||
          normalized.includes("ml") ||
          normalized.includes("index") ||
          normalized.includes("индекс")
        ) {
          return (
            "ML-сервис для обновления индекса временно недоступен. " +
            "Текст мог сохраниться, но RAG-индекс нужно обновить после восстановления app-ml."
          );
        }
        return "Сервис временно недоступен. Проверь app-ml и повтори действие.";
      }
      if (status === 504) {
        return (
          "Операция заняла слишком много времени. " +
          "Проверь, сохранился ли текст, затем обнови индекс."
        );
      }
      return raw || "Неизвестная ошибка";
    }
    function transportErrorMessage(error, timeoutMs) {
      if (error.name === "AbortError") {
        const seconds = Math.round(timeoutMs / 1000);
        return (
          `Операция не завершилась за ${seconds} сек. ` +
          "Проверь статус сервисов и повтори действие. " +
          "Если это сохранение текста, открой чанк заново: " +
          "текст мог сохраниться, а индекс мог не обновиться."
        );
      }
      const raw = String(error.message || error || "").trim();
      const normalized = raw.toLowerCase();
      if (
        normalized.includes("failed to fetch") ||
        normalized.includes("networkerror") ||
        normalized.includes("service unavailable") ||
        normalized.includes("load failed")
      ) {
        return (
          "Админка временно не получила ответ от сервиса. " +
          "Проверь /ready и app-ml, затем обнови страницу или повтори действие."
        );
      }
      return raw || "Не удалось выполнить запрос к админке.";
    }
    function setEditorBusy(isBusy) {
      if (!selectedChunkId) return;
      document.getElementById("saveChunkButton").disabled = isBusy;
      document.getElementById("reindexButton").disabled = isBusy;
      document.getElementById("relatedCasesButton").disabled = isBusy;
      document.getElementById("textClean").disabled = isBusy;
      document.getElementById("chunkStatus").disabled = isBusy;
    }
    async function requestJson(path, options = {}) {
      const timeoutMs = options.timeoutMs || 240000;
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(path, {
          credentials: "same-origin",
          ...options,
          signal: controller.signal,
          headers: {
            ...(options.body !== undefined ? {"Content-Type": "application/json"} : {}),
            ...(options.headers || {}),
          },
        }).catch((error) => {
          throw new Error(transportErrorMessage(error, timeoutMs));
        });
        const text = await response.text();
        let payload = {};
        if (text) {
          try { payload = JSON.parse(text); } catch { payload = {raw: text}; }
        }
        if (!response.ok) {
          if (response.status === 401) {
            setAuthenticated(false);
          }
          const detail = payload.detail || payload.raw || response.statusText;
          throw new Error(adminErrorMessage(response.status, detail));
        }
        return payload;
      } finally {
        window.clearTimeout(timer);
      }
    }
    function queryString(params) {
      const search = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) {
        if (value) search.set(key, value);
      }
      return search.toString();
    }
    function badgeClass(status) {
      if (status === "draft") return "badge status-draft";
      if (status === "archived") return "badge status-archived";
      return "badge";
    }
    function statusLabel(status) {
      if (status === "published") return "опубликован";
      if (status === "draft") return "черновик";
      if (status === "archived") return "архив";
      return status || "не задан";
    }
    function fallbackLabel(value) {
      return value || "не определено";
    }
    function escapeHtml(value) {
      return String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }
    function formatPercent(value) {
      if (value === null || value === undefined || value === "") {
        return "n/a";
      }
      const number = Number(value || 0);
      return (number * 100).toFixed(1) + "%";
    }
    function formatRub(value) {
      return Number(value || 0).toFixed(2) + " ₽";
    }
    function hideOpsDashboard() {
      const dashboard = document.getElementById("opsDashboard");
      dashboard.classList.add("hidden");
      dashboard.innerHTML = "";
    }
    function hideQualityDashboard() {
      const dashboard = document.getElementById("qualityDashboard");
      dashboard.classList.add("hidden");
      dashboard.innerHTML = "";
    }
    function hideReportDashboards() {
      hideOpsDashboard();
      hideQualityDashboard();
    }
    function opsRows(items, fields) {
      const rows = (items || []).slice(0, 6);
      if (!rows.length) {
        return '<div class="ops-item"><span class="ops-meta">Нет данных</span></div>';
      }
      return rows.map((item) => {
        const title = fields.title(item);
        const count = item.requests !== undefined ? `${item.requests} запр.` : "";
        const meta = fields.meta(item);
        const preview = fields.preview ? fields.preview(item) : "";
        return `
          <div class="ops-item">
            <div class="ops-line">
              <span>${escapeHtml(title)}</span>
              <span>${escapeHtml(count)}</span>
            </div>
            ${meta ? `<div class="ops-meta">${escapeHtml(meta)}</div>` : ""}
            ${preview ? `<div class="ops-preview">${escapeHtml(preview)}</div>` : ""}
          </div>
        `;
      }).join("");
    }
    function renderOpsDashboard(data) {
      const summary = data.summary || {};
      const dashboard = document.getElementById("opsDashboard");
      dashboard.classList.remove("hidden");
      dashboard.innerHTML = `
        <div class="ops-kpis">
          <div class="metric">
            <span class="metric-value">${escapeHtml(summary.request_count || 0)}</span>
            <span class="metric-label">запросов за ${escapeHtml(data.days || 7)} дней</span>
          </div>
          <div class="metric">
            <span class="metric-value">
              ${escapeHtml(formatPercent(summary.expected_escalation_rate))}
            </span>
            <span class="metric-label">ожидаемые эскалации</span>
          </div>
          <div class="metric">
            <span class="metric-value">
              ${escapeHtml(formatPercent(summary.quality_issue_rate))}
            </span>
            <span class="metric-label">проблемы качества</span>
          </div>
          <div class="metric">
            <span class="metric-value">
              ${escapeHtml(formatRub(summary.llm_estimated_cost_rub))}
            </span>
            <span class="metric-label">стоимость LLM</span>
          </div>
        </div>
        <div class="ops-section">
          <h3>Проблемные темы</h3>
          <div class="ops-list">
            ${opsRows(data.failed_topics, {
              title: (item) => fallbackLabel(item.topic),
              meta: (item) => [
                `форум=${fallbackLabel(item.forum)}`,
                `причина=${fallbackLabel(item.reason)}`,
              ].join(" · "),
            })}
          </div>
        </div>
        <div class="ops-section">
          <h3>Проблемные форумы</h3>
          <div class="ops-list">
            ${opsRows(data.failed_forums, {
              title: (item) => fallbackLabel(item.forum),
              meta: (item) => `причина=${fallbackLabel(item.reason)}`,
            })}
          </div>
        </div>
        <div class="ops-section">
          <h3>Последние эскалации</h3>
          <div class="ops-list">
            ${opsRows(data.recent_escalations, {
              title: (item) => fallbackLabel(item.reason),
              meta: (item) => [
                fallbackLabel(item.channel),
                fallbackLabel(item.forum),
                `${item.total_latency_ms || 0} мс`,
              ].join(" · "),
              preview: (item) => item.message_preview || "",
            })}
          </div>
        </div>
      `;
    }
    function qualityCasesCount(name, section) {
      if (!section) return 0;
      if (name === "followup") return section.turns_total || 0;
      return section.cases_total || section.cases || 0;
    }
    function qualityPassRate(name, section) {
      if (!section) return null;
      if (name === "followup") return section.turn_pass_rate;
      return section.pass_rate;
    }
    function qualitySectionTitle(name) {
      const titles = {
        forums: "Форумы и составные вопросы",
        safety: "Safety-сценарии",
        off_topic: "Вопросы вне базы",
        pii: "Персональные данные",
        followup: "Контекст диалога",
        typical: "Типовые вопросы",
        atypical: "Нетиповые вопросы",
        controls: "Контрольные проверки",
      };
      return titles[name] || name;
    }
    function qualitySections(report) {
      if (!report) return [];
      if (report.sections) {
        return Object.entries(report.sections).map(([name, section]) => ({name, section}));
      }
      const names = ["typical", "atypical", "safety"];
      const items = names
        .filter((name) => report[name])
        .map((name) => ({name, section: report[name]}));
      if (report.controls) {
        for (const [name, section] of Object.entries(report.controls)) {
          items.push({name, section});
        }
      }
      return items;
    }
    function renderQualityDashboard(data) {
      const validation = data.validation || {};
      const report = data.latest_eval_report || {};
      const reportExists = Boolean(data.latest_eval_report_exists);
      const passed = reportExists ? Boolean(report.passed ?? report.total_pass_rate >= 0.9) : false;
      const totalChecks = report.total_checks_or_turns || report.cases_total || "-";
      const passRate = report.total_pass_rate ?? report.pass_rate;
      const cost = report.llm_estimated_cost_rub ?? report.total_llm_estimated_cost_rub ?? 0;
      const dashboard = document.getElementById("qualityDashboard");
      dashboard.classList.remove("hidden");
      const sectionRows = qualitySections(report).map(({name, section}) => {
        const failures = section.failure_reason_counts || {};
        const failureText = Object.keys(failures).length
          ? Object.entries(failures).map(([key, value]) => `${key}: ${value}`).join(", ")
          : "ошибок нет";
        return `
          <div class="ops-item">
            <div class="ops-line">
              <span>${escapeHtml(qualitySectionTitle(name))}</span>
              <span>${escapeHtml(formatPercent(qualityPassRate(name, section)))}</span>
            </div>
            <div class="ops-meta">
              кейсов: ${escapeHtml(qualityCasesCount(name, section))} ·
              trace: ${escapeHtml(formatPercent(section.trace_coverage_rate))} ·
              стоимость: ${escapeHtml(formatRub(section.llm_estimated_cost_rub))}
            </div>
            <div class="ops-preview">${escapeHtml(failureText)}</div>
          </div>
        `;
      }).join("");
      dashboard.innerHTML = `
        <div class="ops-kpis">
          <div class="metric">
            <span class="metric-value">${escapeHtml(validation.valid_records || "-")}</span>
            <span class="metric-label">валидных чанков</span>
          </div>
          <div class="metric">
            <span class="metric-value">${escapeHtml(totalChecks)}</span>
            <span class="metric-label">проверок в отчёте</span>
          </div>
          <div class="metric">
            <span class="metric-value">${escapeHtml(formatPercent(passRate))}</span>
            <span class="metric-label">pass rate</span>
          </div>
          <div class="metric">
            <span class="metric-value">${escapeHtml(formatRub(cost))}</span>
            <span class="metric-label">стоимость LLM</span>
          </div>
        </div>
        <div class="quality-note">
          Статус отчёта:
          <span class="${passed ? "quality-ok" : "quality-bad"}">
            ${passed ? "пройден" : (reportExists ? "требует внимания" : "отчёт не найден")}
          </span>.
          Проверка базы показывает, что seed валиден, а отчёт качества показывает,
          как бот прошёл форумы, safety, off-topic, PII и follow-up без массовых запросов в HDE.
        </div>
        <div class="ops-section">
          <h3>Блоки quality gate</h3>
          <div class="ops-list">
            ${sectionRows || [
              '<div class="ops-item">',
              '<span class="ops-meta">Отчёт качества ещё не сформирован</span>',
              '</div>',
            ].join("")}
          </div>
        </div>
      `;
    }
    function renderRows(items) {
      const tbody = document.getElementById("chunksTable");
      tbody.innerHTML = "";
      for (const item of items) {
        const tr = document.createElement("tr");
        tr.dataset.chunkId = item.chunk_id;
        tr.innerHTML = `
          <td data-label="ID чанка" class="mono">${escapeHtml(item.chunk_id)}</td>
          <td data-label="статус">
            <span class="${badgeClass(item.status)}">${escapeHtml(statusLabel(item.status))}</span>
          </td>
          <td data-label="форум">${escapeHtml(item.forum_normalized)}</td>
          <td data-label="текст">${escapeHtml(item.text_preview)}</td>
        `;
        tr.addEventListener("click", () => loadChunk(item.chunk_id, tr));
        tbody.appendChild(tr);
      }
    }
    async function login() {
      const token = document.getElementById("tokenInput").value.trim();
      if (!token) {
        setStatus("authStatus", "Вставь ADMIN_AUTH_TOKEN.", "error");
        return;
      }
      try {
        setStatus("authStatus", "Проверяю доступ...");
        await requestJson("/admin/kb/login", {
          method: "POST",
          body: JSON.stringify({token}),
        });
        document.getElementById("tokenInput").value = "";
        setAuthenticated(true);
        await boot();
      } catch (error) {
        setStatus("authStatus", error.message, "error");
      }
    }
    async function logout() {
      try {
        await requestJson("/admin/kb/logout", {method: "POST", body: "{}"});
      } catch {
        // Ignore logout transport errors; local UI still must close the session state.
      }
      setAuthenticated(false);
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
        hideReportDashboards();
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
        document.getElementById("chunkTopic").textContent = data.topic || data.intent_name || "";
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
        hideReportDashboards();
        setEditorBusy(true);
        const shouldReindex = document.getElementById("reindexToggle").checked;
        setStatus(
          "detailStatus",
          shouldReindex
            ? "Сохраняю текст и обновляю RAG-индекс. Это может занять до минуты..."
            : "Сохраняю текст без обновления индекса..."
        );
        const data = await requestJson(
          "/admin/kb/chunks/" + encodeURIComponent(selectedChunkId),
          {
            method: "PATCH",
            timeoutMs: shouldReindex ? 90000 : 30000,
            body: JSON.stringify({
              status: document.getElementById("chunkStatus").value,
              text_clean: document.getElementById("textClean").value,
              reindex: shouldReindex,
            }),
          }
        );
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        if (data.reindex && data.reindex.ok === false) {
          const reason = data.reindex.error || "индекс не обновлён";
          setStatus(
            "detailStatus",
            "Текст сохранён, но RAG-индекс не обновлён: " +
              reason +
              ". Нажми «Только обновить индекс», когда app-ml будет готов.",
            "warn"
          );
        } else {
          const reindex = data.reindex && data.reindex.ok
            ? " Qdrant обновлён, кэш сброшен."
            : "";
          setStatus("detailStatus", "Сохранено." + reindex, "ok");
        }
        await loadChunks();
      } catch (error) {
        setStatus(
          "detailStatus",
          "Сохранение не подтверждено: " +
            error.message +
            " Открой этот чанк заново и проверь текст перед повторной правкой.",
          "error"
        );
      } finally {
        setEditorBusy(false);
      }
    }
    async function reindexChunk() {
      if (!selectedChunkId) return;
      try {
        hideReportDashboards();
        setEditorBusy(true);
        setStatus("detailStatus", "Обновляю RAG-индекс и сбрасываю semantic cache...");
        const data = await requestJson(
          "/admin/kb/chunks/" + encodeURIComponent(selectedChunkId) + "/reindex",
          {method: "POST", body: "{}", timeoutMs: 90000}
        );
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        setStatus("detailStatus", "Qdrant обновлён, семантический кэш сброшен", "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      } finally {
        setEditorBusy(false);
      }
    }
    async function showRelatedCases() {
      if (!selectedChunkId) return;
      try {
        hideReportDashboards();
        setStatus("detailStatus", "Загрузка тест-кейсов...");
        const data = await requestJson(
          "/admin/kb/chunks/" + encodeURIComponent(selectedChunkId) + "/eval-cases",
          {method: "GET"}
        );
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        setStatus("detailStatus", `Тест-кейсов: ${data.total}`, "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function showValidation() {
      try {
        hideReportDashboards();
        const data = await requestJson("/admin/kb/validate", {method: "POST", body: "{}"});
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        setMetric("metricValid", data.valid_records);
        setStatus("detailStatus", `База валидна: ${data.valid_records}`, "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function showQualityCheck() {
      try {
        hideOpsDashboard();
        const data = await requestJson("/admin/kb/quality-check", {
          method: "POST",
          body: JSON.stringify({include_latest_eval_report: true}),
        });
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        renderQualityDashboard(data);
        const report = data.latest_eval_report_exists ? "есть" : "нет";
        setMetric("metricEval", report);
        setStatus("detailStatus", "Отчёт качества загружен", "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function showOpsReport() {
      try {
        hideQualityDashboard();
        const data = await requestJson("/admin/kb/ops-report?days=7", {method: "GET"});
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        renderOpsDashboard(data);
        const summary = data.summary || {};
        const requests = summary.request_count || 0;
        const cost = Number(summary.llm_estimated_cost_rub || 0).toFixed(2);
        setMetric("metricOps", requests);
        setStatus("detailStatus", `Работа бота: ${requests} запросов, ${cost} ₽`, "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function boot() {
      setAuthenticated(true);
      await Promise.allSettled([
        loadChunks(),
        showValidation(),
        showQualityCheck(),
      ]);
      await showOpsReport();
    }
    async function checkSession() {
      try {
        await requestJson("/admin/kb/validate", {method: "POST", body: "{}"});
        await boot();
      } catch {
        setAuthenticated(false);
      }
    }

    document.getElementById("loginButton").addEventListener("click", login);
    document.getElementById("tokenInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter") login();
    });
    document.getElementById("logoutButton").addEventListener("click", logout);
    document.getElementById("loadButton").addEventListener("click", loadChunks);
    document.getElementById("saveChunkButton").addEventListener("click", saveChunk);
    document.getElementById("reindexButton").addEventListener("click", reindexChunk);
    document.getElementById("relatedCasesButton").addEventListener("click", showRelatedCases);
    document.getElementById("validateButton").addEventListener("click", showValidation);
    document.getElementById("qualityButton").addEventListener("click", showQualityCheck);
    document.getElementById("opsButton").addEventListener("click", showOpsReport);
    checkSession();
  </script>
</body>
</html>
"""


ADMIN_KB_HTML = _HTML_TEMPLATE.replace("__LOGO_DATA_URI__", _logo_data_uri())
