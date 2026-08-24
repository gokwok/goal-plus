from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from html import escape
import json
from math import floor, isclose, isfinite, log10
from pathlib import Path
from typing import Any

from goal_plus.agent_hosts import get_agent_host_adapter
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import (
    AgentSessionRecord,
    CandidateRecord,
    EvidenceAnnotationTask,
    FrozenSpec,
    GoalPlusRecord,
    IterationRecord,
    RunRecord,
    SearchPlan,
)
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.runtime import FileSearchRuntime, load_json


REPORT_SCHEMA_VERSION = 1
REPORT_DOCUMENT_SCHEMA_VERSION = 1
STOP_HOOK_EVENT_NAMES = ("Stop", "SubagentStop")
STOP_HOOK_DECISIONS = ("block", "allow", "skipped", "error", "unknown")


_REPORT_CSS = """
:root {
  color-scheme: light;
  --page: #f4f6f8;
  --surface: #ffffff;
  --surface-subtle: #f8fafb;
  --text: #17212b;
  --muted: #5b6977;
  --border: #dce2e8;
  --border-strong: #b8c2cc;
  --accent: #176b87;
  --accent-soft: #e7f2f5;
  --success: #18794e;
  --success-soft: #e8f5ee;
  --warning: #a15c00;
  --warning-soft: #fff3d6;
  --failure: #b42318;
  --failure-soft: #fdecea;
  --worker: #6b5aa6;
  --parent: #b35c00;
  --metric-1: #dceff2;
  --metric-2: #9bcdd5;
  --metric-3: #4f9fb0;
  --metric-4: #176b87;
  --radius: 8px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--page);
  color: var(--text);
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.5;
  letter-spacing: 0;
}
button, input, select { font: inherit; }
button { letter-spacing: 0; }
a { color: var(--accent); }
code, pre, .mono, .metric-value, .timeline-time {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
.wrap { width: min(1440px, 100%); margin: 0 auto; padding: 0 24px; }
.masthead {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.masthead-inner {
  min-height: 82px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}
.identity { min-width: 0; }
.eyebrow {
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 4px; font-size: 28px; line-height: 36px; }
h2 { margin-bottom: 20px; font-size: 20px; line-height: 28px; }
h3 { margin-bottom: 12px; font-size: 14px; line-height: 20px; }
.identity-line { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.id-line { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.masthead-actions { display: flex; align-items: center; gap: 18px; flex: 0 0 auto; }
.generated { color: var(--muted); font-size: 11px; text-align: right; }
.generated strong { display: block; color: var(--text); font-size: 12px; }
.button {
  min-height: 36px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 7px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  font-weight: 650;
}
.button:hover { background: var(--surface-subtle); }
.button svg { width: 16px; height: 16px; }
.status {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-subtle);
  color: var(--muted);
  font-size: 11px;
  font-weight: 750;
  text-transform: uppercase;
  white-space: nowrap;
}
.status.success { color: var(--success); border-color: #b8dfca; background: var(--success-soft); }
.status.warning { color: var(--warning); border-color: #ead298; background: var(--warning-soft); }
.status.failure { color: var(--failure); border-color: #efbbb6; background: var(--failure-soft); }
.status.official { color: var(--accent); border-color: #a9d2dc; background: var(--accent-soft); }
.section-nav {
  position: sticky;
  top: 0;
  z-index: 20;
  background: rgba(255, 255, 255, 0.97);
  border-bottom: 1px solid var(--border);
}
.section-nav .wrap { display: flex; gap: 26px; overflow-x: auto; }
.section-nav a {
  padding: 13px 0 11px;
  color: var(--muted);
  border-bottom: 2px solid transparent;
  font-size: 13px;
  font-weight: 650;
  text-decoration: none;
  white-space: nowrap;
}
.section-nav a:hover { color: var(--accent); border-color: var(--accent); }
main { padding-top: 30px; padding-bottom: 72px; }
main.wrap { overflow-x: clip; }
.report-section {
  scroll-margin-top: 58px;
  padding: 0 0 40px;
  margin: 0 0 40px;
  border-bottom: 1px solid var(--border);
}
.section-kicker { margin-bottom: 14px; color: var(--muted); font-size: 11px; font-weight: 750; text-transform: uppercase; }
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(8, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--border);
}
.kpi { min-width: 0; min-height: 105px; padding: 16px; background: var(--surface); }
.kpi-label { color: var(--muted); font-size: 11px; font-weight: 650; }
.metric-value { margin: 8px 0 2px; font-size: 22px; line-height: 28px; font-weight: 750; overflow-wrap: anywhere; }
.metric-value.success { color: var(--success); }
.metric-value.warning { color: var(--warning); }
.kpi-detail { color: var(--muted); font-size: 11px; }
.two-column { display: grid; grid-template-columns: minmax(0, 2fr) minmax(300px, 1fr); gap: 24px; }
.panel { border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.panel-body { padding: 20px; }
.panel + .panel { margin-top: 16px; }
.objective { margin-bottom: 0; color: var(--text); font-size: 15px; line-height: 24px; overflow-wrap: anywhere; }
.raw-goal { color: var(--muted); white-space: pre-wrap; overflow-wrap: anywhere; }
.fact-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.fact { min-width: 0; padding-top: 12px; border-top: 1px solid var(--border); }
.fact dt { color: var(--muted); font-size: 11px; font-weight: 650; }
.fact dd { margin: 4px 0 0; font-weight: 650; overflow-wrap: anywhere; }
.completion-note { border-left: 3px solid var(--success); }
.completion-note p { margin-bottom: 0; color: var(--muted); }
.metric-gap-list { margin: 0; padding: 0; list-style: none; }
.metric-gap-list li { display: grid; grid-template-columns: minmax(190px, 0.8fr) 110px minmax(260px, 2fr); gap: 14px; padding: 9px 0; border-top: 1px solid var(--border); }
.metric-gap-list li:first-child { border-top: 0; }
.metric-gap-list code { color: var(--text); overflow-wrap: anywhere; }
.metric-gap-kind { color: var(--muted); font-size: 10px; font-weight: 750; text-transform: uppercase; }
.metric-gap-reason { color: var(--muted); font-size: 12px; }
.coverage { margin-top: 16px; }
.coverage-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 12px; }
.coverage-bar { height: 5px; margin-top: 7px; overflow: hidden; border-radius: 3px; background: var(--border); }
.coverage-bar > span { display: block; height: 100%; background: var(--accent); }
.timeline-shell { overflow: hidden; }
.trajectory-shell { margin-bottom: 18px; }
.trajectory-head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 8px; }
.trajectory-head h3 { margin: 0; }
.trajectory-head span { color: var(--muted); font-size: 11px; }
.trajectory-plot { width: 100%; min-height: 380px; }
.stop-progress-chart { padding: 18px 20px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }
.stop-progress-chart svg { display: block; width: 100%; height: auto; min-height: 180px; }
.stop-progress-axis { display: flex; justify-content: space-between; margin-top: 6px; color: var(--muted); font-size: 10px; }
.timeline-head { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 18px 20px; border-bottom: 1px solid var(--border); }
.timeline-head h2 { margin: 0; }
.metric-lens-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-subtle);
}
.metric-scale { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 10px; }
.metric-scale-bar { display: grid; grid-template-columns: repeat(4, 18px); height: 10px; overflow: hidden; border: 1px solid var(--border-strong); border-radius: 3px; }
.metric-scale-bar i:nth-child(1) { background: var(--metric-1); }
.metric-scale-bar i:nth-child(2) { background: var(--metric-2); }
.metric-scale-bar i:nth-child(3) { background: var(--metric-3); }
.metric-scale-bar i:nth-child(4) { background: var(--metric-4); }
.metric-control { display: inline-flex; overflow-x: auto; border: 1px solid var(--border-strong); border-radius: 6px; background: var(--surface); }
.metric-control button {
  min-height: 30px;
  padding: 5px 9px;
  border: 0;
  border-right: 1px solid var(--border);
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  font-size: 11px;
  font-weight: 650;
  white-space: nowrap;
}
.metric-control button:last-child { border-right: 0; }
.metric-control button:hover { background: var(--accent-soft); color: var(--accent); }
.metric-control button[aria-pressed="true"] { background: var(--accent); color: #fff; }
.timeline-scroll { overflow-x: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.timeline { width: var(--timeline-width, 980px); min-width: 980px; }
.score-row { display: grid; grid-template-columns: 190px 1fr; min-height: 64px; border-bottom: 1px solid var(--border); background: var(--surface-subtle); }
.score-label { position: sticky; left: 0; z-index: 5; padding: 11px 14px; background: var(--surface-subtle); box-shadow: 1px 0 0 var(--border); }
.score-label strong, .score-label span { display: block; }
.score-label strong { font-size: 11px; }
.score-label span { margin-top: 2px; color: var(--muted); font-size: 9px; }
.score-track { position: relative; min-height: 64px; overflow: hidden; background: var(--surface); }
.score-track svg { display: block; width: 100%; height: 64px; }
.score-reference { stroke: var(--border-strong); stroke-width: 1; stroke-dasharray: 4 4; vector-effect: non-scaling-stroke; }
.score-step { fill: none; stroke: var(--success); stroke-width: 2; vector-effect: non-scaling-stroke; }
.score-point { fill: var(--surface); stroke: var(--success); stroke-width: 2; vector-effect: non-scaling-stroke; }
.score-ref-label { position: absolute; left: 8px; z-index: 2; padding: 0 3px; background: var(--surface); color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 9px; line-height: 12px; white-space: nowrap; }
.timeline-rows { max-height: min(62vh, 680px); overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.timeline-row { display: grid; grid-template-columns: 190px 1fr; min-height: 48px; border-bottom: 1px solid var(--border); }
.timeline-row:first-child { position: sticky; top: 0; z-index: 4; }
.timeline-label { position: sticky; left: 0; z-index: 3; padding: 13px 14px; background: var(--surface-subtle); box-shadow: 1px 0 0 var(--border); color: var(--muted); font-size: 10px; font-weight: 750; text-transform: uppercase; overflow-wrap: anywhere; }
.timeline-label strong, .timeline-label small { display: block; }
.timeline-label small { margin-top: 2px; color: var(--muted); font-size: 9px; font-weight: 600; line-height: 12px; text-transform: none; }
.timeline-row.redispatched .timeline-label { box-shadow: inset 3px 0 0 var(--accent), 1px 0 0 var(--border); }
.timeline-track { position: relative; min-height: 48px; border-left: 1px solid var(--border); background: var(--surface); }
.timeline-track::before, .timeline-track::after { content: ""; position: absolute; inset: 0 auto 0 33.333%; border-left: 1px solid var(--border); }
.timeline-track::after { left: 66.666%; }
.timeline-event {
  position: absolute;
  top: 12px;
  min-width: 8px;
  height: 24px;
  padding: 4px 7px;
  overflow: hidden;
  border-radius: 4px;
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-overflow: ellipsis;
  white-space: nowrap;
  z-index: 2;
}
.timeline-event.main { background: var(--accent); }
.timeline-event.worker { background: var(--worker); }
.timeline-event.parent { background: var(--parent); }
.timeline-event.success { background: var(--success); }
.timeline-event.failure { background: var(--failure); }
.timeline-event.worker-session { display: flex; align-items: center; gap: 5px; border: 1px solid transparent; }
.timeline-event.worker-session.metric-level-1 { background: var(--metric-1); color: #17434b; }
.timeline-event.worker-session.metric-level-2 { background: var(--metric-2); color: #143e46; }
.timeline-event.worker-session.metric-level-3 { background: var(--metric-3); color: #fff; }
.timeline-event.worker-session.metric-level-4 { background: var(--metric-4); color: #fff; }
.timeline-shell[data-metric-mode="status"] .timeline-event.worker-session { background: var(--worker); color: #fff; }
.timeline-event.worker-session.session-failure { border: 2px solid var(--failure); box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.65); }
.session-state-icon { width: 13px; height: 13px; flex: 0 0 auto; color: var(--failure); }
.timeline-shell[data-metric-mode="status"] .session-state-icon { color: #fff; }
.metric-readout { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.retry-badge { display: inline-block; margin-left: 5px; padding: 0 4px; border: 1px solid var(--border-strong); border-radius: 3px; color: var(--accent); font-size: 8px; line-height: 13px; }
.timeline-idle { position: absolute; inset: 0 auto 0 0; z-index: 1; border-right: 1px dashed var(--border-strong); border-left: 1px dashed var(--border-strong); background: #f0f2f4; }
.timeline-idle-label { position: absolute; inset: 50% auto auto 50%; transform: translate(-50%, -50%); color: var(--muted); font-size: 9px; font-weight: 700; white-space: nowrap; }
.timeline-event.point { top: 4px; width: 10px !important; min-width: 10px; height: 10px; padding: 0; border: 2px solid var(--surface); border-radius: 50%; }
.timeline-axis { display: flex; justify-content: space-between; padding: 8px 10px 9px 200px; color: var(--muted); font-size: 10px; }
.timeline-key { display: flex; flex-wrap: wrap; gap: 14px; padding: 12px 20px; border-top: 1px solid var(--border); color: var(--muted); font-size: 11px; }
.key-dot { display: inline-block; width: 9px; height: 9px; margin-right: 5px; border-radius: 50%; background: var(--accent); }
.key-dot.worker { background: var(--worker); }
.key-dot.parent { background: var(--parent); }
.event-log { margin-top: 16px; }
.event-list { margin: 0; padding: 0; list-style: none; }
.event-list li { display: grid; grid-template-columns: 155px 90px 1fr; gap: 12px; padding: 8px 0; border-top: 1px solid var(--border); }
.lane { color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; }
.task-tabs { display: flex; gap: 6px; margin-bottom: 18px; overflow-x: auto; }
.task-tab {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 38px;
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  white-space: nowrap;
}
.task-tab[aria-selected="true"] { color: var(--accent); border-color: var(--accent); box-shadow: inset 0 -2px 0 var(--accent); }
.task-panel { margin-bottom: 26px; }
.js .task-panel[hidden] { display: none; }
.task-head { padding: 20px; border-bottom: 1px solid var(--border); background: var(--surface-subtle); }
.task-title-line { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.task-title-line h3 { margin: 0; font-size: 16px; }
.task-objective { max-width: 960px; margin: 10px 0 0; color: var(--muted); overflow-wrap: anywhere; }
.task-metrics { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 1px; background: var(--border); border-bottom: 1px solid var(--border); }
.task-metric { min-height: 78px; padding: 12px 16px; background: var(--surface); }
.task-metric strong { display: block; margin-top: 4px; font-size: 16px; overflow-wrap: anywhere; }
.subsection { padding: 20px; border-top: 1px solid var(--border); }
.subsection:first-child { border-top: 0; }
details.summary-block > summary { cursor: pointer; list-style: none; }
details.summary-block > summary::-webkit-details-marker { display: none; }
.table-scroll {
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
  contain: inline-size;
  border: 1px solid var(--border);
  border-radius: 6px;
}
table { width: 100%; border-collapse: collapse; background: var(--surface); font-size: 12px; }
.hook-table { min-width: 1040px; }
th { background: var(--surface-subtle); color: var(--muted); font-size: 10px; text-align: left; text-transform: uppercase; }
th, td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; overflow-wrap: anywhere; }
tbody tr:last-child td { border-bottom: 0; }
.selected-row td:first-child { box-shadow: inset 3px 0 0 var(--success); }
.evidence-view-toolbar {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 18px;
  padding: 12px;
  border: 1px solid var(--border);
  border-bottom: 0;
  border-radius: 6px 6px 0 0;
  background: var(--surface-subtle);
}
.evidence-view-summary { display: flex; flex-wrap: wrap; gap: 14px; color: var(--muted); font-size: 11px; }
.evidence-view-summary strong { display: block; color: var(--text); font-size: 15px; }
.evidence-view-filters { display: flex; align-items: end; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
.evidence-view-filter { display: grid; gap: 3px; color: var(--muted); font-size: 10px; font-weight: 700; text-transform: uppercase; }
.evidence-view-filter select, .evidence-view-filter input {
  min-height: 34px;
  padding: 6px 9px;
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text);
  font-size: 12px;
  text-transform: none;
}
.evidence-view-filter input { width: 220px; }
.evidence-view-count { min-width: 82px; padding-bottom: 8px; color: var(--muted); font-size: 11px; text-align: right; }
.evidence-view-scroll { max-height: 680px; border-radius: 0 0 6px 6px; overflow: auto; scrollbar-gutter: stable; }
.evidence-view-table { min-width: 1880px; table-layout: fixed; }
.evidence-view-table thead { position: sticky; top: 0; z-index: 3; }
.evidence-view-table th:nth-child(1) { width: 178px; }
.evidence-view-table th:nth-child(2) { width: 94px; }
.evidence-view-table th:nth-child(3) { width: 74px; }
.evidence-view-table th:nth-child(4) { width: 90px; }
.evidence-view-table th:nth-child(5) { width: 112px; }
.evidence-view-table th:nth-child(6) { width: 250px; }
.evidence-view-table th:nth-child(7) { width: 330px; }
.evidence-view-table th:nth-child(8) { width: 270px; }
.evidence-view-table th:nth-child(9) { width: 190px; }
.evidence-view-table th:nth-child(10) { width: 150px; }
.evidence-view-table tbody tr:hover td { background: var(--accent-soft); }
.evidence-view-table .official-evidence-row td { background: #f2f8fa; }
.evidence-view-table .official-evidence-row:hover td { background: #e2f0f3; }
.evidence-copy { color: var(--text); line-height: 18px; }
.evidence-score-kind { margin-top: 4px; color: var(--accent); font-size: 10px; font-weight: 700; text-transform: uppercase; }
.evidence-view-copy { color: var(--text); font-weight: 600; line-height: 18px; }
.evidence-view-meta { display: flex; align-items: center; gap: 7px; margin-top: 7px; }
.evidence-view-empty { color: var(--muted); font-style: italic; }
.evidence-tool-list { display: grid; gap: 8px; }
.evidence-tool { line-height: 16px; }
.evidence-tool-id { color: var(--accent); font-size: 11px; font-weight: 700; }
.evidence-tool-detail { margin-top: 3px; color: var(--muted); font-size: 11px; }
.evidence-view-error { margin-top: 5px; color: var(--failure); font-size: 11px; line-height: 16px; }
.evidence-view-monitor { margin-top: 5px; color: var(--muted); font-size: 10px; line-height: 15px; }
.revision { display: block; overflow: hidden; color: var(--accent); text-overflow: ellipsis; white-space: nowrap; }
.stats-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.stats-table { border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.stats-table h3 { margin: 0; padding: 11px 12px; border-bottom: 1px solid var(--border); background: var(--surface-subtle); }
.activity-summary { margin-bottom: 32px; }
.stat-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 8px 12px; border-top: 1px solid var(--border); font-size: 11px; }
.stat-row:first-of-type { border-top: 0; }
.stat-row span:first-child { color: var(--muted); overflow-wrap: anywhere; }
.stat-row strong { text-align: right; overflow-wrap: anywhere; }
details.summary-block { margin-top: 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }
details.summary-block > summary { padding: 11px 13px; color: var(--accent); font-weight: 700; }
details.summary-block > div, details.summary-block > pre { margin: 0; padding: 14px; border-top: 1px solid var(--border); }
pre { max-height: 600px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; }
.warning-list { margin: 0; padding-left: 18px; }
.warning-list li + li { margin-top: 6px; }
.footnote { margin: 18px 0 0; color: var(--muted); font-size: 11px; }
@media (max-width: 1100px) {
  .kpi-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .two-column { grid-template-columns: 1fr; }
  .task-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .fact-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .wrap { padding: 0 14px; }
  .masthead-inner { align-items: flex-start; flex-direction: column; padding: 15px 0; }
  .masthead-actions { width: 100%; justify-content: space-between; }
  .generated { text-align: left; }
  h1 { font-size: 24px; line-height: 31px; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .task-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stats-grid { grid-template-columns: 1fr; }
  .event-list li { grid-template-columns: 1fr; gap: 2px; }
  .metric-gap-list li { grid-template-columns: 1fr; gap: 2px; }
  .timeline-head, .metric-lens-toolbar { align-items: flex-start; flex-direction: column; }
  .trajectory-head { align-items: flex-start; flex-direction: column; gap: 2px; }
  .trajectory-plot { min-height: 340px; }
  .evidence-view-toolbar { align-items: stretch; flex-direction: column; }
  .evidence-view-filters { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .evidence-view-filter input { width: 100%; }
  .evidence-view-count { min-width: 0; padding: 7px 0 0; text-align: left; }
  .metric-control { width: 100%; }
  .metric-control button { min-width: 0; flex: 1 1 0; padding-right: 4px; padding-left: 4px; font-size: 10px; }
}
@media (max-width: 480px) {
  .kpi-grid, .task-metrics, .fact-grid { grid-template-columns: 1fr; }
  .kpi { min-height: auto; }
  .masthead-actions { align-items: flex-start; flex-direction: column; }
  .button { width: 100%; }
  .evidence-view-filters { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
@media print {
  @page { margin: 12mm; }
  body { background: #fff; font-size: 11px; }
  .no-print, .section-nav, .task-tabs { display: none !important; }
  .wrap { width: 100%; max-width: none; padding: 0; }
  .masthead-inner { min-height: auto; padding: 0 0 12px; }
  main { padding: 16px 0 0; }
  .report-section { margin-bottom: 22px; padding-bottom: 22px; }
  .js .task-panel[hidden], .task-panel { display: block !important; }
  .panel, .table-scroll, .stats-table, .timeline-shell, .trajectory-shell { break-inside: avoid; }
  details > * { display: block !important; }
  .timeline-scroll, .timeline-rows { max-height: none; overflow: visible; }
  .evidence-view-scroll { max-height: none; overflow: visible; }
  [data-evidence-row][hidden] { display: table-row !important; }
  .timeline { width: 100% !important; min-width: 0; }
  .timeline-row:first-child, .timeline-label { position: static; }
  .metric-lens-toolbar { display: none; }
  .kpi-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .kpi { min-height: 70px; padding: 10px; }
  .metric-value { font-size: 16px; }
}
"""


