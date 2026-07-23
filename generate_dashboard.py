#!/usr/bin/env python3
"""Generate a self-contained, offline pre-open auction morning brief."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


STATUS_KEYS = {"suspected_fake", "locked_held", "touched", "none"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="將假試撮掃描結果製作成可離線閱讀的盤前試撮晨間簡報。"
    )
    parser.add_argument(
        "--in",
        dest="input_path",
        default="data/result_sample.json",
        help="scanner.py 產生的 result JSON（預設：data/result_sample.json）",
    )
    parser.add_argument(
        "--out",
        dest="output_path",
        default="dashboard.html",
        help="輸出的自包含 HTML（預設：dashboard.html）",
    )
    return parser.parse_args()


def read_result(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("輸入根節點必須是 JSON object")
    stocks = payload.get("stocks")
    if not isinstance(stocks, list):
        raise ValueError("輸入缺少 stocks 陣列")
    for index, stock in enumerate(stocks):
        if not isinstance(stock, dict):
            raise ValueError(f"stocks[{index}] 必須是 object")
        status = stock.get("status", "none")
        if status not in STATUS_KEYS:
            raise ValueError(f"stocks[{index}].status 不支援：{status!r}")
        if not isinstance(stock.get("sim_price_series", []), list):
            raise ValueError(f"stocks[{index}].sim_price_series 必須是陣列")
        if not isinstance(stock.get("bid0_series", []), list):
            raise ValueError(f"stocks[{index}].bid0_series 必須是陣列")
    return payload


def json_for_script(payload: dict[str, Any]) -> str:
    """Serialize safely for an inline script without relying on network assets."""
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>盤前試撮晨間簡報</title>
<style>
:root {
  --paper: #FAF8F3;
  --card: #FFFFFF;
  --card-soft: #F7F4ED;
  --ink: #232019;
  --muted: #6B6459;
  --quiet: #9B9384;
  --rule: #E4DED2;
  --red: #C8322A;
  --amber: #B77514;
  --green: #2F7D5B;
  --gray: #9B9384;
  --blue: #2F6DB0;
  --title-font: Georgia, "Noto Serif TC", "Songti TC", "PMingLiU", serif;
  --body-font: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
    "PingFang TC", "Microsoft JhengHei", sans-serif;
  --shadow: 0 12px 32px rgba(66, 54, 34, .055);
}

* { box-sizing: border-box; }

html {
  color: var(--ink);
  background: var(--paper);
  scroll-behavior: smooth;
}

body {
  min-width: 320px;
  margin: 0;
  overflow-x: hidden;
  color: var(--ink);
  background: var(--paper);
  font-family: var(--body-font);
  font-size: 16px;
  line-height: 1.72;
  text-rendering: optimizeLegibility;
}

button { font: inherit; }

.report {
  width: min(1240px, calc(100% - 56px));
  margin: 0 auto;
  padding: 48px 0 72px;
}

.report-head {
  padding: 24px 0 42px;
  border-top: 7px solid var(--ink);
  border-bottom: 1px solid var(--ink);
}

.edition-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 62px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .2em;
}

.edition-line span:last-child {
  font-variant-numeric: tabular-nums;
  letter-spacing: .08em;
}

.head-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) minmax(260px, .55fr);
  gap: 70px;
  align-items: end;
}

.report-head h1 {
  max-width: 760px;
  margin: 0;
  font-family: var(--title-font);
  font-size: clamp(50px, 7.2vw, 94px);
  font-weight: 700;
  line-height: .98;
  letter-spacing: -.045em;
}

.deck {
  max-width: 700px;
  margin: 24px 0 0;
  color: var(--muted);
  font-family: var(--title-font);
  font-size: clamp(17px, 2vw, 22px);
  line-height: 1.6;
}

.case-meta {
  margin: 0;
  border-top: 1px solid var(--rule);
}

.case-meta div {
  display: grid;
  grid-template-columns: 86px 1fr;
  gap: 18px;
  padding: 11px 0;
  border-bottom: 1px solid var(--rule);
}

.case-meta dt {
  color: var(--muted);
  font-size: 12px;
  letter-spacing: .08em;
}

.case-meta dd {
  margin: 0;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.section {
  padding: 64px 0;
  border-bottom: 1px solid var(--rule);
}

.section-heading {
  display: grid;
  grid-template-columns: 78px minmax(0, 1fr);
  gap: 20px;
  align-items: baseline;
  margin-bottom: 34px;
}

.section-no {
  color: var(--red);
  font-family: var(--title-font);
  font-size: 14px;
  font-style: italic;
  letter-spacing: .08em;
}

.section-heading h2 {
  margin: 0;
  font-family: var(--title-font);
  font-size: clamp(30px, 4vw, 48px);
  font-weight: 600;
  line-height: 1.1;
  letter-spacing: -.02em;
}

.section-note {
  max-width: 720px;
  margin: 10px 0 0 98px;
  color: var(--muted);
  font-size: 14px;
}

.intro-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(300px, .9fr);
  gap: 72px;
}

.lede {
  margin: 0;
  font-family: var(--title-font);
  font-size: clamp(21px, 2.5vw, 30px);
  line-height: 1.65;
}

.lede::first-letter {
  float: left;
  margin: .04em .12em 0 0;
  color: var(--red);
  font-size: 3.1em;
  font-weight: 700;
  line-height: .82;
}

.intro-copy p {
  margin: 24px 0 0;
  color: var(--muted);
}

.reading-notes {
  padding: 28px 30px;
  border-top: 3px solid var(--ink);
  background: var(--card-soft);
}

.reading-notes h3 {
  margin: 0 0 12px;
  font-family: var(--title-font);
  font-size: 21px;
}

.reading-notes p {
  margin: 0 0 20px;
  color: var(--muted);
  font-size: 14px;
}

.status-key {
  display: grid;
  gap: 9px;
}

.status-badge {
  display: inline-flex;
  width: fit-content;
  max-width: 100%;
  align-items: center;
  gap: 9px;
  padding: 6px 11px 6px 7px;
  border: 1px solid var(--rule);
  border-left: 3px solid currentColor;
  border-radius: 2px;
  background: var(--card);
  color: var(--ink);
  font-size: 12px;
  font-weight: 700;
  line-height: 1.25;
  white-space: nowrap;
}

.status-badge.large {
  padding: 9px 14px 9px 9px;
  font-size: 14px;
}

.status-icon {
  display: inline-grid;
  width: 23px;
  height: 23px;
  flex: 0 0 23px;
  place-items: center;
  border: 1px solid currentColor;
  border-radius: 50%;
  font-size: 12px;
  line-height: 1;
}

.status-suspected_fake { border-left-color: var(--red); }
.status-suspected_fake .status-icon { color: var(--red); }
.status-locked_held { border-left-color: var(--amber); }
.status-locked_held .status-icon { color: var(--amber); }
.status-touched { border-left-color: var(--green); }
.status-touched .status-icon { color: var(--green); }
.status-none { border-left-color: var(--gray); }
.status-none .status-icon { color: var(--gray); }

.kpis {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  border-block: 1px solid var(--ink);
}

.kpi {
  min-width: 0;
  padding: 30px 28px 34px;
}

.kpi + .kpi { border-left: 1px solid var(--rule); }

.kpi-label {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  letter-spacing: .06em;
}

.kpi-value {
  margin: 8px 0 0;
  font-family: var(--title-font);
  font-size: clamp(48px, 6vw, 78px);
  font-variant-numeric: tabular-nums;
  line-height: 1;
  letter-spacing: -.04em;
}

.kpi-value small {
  margin-left: 8px;
  color: var(--muted);
  font-family: var(--body-font);
  font-size: 13px;
  font-weight: 500;
  letter-spacing: 0;
}

.kpi:first-child .kpi-value { color: var(--red); }
.kpi:nth-child(2) .kpi-value { color: var(--amber); }

.kpi-note {
  margin: 14px 0 0;
  color: var(--muted);
  font-size: 12px;
}

.overview-grid {
  display: grid;
  grid-template-columns: minmax(290px, .72fr) minmax(0, 1.28fr);
  gap: 22px;
  align-items: stretch;
}

.figure-card {
  min-width: 0;
  margin: 0;
  border: 1px solid var(--rule);
  background: var(--card);
  box-shadow: var(--shadow);
}

.figure-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 24px 26px 18px;
  border-bottom: 1px solid var(--rule);
}

.fig-no {
  margin: 0 0 5px;
  color: var(--red);
  font-family: var(--title-font);
  font-size: 12px;
  font-style: italic;
  letter-spacing: .08em;
}

.figure-head h3,
.figure-head h4 {
  margin: 0;
  font-family: var(--title-font);
  font-size: 21px;
  font-weight: 600;
  line-height: 1.25;
}

.figure-head p {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 12px;
}

.chart-legend {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px 13px;
  color: var(--muted);
  font-size: 11px;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.legend-line {
  width: 17px;
  border-top: 2px solid currentColor;
}

.legend-line.dashed { border-top-style: dashed; }

.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}

.figure-body { padding: 20px 24px 24px; }

.figure-scroll {
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
  overscroll-behavior-inline: contain;
  scrollbar-color: var(--quiet) var(--card-soft);
}

.gap-scroll {
  max-height: 600px;
  overflow: auto;
}

.chart-stage {
  position: relative;
  min-width: 720px;
}

.status-stage {
  position: relative;
  min-height: 254px;
}

.chart-svg {
  display: block;
  width: 100%;
  height: auto;
  min-width: 720px;
  color: var(--muted);
  touch-action: pan-y;
}

.status-svg {
  display: block;
  width: min(260px, 100%);
  height: auto;
  margin: 0 auto;
}

.status-legend {
  display: grid;
  gap: 9px;
  margin-top: 15px;
}

.status-legend-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
}

.status-count {
  font-family: var(--title-font);
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}

.chart-tooltip {
  position: absolute;
  z-index: 8;
  min-width: 180px;
  padding: 10px 12px;
  border: 1px solid var(--ink);
  border-radius: 2px;
  background: rgba(255, 255, 255, .97);
  box-shadow: 0 8px 22px rgba(52, 44, 31, .13);
  color: var(--muted);
  font-size: 11px;
  line-height: 1.65;
  pointer-events: none;
}

.chart-tooltip strong {
  color: var(--ink);
  font-weight: 700;
}

.chart-tooltip[hidden] { display: none; }

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.empty-chart {
  display: grid;
  min-height: 220px;
  place-items: center;
  padding: 30px;
  border: 1px dashed var(--rule);
  background: var(--card-soft);
  color: var(--muted);
  font-family: var(--title-font);
  font-size: 17px;
  text-align: center;
}

.case-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}

.case-summary {
  min-width: 0;
  padding: 26px;
  border: 1px solid var(--rule);
  border-top: 4px solid var(--gray);
  background: var(--card);
}

.case-summary[data-status="suspected_fake"] { border-top-color: var(--red); }
.case-summary[data-status="locked_held"] { border-top-color: var(--amber); }
.case-summary[data-status="touched"] { border-top-color: var(--green); }

.case-summary-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.case-code {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  letter-spacing: .11em;
}

.case-summary h3 {
  margin: 4px 0 0;
  font-family: var(--title-font);
  font-size: 28px;
  font-weight: 600;
  line-height: 1.15;
}

.case-metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  margin: 24px 0;
  border-block: 1px solid var(--rule);
}

.metric {
  min-width: 0;
  padding: 12px 10px 12px 0;
}

.metric + .metric {
  padding-left: 12px;
  border-left: 1px solid var(--rule);
}

.metric:nth-child(n+4) { border-top: 1px solid var(--rule); }
.metric:nth-child(4) { padding-left: 0; border-left: 0; }

.metric span {
  display: block;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: .06em;
}

.metric strong {
  display: block;
  margin-top: 2px;
  overflow: hidden;
  font-size: 14px;
  font-variant-numeric: tabular-nums;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.text-button {
  display: inline-flex;
  min-height: 42px;
  align-items: center;
  justify-content: center;
  gap: 9px;
  padding: 9px 14px;
  border: 1px solid var(--ink);
  border-radius: 1px;
  color: var(--ink);
  background: transparent;
  cursor: pointer;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: .04em;
  transition: background-color .16s ease, color .16s ease;
}

.text-button:hover {
  color: var(--card);
  background: var(--ink);
}

.text-button:focus-visible,
.expand-button:focus-visible,
.gap-row:focus-visible,
.status-arc:focus-visible,
.chart-stage:focus-visible {
  outline: 3px solid var(--blue);
  outline-offset: 3px;
}

.no-featured {
  padding: 34px;
  border: 1px solid var(--rule);
  background: var(--card-soft);
  color: var(--muted);
}

.index-card {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--rule);
  background: var(--card);
}

.table-scroll {
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
}

.stock-table {
  width: 100%;
  min-width: 1030px;
  border-collapse: collapse;
  table-layout: fixed;
}

.stock-table th {
  padding: 12px 14px;
  border-bottom: 1px solid var(--ink);
  background: var(--card-soft);
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .05em;
  text-align: left;
}

.stock-table td {
  padding: 15px 14px;
  border-bottom: 1px solid var(--rule);
  font-size: 13px;
  vertical-align: middle;
}

.stock-table .num {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.stock-identity strong {
  display: block;
  font-family: var(--title-font);
  font-size: 17px;
  font-weight: 600;
}

.stock-identity span {
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  letter-spacing: .08em;
}

.gap-negative { color: var(--red); }
.gap-positive { color: var(--green); }
.empty-value { color: var(--muted); }

.expand-button {
  display: inline-grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border: 1px solid var(--rule);
  border-radius: 50%;
  color: var(--ink);
  background: var(--card);
  cursor: pointer;
}

.expand-button:hover { border-color: var(--ink); }
.expand-button span { transition: transform .18s ease; }
.expand-button[aria-expanded="true"] span { transform: rotate(45deg); }

.detail-row[hidden] { display: none; }

.detail-row td {
  padding: 0;
  background: var(--card-soft);
}

.case-detail {
  padding: 30px;
  border-left: 4px solid var(--rule);
}

.case-detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 22px;
}

.case-detail-head h3 {
  margin: 2px 0 0;
  font-family: var(--title-font);
  font-size: 28px;
  font-weight: 600;
}

.case-detail-head p {
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 13px;
}

.evidence-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(105px, 1fr));
  margin-bottom: 18px;
  border: 1px solid var(--rule);
}

.evidence {
  min-width: 0;
  padding: 13px 14px;
  background: var(--card);
}

.evidence + .evidence { border-left: 1px solid var(--rule); }

.evidence span {
  display: block;
  overflow: hidden;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: .05em;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.evidence strong {
  display: block;
  margin-top: 4px;
  overflow: hidden;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.case-figures {
  display: grid;
  gap: 16px;
}

.case-figures .figure-card { box-shadow: none; }

.source-note {
  margin: 13px 0 0;
  color: var(--muted);
  font-size: 11px;
}

.report-foot {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 30px;
  padding-top: 30px;
  color: var(--muted);
  font-size: 11px;
}

.report-foot p { margin: 0; }
.report-foot strong { color: var(--ink); }

.reveal {
  animation: report-in .36s ease-out both;
  animation-delay: var(--delay, 0ms);
}

@keyframes report-in {
  from { opacity: 0; transform: translateY(7px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 980px) {
  .head-grid,
  .intro-layout,
  .overview-grid { grid-template-columns: 1fr; gap: 36px; }
  .case-meta { max-width: 520px; }
  .case-grid { grid-template-columns: 1fr; }
  .evidence-strip { grid-template-columns: repeat(3, 1fr); }
  .evidence:nth-child(4) { border-left: 0; border-top: 1px solid var(--rule); }
  .evidence:nth-child(n+5) { border-top: 1px solid var(--rule); }
}

@media (max-width: 700px) {
  .report { width: min(100% - 28px, 1240px); padding-top: 20px; }
  .edition-line { margin-bottom: 38px; align-items: flex-start; flex-direction: column; gap: 7px; }
  .report-head { padding-bottom: 28px; }
  .report-head h1 { font-size: clamp(44px, 15vw, 70px); }
  .section { padding: 48px 0; }
  .section-heading { grid-template-columns: 48px minmax(0, 1fr); gap: 12px; }
  .section-note { margin-left: 60px; }
  .reading-notes { padding: 22px 20px; }
  .kpis { grid-template-columns: 1fr; }
  .kpi + .kpi { border-top: 1px solid var(--rule); border-left: 0; }
  .figure-head { flex-direction: column; padding: 20px 18px 15px; }
  .chart-legend { justify-content: flex-start; }
  .figure-body { padding: 16px 12px 18px; }
  .case-summary { padding: 21px 18px; }
  .case-summary-head,
  .case-detail-head { flex-direction: column; }
  .case-metrics { grid-template-columns: 1fr; }
  .metric + .metric { padding-left: 0; border-top: 1px solid var(--rule); border-left: 0; }
  .case-detail { padding: 22px 12px; }
  .evidence-strip { grid-template-columns: repeat(2, 1fr); }
  .evidence:nth-child(odd) { border-left: 0; }
  .evidence:nth-child(n+3) { border-top: 1px solid var(--rule); }
  .report-foot { grid-template-columns: 1fr; gap: 10px; }
}

@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after {
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .01ms !important;
  }
}

@media print {
  body { background: #fff; }
  .report { width: 100%; padding: 0; }
  .figure-card { box-shadow: none; break-inside: avoid; }
  .text-button, .expand-button { display: none; }
}
</style>
</head>
<body>
<main class="report">
  <header class="report-head reveal" style="--delay:20ms">
    <div class="edition-line">
      <span>PRE-OPEN AUCTION · MORNING EDITION</span>
      <span id="editionDate">—</span>
    </div>
    <div class="head-grid">
      <div>
        <h1>盤前試撮<br>晨間簡報</h1>
        <p class="deck">從試撮價、漲停鎖單與開盤落差，還原市場開門前最後幾分鐘的真實線索。</p>
      </div>
      <dl class="case-meta" aria-label="案卷資訊">
        <div><dt>案卷日期</dt><dd id="metaDate">—</dd></div>
        <div><dt>場次</dt><dd id="metaSession">—</dd></div>
        <div><dt>監控檔數</dt><dd id="metaCount">—</dd></div>
        <div><dt>產檔時間</dt><dd id="metaGenerated">—</dd></div>
      </dl>
    </div>
  </header>

  <section class="section reveal" style="--delay:70ms" aria-labelledby="guide-title">
    <div class="section-heading">
      <span class="section-no">01</span>
      <h2 id="guide-title">導讀</h2>
    </div>
    <div class="intro-layout">
      <div class="intro-copy">
        <p class="lede">所謂「假試撮拉漲停」，是指開盤前的試撮價格一度被大量買單推到漲停附近，但真正開盤時，買單可能撤退、價格也沒有守在漲停。</p>
        <p>這份簡報不是在宣判誰造假，而是把盤前曾鎖漲停、買一堆量是否驟降，以及開盤價離漲停價多遠放在同一張案卷裡。它幫你先找到值得回看逐筆資料的標的，再自行判斷是否存在短暫拉抬或流動性異常。</p>
      </div>
      <aside class="reading-notes">
        <h3>怎麼讀這份簡報</h3>
        <p>價格圖看藍線是否碰到紅色漲停線，以及綠色開盤標記落在哪裡；買一圖看藍色堆量是否在開盤前明顯縮水。圖中的十字準線可同步查看同一時間的價與量。</p>
        <div class="status-key" id="statusKey" aria-label="四種狀態說明"></div>
      </aside>
    </div>
  </section>

  <section class="section reveal" style="--delay:110ms" aria-label="今日重點數字">
    <div class="kpis">
      <article class="kpi">
        <p class="kpi-label">今日疑似假試撮</p>
        <p class="kpi-value"><span id="kpiSuspected">0</span><small>檔</small></p>
        <p class="kpi-note">曾鎖漲停，但開盤沒有守住的優先案例。</p>
      </article>
      <article class="kpi">
        <p class="kpi-label">曾鎖漲停</p>
        <p class="kpi-value"><span id="kpiLocked">0</span><small>檔</small></p>
        <p class="kpi-note">盤前觀察期間曾出現漲停鎖單，不代表最終開盤結果。</p>
      </article>
      <article class="kpi">
        <p class="kpi-label">監控檔數</p>
        <p class="kpi-value"><span id="kpiTotal">0</span><small>檔</small></p>
        <p class="kpi-note">本案卷實際收錄並檢查的股票數。</p>
      </article>
    </div>
  </section>

  <section class="section reveal" style="--delay:150ms" aria-labelledby="overview-title">
    <div class="section-heading">
      <span class="section-no">02</span>
      <h2 id="overview-title">全體概覽</h2>
    </div>
    <p class="section-note">先看狀態占比，再比對有開盤資料標的的落差；落差以「開盤價相對漲停價」計算。</p>
    <div class="overview-grid">
      <figure class="figure-card">
        <figcaption class="figure-head">
          <div>
            <p class="fig-no">Fig. 01</p>
            <h3>狀態分佈</h3>
            <p>四種判讀結果的檔數與占比</p>
          </div>
        </figcaption>
        <div class="figure-body" id="statusFigure"></div>
      </figure>
      <figure class="figure-card">
        <figcaption class="figure-head">
          <div>
            <p class="fig-no">Fig. 02</p>
            <h3>全體開盤落差</h3>
            <p id="gapSubtitle">可疑標的優先，組內依落差絕對值由大到小</p>
          </div>
          <div class="chart-legend" aria-label="開盤落差圖圖例" id="gapLegend"></div>
        </figcaption>
        <div class="figure-body">
          <div class="figure-scroll gap-scroll">
            <div class="chart-stage" id="gapStage" tabindex="0" aria-label="開盤落差橫條圖"></div>
          </div>
        </div>
      </figure>
    </div>
  </section>

  <section class="section reveal" style="--delay:190ms" aria-labelledby="cases-title">
    <div class="section-heading">
      <span class="section-no">03</span>
      <h2 id="cases-title">優先案例</h2>
    </div>
    <p class="section-note">先列出疑似假試撮、鎖漲停守住與曾觸漲停標的；點開完整圖證可查看逐檔價量走勢。</p>
    <div class="case-grid" id="featuredCases"></div>
  </section>

  <section class="section reveal" style="--delay:230ms" aria-labelledby="index-title">
    <div class="section-heading">
      <span class="section-no">04</span>
      <h2 id="index-title">全體標的索引</h2>
    </div>
    <p class="section-note">所有監控標的完整列示；每檔都可展開 Fig. A 試撮價與 Fig. B 買一堆量。</p>
    <div class="index-card">
      <div class="table-scroll">
        <table class="stock-table">
          <colgroup>
            <col style="width:155px"><col style="width:175px"><col style="width:110px">
            <col style="width:110px"><col style="width:115px"><col style="width:110px">
            <col style="width:100px"><col style="width:70px">
          </colgroup>
          <thead>
            <tr>
              <th scope="col">代碼／名稱</th>
              <th scope="col">狀態</th>
              <th scope="col">試撮最高</th>
              <th scope="col">漲停價</th>
              <th scope="col">買一堆量</th>
              <th scope="col">開盤價</th>
              <th scope="col">落差 %</th>
              <th scope="col"><span class="sr-only">展開</span></th>
            </tr>
          </thead>
          <tbody id="stockRows"></tbody>
        </table>
      </div>
    </div>
  </section>

  <footer class="report-foot">
    <p><strong>判讀提醒：</strong>狀態僅是盤前試撮資料的規則式整理，不構成不法行為認定或交易建議。</p>
    <p>完整離線版 · 資料、圖表與互動均內嵌</p>
  </footer>
</main>

<script>
"use strict";

const RESULT = __RESULT_JSON__;
const STOCKS = Array.isArray(RESULT.stocks) ? RESULT.stocks : [];
const STATUS_ORDER = ["suspected_fake", "locked_held", "touched", "none"];
const STATUS = {
  suspected_fake: {label: "疑似假試撮", short: "疑似", icon: "!", color: "#C8322A", note: "曾鎖漲停，開盤未守住"},
  locked_held: {label: "鎖漲停守住", short: "守住", icon: "◆", color: "#B77514", note: "開盤仍守在漲停附近"},
  touched: {label: "曾觸漲停", short: "曾觸", icon: "✓", color: "#2F7D5B", note: "曾碰漲停，未形成優先疑點"},
  none: {label: "未觸及", short: "未觸", icon: "—", color: "#9B9384", note: "觀察期間未碰漲停"}
};
const COLORS = {
  ink: "#232019", muted: "#6B6459", quiet: "#9B9384", rule: "#E4DED2",
  paper: "#FAF8F3", card: "#FFFFFF", red: "#C8322A", amber: "#B77514",
  green: "#2F7D5B", blue: "#2F6DB0"
};
const sortedStocks = [...STOCKS].sort((a, b) => {
  const rank = STATUS_ORDER.indexOf(a.status || "none") - STATUS_ORDER.indexOf(b.status || "none");
  return rank || String(a.code || "").localeCompare(String(b.code || ""), "zh-Hant", {numeric: true});
});

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function finiteNumber(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && value.trim() === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function fmtPrice(value) {
  const number = finiteNumber(value);
  if (number === null) return '<span class="empty-value">—</span>';
  const digits = Math.abs(number) >= 1000 ? 1 : 2;
  return number.toLocaleString("zh-TW", {minimumFractionDigits: digits, maximumFractionDigits: digits});
}

function fmtVolume(value) {
  const number = finiteNumber(value);
  return number === null
    ? '<span class="empty-value">—</span>'
    : Math.round(number).toLocaleString("zh-TW");
}

function fmtGap(value) {
  const number = finiteNumber(value);
  if (number === null) return '<span class="empty-value">—</span>';
  const className = number < 0 ? "gap-negative" : number > 0 ? "gap-positive" : "";
  return `<span class="${className}">${number > 0 ? "+" : ""}${number.toFixed(2)}%</span>`;
}

function fmtDate(value) {
  if (!value) return "未提供";
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[1]} 年 ${Number(match[2])} 月 ${Number(match[3])} 日` : String(value);
}

function fmtTime(value, withSeconds = true) {
  if (!value) return "—";
  const text = String(value);
  const match = text.match(/T(\d{2}:\d{2}(?::\d{2})?)/) || text.match(/^(\d{2}:\d{2}(?::\d{2})?)/);
  if (match) return withSeconds ? match[1] : match[1].slice(0, 5);
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  return date.toLocaleTimeString("zh-TW", {hour: "2-digit", minute: "2-digit", second: withSeconds ? "2-digit" : undefined, hour12: false});
}

function fmtEpochTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}

function fmtGenerated(value) {
  if (!value) return "未提供";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-TW", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
  });
}

function sessionLabel(value) {
  const labels = {preopen: "盤前試撮", preclose: "收盤前試撮"};
  return labels[value] || (value ? String(value) : "未提供");
}

function statusBadge(status, large = false) {
  const key = STATUS[status] ? status : "none";
  const meta = STATUS[key];
  return `<span class="status-badge status-${key}${large ? " large" : ""}">
    <span class="status-icon" aria-hidden="true">${meta.icon}</span>
    <span>${meta.label}</span>
  </span>`;
}

function legendItem(label, color, style = "dot") {
  const shape = style === "line"
    ? '<span class="legend-line"></span>'
    : style === "dashed"
      ? '<span class="legend-line dashed"></span>'
      : '<span class="legend-dot"></span>';
  return `<span class="legend-item" style="color:${color}">${shape}<span style="color:${COLORS.muted}">${escapeHtml(label)}</span></span>`;
}

function statusRank(status) {
  const index = STATUS_ORDER.indexOf(status || "none");
  return index < 0 ? STATUS_ORDER.length : index;
}

function initReport() {
  const count = STOCKS.length;
  const suspected = STOCKS.filter(stock => stock.status === "suspected_fake").length;
  const locked = STOCKS.filter(stock => stock.locked_limit_up === true).length;
  document.getElementById("editionDate").textContent = fmtDate(RESULT.date);
  document.getElementById("metaDate").textContent = fmtDate(RESULT.date);
  document.getElementById("metaSession").textContent = sessionLabel(RESULT.session);
  document.getElementById("metaCount").textContent = `${count.toLocaleString("zh-TW")} 檔`;
  document.getElementById("metaGenerated").textContent = fmtGenerated(RESULT.generated_at);
  document.getElementById("kpiSuspected").textContent = suspected.toLocaleString("zh-TW");
  document.getElementById("kpiLocked").textContent = locked.toLocaleString("zh-TW");
  document.getElementById("kpiTotal").textContent = count.toLocaleString("zh-TW");
  document.getElementById("statusKey").innerHTML = STATUS_ORDER.map(key =>
    `<div>${statusBadge(key)}<span class="sr-only">：${escapeHtml(STATUS[key].note)}</span></div>`
  ).join("");
}

function drawStatusFigure() {
  const target = document.getElementById("statusFigure");
  const total = STOCKS.length;
  const counts = Object.fromEntries(STATUS_ORDER.map(key => [
    key, STOCKS.filter(stock => (stock.status || "none") === key).length
  ]));
  const radius = 76;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  const arcs = STATUS_ORDER.map(key => {
    const count = counts[key];
    const length = total ? count / total * circumference : 0;
    const arc = `<circle class="status-arc" tabindex="0" data-status="${key}" cx="112" cy="112" r="${radius}"
      fill="none" stroke="${STATUS[key].color}" stroke-width="24"
      stroke-dasharray="${length} ${Math.max(0, circumference - length)}"
      stroke-dashoffset="${-offset}" transform="rotate(-90 112 112)">
      <title>${STATUS[key].label}：${count} 檔</title>
    </circle>`;
    offset += length;
    return arc;
  }).join("");
  target.innerHTML = `
    <div class="status-stage">
      <svg class="status-svg" viewBox="0 0 224 224" role="img" aria-labelledby="status-title status-desc">
        <title id="status-title">四種試撮狀態分佈</title>
        <desc id="status-desc">${STATUS_ORDER.map(key => `${STATUS[key].label}${counts[key]}檔`).join("，")}</desc>
        <circle cx="112" cy="112" r="${radius}" fill="none" stroke="${COLORS.rule}" stroke-width="24"/>
        ${arcs}
        <text x="112" y="105" text-anchor="middle" fill="${COLORS.muted}" font-size="12"
          font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif">監控合計</text>
        <text x="112" y="133" text-anchor="middle" fill="${COLORS.ink}" font-size="29"
          font-family="Georgia,PMingLiU,serif">${total}</text>
      </svg>
      <div class="chart-tooltip" id="statusTooltip" hidden></div>
    </div>
    <div class="status-legend">
      ${STATUS_ORDER.map(key => `
        <div class="status-legend-row">
          ${statusBadge(key)}
          <span class="status-count">${counts[key]}</span>
        </div>`).join("")}
    </div>`;

  const tooltip = document.getElementById("statusTooltip");
  target.querySelectorAll(".status-arc").forEach(arc => {
    const show = event => {
      const key = arc.dataset.status;
      const pct = total ? counts[key] / total * 100 : 0;
      tooltip.innerHTML = `<strong>${STATUS[key].icon} ${STATUS[key].label}</strong><br>${counts[key]} 檔 · ${pct.toFixed(1)}%`;
      tooltip.hidden = false;
      const box = target.querySelector(".status-stage").getBoundingClientRect();
      const x = event.clientX ? event.clientX - box.left : box.width / 2;
      const y = event.clientY ? event.clientY - box.top : box.height / 2;
      tooltip.style.left = `${Math.max(8, Math.min(x + 10, box.width - 190))}px`;
      tooltip.style.top = `${Math.max(8, y - 55)}px`;
    };
    arc.addEventListener("pointerenter", show);
    arc.addEventListener("pointermove", show);
    arc.addEventListener("pointerleave", () => { tooltip.hidden = true; });
    arc.addEventListener("focus", show);
    arc.addEventListener("blur", () => { tooltip.hidden = true; });
  });
}

function gapColor(stock) {
  return STATUS[stock.status || "none"]?.color || COLORS.quiet;
}

function drawGapFigure() {
  const stage = document.getElementById("gapStage");
  const data = STOCKS
    .map(stock => ({stock, gap: finiteNumber(stock.open_gap_pct)}))
    .filter(item => item.gap !== null)
    .sort((a, b) => statusRank(a.stock.status) - statusRank(b.stock.status) || Math.abs(b.gap) - Math.abs(a.gap));
  document.getElementById("gapLegend").innerHTML = STATUS_ORDER.map(key =>
    legendItem(`${STATUS[key].icon} ${STATUS[key].label}`, STATUS[key].color)
  ).join("");
  document.getElementById("gapSubtitle").textContent =
    `有開盤資料 ${data.length}／監控 ${STOCKS.length} 檔；可疑優先，組內依落差絕對值由大到小`;

  if (!data.length) {
    stage.innerHTML = '<div class="empty-chart">本場次尚無可用的開盤價與開盤落差資料。<br>狀態分佈與買一堆量仍可正常閱讀。</div>';
    return;
  }

  const width = 820;
  const rowHeight = 34;
  const margin = {top: 30, right: 74, bottom: 44, left: 150};
  const height = margin.top + margin.bottom + data.length * rowHeight;
  const rawMin = Math.min(0, ...data.map(item => item.gap));
  const rawMax = Math.max(0, ...data.map(item => item.gap));
  const spread = Math.max(rawMax - rawMin, .5);
  const min = rawMin - spread * .08;
  const max = rawMax + spread * .08;
  const plotWidth = width - margin.left - margin.right;
  const x = value => margin.left + (value - min) / (max - min) * plotWidth;
  const zeroX = x(0);
  const ticks = Array.from({length: 5}, (_, index) => min + (max - min) * index / 4);
  const grid = ticks.map(value => `
    <line x1="${x(value)}" y1="${margin.top - 8}" x2="${x(value)}" y2="${height - margin.bottom}"
      stroke="${COLORS.rule}" stroke-width="1"/>
    <text x="${x(value)}" y="${height - 17}" fill="${COLORS.muted}" font-size="10"
      text-anchor="middle" font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif">${value.toFixed(1)}%</text>`
  ).join("");
  const bars = data.map((item, index) => {
    const yy = margin.top + index * rowHeight;
    const valueX = x(item.gap);
    const left = Math.min(zeroX, valueX);
    const barWidth = Math.max(2, Math.abs(valueX - zeroX));
    const labelX = item.gap < 0 ? left - 7 : left + barWidth + 7;
    const anchor = item.gap < 0 ? "end" : "start";
    return `<g class="gap-row" tabindex="0" role="img" data-index="${index}"
      aria-label="${escapeHtml(item.stock.code)} ${escapeHtml(item.stock.name || "")}，${STATUS[item.stock.status || "none"].label}，開盤落差 ${item.gap.toFixed(2)}%">
      <text x="${margin.left - 12}" y="${yy + 18}" text-anchor="end" fill="${COLORS.ink}" font-size="11"
        font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif">${STATUS[item.stock.status || "none"].icon} ${STATUS[item.stock.status || "none"].short} · ${escapeHtml(item.stock.code)} ${escapeHtml(item.stock.name || "")}</text>
      <rect x="${left}" y="${yy + 5}" width="${barWidth}" height="18" rx="2" fill="${gapColor(item.stock)}" opacity=".88"/>
      <text x="${labelX}" y="${yy + 18}" text-anchor="${anchor}" fill="${COLORS.ink}" font-size="10"
        font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif">${item.gap > 0 ? "+" : ""}${item.gap.toFixed(2)}%</text>
    </g>`;
  }).join("");

  stage.innerHTML = `
    <svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="gap-title gap-desc">
      <title id="gap-title">全體開盤落差橫條圖</title>
      <desc id="gap-desc">共有 ${data.length} 檔有開盤資料，先列可疑標的，同狀態內按落差絕對值由大到小。</desc>
      ${grid}
      <line x1="${zeroX}" y1="${margin.top - 8}" x2="${zeroX}" y2="${height - margin.bottom}"
        stroke="${COLORS.ink}" stroke-width="1.2"/>
      ${bars}
      <line id="gapCrosshair" x1="${zeroX}" y1="${margin.top - 8}" x2="${zeroX}" y2="${height - margin.bottom}"
        stroke="${COLORS.blue}" stroke-width="1" stroke-dasharray="3 4" opacity="0" pointer-events="none"/>
    </svg>
    <div class="chart-tooltip" id="gapTooltip" hidden></div>`;

  const svg = stage.querySelector("svg");
  const tooltip = document.getElementById("gapTooltip");
  const crosshair = document.getElementById("gapCrosshair");
  const show = (event, index) => {
    const item = data[index];
    const rect = svg.getBoundingClientRect();
    const pointerX = event.clientX || rect.left + x(item.gap) / width * rect.width;
    const pointerY = event.clientY || rect.top + (margin.top + index * rowHeight) / height * rect.height;
    const viewX = (pointerX - rect.left) / rect.width * width;
    crosshair.setAttribute("x1", viewX);
    crosshair.setAttribute("x2", viewX);
    crosshair.setAttribute("opacity", "1");
    tooltip.innerHTML = `<strong>${escapeHtml(item.stock.code)} ${escapeHtml(item.stock.name || "")}</strong><br>
      ${STATUS[item.stock.status || "none"].icon} ${STATUS[item.stock.status || "none"].label}<br>
      開盤價　<strong>${fmtPrice(item.stock.open_price)}</strong><br>
      漲停價　<strong>${fmtPrice(item.stock.limit_up)}</strong><br>
      落差　　<strong>${item.gap > 0 ? "+" : ""}${item.gap.toFixed(2)}%</strong>`;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(6, Math.min((pointerX - rect.left) + 12, rect.width - 198))}px`;
    tooltip.style.top = `${Math.max(6, pointerY - rect.top - 54)}px`;
  };
  stage.querySelectorAll(".gap-row").forEach(row => {
    row.addEventListener("pointerenter", event => show(event, Number(row.dataset.index)));
    row.addEventListener("pointermove", event => show(event, Number(row.dataset.index)));
    row.addEventListener("pointerleave", () => {
      tooltip.hidden = true;
      crosshair.setAttribute("opacity", "0");
    });
    row.addEventListener("focus", event => show(event, Number(row.dataset.index)));
    row.addEventListener("blur", () => {
      tooltip.hidden = true;
      crosshair.setAttribute("opacity", "0");
    });
  });
}

function metric(label, value) {
  return `<div class="metric"><span>${escapeHtml(label)}</span><strong>${value}</strong></div>`;
}

function renderFeatured() {
  const target = document.getElementById("featuredCases");
  const featured = sortedStocks.filter(stock => (stock.status || "none") !== "none");
  if (!featured.length) {
    target.innerHTML = '<div class="no-featured">本場次沒有標記為疑似假試撮、鎖漲停守住或曾觸漲停的優先案例。</div>';
    return;
  }
  target.innerHTML = featured.map(stock => {
    const index = sortedStocks.indexOf(stock);
    return `<article class="case-summary" data-status="${escapeHtml(stock.status || "none")}">
      <div class="case-summary-head">
        <div>
          <p class="case-code">CASE ${escapeHtml(stock.code)}</p>
          <h3>${escapeHtml(stock.name || "未命名")}</h3>
        </div>
        ${statusBadge(stock.status, true)}
      </div>
      <div class="case-metrics">
        ${metric("試撮最高", fmtPrice(stock.sim_high))}
        ${metric("漲停價", fmtPrice(stock.limit_up))}
        ${metric("買一堆量", fmtVolume(stock.max_bid0_volume))}
        ${metric("開盤價", fmtPrice(stock.open_price))}
        ${metric("開盤落差", fmtGap(stock.open_gap_pct))}
      </div>
      <button class="text-button" type="button" data-jump-index="${index}">
        <span>展開完整圖證</span><span aria-hidden="true">↓</span>
      </button>
    </article>`;
  }).join("");
  target.querySelectorAll("[data-jump-index]").forEach(button => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.jumpIndex);
      toggleDetail(index, true);
      document.getElementById(`stock-${index}`).scrollIntoView({behavior: "smooth", block: "start"});
    });
  });
}

function renderTable() {
  const target = document.getElementById("stockRows");
  target.innerHTML = sortedStocks.map((stock, index) => `
    <tr class="stock-row" id="stock-${index}">
      <td class="stock-identity"><strong>${escapeHtml(stock.name || "未命名")}</strong><span>${escapeHtml(stock.code)}</span></td>
      <td>${statusBadge(stock.status)}</td>
      <td class="num">${fmtPrice(stock.sim_high)}</td>
      <td class="num">${fmtPrice(stock.limit_up)}</td>
      <td class="num">${fmtVolume(stock.max_bid0_volume)}</td>
      <td class="num">${fmtPrice(stock.open_price)}</td>
      <td class="num">${fmtGap(stock.open_gap_pct)}</td>
      <td><button class="expand-button" type="button" data-index="${index}" aria-expanded="false"
        aria-controls="detail-${index}" aria-label="展開 ${escapeHtml(stock.code)} ${escapeHtml(stock.name || "")} 圖證"><span aria-hidden="true">＋</span></button></td>
    </tr>
    <tr class="detail-row" id="detail-${index}" hidden>
      <td colspan="8"><div class="detail-mount" data-index="${index}"></div></td>
    </tr>`).join("");
  target.querySelectorAll(".expand-button").forEach(button => {
    button.addEventListener("click", () => toggleDetail(Number(button.dataset.index)));
  });
}

function evidence(label, value) {
  return `<div class="evidence"><span>${escapeHtml(label)}</span><strong>${value}</strong></div>`;
}

function detailMarkup(stock, index) {
  return `<article class="case-detail">
    <header class="case-detail-head">
      <div>
        <p class="case-code">CASE ${escapeHtml(stock.code)}</p>
        <h3>${escapeHtml(stock.name || "未命名")} · 圖證</h3>
        <p>${STATUS[stock.status || "none"].note}；首次鎖漲停時間 ${escapeHtml(fmtTime(stock.first_lock_time))}。</p>
      </div>
      ${statusBadge(stock.status, true)}
    </header>
    <div class="evidence-strip">
      ${evidence("參考價", fmtPrice(stock.reference))}
      ${evidence("試撮最高", fmtPrice(stock.sim_high))}
      ${evidence("漲停價", fmtPrice(stock.limit_up))}
      ${evidence("買一堆量", fmtVolume(stock.max_bid0_volume))}
      ${evidence("開盤價", fmtPrice(stock.open_price))}
      ${evidence("落差 %", fmtGap(stock.open_gap_pct))}
    </div>
    <div class="case-figures">
      <figure class="figure-card">
        <figcaption class="figure-head">
          <div>
            <p class="fig-no">Fig. ${escapeHtml(stock.code)}–A</p>
            <h4>試撮價走勢</h4>
            <p>藍線為試撮價，紅虛線為漲停價，綠點／線為開盤標記。</p>
          </div>
          <div class="chart-legend" aria-label="試撮價圖圖例">
            ${legendItem("試撮價", COLORS.blue, "line")}
            ${legendItem("漲停價", COLORS.red, "dashed")}
            ${legendItem("開盤", COLORS.green)}
          </div>
        </figcaption>
        <div class="figure-body">
          <div class="figure-scroll">
            <div class="chart-stage price-stage" data-index="${index}" data-kind="price" tabindex="0"
              aria-label="${escapeHtml(stock.code)} ${escapeHtml(stock.name || "")} 試撮價圖；按左右方向鍵查看資料點，按 Esc 清除提示"></div>
          </div>
        </div>
      </figure>
      <figure class="figure-card">
        <figcaption class="figure-head">
          <div>
            <p class="fig-no">Fig. ${escapeHtml(stock.code)}–B</p>
            <h4>買一堆量隨時間</h4>
            <p>藍色面積柱為買一量；若偵測撤單驟降，以硃紅直接標記。</p>
          </div>
          <div class="chart-legend" aria-label="買一堆量圖圖例">
            ${legendItem("買一堆量", COLORS.blue)}
            ${legendItem("撤單驟降", COLORS.red)}
            ${legendItem("開盤", COLORS.green, "dashed")}
          </div>
        </figcaption>
        <div class="figure-body">
          <div class="figure-scroll">
            <div class="chart-stage volume-stage" data-index="${index}" data-kind="volume" tabindex="0"
              aria-label="${escapeHtml(stock.code)} ${escapeHtml(stock.name || "")} 買一堆量圖；按左右方向鍵查看資料點，按 Esc 清除提示"></div>
          </div>
          <p class="source-note">表列「買一堆量」沿用結果檔摘要值；圖形忠實呈現逐筆序列，兩者可能因來源取樣時點不同而有差異。</p>
        </div>
      </figure>
    </div>
  </article>`;
}

function toggleDetail(index, forceOpen = false) {
  const detail = document.getElementById(`detail-${index}`);
  const button = document.querySelector(`.expand-button[data-index="${index}"]`);
  if (!detail || !button) return;
  const shouldOpen = forceOpen || detail.hidden;
  detail.hidden = !shouldOpen;
  button.setAttribute("aria-expanded", String(shouldOpen));
  button.setAttribute("aria-label", `${shouldOpen ? "收合" : "展開"} ${sortedStocks[index].code} ${sortedStocks[index].name || ""} 圖證`);
  if (shouldOpen) {
    const mount = detail.querySelector(".detail-mount");
    if (!mount.dataset.rendered) {
      mount.innerHTML = detailMarkup(sortedStocks[index], index);
      mount.dataset.rendered = "true";
      drawCaseCharts(mount, sortedStocks[index], index);
    }
  }
}

function timeValue(value, fallback) {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function chartModel(stock) {
  const pricePoints = (Array.isArray(stock.sim_price_series) ? stock.sim_price_series : [])
    .map((point, index) => ({
      t: point?.t || "",
      x: timeValue(point?.t, index),
      value: finiteNumber(point?.price),
      simtrade: point?.simtrade !== false
    }))
    .filter(point => point.value !== null);
  const volumePoints = (Array.isArray(stock.bid0_series) ? stock.bid0_series : [])
    .map((point, index) => ({
      t: point?.t || "",
      x: timeValue(point?.t, index),
      value: finiteNumber(point?.bid0_volume),
      bid0Price: finiteNumber(point?.bid0_price)
    }))
    .filter(point => point.value !== null);
  const all = [...pricePoints, ...volumePoints];
  let minX = all.length ? Math.min(...all.map(point => point.x)) : 0;
  let maxX = all.length ? Math.max(...all.map(point => point.x)) : 1;
  if (minX === maxX) maxX = minX + 1;
  const openPoint = pricePoints.find(point => point.simtrade === false) || null;
  let openX = openPoint?.x ?? null;
  if (openX === null && RESULT.window?.end) {
    const end = String(RESULT.window.end);
    if (/^\d{2}:\d{2}/.test(end) && RESULT.date) {
      openX = timeValue(`${RESULT.date}T${end.length === 5 ? `${end}:00` : end}`, NaN);
    } else {
      openX = timeValue(end, NaN);
    }
    if (!Number.isFinite(openX)) openX = null;
  }
  if (openX !== null && (openX < minX || openX > maxX)) openX = null;
  let dropIndex = -1;
  if (stock.bid0_dropped && volumePoints.length > 1) {
    let largestDrop = 0;
    for (let index = 1; index < volumePoints.length; index += 1) {
      const drop = volumePoints[index - 1].value - volumePoints[index].value;
      if (drop > largestDrop) {
        largestDrop = drop;
        dropIndex = index;
      }
    }
  }
  return {pricePoints, volumePoints, minX, maxX, openPoint, openX, dropIndex};
}

function scales(model, yMin, yMax) {
  const width = 820;
  const height = 272;
  const margin = {top: 22, right: 28, bottom: 38, left: 62};
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  return {
    width, height, margin, plotWidth, plotHeight, yMin, yMax,
    x: value => margin.left + (value - model.minX) / (model.maxX - model.minX) * plotWidth,
    y: value => margin.top + (yMax - value) / (yMax - yMin) * plotHeight
  };
}

function timeTicks(model, chart) {
  return Array.from({length: 5}, (_, index) => {
    const value = model.minX + (model.maxX - model.minX) * index / 4;
    const xx = chart.x(value);
    return `<line x1="${xx}" y1="${chart.margin.top}" x2="${xx}" y2="${chart.height - chart.margin.bottom}"
      stroke="${COLORS.rule}" stroke-width="1" opacity=".72"/>
      <text x="${xx}" y="${chart.height - 15}" text-anchor="middle" fill="${COLORS.muted}" font-size="10"
      font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif">${fmtEpochTime(value)}</text>`;
  }).join("");
}

function valueTicks(chart, formatter) {
  return Array.from({length: 5}, (_, index) => {
    const value = chart.yMin + (chart.yMax - chart.yMin) * index / 4;
    const yy = chart.y(value);
    return `<line x1="${chart.margin.left}" y1="${yy}" x2="${chart.width - chart.margin.right}" y2="${yy}"
      stroke="${COLORS.rule}" stroke-width="1" opacity=".72"/>
      <text x="${chart.margin.left - 10}" y="${yy + 4}" text-anchor="end" fill="${COLORS.muted}" font-size="10"
      font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif">${formatter(value)}</text>`;
  }).join("");
}

function openMarker(model, chart, stock) {
  if (model.openX === null) return "";
  const xx = chart.x(model.openX);
  const openPrice = finiteNumber(stock.open_price);
  const point = openPrice !== null && openPrice >= chart.yMin && openPrice <= chart.yMax
    ? `<circle cx="${xx}" cy="${chart.y(openPrice)}" r="5" fill="${COLORS.green}" stroke="${COLORS.card}" stroke-width="2"/>`
    : "";
  return `<line x1="${xx}" y1="${chart.margin.top}" x2="${xx}" y2="${chart.height - chart.margin.bottom}"
    stroke="${COLORS.green}" stroke-width="1.3" stroke-dasharray="3 4"/>
    <text x="${xx + 5}" y="${chart.margin.top + 11}" fill="${COLORS.green}" font-size="10"
      font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif">開盤</text>${point}`;
}

function hoverLayer(chart) {
  return `<g class="hover-layer" opacity="0" pointer-events="none">
      <line class="hover-line" x1="0" y1="${chart.margin.top}" x2="0" y2="${chart.height - chart.margin.bottom}"
        stroke="${COLORS.blue}" stroke-width="1" stroke-dasharray="3 4"/>
      <circle class="hover-dot" cx="0" cy="0" r="4" fill="${COLORS.card}" stroke="${COLORS.blue}" stroke-width="2"/>
    </g>
    <rect class="plot-hitbox" x="${chart.margin.left}" y="${chart.margin.top}"
      width="${chart.plotWidth}" height="${chart.plotHeight}" fill="transparent"/>`;
}

function drawPriceChart(stage, stock, model, index) {
  if (!model.pricePoints.length) {
    stage.innerHTML = '<div class="empty-chart">本檔無試撮價資料。<br>漲停價與買一堆量仍保留於摘要及下一張圖。</div>';
    return;
  }
  const limit = finiteNumber(stock.limit_up);
  const values = model.pricePoints.map(point => point.value);
  if (limit !== null) values.push(limit);
  const open = finiteNumber(stock.open_price);
  if (open !== null) values.push(open);
  let yMin = Math.min(...values);
  let yMax = Math.max(...values);
  const pad = Math.max((yMax - yMin) * .13, Math.abs(yMax) * .004, .25);
  yMin -= pad;
  yMax += pad;
  const chart = scales(model, yMin, yMax);
  const path = model.pricePoints.map((point, pointIndex) =>
    `${pointIndex ? "L" : "M"}${chart.x(point.x).toFixed(2)},${chart.y(point.value).toFixed(2)}`
  ).join(" ");
  const limitLine = limit === null ? "" : `
    <line class="limit-line" x1="${chart.margin.left}" y1="${chart.y(limit)}"
      x2="${chart.width - chart.margin.right}" y2="${chart.y(limit)}"
      stroke="${COLORS.red}" stroke-width="1.4" stroke-dasharray="7 5"/>
    <text x="${chart.width - chart.margin.right - 4}" y="${chart.y(limit) - 6}"
      text-anchor="end" fill="${COLORS.red}" font-size="10"
      font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif">漲停 ${limit.toFixed(2)}</text>`;
  const openDots = model.pricePoints.filter(point => point.simtrade === false).map(point =>
    `<circle class="open-dot" cx="${chart.x(point.x)}" cy="${chart.y(point.value)}" r="5"
      fill="${COLORS.green}" stroke="${COLORS.card}" stroke-width="2"/>`
  ).join("");
  stage.innerHTML = `<svg class="chart-svg" viewBox="0 0 ${chart.width} ${chart.height}" role="img"
      aria-labelledby="price-title-${index} price-desc-${index}">
      <title id="price-title-${index}">${escapeHtml(stock.code)} ${escapeHtml(stock.name || "")} 試撮價走勢</title>
      <desc id="price-desc-${index}">藍線為試撮價，紅色虛線為漲停價，綠色標記為開盤。</desc>
      ${timeTicks(model, chart)}
      ${valueTicks(chart, value => value.toFixed(Math.abs(value) >= 1000 ? 0 : 2))}
      ${limitLine}
      ${openMarker(model, chart, stock)}
      <path class="price-series" d="${path}" fill="none" stroke="${COLORS.blue}" stroke-width="2.4"
        stroke-linejoin="round" stroke-linecap="round"/>
      ${openDots}
      ${hoverLayer(chart)}
    </svg><div class="chart-tooltip" hidden aria-hidden="true"></div>`;
  stage._chart = chart;
}

function drawVolumeChart(stage, stock, model, index) {
  if (!model.volumePoints.length) {
    stage.innerHTML = '<div class="empty-chart">本檔無買一堆量序列，無法繪製時間變化。</div>';
    return;
  }
  const yMax = Math.max(1, ...model.volumePoints.map(point => point.value)) * 1.12;
  const chart = scales(model, 0, yMax);
  const barWidth = Math.max(3, Math.min(15, chart.plotWidth / Math.max(1, model.volumePoints.length) * .62));
  const bars = model.volumePoints.map((point, pointIndex) => {
    const xx = chart.x(point.x);
    const yy = chart.y(point.value);
    const isDrop = pointIndex === model.dropIndex;
    return `<rect class="volume-bar${isDrop ? " drop-bar" : ""}" x="${xx - barWidth / 2}" y="${yy}"
      width="${barWidth}" height="${chart.y(0) - yy}" rx="1"
      fill="${isDrop ? COLORS.red : COLORS.blue}" opacity="${isDrop ? .92 : .55}"/>`;
  }).join("");
  const area = [
    `M${chart.x(model.volumePoints[0].x)},${chart.y(0)}`,
    ...model.volumePoints.map(point => `L${chart.x(point.x)},${chart.y(point.value)}`),
    `L${chart.x(model.volumePoints[model.volumePoints.length - 1].x)},${chart.y(0)} Z`
  ].join(" ");
  let dropMarker = "";
  if (model.dropIndex >= 0) {
    const point = model.volumePoints[model.dropIndex];
    const xx = chart.x(point.x);
    const yy = chart.y(point.value);
    dropMarker = `<circle class="drop-marker" cx="${xx}" cy="${yy}" r="5"
      fill="${COLORS.red}" stroke="${COLORS.card}" stroke-width="2"/>
      <text x="${xx}" y="${Math.max(chart.margin.top + 10, yy - 11)}" text-anchor="middle"
      fill="${COLORS.red}" font-size="10" font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif">撤單驟降</text>`;
  }
  stage.innerHTML = `<svg class="chart-svg" viewBox="0 0 ${chart.width} ${chart.height}" role="img"
      aria-labelledby="volume-title-${index} volume-desc-${index}">
      <title id="volume-title-${index}">${escapeHtml(stock.code)} ${escapeHtml(stock.name || "")} 買一堆量隨時間</title>
      <desc id="volume-desc-${index}">藍色面積柱為買一堆量，硃紅標記為撤單驟降，綠色虛線為開盤。</desc>
      ${timeTicks(model, chart)}
      ${valueTicks(chart, value => Math.round(value).toLocaleString("zh-TW"))}
      ${openMarker(model, chart, stock)}
      <path class="volume-area" d="${area}" fill="${COLORS.blue}" opacity=".09"/>
      ${bars}${dropMarker}${hoverLayer(chart)}
    </svg><div class="chart-tooltip" hidden aria-hidden="true"></div>`;
  stage._chart = chart;
}

function nearest(points, targetX) {
  if (!points.length) return null;
  return points.reduce((best, point) =>
    Math.abs(point.x - targetX) < Math.abs(best.x - targetX) ? point : best
  );
}

function bindCaseHover(mount, stock, model) {
  const stages = [...mount.querySelectorAll(".chart-stage")].filter(stage => stage._chart);
  if (!stages.length) return;
  const liveTooltip = stages[0].querySelector(".chart-tooltip");
  liveTooltip.removeAttribute("aria-hidden");
  liveTooltip.setAttribute("role", "status");
  liveTooltip.setAttribute("aria-live", "polite");

  const update = targetX => {
    const price = nearest(model.pricePoints, targetX);
    const volume = nearest(model.volumePoints, targetX);
    const time = price?.t || volume?.t || "";
    const limit = finiteNumber(stock.limit_up);
    const distance = price && limit
      ? (price.value / limit - 1) * 100
      : null;
    const isDrop = Boolean(volume && model.dropIndex >= 0 && model.volumePoints[model.dropIndex] === volume);
    const tooltipHtml = `<strong>${escapeHtml(fmtTime(time))}</strong><br>
      試撮價　<strong>${price ? fmtPrice(price.value) : "—"}</strong><br>
      買一價　<strong>${volume ? fmtPrice(volume.bid0Price) : "—"}</strong><br>
      買一量　<strong>${volume ? fmtVolume(volume.value) : "—"}</strong><br>
      距漲停　<strong>${distance === null ? "—" : `${distance > 0 ? "+" : ""}${distance.toFixed(2)}%`}</strong>
      ${isDrop ? `<br><strong style="color:${COLORS.red}">! 撤單驟降</strong>` : ""}`;

    stages.forEach(stage => {
      const chart = stage._chart;
      const kind = stage.dataset.kind;
      const point = kind === "price" ? price : volume;
      const layer = stage.querySelector(".hover-layer");
      const line = stage.querySelector(".hover-line");
      const dot = stage.querySelector(".hover-dot");
      const tooltip = stage.querySelector(".chart-tooltip");
      const xx = chart.x(targetX);
      layer.setAttribute("opacity", "1");
      line.setAttribute("x1", xx);
      line.setAttribute("x2", xx);
      dot.setAttribute("cx", xx);
      dot.setAttribute("cy", point ? chart.y(point.value) : chart.y(chart.yMin));
      tooltip.innerHTML = tooltipHtml;
      tooltip.hidden = false;
      const leftPct = xx / chart.width * 100;
      const topPct = (point ? chart.y(point.value) : chart.margin.top) / chart.height * 100;
      tooltip.style.left = `${leftPct}%`;
      tooltip.style.top = `${topPct}%`;
      const horizontal = leftPct > 75 ? "calc(-100% - 10px)" : "10px";
      const vertical = topPct < 38 ? "12px" : "-102%";
      tooltip.style.transform = `translate(${horizontal}, ${vertical})`;
    });
  };

  const clear = () => stages.forEach(stage => {
    stage.querySelector(".hover-layer").setAttribute("opacity", "0");
    stage.querySelector(".chart-tooltip").hidden = true;
  });

  stages.forEach(stage => {
    const svg = stage.querySelector("svg");
    const hitbox = stage.querySelector(".plot-hitbox");
    if (!svg || !hitbox) return;
    const move = event => {
      const rect = svg.getBoundingClientRect();
      const viewX = (event.clientX - rect.left) / rect.width * stage._chart.width;
      const ratio = Math.max(0, Math.min(1, (viewX - stage._chart.margin.left) / stage._chart.plotWidth));
      update(model.minX + ratio * (model.maxX - model.minX));
    };
    hitbox.addEventListener("pointermove", move);
    hitbox.addEventListener("pointerdown", move);
    hitbox.addEventListener("pointerleave", clear);
    stage.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Escape"].includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Escape") {
        clear();
        stage._focusRatio = null;
        return;
      }
      const step = 1 / Math.max(1, Math.max(model.pricePoints.length, model.volumePoints.length) - 1);
      stage._focusRatio = Math.max(0, Math.min(1, (stage._focusRatio ?? 0) + (event.key === "ArrowRight" ? step : -step)));
      update(model.minX + stage._focusRatio * (model.maxX - model.minX));
    });
  });
}

function drawCaseCharts(mount, stock, index) {
  const model = chartModel(stock);
  drawPriceChart(mount.querySelector(".price-stage"), stock, model, index);
  drawVolumeChart(mount.querySelector(".volume-stage"), stock, model, index);
  bindCaseHover(mount, stock, model);
}

initReport();
drawStatusFigure();
drawGapFigure();
renderFeatured();
renderTable();
</script>
</body>
</html>
"""


def generate(payload: dict[str, Any], output_path: Path) -> None:
    html = HTML_TEMPLATE.replace("__RESULT_JSON__", json_for_script(payload))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(html)


def main() -> int:
    args = parse_args()
    input_path = Path(args.input_path)
    output_path = Path(args.output_path)
    if not input_path.is_file():
        raise SystemExit(f"找不到輸入檔：{input_path}")
    try:
        payload = read_result(input_path)
        generate(payload, output_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"產生 dashboard 失敗：{exc}") from exc

    print(f"晨間簡報已產生：{output_path.resolve()}")
    print(f"內嵌股票數：{len(payload.get('stocks', []))}")
    print("離線模式：資料、CSS、JavaScript 與 SVG 圖表均已內嵌")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
