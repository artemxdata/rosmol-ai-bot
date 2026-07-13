# ruff: noqa: E501

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
      --shell: #080b10;
      --sidebar: #0d1118;
      --sidebar-hover: #171d26;
      --canvas: #f4f6f8;
      --surface: #ffffff;
      --surface-soft: #f8fafb;
      --surface-hover: #f1f5f7;
      --line: #dde3e8;
      --line-strong: #c9d2da;
      --text: #17202b;
      --text-strong: #071425;
      --muted: #6b7787;
      --muted-dark: #98a5b5;
      --accent: #f5d90a;
      --accent-strong: #e9c900;
      --cyan: #00a4bd;
      --cyan-soft: #e3f7fa;
      --danger: #c9362b;
      --danger-soft: #fff0ef;
      --ok: #0b8a5f;
      --ok-soft: #e7f7f0;
      --warn: #9a6c00;
      --warn-soft: #fff8d8;
      --shadow: 0 14px 38px rgba(8, 15, 24, 0.10);
      --font: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --mono: "Cascadia Code", "JetBrains Mono", Consolas, monospace;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      background: var(--canvas);
      color: var(--text);
      font-family: var(--font);
      font-size: 14px;
      letter-spacing: 0;
    }
    button, input, select, textarea { font: inherit; letter-spacing: 0; }
    button {
      min-height: 36px;
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 8px 12px;
      background: #111927;
      color: #fff;
      font-weight: 700;
      cursor: pointer;
      transition: background 120ms ease, border-color 120ms ease, color 120ms ease, transform 120ms ease;
    }
    button:hover:not(:disabled) { transform: translateY(-1px); }
    button:active:not(:disabled) { transform: translateY(0); }
    button:disabled { opacity: 0.46; cursor: not-allowed; }
    button.primary { background: var(--accent); color: #161400; }
    button.primary:hover:not(:disabled) { background: var(--accent-strong); }
    button.secondary { background: #fff; border-color: var(--line); color: var(--text-strong); }
    button.secondary:hover:not(:disabled) { background: var(--surface-hover); border-color: var(--line-strong); }
    button.danger { background: transparent; border-color: #61302f; color: #ffaaa3; }
    button.danger:hover:not(:disabled) { background: #2b1718; }
    button .icon { margin-right: 7px; }
    .icon {
      width: 16px;
      height: 16px;
      display: inline-block;
      vertical-align: -3px;
      fill: none;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      flex: 0 0 auto;
    }
    input, select, textarea {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 9px 11px;
      min-height: 38px;
    }
    input::placeholder, textarea::placeholder { color: #95a0ad; }
    input:focus, select:focus, textarea:focus {
      outline: 2px solid rgba(0, 164, 189, 0.15);
      border-color: var(--cyan);
    }
    .topbar {
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 9px 14px;
      background: var(--shell);
      color: #fff;
      border-bottom: 1px solid #202631;
      position: sticky;
      top: 0;
      z-index: 20;
    }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .logo-mark {
      display: grid;
      place-items: center;
      width: 142px;
      height: 38px;
      flex: 0 0 auto;
      border-radius: 5px;
      background: #fff;
      padding: 5px 10px;
      overflow: hidden;
    }
    .logo-mark img { width: 100%; height: 100%; display: block; object-fit: contain; }
    .brand-copy { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
    h1 { margin: 0; font-size: 15px; line-height: 1.2; font-weight: 750; white-space: nowrap; }
    .subtitle { color: var(--muted-dark); font-size: 12px; line-height: 1.3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .top-actions { display: flex; align-items: center; gap: 9px; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 30px;
      border-radius: 999px;
      padding: 5px 10px;
      background: #141a23;
      border: 1px solid #242d39;
      color: #cbd4df;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }
    .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 3px rgba(245, 217, 10, 0.08); }
    .product-chip { color: #758194; font-family: var(--mono); font-size: 11px; }
    .auth {
      max-width: 440px;
      margin: 72px auto;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
      color: var(--text);
    }
    .auth-head { padding: 22px; background: #10151d; color: #fff; border-bottom: 1px solid #222a35; }
    .auth-head h2 { margin: 0 0 7px; font-size: 20px; }
    .auth-head p { margin: 0; color: #aab5c3; line-height: 1.5; font-size: 13px; }
    .auth-body { padding: 20px; display: grid; gap: 10px; }
    .auth-body input { width: 100%; }
    .workspace { min-height: calc(100vh - 58px); display: grid; grid-template-columns: 214px minmax(0, 1fr); }
    .nav-rail {
      position: sticky;
      top: 58px;
      height: calc(100vh - 58px);
      display: flex;
      flex-direction: column;
      padding: 12px 9px;
      background: var(--sidebar);
      border-right: 1px solid #202631;
      color: #d8e0e9;
      overflow: auto;
    }
    .nav-label { padding: 8px 10px 7px; color: #647184; font-size: 10px; font-weight: 800; text-transform: uppercase; }
    .nav-group { display: grid; gap: 3px; }
    .nav-item {
      width: 100%;
      min-height: 38px;
      display: flex;
      align-items: center;
      justify-content: flex-start;
      gap: 9px;
      padding: 8px 10px;
      border: 1px solid transparent;
      border-radius: 5px;
      background: transparent;
      color: #aeb9c7;
      font-size: 13px;
      font-weight: 650;
      text-align: left;
    }
    .nav-item .icon { margin: 0; }
    .nav-item:hover:not(:disabled) { background: var(--sidebar-hover); color: #fff; transform: none; }
    .nav-item.active { background: #1b222d; border-color: #2b3543; color: #fff; }
    .nav-item.active::after { content: ""; width: 3px; height: 18px; margin-left: auto; border-radius: 2px; background: var(--accent); }
    .nav-spacer { flex: 1; min-height: 20px; }
    .nav-foot { display: grid; gap: 8px; padding-top: 12px; border-top: 1px solid #202631; }
    .nav-hint { padding: 0 10px; color: #647184; font-size: 11px; line-height: 1.45; }
    .content { min-width: 0; padding: 12px; overflow: hidden; }
    .layout { display: grid; grid-template-columns: minmax(390px, 0.82fr) minmax(560px, 1.35fr); gap: 10px; min-height: calc(100vh - 82px); }
    .panel {
      min-width: 0;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 7px;
      box-shadow: 0 1px 2px rgba(8, 15, 24, 0.04);
      overflow: hidden;
    }
    .panel-head {
      min-height: 51px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 9px 12px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .panel-title { margin: 0; color: var(--text-strong); font-size: 14px; font-weight: 750; }
    .panel-eyebrow { display: block; margin-bottom: 2px; color: var(--muted); font-size: 10px; font-weight: 750; text-transform: uppercase; }
    .search-grid {
      display: grid;
      grid-template-columns: minmax(190px, 1.45fr) minmax(130px, 0.85fr) minmax(105px, 0.7fr);
      gap: 7px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--surface-soft);
    }
    .search-grid input, .search-grid select, .search-grid button { width: 100%; min-width: 0; }
    .search-grid .category-field { grid-column: 1 / 2; }
    .search-grid .search-button { grid-column: 2 / 4; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .metric { min-height: 66px; padding: 11px 12px; border-right: 1px solid var(--line); background: #fff; }
    .metric:last-child { border-right: 0; }
    .metric-value { display: block; color: var(--text-strong); font-size: 19px; font-weight: 780; line-height: 1.1; margin-bottom: 5px; }
    .metric-label { color: var(--muted); font-size: 10px; font-weight: 700; line-height: 1.25; }
    .status { min-height: 18px; color: var(--muted); font-size: 12px; line-height: 1.45; }
    .status.error { color: var(--danger); }
    .status.warn { color: var(--warn); }
    .status.ok { color: var(--ok); }
    .list-status { display: inline-flex; align-items: center; min-height: 28px; padding: 4px 8px; border-radius: 5px; background: var(--surface-soft); }
    .table-wrap { height: calc(100vh - 276px); min-height: 400px; overflow: auto; background: #fff; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td { border-bottom: 1px solid #e8edf1; padding: 9px 10px; vertical-align: top; text-align: left; word-break: break-word; }
    th { position: sticky; top: 0; z-index: 2; background: #f7f9fb; color: #758194; font-size: 10px; font-weight: 800; text-transform: uppercase; }
    tbody tr { cursor: pointer; position: relative; }
    tbody tr:hover td { background: #f5f8fa; }
    tr.selected td { background: #e9f6f8; border-bottom-color: #cde8ec; }
    tr.selected td:first-child { box-shadow: inset 3px 0 0 var(--cyan); }
    .mono { font-family: var(--mono); font-size: 11px; }
    .muted { color: var(--muted); }
    .badge { display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 7px; background: var(--ok-soft); color: var(--ok); font-size: 10px; font-weight: 800; white-space: nowrap; }
    .badge.status-draft { background: var(--warn-soft); color: var(--warn); }
    .badge.status-archived { background: #eef1f4; color: #657181; }
    .empty-row { padding: 36px 16px; text-align: center; color: var(--muted); }
    .skeleton { height: 11px; border-radius: 3px; background: linear-gradient(90deg, #eef1f4 25%, #f8fafb 50%, #eef1f4 75%); background-size: 200% 100%; animation: shimmer 1.2s infinite; }
    @keyframes shimmer { to { background-position: -200% 0; } }
    .editor-panel { display: flex; flex-direction: column; min-height: 0; }
    .editor-panel.is-busy { cursor: progress; }
    .editor-panel.report-mode .document-head,
    .editor-panel.report-mode .editor-surface,
    .editor-panel.report-mode .editor-statusbar .toggle,
    .editor-panel.report-mode .panel-head .editor-actions {
      display: none;
    }
    .editor-panel.report-mode .editor-statusbar { justify-content: flex-end; }
    .editor-actions { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
    .editor-actions button { white-space: nowrap; }
    .editor-body { min-height: 0; padding: 0; display: flex; flex-direction: column; }
    .editor-statusbar { min-height: 40px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 7px 12px; border-bottom: 1px solid var(--line); background: var(--surface-soft); }
    .toggle { display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: 11px; font-weight: 650; user-select: none; }
    .toggle input { width: 15px; height: 15px; min-height: 15px; accent-color: var(--cyan); }
    .document-head { padding: 13px 14px 10px; border-bottom: 1px solid var(--line); }
    .document-path { display: flex; align-items: center; gap: 7px; margin-bottom: 9px; color: var(--muted); font-family: var(--mono); font-size: 10px; }
    .detail-title-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
    .detail-title { margin: 0; color: var(--text-strong); font-family: var(--mono); font-size: 15px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .dirty-indicator { display: inline-flex; align-items: center; gap: 5px; color: var(--warn); font-size: 11px; font-weight: 750; white-space: nowrap; }
    .dirty-indicator::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--accent-strong); }
    .field-grid { display: grid; grid-template-columns: minmax(190px, 1fr) repeat(3, minmax(110px, 0.6fr)); gap: 8px; align-items: stretch; margin-top: 11px; }
    .meta-field { min-width: 0; padding: 7px 9px; border: 1px solid var(--line); border-radius: 5px; background: var(--surface-soft); }
    .meta-label { display: block; margin-bottom: 4px; color: var(--muted); font-size: 9px; font-weight: 800; text-transform: uppercase; }
    .meta-value { display: block; overflow: hidden; color: var(--text); text-overflow: ellipsis; white-space: nowrap; }
    .meta-field select { width: 100%; min-height: 27px; padding: 0; border: 0; background: transparent; outline: 0; }
    .editor-surface { position: relative; min-height: 360px; flex: 1; padding: 12px; background: #fff; }
    .editor-label { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 7px; color: var(--muted); font-size: 10px; font-weight: 750; text-transform: uppercase; }
    #textClean { width: 100%; height: calc(100vh - 334px); min-height: 320px; resize: vertical; border-color: transparent; border-radius: 4px; background: #fbfcfd; font-family: var(--font); font-size: 14px; line-height: 1.65; }
    #textClean:hover { border-color: var(--line); }
    #textClean:focus { background: #fff; border-color: var(--cyan); }
    .raw-report { margin: 0 12px 12px; border: 1px solid var(--line); border-radius: 5px; background: #fff; }
    .raw-report summary { padding: 9px 11px; color: var(--muted); cursor: pointer; font-size: 11px; font-weight: 700; }
    pre { max-height: 300px; overflow: auto; margin: 0; border-top: 1px solid #232b37; background: #0c1119; color: #d5deea; padding: 13px; white-space: pre-wrap; word-break: break-word; font-family: var(--mono); font-size: 11px; }
    .ops-dashboard, .quality-dashboard { display: grid; gap: 9px; padding: 12px; border-top: 1px solid var(--line); background: var(--surface-soft); }
    .ops-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
    .ops-kpis .metric { border-bottom: 0; }
    .ops-section { border: 1px solid var(--line); border-radius: 6px; background: #fff; overflow: hidden; }
    .ops-section h3 { margin: 0; padding: 10px 11px; border-bottom: 1px solid var(--line); background: #f7f9fb; color: var(--text-strong); font-size: 12px; font-weight: 750; }
    .ops-list { display: grid; }
    .ops-item { display: grid; gap: 4px; padding: 9px 11px; border-bottom: 1px solid var(--line); }
    .ops-item:last-child { border-bottom: 0; }
    .ops-line { display: flex; justify-content: space-between; gap: 10px; color: var(--text-strong); font-weight: 700; }
    .ops-meta { color: var(--muted); font-size: 11px; line-height: 1.4; }
    .ops-preview { color: var(--text); font-size: 12px; line-height: 1.45; }
    .quality-note { border-left: 3px solid var(--cyan); background: #edf8fa; padding: 10px 12px; color: var(--text); line-height: 1.45; }
    .quality-ok { color: var(--ok); font-weight: 800; }
    .quality-bad { color: var(--danger); font-weight: 800; }
    .toast-region { position: fixed; right: 16px; bottom: 16px; z-index: 50; display: grid; gap: 8px; width: min(380px, calc(100vw - 32px)); pointer-events: none; }
    .toast { display: flex; align-items: flex-start; gap: 9px; padding: 11px 12px; border: 1px solid #2c3542; border-radius: 6px; background: #111720; color: #e7edf5; box-shadow: 0 14px 32px rgba(0,0,0,.28); font-size: 12px; line-height: 1.45; animation: toast-in 180ms ease-out; }
    .toast.ok { border-left: 3px solid #38c793; }
    .toast.error { border-left: 3px solid #ef6b62; }
    .toast.warn { border-left: 3px solid var(--accent); }
    @keyframes toast-in { from { transform: translateY(8px); opacity: 0; } }
    .hidden { display: none !important; }
    @media (max-width: 1240px) {
      .workspace { grid-template-columns: 72px minmax(0, 1fr); }
      .nav-label, .nav-item span, .nav-hint { display: none; }
      .nav-item { justify-content: center; padding: 9px; }
      .nav-item.active::after { position: absolute; right: 5px; }
      .layout { grid-template-columns: minmax(350px, 0.78fr) minmax(500px, 1.22fr); }
      .editor-actions button { font-size: 12px; }
    }
    @media (max-width: 980px) {
      .topbar { position: relative; }
      .workspace { min-height: auto; grid-template-columns: 1fr; }
      .nav-rail { position: sticky; top: 0; z-index: 15; height: auto; display: flex; flex-direction: row; align-items: center; padding: 7px 9px; border-right: 0; border-bottom: 1px solid #202631; overflow-x: auto; }
      .nav-group { display: flex; gap: 4px; }
      .nav-spacer, .nav-label, .nav-hint { display: none; }
      .nav-foot { margin-left: auto; padding: 0 0 0 8px; border: 0; }
      .nav-item { width: auto; min-width: 38px; }
      .nav-item span { display: inline; }
      .nav-item.active::after { display: none; }
      .content { padding: 9px; }
      .layout { grid-template-columns: 1fr; min-height: 0; }
      .table-wrap { height: 430px; min-height: 320px; }
      #textClean { height: 430px; }
    }
    @media (max-width: 700px) {
      .topbar { min-height: 54px; padding: 8px 10px; }
      .logo-mark { width: 112px; height: 34px; }
      .brand-copy { display: none; }
      .product-chip { display: none; }
      .workspace { min-height: calc(100vh - 54px); }
      .nav-rail { top: 0; }
      .nav-item span { display: none; }
      .panel-head { align-items: flex-start; flex-direction: column; }
      .panel-head .editor-actions { width: 100%; display: grid; grid-template-columns: 1fr 1fr; }
      .panel-head .editor-actions button { width: 100%; }
      .panel-head .editor-actions button:first-child { grid-column: 1 / -1; }
      .search-grid { grid-template-columns: 1fr 1fr; }
      .search-grid > * { grid-column: auto !important; }
      .search-grid > :first-child { grid-column: 1 / -1 !important; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .metric:nth-child(2) { border-right: 0; }
      .metric:nth-child(-n+2) { border-bottom: 1px solid var(--line); }
      .table-wrap { height: 420px; overflow: auto; }
      table { min-width: 660px; }
      .editor-statusbar { align-items: flex-start; flex-direction: column; }
      .field-grid { grid-template-columns: 1fr 1fr; }
      .field-grid .meta-field:first-child { grid-column: 1 / -1; }
      .ops-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      #textClean { min-height: 360px; height: 55vh; }
    }
    @media (max-width: 520px) {
      .table-wrap { height: 520px; overflow-y: auto; overflow-x: hidden; }
      table, tbody, tr, td { display: block; width: 100%; min-width: 0; }
      thead { display: none; }
      tbody { display: grid; }
      tbody tr {
        padding: 9px 11px;
        border-bottom: 1px solid var(--line);
        background: #fff;
      }
      tbody tr.selected {
        background: #e9f6f8;
        box-shadow: inset 3px 0 0 var(--cyan);
      }
      tbody tr.selected td { background: transparent; }
      tr.selected td:first-child { box-shadow: none; }
      td {
        display: grid;
        grid-template-columns: 76px minmax(0, 1fr);
        gap: 7px;
        padding: 3px 0;
        border: 0;
      }
      td::before {
        content: attr(data-label);
        color: var(--muted);
        font-size: 9px;
        font-weight: 800;
        text-transform: uppercase;
      }
      td[data-label="текст"] {
        max-height: 4.5em;
        overflow: hidden;
      }
      td.empty-row { display: block; padding: 30px 12px; }
      td.empty-row::before { display: none; }
    }
    @media (max-width: 430px) {
      .status-pill { padding-inline: 8px; }
      .search-grid { grid-template-columns: 1fr; }
      .search-grid > * { grid-column: 1 !important; }
      .field-grid, .ops-kpis { grid-template-columns: 1fr; }
      .field-grid .meta-field:first-child { grid-column: 1; }
      .ops-kpis .metric { border-right: 0; border-bottom: 1px solid var(--line); }
      .ops-kpis .metric:last-child { border-bottom: 0; }
    }
  </style>
</head>
<body>
  <svg class="hidden" aria-hidden="true">
    <symbol id="icon-database" viewBox="0 0 24 24">
      <ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>
      <path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"></path>
      <path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"></path>
    </symbol>
    <symbol id="icon-shield" viewBox="0 0 24 24">
      <path d="M20 13c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V5l8-3 8 3z"></path>
      <path d="m9 12 2 2 4-4"></path>
    </symbol>
    <symbol id="icon-chart" viewBox="0 0 24 24">
      <path d="M3 3v18h18"></path>
      <path d="M7 16v-3"></path>
      <path d="M12 16V8"></path>
      <path d="M17 16V5"></path>
    </symbol>
    <symbol id="icon-refresh" viewBox="0 0 24 24">
      <path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5"></path>
      <path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"></path>
    </symbol>
    <symbol id="icon-logout" viewBox="0 0 24 24">
      <path d="M10 17l5-5-5-5"></path>
      <path d="M15 12H3"></path>
      <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"></path>
    </symbol>
    <symbol id="icon-search" viewBox="0 0 24 24">
      <circle cx="11" cy="11" r="8"></circle>
      <path d="m21 21-4.3-4.3"></path>
    </symbol>
    <symbol id="icon-save" viewBox="0 0 24 24">
      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path>
      <path d="M17 21v-8H7v8"></path>
      <path d="M7 3v5h8"></path>
    </symbol>
    <symbol id="icon-flask" viewBox="0 0 24 24">
      <path d="M9 3h6"></path>
      <path d="M10 9V3h4v6l5 9a2 2 0 0 1-1.7 3H6.7A2 2 0 0 1 5 18z"></path>
      <path d="M7 15h10"></path>
    </symbol>
    <symbol id="icon-file" viewBox="0 0 24 24">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
      <path d="M14 2v6h6"></path>
    </symbol>
  </svg>
  <header class="topbar">
    <div class="brand">
      <div class="logo-mark">
        <img src="__LOGO_DATA_URI__" alt="Импульс кода">
      </div>
      <div class="brand-copy">
        <h1>Knowledge Studio</h1>
        <span class="subtitle">База знаний Росмолодёжи · редактор RAG</span>
      </div>
    </div>
    <div class="top-actions">
      <span class="product-chip">production workspace</span>
      <span class="status-pill">
        <span class="status-dot"></span>
        <span id="authState">проверка доступа</span>
      </span>
    </div>
  </header>

  <section id="authPanel" class="auth hidden">
    <div class="auth-head">
      <h2>Вход в Knowledge Studio</h2>
      <p>
        Введи токен администратора. Сервер сохранит сессию в защищённой
        HttpOnly-cookie, сам токен не хранится в браузерном JavaScript.
      </p>
    </div>
    <div class="auth-body">
      <input id="tokenInput" type="password" autocomplete="off" placeholder="ADMIN_AUTH_TOKEN">
      <button id="loginButton" class="primary" type="button">Открыть рабочее пространство</button>
      <div id="authStatus" class="status"></div>
    </div>
  </section>

  <main id="appShell" class="workspace hidden">
    <aside class="nav-rail" aria-label="Разделы админки">
      <div class="nav-label">Рабочее пространство</div>
      <div class="nav-group">
        <button id="knowledgeButton" class="nav-item active" type="button" title="База знаний">
          <svg class="icon"><use href="#icon-database"></use></svg>
          <span>База знаний</span>
        </button>
        <button id="validateButton" class="nav-item" type="button" title="Проверка базы">
          <svg class="icon"><use href="#icon-shield"></use></svg>
          <span>Проверка базы</span>
        </button>
        <button id="qualityButton" class="nav-item" type="button" title="Отчёт качества">
          <svg class="icon"><use href="#icon-flask"></use></svg>
          <span>Отчёт качества</span>
        </button>
        <button id="opsButton" class="nav-item" type="button" title="Работа бота">
          <svg class="icon"><use href="#icon-chart"></use></svg>
          <span>Работа бота</span>
        </button>
        <button id="yonoteButton" class="nav-item" type="button" title="Синхронизация Yonote">
          <svg class="icon"><use href="#icon-refresh"></use></svg>
          <span>Синхронизация Yonote</span>
        </button>
      </div>
      <div class="nav-spacer"></div>
      <div class="nav-foot">
        <div class="nav-hint">Ctrl + S — сохранить<br>/ — поиск по базе</div>
        <button id="logoutButton" class="nav-item danger" type="button" title="Выйти">
          <svg class="icon"><use href="#icon-logout"></use></svg>
          <span>Выйти</span>
        </button>
      </div>
    </aside>

    <div class="content">
      <div class="layout">
        <section class="panel">
          <div class="panel-head">
            <div>
              <span class="panel-eyebrow">Knowledge base</span>
              <h2 class="panel-title">Документы и чанки</h2>
            </div>
            <div id="listStatus" class="status list-status"></div>
          </div>
          <div class="search-grid">
            <input id="searchInput" type="search" placeholder="Поиск по тексту или ID">
            <select id="statusFilter">
              <option value="">Все статусы</option>
              <option value="published">Опубликованные</option>
              <option value="draft">Черновики</option>
              <option value="archived">Архивные</option>
            </select>
            <input id="forumFilter" type="text" placeholder="Форум">
            <input id="categoryFilter" class="category-field" type="text" placeholder="Категория">
            <button id="loadButton" class="search-button" type="button">
              <svg class="icon"><use href="#icon-search"></use></svg>Найти
            </button>
          </div>
          <div class="metrics">
            <div class="metric">
              <span id="metricFound" class="metric-value">-</span>
              <span class="metric-label">по текущему фильтру</span>
            </div>
            <div class="metric">
              <span id="metricValid" class="metric-value">-</span>
              <span class="metric-label">валидных чанков</span>
            </div>
            <div class="metric">
              <span id="metricEval" class="metric-value">-</span>
              <span class="metric-label">quality report</span>
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
                  <th style="width: 27%;">ID чанка</th>
                  <th style="width: 15%;">Статус</th>
                  <th style="width: 18%;">Форум</th>
                  <th>Текст</th>
                </tr>
              </thead>
              <tbody id="chunksTable"></tbody>
            </table>
          </div>
        </section>

        <section id="editorPanel" class="panel editor-panel">
          <div class="panel-head">
            <div>
              <span class="panel-eyebrow">RAG document</span>
              <h2 id="editorPanelTitle" class="panel-title">Редактор источника</h2>
            </div>
            <div class="editor-actions">
              <button id="saveChunkButton" class="primary" type="button" disabled>
                <svg class="icon"><use href="#icon-save"></use></svg>Сохранить и обновить RAG
              </button>
              <button id="reindexButton" class="secondary" type="button" disabled>
                <svg class="icon"><use href="#icon-refresh"></use></svg>Обновить индекс
              </button>
              <button id="relatedCasesButton" class="secondary" type="button" disabled>
                <svg class="icon"><use href="#icon-flask"></use></svg>Тест-кейсы
              </button>
            </div>
          </div>
          <div class="editor-body">
            <div class="editor-statusbar">
              <label class="toggle">
                <input id="reindexToggle" type="checkbox" checked>
                Обновить Qdrant и сбросить semantic cache после сохранения
              </label>
              <span id="detailStatus" class="status"></span>
            </div>
            <div class="document-head">
              <div class="document-path">
                <svg class="icon"><use href="#icon-file"></use></svg>
                knowledge_base / chunks / selected
              </div>
              <div class="detail-title-row">
                <h2 id="detailTitle" class="detail-title muted">Выбери чанк в списке</h2>
                <span id="dirtyIndicator" class="dirty-indicator hidden">не сохранено</span>
              </div>
              <div class="field-grid">
                <div class="meta-field">
                  <label class="meta-label" for="chunkStatus">Статус</label>
                  <select id="chunkStatus" disabled>
                    <option value="published">Опубликован</option>
                    <option value="draft">Черновик</option>
                    <option value="archived">Архив</option>
                  </select>
                </div>
                <div class="meta-field">
                  <span class="meta-label">Форум</span>
                  <span id="chunkForum" class="meta-value mono">—</span>
                </div>
                <div class="meta-field">
                  <span class="meta-label">Тема</span>
                  <span id="chunkTopic" class="meta-value mono">—</span>
                </div>
                <div class="meta-field">
                  <span class="meta-label">Источник</span>
                  <span id="chunkSource" class="meta-value mono">—</span>
                </div>
              </div>
            </div>
            <div class="editor-surface">
              <div class="editor-label">
                <span>Текст ответа</span>
                <span id="charCount">0 символов</span>
              </div>
              <textarea
                id="textClean"
                disabled
                spellcheck="true"
                placeholder="Выбери чанк слева, чтобы открыть текст ответа."
              ></textarea>
            </div>
            <div id="qualityDashboard" class="quality-dashboard hidden"></div>
            <div id="opsDashboard" class="ops-dashboard hidden"></div>
            <div id="yonoteDashboard" class="quality-dashboard hidden"></div>
            <details id="rawReportDetails" class="raw-report">
              <summary>Технические данные ответа API</summary>
              <pre id="reportOutput"></pre>
            </details>
          </div>
        </section>
      </div>
    </div>
  </main>
  <div id="toastRegion" class="toast-region" aria-live="polite"></div>

  <script>
    let selectedChunkId = "";
    let editorOriginalText = "";
    let editorOriginalStatus = "";
    let editorDirty = false;

    function setStatus(id, message, cls = "") {
      const el = document.getElementById(id);
      el.className = "status " + cls;
      el.textContent = message;
    }
    function showToast(message, cls = "ok") {
      const region = document.getElementById("toastRegion");
      const toast = document.createElement("div");
      toast.className = "toast " + cls;
      toast.textContent = message;
      region.appendChild(toast);
      window.setTimeout(() => toast.remove(), 4200);
    }
    function setActiveNav(buttonId) {
      document.querySelectorAll(".nav-item").forEach((button) => {
        button.classList.toggle("active", button.id === buttonId);
      });
    }
    function setWorkspaceMode(mode, title) {
      const editorPanel = document.getElementById("editorPanel");
      editorPanel.classList.toggle("report-mode", mode !== "knowledge");
      document.getElementById("editorPanelTitle").textContent = title;
    }
    function updateEditorDirty() {
      const text = document.getElementById("textClean").value;
      const status = document.getElementById("chunkStatus").value;
      editorDirty = Boolean(
        selectedChunkId &&
        (text !== editorOriginalText || status !== editorOriginalStatus)
      );
      document.getElementById("dirtyIndicator").classList.toggle("hidden", !editorDirty);
      document.getElementById("charCount").textContent = text.length + " символов";
    }
    function showKnowledgeWorkspace() {
      setActiveNav("knowledgeButton");
      setWorkspaceMode("knowledge", "Редактор источника");
      hideReportDashboards();
      setStatus(
        "detailStatus",
        selectedChunkId ? (editorDirty ? "Есть несохранённые изменения" : "Готово") : "Выбери чанк в списке",
        editorDirty ? "warn" : ""
      );
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
      document.getElementById("editorPanel").classList.toggle("is-busy", isBusy);
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
    function hideYonoteDashboard() {
      const dashboard = document.getElementById("yonoteDashboard");
      dashboard.classList.add("hidden");
      dashboard.innerHTML = "";
    }
    function hideReportDashboards() {
      hideOpsDashboard();
      hideQualityDashboard();
      hideYonoteDashboard();
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
    function renderValidationDashboard(data) {
      const dashboard = document.getElementById("qualityDashboard");
      const statuses = Object.entries(data.status_counts || {});
      const categories = Object.entries(data.category_counts || {})
        .sort((left, right) => Number(right[1]) - Number(left[1]))
        .slice(0, 10);
      const categoryRows = categories.map(([name, count]) => [
        '<div class="ops-item">',
        '<div class="ops-line"><span>' + escapeHtml(name) + '</span>',
        '<span>' + escapeHtml(count) + '</span></div>',
        "</div>",
      ].join("")).join("");
      dashboard.classList.remove("hidden");
      dashboard.innerHTML = [
        '<div class="ops-kpis">',
        '<div class="metric"><span class="metric-value">' + escapeHtml(data.valid_records || 0) + '</span>',
        '<span class="metric-label">валидных записей</span></div>',
        '<div class="metric"><span class="metric-value">' + escapeHtml(statuses.length) + '</span>',
        '<span class="metric-label">статусов</span></div>',
        '<div class="metric"><span class="metric-value">' + escapeHtml(Object.keys(data.category_counts || {}).length) + '</span>',
        '<span class="metric-label">категорий</span></div>',
        '<div class="metric"><span class="metric-value quality-ok">OK</span>',
        '<span class="metric-label">структура seed</span></div>',
        "</div>",
        '<div class="quality-note"><b>База прошла структурную проверку.</b> ',
        "Все записи имеют обязательные поля и могут быть использованы для индексации.</div>",
        '<div class="ops-section"><h3>Крупнейшие категории</h3><div class="ops-list">',
        categoryRows || '<div class="ops-item"><span class="ops-meta">Нет данных</span></div>',
        "</div></div>",
      ].join("");
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
    function renderYonoteDashboard(data) {
      const dashboard = document.getElementById("yonoteDashboard");
      dashboard.classList.remove("hidden");
      const shouldShowApply = !data.applied;
      dashboard.innerHTML = `
        <div class="ops-kpis">
          <div class="metric">
            <span class="metric-value">${escapeHtml(data.documents || 0)}</span>
            <span class="metric-label">документов Yonote</span>
          </div>
          <div class="metric">
            <span class="metric-value">${escapeHtml(data.fresh_yonote_records || 0)}</span>
            <span class="metric-label">новых Yonote-чанков</span>
          </div>
          <div class="metric">
            <span class="metric-value">${escapeHtml(data.changed || 0)}</span>
            <span class="metric-label">изменённых чанков</span>
          </div>
          <div class="metric">
            <span class="metric-value">${escapeHtml(data.merged_records || 0)}</span>
            <span class="metric-label">всего после синка</span>
          </div>
        </div>
        <div class="quality-note">
          <b>${data.applied ? "Обновление применено" : "Предпросмотр Yonote"}</b>.
          Добавится: ${escapeHtml(data.added || 0)} ·
          изменится: ${escapeHtml(data.changed || 0)} ·
          удалится из Yonote-слоя: ${escapeHtml(data.removed || 0)}.
          ${data.applied
            ? [
                "Seed обновлён. Чтобы бот начал отвечать по новым данным,",
                "нужна полная переиндексация Qdrant.",
              ].join(" ")
            : "Это только проверка: knowledge_base_seed.json ещё не изменён."}
        </div>
        <div class="ops-section">
          <h3>Примеры изменений</h3>
          <div class="ops-list">
            <div class="ops-item">
              <div class="ops-line">
                <span>Добавятся</span>
                <span>${escapeHtml(data.added || 0)}</span>
              </div>
              <div class="ops-meta">
                ${escapeHtml((data.added_sample || []).join(", ") || "нет")}
              </div>
            </div>
            <div class="ops-item">
              <div class="ops-line">
                <span>Изменятся</span>
                <span>${escapeHtml(data.changed || 0)}</span>
              </div>
              <div class="ops-meta">
                ${escapeHtml((data.changed_sample || []).join(", ") || "нет")}
              </div>
            </div>
            <div class="ops-item">
              <div class="ops-line">
                <span>Удалятся из Yonote-слоя</span>
                <span>${escapeHtml(data.removed || 0)}</span>
              </div>
              <div class="ops-meta">
                ${escapeHtml((data.removed_sample || []).join(", ") || "нет")}
              </div>
            </div>
          </div>
        </div>
        <div class="editor-actions">
          ${shouldShowApply
            ? '<button id="applyYonoteButton" class="primary" type="button">Применить в KB</button>'
            : '<button class="secondary" type="button" disabled>Применено</button>'}
        </div>
      `;
      const applyButton = document.getElementById("applyYonoteButton");
      if (applyButton) {
        applyButton.addEventListener("click", applyYonoteSync);
      }
    }
    function renderListLoading() {
      const row = [
        "<tr>",
        '<td><div class="skeleton" style="width:82%"></div></td>',
        '<td><div class="skeleton" style="width:70%"></div></td>',
        '<td><div class="skeleton" style="width:74%"></div></td>',
        '<td><div class="skeleton" style="width:94%"></div></td>',
        "</tr>",
      ].join("");
      document.getElementById("chunksTable").innerHTML = row.repeat(7);
    }
    function renderRows(items) {
      const tbody = document.getElementById("chunksTable");
      tbody.innerHTML = "";
      if (!items.length) {
        tbody.innerHTML = [
          '<tr><td colspan="4" class="empty-row">',
          "Ничего не найдено. Измени фильтры или поисковый запрос.",
          "</td></tr>",
        ].join("");
        return;
      }
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
        renderListLoading();
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
        showToast(error.message, "error");
      }
    }
    async function loadChunk(chunkId, row) {
      if (
        editorDirty &&
        selectedChunkId &&
        selectedChunkId !== chunkId &&
        !window.confirm("В текущем чанке есть несохранённые изменения. Перейти без сохранения?")
      ) {
        return;
      }
      try {
        setActiveNav("knowledgeButton");
        setWorkspaceMode("knowledge", "Редактор источника");
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
        editorOriginalText = document.getElementById("textClean").value;
        editorOriginalStatus = document.getElementById("chunkStatus").value;
        updateEditorDirty();
        document.getElementById("textClean").disabled = false;
        document.getElementById("saveChunkButton").disabled = false;
        document.getElementById("reindexButton").disabled = false;
        document.getElementById("relatedCasesButton").disabled = false;
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        setStatus("detailStatus", "Готово", "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
        showToast(error.message, "error");
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
          showToast("Текст сохранён, но индекс нужно обновить отдельно.", "warn");
        } else {
          const reindex = data.reindex && data.reindex.ok
            ? " Qdrant обновлён, кэш сброшен."
            : "";
          setStatus("detailStatus", "Сохранено." + reindex, "ok");
          showToast("Чанк сохранён, знания бота обновлены.", "ok");
        }
        editorOriginalText = document.getElementById("textClean").value;
        editorOriginalStatus = document.getElementById("chunkStatus").value;
        updateEditorDirty();
        await loadChunks();
      } catch (error) {
        setStatus(
          "detailStatus",
          "Сохранение не подтверждено: " +
            error.message +
            " Открой этот чанк заново и проверь текст перед повторной правкой.",
          "error"
        );
        showToast("Сохранение не подтверждено. Проверь состояние чанка.", "error");
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
        showToast("Индекс чанка обновлён, кэш сброшен.", "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
        showToast(error.message, "error");
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
        document.getElementById("rawReportDetails").open = true;
        setStatus("detailStatus", `Тест-кейсов: ${data.total}`, "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function showValidation() {
      try {
        setActiveNav("validateButton");
        setWorkspaceMode("validation", "Проверка базы");
        hideReportDashboards();
        const data = await requestJson("/admin/kb/validate", {method: "POST", body: "{}"});
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        renderValidationDashboard(data);
        setMetric("metricValid", data.valid_records);
        setStatus("detailStatus", `База валидна: ${data.valid_records}`, "ok");
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function showQualityCheck() {
      try {
        setActiveNav("qualityButton");
        setWorkspaceMode("quality", "Отчёт качества");
        hideOpsDashboard();
        hideYonoteDashboard();
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
        setActiveNav("opsButton");
        setWorkspaceMode("ops", "Работа бота");
        hideQualityDashboard();
        hideYonoteDashboard();
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
    async function previewYonoteSync() {
      try {
        setActiveNav("yonoteButton");
        setWorkspaceMode("yonote", "Синхронизация Yonote");
        hideOpsDashboard();
        hideQualityDashboard();
        setStatus("detailStatus", "Читаю Yonote и считаю изменения. База бота пока не меняется...");
        const data = await requestJson("/admin/kb/yonote/preview", {
          method: "POST",
          body: "{}",
          timeoutMs: 300000,
        });
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        renderYonoteDashboard(data);
        setStatus(
          "detailStatus",
          `Yonote проверен: +${data.added}, изменится ${data.changed}, удалится ${data.removed}`,
          "ok"
        );
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function applyYonoteSync() {
      const confirmed = window.confirm(
        "Применить данные Yonote в knowledge_base_seed.json? " +
        "Yonote не будет изменён. После применения нужна полная индексация Qdrant."
      );
      if (!confirmed) return;
      try {
        setStatus(
          "detailStatus",
          "Применяю Yonote в KB seed. Это не пишет ничего в Yonote..."
        );
        const data = await requestJson("/admin/kb/yonote/apply", {
          method: "POST",
          body: "{}",
          timeoutMs: 300000,
        });
        document.getElementById("reportOutput").textContent = JSON.stringify(data, null, 2);
        renderYonoteDashboard(data);
        setStatus(
          "detailStatus",
          "Yonote применён в KB seed. Теперь нужна полная переиндексация Qdrant.",
          "warn"
        );
        await Promise.allSettled([loadChunks(), showValidation()]);
      } catch (error) {
        setStatus("detailStatus", error.message, "error");
      }
    }
    async function loadOverviewMetrics() {
      const results = await Promise.allSettled([
        requestJson("/admin/kb/validate", {method: "POST", body: "{}"}),
        requestJson("/admin/kb/quality-check", {
          method: "POST",
          body: JSON.stringify({include_latest_eval_report: true}),
        }),
        requestJson("/admin/kb/ops-report?days=7", {method: "GET"}),
      ]);
      const validation = results[0];
      const quality = results[1];
      const ops = results[2];
      if (validation.status === "fulfilled") {
        setMetric("metricValid", validation.value.valid_records || 0);
      }
      if (quality.status === "fulfilled") {
        setMetric(
          "metricEval",
          quality.value.latest_eval_report_exists ? "есть" : "нет"
        );
      }
      if (ops.status === "fulfilled") {
        setMetric("metricOps", (ops.value.summary || {}).request_count || 0);
      }
    }
    async function boot() {
      setAuthenticated(true);
      setActiveNav("knowledgeButton");
      setWorkspaceMode("knowledge", "Редактор источника");
      hideReportDashboards();
      await Promise.allSettled([
        loadChunks(),
        loadOverviewMetrics(),
      ]);
      setStatus("detailStatus", "Выбери чанк в списке");
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
    document.getElementById("knowledgeButton").addEventListener("click", showKnowledgeWorkspace);
    document.getElementById("logoutButton").addEventListener("click", logout);
    document.getElementById("loadButton").addEventListener("click", loadChunks);
    document.getElementById("searchInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadChunks();
    });
    document.getElementById("textClean").addEventListener("input", updateEditorDirty);
    document.getElementById("chunkStatus").addEventListener("change", updateEditorDirty);
    document.getElementById("saveChunkButton").addEventListener("click", saveChunk);
    document.getElementById("reindexButton").addEventListener("click", reindexChunk);
    document.getElementById("relatedCasesButton").addEventListener("click", showRelatedCases);
    document.getElementById("validateButton").addEventListener("click", showValidation);
    document.getElementById("qualityButton").addEventListener("click", showQualityCheck);
    document.getElementById("opsButton").addEventListener("click", showOpsReport);
    document.getElementById("yonoteButton").addEventListener("click", previewYonoteSync);
    document.addEventListener("keydown", (event) => {
      const target = event.target;
      const isTyping = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (selectedChunkId && !document.getElementById("saveChunkButton").disabled) {
          saveChunk();
        }
      } else if (event.key === "/" && !isTyping) {
        event.preventDefault();
        document.getElementById("searchInput").focus();
      }
    });
    window.addEventListener("beforeunload", (event) => {
      if (!editorDirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
    checkSession();
  </script>
</body>
</html>
"""


ADMIN_KB_HTML = _HTML_TEMPLATE.replace("__LOGO_DATA_URI__", _logo_data_uri())