_REPORT_SCRIPT = """
(function () {
  function reportColor(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function axisRef(prefix, index) {
    return index === 0 ? prefix : prefix + String(index + 1);
  }

  function layoutAxisKey(prefix, index) {
    return prefix + 'axis' + (index === 0 ? '' : String(index + 1));
  }

  function windowSeries(calls, values, details, start, end) {
    var result = {calls: [], values: [], details: []};
    calls.forEach(function (call, index) {
      if (call < start || call > end) return;
      result.calls.push(call);
      result.values.push(values[index]);
      result.details.push(details[index]);
    });
    return result;
  }

  function windowStep(calls, values, start, end) {
    var result = {calls: [], values: []};
    var priorIndex = null;
    calls.forEach(function (call, index) {
      if (call <= start) priorIndex = index;
      if (call >= start && call <= end) {
        result.calls.push(call);
        result.values.push(values[index]);
      }
    });
    if (priorIndex !== null && (result.calls.length === 0 || result.calls[0] > start)) {
      result.calls.unshift(start);
      result.values.unshift(values[priorIndex]);
    }
    return result;
  }

  function failureBand(axisSpec) {
    var range = axisSpec.range || [0, 1];
    var span = Math.max(range[1] - range[0], 0.1);
    if (axisSpec.type === 'log') {
      return {
        low: Math.pow(10, range[0]),
        marker: Math.pow(10, range[0] + span * 0.035),
        high: Math.pow(10, range[0] + span * 0.075)
      };
    }
    return {
      low: range[0],
      marker: range[0] + span * 0.035,
      high: range[0] + span * 0.075
    };
  }

  function renderTrajectory(node) {
    if (!window.Plotly || node.dataset.plotlyRendered === 'true') return;
    var payload;
    try {
      payload = JSON.parse(node.dataset.searchTrajectory || '{}');
    } catch (error) {
      node.textContent = 'Search trajectory data could not be decoded.';
      return;
    }
    var windows = payload.call_window
      ? [payload.call_window]
      : [{
          start: 0,
          end: payload.evaluations,
          tick: Math.max(1, Math.ceil(payload.evaluations / 12)),
          marker_size: 5
        }];
    var callLabel = payload.call_label || 'Verifier call';
    var bestLabel = payload.best_label || 'Global best';
    var pointLabel = payload.point_label || 'Iteration';
    var failureLabel = payload.failure_label || 'verifier failed';
    var failureLegend = payload.failure_legend || 'Failed verifier · not scored';
    var axisSpec = payload.score_axis || {type: 'linear', range: null};
    var palette = [
      reportColor('--accent'),
      reportColor('--worker'),
      reportColor('--parent'),
      reportColor('--success'),
      reportColor('--warning'),
      reportColor('--failure')
    ];
    var symbols = ['circle', 'square', 'diamond', 'triangle-up', 'triangle-down', 'cross'];
    var traces = [];
    var shapes = [];
    var annotations = [];
    var candidateLegendSeen = {};
    var globalLegendSeen = false;
    var selectedLegendSeen = false;
    var failureLegendSeen = false;
    var layout = {
      autosize: true,
      height: windows.length === 1 ? 500 : 170 + windows.length * 230,
      margin: {l: 74, r: 24, t: 92, b: 56},
      paper_bgcolor: reportColor('--surface'),
      plot_bgcolor: reportColor('--surface'),
      font: {family: 'Inter, ui-sans-serif, system-ui, sans-serif', color: reportColor('--text')},
      hoverlabel: {
        bgcolor: reportColor('--surface'),
        bordercolor: reportColor('--border-strong'),
        font: {color: reportColor('--text')}
      },
      hovermode: 'closest',
      legend: {
        orientation: 'h', x: 0, y: 1.04, xanchor: 'left', yanchor: 'bottom',
        font: {color: reportColor('--muted')}
      },
      shapes: shapes,
      annotations: annotations
    };
    var verticalGap = windows.length === 1 ? 0 : 0.065;
    var panelHeight = (1 - verticalGap * (windows.length - 1)) / windows.length;
    var failurePosition = failureBand(axisSpec);

    windows.forEach(function (windowSpec, windowIndex) {
      var xRef = axisRef('x', windowIndex);
      var yRef = axisRef('y', windowIndex);
      var xKey = layoutAxisKey('x', windowIndex);
      var yKey = layoutAxisKey('y', windowIndex);
      var domainTop = 1 - windowIndex * (panelHeight + verticalGap);
      var domainBottom = Math.max(0, domainTop - panelHeight);
      var failureCalls = [];
      var failureValues = [];
      var failureDetails = [];

      layout[xKey] = {
        domain: [0, 1],
        anchor: yRef,
        range: [windowSpec.start - 0.5, windowSpec.end + 0.5],
        dtick: windowSpec.tick,
        title: {text: windowIndex === windows.length - 1 ? callLabel : ''},
        color: reportColor('--muted'),
        gridcolor: reportColor('--border'),
        zerolinecolor: reportColor('--border'),
        automargin: true
      };
      layout[yKey] = {
        domain: [domainBottom, domainTop],
        anchor: xRef,
        type: axisSpec.type || 'linear',
        title: {text: payload.metric_name + (axisSpec.type === 'log' ? ' · log' : '')},
        color: reportColor('--muted'),
        gridcolor: reportColor('--border'),
        zerolinecolor: reportColor('--border'),
        tickformat: '~s',
        automargin: true
      };
      if (axisSpec.type === 'log') layout[yKey].dtick = 'D2';
      if (axisSpec.range) layout[yKey].range = axisSpec.range;

      if (windows.length > 1) {
        annotations.push({
          xref: 'paper', yref: 'paper', x: 0.995, y: domainTop,
          text: 'Calls ' + windowSpec.start + '–' + windowSpec.end,
          showarrow: false, xanchor: 'right', yanchor: 'top', yshift: -4,
          bgcolor: reportColor('--surface'), borderpad: 2,
          font: {size: 10, color: reportColor('--muted')}
        });
      }

      payload.trajectories.forEach(function (trajectory, trajectoryIndex) {
        var color = palette[trajectoryIndex % palette.length];
        var series = windowSeries(
          trajectory.calls, trajectory.scores, trajectory.details,
          windowSpec.start, windowSpec.end
        );
        if (series.calls.length) {
          traces.push({
            type: 'scatter',
            mode: 'lines+markers',
            name: trajectory.candidate_id + (trajectory.selected ? ' · selected' : ''),
            x: series.calls,
            y: series.values,
            customdata: series.details,
            xaxis: xRef,
            yaxis: yRef,
            showlegend: !candidateLegendSeen[trajectory.candidate_id],
            line: {color: color, width: trajectory.selected ? 2.6 : 1.8},
            marker: {
              color: color,
              size: (windowSpec.marker_size || 5) + (trajectory.selected ? 1.5 : 0),
              symbol: symbols[trajectoryIndex % symbols.length],
              line: {color: reportColor('--surface'), width: 0.8}
            },
            hovertemplate:
              '<b>' + trajectory.candidate_id + '</b><br>' +
              callLabel + ' %{x}<br>' +
              payload.metric_name + ' %{y:.4f}<br>' +
              pointLabel + ' %{customdata[0]} · %{customdata[1]}<br>' +
              '%{customdata[2]}<extra></extra>'
          });
          candidateLegendSeen[trajectory.candidate_id] = true;
        }
        (trajectory.failed_calls || []).forEach(function (call, failureIndex) {
          if (call < windowSpec.start || call > windowSpec.end) return;
          failureCalls.push(call);
          failureValues.push(failurePosition.marker);
          failureDetails.push([
            trajectory.candidate_id,
            (trajectory.failed_details[failureIndex] || [])[0],
            (trajectory.failed_details[failureIndex] || [])[1],
            (trajectory.failed_details[failureIndex] || [])[2],
            (trajectory.failed_scores || [])[failureIndex]
          ]);
        });
      });

      var globalSeries = windowStep(
        payload.global_best.calls, payload.global_best.scores,
        windowSpec.start, windowSpec.end
      );
      if (globalSeries.calls.length) {
        traces.push({
          type: 'scatter',
          mode: 'lines',
          name: bestLabel,
          x: globalSeries.calls,
          y: globalSeries.values,
          xaxis: xRef,
          yaxis: yRef,
          showlegend: !globalLegendSeen,
          line: {color: reportColor('--text'), width: 3, shape: 'hv'},
          hovertemplate: callLabel + ' %{x}<br>Best-so-far %{y:.4f}<extra></extra>'
        });
        globalLegendSeen = true;
      }

      if (payload.selected_point && payload.selected_point.call >= windowSpec.start &&
          payload.selected_point.call <= windowSpec.end) {
        traces.push({
          type: 'scatter',
          mode: 'markers',
          name: 'Selected point',
          x: [payload.selected_point.call],
          y: [payload.selected_point.score],
          xaxis: xRef,
          yaxis: yRef,
          showlegend: !selectedLegendSeen,
          marker: {
            color: reportColor('--success'),
            size: 15,
            symbol: 'star',
            line: {color: reportColor('--text'), width: 2}
          },
          hovertemplate:
            '<b>Selected · ' + payload.selected_point.candidate_id + '</b><br>' +
            callLabel + ' %{x}<br>' + payload.metric_name + ' %{y:.4f}<extra></extra>'
        });
        selectedLegendSeen = true;
      }

      if (failureCalls.length) {
        shapes.push({
          type: 'rect', xref: xRef, yref: yRef,
          x0: windowSpec.start, x1: windowSpec.end,
          y0: failurePosition.low, y1: failurePosition.high,
          fillcolor: reportColor('--failure'), opacity: 0.055, line: {width: 0}, layer: 'below'
        });
        traces.push({
          type: 'scatter',
          mode: 'markers',
          name: failureLegend,
          x: failureCalls,
          y: failureValues,
          customdata: failureDetails,
          xaxis: xRef,
          yaxis: yRef,
          showlegend: !failureLegendSeen,
          marker: {
            color: reportColor('--failure'),
            size: (windowSpec.marker_size || 5) + 3,
            symbol: 'x',
            line: {width: 1}
          },
          hovertemplate:
            '<b>%{customdata[0]} · ' + failureLabel + '</b><br>' +
            callLabel + ' %{x}<br>' + pointLabel + ' %{customdata[1]} · %{customdata[2]}<br>' +
            '%{customdata[3]}<br>Raw score %{customdata[4]} · excluded from ranking<extra></extra>'
        });
        failureLegendSeen = true;
      }

      if (Number.isFinite(payload.baseline) && (axisSpec.type !== 'log' || payload.baseline > 0)) {
        shapes.push({
          type: 'line', xref: xRef, yref: yRef,
          x0: windowSpec.start, x1: windowSpec.end,
          y0: payload.baseline, y1: payload.baseline,
          line: {color: reportColor('--muted'), width: 1, dash: 'dot'}
        });
      }
    });

    node.style.minHeight = String(layout.height) + 'px';
    node.style.height = String(layout.height) + 'px';
    node.dataset.plotlyRendered = 'true';
    window.Plotly.newPlot(node, traces, layout, {
      displaylogo: false,
      responsive: true,
      modeBarButtonsToRemove: ['lasso2d', 'select2d'],
      toImageButtonOptions: {format: 'svg', filename: payload.export_name || 'complete-search-trajectory'}
    });
  }

  function renderVisibleTrajectories() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-search-trajectory]'), function (node) {
      if (node.closest('.task-panel[hidden]')) return;
      if (node.dataset.plotlyRendered === 'true') {
        window.Plotly.Plots.resize(node);
      } else {
        renderTrajectory(node);
      }
    });
  }

  var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-task-target]'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('.task-panel'));
  function activate(runId, updateHash) {
    buttons.forEach(function (button) {
      button.setAttribute('aria-selected', button.dataset.taskTarget === runId ? 'true' : 'false');
    });
    panels.forEach(function (panel) { panel.hidden = panel.dataset.runId !== runId; });
    renderVisibleTrajectories();
    if (updateHash && history.replaceState) history.replaceState(null, '', '#task-' + runId);
  }
  buttons.forEach(function (button) {
    button.addEventListener('click', function () { activate(button.dataset.taskTarget, true); });
  });
  if (buttons.length) {
    var requested = location.hash.indexOf('#task-') === 0 ? location.hash.slice(6) : null;
    var initial = buttons.some(function (button) { return button.dataset.taskTarget === requested; })
      ? requested : buttons.find(function (button) { return button.getAttribute('aria-selected') === 'true'; }).dataset.taskTarget;
    activate(initial, false);
  }
  renderVisibleTrajectories();

  window.addEventListener('beforeprint', function () {
    Array.prototype.forEach.call(document.querySelectorAll('[data-search-trajectory]'), renderTrajectory);
  });

  var metricFormatters = {
    'score-gain': function (value) { return (value >= 0 ? '+' : '') + value.toFixed(4); },
    'score-raw': function (value) { return value.toFixed(4); },
    'tokens-per-minute': function (value) { return Math.round(value).toLocaleString() + '/min'; },
    'cost-per-minute': function (value) { return '$' + value.toFixed(4) + '/min'; },
    'verifier-density': function (value) { return value.toFixed(1) + '/min'; }
  };
  Array.prototype.forEach.call(document.querySelectorAll('[data-metric-lens]'), function (lens) {
    var metricButtons = Array.prototype.slice.call(lens.querySelectorAll('[data-metric-mode]'));
    var workerEvents = Array.prototype.slice.call(lens.querySelectorAll('.worker-session'));
    var lowLabel = lens.querySelector('[data-metric-low]');
    var highLabel = lens.querySelector('[data-metric-high]');
    var scaleBar = lens.querySelector('.metric-scale-bar');

    function missingLabel(mode) {
      return mode === 'score-gain' && lens.dataset.scoreGainBaseline === 'false'
        ? 'No baseline' : 'Not observed';
    }

    function setMode(mode) {
      var values = workerEvents.map(function (event) {
        var raw = event.getAttribute('data-metric-' + mode);
        return raw === null || raw === '' ? NaN : Number(raw);
      }).filter(Number.isFinite);
      var low = values.length ? Math.min.apply(Math, values) : null;
      var high = values.length ? Math.max.apply(Math, values) : null;
      lens.dataset.metricMode = mode;
      metricButtons.forEach(function (button) {
        button.setAttribute('aria-pressed', button.dataset.metricMode === mode ? 'true' : 'false');
      });
      workerEvents.forEach(function (event) {
        event.classList.remove('metric-level-1', 'metric-level-2', 'metric-level-3', 'metric-level-4');
        var readout = event.querySelector('.metric-readout');
        if (mode === 'status') {
          if (readout) readout.textContent = event.dataset.terminalState || 'unknown';
          return;
        }
        var raw = event.getAttribute('data-metric-' + mode);
        var value = raw === null || raw === '' ? NaN : Number(raw);
        if (!Number.isFinite(value)) {
          if (readout) readout.textContent = missingLabel(mode);
          return;
        }
        var ratio = high === low ? 0.5 : (value - low) / (high - low);
        var level = Math.min(4, Math.floor(ratio * 4) + 1);
        event.classList.add('metric-level-' + level);
        if (readout) readout.textContent = metricFormatters[mode](value);
      });
      if (mode === 'status') {
        if (lowLabel) lowLabel.textContent = 'Completed';
        if (highLabel) highLabel.textContent = 'Timed out';
        if (scaleBar) scaleBar.hidden = true;
      } else {
        if (lowLabel) lowLabel.textContent = low === null ? missingLabel(mode) : metricFormatters[mode](low);
        if (highLabel) highLabel.textContent = high === null ? missingLabel(mode) : metricFormatters[mode](high);
        if (scaleBar) scaleBar.hidden = false;
      }
    }

    metricButtons.forEach(function (button) {
      button.addEventListener('click', function () { setMode(button.dataset.metricMode); });
    });
    setMode(lens.dataset.metricMode || 'tokens-per-minute');
  });

  Array.prototype.forEach.call(document.querySelectorAll('[data-evidence-view]'), function (view) {
    var rows = Array.prototype.slice.call(view.querySelectorAll('[data-evidence-row]'));
    var count = view.querySelector('[data-evidence-count]');
    var filters = {
      candidate: view.querySelector('[data-evidence-filter="candidate"]'),
      disposition: view.querySelector('[data-evidence-filter="disposition"]'),
      viewState: view.querySelector('[data-evidence-filter="view-state"]'),
      text: view.querySelector('[data-evidence-filter="text"]')
    };

    function applyEvidenceFilters() {
      var query = filters.text ? filters.text.value.trim().toLowerCase() : '';
      var visible = 0;
      rows.forEach(function (row) {
        var matches = (!filters.candidate || !filters.candidate.value || row.dataset.candidate === filters.candidate.value) &&
          (!filters.disposition || !filters.disposition.value || row.dataset.disposition === filters.disposition.value) &&
          (!filters.viewState || !filters.viewState.value || row.dataset.viewState === filters.viewState.value) &&
          (!query || row.textContent.toLowerCase().indexOf(query) !== -1);
        row.hidden = !matches;
        if (matches) visible += 1;
      });
      if (count) count.textContent = visible + ' of ' + rows.length + ' rows';
    }

    [filters.candidate, filters.disposition, filters.viewState].forEach(function (filter) {
      if (filter) filter.addEventListener('change', applyEvidenceFilters);
    });
    if (filters.text) filters.text.addEventListener('input', applyEvidenceFilters);
    applyEvidenceFilters();
  });
})();
"""


def _text(value: Any) -> str:
    if value is None or value == "":
        return "Not observed"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _html(value: Any) -> str:
    return escape(_text(value), quote=True)


def _artifact_identity(reference: Any, git_head: Any = None) -> str | None:
    if isinstance(reference, dict):
        kind = str(reference.get("kind") or "artifact")
        identifier = reference.get("snapshot_id") or reference.get("commit")
        if isinstance(identifier, str) and identifier:
            return f"{kind}:{identifier}"
        return json.dumps(reference, sort_keys=True, separators=(",", ":"))
    if isinstance(git_head, str) and git_head:
        return git_head
    return None


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _timestamp(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _duration(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    seconds = float(value)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _milliseconds(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    formatted = f"{float(value):,.3f}".rstrip("0").rstrip(".")
    return f"{formatted} ms"


def _number(value: Any, *, digits: int = 2) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{float(value):,.{digits}f}".rstrip("0").rstrip(".")


def _percent(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    return f"{float(value) * 100:.1f}%"


def _cost(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    return f"${float(value):,.6f}".rstrip("0").rstrip(".")


def _status_class(value: Any) -> str:
    normalized = str(value or "").lower()
    if normalized in {
        "allow",
        "complete",
        "completed",
        "promoted",
        "passed",
        "valid",
        "keep",
        "evaluated",
        "productive",
        "quiet",
        "success",
        "budget_reached",
        "completed_in_finalization_grace",
        "terminal-allow",
    }:
        return "success"
    if normalized in {
        "block",
        "error",
        "failed",
        "failure",
        "aborted",
        "blocked",
        "timed_out",
        "timeout",
        "polling-only",
        "answer-only",
        "empty-output",
        "cannot-continue",
        "terminal_error",
        "invalid",
    }:
        return "failure"
    if normalized in {
        "active",
        "running",
        "waiting_for_workers",
        "ready_to_promote",
        "planned",
        "started",
        "pending",
        "retry_wait",
        "discard",
        "unavailable",
        "unverified-edit",
        "unverified-output",
        "unverified-tail",
        "unverified-tooling",
        "verified-no-revision",
    }:
        return "warning"
    if normalized == "official":
        return "official"
    return ""


def _status(value: Any) -> str:
    return f'<span class="status {_status_class(value)}">{_html(value)}</span>'


def _finite_float(value: Any) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _per_minute(value: Any, duration_seconds: Any) -> float | None:
    amount = _finite_float(value)
    duration = _finite_float(duration_seconds)
    if amount is None or duration is None or duration <= 0:
        return None
    return amount / duration * 60.0


def _is_better_score(value: float, current: float, direction: str) -> bool:
    if isclose(value, current, rel_tol=1e-9, abs_tol=1e-12):
        return False
    return value < current if direction == "minimize" else value > current


def _session_scores(task: dict[str, Any], direction: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for candidate in task.get("candidates", []):
        for iteration in candidate.get("iterations", []):
            if iteration.get("process_passed") is not True:
                continue
            session_id = iteration.get("agent_session_id")
            score = _finite_float(iteration.get("score"))
            if not isinstance(session_id, str) or score is None:
                continue
            current = scores.get(session_id)
            if current is None or _is_better_score(score, current, direction):
                scores[session_id] = score
    return scores


def _timeline_score_baseline(
    task: dict[str, Any],
) -> tuple[float | None, str | None]:
    scores = (task.get("statistics") or {}).get("scores") or {}
    configured = _finite_float(scores.get("baseline"))
    if configured is not None:
        return configured, "configured"
    return None, None


def _pi_dispatch_usage(usage: Any) -> tuple[float | None, float | None]:
    if not isinstance(usage, dict):
        return None, None
    token_parts = [
        _finite_float(usage.get(field))
        for field in ("input", "output", "cacheRead", "cacheWrite")
    ]
    processed_tokens = (
        sum(value for value in token_parts if value is not None)
        if any(value is not None for value in token_parts)
        else None
    )
    return processed_tokens, _finite_float(usage.get("costTotal"))


def _timeline_performance(
    task: dict[str, Any], timeline: dict[str, Any]
) -> dict[str, Any]:
    start_epoch = _epoch(timeline.get("start_at"))
    duration = _finite_float(timeline.get("duration_seconds"))
    if start_epoch is None or duration is None:
        return {}

    statistics = task.get("statistics") or {}
    scores = statistics.get("scores") or {}
    direction = str(
        scores.get("direction")
        or (task.get("frozen_spec") or {}).get("metric_direction")
        or "maximize"
    )
    baseline, baseline_source = _timeline_score_baseline(task)
    selected = _finite_float(scores.get("selected"))
    checkpoints: list[dict[str, Any]] = []
    for candidate in task.get("candidates", []):
        for iteration in candidate.get("iterations", []):
            if iteration.get("process_passed") is not True:
                continue
            score = _finite_float(iteration.get("score"))
            created_epoch = _epoch(iteration.get("created_at"))
            if score is None or created_epoch is None:
                continue
            checkpoints.append(
                {
                    "at": iteration.get("created_at"),
                    "epoch": created_epoch,
                    "score": score,
                    "candidate_id": candidate.get("candidate_id"),
                    "session_id": iteration.get("agent_session_id"),
                }
            )
    checkpoints.sort(key=lambda item: item["epoch"])
    current = baseline
    best_points: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        score = checkpoint["score"]
        if current is not None and not _is_better_score(score, current, direction):
            continue
        current = score
        best_points.append(
            {key: value for key, value in checkpoint.items() if key != "epoch"}
        )

    worker_events = [
        event
        for event in timeline.get("events", [])
        if event.get("kind") == "worker_session"
    ]
    metric_keys = (
        "score_gain",
        "score_raw",
        "tokens_per_minute",
        "cost_per_minute",
        "verifier_density",
    )
    metric_ranges: dict[str, dict[str, float | int]] = {}
    for key in metric_keys:
        values = [
            value
            for event in worker_events
            if (value := _finite_float(event.get(key))) is not None
        ]
        if values:
            metric_ranges[key] = {
                "min": min(values),
                "max": max(values),
                "observed": len(values),
            }

    spans = sorted(
        (start, end)
        for event in worker_events
        if (start := _epoch(event.get("start_at"))) is not None
        and (end := _epoch(event.get("end_at"))) is not None
        and end > start
    )
    merged: list[list[float]] = []
    for span_start, span_end in spans:
        if not merged or span_start > merged[-1][1]:
            merged.append([span_start, span_end])
        else:
            merged[-1][1] = max(merged[-1][1], span_end)
    idle_threshold = max(60.0, duration * 0.05)
    idle_intervals = [
        {
            "start_at": _timestamp(previous[1]),
            "end_at": _timestamp(following[0]),
            "duration_seconds": following[0] - previous[1],
        }
        for previous, following in zip(merged, merged[1:])
        if following[0] - previous[1] >= idle_threshold
    ]
    return {
        "metric_name": scores.get("metric_name")
        or (task.get("frozen_spec") or {}).get("metric_name"),
        "metric_direction": direction,
        "score": {
            "baseline": baseline,
            "baseline_source": baseline_source,
            "selected": selected,
            "points": best_points,
        },
        "metric_ranges": metric_ranges,
        "idle_intervals": idle_intervals,
        "max_parallel": ((task.get("frozen_spec") or {}).get("budget") or {}).get(
            "max_parallel"
        ),
    }


def _load_models(path: Path, pattern: str, model: Any) -> list[Any]:
    if not path.exists():
        return []
    return [
        model.model_validate(load_json(item)) for item in sorted(path.glob(pattern))
    ]


def _find_goal_record(root: Path, run_id: str) -> GoalPlusRecord | None:
    runtime = FileGoalPlusRuntime(root)
    for path in sorted((root / "goal-plus").glob("*/goal.json")):
        try:
            record = runtime.status(path.parent.name)
        except (OSError, ValueError):
            continue
        if any(task.run_id == run_id for task in record.search_tasks):
            return record
    return None


def _collect_observability(session: AgentSessionRecord) -> dict[str, Any]:
    try:
        return get_agent_host_adapter(session.host).collect_observability(session)
    except Exception as exc:
        return {
            "source": "collection_failed",
            "execution": {
                "terminal_state": "unknown",
                "started_at": session.created_at,
                "ended_at": session.updated_at,
                "duration_seconds": None,
                "timed_out": bool(session.host_handle.metadata.get("timed_out")),
                "runner_failed": bool(
                    session.host_handle.metadata.get("runner_failed")
                ),
            },
            "usage": {},
            "context": {},
            "errors": [f"{type(exc).__name__}: {exc}"],
        }


def _report_iteration_payload(
    run_dir: Path,
    run_id: str,
    candidate_id: str,
    iteration: IterationRecord,
) -> dict[str, Any]:
    annotation_path = (
        run_dir
        / "candidates"
        / candidate_id
        / "evidence-annotations"
        / f"iteration-{iteration.iteration:04d}.json"
    )
    annotation = (
        EvidenceAnnotationTask.model_validate(load_json(annotation_path))
        if annotation_path.exists()
        else None
    )
    if annotation is not None and (
        annotation.run_id != run_id
        or annotation.candidate_id != candidate_id
        or annotation.iteration != iteration.iteration
        or (
            annotation.attempt_ref is not None
            and annotation.attempt_ref != iteration.attempt_ref
        )
        or annotation.attempt_commit != iteration.git_head
    ):
        raise RuntimeError("evidence annotation does not match iteration")

    view = (
        annotation.view
        if annotation is not None and annotation.state == "completed"
        else None
    )
    if view is not None and (
        view.run_id != run_id
        or view.candidate_id != candidate_id
        or view.iteration != iteration.iteration
        or (
            view.attempt_ref is not None
            and view.attempt_ref != iteration.attempt_ref
        )
        or view.attempt_commit != iteration.git_head
    ):
        raise RuntimeError("evidence view does not match iteration")

    annotation_monitor = None
    if annotation is not None and annotation.attempt_history:
        monitor_relative = annotation.attempt_history[-1].get("monitor_path")
        if isinstance(monitor_relative, str) and monitor_relative:
            runtime_root = run_dir.parents[1].resolve()
            monitor_path = (runtime_root / monitor_relative).resolve()
            if not monitor_path.is_relative_to(runtime_root):
                raise RuntimeError("evidence annotation monitor escapes runtime root")
            if monitor_path.exists():
                monitor = load_json(monitor_path)
                if not isinstance(monitor, dict) or (
                    monitor.get("run_id") != run_id
                    or monitor.get("candidate_id") != candidate_id
                    or monitor.get("iteration") != iteration.iteration
                    or monitor.get("attempt") != annotation.attempts
                ):
                    raise RuntimeError("evidence annotation monitor does not match iteration")
                annotation_monitor = monitor

    return {
        "candidate_id": candidate_id,
        "iteration": iteration.iteration,
        "agent_session_id": iteration.agent_session_id,
        "selected_model": iteration.selected_model,
        "exact_model_ref": iteration.exact_model_ref,
        "adapter_version": iteration.adapter_version,
        "score": iteration.score,
        "process_passed": iteration.process_passed,
        "hypothesis": iteration.hypothesis,
        "summary": iteration.summary,
        "failure_class": iteration.failure_class,
        "artifact_ref": (
            iteration.attempt_ref.model_dump(mode="json")
            if iteration.attempt_ref is not None
            else None
        ),
        "git_head": iteration.git_head,
        "disposition": iteration.disposition,
        "restored_to_iteration": iteration.restored_to_iteration,
        "restored_to_git_head": iteration.restored_to_git_head,
        "workspace_git_head_after_settlement": (
            iteration.workspace_git_head_after_settlement
        ),
        "created_at": iteration.created_at,
        "changed_files": iteration.changed_files,
        "view": view.description if view is not None else None,
        "view_state": annotation.state if annotation is not None else "not_requested",
        "view_created_at": view.created_at if view is not None else None,
        "view_error": annotation.last_error if annotation is not None else None,
        "annotation_attempts": annotation.attempts if annotation is not None else 0,
        "annotation_monitor": annotation_monitor,
        "published_tool_views": (
            [tool_view.model_dump(mode="json") for tool_view in view.tool_views]
            if view is not None
            else []
        ),
        "adopted_tools": [
            adopted_tool.model_dump(mode="json")
            for adopted_tool in iteration.adopted_tools
        ],
        "adoption_confounded": iteration.adoption_confounded,
        "toolization_decision": (
            iteration.toolization_decision.model_dump(mode="json")
            if iteration.toolization_decision is not None
            else None
        ),
        "toolization_advisories": list(iteration.toolization_advisories),
        "shared_tool_staged_entries": list(iteration.shared_tool_staged_entries),
        "shared_tool_publish_status": iteration.shared_tool_publish_status,
    }


def _task_details(
    root: Path, task_summary: dict[str, Any], report_run_id: str
) -> dict[str, Any]:
    run_id = task_summary.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {
            **task_summary,
            "is_report_run": False,
            "plans": [],
            "candidates": [],
            "sessions": [],
        }
    run_dir = root / "runs" / run_id
    run = RunRecord.model_validate(load_json(run_dir / "run.json"))
    frozen = FrozenSpec.model_validate(
        load_json(root / "specs" / run.frozen_spec_id / "frozen_spec.json")
    )
    plans = _load_models(run_dir / "plans", "plan_*.json", SearchPlan)
    candidates = _load_models(
        run_dir / "candidates", "*/candidate.json", CandidateRecord
    )
    sessions = _load_models(
        run_dir / "agent_sessions", "agent_*.json", AgentSessionRecord
    )
    observations = {
        session.agent_session_id: _collect_observability(session)
        for session in sessions
    }
    session_ids_by_candidate: dict[str, list[str]] = {}
    for session in sessions:
        session_ids_by_candidate.setdefault(session.candidate_id, []).append(
            session.agent_session_id
        )

    candidate_payloads: list[dict[str, Any]] = []
    for candidate in candidates:
        scored = [
            iteration
            for iteration in candidate.iterations
            if iteration.process_passed is True
            and iteration.score is not None
            and not iteration.touched_denied_files
            and not iteration.changed_outside_allowed
        ]
        best = None
        if scored:
            reverse = frozen.spec.metric_direction == "maximize"
            best = sorted(scored, key=lambda item: item.score, reverse=reverse)[0]
        candidate_payloads.append(
            {
                "candidate_id": candidate.candidate_id,
                "status": candidate.status,
                "plan_id": candidate.task.plan_id,
                "parent_id": candidate.task.parent_id,
                "parent_candidate_ids": candidate.task.parent_candidate_ids,
                "base_candidate_id": candidate.task.base_candidate_id,
                "hypothesis": candidate.task.hypothesis,
                "selected_model": (
                    candidate.task.selected_model.model
                    if candidate.task.selected_model
                    else None
                ),
                "model_provenance": candidate.task.model_provenance,
                "selected": candidate.candidate_id == run.selected_candidate_id,
                "score": (
                    best.score
                    if best is not None
                    else (
                        candidate.score_report.aggregate_score
                        if candidate.score_report is not None
                        and candidate.score_report.process_passed
                        else None
                    )
                ),
                "process_passed": (
                    True
                    if best is not None
                    else (
                        candidate.score_report.process_passed
                        if candidate.score_report is not None
                        else None
                    )
                ),
                "best_iteration": best.iteration if best is not None else None,
                "best_score": best.score if best is not None else None,
                "best_artifact_ref": (
                    best.attempt_ref.model_dump(mode="json")
                    if best is not None and best.attempt_ref is not None
                    else None
                ),
                "settled_artifact_ref": (
                    candidate.settled_artifact_ref.model_dump(mode="json")
                    if candidate.settled_artifact_ref is not None
                    else None
                ),
                "iterations_total": len(candidate.iterations),
                "session_ids": session_ids_by_candidate.get(candidate.candidate_id, []),
                "changed_files": candidate.detected_changed_files,
                "promotion_passed": (
                    candidate.promotion_report.promotion_passed
                    if candidate.promotion_report is not None
                    else None
                ),
                "promotion_evidence_at": (
                    candidate.promotion_evidence.created_at
                    if candidate.promotion_evidence is not None
                    else None
                ),
                "iterations": [
                    _report_iteration_payload(
                        run_dir,
                        run_id,
                        candidate.candidate_id,
                        iteration,
                    )
                    for iteration in candidate.iterations
                ],
            }
        )

    FileSearchRuntime.attach_external_evaluations(
        run_id,
        [
            iteration
            for candidate in candidate_payloads
            for iteration in candidate["iterations"]
        ],
    )

    candidate_iterations_by_id = {
        candidate.candidate_id: list(candidate.iterations) for candidate in candidates
    }
    session_payloads: list[dict[str, Any]] = []
    for session in sessions:
        observation = observations[session.agent_session_id]
        execution = observation.get("execution")
        execution = execution if isinstance(execution, dict) else {}
        usage = observation.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        context = observation.get("context")
        context = context if isinstance(context, dict) else {}
        activity = observation.get("activity")
        activity = activity if isinstance(activity, dict) else None
        session_iterations = [
            iteration
            for candidate in candidates
            for iteration in candidate.iterations
            if iteration.agent_session_id == session.agent_session_id
        ]
        base_payload = {
            "agent_session_id": session.agent_session_id,
            "candidate_id": session.candidate_id,
            "selected_model": (
                session.selected_model.model if session.selected_model else None
            ),
            "model_provenance": session.model_provenance,
            "host": session.host,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "provider": execution.get("provider"),
            "model": execution.get("model"),
            "terminal_state": execution.get("terminal_state"),
            "started_at": execution.get("started_at") or session.created_at,
            "ended_at": execution.get("ended_at"),
            "duration_seconds": execution.get("duration_seconds"),
            "timed_out": bool(execution.get("timed_out")),
            "runner_failed": bool(execution.get("runner_failed")),
            "processed_tokens": usage.get("processed_tokens"),
            "cost_usd": usage.get("cost_usd"),
            "tool_calls": usage.get("tool_calls"),
            "context_tokens": context.get("tokens"),
            "context_percent": context.get("percent"),
            "verifier_runs": len(session_iterations),
            "observability_source": observation.get("source"),
            "activity": activity,
            "errors": observation.get("errors") or [],
        }
        raw_dispatches = session.host_handle.metadata.get("dispatches")
        dispatches = raw_dispatches if isinstance(raw_dispatches, list) else []
        if not dispatches:
            session_payloads.append(
                {
                    **base_payload,
                    "timeline_session_id": session.agent_session_id,
                    "dispatch_index": 1,
                    "dispatch_count": 1,
                    "process_pid": session.host_handle.metadata.get("process_pid"),
                }
            )
            continue

        candidate_iterations = candidate_iterations_by_id.get(session.candidate_id, [])
        for index, raw_dispatch in enumerate(dispatches, start=1):
            dispatch = raw_dispatch if isinstance(raw_dispatch, dict) else {}
            start_at = dispatch.get("started_at")
            end_at = dispatch.get("ended_at")
            start_epoch = _epoch(start_at)
            end_epoch = _epoch(end_at)
            next_start_epoch = (
                _epoch(dispatches[index].get("started_at"))
                if index < len(dispatches) and isinstance(dispatches[index], dict)
                else None
            )
            score_end_epoch = next_start_epoch
            if score_end_epoch is None and end_epoch is not None:
                score_end_epoch = end_epoch + 5.0
            attributed_iterations = []
            for iteration in candidate_iterations:
                if iteration.agent_session_id not in {
                    None,
                    session.agent_session_id,
                }:
                    continue
                iteration_epoch = _epoch(iteration.created_at)
                if iteration_epoch is None:
                    continue
                if start_epoch is not None and iteration_epoch < start_epoch - 1.0:
                    continue
                if score_end_epoch is not None and iteration_epoch > score_end_epoch:
                    continue
                attributed_iterations.append(iteration)
            scored = [
                iteration.score
                for iteration in attributed_iterations
                if iteration.process_passed is True
                and iteration.score is not None
                and not iteration.touched_denied_files
                and not iteration.changed_outside_allowed
            ]
            dispatch_score = None
            if scored:
                dispatch_score = (
                    min(scored)
                    if frozen.spec.metric_direction == "minimize"
                    else max(scored)
                )
            processed_tokens, cost_usd = _pi_dispatch_usage(dispatch.get("usage"))
            timed_out = bool(dispatch.get("timed_out"))
            runner_failed = bool(dispatch.get("runner_failed"))
            terminal_state = (
                "failed" if runner_failed else "timed_out" if timed_out else "completed"
            )
            session_payloads.append(
                {
                    **base_payload,
                    "timeline_session_id": (
                        f"{session.agent_session_id} / dispatch {index}"
                    ),
                    "dispatch_index": index,
                    "dispatch_count": len(dispatches),
                    "process_pid": dispatch.get("process_pid"),
                    "terminal_state": terminal_state,
                    "started_at": start_at or base_payload["started_at"],
                    "ended_at": end_at or base_payload["ended_at"],
                    "duration_seconds": dispatch.get("duration_seconds"),
                    "timed_out": timed_out,
                    "runner_failed": runner_failed,
                    "processed_tokens": processed_tokens,
                    "cost_usd": cost_usd,
                    "tool_calls": None,
                    "verifier_runs": sum(
                        iteration.agent_session_id == session.agent_session_id
                        for iteration in attributed_iterations
                    ),
                    "score": dispatch_score,
                }
            )

    plan_payloads = [
        {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "created_at": plan.created_at,
            "strategy": plan.strategy.name,
            "orchestration_mode": plan.strategy.orchestration_mode,
            "requested_k": plan.requested_k,
            "planned_k": plan.planned_k,
            "remaining_budget": plan.remaining_budget,
            "started_candidate_ids": plan.started_candidate_ids,
            "work_orders_total": len(plan.work_orders),
            "trace": (
                plan.strategy_trace.get("reason")
                or plan.strategy_trace.get("selection_rule")
                or plan.strategy_trace
            ),
        }
        for plan in plans
    ]
    return {
        **task_summary,
        "is_report_run": run_id == report_run_id,
        "run": run.model_dump(mode="json"),
        "strategy": frozen.spec.strategy.model_dump(mode="json", exclude_none=True),
        "frozen_spec": {
            "frozen_spec_id": frozen.frozen_spec_id,
            "spec_hash": frozen.spec_hash,
            "objective": frozen.spec.objective,
            "metric_name": frozen.spec.metric_name,
            "metric_direction": frozen.spec.metric_direction,
            "strategy": frozen.spec.strategy.model_dump(mode="json", exclude_none=True),
            "budget": frozen.spec.budget.model_dump(mode="json", exclude_none=True),
        },
        "plans": plan_payloads,
        "candidates": candidate_payloads,
        "sessions": session_payloads,
    }


_GOAL_EVENT_LABELS = {
    "created": "Goal created",
    "session_activated": "Main-agent session attached",
    "triage_recorded": "Triage recorded",
    "spec_draft_saved": "Search specification drafted",
    "frozen_verifier_confirmed": "Verifier frozen",
    "search_linked": "Search task linked",
    "search_result_recorded": "Search result recorded",
    "goal_updated": "Goal revision created",
    "status_changed": "Goal status changed",
    "final_check_requested": "Final check requested",
    "final_check_submitted": "Final check completed",
}


def _goal_event_label(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "event")
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    base = _GOAL_EVENT_LABELS.get(event_type, event_type.replace("_", " ").title())
    if event_type in {"search_linked", "search_result_recorded"} and payload.get(
        "run_id"
    ):
        return f"{base}: {payload['run_id']}"
    if event_type == "status_changed" and payload.get("status"):
        return f"{base}: {payload['status']}"
    if event_type == "goal_updated" and payload.get("goal_revision"):
        return f"{base}: revision {payload['goal_revision']}"
    return base


def _build_timeline(
    goal: GoalPlusRecord | None,
    goal_events: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    goal_timeline_events: list[dict[str, Any]] = []
    if goal is not None:
        goal_timeline_events.append(
            {
                "lane": "main",
                "kind": "main_span",
                "label": "Goal record activity window",
                "start_at": goal.created_at,
                "end_at": goal.updated_at,
                "inferred_end": False,
                "run_id": None,
            }
        )
    for event in goal_events:
        if event.get("event_type") not in _GOAL_EVENT_LABELS:
            continue
        created_at = event.get("created_at")
        if not isinstance(created_at, str):
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        goal_timeline_events.append(
            {
                "lane": "main",
                "kind": "milestone",
                "label": _goal_event_label(event),
                "start_at": created_at,
                "end_at": None,
                "inferred_end": False,
                "run_id": payload.get("run_id"),
            }
        )
    for task in tasks:
        run_id = task.get("run_id")
        task_events: list[dict[str, Any]] = []
        task_scores = (task.get("statistics") or {}).get("scores") or {}
        metric_direction = str(
            task_scores.get("direction")
            or (task.get("frozen_spec") or {}).get("metric_direction")
            or "maximize"
        )
        baseline_score, _ = _timeline_score_baseline(task)
        session_scores = _session_scores(task, metric_direction)
        sessions_by_candidate: dict[str, list[str]] = {}
        for session in task.get("sessions", []):
            sessions_by_candidate.setdefault(
                str(session.get("candidate_id") or "unknown"), []
            ).append(
                str(
                    session.get("timeline_session_id")
                    or session.get("agent_session_id")
                    or "unknown"
                )
            )
        for session in task.get("sessions", []):
            start_at = session.get("started_at") or session.get("created_at")
            end_at = session.get("ended_at")
            duration_seconds = _finite_float(session.get("duration_seconds"))
            inferred = False
            if end_at is None and duration_seconds is not None:
                start_epoch = _epoch(start_at)
                if start_epoch is not None:
                    end_at = _timestamp(start_epoch + duration_seconds)
                    inferred = True
            if end_at is None:
                end_at = session.get("updated_at")
                inferred = True
            if duration_seconds is None:
                start_epoch = _epoch(start_at)
                end_epoch = _epoch(end_at)
                if (
                    start_epoch is not None
                    and end_epoch is not None
                    and end_epoch >= start_epoch
                ):
                    duration_seconds = end_epoch - start_epoch
            terminal = session.get("terminal_state") or "unknown"
            session_id = str(session["agent_session_id"])
            timeline_session_id = str(session.get("timeline_session_id") or session_id)
            candidate_id = str(session.get("candidate_id") or "unknown")
            candidate_sessions = sessions_by_candidate.get(
                candidate_id, [timeline_session_id]
            )
            attempt_index = candidate_sessions.index(timeline_session_id) + 1
            score = _finite_float(session.get("score"))
            if score is None and "score" not in session:
                score = session_scores.get(session_id)
            score_improvement = None
            if score is not None and baseline_score is not None:
                score_improvement = (
                    baseline_score - score
                    if metric_direction == "minimize"
                    else score - baseline_score
                )
            task_events.append(
                {
                    "lane": "worker",
                    "kind": "worker_session",
                    "label": f"{timeline_session_id} / {terminal}",
                    "start_at": start_at,
                    "end_at": end_at,
                    "inferred_end": inferred,
                    "run_id": run_id,
                    "session_id": session_id,
                    "track_label": timeline_session_id,
                    "dispatch_index": session.get("dispatch_index"),
                    "dispatch_count": session.get("dispatch_count"),
                    "process_pid": session.get("process_pid"),
                    "candidate_id": candidate_id,
                    "terminal_state": terminal,
                    "duration_seconds": duration_seconds,
                    "processed_tokens": session.get("processed_tokens"),
                    "cost_usd": session.get("cost_usd"),
                    "tool_calls": session.get("tool_calls"),
                    "verifier_runs": session.get("verifier_runs"),
                    "tokens_per_minute": _per_minute(
                        session.get("processed_tokens"), duration_seconds
                    ),
                    "cost_per_minute": _per_minute(
                        session.get("cost_usd"), duration_seconds
                    ),
                    "verifier_density": _per_minute(
                        session.get("verifier_runs"), duration_seconds
                    ),
                    "score": score,
                    "score_raw": score,
                    "score_gain": score_improvement,
                    "attempt_index": attempt_index,
                    "attempt_count": len(candidate_sessions),
                }
            )
        for candidate in task.get("candidates", []):
            for iteration in candidate.get("iterations", []):
                parent_owned = iteration.get("agent_session_id") is None
                task_events.append(
                    {
                        "lane": "verifier",
                        "kind": (
                            "parent_verifier" if parent_owned else "worker_verifier"
                        ),
                        "label": (
                            f"Parent verifier {candidate['candidate_id']} #{iteration['iteration']}"
                            if parent_owned
                            else f"Worker verifier {candidate['candidate_id']} #{iteration['iteration']}"
                        ),
                        "start_at": iteration.get("created_at"),
                        "end_at": None,
                        "inferred_end": False,
                        "run_id": run_id,
                        "session_id": iteration.get("agent_session_id"),
                        "score": iteration.get("score"),
                    }
                )
        for candidate in task.get("candidates", []):
            if not candidate.get("promotion_passed"):
                continue
            run = task.get("run") or {}
            promotion_at = (
                candidate.get("promotion_evidence_at")
                or task.get("result_recorded_at")
                or run.get("created_at")
            )
            task_events.append(
                {
                    "lane": "verifier",
                    "kind": "promotion",
                    "label": f"Promotion passed: {candidate['candidate_id']}",
                    "start_at": promotion_at,
                    "end_at": None,
                    "inferred_end": candidate.get("promotion_evidence_at") is None,
                    "run_id": run_id,
                }
            )

        run = task.get("run") or {}
        run_started_at = run.get("created_at")
        task_epochs = [
            value
            for event in task_events
            for value in (_epoch(event.get("start_at")), _epoch(event.get("end_at")))
            if value is not None
        ]
        run_started_epoch = _epoch(run_started_at)
        task_end_epoch = max(task_epochs) if task_epochs else run_started_epoch
        if run_started_epoch is not None and task_end_epoch is not None:
            task_events.insert(
                0,
                {
                    "lane": "main",
                    "kind": "main_span",
                    "label": "Search orchestration",
                    "start_at": run_started_at,
                    "end_at": _timestamp(max(run_started_epoch + 1.0, task_end_epoch)),
                    "inferred_end": False,
                    "run_id": run_id,
                },
            )
        task_timeline = _timeline_payload(task_events)
        task_timeline["performance"] = _timeline_performance(task, task_timeline)
        task["timeline"] = task_timeline

    gate_counts = Counter(
        str(event.get("event_type"))
        for event in goal_events
        if event.get("event_type") in {"gate_allowed", "gate_blocked"}
    )
    return _timeline_payload(
        goal_timeline_events,
        gate_events=dict(sorted(gate_counts.items())),
    )


def _timeline_payload(
    events: list[dict[str, Any]],
    *,
    gate_events: dict[str, int] | None = None,
) -> dict[str, Any]:
    epochs = [
        value
        for event in events
        for value in (_epoch(event.get("start_at")), _epoch(event.get("end_at")))
        if value is not None
    ]
    start_epoch = min(epochs) if epochs else None
    end_epoch = max(epochs) if epochs else None
    duration = (
        max(1.0, end_epoch - start_epoch)
        if start_epoch is not None and end_epoch is not None
        else None
    )
    events.sort(key=lambda event: _epoch(event.get("start_at")) or float("inf"))
    return {
        "start_at": _timestamp(start_epoch),
        "end_at": _timestamp(end_epoch),
        "duration_seconds": duration,
        "events": events,
        "gate_events": gate_events or {},
    }


def _stop_hook_decision_counts() -> dict[str, int]:
    return {decision: 0 for decision in STOP_HOOK_DECISIONS}


def _normalized_stop_hook_event(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    event_name = payload.get("hook_event_name")
    if event_name not in STOP_HOOK_EVENT_NAMES:
        return None
    decision = payload.get("decision")
    if decision not in STOP_HOOK_DECISIONS:
        decision = "unknown"
    duration_ms = payload.get("duration_ms")
    if (
        not isinstance(duration_ms, int | float)
        or isinstance(duration_ms, bool)
        or not isfinite(duration_ms)
        or duration_ms < 0
    ):
        duration_ms = None
    text_fields = (
        "invocation_id",
        "started_at",
        "finished_at",
        "outcome",
        "reason",
        "goal_plus_id",
        "session_id",
        "host_agent_id",
        "agent_session_id",
        "run_id",
        "candidate_id",
        "stop_reason",
        "error_type",
        "error",
    )
    event = {
        key: value if isinstance((value := payload.get(key)), str) and value else None
        for key in text_fields
    }
    event.update(
        {
            "schema_version": payload.get("schema_version"),
            "hook_event_name": event_name,
            "decision": decision,
            "duration_ms": duration_ms,
        }
    )
    return event


def _build_stop_hook_statistics(
    root: Path,
    *,
    goal_plus_id: str | None,
    run_ids: set[str],
) -> dict[str, Any]:
    events = []
    event_dir = root / "host-logs" / "codex-hook-events"
    source_available = event_dir.is_dir()
    for path in sorted(event_dir.glob("*.json")):
        try:
            event = _normalized_stop_hook_event(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if event is None:
            continue
        event_goal_id = event.get("goal_plus_id")
        matches_goal = goal_plus_id is not None and event_goal_id == goal_plus_id
        matches_run = event.get("run_id") in run_ids
        if goal_plus_id is not None:
            included = matches_goal or (event_goal_id is None and matches_run)
        else:
            included = matches_run
        if not included:
            continue
        events.append(event)

    events.sort(
        key=lambda event: (
            _epoch(event.get("started_at")) or float("inf"),
            str(event.get("invocation_id") or ""),
        )
    )
    by_decision = _stop_hook_decision_counts()
    by_event = {
        event_name: {
            "events_total": 0,
            "duration_ms_total": 0.0,
            "decisions": _stop_hook_decision_counts(),
        }
        for event_name in STOP_HOOK_EVENT_NAMES
    }
    subagents: dict[str, dict[str, Any]] = {}
    host_agent_sessions = {
        str(event["host_agent_id"]): str(event["agent_session_id"])
        for event in events
        if event["hook_event_name"] == "SubagentStop"
        and event.get("host_agent_id")
        and event.get("agent_session_id")
    }
    duration_ms_total = 0.0
    for event in events:
        event_name = str(event["hook_event_name"])
        decision = str(event["decision"])
        duration_ms = float(event.get("duration_ms") or 0.0)
        duration_ms_total += duration_ms
        by_decision[decision] += 1
        by_event[event_name]["events_total"] += 1
        by_event[event_name]["duration_ms_total"] += duration_ms
        by_event[event_name]["decisions"][decision] += 1
        if event_name != "SubagentStop":
            continue
        agent_session_id = event.get("agent_session_id")
        host_agent_id = event.get("host_agent_id")
        if not agent_session_id and host_agent_id:
            agent_session_id = host_agent_sessions.get(str(host_agent_id))
        if agent_session_id:
            identity = str(agent_session_id)
            identity_source = "agent_session_id"
        elif host_agent_id:
            identity = str(host_agent_id)
            identity_source = "host_agent_id"
        else:
            identity = "unresolved"
            identity_source = "unresolved"
        key = f"{identity_source}:{identity}"
        summary = subagents.setdefault(
            key,
            {
                "identity": identity,
                "identity_source": identity_source,
                "agent_session_id": agent_session_id,
                "host_agent_ids": set(),
                "run_ids": set(),
                "candidate_ids": set(),
                "events_total": 0,
                "duration_ms_total": 0.0,
                "decisions": _stop_hook_decision_counts(),
                "last_event_at": None,
            },
        )
        for field, target in (
            ("host_agent_id", "host_agent_ids"),
            ("run_id", "run_ids"),
            ("candidate_id", "candidate_ids"),
        ):
            value = event.get(field)
            if isinstance(value, str) and value:
                summary[target].add(value)
        summary["events_total"] += 1
        summary["duration_ms_total"] += duration_ms
        summary["decisions"][decision] += 1
        observed_at = event.get("finished_at") or event.get("started_at")
        if observed_at and (
            summary["last_event_at"] is None
            or (_epoch(observed_at) or float("-inf"))
            > (_epoch(summary["last_event_at"]) or float("-inf"))
        ):
            summary["last_event_at"] = observed_at

    subagent_rows = []
    for summary in subagents.values():
        subagent_rows.append(
            {
                **summary,
                "host_agent_ids": sorted(summary["host_agent_ids"]),
                "run_ids": sorted(summary["run_ids"]),
                "candidate_ids": sorted(summary["candidate_ids"]),
                "duration_ms_total": round(summary["duration_ms_total"], 3),
            }
        )
    subagent_rows.sort(key=lambda item: (item["identity_source"], item["identity"]))
    captured_through = max(
        (
            observed_at
            for event in events
            if (observed_at := event.get("finished_at") or event.get("started_at"))
        ),
        key=lambda value: _epoch(value) or float("-inf"),
        default=None,
    )
    for summary in by_event.values():
        summary["duration_ms_total"] = round(summary["duration_ms_total"], 3)
    return {
        "schema_version": 1,
        "source_available": source_available,
        "source_path": str(event_dir),
        "events_total": len(events),
        "duration_ms_total": round(duration_ms_total, 3),
        "captured_through": captured_through,
        "by_event": by_event,
        "by_decision": by_decision,
        "subagents": subagent_rows,
        "events": events,
    }


def _activity_window_counts(
    events: list[dict[str, Any]],
    *,
    start_epoch: float | None = None,
    end_epoch: float | None = None,
) -> dict[str, Any]:
    tool_counts: Counter[str] = Counter()
    message_counts: Counter[str] = Counter()
    context_samples: list[tuple[float, float]] = []
    for event in events:
        event_epoch = _epoch(event.get("at"))
        if event_epoch is None:
            continue
        if start_epoch is not None and event_epoch < start_epoch:
            continue
        if end_epoch is not None and event_epoch >= end_epoch:
            continue
        kind = event.get("kind")
        if kind == "tool_call":
            tool_counts[str(event.get("category") or "other")] += 1
        elif kind == "assistant_message":
            message_counts[str(event.get("classification") or "substantive")] += 1
            if event.get("duplicate") is True:
                message_counts["duplicate"] += 1
        elif kind == "context":
            percent = _finite_float(event.get("percent"))
            if percent is not None:
                context_samples.append((event_epoch, percent))
    return {
        "tool_calls": sum(tool_counts.values()),
        "context_calls": tool_counts.get("context", 0),
        "global_evidence_calls": tool_counts.get("global_evidence", 0),
        "verifier_tool_calls": tool_counts.get("verifier", 0),
        "iteration_plan_calls": tool_counts.get("iteration_plan", 0),
        "edit_capable_calls": tool_counts.get("edit_capable", 0),
        "other_tool_calls": tool_counts.get("other", 0),
        "assistant_messages": sum(
            count
            for classification, count in message_counts.items()
            if classification != "duplicate"
        ),
        "empty_messages": message_counts.get("empty", 0),
        "best_remains_messages": message_counts.get("best_remains", 0),
        "cannot_continue_messages": message_counts.get("cannot_continue", 0),
        "substantive_messages": message_counts.get("substantive", 0),
        "duplicate_messages": message_counts.get("duplicate", 0),
        "context_samples": len(context_samples),
        "context_percent_final": (
            max(context_samples, key=lambda item: item[0])[1]
            if context_samples
            else None
        ),
        "context_percent_max": (
            max(value for _, value in context_samples) if context_samples else None
        ),
    }


def _nearest_context_percent(
    events: list[dict[str, Any]],
    target_epoch: float | None,
) -> float | None:
    if target_epoch is None:
        return None
    samples = [
        (abs(event_epoch - target_epoch), percent)
        for event in events
        if event.get("kind") == "context"
        and (event_epoch := _epoch(event.get("at"))) is not None
        and (percent := _finite_float(event.get("percent"))) is not None
    ]
    return min(samples, default=(0.0, None), key=lambda item: item[0])[1]


def _loop_tail_signal(
    *,
    activity_available: bool,
    last_verifier_at: str | None,
    post_last: dict[str, Any],
) -> str:
    if not activity_available:
        return "unavailable"
    if last_verifier_at is None:
        return "no-verifier"
    messages = int(post_last.get("assistant_messages") or 0)
    empty = int(post_last.get("empty_messages") or 0)
    best_remains = int(post_last.get("best_remains_messages") or 0)
    cannot_continue = int(post_last.get("cannot_continue_messages") or 0)
    context_reads = int(post_last.get("context_calls") or 0)
    evidence_reads = int(post_last.get("global_evidence_calls") or 0)
    edits = int(post_last.get("edit_capable_calls") or 0)
    tool_calls = int(post_last.get("tool_calls") or 0)
    if messages >= 5 and empty * 5 >= messages * 4:
        return "empty-output"
    if context_reads + evidence_reads >= 10 and edits == 0:
        return "polling-only"
    if (
        messages >= 3
        and best_remains >= 2
        and best_remains * 2 >= messages
        and edits == 0
        and tool_calls <= 1
    ):
        return "answer-only"
    if cannot_continue > 0 and edits == 0:
        return "cannot-continue"
    if edits > 0:
        return "unverified-edit"
    if messages > 0 or tool_calls > 0:
        return "unverified-tail"
    return "quiet"


def _build_loop_agent_statistics(
    tasks: list[dict[str, Any]],
    stop_hook_statistics: dict[str, Any],
) -> dict[str, Any]:
    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    candidate_sessions: dict[tuple[str, str], set[str]] = {}
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    task_by_run: dict[str, dict[str, Any]] = {}
    for task in tasks:
        run_id = task.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        task_by_run[run_id] = task
        for candidate in task.get("candidates") or []:
            candidate_id = candidate.get("candidate_id")
            if isinstance(candidate_id, str) and candidate_id:
                candidates[(run_id, candidate_id)] = candidate
        for session in task.get("sessions") or []:
            if session.get("host") != "codex":
                continue
            agent_session_id = session.get("agent_session_id")
            candidate_id = session.get("candidate_id")
            if not isinstance(agent_session_id, str) or not agent_session_id:
                continue
            if not isinstance(candidate_id, str) or not candidate_id:
                continue
            sessions.setdefault((run_id, agent_session_id), session)
            candidate_sessions.setdefault((run_id, candidate_id), set()).add(
                agent_session_id
            )

    host_agent_sessions: dict[str, str] = {}
    for summary in stop_hook_statistics.get("subagents") or []:
        agent_session_id = summary.get("agent_session_id")
        if not isinstance(agent_session_id, str) or not agent_session_id:
            continue
        for host_agent_id in summary.get("host_agent_ids") or []:
            if isinstance(host_agent_id, str) and host_agent_id:
                host_agent_sessions[host_agent_id] = agent_session_id

    events_by_session: dict[tuple[str, str], list[dict[str, Any]]] = {}
    subagent_hook_streams: set[str] = set()
    for raw_event in stop_hook_statistics.get("events") or []:
        if raw_event.get("hook_event_name") != "SubagentStop":
            continue
        event = dict(raw_event)
        run_id = event.get("run_id")
        candidate_id = event.get("candidate_id")
        if isinstance(run_id, str) and run_id:
            subagent_hook_streams.add(run_id)
        agent_session_id = event.get("agent_session_id")
        host_agent_id = event.get("host_agent_id")
        if not isinstance(agent_session_id, str) and isinstance(host_agent_id, str):
            agent_session_id = host_agent_sessions.get(host_agent_id)
        if (
            not isinstance(agent_session_id, str)
            and isinstance(run_id, str)
            and isinstance(candidate_id, str)
        ):
            candidates_for_event = candidate_sessions.get((run_id, candidate_id), set())
            if len(candidates_for_event) == 1:
                agent_session_id = next(iter(candidates_for_event))
        if not isinstance(run_id, str) or not isinstance(agent_session_id, str):
            continue
        event["resolved_agent_session_id"] = agent_session_id
        events_by_session.setdefault((run_id, agent_session_id), []).append(event)
    for events in events_by_session.values():
        events.sort(
            key=lambda event: (
                _epoch(event.get("started_at")) or float("inf"),
                str(event.get("invocation_id") or ""),
            )
        )

    rows: list[dict[str, Any]] = []
    stop_windows: list[dict[str, Any]] = []
    for (run_id, agent_session_id), session in sorted(sessions.items()):
        task = task_by_run[run_id]
        candidate_id = str(session.get("candidate_id"))
        candidate = candidates.get((run_id, candidate_id)) or {}
        activity = session.get("activity")
        activity = activity if isinstance(activity, dict) else {}
        activity_available = activity.get("available") is True
        activity_events = activity.get("events")
        activity_events = (
            [event for event in activity_events if isinstance(event, dict)]
            if isinstance(activity_events, list)
            else []
        )
        direction = str(
            (task.get("frozen_spec") or {}).get("metric_direction") or "maximize"
        )
        candidate_session_ids = candidate_sessions.get((run_id, candidate_id), set())
        candidate_iterations = candidate.get("iterations") or []
        has_session_attribution = any(
            isinstance(iteration, dict)
            and isinstance(iteration.get("agent_session_id"), str)
            for iteration in candidate_iterations
        )
        iteration_rows = []
        current_best: float | None = None
        previous_git_head: str | None = None
        for iteration in sorted(
            candidate_iterations,
            key=lambda item: _epoch(item.get("created_at")) or float("inf"),
        ):
            iteration_session_id = iteration.get("agent_session_id")
            belongs_to_session = iteration_session_id == agent_session_id or (
                not has_session_attribution
                and iteration_session_id is None
                and len(candidate_session_ids) == 1
            )
            if not belongs_to_session:
                continue
            item = dict(iteration)
            git_head = item.get("git_head")
            item["_revision_changed"] = bool(
                isinstance(git_head, str) and git_head and git_head != previous_git_head
            )
            if isinstance(git_head, str) and git_head:
                previous_git_head = git_head
            score = _finite_float(item.get("score"))
            improved = False
            if item.get("process_passed") is True and score is not None:
                improved = current_best is None or _is_better_score(
                    score, current_best, direction
                )
                if improved:
                    current_best = score
            item["_improved"] = improved
            iteration_rows.append(item)

        last_verifier = max(
            (
                iteration.get("created_at")
                for iteration in iteration_rows
                if _epoch(iteration.get("created_at")) is not None
            ),
            key=lambda value: _epoch(value) or float("-inf"),
            default=None,
        )
        last_verifier_epoch = _epoch(last_verifier)
        total_activity = _activity_window_counts(activity_events)
        post_last = _activity_window_counts(
            activity_events,
            start_epoch=last_verifier_epoch,
        )
        session_events = events_by_session.get((run_id, agent_session_id), [])
        blocked_events = [
            event for event in session_events if event.get("decision") == "block"
        ]
        for index, event in enumerate(session_events):
            start_at = event.get("finished_at") or event.get("started_at")
            start_epoch = _epoch(start_at)
            next_event = (
                session_events[index + 1] if index + 1 < len(session_events) else None
            )
            end_at = next_event.get("started_at") if next_event is not None else None
            end_epoch = _epoch(end_at)
            window_activity = _activity_window_counts(
                activity_events,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
            )
            window_iterations = [
                iteration
                for iteration in iteration_rows
                if (iteration_epoch := _epoch(iteration.get("created_at"))) is not None
                and (start_epoch is None or iteration_epoch >= start_epoch)
                and (end_epoch is None or iteration_epoch < end_epoch)
            ]
            revision_changes = sum(
                iteration.get("_revision_changed") is True
                for iteration in window_iterations
            )
            improvements = sum(
                iteration.get("_improved") is True for iteration in window_iterations
            )
            if event.get("decision") != "block":
                outcome = "terminal-allow"
            elif window_iterations and revision_changes:
                outcome = "productive"
            elif window_iterations:
                outcome = "verified-no-revision"
            elif (
                window_activity["context_calls"]
                + window_activity["global_evidence_calls"]
            ):
                outcome = "polling-only"
            elif window_activity["assistant_messages"]:
                if (
                    window_activity["empty_messages"] * 5
                    >= window_activity["assistant_messages"] * 4
                ):
                    outcome = "empty-output"
                elif (
                    window_activity["best_remains_messages"]
                    or window_activity["cannot_continue_messages"]
                    or window_activity["duplicate_messages"]
                ):
                    outcome = "answer-only"
                else:
                    outcome = "unverified-output"
            elif window_activity["tool_calls"]:
                outcome = "unverified-tooling"
            else:
                outcome = "no-observed-followup"
            stop_windows.append(
                {
                    "run_id": run_id,
                    "candidate_id": candidate_id,
                    "agent_session_id": agent_session_id,
                    "stop_index": index + 1,
                    "started_at": event.get("started_at"),
                    "decision": event.get("decision"),
                    "verifier_runs": len(window_iterations),
                    "verified_revision_changes": revision_changes,
                    "improvements": improvements,
                    **window_activity,
                    "outcome": outcome,
                }
            )

        blocked_windows = [
            window
            for window in stop_windows
            if window["run_id"] == run_id
            and window["agent_session_id"] == agent_session_id
            and window["decision"] == "block"
        ]
        first_block_epoch = (
            _epoch(blocked_events[0].get("started_at")) if blocked_events else None
        )
        context_at_first_block = _nearest_context_percent(
            activity_events, first_block_epoch
        )
        context_final = total_activity.get("context_percent_final")
        context_growth = (
            context_final - context_at_first_block
            if context_final is not None and context_at_first_block is not None
            else None
        )
        hook_stream_available = run_id in subagent_hook_streams
        decision_counts = Counter(
            str(event.get("decision") or "unknown") for event in session_events
        )
        rows.append(
            {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "agent_session_id": agent_session_id,
                "selected": candidate.get("selected") is True,
                "best_score": candidate.get("best_score"),
                "hook_stream_available": hook_stream_available,
                "stop_calls": len(session_events) if hook_stream_available else None,
                "blocked_stops": (
                    decision_counts.get("block", 0) if hook_stream_available else None
                ),
                "allowed_stops": (
                    decision_counts.get("allow", 0) if hook_stream_available else None
                ),
                "productive_blocked_stops": (
                    sum(window["outcome"] == "productive" for window in blocked_windows)
                    if hook_stream_available
                    else None
                ),
                "verifier_runs_after_blocked_stops": (
                    sum(window["verifier_runs"] for window in blocked_windows)
                    if hook_stream_available
                    else None
                ),
                "verified_revision_changes_after_blocked_stops": (
                    sum(
                        window["verified_revision_changes"]
                        for window in blocked_windows
                    )
                    if hook_stream_available
                    else None
                ),
                "improvements_after_blocked_stops": (
                    sum(window["improvements"] for window in blocked_windows)
                    if hook_stream_available
                    else None
                ),
                "activity_available": activity_available,
                "activity": total_activity,
                "last_verifier_at": last_verifier,
                "post_last_verifier": post_last,
                "context_percent_at_first_block_nearest": context_at_first_block,
                "context_percent_final": context_final,
                "context_percent_max": total_activity.get("context_percent_max"),
                "context_growth_after_first_block": context_growth,
                "tail_signal": _loop_tail_signal(
                    activity_available=activity_available,
                    last_verifier_at=last_verifier,
                    post_last=post_last,
                ),
            }
        )

    return {
        "schema_version": 1,
        "rows": rows,
        "stop_windows": stop_windows,
        "definitions": {
            "productive_stop": (
                "A blocked SubagentStop followed before the next stop by a "
                "verifier on a different immutable Git revision."
            ),
            "polling_only": (
                "At least ten context/global-evidence reads after the last "
                "verifier, with no edit-capable call."
            ),
            "answer_only": (
                "At least three post-verifier assistant messages, at least "
                "half classified as short best-remains replies, and at most "
                "one tool call."
            ),
            "empty_output": (
                "At least five post-verifier assistant messages, at least "
                "80 percent empty."
            ),
        },
    }


def build_html_report_data(root_dir: Path | str, run_id: str) -> dict[str, Any]:
    root = Path(root_dir).resolve()
    goal = _find_goal_record(root, run_id)
    goal_id = goal.goal_plus_id if goal is not None else None
    snapshot = goal_plus_monitor_snapshot(
        root,
        goal_plus_id=goal_id,
        run_id=run_id,
    )
    task_summaries = snapshot.get("search_tasks")
    task_summaries = task_summaries if isinstance(task_summaries, list) else []
    tasks = [
        _task_details(root, summary, run_id)
        for summary in task_summaries
        if isinstance(summary, dict) and summary.get("run_exists")
    ]
    goal_runtime = FileGoalPlusRuntime(root)
    goal_events = goal_runtime.list_events(goal_id) if goal_id is not None else []
    timeline = _build_timeline(goal, goal_events, tasks)
    stop_hook_statistics = _build_stop_hook_statistics(
        root,
        goal_plus_id=goal_id,
        run_ids={
            str(task["run_id"])
            for task in tasks
            if isinstance(task.get("run_id"), str) and task.get("run_id")
        }
        | {run_id},
    )
    loop_agent_statistics = _build_loop_agent_statistics(
        tasks,
        stop_hook_statistics,
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": snapshot.get("snapshot_at"),
        "goal_plus_id": goal_id,
        "report_run_id": run_id,
        "snapshot": snapshot,
        "search_tasks": tasks,
        "timeline": timeline,
        "stop_hook_statistics": stop_hook_statistics,
        "loop_agent_statistics": loop_agent_statistics,
    }


def _metric_card(label: str, value: str, detail: str = "", tone: str = "") -> str:
    return (
        '<div class="kpi">'
        f'<div class="kpi-label">{escape(label)}</div>'
        f'<div class="metric-value {tone}">{escape(value)}</div>'
        f'<div class="kpi-detail">{escape(detail)}</div>'
        "</div>"
    )


_METRIC_GAP_INFO = {
    "target_score": (
        "Not configured",
        "No score threshold was declared. This does not mean the Goal Plus task failed.",
    ),
    "baseline_score": (
        "Not configured",
        "No baseline score was supplied for improvement analysis.",
    ),
    "orchestrator_cost_usd": (
        "Not collected",
        "The attached main-agent evidence does not expose a reliable orchestrator cost.",
    ),
    "orchestrator_token_usage": (
        "Not collected",
        "No readable main-agent transcript usage record was attached.",
    ),
    "orchestrator_usage_breakdown": (
        "Not collected",
        "Detailed main-agent input, cache, output, and tool usage are not available.",
    ),
    "hardware_utilization": (
        "Not collected",
        "Worker host CPU, GPU, memory, and accelerator telemetry are outside the current evidence contract.",
    ),
    "worker_time_to_first_token": (
        "Not collected",
        "The worker host did not publish time-to-first-token for every session.",
    ),
    "worker_processed_tokens": (
        "Partial coverage",
        "Processed-token usage is missing for one or more worker sessions.",
    ),
    "worker_cost_usd": (
        "Partial coverage",
        "A model-rate cost estimate is missing for one or more worker sessions.",
    ),
    "worker_duration": (
        "Partial coverage",
        "Observed duration is missing for one or more worker sessions.",
    ),
    "semantic_candidate_coverage": (
        "Not computed",
        "Candidate semantic diversity is not currently scored.",
    ),
    "redundant_attempt_rate": (
        "Not computed",
        "The report does not classify semantically duplicate attempts.",
    ),
    "temporal_collision_rate": (
        "Not computed",
        "The report does not classify concurrent workers as colliding or duplicating work.",
    ),
    "research_rollup_quality": (
        "Not computed",
        "No verifier-backed quality metric exists for the final research synthesis.",
    ),
    "normalized_score": (
        "Not computed",
        "The verifier score is reported in its native scale; no cross-task normalization is declared.",
    ),
    "promotion_attempt_history": (
        "Not retained",
        "The durable report has final promotion evidence, not a complete history of every attempted promotion.",
    ),
}


def _render_metric_availability(items: list[Any]) -> str:
    names = list(dict.fromkeys(str(item) for item in items if item))
    if not names:
        return "<p>No known metric availability gaps.</p>"
    rows = []
    for name in names:
        kind, reason = _METRIC_GAP_INFO.get(
            name,
            (
                "Not observed",
                "This value was not present in the durable report evidence.",
            ),
        )
        rows.append(
            "<li>"
            f"<code>{escape(name)}</code>"
            f'<span class="metric-gap-kind">{escape(kind)}</span>'
            f'<span class="metric-gap-reason">{escape(reason)}</span>'
            "</li>"
        )
    return (
        '<details class="summary-block">'
        f"<summary>Metric availability ({len(names)} gaps)</summary>"
        f'<div><ul class="metric-gap-list">{"".join(rows)}</ul></div>'
        "</details>"
    )


def _stat_rows(values: dict[str, Any], formatters: dict[str, Any] | None = None) -> str:
    formatters = formatters or {}
    rows = []
    for key, value in values.items():
        formatter = formatters.get(key, _text)
        rows.append(
            '<div class="stat-row">'
            f'<span>{escape(key.replace("_", " ").title())}</span>'
            f'<strong class="mono">{escape(formatter(value))}</strong>'
            "</div>"
        )
    return "".join(rows)


def _timeline_position(
    event: dict[str, Any], start_epoch: float, duration: float
) -> tuple[float, float]:
    event_start = _epoch(event.get("start_at")) or start_epoch
    event_end = _epoch(event.get("end_at"))
    left = max(0.0, min(99.0, (event_start - start_epoch) / duration * 100))
    if event_end is None:
        return left, 0.8
    width = max(1.0, (event_end - event_start) / duration * 100)
    return left, min(width, 100.0 - left)


def _timeline_width(duration_seconds: float) -> int:
    duration_minutes = max(0.0, duration_seconds / 60.0)
    return max(980, min(20_000, int(round(190 + duration_minutes * 80))))


def _metric_level(value: Any, metric_range: dict[str, Any]) -> int | None:
    number = _finite_float(value)
    low = _finite_float(metric_range.get("min"))
    high = _finite_float(metric_range.get("max"))
    if number is None or low is None or high is None:
        return None
    ratio = 0.5 if high == low else (number - low) / (high - low)
    return min(4, max(1, int(ratio * 4) + 1))


def _metric_readout(metric: str, value: Any) -> str:
    number = _finite_float(value)
    if number is None:
        return "Not observed"
    if metric == "score_gain":
        return f"{number:+.4f}".rstrip("0").rstrip(".")
    if metric == "score_raw":
        return _number(number, digits=4)
    if metric == "tokens_per_minute":
        return f"{_number(number, digits=0)}/min"
    if metric == "cost_per_minute":
        return f"${number:.4f}/min"
    if metric == "verifier_density":
        return f"{number:.1f}/min"
    return _number(number)


def _render_score_chart(
    performance: dict[str, Any],
    start_epoch: float,
    duration: float,
) -> str:
    score_data = performance.get("score") or {}
    baseline = _finite_float(score_data.get("baseline"))
    selected = _finite_float(score_data.get("selected"))
    points = [
        point
        for point in score_data.get("points", [])
        if _finite_float(point.get("score")) is not None
        and _epoch(point.get("at")) is not None
    ]
    values = [value for value in (baseline, selected) if value is not None]
    values.extend(float(point["score"]) for point in points)
    if not values:
        return ""

    low = min(values)
    high = max(values)
    padding = max((high - low) * 0.15, abs(high) * 0.02, 0.01)
    plot_low = low - padding
    plot_high = high + padding

    def y_position(value: float) -> float:
        return 54.0 - (value - plot_low) / (plot_high - plot_low) * 44.0

    current = baseline if baseline is not None else float(points[0]["score"])
    path_parts = [f"M 0 {y_position(current):.2f}"]
    point_marks = []
    for point in points:
        point_epoch = _epoch(point.get("at"))
        score = float(point["score"])
        if point_epoch is None:
            continue
        x = max(0.0, min(1000.0, (point_epoch - start_epoch) / duration * 1000.0))
        y = y_position(score)
        path_parts.extend((f"H {x:.2f}", f"V {y:.2f}"))
        tooltip = (
            f"{point.get('candidate_id') or 'candidate'}: {_number(score, digits=4)} "
            f"at {point.get('at')}"
        )
        point_marks.append(
            f'<circle class="score-point" cx="{x:.2f}" cy="{y:.2f}" r="3" '
            f"<title>{escape(tooltip)}</title></circle>"
        )
    path_parts.append("H 1000")
    reference_lines = []
    reference_labels = []
    for label, value in (("Baseline", baseline), ("Selected", selected)):
        if value is None:
            continue
        y = y_position(value)
        reference_lines.append(
            f'<line class="score-reference" x1="0" y1="{y:.2f}" x2="1000" y2="{y:.2f}" />'
        )
        label_top = min(51.0, max(1.0, y - 11.0))
        reference_labels.append(
            f'<span class="score-ref-label" style="top:{label_top:.2f}px">'
            f"{escape(label)} {_html(_number(value, digits=4))}</span>"
        )
    metric_name = str(performance.get("metric_name") or "score")
    baseline_summary = (
        _number(baseline, digits=4) if baseline is not None else "No baseline"
    )
    summary = f"{baseline_summary} to {_number(selected, digits=4)}"
    return (
        '<div class="score-row">'
        '<div class="score-label">'
        "<strong>Best score</strong>"
        f"<span>{escape(metric_name)} / {escape(summary)}</span>"
        "</div>"
        f'<div class="score-track" role="img" aria-label="Best score progression: {escape(summary, quote=True)}">'
        '<svg viewBox="0 0 1000 64" preserveAspectRatio="none" aria-hidden="true">'
        f'{"".join(reference_lines)}'
        f'<path class="score-step" d="{" ".join(path_parts)}" />'
        f'{"".join(point_marks)}'
        f'</svg>{"".join(reference_labels)}</div></div>'
    )


@lru_cache(maxsize=1)
def _load_plotly_javascript() -> str | None:
    try:
        from plotly.offline import get_plotlyjs
    except ImportError:
        return None
    return str(get_plotlyjs())


def _nice_trajectory_tick(raw_step: float) -> int:
    if raw_step <= 1:
        return 1
    magnitude = 10 ** floor(log10(raw_step))
    fraction = raw_step / magnitude
    nice_fraction = (
        1 if fraction <= 1 else 2 if fraction <= 2 else 5 if fraction <= 5 else 10
    )
    return max(1, int(nice_fraction * magnitude))


def _trajectory_call_window(evaluations: int) -> dict[str, int]:
    if evaluations <= 0:
        return {"start": 0, "end": 0, "tick": 1, "marker_size": 7}
    marker_size = 7 if evaluations <= 80 else 5 if evaluations <= 250 else 4
    return {
        "start": 0,
        "end": evaluations,
        "tick": _nice_trajectory_tick(max(1.0, evaluations / 12)),
        "marker_size": marker_size,
    }


def _trajectory_score_axis(scores: list[float]) -> dict[str, Any]:
    if not scores:
        return {
            "type": "linear",
            "range": None,
            "minimum": None,
            "maximum": None,
            "ratio": None,
        }
    minimum = min(scores)
    maximum = max(scores)
    ratio = maximum / minimum if minimum > 0 else None
    use_log = ratio is not None and ratio >= 20
    if use_log:
        low = log10(minimum)
        high = log10(maximum)
        span = max(high - low, 0.1)
        padding = max(0.06, span * 0.08)
        axis_range = [low - padding, high + padding]
    else:
        span = maximum - minimum
        padding = max(span * 0.08, abs(maximum) * 0.04, 1e-9)
        axis_range = [minimum - padding, maximum + padding]
    return {
        "type": "log" if use_log else "linear",
        "range": axis_range,
        "minimum": minimum,
        "maximum": maximum,
        "ratio": ratio,
    }


def _search_trajectory_payload(task: dict[str, Any]) -> dict[str, Any] | None:
    statistics = task.get("statistics") or {}
    scores = statistics.get("scores") or {}
    frozen = task.get("frozen_spec") or {}
    metric_name = str(scores.get("metric_name") or frozen.get("metric_name") or "score")
    metric_name = metric_name.replace("<", "").replace(">", "")
    direction = str(
        scores.get("direction") or frozen.get("metric_direction") or "maximize"
    )
    baseline = _finite_float(scores.get("baseline"))
    selected_score = _finite_float(scores.get("selected"))
    evaluations: list[dict[str, Any]] = []
    candidates = task.get("candidates") or []
    for candidate_index, candidate in enumerate(candidates):
        candidate_id = str(
            candidate.get("candidate_id") or f"candidate-{candidate_index + 1}"
        )
        for iteration_index, iteration in enumerate(candidate.get("iterations") or []):
            score = _finite_float(iteration.get("score"))
            if score is None:
                continue
            created_at = iteration.get("created_at")
            created_epoch = _epoch(created_at)
            session_id = iteration.get("agent_session_id")
            evaluations.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_index": candidate_index,
                    "selected": bool(candidate.get("selected")),
                    "iteration": iteration.get("iteration") or iteration_index + 1,
                    "score": score,
                    "process_passed": iteration.get("process_passed") is True,
                    "created_at": created_at,
                    "created_epoch": created_epoch,
                    "source": (
                        "worker verifier"
                        if session_id is not None
                        else "parent verifier"
                    ),
                    "fallback_order": iteration_index,
                }
            )
    if not evaluations:
        return None

    evaluations.sort(
        key=lambda item: (
            item["created_epoch"] is None,
            item["created_epoch"] if item["created_epoch"] is not None else 0.0,
            item["candidate_index"],
            item["fallback_order"],
        )
    )
    for call, evaluation in enumerate(evaluations, start=1):
        evaluation["call"] = call

    trajectories: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(candidates):
        candidate_id = str(
            candidate.get("candidate_id") or f"candidate-{candidate_index + 1}"
        )
        points = [
            evaluation
            for evaluation in evaluations
            if evaluation["candidate_id"] == candidate_id
        ]
        if not points:
            continue
        passing_points = [point for point in points if point["process_passed"]]
        failed_points = [point for point in points if not point["process_passed"]]
        trajectories.append(
            {
                "candidate_id": candidate_id,
                "selected": bool(candidate.get("selected")),
                "calls": [point["call"] for point in passing_points],
                "scores": [point["score"] for point in passing_points],
                "details": [
                    [
                        point["iteration"],
                        point["source"],
                        str(point.get("created_at") or "timestamp unavailable"),
                    ]
                    for point in passing_points
                ],
                "failed_calls": [point["call"] for point in failed_points],
                "failed_scores": [point["score"] for point in failed_points],
                "failed_details": [
                    [
                        point["iteration"],
                        point["source"],
                        str(point.get("created_at") or "timestamp unavailable"),
                    ]
                    for point in failed_points
                ],
            }
        )

    global_calls: list[int] = [0] if baseline is not None else []
    global_scores: list[float] = [baseline] if baseline is not None else []
    current = baseline
    for evaluation in evaluations:
        score = float(evaluation["score"])
        if evaluation["process_passed"] and (
            current is None or _is_better_score(score, current, direction)
        ):
            current = score
        if current is not None:
            global_calls.append(int(evaluation["call"]))
            global_scores.append(float(current))

    selected_evaluations = [
        item for item in evaluations if item["selected"] and item["process_passed"]
    ]
    selected_point = None
    if selected_evaluations:
        if selected_score is not None:
            selected_evaluation = min(
                selected_evaluations,
                key=lambda item: (
                    abs(float(item["score"]) - selected_score),
                    -int(item["call"]),
                ),
            )
        else:
            selected_evaluation = sorted(
                selected_evaluations,
                key=lambda item: float(item["score"]),
                reverse=direction == "maximize",
            )[0]
        selected_point = {
            "candidate_id": selected_evaluation["candidate_id"],
            "call": selected_evaluation["call"],
            "score": selected_evaluation["score"],
        }

    passing_scores = [
        float(evaluation["score"])
        for evaluation in evaluations
        if evaluation["process_passed"]
    ]
    if baseline is not None:
        passing_scores.append(float(baseline))
    passing_count = sum(1 for evaluation in evaluations if evaluation["process_passed"])
    return {
        "metric_name": metric_name,
        "metric_direction": direction,
        "baseline": baseline,
        "selected": selected_score,
        "evaluations": len(evaluations),
        "passing_evaluations": passing_count,
        "failed_evaluations": len(evaluations) - passing_count,
        "call_window": _trajectory_call_window(len(evaluations)),
        "score_axis": _trajectory_score_axis(passing_scores),
        "trajectories": trajectories,
        "global_best": {"calls": global_calls, "scores": global_scores},
        "selected_point": selected_point,
    }


def _render_search_trajectory(payload: dict[str, Any]) -> str:
    encoded = escape(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        quote=True,
    )
    evaluations = int(payload.get("evaluations") or 0)
    passing = int(payload.get("passing_evaluations") or 0)
    failed = int(payload.get("failed_evaluations") or 0)
    trajectories = len(payload.get("trajectories") or [])
    axis_type = str((payload.get("score_axis") or {}).get("type") or "linear")
    metric_name = str(payload.get("metric_name") or "score")
    title = str(payload.get("title") or "Complete Search Trajectory")
    unit_label = str(payload.get("unit_label") or "calls")
    group_label = str(payload.get("group_label") or "loops")
    aria_subject = str(payload.get("aria_subject") or "search trajectory")
    aria_label = (
        f"{aria_subject} with {evaluations} {unit_label} across "
        f"{trajectories} {group_label} for {metric_name}."
    )
    return (
        '<div class="trajectory-shell">'
        f'<div class="trajectory-head"><h3>{escape(title)}</h3>'
        f"<span>{evaluations} {escape(unit_label)} / {trajectories} {escape(group_label)} · "
        f"{passing} scored / {failed} failed · "
        f"{axis_type} score axis</span></div>"
        f'<div class="trajectory-plot" role="img" aria-label="{escape(aria_label, quote=True)}" '
        f'data-search-trajectory="{encoded}"></div></div>'
    )


def _render_metric_toolbar(performance: dict[str, Any], default_metric: str) -> str:
    metric_ranges = performance.get("metric_ranges") or {}
    selected_range = metric_ranges.get(default_metric) or {}
    score_baseline = _finite_float((performance.get("score") or {}).get("baseline"))
    no_score_baseline = default_metric == "score_gain" and score_baseline is None
    low = (
        "No baseline"
        if no_score_baseline
        else _metric_readout(default_metric, selected_range.get("min"))
    )
    high = (
        "No baseline"
        if no_score_baseline
        else _metric_readout(default_metric, selected_range.get("max"))
    )
    options = (
        ("score-gain", "Score gain"),
        ("score-raw", "Score raw"),
        ("tokens-per-minute", "Tokens/min"),
        ("cost-per-minute", "Cost/min"),
        ("verifier-density", "Verifier/min"),
    )
    buttons = "".join(
        f'<button type="button" data-metric-mode="{key}" aria-pressed="{str(key == default_metric.replace("_", "-")).lower()}">{label}</button>'
        for key, label in options
    )
    return (
        '<div class="metric-lens-toolbar no-print">'
        '<div class="metric-scale" aria-label="Selected metric range">'
        f"<span data-metric-low>{escape(low)}</span>"
        '<span class="metric-scale-bar" aria-hidden="true"><i></i><i></i><i></i><i></i></span>'
        f"<span data-metric-high>{escape(high)}</span>"
        "</div>"
        f'<div class="metric-control" role="group" aria-label="Worker session color metric">{buttons}</div>'
        "</div>"
    )


_SESSION_ALERT_ICON = (
    '<svg class="session-state-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>'
)


def _render_timeline(
    timeline: dict[str, Any],
    *,
    title: str,
    span_label: str = "Observed span",
    include_score_chart: bool = True,
) -> str:
    events = timeline.get("events") or []
    start_epoch = _epoch(timeline.get("start_at"))
    duration = timeline.get("duration_seconds")
    if start_epoch is None or not isinstance(duration, int | float):
        return '<div class="panel panel-body">No durable timeline timestamps were observed.</div>'

    main_events = [event for event in events if event.get("lane") == "main"]
    verifier_events = [event for event in events if event.get("lane") == "verifier"]
    worker_events = [event for event in events if event.get("lane") == "worker"]
    performance = timeline.get("performance") or {}
    metric_ranges = performance.get("metric_ranges") or {}
    score_gain_has_baseline = (
        _finite_float((performance.get("score") or {}).get("baseline")) is not None
    )
    default_metric = (
        "score_gain"
        if ("score_gain" in metric_ranges or "score_raw" in metric_ranges)
        else (
            "tokens_per_minute"
            if "tokens_per_minute" in metric_ranges
            else next(iter(metric_ranges), "status")
        )
    )
    tracks: list[tuple[str, str, list[dict[str, Any]]]] = [
        ("Main Agent", "main", main_events),
    ]
    for event in worker_events:
        label = str(
            event.get("track_label") or event.get("session_id") or "Worker session"
        )
        tracks.append((label, "worker", [event]))
    if verifier_events:
        tracks.append(("Verifier activity", "parent", verifier_events))
    timeline_width = _timeline_width(float(duration))
    idle_intervals = performance.get("idle_intervals") or []

    rows = []
    worker_track_index = 0
    for label, lane_class, track_events in tracks:
        marks = []
        if lane_class == "worker":
            for idle in idle_intervals:
                left, width = _timeline_position(idle, start_epoch, float(duration))
                idle_label = ""
                if worker_track_index == 0:
                    idle_label = f'<span class="timeline-idle-label">Idle {_html(_duration(idle.get("duration_seconds")))}</span>'
                marks.append(
                    f'<span class="timeline-idle" style="left:{left:.3f}%;width:{width:.3f}%;" '
                    f'title="No active worker sessions for {escape(_duration(idle.get("duration_seconds")), quote=True)}">'
                    f"{idle_label}</span>"
                )
        for event in track_events:
            left, width = _timeline_position(event, start_epoch, float(duration))
            point = event.get("end_at") is None
            kind = event.get("kind")
            css_class = lane_class
            if kind == "worker_verifier":
                css_class = "worker"
            elif kind == "promotion":
                css_class = "success"
            elif kind != "worker_session" and str(event.get("terminal_state")) in {
                "timed_out",
                "failed",
            }:
                css_class = "failure"
            tooltip = str(event.get("label") or "event")
            if event.get("inferred_end"):
                tooltip += " (end inferred)"
            style = f"left:{left:.3f}%;width:{width:.3f}%;"
            event_attributes = ""
            label_html = "" if point else escape(str(event.get("label") or ""))
            if kind == "worker_session":
                terminal_state = str(event.get("terminal_state") or "unknown")
                failed = terminal_state in {"timed_out", "failed"}
                level = _metric_level(
                    event.get(default_metric), metric_ranges.get(default_metric) or {}
                )
                css_class = "worker worker-session"
                if level is not None:
                    css_class += f" metric-level-{level}"
                if failed:
                    css_class += " session-failure"
                metric_attributes = []
                for metric_name in (
                    "score_gain",
                    "score_raw",
                    "tokens_per_minute",
                    "cost_per_minute",
                    "verifier_density",
                ):
                    metric_value = _finite_float(event.get(metric_name))
                    if metric_value is not None:
                        metric_attributes.append(
                            f'data-metric-{metric_name.replace("_", "-")}="{metric_value:.9f}"'
                        )
                event_attributes = (
                    f'data-terminal-state="{escape(terminal_state, quote=True)}" '
                    + " ".join(metric_attributes)
                    + " "
                )
                metric_value = event.get(default_metric)
                default_readout = (
                    "No baseline"
                    if default_metric == "score_gain" and not score_gain_has_baseline
                    else _metric_readout(default_metric, metric_value)
                )
                score_gain_readout = (
                    "No baseline"
                    if not score_gain_has_baseline
                    else _metric_readout("score_gain", event.get("score_gain"))
                )
                label_html = (
                    _SESSION_ALERT_ICON if failed else ""
                ) + f'<span class="metric-readout">{escape(default_readout)}</span>'
                details = [
                    f"candidate {event.get('candidate_id')}",
                    f"duration {_duration(event.get('duration_seconds'))}",
                    f"score gain {score_gain_readout}",
                    f"score raw {_metric_readout('score_raw', event.get('score_raw'))}",
                    f"tokens/min {_metric_readout('tokens_per_minute', event.get('tokens_per_minute'))}",
                    f"cost/min {_metric_readout('cost_per_minute', event.get('cost_per_minute'))}",
                    f"verifier density {_metric_readout('verifier_density', event.get('verifier_density'))}",
                ]
                if _finite_float(event.get("score")) is not None:
                    details.append(f"score {_number(event.get('score'), digits=4)}")
                tooltip += " | " + " | ".join(details)
            marks.append(
                f'<span class="timeline-event {css_class}{" point" if point else ""}" {event_attributes}'
                f'style="{style}" title="{escape(tooltip, quote=True)}">{label_html}</span>'
            )
        label_html = escape(label)
        row_class = "timeline-row"
        if lane_class == "worker" and track_events:
            event = track_events[0]
            session_id = str(event.get("session_id") or label)
            suffix = session_id.rsplit("_", 1)[-1]
            candidate_id = str(event.get("candidate_id") or "unknown")
            attempt_index = int(event.get("attempt_index") or 1)
            attempt_count = int(event.get("attempt_count") or 1)
            score = _finite_float(event.get("score"))
            retry = (
                f'<span class="retry-badge">retry {attempt_index}/{attempt_count}</span>'
                if attempt_count > 1
                else ""
            )
            score_text = (
                f" / score {_number(score, digits=4)}" if score is not None else ""
            )
            label_html = (
                f'<strong title="{escape(session_id, quote=True)}">agent_{escape(suffix)}{retry}</strong>'
                f"<small>{escape(candidate_id)}{escape(score_text)}</small>"
            )
            if attempt_count > 1:
                row_class += " redispatched"
            worker_track_index += 1
        rows.append(
            f'<div class="{row_class}">'
            f'<div class="timeline-label">{label_html}</div>'
            f'<div class="timeline-track">{"".join(marks)}</div>'
            "</div>"
        )
    end_epoch = start_epoch + float(duration)
    event_items = []
    for event in events:
        event_items.append(
            "<li>"
            f'<time class="timeline-time">{_html(event.get("start_at"))}</time>'
            f'<span class="lane">{_html(event.get("lane"))}</span>'
            f'<span>{_html(event.get("label"))}'
            f'{" <em>(end inferred)</em>" if event.get("inferred_end") else ""}</span>'
            "</li>"
        )
    metric_lens = bool(worker_events and metric_ranges)
    score_chart = (
        _render_score_chart(performance, start_epoch, float(duration))
        if worker_events and include_score_chart
        else ""
    )
    toolbar = _render_metric_toolbar(performance, default_metric) if metric_lens else ""
    default_mode = default_metric.replace("_", "-") if metric_lens else "status"
    return (
        f'<div class="panel timeline-shell" data-metric-mode="{default_mode}"'
        f' data-score-gain-baseline="{str(score_gain_has_baseline).lower()}"'
        f'{" data-metric-lens" if metric_lens else ""}>'
        '<div class="timeline-head">'
        f"<h2>{escape(title)}</h2>"
        f'<span class="mono">{escape(span_label)}: {escape(_duration(duration))}</span>'
        "</div>"
        f"{toolbar}"
        f'<div class="timeline-scroll" tabindex="0" aria-label="{escape(title, quote=True)} scroll area">'
        f'<div class="timeline" style="--timeline-width:{timeline_width}px">'
        f"{score_chart}"
        f'<div class="timeline-rows" data-track-count="{len(tracks)}">{"".join(rows)}</div>'
        '<div class="timeline-axis">'
        f'<span>{escape(_timestamp(start_epoch) or "")}</span>'
        f"<span>+{escape(_duration(float(duration) / 2))}</span>"
        f'<span>{escape(_timestamp(end_epoch) or "")}</span>'
        "</div></div></div>"
        '<div class="timeline-key">'
        '<span><i class="key-dot"></i>Main agent</span>'
        '<span><i class="key-dot worker"></i>Worker session / worker verifier</span>'
        '<span><i class="key-dot parent"></i>Parent verifier</span>'
        f'{"<span>Fill intensity = selected metric</span><span>Red outline = timed out / failed</span><span>Retry n/N = same candidate redispatch</span>" if metric_lens else ""}'
        "</div></div>"
        '<details class="summary-block event-log"><summary>Chronological event evidence</summary>'
        f'<div><ul class="event-list">{"".join(event_items)}</ul></div></details>'
    )


def _render_shared_evidence_view(task: dict[str, Any]) -> str:
    local_rows = [
        {
            **iteration,
            "candidate_id": candidate.get("candidate_id"),
            "candidate_selected": bool(candidate.get("selected")),
        }
        for candidate in task.get("candidates") or []
        for iteration in candidate.get("iterations") or []
        if iteration.get("agent_session_id")
    ]
    if not local_rows:
        return "<p>No worker Evidence was persisted for this Search task.</p>"

    official_rows = []
    for item in local_rows:
        for evaluation in item.get("external_evaluations") or []:
            if not isinstance(evaluation, dict):
                continue
            is_official = (
                evaluation.get("authority") == "edgebench_official_hidden_judge"
                or evaluation.get("source") == "edgebench"
            )
            score_100 = _finite_float(evaluation.get("score_0_100"))
            valid = evaluation.get("valid")
            evidence_source = "official" if is_official else "external"
            official_rows.append(
                {
                    **item,
                    "created_at": evaluation.get("published_at")
                    or item.get("created_at"),
                    "score": (
                        score_100
                        if score_100 is not None
                        else evaluation.get("score")
                    ),
                    "score_kind": (
                        f"{evidence_source} / 100"
                        if score_100 is not None
                        else f"{evidence_source} raw score"
                    ),
                    "disposition": (
                        "valid"
                        if valid is True
                        else "invalid"
                        if valid is False
                        else evaluation.get("status") or evidence_source
                    ),
                    "hypothesis": (
                        "Official Judge evaluation"
                        if is_official
                        else "External evaluation"
                    ),
                    "view": evaluation.get("summary")
                    or evaluation.get("error")
                    or f"{evidence_source.title()} evaluation {evaluation.get('status') or 'recorded'}.",
                    "view_state": evidence_source,
                    "view_error": None,
                    "annotation_monitor": None,
                    "evidence_source": evidence_source,
                }
            )

    rows_payload = [*local_rows, *official_rows]
    rows_payload.sort(
        key=lambda item: (
            _epoch(item.get("created_at")) or float("inf"),
            str(item.get("candidate_id") or ""),
            int(item.get("iteration") or 0),
        )
    )
    candidates = sorted(
        {
            str(item.get("candidate_id"))
            for item in rows_payload
            if item.get("candidate_id")
        }
    )
    dispositions = sorted(
        {str(item.get("disposition") or "unsettled") for item in rows_payload}
    )
    view_states = sorted(
        {str(item.get("view_state") or "not_requested") for item in rows_payload}
    )
    published = sum(bool(item.get("view")) for item in local_rows)
    state_counts = Counter(
        str(item.get("view_state") or "not_requested") for item in local_rows
    )

    def options(values: list[str]) -> str:
        return "".join(
            f'<option value="{escape(value, quote=True)}">{escape(value.replace("_", " "))}</option>'
            for value in values
        )

    def published_tool_views(item: dict[str, Any]) -> str:
        if item.get("evidence_source"):
            return '<div class="evidence-view-empty">Not applicable</div>'
        views = item.get("published_tool_views")
        if not isinstance(views, list) or not views:
            return '<div class="evidence-view-empty">None published</div>'
        rendered = []
        for tool_view in views:
            if not isinstance(tool_view, dict):
                continue
            tool_id = tool_view.get("tool_id")
            summary = tool_view.get("summary")
            if not isinstance(tool_id, str) or not isinstance(summary, str):
                continue
            detail = []
            entrypoint = tool_view.get("entrypoint")
            if isinstance(entrypoint, str) and entrypoint:
                detail.append(f"Entrypoint: {entrypoint}")
            when_to_use = tool_view.get("when_to_use")
            if isinstance(when_to_use, str) and when_to_use:
                detail.append(f"Use: {when_to_use}")
            rendered.append(
                '<div class="evidence-tool">'
                f'<div class="evidence-tool-id">{_html(tool_id)}</div>'
                f'<div>{_html(summary)}</div>'
                + (
                    f'<div class="evidence-tool-detail">{_html(" | ".join(detail))}</div>'
                    if detail
                    else ""
                )
                + "</div>"
            )
        return (
            f'<div class="evidence-tool-list">{"".join(rendered)}</div>'
            if rendered
            else '<div class="evidence-view-empty">None published</div>'
        )

    def tool_adoption_summary(item: dict[str, Any]) -> str:
        if item.get("evidence_source"):
            return '<div class="evidence-view-empty">Not applicable</div>'
        adoptions = item.get("adopted_tools")
        if not isinstance(adoptions, list) or not adoptions:
            return '<div class="evidence-view-empty">No shared tool adopted</div>'
        rendered = []
        for adoption in adoptions:
            if not isinstance(adoption, dict):
                continue
            tool_id = adoption.get("tool_id")
            snapshot_hash = adoption.get("snapshot_hash")
            if not isinstance(tool_id, str) or not isinstance(snapshot_hash, str):
                continue
            receipt_id = adoption.get("receipt_id")
            detail = f"snapshot {snapshot_hash[:12]}"
            if isinstance(receipt_id, str) and receipt_id:
                detail += f" | receipt {receipt_id}"
            rendered.append(
                '<div class="evidence-tool">'
                f'<div class="evidence-tool-id">{_html(tool_id)}</div>'
                f'<div class="evidence-tool-detail">{_html(detail)}</div>'
                "</div>"
            )
        if not rendered:
            return '<div class="evidence-view-empty">No shared tool adopted</div>'
        qualifier = (
            '<div class="evidence-tool-detail">Confounded adoption trial</div>'
            if item.get("adoption_confounded") is True
            else '<div class="evidence-tool-detail">Isolated adoption trial</div>'
        )
        return f'<div class="evidence-tool-list">{"".join(rendered)}{qualifier}</div>'

    def toolization_review(item: dict[str, Any]) -> str:
        if item.get("evidence_source"):
            return '<div class="evidence-view-empty">Not applicable</div>'
        decision = item.get("toolization_decision")
        advisories = item.get("toolization_advisories") or []
        staged_entries = item.get("shared_tool_staged_entries") or []
        publish_status = item.get("shared_tool_publish_status")
        if not isinstance(decision, dict):
            decision_copy = '<div class="evidence-view-empty">Review missing</div>'
        else:
            outcome = decision.get("outcome")
            signals = decision.get("signals") or []
            exclusion = decision.get("exclusion")
            tool_names = decision.get("tool_names") or []
            details = []
            if signals:
                details.append("Signals: " + ", ".join(str(item) for item in signals))
            if exclusion:
                details.append(f"Exclusion: {exclusion}")
            if tool_names:
                details.append("Tools: " + ", ".join(str(item) for item in tool_names))
            decision_copy = (
                f'<div class="evidence-tool-id">{_html(outcome)}</div>'
                f'<div>{_html(decision.get("rationale"))}</div>'
                + (
                    f'<div class="evidence-tool-detail">{_html(" | ".join(details))}</div>'
                    if details
                    else ""
                )
            )
        settlement = [
            f"Staged: {', '.join(str(item) for item in staged_entries) or 'none'}",
            f"Publish: {publish_status or 'unknown'}",
        ]
        if advisories:
            settlement.append(
                "Advisory: " + ", ".join(str(item) for item in advisories)
            )
        return (
            '<div class="evidence-tool">'
            f"{decision_copy}"
            f'<div class="evidence-tool-detail">{_html(" | ".join(settlement))}</div>'
            "</div>"
        )

    rows = []
    for item in rows_payload:
        candidate_id = str(item.get("candidate_id") or "")
        disposition = str(item.get("disposition") or "unsettled")
        view_state = str(item.get("view_state") or "not_requested")
        revision = _artifact_identity(item.get("artifact_ref"), item.get("git_head"))
        evidence_source = item.get("evidence_source")
        view = item.get("view")
        tool_view_copy = published_tool_views(item)
        toolization_copy = toolization_review(item)
        adoption_copy = tool_adoption_summary(item)
        view_copy = (
            f'<div class="evidence-view-copy">{_html(view)}</div>'
            if view
            else '<div class="evidence-view-empty">Not published</div>'
        )
        view_error = (
            f'<div class="evidence-view-error">{_html(item.get("view_error"))}</div>'
            if item.get("view_error")
            else ""
        )
        monitor = item.get("annotation_monitor")
        monitor_copy = ""
        if isinstance(monitor, dict):
            last_events = monitor.get("last_events")
            last_event = (
                last_events[-1].get("type")
                if isinstance(last_events, list)
                and last_events
                and isinstance(last_events[-1], dict)
                else None
            )
            monitor_parts = [
                f"Annotator {monitor.get('state') or 'unknown'}",
                _duration(_finite_float(monitor.get("elapsed_seconds"))),
                f"{_number(monitor.get('json_lines'))} JSON events",
                f"last {last_event}" if last_event else None,
                (
                    f"stdout {_number(monitor.get('stdout_bytes'))} B"
                    if monitor.get("stdout_bytes") is not None
                    else None
                ),
            ]
            monitor_copy = (
                '<div class="evidence-view-monitor">'
                + _html(" | ".join(str(part) for part in monitor_parts if part))
                + "</div>"
            )
        attempt = item.get("hypothesis") or item.get("summary")
        row_classes = []
        if item.get("candidate_selected"):
            row_classes.append("selected-row")
        if evidence_source:
            row_classes.append("official-evidence-row")
        score = _number(item.get("score"), digits=3 if evidence_source else 2)
        score_copy = _html(score)
        if item.get("score_kind"):
            score_copy = (
                f"<strong>{_html(score)}</strong>"
                f'<div class="evidence-score-kind">{_html(item.get("score_kind"))}</div>'
            )
        rows.append(
            f'<tr class="{escape(" ".join(row_classes), quote=True)}"'
            " data-evidence-row"
            f' data-candidate="{escape(candidate_id, quote=True)}"'
            f' data-disposition="{escape(disposition, quote=True)}"'
            f' data-view-state="{escape(view_state, quote=True)}">'
            f'<td class="mono">{_html(item.get("created_at"))}</td>'
            f'<td class="mono"><strong>{_html(candidate_id)}</strong></td>'
            f'<td class="mono">{_html(item.get("iteration"))}</td>'
            f'<td class="mono">{score_copy}</td>'
            f"<td>{_status(disposition)}</td>"
            f'<td><div class="evidence-copy">{_html(attempt)}</div></td>'
            f'<td>{view_copy}{view_error}{monitor_copy}<div class="evidence-view-meta">{_status(view_state)}</div></td>'
            f"<td>{toolization_copy}</td>"
            f"<td>{tool_view_copy}</td>"
            f"<td>{adoption_copy}</td>"
            f'<td><code class="revision" title="{escape(revision or "", quote=True)}">'
            f"{_html(revision[:28] if revision else None)}</code></td>"
            "</tr>"
        )

    return (
        "<div data-evidence-view>"
        '<div class="evidence-view-toolbar no-print">'
        '<div class="evidence-view-summary">'
        f"<span><strong>{_html(len(local_rows))}</strong>Settled iterations</span>"
        f"<span><strong>{_html(published)}</strong>Views published</span>"
        f"<span><strong>{_html(len(official_rows))}</strong>Official evaluations</span>"
        f'<span><strong>{_html(state_counts.get("pending", 0))}</strong>Pending</span>'
        f'<span><strong>{_html(state_counts.get("terminal_error", 0))}</strong>Terminal errors</span>'
        "</div>"
        '<div class="evidence-view-filters">'
        '<label class="evidence-view-filter"><span>Candidate</span>'
        f'<select data-evidence-filter="candidate"><option value="">All</option>{options(candidates)}</select></label>'
        '<label class="evidence-view-filter"><span>Settlement</span>'
        f'<select data-evidence-filter="disposition"><option value="">All</option>{options(dispositions)}</select></label>'
        '<label class="evidence-view-filter"><span>View state</span>'
        f'<select data-evidence-filter="view-state"><option value="">All</option>{options(view_states)}</select></label>'
        '<label class="evidence-view-filter"><span>Text</span>'
        '<input type="search" data-evidence-filter="text" aria-label="Filter Evidence text"></label>'
        f'<div class="evidence-view-count" data-evidence-count aria-live="polite">{len(rows_payload)} of {len(rows_payload)} rows</div>'
        "</div></div>"
        '<div class="table-scroll evidence-view-scroll">'
        '<table class="evidence-view-table"><thead><tr>'
        "<th>Time</th><th>Candidate</th><th>Iteration</th><th>Score</th>"
        "<th>Settlement</th><th>Worker attempt</th><th>Objective View</th>"
        "<th>Toolization Review</th><th>Published Tool View</th>"
        "<th>Tool Adoption Summary</th><th>Revision</th>"
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        '<p class="footnote">Worker attempt is the candidate-authored settled hypothesis. '
        "Objective View is the immutable best-effort annotation bound to the exact candidate, "
        "iteration, and revision; missing Views are not inferred from worker text. Published "
        "Toolization Review compares the worker decision with the authoritative staging and "
        "publication facts; its advisories do not affect scoring or settlement. Published "
        "Tool Views appear only after that annotation binds them to the published snapshot. Tool "
        "adoption summarizes verifier-consumed copy receipts and whether the trial was confounded. Official "
        "evaluation rows are external Judge observations bound to that same exact identity; "
        "they do not change local settlement or ranking.</p>"
        "</div>"
    )


def _render_candidates(task: dict[str, Any]) -> str:
    candidates = task.get("candidates") or []
    if not candidates:
        return "<p>No candidates were persisted.</p>"
    rows = []
    for candidate in candidates:
        artifact = _artifact_identity(
            candidate.get("best_artifact_ref")
            or candidate.get("settled_artifact_ref"),
            None,
        )
        rows.append(
            f'<tr class="{"selected-row" if candidate.get("selected") else ""}">'
            f'<td class="mono"><strong>{_html(candidate.get("candidate_id"))}</strong></td>'
            f'<td>{_status("selected" if candidate.get("selected") else candidate.get("status"))}</td>'
            f'<td class="mono">{_html(_number(candidate.get("score")))}</td>'
            f'<td class="mono">{_html(_number(candidate.get("best_score")))}</td>'
            f'<td><code class="revision" title="{escape(artifact or "", quote=True)}">'
            f"{_html(artifact[:28] if artifact else None)}</code></td>"
            f'<td>{_html(candidate.get("process_passed"))}</td>'
            f'<td class="mono">{_html(candidate.get("iterations_total"))}</td>'
            f'<td class="mono">{_html(", ".join(candidate.get("session_ids") or []) or None)}</td>'
            f'<td>{_html(", ".join(candidate.get("changed_files") or []) or None)}</td>'
            f'<td>{_html(candidate.get("hypothesis"))}</td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Candidate</th><th>Status</th><th>Final score</th><th>Best score</th>"
        "<th>Best artifact</th><th>Process pass</th><th>Iterations</th><th>Sessions</th><th>Changed files</th><th>Hypothesis</th>"
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def _render_sessions(task: dict[str, Any]) -> str:
    sessions = task.get("sessions") or []
    if not sessions:
        return "<p>No worker sessions were persisted.</p>"
    rows = []
    for session in sessions:
        terminal = session.get("terminal_state") or (
            "timed_out" if session.get("timed_out") else "unknown"
        )
        dispatch_label = "{} / {}".format(
            session.get("dispatch_index"), session.get("dispatch_count")
        )
        rows.append(
            "<tr>"
            f'<td class="mono"><strong>{_html(session.get("agent_session_id"))}</strong></td>'
            f'<td class="mono">{_html(dispatch_label)}</td>'
            f'<td class="mono">{_html(session.get("candidate_id"))}</td>'
            f'<td>{_html(session.get("host"))}</td>'
            f'<td>{_html(session.get("provider"))}</td>'
            f'<td>{_html(session.get("model"))}</td>'
            f"<td>{_status(terminal)}</td>"
            f'<td class="mono">{_html(_duration(session.get("duration_seconds")))}</td>'
            f'<td class="mono">{_html(_number(session.get("processed_tokens")))}</td>'
            f'<td class="mono">{_html(_cost(session.get("cost_usd")))}</td>'
            f'<td class="mono">{_html(session.get("verifier_runs"))}</td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Session</th><th>Dispatch</th><th>Candidate</th><th>Host</th><th>Provider</th><th>Model</th>"
        "<th>Terminal state</th><th>Duration</th><th>Processed tokens</th><th>Estimated cost</th><th>Verifier runs</th>"
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def _render_statistics(task: dict[str, Any]) -> str:
    statistics = task.get("statistics")
    if not isinstance(statistics, dict):
        return "<p>Unified statistics were not available for this Search task.</p>"
    timing = statistics.get("timing") or {}
    workers = statistics.get("workers") or {}
    verifiers = statistics.get("verifiers") or {}
    usage = statistics.get("usage") or {}
    efficiency = statistics.get("efficiency") or {}
    lineage = statistics.get("lineage") or {}
    tables = [
        ("Timing", timing, {key: _duration for key in timing if "_seconds" in key}),
        (
            "Workers",
            workers,
            {key: _percent for key in workers if key.endswith("_rate")},
        ),
        (
            "Verifiers",
            verifiers,
            {key: _percent for key in verifiers if key.endswith("_rate")},
        ),
        (
            "Usage",
            {
                key: usage.get(key)
                for key in (
                    "processed_tokens",
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "cost_usd",
                    "tool_calls",
                )
            },
            {"cost_usd": _cost},
        ),
        (
            "Efficiency",
            efficiency,
            {key: _cost for key in efficiency if key.endswith("_usd")},
        ),
        ("Lineage", lineage, {}),
    ]
    return (
        '<div class="stats-grid">'
        + "".join(
            '<div class="stats-table">'
            f"<h3>{escape(title)}</h3>{_stat_rows(values, formatters)}"
            "</div>"
            for title, values, formatters in tables
        )
        + "</div>"
    )


def _render_stop_hook_statistics(statistics: dict[str, Any]) -> str:
    by_event = statistics.get("by_event") or {}
    source_available = statistics.get("source_available") is True

    def event_summary(event_name: str) -> dict[str, Any]:
        if not source_available:
            return {
                "calls": None,
                "block": None,
                "allow": None,
                "skipped": None,
                "error": None,
                "unknown": None,
                "hook_time": None,
            }
        summary = by_event.get(event_name) or {}
        decisions = summary.get("decisions") or {}
        return {
            "calls": summary.get("events_total", 0),
            "block": decisions.get("block", 0),
            "allow": decisions.get("allow", 0),
            "skipped": decisions.get("skipped", 0),
            "error": decisions.get("error", 0),
            "unknown": decisions.get("unknown", 0),
            "hook_time": summary.get("duration_ms_total", 0.0),
        }

    overview = {
        "calls": statistics.get("events_total", 0) if source_available else None,
        "hook_time": (
            statistics.get("duration_ms_total", 0.0) if source_available else None
        ),
        "captured_through": statistics.get("captured_through"),
    }
    summary_tables = (
        '<div class="stats-grid">'
        '<div class="stats-table"><h3>All Stop Hooks</h3>'
        f'{_stat_rows(overview, {"hook_time": _milliseconds})}</div>'
        '<div class="stats-table"><h3>Top-level Stop</h3>'
        f'{_stat_rows(event_summary("Stop"), {"hook_time": _milliseconds})}</div>'
        '<div class="stats-table"><h3>SubagentStop</h3>'
        f'{_stat_rows(event_summary("SubagentStop"), {"hook_time": _milliseconds})}</div>'
        "</div>"
    )

    subagent_rows = []
    for subagent in statistics.get("subagents") or []:
        decisions = subagent.get("decisions") or {}
        subagent_rows.append(
            "<tr>"
            f'<td class="mono"><strong>{_html(subagent.get("identity"))}</strong></td>'
            f'<td>{_html(subagent.get("identity_source"))}</td>'
            f'<td class="mono">{_html(", ".join(subagent.get("host_agent_ids") or []) or None)}</td>'
            f'<td class="mono">{_html(", ".join(subagent.get("run_ids") or []) or None)}</td>'
            f'<td class="mono">{_html(", ".join(subagent.get("candidate_ids") or []) or None)}</td>'
            f'<td class="mono">{_html(subagent.get("events_total"))}</td>'
            f'<td class="mono">{_html(decisions.get("block", 0))}</td>'
            f'<td class="mono">{_html(decisions.get("allow", 0))}</td>'
            f'<td class="mono">{_html(decisions.get("skipped", 0))}</td>'
            f'<td class="mono">{_html(decisions.get("error", 0))}</td>'
            f'<td class="mono">{_html(_milliseconds(subagent.get("duration_ms_total")))}</td>'
            f'<td class="mono">{_html(subagent.get("last_event_at"))}</td>'
            "</tr>"
        )
    if not source_available:
        subagent_table = (
            "<p>Stop-hook event evidence was not persisted for this report. "
            "Counts cannot be distinguished from zero.</p>"
        )
    elif subagent_rows:
        subagent_table = (
            '<div class="table-scroll"><table class="hook-table"><thead><tr>'
            "<th>Subagent</th><th>Identity source</th><th>Host agent</th><th>Run</th><th>Candidate</th>"
            "<th>Calls</th><th>Block</th><th>Allow</th><th>Skipped</th><th>Error</th>"
            "<th>Hook time</th><th>Last event</th>"
            f'</tr></thead><tbody>{"".join(subagent_rows)}</tbody></table></div>'
        )
    else:
        subagent_table = (
            "<p>No SubagentStop invocation was captured in this report snapshot.</p>"
        )

    event_rows = []
    for event in statistics.get("events") or []:
        runtime_agent = event.get("agent_session_id")
        host_agent = event.get("host_agent_id")
        reason = event.get("error") or event.get("reason")
        event_rows.append(
            "<tr>"
            f'<td class="mono">{_html(event.get("started_at"))}</td>'
            f'<td>{_html(event.get("hook_event_name"))}</td>'
            f'<td>{_status(event.get("decision"))}</td>'
            f'<td class="mono">{_html(runtime_agent)}</td>'
            f'<td class="mono">{_html(host_agent)}</td>'
            f'<td class="mono">{_html(event.get("run_id"))}</td>'
            f'<td class="mono">{_html(event.get("candidate_id"))}</td>'
            f'<td class="mono">{_html(_milliseconds(event.get("duration_ms")))}</td>'
            f'<td>{_html(event.get("stop_reason"))}</td>'
            f"<td>{_html(reason)}</td>"
            "</tr>"
        )
    event_evidence = (
        '<details class="summary-block"><summary>Per-invocation hook evidence '
        f'({len(event_rows)})</summary><div><div class="table-scroll"><table class="hook-table"><thead><tr>'
        "<th>Started</th><th>Event</th><th>Decision</th><th>Agent session</th>"
        "<th>Host agent</th><th>Run</th><th>Candidate</th><th>Hook time</th>"
        "<th>Stop reason</th><th>Gate reason / error</th>"
        f'</tr></thead><tbody>{"".join(event_rows)}</tbody></table></div></div></details>'
        if event_rows
        else ""
    )
    return (
        summary_tables
        + '<div class="subsection"><h3>Subagent Breakdown</h3>'
        + subagent_table
        + "</div>"
        + event_evidence
    )


def _render_loop_agent_statistics(statistics: dict[str, Any]) -> str:
    rows = statistics.get("rows") or []
    if not rows:
        return "<p>No Codex loop-agent session was persisted for this report.</p>"

    def observed(row: dict[str, Any], value: Any) -> Any:
        return value if row.get("activity_available") else None

    def count_pair(total: Any, tail: Any, *, label: str = "tail") -> str | None:
        if total is None or tail is None:
            return None
        return f"{_number(total)} total / {_number(tail)} {label}"

    def context_label(row: dict[str, Any]) -> str | None:
        first = _finite_float(row.get("context_percent_at_first_block_nearest"))
        final = _finite_float(row.get("context_percent_final"))
        maximum = _finite_float(row.get("context_percent_max"))
        if first is None and final is None and maximum is None:
            return None

        def percent(value: float | None) -> str:
            return (
                f"{_number(value, digits=1)}%" if value is not None else "Not observed"
            )

        return f"{percent(first)} → {percent(final)} " f"(max {percent(maximum)})"

    summary_rows = []
    for row in rows:
        activity = row.get("activity") or {}
        post_last = row.get("post_last_verifier") or {}
        hook_stream_available = row.get("hook_stream_available") is True
        stop_label = (
            f"{_number(row.get('blocked_stops'))} block / "
            f"{_number(row.get('allowed_stops'))} allow"
            if hook_stream_available
            else None
        )
        productive_label = (
            f"{_number(row.get('productive_blocked_stops'))} / "
            f"{_number(row.get('blocked_stops'))}"
            if hook_stream_available
            else None
        )
        verifier_label = (
            f"{_number(row.get('verified_revision_changes_after_blocked_stops'))} revisions / "
            f"{_number(row.get('verifier_runs_after_blocked_stops'))} verifiers / "
            f"{_number(row.get('improvements_after_blocked_stops'))} improvements"
            if hook_stream_available
            else None
        )
        message_label = observed(
            row,
            (
                f"{_number(post_last.get('assistant_messages'))} messages / "
                f"{_number(post_last.get('empty_messages'))} empty / "
                f"{_number(post_last.get('best_remains_messages'))} best-remains / "
                f"{_number(post_last.get('cannot_continue_messages'))} cannot-continue"
            ),
        )
        summary_rows.append(
            "<tr>"
            f'<td class="mono"><strong>{_html(row.get("candidate_id"))}</strong></td>'
            f'<td class="mono">{_html(row.get("agent_session_id"))}</td>'
            f'<td>{_status("incumbent" if row.get("selected") else "follower")}</td>'
            f'<td class="mono">{_html(_number(row.get("best_score")))}</td>'
            f'<td class="mono">{_html(stop_label)}</td>'
            f'<td class="mono">{_html(productive_label)}</td>'
            f'<td class="mono">{_html(verifier_label)}</td>'
            f'<td class="mono">{_html(observed(row, count_pair(activity.get("context_calls"), post_last.get("context_calls"))))}</td>'
            f'<td class="mono">{_html(observed(row, count_pair(activity.get("global_evidence_calls"), post_last.get("global_evidence_calls"))))}</td>'
            f'<td class="mono">{_html(message_label)}</td>'
            f'<td class="mono">{_html(context_label(row) if row.get("activity_available") else None)}</td>'
            f'<td>{_status(row.get("tail_signal"))}</td>'
            "</tr>"
        )
    summary_table = (
        '<div class="table-scroll"><table class="hook-table"><thead><tr>'
        "<th>Candidate</th><th>Loop agent</th><th>Final role</th><th>Best score</th>"
        "<th>SubagentStop</th><th>Productive blocks</th><th>Post-block verified work</th>"
        "<th>Context reads</th><th>Global-evidence reads</th><th>After last verifier</th>"
        "<th>Context near first block → final</th><th>Tail signal</th>"
        f'</tr></thead><tbody>{"".join(summary_rows)}</tbody></table></div>'
    )

    window_rows = []
    for window in statistics.get("stop_windows") or []:
        window_rows.append(
            "<tr>"
            f'<td class="mono">{_html(window.get("candidate_id"))}</td>'
            f'<td class="mono">{_html(window.get("agent_session_id"))}</td>'
            f'<td class="mono">{_html(window.get("stop_index"))}</td>'
            f'<td>{_status(window.get("decision"))}</td>'
            f'<td class="mono">{_html(window.get("started_at"))}</td>'
            f'<td class="mono">{_html(window.get("context_calls"))}</td>'
            f'<td class="mono">{_html(window.get("global_evidence_calls"))}</td>'
            f'<td class="mono">{_html(window.get("edit_capable_calls"))}</td>'
            f'<td class="mono">{_html(window.get("verifier_runs"))}</td>'
            f'<td class="mono">{_html(window.get("verified_revision_changes"))}</td>'
            f'<td class="mono">{_html(window.get("improvements"))}</td>'
            f'<td class="mono">{_html(window.get("assistant_messages"))}</td>'
            f'<td class="mono">{_html(window.get("empty_messages"))}</td>'
            f'<td class="mono">{_html(window.get("best_remains_messages"))}</td>'
            f'<td class="mono">{_html(window.get("cannot_continue_messages"))}</td>'
            f'<td>{_status(window.get("outcome"))}</td>'
            "</tr>"
        )
    window_table = (
        '<details class="summary-block"><summary>Per-stop continuation windows '
        f'({len(window_rows)})</summary><div><div class="table-scroll"><table class="hook-table"><thead><tr>'
        "<th>Candidate</th><th>Loop agent</th><th>Stop #</th><th>Decision</th><th>Started</th>"
        "<th>Context</th><th>Evidence</th><th>Edit-capable</th><th>Verifier</th>"
        "<th>Revision changes</th><th>Improvements</th><th>Assistant</th><th>Empty</th>"
        "<th>Best-remains</th><th>Cannot-continue</th><th>Outcome</th>"
        f'</tr></thead><tbody>{"".join(window_rows)}</tbody></table></div></div></details>'
        if window_rows
        else '<p class="footnote">No per-invocation SubagentStop window was captured.</p>'
    )
    return (
        summary_table
        + window_table
        + '<p class="footnote">Final role is a report-time incumbent/follower label, not a causal claim. '
        "A productive block requires a blocked SubagentStop followed before the next stop by a verifier "
        "on a different immutable Git revision. Context and global-evidence counts come from content-free "
        "native Codex activity events. Tail signals are deterministic heuristics over the period after the "
        "last verifier; unavailable evidence remains Not observed.</p>"
    )


def _render_candidate_loop_statistics(
    activity: dict[str, Any],
    stop_hook_statistics: dict[str, Any],
) -> str:
    hook_decisions = stop_hook_statistics.get("by_decision") or {}
    hook_by_event = stop_hook_statistics.get("by_event") or {}
    stop_blocks = (hook_by_event.get("Stop") or {}).get("decisions") or {}
    subagent_blocks = (hook_by_event.get("SubagentStop") or {}).get("decisions") or {}
    requested = {
        "candidate_submissions": activity.get("candidates_submitted", 0),
        "completed_with_result": activity.get("candidates_completed_with_result", 0),
        "rejected_results": activity.get("results_rejected", 0),
        "agent_resumes": activity.get("agent_resumes", 0),
        "stop_hook_continue_triggers": hook_decisions.get("block", 0),
    }
    result_details = {
        "durable_results": activity.get("results_total", 0),
        "kept_results": activity.get("results_kept", 0),
        "rejected_results": activity.get("results_rejected", 0),
        "unsettled_results": activity.get("results_unsettled", 0),
    }
    continuation_details = {
        "same_session_resumes": activity.get("same_session_resumes", 0),
        "redispatch_resumes": activity.get("redispatch_resumes", 0),
        "top_level_stop_triggers": stop_blocks.get("block", 0),
        "subagent_stop_triggers": subagent_blocks.get("block", 0),
    }
    return (
        '<div class="stats-grid">'
        '<div class="stats-table"><h3>Requested Candidate Metrics</h3>'
        f"{_stat_rows(requested)}</div>"
        '<div class="stats-table"><h3>Result Settlement</h3>'
        f"{_stat_rows(result_details)}</div>"
        '<div class="stats-table"><h3>Continuation Sources</h3>'
        f"{_stat_rows(continuation_details)}</div>"
        "</div>"
    )


def _render_task(
    task: dict[str, Any],
    index: int,
    *,
    trajectory_payload: dict[str, Any] | None = None,
) -> str:
    run = task.get("run") or {}
    frozen = task.get("frozen_spec") or {}
    stats = task.get("statistics") or {}
    scores = stats.get("scores") or {}
    timing = stats.get("timing") or {}
    score_target = scores.get("target")
    score_target_text = (
        "Not configured" if score_target is None else _number(score_target, digits=4)
    )
    run_id = str(task.get("run_id") or f"task-{index}")
    selected = task.get("is_report_run")
    trajectory = (
        _render_search_trajectory(trajectory_payload)
        if trajectory_payload is not None
        else ""
    )
    return (
        f'<article class="panel task-panel" data-run-id="{escape(run_id, quote=True)}" '
        f'id="task-{escape(run_id, quote=True)}" {"" if selected else "hidden"}>'
        '<header class="task-head">'
        '<div class="task-title-line">'
        f'<h3>Search Task {index:02d}: <span class="mono">{escape(run_id)}</span></h3>'
        f'{_status(run.get("state") or task.get("state"))}'
        "</div>"
        f'<p class="task-objective">{_html(frozen.get("objective"))}</p>'
        "</header>"
        '<div class="task-metrics">'
        f'<div class="task-metric"><span class="kpi-label">Goal revision</span><strong>{_html(task.get("goal_revision"))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Strategy</span><strong>{_html((task.get("strategy") or {}).get("name"))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Orchestration</span><strong>{_html((task.get("strategy") or {}).get("orchestration_mode"))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Baseline</span><strong class="mono">{_html(_number(scores.get("baseline")))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Score target</span><strong class="mono">{escape(score_target_text)}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Best / selected</span><strong class="mono">{_html(_number(scores.get("best")))} / {_html(_number(scores.get("selected")))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">First improvement</span><strong class="mono">{_html(_duration(timing.get("time_to_first_improvement_seconds")))}</strong></div>'
        "</div>"
        '<section class="subsection">'
        f"{trajectory}"
        f'{_render_timeline(task.get("timeline") or {}, title="Search Execution Timeline", include_score_chart=not bool(trajectory))}'
        '<p class="footnote">This axis is scoped to this Search run. Worker bars show actual host execution, not the configured maximum or an aspirational exploration window.</p>'
        "</section>"
        '<section class="subsection"><h3>Shared Evidence View</h3>'
        f"{_render_shared_evidence_view(task)}</section>"
        '<section class="subsection"><h3>Candidate Evidence</h3>'
        f"{_render_candidates(task)}</section>"
        '<section class="subsection"><h3>Worker Sessions</h3>'
        f"{_render_sessions(task)}</section>"
        '<section class="subsection"><h3>Complete Statistical View</h3>'
        f"{_render_statistics(task)}"
        '<details class="summary-block"><summary>Raw normalized Search statistics</summary>'
        f"<pre>{escape(json.dumps(stats, indent=2, ensure_ascii=False, sort_keys=True))}</pre></details>"
        "</section></article>"
    )


def render_html_report(data: dict[str, Any]) -> str:
    snapshot = data.get("snapshot") or {}
    goal = snapshot.get("goal_plus") or {}
    aggregate = snapshot.get("search_task_aggregate") or {}
    aggregate_stats = aggregate.get("statistics") or {}
    candidate_activity = aggregate_stats.get("activity") or {}
    aggregate_usage = aggregate_stats.get("usage") or {}
    total_statistics = snapshot.get("statistics") or {}
    total_usage = total_statistics.get("total_usage") or {}
    orchestrator = total_statistics.get("orchestrator") or {}
    orchestrator_usage = orchestrator.get("usage") or {}
    tasks = data.get("search_tasks") or []
    report_run_id = str(data.get("report_run_id") or "unknown")
    selected_task = next(
        (task for task in tasks if task.get("is_report_run")),
        tasks[-1] if tasks else {},
    )
    selected_frozen = selected_task.get("frozen_spec") or {}
    selected_stats = selected_task.get("statistics") or {}
    selected_scores = selected_stats.get("scores") or {}
    unavailable = total_statistics.get("unavailable_metrics") or []
    missing = (selected_stats.get("data_quality") or {}).get("missing") or []
    warnings = snapshot.get("warnings") or []
    stop_hook_statistics = data.get("stop_hook_statistics") or {}
    loop_agent_statistics = data.get("loop_agent_statistics") or {}
    goal_id = data.get("goal_plus_id")
    title_id = str(goal_id or report_run_id)
    state = goal.get("status") or (selected_task.get("run") or {}).get("state")
    search_count = aggregate.get("search_tasks_total", len(tasks))
    run_state = (selected_task.get("run") or {}).get("state") or selected_task.get(
        "state"
    )
    score_target = selected_scores.get("target")
    score_gain = selected_scores.get("selected_improvement_from_baseline")
    score_baseline = _finite_float(selected_scores.get("baseline"))
    score_detail = (
        "No baseline / gain No baseline"
        if score_baseline is None
        else f"baseline {_number(score_baseline, digits=4)} / "
        f"gain {_metric_readout('score_gain', score_gain)}"
    )
    completion_detail = f"run {str(run_state or 'unknown').lower()}"
    if score_target is None:
        completion_detail += " / no score threshold"
        completion_semantics = (
            "No score threshold was configured for this Search task. Complete means the Goal Plus "
            "workflow finished and the selected candidate survived the required verification and promotion; "
            "it does not claim that an unspecified threshold was reached."
        )
        score_target_text = "Not configured"
    else:
        target_reached = selected_scores.get("target_reached")
        target_result = (
            "reached"
            if target_reached is True
            else "not reached" if target_reached is False else "not evaluated"
        )
        completion_detail += f" / target {target_result}"
        completion_semantics = (
            f"A score threshold of {_number(score_target, digits=4)} was configured and was "
            f"{target_result} by the selected result."
        )
        score_target_text = _number(score_target, digits=4)

    kpis = "".join(
        [
            _metric_card(
                "Goal status",
                _text(state).title(),
                completion_detail,
                _status_class(state),
            ),
            _metric_card("Search tasks", _number(search_count), "GP-level tasks"),
            _metric_card(
                "Selected score",
                _number(selected_scores.get("selected"), digits=4),
                score_detail,
                "success",
            ),
            _metric_card(
                "Candidates",
                _number(aggregate.get("candidates_total")),
                f"{_number(aggregate.get('candidates_evaluated'))} evaluated",
            ),
            _metric_card(
                "Worker sessions",
                _number(aggregate.get("worker_sessions_total")),
                f"{_number((aggregate_stats.get('workers') or {}).get('timed_out'))} timed out",
            ),
            _metric_card(
                "Verifier runs",
                _number(aggregate.get("verifier_runs_total")),
                f"{_number((aggregate_stats.get('verifiers') or {}).get('parent_process_runs'))} parent-owned",
            ),
            _metric_card(
                "Processed tokens",
                _number(aggregate_usage.get("processed_tokens")),
                "worker sessions",
            ),
            _metric_card(
                "Estimated worker cost",
                _cost(aggregate_usage.get("cost_usd")),
                "coverage-aware",
            ),
        ]
    )

    trajectory_payloads = {
        str(task.get("run_id")): payload
        for task in tasks
        if (payload := _search_trajectory_payload(task)) is not None
    }
    plotly_javascript = _load_plotly_javascript() if trajectory_payloads else None
    plotly_script = f"<script>{plotly_javascript}</script>" if plotly_javascript else ""

    task_tabs = "".join(
        f'<button class="task-tab" type="button" data-task-target="{escape(str(task.get("run_id")), quote=True)}" '
        f'aria-selected="{"true" if task.get("is_report_run") else "false"}">'
        f'<span>Task {index:02d}</span><span class="mono">r{_html(task.get("goal_revision"))}</span>'
        f'{_status((task.get("run") or {}).get("state"))}</button>'
        for index, task in enumerate(tasks, start=1)
    )
    task_panels = "".join(
        _render_task(
            task,
            index,
            trajectory_payload=(
                trajectory_payloads.get(str(task.get("run_id")))
                if plotly_javascript
                else None
            ),
        )
        for index, task in enumerate(tasks, start=1)
    )
    warning_items = (
        "".join(
            f'<li><span class="mono">{_html(item.get("kind") if isinstance(item, dict) else "warning")}</span>: '
            f"{_html(json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else item)}</li>"
            for item in warnings
        )
        or "<li>No monitor warnings.</li>"
    )
    metric_availability = _render_metric_availability(unavailable + missing)

    worker_sources = int(aggregate_usage.get("sources_total") or 0)
    worker_coverage = (aggregate_usage.get("coverage") or {}).get(
        "processed_tokens"
    ) or 0
    coverage_percent = (
        min(100.0, worker_coverage / worker_sources * 100) if worker_sources else 0.0
    )
    raw_payload = {
        "schema_version": data.get("schema_version"),
        "generated_at": data.get("generated_at"),
        "snapshot": snapshot,
        "search_tasks": tasks,
        "timeline": data.get("timeline"),
        "stop_hook_statistics": stop_hook_statistics,
        "loop_agent_statistics": loop_agent_statistics,
    }

    print_icon = (
        '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
        '<rect width="12" height="8" x="6" y="14"/></svg>'
    )

    return f"""<!doctype html>
<html lang="en" data-report-schema="goal-plus-report/v{REPORT_SCHEMA_VERSION}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Goal Plus Execution Report: {escape(title_id)}</title>
  <script>document.documentElement.classList.add('js');</script>
  <style>{_REPORT_CSS}</style>
</head>
<body data-goal-plus-id="{escape(str(goal_id or ''), quote=True)}" data-run-id="{escape(report_run_id, quote=True)}">
  <header class="masthead">
    <div class="wrap masthead-inner">
      <div class="identity">
        <div class="eyebrow">Goal Plus Execution Report</div>
        <div class="identity-line"><h1>{escape(title_id)}</h1>{_status(state)}</div>
        <div class="id-line mono">Report run: {escape(report_run_id)}</div>
      </div>
      <div class="masthead-actions">
        <div class="generated">Generated<strong class="mono">{_html(data.get("generated_at"))}</strong></div>
        <button class="button no-print" type="button" onclick="window.print()" title="Print report">{print_icon}<span>Print</span></button>
      </div>
    </div>
  </header>
  <nav class="section-nav no-print" aria-label="Report sections">
    <div class="wrap">
      <a href="#aggregate">Summary</a><a href="#goal">Goal</a>
      <a href="#hooks">Candidate / hooks</a>
      <a href="#tasks">Search tasks ({escape(_number(search_count))})</a><a href="#audit">Audit</a>
    </div>
  </nav>
  <main class="wrap">
    <section id="aggregate" class="report-section">
      <div class="section-kicker">Goal Plus Summary</div>
      <div class="kpi-grid">{kpis}</div>
    </section>
    <section id="goal" class="report-section">
      <h2>Goal And Completion</h2>
      <div class="panel panel-body">
        <h3>Selected Search Objective</h3>
        <p class="objective">{_html(selected_frozen.get("objective"))}</p>
      </div>
      <div class="panel panel-body completion-note">
        <h3>Completion semantics</h3>
        <p>{escape(completion_semantics)}</p>
      </div>
      <div class="panel panel-body">
        <h3>Goal Record</h3>
        <dl class="fact-grid">
          <div class="fact"><dt>Goal revision</dt><dd>{_html(goal.get("goal_revision"))}</dd></div>
          <div class="fact"><dt>Phase</dt><dd>{_html(goal.get("phase"))}</dd></div>
          <div class="fact"><dt>Selection survival</dt><dd>{_html(_percent(aggregate_stats.get("selection_survival_rate")))}</dd></div>
          <div class="fact"><dt>Score target</dt><dd>{escape(score_target_text)}</dd></div>
        </dl>
        <details class="summary-block"><summary>Original raw goal</summary><div class="raw-goal">{_html(goal.get("raw_goal"))}</div></details>
      </div>
      <div class="panel panel-body">
        <h3>Usage Coverage</h3>
        <div class="coverage-row"><span>Worker processed-token coverage</span><strong>{worker_coverage}/{worker_sources} sessions</strong></div>
        <div class="coverage-bar" aria-label="Worker usage coverage"><span style="width:{coverage_percent:.2f}%"></span></div>
        <dl class="fact-grid coverage">
          <div class="fact"><dt>Worker processed tokens</dt><dd class="mono">{_html(_number(aggregate_usage.get("processed_tokens")))}</dd></div>
          <div class="fact"><dt>Orchestrator processed tokens</dt><dd class="mono">{_html(_number(orchestrator_usage.get("processed_tokens")))}</dd></div>
          <div class="fact"><dt>Combined known tokens</dt><dd class="mono">{_html(_number(total_usage.get("processed_tokens")))}</dd></div>
          <div class="fact"><dt>Combined estimated cost</dt><dd class="mono">{_html(_cost(total_usage.get("cost_usd")))}</dd></div>
        </dl>
      </div>
    </section>
    <section id="hooks" class="report-section">
      <div class="section-kicker">Candidate Loop And Host Hook Evidence</div>
      <div class="activity-summary">
      <h2>Candidate Loop Activity</h2>
      {_render_candidate_loop_statistics(candidate_activity, stop_hook_statistics)}
      <p class="footnote">Candidate submissions count durable candidate records. Completed with Result counts unique candidates with at least one verifier-settled iteration. Rejected Results are discard/failure dispositions. Agent resumes combine accepted same-session continuations and state redispatches. Stop-hook continue triggers count automatic Stop/SubagentStop block decisions.</p>
      </div>
      <div class="activity-summary">
      <h2>Loop Agent Stop Outcomes</h2>
      {_render_loop_agent_statistics(loop_agent_statistics)}
      </div>
      <h2>Stop Hook Activity</h2>
      {_render_stop_hook_statistics(stop_hook_statistics)}
      <p class="footnote">This is a static snapshot of durable automatic Stop and SubagentStop invocations available when the report was generated. Direct goal_plus_gate calls are excluded.</p>
    </section>
    <section id="tasks" class="report-section">
      <div class="section-kicker">Per-Task Evidence</div>
      <h2>Search Tasks</h2>
      <div class="task-tabs no-print" role="tablist" aria-label="Search tasks">{task_tabs}</div>
      {task_panels or '<div class="panel panel-body">No linked Search tasks were found.</div>'}
    </section>
    <section id="audit" class="report-section">
      <h2>Report Audit</h2>
      <div class="two-column">
        <div class="panel panel-body"><h3>Monitor Warnings</h3><ul class="warning-list">{warning_items}</ul></div>
        <div class="panel panel-body"><h3>Timeline Gate Summary</h3>{_stat_rows((data.get("timeline") or {}).get("gate_events") or {}) or '<p>No gate events observed.</p>'}</div>
      </div>
      {metric_availability}
      <details class="summary-block"><summary>Complete normalized report data</summary><pre>{escape(json.dumps(raw_payload, indent=2, ensure_ascii=False, sort_keys=True))}</pre></details>
      <p class="footnote">Schema goal-plus-report/v{REPORT_SCHEMA_VERSION}. This file is self-contained and generated from durable Goal Plus/Search state. Host-native transcripts remain external evidence and are summarized only through normalized observability.</p>
    </section>
  </main>
  {plotly_script}
  <script>{_REPORT_SCRIPT}</script>
</body>
</html>
"""


def _codex_report_hook_statistics(observation: dict[str, Any]) -> dict[str, Any]:
    existing = observation.get("hook_statistics")
    if isinstance(existing, dict):
        return existing

    hooks = observation.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    by_decision = _stop_hook_decision_counts()
    by_event: dict[str, dict[str, Any]] = {}
    events_total = 0
    for event_name, source_name in (
        ("Stop", "stop"),
        ("SubagentStop", "subagent_stop"),
    ):
        source = hooks.get(source_name)
        source = source if isinstance(source, dict) else {}
        started = int(source.get("started") or 0)
        block = int(source.get("blocked") or 0)
        allow = int(source.get("completed_or_allowed") or 0)
        error = int(source.get("other_terminal_statuses") or 0)
        unknown = max(0, started - block - allow - error)
        decisions = {
            "block": block,
            "allow": allow,
            "skipped": 0,
            "error": error,
            "unknown": unknown,
        }
        by_event[event_name] = {
            "events_total": started,
            "duration_ms_total": None,
            "decisions": decisions,
        }
        events_total += started
        for decision, count in decisions.items():
            by_decision[decision] += count
    return {
        "schema_version": 1,
        "events_total": events_total,
        "duration_ms_total": None,
        "captured_through": None,
        "by_event": by_event,
        "by_decision": by_decision,
        "subagents": [],
        "events": [],
    }


def _codex_trajectory_payload(observation: dict[str, Any]) -> dict[str, Any] | None:
    evaluations = observation.get("evaluations")
    evaluations = evaluations if isinstance(evaluations, dict) else {}
    entries = evaluations.get("entries")
    entries = entries if isinstance(entries, list) else []
    if not entries:
        return None

    metric_name = str(evaluations.get("metric_name") or "score")
    direction = str(evaluations.get("metric_direction") or "maximize")
    if direction not in {"minimize", "maximize"}:
        direction = "maximize"

    normalized: list[dict[str, Any]] = []
    for call, raw_entry in enumerate(entries, start=1):
        if not isinstance(raw_entry, dict):
            continue
        score = _finite_float(raw_entry.get("score"))
        round_name = str(raw_entry.get("round") or f"submission-{call}")
        kind = str(raw_entry.get("kind") or "submission")
        normalized.append(
            {
                "call": call,
                "round": round_name,
                "kind": kind,
                "score": score,
                "valid": raw_entry.get("valid") is not False and score is not None,
                "at": raw_entry.get("at"),
            }
        )
    scored = [entry for entry in normalized if entry["score"] is not None]
    if not scored:
        return None

    trajectories: list[dict[str, Any]] = []
    kinds = list(dict.fromkeys(str(entry["kind"]) for entry in scored))
    for kind in kinds:
        points = [entry for entry in scored if entry["kind"] == kind]
        passing = [entry for entry in points if entry["valid"]]
        failed = [entry for entry in points if not entry["valid"]]
        trajectories.append(
            {
                "candidate_id": f"{kind} submissions",
                "selected": any(
                    entry["round"] == evaluations.get("best_round") for entry in points
                ),
                "calls": [entry["call"] for entry in passing],
                "scores": [entry["score"] for entry in passing],
                "details": [
                    [
                        entry["round"],
                        f"{kind} submission",
                        str(entry.get("at") or "timestamp unavailable"),
                    ]
                    for entry in passing
                ],
                "failed_calls": [entry["call"] for entry in failed],
                "failed_scores": [entry["score"] for entry in failed],
                "failed_details": [
                    [
                        entry["round"],
                        f"{kind} submission",
                        str(entry.get("at") or "timestamp unavailable"),
                    ]
                    for entry in failed
                ],
            }
        )

    global_calls: list[int] = []
    global_scores: list[float] = []
    current: float | None = None
    for entry in normalized:
        if not entry["valid"]:
            continue
        score = float(entry["score"])
        if current is None or _is_better_score(score, current, direction):
            current = score
        global_calls.append(int(entry["call"]))
        global_scores.append(float(current))

    best_round = evaluations.get("best_round")
    selected_entry = next(
        (
            entry
            for entry in normalized
            if entry["valid"] and entry["round"] == best_round
        ),
        None,
    )
    selected_point = (
        {
            "candidate_id": f"{selected_entry['kind']} submissions",
            "call": selected_entry["call"],
            "score": selected_entry["score"],
        }
        if selected_entry is not None
        else None
    )
    passing_scores = [float(entry["score"]) for entry in normalized if entry["valid"]]
    passing_count = sum(entry["valid"] for entry in normalized)
    return {
        "title": "Submission Score Trajectory",
        "aria_subject": "Codex submission score trajectory",
        "unit_label": "submissions",
        "group_label": "series",
        "call_label": "Submission",
        "point_label": "Round",
        "best_label": "Best-so-far",
        "failure_label": "invalid submission",
        "failure_legend": "Invalid submission · not ranked",
        "export_name": "codex-run-submission-trajectory",
        "metric_name": metric_name,
        "metric_direction": direction,
        "baseline": None,
        "selected": _finite_float(evaluations.get("best_score")),
        "evaluations": len(entries),
        "passing_evaluations": passing_count,
        "failed_evaluations": len(entries) - passing_count,
        "call_window": _trajectory_call_window(len(entries)),
        "score_axis": _trajectory_score_axis(passing_scores),
        "trajectories": trajectories,
        "global_best": {"calls": global_calls, "scores": global_scores},
        "selected_point": selected_point,
    }


def _render_stop_progress_chart(observation: dict[str, Any]) -> str:
    hooks = observation.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    stop = hooks.get("stop")
    stop = stop if isinstance(stop, dict) else {}
    raw_bins = stop.get("blocked_output_progress_bins")
    bins = [int(value or 0) for value in raw_bins] if isinstance(raw_bins, list) else []
    if not bins:
        return '<p class="footnote">No output-progress distribution was persisted.</p>'

    width = 1000.0
    height = 210.0
    chart_top = 18.0
    chart_bottom = 178.0
    chart_height = chart_bottom - chart_top
    maximum = max(bins) or 1
    step = width / len(bins)
    gap = min(2.0, step * 0.18)
    bars = []
    for index, count in enumerate(bins):
        bar_height = chart_height * count / maximum
        x = index * step + gap / 2
        y = chart_bottom - bar_height
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(0.5, step - gap):.2f}" '
            f'height="{bar_height:.2f}" fill="var(--accent)">'
            f"<title>Output progress bin {index + 1}: {count} blocked Stop attempts</title>"
            "</rect>"
        )
    return (
        '<div class="stop-progress-chart">'
        f'<svg viewBox="0 0 1000 {height:.0f}" preserveAspectRatio="none" '
        'role="img" aria-label="Blocked Stop attempts by execution output progress">'
        f'<line x1="0" y1="{chart_bottom:.2f}" x2="{width:.2f}" y2="{chart_bottom:.2f}" '
        'stroke="var(--border-strong)" stroke-width="1"/>'
        + "".join(bars)
        + f'<text x="4" y="12" fill="var(--muted)" font-size="10">max bin {maximum}</text>'
        "</svg>"
        '<div class="stop-progress-axis"><span>0% output</span>'
        "<span>Execution-output progress, not wall-clock time</span>"
        "<span>100% output</span></div></div>"
    )


def _render_codex_execution_timeline(observation: dict[str, Any]) -> str:
    run = observation.get("run")
    run = run if isinstance(run, dict) else {}
    run_log = observation.get("run_log")
    run_log = run_log if isinstance(run_log, dict) else {}
    evaluations = observation.get("evaluations")
    evaluations = evaluations if isinstance(evaluations, dict) else {}
    entries = evaluations.get("entries")
    entries = entries if isinstance(entries, list) else []
    started_at = run_log.get("first_timestamp") or run.get("started_at")
    ended_at = run_log.get("last_timestamp") or run.get("ended_at")
    start_epoch = _epoch(started_at)
    end_epoch = _epoch(ended_at)
    if start_epoch is None or end_epoch is None or end_epoch <= start_epoch:
        return (
            '<div class="panel panel-body"><p>Execution start/end timestamps were not '
            "available for a wall-clock timeline.</p></div>"
        )

    span = end_epoch - start_epoch
    auto_points = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "auto":
            continue
        timestamp = entry.get("at")
        event_epoch = _epoch(timestamp)
        if event_epoch is None:
            continue
        left = max(0.0, min(99.0, (event_epoch - start_epoch) / span * 100.0))
        auto_points.append(
            f'<span class="timeline-event parent point" style="left:{left:.3f}%;width:0.8%;" '
            f'title="{escape(str(entry.get("round") or "auto evaluation"), quote=True)} · '
            f'{escape(str(timestamp), quote=True)}"></span>'
        )
    terminal_state = str(
        run.get("terminal_state")
        or ("timed_out" if run.get("timed_out") else "completed")
    )
    terminal_label = terminal_state.replace("_", " ")
    return (
        '<div class="panel timeline-shell">'
        '<div class="timeline-head"><h2>Codex Execution Timeline</h2>'
        f'<span class="mono">Observed span: {escape(_duration(span))}</span></div>'
        '<div class="timeline-scroll" tabindex="0" aria-label="Codex execution timeline">'
        '<div class="timeline" style="--timeline-width:980px">'
        '<div class="timeline-rows" data-track-count="2">'
        '<div class="timeline-row"><div class="timeline-label">Codex process</div>'
        '<div class="timeline-track"><span class="timeline-event main" '
        f'style="left:0%;width:100%;" title="Codex execution · {escape(terminal_label, quote=True)}">'
        f"Codex execution · {escape(terminal_label)}</span></div></div>"
        '<div class="timeline-row"><div class="timeline-label">Auto evaluations</div>'
        f'<div class="timeline-track">{"".join(auto_points)}</div></div>'
        "</div>"
        '<div class="timeline-axis">'
        f"<span>{escape(str(started_at))}</span><span>+{escape(_duration(span / 2))}</span>"
        f"<span>{escape(str(ended_at))}</span></div></div></div>"
        '<div class="timeline-key"><span><i class="key-dot"></i>Codex process</span>'
        '<span><i class="key-dot parent"></i>Timestamped auto evaluation</span>'
        "<span>Agent submissions without persisted timestamps remain in submission order only.</span>"
        "</div></div>"
    )


def _render_codex_submissions(observation: dict[str, Any]) -> str:
    evaluations = observation.get("evaluations")
    evaluations = evaluations if isinstance(evaluations, dict) else {}
    entries = evaluations.get("entries")
    entries = entries if isinstance(entries, list) else []
    rows = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        passed = entry.get("passed")
        total_tests = entry.get("total_tests")
        test_result = (
            f"{passed}/{total_tests}"
            if isinstance(passed, int) and isinstance(total_tests, int)
            else "Not observed"
        )
        rows.append(
            "<tr>"
            f'<td class="mono">{index}</td>'
            f'<td class="mono"><strong>{_html(entry.get("round"))}</strong></td>'
            f"<td>{_html(entry.get('kind'))}</td>"
            f'<td class="mono">{_html(_number(entry.get("score"), digits=4))}</td>'
            f'<td class="mono">{_html(_percent(entry.get("pass_rate")))}</td>'
            f"<td>{_html(test_result)}</td>"
            f"<td>{_html('Yes' if entry.get('valid') is True else 'No')}</td>"
            f'<td class="mono">{_html(entry.get("at"))}</td>'
            f"<td>{_html(entry.get('summary'))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>No persisted submission history was found.</p>"
    return (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>#</th><th>Round</th><th>Kind</th><th>Score</th><th>Pass rate</th>"
        "<th>Tests</th><th>Valid</th><th>Timestamp</th><th>Summary</th>"
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def _artifact_link(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "Not observed"
    path = Path(value)
    try:
        uri = path.as_uri()
    except ValueError:
        return _html(value)
    return f'<a href="{escape(uri, quote=True)}">{_html(path.name)}</a>'


def _render_codex_goal_plus_finalization(observation: dict[str, Any]) -> str:
    goal_plus = observation.get("goal_plus")
    if not isinstance(goal_plus, dict) or not goal_plus.get("source_available"):
        return ""
    records = goal_plus.get("records")
    records = records if isinstance(records, list) else []
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        search_tasks = record.get("search_tasks")
        search_tasks = search_tasks if isinstance(search_tasks, list) else []
        search_state = (
            ", ".join(
                f"{task.get('run_id')}={task.get('state') or 'unknown'}"
                for task in search_tasks
                if isinstance(task, dict)
            )
            or "None"
        )
        rows.append(
            "<tr>"
            f"<td class=\"mono\"><strong>{_html(record.get('goal_plus_id'))}</strong></td>"
            f"<td>{_status(record.get('status'))}</td>"
            f"<td>{_html(record.get('phase'))}</td>"
            f"<td>{_html(record.get('next_action'))}</td>"
            f"<td>{_html(record.get('result_recorded_at'))}</td>"
            f"<td class=\"mono\">{_html(record.get('selected_candidate_id'))}</td>"
            f'<td class="mono">{_html(search_state)}</td>'
            "</tr>"
        )
    overall = str(goal_plus.get("overall_status") or "unknown")
    overall_label = {
        "complete": "Complete",
        "incomplete": "Incomplete",
        "unavailable": "Not observed",
    }.get(overall, overall.replace("_", " ").title())
    tone = (
        "success"
        if overall == "complete"
        else "failure" if overall == "incomplete" else "warning"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Goal</th><th>Status</th><th>Phase</th><th>Next action</th>"
        "<th>Result recorded</th><th>Selected candidate</th><th>Search runs</th>"
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        if rows
        else "<p>No Goal Plus records were found in the persisted state archive.</p>"
    )
    return (
        '<section id="goal-plus-finalization" class="report-section">'
        '<div class="section-kicker">Goal Plus Durable State</div>'
        '<div class="kpi-grid">'
        f'{_metric_card("Goal Plus finalization", overall_label, f"{len(records)} persisted record(s)", tone)}'
        f'{_metric_card("Active records", _number(goal_plus.get("active_records")), "must be zero for a final Goal Plus report", "failure" if goal_plus.get("active_records") else "success")}'
        f'{_metric_card("Terminal records", _number(goal_plus.get("terminal_records")), "complete / blocked / abandoned")}'
        f'{_metric_card("Persisted reports", _number(goal_plus.get("reports_generated")), "terminal Goal Plus report paths")}'
        "</div>"
        '<div class="panel panel-body">'
        "<h3>Goal Plus Finalization Evidence</h3>"
        "<p>Benchmark evaluator outcome and Goal Plus lifecycle completion are "
        "independent facts. A valid promoted result does not make an active Goal "
        "record terminal.</p>"
        f"{table}</div></section>"
    )


def render_codex_observability_report(observation: dict[str, Any]) -> str:
    run = observation.get("run")
    run = run if isinstance(run, dict) else {}
    result = observation.get("result")
    result = result if isinstance(result, dict) else {}
    usage = observation.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    evaluations = observation.get("evaluations")
    evaluations = evaluations if isinstance(evaluations, dict) else {}
    availability = observation.get("availability")
    availability = availability if isinstance(availability, dict) else {}
    evidence = observation.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    warnings = observation.get("warnings")
    warnings = warnings if isinstance(warnings, list) else []
    hook_statistics = _codex_report_hook_statistics(observation)
    stop = (observation.get("hooks") or {}).get("stop") or {}
    stop_observed = availability.get("stop_hook_events")
    if not isinstance(stop_observed, bool):
        stop_observed = bool(hook_statistics.get("events_total"))
    hook_activity_html = (
        _render_stop_hook_statistics(hook_statistics)
        if stop_observed
        else (
            '<div class="panel panel-body"><p>Stop/SubagentStop activity was not '
            "observed because no structured hook evidence or hook lifecycle output "
            "was persisted for this run.</p></div>"
        )
    )
    stop_progress_html = (
        _render_stop_progress_chart(observation)
        if stop_observed
        else '<p class="footnote">No persisted hook evidence was available.</p>'
    )
    trajectory_payload = _codex_trajectory_payload(observation)
    plotly_javascript = _load_plotly_javascript() if trajectory_payload else None
    trajectory_html = (
        _render_search_trajectory(trajectory_payload)
        if trajectory_payload is not None and plotly_javascript
        else (
            '<div class="panel panel-body"><p>Submission history is available below, '
            "but Plotly is not installed in this renderer environment.</p></div>"
            if trajectory_payload is not None
            else '<div class="panel panel-body"><p>No scored submission trajectory was found.</p></div>'
        )
    )
    plotly_script = f"<script>{plotly_javascript}</script>" if plotly_javascript else ""

    run_id = str(run.get("run_id") or "unknown")
    task_id = str(run.get("task_id") or "unknown")
    timed_out = bool(run.get("timed_out"))
    termination_reason = str(
        run.get("termination_reason") or ("timeout" if timed_out else "completed")
    )
    outcome_status = str(result.get("outcome_status") or "unknown")
    observed_terminal_state = run.get("terminal_state")
    if isinstance(observed_terminal_state, str) and observed_terminal_state:
        terminal_state = observed_terminal_state
    elif timed_out:
        terminal_state = (
            "budget_reached"
            if termination_reason == "budget_exhausted"
            else "timed_out"
        )
    else:
        terminal_state = "complete"
    outcome_label = {
        "success": "Successful",
        "valid_result": "Valid result",
        "no_valid_result": "No valid result",
        "no_result": "No result",
    }.get(outcome_status, outcome_status.replace("_", " ").title())
    terminal_label = {
        "budget_reached": "Budget reached",
        "timed_out": "Timed out",
        "complete": "Completed",
        "completed": "Completed",
        "completed_in_finalization_grace": "Completed in finalization grace",
        "failed": "Failed",
    }.get(terminal_state, terminal_state.replace("_", " ").title())
    terminal_tone = (
        "success"
        if terminal_state == "budget_reached" and outcome_status == "success"
        else _status_class(terminal_state)
    )
    total_submissions = int(
        result.get("total_rounds") or len(evaluations.get("entries") or [])
    )
    goal_plus = observation.get("goal_plus")
    goal_plus = goal_plus if isinstance(goal_plus, dict) else {}
    kpi_cards = [
        _metric_card(
            "Run status",
            terminal_label,
            f"{outcome_label} · resumes {_number(run.get('resume_count'))}",
            terminal_tone,
        ),
        _metric_card(
            "Runtime",
            _duration(run.get("runtime_seconds")),
            "observed agent runtime",
        ),
        _metric_card(
            "Model",
            _text(run.get("model")),
            f"reasoning {_text(run.get('reasoning_effort'))}",
        ),
        _metric_card(
            "Blocked Stop",
            _number(stop.get("blocked")) if stop_observed else "Not observed",
            (
                f"{_number(stop.get('blocked_per_agent_hour'), digits=1)} / agent-hour"
                if stop_observed
                else "no persisted hook evidence"
            ),
            "warning",
        ),
    ]
    if goal_plus.get("source_available"):
        goal_status = str(goal_plus.get("overall_status") or "unknown")
        kpi_cards.append(
            _metric_card(
                "Goal Plus finalization",
                {
                    "complete": "Complete",
                    "incomplete": "Incomplete",
                }.get(goal_status, goal_status.replace("_", " ").title()),
                f"{_number(goal_plus.get('active_records'))} active record(s)",
                "success" if goal_status == "complete" else "failure",
            )
        )
    if total_submissions:
        kpi_cards.extend(
            [
                _metric_card(
                    "Best score",
                    _number(result.get("best_score"), digits=4),
                    f"round {_text(result.get('best_round'))}",
                    "success",
                ),
                _metric_card(
                    "Best pass rate",
                    _percent(result.get("best_pass_rate")),
                    "persisted evaluator evidence",
                    "success",
                ),
                _metric_card(
                    "Submissions",
                    _number(total_submissions),
                    f"{_number(result.get('agent_submissions'))} agent / "
                    f"{_number(result.get('auto_submissions'))} auto",
                ),
                _metric_card(
                    "Codex sessions",
                    _number(len(run.get("session_ids") or [])),
                    ", ".join(run.get("codex_versions") or []) or "version unavailable",
                ),
            ]
        )
    else:
        kpi_cards.extend(
            [
                _metric_card(
                    "Processed tokens",
                    _number(usage.get("processed_tokens")),
                    _text(usage.get("scope")),
                ),
                _metric_card(
                    "API-equivalent cost",
                    _cost(usage.get("cost_usd")),
                    "estimate when complete",
                ),
                _metric_card(
                    "Tool calls",
                    _number(usage.get("tool_calls")),
                    f"{_number(usage.get('assistant_messages'))} assistant messages",
                ),
                _metric_card(
                    "Codex sessions",
                    _number(len(run.get("session_ids") or [])),
                    ", ".join(run.get("codex_versions") or []) or "version unavailable",
                ),
            ]
        )
    kpis = "".join(kpi_cards)
    warning_items = (
        "".join(f"<li>{_html(item)}</li>" for item in warnings)
        or "<li>No evidence warnings.</li>"
    )
    artifact_rows = "".join(
        "<tr>"
        f"<th>{_html(key.replace('_', ' ').title())}</th>"
        f"<td>{_artifact_link(value)}</td>"
        "</tr>"
        for key, value in evidence.items()
    )
    raw_payload = json.dumps(
        observation,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    )
    goal_plus_section = _render_codex_goal_plus_finalization(observation)
    goal_plus_nav = (
        '<a href="#goal-plus-finalization">Goal Plus</a>' if goal_plus_section else ""
    )
    print_icon = (
        '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 '
        '2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect width="12" height="8" x="6" y="14"/></svg>'
    )
    return f"""<!doctype html>
<html lang="en" data-report-schema="codex-observability-report/v{REPORT_DOCUMENT_SCHEMA_VERSION}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Codex Execution Report: {escape(run_id)}</title>
  <script>document.documentElement.classList.add('js');</script>
  <style>{_REPORT_CSS}</style>
</head>
<body data-run-id="{escape(run_id, quote=True)}">
  <header class="masthead">
    <div class="wrap masthead-inner">
      <div class="identity">
        <div class="eyebrow">Codex Execution Report</div>
        <div class="identity-line"><h1>{escape(run_id)}</h1><span class="status {_status_class(terminal_state)}">{escape(terminal_label)}</span></div>
        <div class="id-line mono">Task: {escape(task_id)} · source: {_html(observation.get("source_kind"))}</div>
      </div>
      <div class="masthead-actions">
        <div class="generated">Generated<strong class="mono">{_html(observation.get("generated_at"))}</strong></div>
        <button class="button no-print" type="button" onclick="window.print()" title="Print report">{print_icon}<span>Print</span></button>
      </div>
    </div>
  </header>
  <nav class="section-nav no-print" aria-label="Report sections">
    <div class="wrap">
      <a href="#aggregate">Summary</a><a href="#execution">Execution</a>
      {goal_plus_nav}<a href="#trajectory">Trajectory</a><a href="#hooks">Stop hooks</a>
      <a href="#audit">Audit</a>
    </div>
  </nav>
  <main class="wrap">
    <section id="aggregate" class="report-section">
      <div class="section-kicker">Codex Run Summary</div>
      <div class="kpi-grid">{kpis}</div>
    </section>
    {goal_plus_section}
    <section id="execution" class="report-section">
      <div class="section-kicker">Host Execution Evidence</div>
      {_render_codex_execution_timeline(observation)}
      <div class="panel panel-body">
        <h3>Run Facts</h3>
        <dl class="fact-grid">
          <div class="fact"><dt>Agent</dt><dd>{_html(run.get("agent"))}</dd></div>
          <div class="fact"><dt>Model</dt><dd>{_html(run.get("model"))}</dd></div>
          <div class="fact"><dt>Reasoning effort</dt><dd>{_html(run.get("reasoning_effort"))}</dd></div>
          <div class="fact"><dt>Metric direction</dt><dd>{_html(evaluations.get("metric_direction"))}</dd></div>
          <div class="fact"><dt>Termination reason</dt><dd>{_html(termination_reason.replace("_", " ").title())}</dd></div>
          <div class="fact"><dt>Outcome</dt><dd>{_html(outcome_label)}</dd></div>
          <div class="fact"><dt>Raw timeout flag</dt><dd>{_html(str(timed_out).lower())}</dd></div>
          <div class="fact"><dt>Exploration budget</dt><dd>{_html(_duration(run.get("exploration_budget_seconds")))}</dd></div>
          <div class="fact"><dt>Finalization grace</dt><dd>{_html(_duration(run.get("finalization_grace_seconds")))}</dd></div>
          <div class="fact"><dt>Finalization runtime</dt><dd>{_html(_duration(run.get("finalization_runtime_seconds")))}</dd></div>
        </dl>
      </div>
      <div class="panel panel-body">
        <h3>Usage Evidence</h3>
        <dl class="fact-grid">
          <div class="fact"><dt>Processed tokens</dt><dd>{_html(_number(usage.get("processed_tokens")))}</dd></div>
          <div class="fact"><dt>Input tokens</dt><dd>{_html(_number(usage.get("input_tokens")))}</dd></div>
          <div class="fact"><dt>Cached input tokens</dt><dd>{_html(_number(usage.get("cached_input_tokens")))}</dd></div>
          <div class="fact"><dt>Output tokens</dt><dd>{_html(_number(usage.get("output_tokens")))}</dd></div>
          <div class="fact"><dt>API-equivalent cost</dt><dd>{_html(_cost(usage.get("cost_usd")))}</dd></div>
          <div class="fact"><dt>Tool calls</dt><dd>{_html(_number(usage.get("tool_calls")))}</dd></div>
          <div class="fact"><dt>Assistant messages</dt><dd>{_html(_number(usage.get("assistant_messages")))}</dd></div>
          <div class="fact"><dt>Usage scope</dt><dd>{_html(usage.get("scope"))}</dd></div>
        </dl>
      </div>
    </section>
    <section id="trajectory" class="report-section">
      <div class="section-kicker">Evaluator Evidence</div>
      {trajectory_html}
      <div class="panel panel-body">
        <h3>Submission Evidence</h3>
        {_render_codex_submissions(observation)}
      </div>
    </section>
    <section id="hooks" class="report-section">
      <div class="section-kicker">Codex Host Hook Evidence</div>
      <h2>Stop Hook Activity</h2>
      {hook_activity_html}
      <div class="subsection">
        <h3>Blocked Stop Distribution</h3>
        {stop_progress_html}
      </div>
      <p class="footnote">When a source preserves hook lifecycle counts and output positions but not per-hook timestamps, output-progress bins must not be interpreted as wall-clock time.</p>
    </section>
    <section id="audit" class="report-section">
      <h2>Report Audit</h2>
      <div class="two-column">
        <div class="panel panel-body"><h3>Evidence Warnings</h3><ul class="warning-list">{warning_items}</ul></div>
        <div class="panel panel-body"><h3>Evidence Availability</h3>
          <dl class="fact-grid">
            <div class="fact"><dt>Stop hook events</dt><dd>{_html(availability.get("stop_hook_events"))}</dd></div>
            <div class="fact"><dt>Per-event timestamps</dt><dd>{_html(availability.get("per_event_wall_clock_timestamps"))}</dd></div>
            <div class="fact"><dt>stop_hook_active</dt><dd>{_html(availability.get("stop_hook_active"))}</dd></div>
            <div class="fact"><dt>Token usage</dt><dd>{_html(availability.get("token_usage"))}</dd></div>
            <div class="fact"><dt>Goal Plus state</dt><dd>{_html(availability.get("goal_plus_state"))}</dd></div>
            <div class="fact"><dt>Reason</dt><dd>{_html(availability.get("reason"))}</dd></div>
          </dl>
        </div>
      </div>
      <div class="panel panel-body"><h3>Source Artifacts</h3>
        <div class="table-scroll"><table><tbody>{artifact_rows}</tbody></table></div>
      </div>
      <details class="summary-block"><summary>Complete normalized report data</summary><pre>{escape(raw_payload)}</pre></details>
      <p class="footnote">Schema codex-observability-report/v{REPORT_DOCUMENT_SCHEMA_VERSION}. This file is self-contained and contains normalized evidence only; prompts and assistant messages are excluded.</p>
    </section>
  </main>
  {plotly_script}
  <script>{_REPORT_SCRIPT}</script>
</body>
</html>
"""


def render_report_document(document: dict[str, Any]) -> str:
    if not isinstance(document, dict):
        raise TypeError("report document must be a mapping")
    report_kind = document.get("report_kind")
    data = document.get("data")
    if not isinstance(data, dict):
        raise ValueError("report document data must be a mapping")
    if report_kind == "goal-plus":
        return render_html_report(data)
    if report_kind == "codex-run":
        return render_codex_observability_report(data)
    raise ValueError(f"unsupported report kind: {report_kind!r}")


def _require_terminal_goal_plus_report(data: dict[str, Any]) -> None:
    goal_plus_id = data.get("goal_plus_id")
    if not goal_plus_id:
        return
    snapshot = data.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    goal = snapshot.get("goal_plus")
    goal = goal if isinstance(goal, dict) else {}
    status = str(goal.get("status") or "active")
    if status in {"complete", "blocked", "abandoned"}:
        return
    raise RuntimeError(
        "cannot generate a Goal Plus HTML report before the linked record "
        "reaches a terminal status (complete, blocked, or abandoned); "
        f"current: {goal_plus_id}={status}"
    )


def write_html_report(
    root_dir: Path | str,
    run_id: str,
    output_path: Path | None = None,
) -> Path:
    root = Path(root_dir).resolve()
    destination = output_path or root / "runs" / run_id / "report.html"
    data = build_html_report_data(root, run_id)
    _require_terminal_goal_plus_report(data)
    destination.write_text(
        render_report_document(
            {
                "schema_version": REPORT_DOCUMENT_SCHEMA_VERSION,
                "report_kind": "goal-plus",
                "data": data,
            }
        ),
        encoding="utf-8",
    )
    return destination
