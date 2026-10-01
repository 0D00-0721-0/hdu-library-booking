import argparse
import base64
import hmac
import json
import os
import re
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from instant_book import (
    DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES,
    DEFAULT_BOOK_DAYS,
    DEFAULT_CONFIG,
    DEFAULT_MAX_TRIALS,
    DEFAULT_RETRY_DELAY,
    DEFAULT_HOLD_BEFORE_MINUTES,
    InstantBooker,
    ResultUncertain,
    TaskCancelled,
    build_execute_time,
    build_begin_time,
    cancel_booking_by_id,
    check_in_test_by_id,
    continue_seat_by_id,
    get_current_bookings,
    load_config,
    measure_server_clock,
    normalize_check_in_delay_minutes,
    normalize_execute_at,
    normalize_retry_delay,
    parse_fallback_seats,
    parse_plan,
    run_auto_check_in,
    run_booking,
)


HOST = "127.0.0.1"
PORT = 8765
LOG_DIR = Path(__file__).with_name("logs")
WEB_AUTH_USERNAME = os.environ.get("HDU_WEB_USERNAME", "hdu")
WEB_AUTH_PASSWORD = os.environ.get("HDU_WEB_PASSWORD", "")
JOBS = {}
JOBS_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()


class JobNotFound(ValueError):
    pass


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#183842">
  <title>HDU 图书馆即时预约</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='10' fill='%23256f67'/%3E%3Cpath d='M18 16h28v32H18z' fill='%23fffdf6'/%3E%3Cpath d='M23 24h18M23 32h18M23 40h12' stroke='%23b4762e' stroke-width='4' stroke-linecap='round'/%3E%3C/svg%3E">
  <style>
    :root {
      color-scheme: light;
      --bg: #eaf1f3;
      --panel: #ffffff;
      --panel-soft: #f4f8f9;
      --ink: #18323e;
      --muted: #5d737d;
      --subtle: #80939c;
      --line: #d7e3e7;
      --line-strong: #bfd2d7;
      --primary: #176e75;
      --primary-strong: #10545b;
      --primary-soft: #e8f5f4;
      --warning: #aa7135;
      --warning-soft: #fbf1e4;
      --danger: #b74444;
      --danger-soft: #fceeee;
      --ok: #16704e;
      --log-bg: #132934;
      --log-text: #d8e8e7;
      --shadow: 0 18px 52px rgba(26, 58, 70, 0.07);
      --focus: 0 0 0 3px rgba(23, 110, 117, 0.22);
    }

    * { box-sizing: border-box; }
    html { background: var(--bg); scroll-behavior: smooth; }
    body {
      margin: 0;
      min-width: 320px;
      min-height: 100vh;
      background: linear-gradient(180deg, #f4f8f9 0, var(--bg) 360px);
      color: var(--ink);
      font-family: "Avenir Next", "PingFang SC", "Hiragino Sans GB", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      -webkit-font-smoothing: antialiased;
      -webkit-tap-highlight-color: rgba(23, 110, 117, 0.12);
    }
    button, input, select { font: inherit; }
    .skip-link {
      position: fixed;
      top: 12px;
      left: 12px;
      z-index: 20;
      transform: translateY(-160%);
      border-radius: 8px;
      padding: 10px 14px;
      background: #fff;
      color: var(--ink);
      text-decoration: none;
      font-weight: 700;
    }
    .skip-link:focus-visible { transform: none; box-shadow: var(--focus); }
    .app { min-height: 100vh; }
    .topbar { background: #183842; color: #f5fbfb; }
    .bar {
      max-width: 1360px;
      margin: 0 auto;
      padding: 24px 32px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
    }
    .brand { display: flex; align-items: center; gap: 16px; min-width: 0; }
    .brand-mark {
      width: 46px;
      height: 46px;
      flex: 0 0 auto;
      display: grid;
      place-items: center;
      border: 1px solid rgba(255,255,255,0.32);
      border-radius: 13px;
      color: #f1d59f;
      font: 700 23px/1 "Songti SC", "STSong", serif;
    }
    .kicker, .eyebrow {
      margin: 0;
      font: 750 11px/1.3 "Avenir Next", "SFMono-Regular", monospace;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }
    .kicker { color: #8bd0c9; }
    h1 { margin: 4px 0 0; font-size: clamp(20px, 2.6vw, 27px); font-weight: 750; letter-spacing: 0.015em; line-height: 1.25; }
    .top-tools { display: flex; align-items: center; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
    .clock, .status {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 37px;
      padding: 8px 13px;
      border: 1px solid rgba(255,255,255,0.2);
      border-radius: 999px;
      background: rgba(255,255,255,0.07);
      color: #dcebea;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .clock { font: 700 13px/1 "SFMono-Regular", Menlo, monospace; font-variant-numeric: tabular-nums; }
    .status::before { content: ""; width: 7px; height: 7px; margin-right: 8px; border-radius: 50%; background: #a8c3c4; }
    .status.running { color: #bcf1e4; border-color: rgba(139,208,201,0.38); }
    .status.running::before { background: #6ee7bb; box-shadow: 0 0 0 4px rgba(110,231,187,0.13); }
    .status.error { color: #ffd5cf; border-color: rgba(255,173,162,0.35); }
    .status.error::before { background: #ff9b8e; }

    main {
      width: 100%;
      max-width: 1360px;
      margin: 0 auto;
      padding: 28px 32px 54px;
      display: grid;
      grid-template-columns: minmax(0, 1.18fr) minmax(360px, 0.82fr);
      gap: 24px;
      align-items: start;
    }
    .command-pane, .log-panel {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }
    .command-pane { overflow: hidden; }
    .pane-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 26px 28px 18px;
    }
    .eyebrow { color: var(--primary); }
    .pane-head h2, .log-head h2 { margin: 4px 0 0; font-size: 23px; line-height: 1.25; font-weight: 760; letter-spacing: 0.01em; }
    .seat-stamp {
      display: flex;
      align-items: baseline;
      gap: 8px;
      padding: 8px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel-soft);
      white-space: nowrap;
    }
    .seat-stamp span { color: var(--muted); font-size: 11px; font-weight: 700; }
    .seat-stamp strong { color: var(--primary-strong); font: 800 21px/1 "SFMono-Regular", Menlo, monospace; font-variant-numeric: tabular-nums; }
    .plan-strip {
      min-height: 45px;
      margin: 0 28px 24px;
      padding: 11px 14px;
      border-radius: 10px;
      border-left: 3px solid var(--primary);
      background: var(--primary-soft);
      color: var(--primary-strong);
      font-size: 13px;
      font-weight: 680;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }
    .form-section { border-top: 1px solid var(--line); padding: 24px 28px; }
    .section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
    .section-head h3 { margin: 0; font-size: 16px; font-weight: 760; }
    .section-head p { margin: 0; color: var(--subtle); font-size: 12px; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 18px; }
    .field { display: grid; align-content: start; gap: 7px; min-width: 0; }
    .field > label, .field-label { color: #4b626d; font-size: 12px; font-weight: 700; line-height: 1.35; }
    .wide { grid-column: 1 / -1; }
    input:not([type="radio"]):not([type="checkbox"]), select {
      width: 100%;
      height: 44px;
      border: 1px solid var(--line-strong);
      border-radius: 9px;
      padding: 0 12px;
      background: #fff;
      color: var(--ink);
      font-size: 14px;
      outline: none;
      font-variant-numeric: tabular-nums;
      transition: border-color 140ms ease, box-shadow 140ms ease;
    }
    input::placeholder { color: #9aacb2; }
    input:focus-visible, select:focus-visible, button:focus-visible, summary:focus-visible, .segmented input:focus-visible + span, .switch input:focus-visible + span {
      border-color: var(--primary);
      outline: none;
      box-shadow: var(--focus);
    }
    input:disabled, select:disabled { opacity: 0.65; cursor: not-allowed; background: var(--panel-soft); }
    .segmented { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; padding: 4px; border-radius: 10px; background: #eaf1f2; }
    .segmented input, .switch input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
    .segmented span {
      display: flex;
      height: 37px;
      align-items: center;
      justify-content: center;
      border: 1px solid transparent;
      border-radius: 7px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      user-select: none;
    }
    .segmented input:checked + span { border-color: var(--line); background: #fff; color: var(--primary-strong); box-shadow: 0 2px 5px rgba(24,50,62,0.06); }
    .segmented input:disabled + span, .switch input:disabled + span { cursor: not-allowed; opacity: 0.6; }
    .switch { display: block; }
    .switch span {
      min-height: 56px;
      display: grid;
      align-content: center;
      gap: 3px;
      padding: 9px 13px 9px 42px;
      border: 1px solid var(--line-strong);
      border-radius: 10px;
      background: #fff;
      cursor: pointer;
      position: relative;
    }
    .switch span::before { content: ""; position: absolute; left: 13px; top: 18px; width: 17px; height: 17px; border: 1.5px solid #8ca6ad; border-radius: 5px; }
    .switch input:checked + span { border-color: var(--primary); background: var(--primary-soft); }
    .switch input:checked + span::before { content: "✓"; display: grid; place-items: center; border-color: var(--primary); background: var(--primary); color: #fff; font-size: 12px; line-height: 1; }
    .switch strong { font-size: 13px; line-height: 1.2; }
    .switch small { color: var(--muted); font-size: 11px; line-height: 1.3; }
    .action-area { margin-top: 20px; padding: 17px; border: 1px solid #d4e7e6; border-radius: 13px; background: #f1f8f8; }
    .primary-actions, .support-actions, .booking-actions, .config-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
    .primary-actions { margin-top: 12px; }
    .support-actions { margin-top: 9px; }
    .booking-actions { margin-top: 12px; }
    .booking-actions .full { grid-column: 1 / -1; }
    button {
      min-height: 41px;
      border: 1px solid var(--line-strong);
      border-radius: 9px;
      padding: 8px 12px;
      background: #fff;
      color: var(--ink);
      font-size: 13px;
      font-weight: 700;
      line-height: 1.25;
      cursor: pointer;
      touch-action: manipulation;
      transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
    }
    button:hover:not(:disabled) { border-color: var(--primary); background: var(--primary-soft); transform: translateY(-1px); }
    button.primary { border-color: var(--primary); background: var(--primary); color: #fff; }
    button.primary:hover:not(:disabled) { background: var(--primary-strong); }
    .primary-actions button { min-height: 47px; font-size: 14px; }
    button.danger { border-color: #eac5c3; color: var(--danger); background: #fff; }
    button.danger:hover:not(:disabled) { border-color: var(--danger); background: var(--danger-soft); }
    button.brass { border-color: #e5d2b9; color: #8b592c; background: #fffaf3; }
    button.brass:hover:not(:disabled) { border-color: var(--warning); background: var(--warning-soft); }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .notice { min-height: 18px; margin-top: 11px; color: var(--muted); font-size: 13px; line-height: 1.45; }
    .notice.error { color: var(--danger); }
    .notice.ok { color: var(--ok); }
    .clock-result { margin-top: 12px; padding: 12px 14px; border: 1px solid #cce1de; border-radius: 9px; background: var(--primary-soft); color: var(--primary-strong); font-size: 13px; line-height: 1.55; white-space: pre-line; }
    .clock-result.error { border-color: #eac5c3; background: var(--danger-soft); color: var(--danger); }
    .clock-result[hidden] { display: none; }
    .advanced { border-top: 1px solid var(--line); }
    .advanced summary { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 20px 28px; color: var(--ink); font-size: 14px; font-weight: 740; cursor: pointer; list-style: none; }
    .advanced summary::-webkit-details-marker { display: none; }
    .advanced summary::after { content: "+"; color: var(--primary); font: 500 24px/1 "Avenir Next", sans-serif; }
    .advanced[open] summary::after { content: "−"; }
    .advanced-inner { padding: 0 28px 25px; }
    .config-actions { margin-top: 15px; }
    .booking-meta { min-height: 42px; padding: 11px 12px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel-soft); color: var(--muted); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
    .map-card { margin: 19px 0 20px; border: 1px solid var(--line); border-radius: 13px; overflow: hidden; background: #f8fbfb; }
    .map-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; padding: 13px 14px; border-bottom: 1px solid var(--line); }
    .map-head strong { display: block; font-size: 14px; }
    .map-head small { display: block; margin-top: 3px; color: var(--muted); font-size: 11px; line-height: 1.4; }
    .map-tools { display: flex; align-items: center; gap: 6px; }
    .map-tools button { min-height: 33px; padding: 5px 10px; font-size: 12px; }
    .map-zoom { min-width: 42px; text-align: center; color: var(--muted); font: 700 11px/1 ui-monospace, monospace; }
    .map-mode { display: flex; gap: 7px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
    .map-mode button { min-height: 34px; padding: 6px 12px; font-size: 12px; }
    .map-mode button[aria-pressed="true"] { border-color: var(--primary); background: var(--primary); color: #fff; }
    .map-viewport { position: relative; height: min(53vh, 430px); min-height: 290px; overflow: auto; background: #fff; overscroll-behavior: contain; }
    .map-viewport svg { display: block; max-width: none; }
    .map-seat { cursor: pointer; }
    .map-seat rect { fill: #e7f3f0; stroke: #328c83; stroke-width: .13; }
    .map-seat.busy rect { fill: #e8edef; stroke: #9badb1; }
    .map-seat.locked rect { fill: #fcf0da; stroke: #bb8739; }
    .map-seat.closed rect { fill: #f4e9e4; stroke: #bd8a71; }
    .map-seat.unknown rect { fill: #eff4f5; stroke: #6f8f96; }
    .map-seat.primary rect { fill: #146d73; stroke: #0b4c55; stroke-width: .23; }
    .map-seat.fallback rect { fill: #f2cd7e; stroke: #a46d22; stroke-width: .23; }
    .map-seat text { fill: #143a42; font: 700 1.05px/1 ui-monospace, monospace; pointer-events: none; user-select: none; }
    .map-seat.primary text { fill: #fff; }
    .map-seat:focus-visible rect { stroke: #e78e35; stroke-width: .35; }
    .map-seat:hover rect { stroke-width: .3; }
    .map-empty { display: grid; place-items: center; min-height: 290px; padding: 24px; color: var(--muted); text-align: center; font-size: 13px; line-height: 1.5; }
    .map-legend { display: flex; align-items: center; flex-wrap: wrap; gap: 7px 15px; padding: 10px 14px 4px; color: var(--muted); font-size: 11px; }
    .map-legend span { display: inline-flex; align-items: center; gap: 5px; }
    .map-dot { width: 12px; height: 12px; border: 1px solid #328c83; border-radius: 3px; background: #e7f3f0; }
    .map-dot.busy { border-color: #9badb1; background: #e8edef; }
    .map-dot.locked { border-color: #bb8739; background: #fcf0da; }
    .map-dot.closed { border-color: #bd8a71; background: #f4e9e4; }
    .map-dot.primary { border-color: #0b4c55; background: #146d73; }
    .map-dot.fallback { border-color: #a46d22; background: #f2cd7e; }
    .map-message { min-height: 31px; margin: 0; padding: 6px 14px 12px; color: var(--muted); font-size: 11px; line-height: 1.5; }
    .map-message.error { color: var(--danger); }

    .observer { display: grid; gap: 18px; min-width: 0; }
    .ticket {
      position: relative;
      overflow: hidden;
      border-radius: 18px;
      padding: 25px 27px 23px;
      background: #193d47;
      color: #fff;
      box-shadow: 0 21px 47px rgba(24, 56, 66, 0.2);
    }
    .ticket::before { content: ""; position: absolute; top: -90px; right: -85px; width: 260px; height: 260px; border: 1px solid rgba(255,255,255,0.1); border-radius: 50%; box-shadow: 0 0 0 50px rgba(255,255,255,0.025), 0 0 0 100px rgba(255,255,255,0.02); pointer-events: none; }
    .ticket-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; position: relative; }
    .ticket-head .eyebrow { color: #8bd0c9; }
    .ticket-tag { border: 1px solid rgba(255,255,255,0.25); border-radius: 999px; padding: 6px 10px; color: #e9f5f3; font-size: 11px; font-weight: 700; }
    .summary-grid { display: grid; grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr); gap: 18px; margin-top: 35px; position: relative; }
    .metric-card { min-width: 0; }
    .metric-card + .metric-card { border-left: 1px solid rgba(255,255,255,0.2); padding-left: 19px; }
    .metric-card span { display: block; margin-bottom: 8px; color: #a4c3c4; font-size: 12px; font-weight: 650; }
    .metric-card strong { display: block; overflow: hidden; color: #fff; font: 750 clamp(25px, 2.2vw, 31px)/1.1 "Avenir Next", "SFMono-Regular", Menlo, monospace; font-variant-numeric: tabular-nums; text-overflow: ellipsis; white-space: nowrap; }
    .metric-card:first-child strong { font-size: clamp(49px, 5vw, 68px); letter-spacing: -0.05em; }
    .metric-card small { display: block; overflow: hidden; margin-top: 8px; color: #b6cdcd; font-size: 12px; line-height: 1.4; text-overflow: ellipsis; white-space: nowrap; }
    .ticket-divider { position: relative; margin: 28px -27px 20px; border-top: 1px dashed rgba(219,240,238,0.39); }
    .ticket-divider::before, .ticket-divider::after { content: ""; position: absolute; top: -10px; width: 20px; height: 20px; border-radius: 50%; background: var(--bg); }
    .ticket-divider::before { left: -10px; }
    .ticket-divider::after { right: -10px; }
    .timeline-panel { position: relative; }
    .timeline-panel .eyebrow { color: #8bd0c9; }
    .timeline { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin-top: 16px; }
    .tick { display: grid; gap: 5px; min-width: 0; }
    .tick strong { overflow: hidden; color: #f5fbfb; font: 750 clamp(19px, 1.75vw, 25px)/1.1 "Avenir Next", "SFMono-Regular", Menlo, monospace; font-variant-numeric: tabular-nums; text-overflow: ellipsis; white-space: nowrap; }
    .tick span { color: #a4c3c4; font-size: 12px; }
    .log-panel { padding: 22px; }
    .log-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 17px; }
    .log-head h2 { font-size: 19px; }
    .log-head button { min-height: 35px; font-size: 12px; }
    pre { min-height: 300px; max-height: 54vh; margin: 0; overflow: auto; border-radius: 10px; padding: 17px; background: var(--log-bg); color: var(--log-text); font: 12px/1.6 ui-monospace, "SFMono-Regular", Menlo, monospace; font-variant-numeric: tabular-nums; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
    pre:empty::before { content: "日志会在任务开始后写入。"; color: #91abb1; }

    @media (max-width: 1030px) {
      main { grid-template-columns: minmax(0, 1fr); max-width: 760px; }
    }
    @media (max-width: 620px) {
      .bar { align-items: flex-start; flex-direction: column; padding: 20px 18px; gap: 17px; }
      .top-tools { width: 100%; justify-content: flex-start; }
      main { padding: 16px 12px 36px; gap: 15px; }
      .pane-head { padding: 21px 19px 15px; }
      .pane-head h2 { font-size: 21px; }
      .plan-strip { margin: 0 19px 20px; }
      .form-section { padding: 21px 19px; }
      .grid { gap: 14px 11px; }
      .section-head { flex-direction: column; gap: 3px; }
      .advanced summary { padding: 19px; }
      .advanced-inner { padding: 0 19px 21px; }
      .ticket { padding: 22px; }
      .ticket-divider { margin-left: -22px; margin-right: -22px; }
      .summary-grid { gap: 10px; }
      .metric-card + .metric-card { padding-left: 12px; }
      .metric-card strong { font-size: 24px; }
      .metric-card:first-child strong { font-size: 52px; }
      .log-panel { padding: 18px; }
      pre { min-height: 220px; max-height: 320px; }
    }
    @media (max-width: 390px) {
      .grid { grid-template-columns: 1fr; }
      .summary-grid { grid-template-columns: 1fr; gap: 16px; margin-top: 25px; }
      .metric-card + .metric-card { border-left: 0; border-top: 1px solid rgba(255,255,255,0.2); padding: 14px 0 0; }
      .tick strong { font-size: 16px; }
      .primary-actions, .support-actions, .booking-actions { grid-template-columns: 1fr; }
      .booking-actions .full { grid-column: auto; }
    }
    @media (prefers-reduced-motion: reduce) {
      html { scroll-behavior: auto; }
      button, input, select { transition: none; }
      button:hover:not(:disabled) { transform: none; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#mainContent">跳到主内容</a>
  <div class="app">
    <header class="topbar">
      <div class="bar">
        <div class="brand">
          <div class="brand-mark" aria-hidden="true">座</div>
          <div>
            <div class="kicker">HDU Library / Seat Desk</div>
            <h1>图书馆预约控制台</h1>
          </div>
        </div>
        <div class="top-tools">
          <div id="clock" class="clock" aria-label="当前时间">--:--:--</div>
          <div id="status" class="status" role="status" aria-live="polite">就绪</div>
        </div>
      </div>
    </header>
    <main id="mainContent">
      <section class="command-pane" aria-labelledby="controlTitle">
        <div class="pane-head">
          <div>
            <p class="eyebrow">Reservation plan</p>
            <h2 id="controlTitle">安排下一次预约</h2>
          </div>
          <div class="seat-stamp" aria-label="目标座位"><span>座位</span><strong id="seatBadge">--</strong></div>
        </div>
        <div id="planSummary" class="plan-strip">载入配置后显示预约计划</div>

        <section class="form-section" aria-labelledby="seatSectionTitle">
          <div class="section-head"><h3 id="seatSectionTitle">座位与时间</h3><p>先选目标座位，再确认预约时段</p></div>
          <div class="grid">
            <div class="field wide">
              <label for="floorId">楼层 / 区域</label>
              <select id="floorId" name="floor_id" required>
                <option value="">请选择楼层</option>
                <option value="1557">二楼东 · 格物E堂</option>
                <option value="1524">二楼西 · 比特庭园</option>
                <option value="1554">二楼信息检索室 · 数智渊阁</option>
                <option value="1558">四楼 · 宋韵云图</option>
                <option value="1559">六楼 · 杭韵数阁</option>
                <option value="1543">十二楼 · 芯灵驿站</option>
              </select>
            </div>
            <div class="field"><label for="seatNum">座位号</label><input id="seatNum" name="seat_num" type="number" min="1" step="1" autocomplete="off"></div>
            <div class="field"><label for="fallbackSeats">备选座位</label><input id="fallbackSeats" name="fallback_seats" type="text" inputmode="numeric" autocomplete="off" placeholder="例如 22,23"></div>
            <div class="field"><label for="startHour">开始小时</label><input id="startHour" name="start_hour" type="number" min="0" max="23" step="1" autocomplete="off"></div>
            <div class="field"><label for="durationHours">时长（小时）</label><input id="durationHours" name="duration_hours" type="number" min="1" max="24" step="1" autocomplete="off"></div>
            <div class="field wide"><div class="field-label">预约日期</div>
              <div class="segmented" id="dayGroup">
                <label><input type="radio" name="days" value="0"><span>今天</span></label>
                <label><input type="radio" name="days" value="1"><span>明天</span></label>
                <label><input type="radio" name="days" value="2"><span>后天</span></label>
              </div>
            </div>
          </div>
          <div class="map-card" aria-labelledby="mapTitle">
            <div class="map-head">
              <div><strong id="mapTitle">座位位置图</strong><small>官方平面图 · 点击座位填入号码</small></div>
              <div class="map-tools">
                <button id="refreshMapBtn" type="button">刷新</button>
                <button id="zoomOutBtn" type="button" aria-label="缩小座位图">−</button>
                <span id="mapZoom" class="map-zoom">100%</span>
                <button id="zoomInBtn" type="button" aria-label="放大座位图">+</button>
              </div>
            </div>
            <div class="map-mode" aria-label="点选方式">
              <button id="pickPrimaryBtn" type="button" aria-pressed="true">选主座位</button>
              <button id="pickFallbackBtn" type="button" aria-pressed="false">添加备选</button>
            </div>
            <div id="mapViewport" class="map-viewport" aria-label="可滚动座位平面图">
              <div id="mapEmpty" class="map-empty">选择楼层和预约时段后显示座位位置图</div>
            </div>
            <div class="map-legend" aria-label="座位图图例">
              <span><i class="map-dot"></i>可用</span><span><i class="map-dot busy"></i>已占用</span><span><i class="map-dot locked"></i>锁定</span><span><i class="map-dot closed"></i>关闭</span><span><i class="map-dot primary"></i>主座位</span><span><i class="map-dot fallback"></i>备选</span>
            </div>
            <p id="mapMessage" class="map-message" role="status" aria-live="polite"></p>
          </div>
          <div class="grid">
            <div class="field wide"><label for="executeAt">定时提交时间</label><input id="executeAt" name="execute_at" type="text" inputmode="decimal" autocomplete="off" placeholder="例如 20:00:00.500"></div>
            <div class="field wide"><label for="holdBeforeMinutes">提前预留主座位（分钟；0 为关闭，最多 14）</label><input id="holdBeforeMinutes" name="hold_before_minutes" type="number" min="0" max="14" step="1" autocomplete="off"></div>
          </div>
          <div class="action-area">
            <label class="switch"><input id="dryRun" type="checkbox"><span><strong>只测试，不提交</strong><small>检查座位和请求流程</small></span></label>
            <div class="primary-actions">
              <button id="instantRunBtn" class="primary" type="button">立即预约</button>
              <button id="runBtn" type="button">定时预约</button>
            </div>
            <div class="support-actions">
              <button id="measureClockBtn" type="button">测量并推荐时间</button>
              <button id="cancelBtn" class="danger" type="button" disabled>停止任务</button>
            </div>
            <div id="clockResult" class="clock-result" role="status" aria-live="polite" hidden></div>
            <div id="notice" class="notice" role="status" aria-live="polite"></div>
          </div>
        </section>

        <section class="form-section" aria-labelledby="bookingSectionTitle">
          <div class="section-head"><h3 id="bookingSectionTitle">当前预约</h3><button id="refreshBookingsBtn" type="button">刷新预约</button></div>
          <div class="field"><label for="bookingSelect">选择预约</label><select id="bookingSelect"></select><div id="bookingMeta" class="booking-meta">刷新当前预约后显示签到窗口和状态</div></div>
          <div class="booking-actions">
            <button id="autoCheckInBtn" class="brass" type="button" disabled>自动签到</button>
            <button id="continueSeatBtn" class="brass" type="button" disabled>续座</button>
            <button id="checkInTestBtn" type="button" disabled>签到测试</button>
            <button id="cancelBookingBtn" class="danger" type="button" disabled>取消预约</button>
          </div>
        </section>

        <details class="advanced">
          <summary>高级设置与配置文件</summary>
          <div class="advanced-inner">
            <div class="grid">
              <div class="field wide"><label for="configPath">配置文件</label><input id="configPath" name="config_path" autocomplete="off" spellcheck="false"></div>
              <div class="field"><label for="roomType">房间类型</label><input id="roomType" name="room_type" type="number" min="1" step="1" autocomplete="off"></div>
              <div class="field"><label for="maxTrials">重试次数</label><input id="maxTrials" name="max_trials" type="number" min="1" max="20" step="1" autocomplete="off"></div>
              <div class="field wide"><label for="retryDelay">响应后重试 / 切换间隔（至少 3 秒）</label><input id="retryDelay" name="retry_delay" type="number" min="3" max="10" step="0.1" autocomplete="off"></div>
            </div>
            <div class="config-actions"><button id="loadBtn" type="button">载入配置</button><button id="saveBtn" type="button">保存计划</button></div>
          </div>
        </details>
      </section>

      <section class="observer" aria-label="执行状态">
        <div class="ticket" aria-label="预约概览">
          <div class="ticket-head"><p class="eyebrow">Seat ticket</p><span class="ticket-tag">预约概览</span></div>
          <div class="summary-grid" aria-label="关键状态">
            <div class="metric-card"><span>目标座位</span><strong id="seatMetric">--</strong><small id="floorMetric">楼层 --</small></div>
            <div class="metric-card"><span>预约时段</span><strong id="timeMetric">--:-- — --:--</strong><small id="submitMetric">提交时间 --</small></div>
          </div>
          <div class="ticket-divider" aria-hidden="true"></div>
          <div class="timeline-panel">
            <p class="eyebrow">Time markers</p>
            <div class="timeline" aria-label="预约时间轨">
              <div class="tick"><strong id="railStart">--:--</strong><span>预约开始</span></div>
              <div class="tick"><strong id="railSubmit">--:--</strong><span>提交请求</span></div>
            </div>
          </div>
        </div>
        <div class="log-panel">
          <div class="log-head"><div><p class="eyebrow">Activity</p><h2>执行日志</h2></div><button id="clearBtn" type="button">清空日志</button></div>
          <pre id="logBox" aria-live="polite"></pre>
        </div>
      </section>
    </main>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    const fields = {
      configPath: $("configPath"),
      roomType: $("roomType"),
      floorId: $("floorId"),
      seatNum: $("seatNum"),
      fallbackSeats: $("fallbackSeats"),
      startHour: $("startHour"),
      durationHours: $("durationHours"),
      executeAt: $("executeAt"),
      holdBeforeMinutes: $("holdBeforeMinutes"),
      maxTrials: $("maxTrials"),
      retryDelay: $("retryDelay"),
      dryRun: $("dryRun"),
      bookingSelect: $("bookingSelect"),
      bookingMeta: $("bookingMeta"),
      seatBadge: $("seatBadge"),
      seatMetric: $("seatMetric"),
      floorMetric: $("floorMetric"),
      timeMetric: $("timeMetric"),
      submitMetric: $("submitMetric"),
      planSummary: $("planSummary"),
      railStart: $("railStart"),
      railSubmit: $("railSubmit"),
      clock: $("clock"),
      logBox: $("logBox"),
      notice: $("notice"),
      status: $("status"),
      loadBtn: $("loadBtn"),
      saveBtn: $("saveBtn"),
      instantRunBtn: $("instantRunBtn"),
      runBtn: $("runBtn"),
      cancelBtn: $("cancelBtn"),
      refreshBookingsBtn: $("refreshBookingsBtn"),
      measureClockBtn: $("measureClockBtn"),
      clockResult: $("clockResult"),
      cancelBookingBtn: $("cancelBookingBtn"),
      checkInTestBtn: $("checkInTestBtn"),
      autoCheckInBtn: $("autoCheckInBtn"),
      continueSeatBtn: $("continueSeatBtn"),
      clearBtn: $("clearBtn"),
      mapViewport: $("mapViewport"),
      mapMessage: $("mapMessage"),
      mapZoom: $("mapZoom"),
      refreshMapBtn: $("refreshMapBtn"),
      zoomOutBtn: $("zoomOutBtn"),
      zoomInBtn: $("zoomInBtn"),
      pickPrimaryBtn: $("pickPrimaryBtn"),
      pickFallbackBtn: $("pickFallbackBtn"),
    };
    const formControls = [
      fields.configPath,
      fields.roomType,
      fields.floorId,
      fields.seatNum,
      fields.fallbackSeats,
      fields.startHour,
      fields.durationHours,
      fields.executeAt,
      fields.holdBeforeMinutes,
      fields.maxTrials,
      fields.retryDelay,
      fields.dryRun,
      fields.bookingSelect,
      ...document.querySelectorAll("input[name='days']"),
    ];

    let pollTimer = null;
    let pollFailures = 0;
    let currentJobId = "";
    let currentBookings = [];
    let busyState = false;
    let clockMeasuring = false;
    let seatMapData = null;
    let mapRequestId = 0;
    let mapZoom = 1;
    let mapMode = "primary";

    function setMapMessage(message, error = false) {
      fields.mapMessage.textContent = message;
      fields.mapMessage.className = `map-message${error ? " error" : ""}`;
    }

    function fallbackNumbers() {
      return fields.fallbackSeats.value.split(/[,，\s]+/).map((value) => value.trim()).filter(Boolean);
    }

    function mapSeatStatus(state, exact) {
      if (!exact) return { cls: "unknown", label: "状态未查询" };
      if (state === "0") return { cls: "available", label: "可用" };
      if (state === "1") return { cls: "busy", label: "已占用" };
      if (state === "2" || state === "4") return { cls: "locked", label: "锁定" };
      if (state === "3") return { cls: "closed", label: "关闭" };
      return { cls: "unknown", label: "状态未知" };
    }

    function setMapMode(mode) {
      mapMode = mode;
      fields.pickPrimaryBtn.setAttribute("aria-pressed", String(mode === "primary"));
      fields.pickFallbackBtn.setAttribute("aria-pressed", String(mode === "fallback"));
    }

    function chooseMapSeat(number) {
      if (busyState) return;
      const currentPrimary = fields.seatNum.value.trim();
      const fallbacks = fallbackNumbers();
      const item = seatMapData && seatMapData.seats.find((seat) => seat.number === number);
      const status = mapSeatStatus(item && item.state, seatMapData && seatMapData.availability_exact);
      const warning = ["busy", "locked", "closed"].includes(status.cls)
        ? `；当前时段显示${status.label}，请留意状态变化` : "";
      if (mapMode === "primary") {
        fields.seatNum.value = number;
        fields.fallbackSeats.value = fallbacks.filter((item) => item !== number).join(",");
        setMapMessage(`已选 ${number} 座为主座位${warning}`);
      } else if (number === currentPrimary) {
        setMapMessage(`${number} 座已经是主座位`);
        return;
      } else if (fallbacks.includes(number)) {
        fields.fallbackSeats.value = fallbacks.filter((item) => item !== number).join(",");
        setMapMessage(`已移除备选 ${number} 座`);
      } else if (fallbacks.length >= 5) {
        setMapMessage("备选座位最多选择 5 个", true);
        return;
      } else {
        fields.fallbackSeats.value = [...fallbacks, number].join(",");
        setMapMessage(`已添加备选 ${number} 座${warning}`);
      }
      updatePlanSummary();
      renderSeatMap();
    }

    function renderSeatMap(centerSelection = false) {
      if (!seatMapData) return;
      const viewport = fields.mapViewport;
      const previousLeft = viewport.scrollLeft;
      const previousTop = viewport.scrollTop;
      const ns = "http://www.w3.org/2000/svg";
      const make = (tag) => document.createElementNS(ns, tag);
      const map = seatMapData;
      const svg = make("svg");
      const pixelsPerUnit = 12 * mapZoom;
      svg.setAttribute("viewBox", `0 0 ${map.width} ${map.height}`);
      svg.setAttribute("width", String(map.width * pixelsPerUnit));
      svg.setAttribute("height", String(map.height * pixelsPerUnit));
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", `${map.floor_name}座位位置图，${map.seats.length}个座位`);
      if (map.plan_url) {
        const plan = make("image");
        plan.setAttribute("href", map.plan_url);
        plan.setAttribute("x", "0"); plan.setAttribute("y", "0");
        plan.setAttribute("width", String(map.width));
        plan.setAttribute("height", String(map.height));
        plan.setAttribute("preserveAspectRatio", "none");
        svg.appendChild(plan);
      }
      const primary = fields.seatNum.value.trim();
      const backups = new Set(fallbackNumbers());
      let selected = null;
      map.seats.forEach((seat) => {
        const group = make("g");
        const status = mapSeatStatus(seat.state, map.availability_exact);
        const role = seat.number === primary ? "primary" : backups.has(seat.number) ? "fallback" : status.cls;
        group.setAttribute("class", `map-seat ${role}`);
        group.setAttribute("tabindex", "0");
        group.setAttribute("role", "button");
        group.setAttribute("aria-label", `${seat.number} 座，${status.label}${role === "primary" ? "，主座位" : role === "fallback" ? "，备选座位" : ""}`);
        const rect = make("rect");
        rect.setAttribute("x", String(seat.x)); rect.setAttribute("y", String(seat.y));
        rect.setAttribute("width", String(Math.max(seat.w, 1.7)));
        rect.setAttribute("height", String(Math.max(seat.h, 1.7)));
        rect.setAttribute("rx", ".22");
        group.appendChild(rect);
        const label = make("text");
        label.setAttribute("x", String(seat.x + Math.max(seat.w, 1.7) / 2));
        label.setAttribute("y", String(seat.y + Math.max(seat.h, 1.7) / 2 + .36));
        label.setAttribute("text-anchor", "middle");
        label.textContent = seat.number;
        group.appendChild(label);
        group.addEventListener("click", () => chooseMapSeat(seat.number));
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault(); chooseMapSeat(seat.number);
          }
        });
        svg.appendChild(group);
        if (seat.number === primary) selected = seat;
      });
      viewport.replaceChildren(svg);
      if (centerSelection && selected) {
        viewport.scrollLeft = Math.max(0, (selected.x + selected.w / 2) * pixelsPerUnit - viewport.clientWidth / 2);
        viewport.scrollTop = Math.max(0, (selected.y + selected.h / 2) * pixelsPerUnit - viewport.clientHeight / 2);
      } else {
        viewport.scrollLeft = previousLeft;
        viewport.scrollTop = previousTop;
      }
      fields.mapZoom.textContent = `${Math.round(mapZoom * 100)}%`;
    }

    async function loadSeatMap() {
      const requestId = ++mapRequestId;
      const floorId = fields.floorId.value.trim();
      const start = Number(fields.startHour.value);
      const duration = Number(fields.durationHours.value);
      if (!floorId || !fields.startHour.value || !fields.durationHours.value ||
          !Number.isInteger(start) || !Number.isInteger(duration) || start < 0 ||
          start > 23 || duration < 1 || start + duration > 24) {
        seatMapData = null;
        fields.mapViewport.innerHTML = '<div class="map-empty">请选择楼层和有效预约时段</div>';
        setMapMessage("");
        return;
      }
      fields.mapViewport.innerHTML = '<div class="map-empty">正在读取官方座位图…</div>';
      setMapMessage("");
      try {
        const params = new URLSearchParams({
          path: fields.configPath.value.trim(), room_type: fields.roomType.value.trim(),
          floor_id: floorId, days: String(selectedDays()),
          start_hour: String(start), duration_hours: String(duration),
        });
        const data = await requestJson(`/api/seat-map?${params}`, {}, 12000);
        if (requestId !== mapRequestId) return;
        seatMapData = data;
        mapZoom = 1;
        renderSeatMap(true);
        setMapMessage(data.availability_exact
          ? `${data.floor_name} · ${data.seats.length} 座 · ${selectedDayText()} ${hourText(start)}-${hourText(start + duration)} 的状态，${data.fetched_at} 更新。状态会变化，提交时以接口结果为准。`
          : `${data.floor_name} · ${data.seats.length} 座 · 目标时段尚无座位状态，当前位置来自其他可查询时段；点击可选座。`);
      } catch (error) {
        if (requestId !== mapRequestId) return;
        seatMapData = null;
        fields.mapViewport.innerHTML = '<div class="map-empty">座位图暂时无法显示，可继续手动填写座位号</div>';
        setMapMessage(`读取座位图失败：${error.message}`, true);
      }
    }

    function numberValue(field, fallback = 0) {
      const parsed = Number(field.value);
      return Number.isFinite(parsed) ? parsed : fallback;
    }

    function hourText(hour) {
      if (!Number.isFinite(hour)) return "--:--";
      const normalized = Math.max(0, Math.min(24, Math.round(hour)));
      return `${String(normalized).padStart(2, "0")}:00`;
    }

    function selectedDayText() {
      const days = selectedDays();
      if (days === 0) return "今天";
      if (days === 1) return "明天";
      return "后天";
    }

    function selectedDays() {
      const item = document.querySelector("input[name='days']:checked");
      return item ? Number(item.value) : 1;
    }

    function setDays(days) {
      const item = document.querySelector(`input[name='days'][value="${days}"]`);
      (item || document.querySelector("input[name='days'][value='1']")).checked = true;
      updatePlanSummary();
    }

    function updateClock() {
      const now = new Date();
      fields.clock.textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
    }

    function updatePlanSummary() {
      const seat = fields.seatNum.value.trim() || "--";
      const fallback = fields.fallbackSeats.value.trim();
      const floor = fields.floorId.value
        ? fields.floorId.selectedOptions[0].textContent : "--";
      const start = numberValue(fields.startHour, NaN);
      const duration = numberValue(fields.durationHours, NaN);
      const end = Number.isFinite(start) && Number.isFinite(duration) ? start + duration : NaN;
      const executeAt = fields.executeAt.value.trim() || "立即";
      const holdMinutes = Number(fields.holdBeforeMinutes.value) || 0;

      fields.seatBadge.textContent = seat;
      fields.seatMetric.textContent = seat;
      fields.floorMetric.textContent = floor === "--" ? "楼层 --" : floor;
      fields.timeMetric.textContent = `${hourText(start)}-${hourText(end)}`;
      fields.submitMetric.textContent = `${executeAt} 提交`;
      const fallbackText = fallback ? ` · 备选 ${fallback}` : "";
      const holdText = holdMinutes > 0 && executeAt !== "立即" ? ` · 提前 ${holdMinutes} 分钟预留` : "";
      fields.planSummary.textContent = `${selectedDayText()} · ${floor} / ${seat} 座${fallbackText} · ${hourText(start)}-${hourText(end)} · ${executeAt} 提交${holdText}`;
      fields.railStart.textContent = hourText(start);
      fields.railSubmit.textContent = executeAt;
    }

    function updateBookingMeta(item) {
      if (!item) {
        fields.bookingMeta.textContent = currentBookings.length ? "请选择一条预约" : "刷新当前预约后显示签到窗口和状态";
        return;
      }
      const windowText = item.sign_start_text && item.sign_deadline_text
        ? `签到 ${item.sign_start_text} 至 ${item.sign_deadline_text}`
        : "签到窗口暂未返回";
      const autoText = String(item.status) === "0" && item.auto_check_in_text
        ? ` · 自动签到 ${item.auto_check_in_text}`
        : "";
      const continueText = String(item.status) === "2"
        ? " · 可续座"
        : String(item.status) === "6" ? " · 续座期限已过" : "";
      fields.bookingMeta.textContent = `${item.status_label || "未知状态"}${continueText} · ${windowText}${autoText} · bookingId=${item.id}`;
    }

    function payload() {
      return {
        config_path: fields.configPath.value.trim(),
        room_type: fields.roomType.value.trim(),
        floor_id: fields.floorId.value.trim(),
        seat_num: fields.seatNum.value.trim(),
        fallback_seats: fields.fallbackSeats.value.trim(),
        start_hour: fields.startHour.value.trim(),
        duration_hours: fields.durationHours.value.trim(),
        execute_at: fields.executeAt.value.trim(),
        hold_before_minutes: fields.holdBeforeMinutes.value.trim(),
        max_trials: fields.maxTrials.value.trim(),
        retry_delay: fields.retryDelay.value.trim(),
        days: selectedDays(),
        dry_run: fields.dryRun.checked,
      };
    }

    function setStatus(text, cls = "") {
      fields.status.textContent = text;
      fields.status.className = `status ${cls}`;
    }

    function setNotice(text, cls = "") {
      fields.notice.textContent = text;
      fields.notice.className = `notice ${cls}`;
    }

    function appendLog(lines) {
      if (!Array.isArray(lines)) lines = [String(lines)];
      if (!lines.length) return;
      fields.logBox.textContent += lines.join("\n") + "\n";
      fields.logBox.scrollTop = fields.logBox.scrollHeight;
    }

    function setBusy(busy) {
      busyState = busy;
      formControls.forEach((control) => { control.disabled = busy; });
      fields.loadBtn.disabled = busy;
      fields.saveBtn.disabled = busy;
      fields.instantRunBtn.disabled = busy;
      fields.runBtn.disabled = busy;
      fields.cancelBtn.disabled = !busy;
      fields.refreshBookingsBtn.disabled = busy;
      fields.refreshMapBtn.disabled = busy;
      fields.pickPrimaryBtn.disabled = busy;
      fields.pickFallbackBtn.disabled = busy;
      fields.measureClockBtn.disabled = clockMeasuring;
      updateCancelBookingButton();
      setStatus(busy ? "执行中" : "就绪", busy ? "running" : "");
    }

    function selectedBooking() {
      return currentBookings.find((item) => String(item.id) === fields.bookingSelect.value);
    }

    function updateCancelBookingButton() {
      const item = selectedBooking();
      fields.cancelBookingBtn.disabled = busyState || !item || !item.cancelable;
      fields.checkInTestBtn.disabled = busyState || !item || String(item.status) !== "0";
      fields.autoCheckInBtn.disabled = busyState || !item || String(item.status) !== "0";
      fields.continueSeatBtn.disabled = busyState || !item || !item.continuable;
      fields.continueSeatBtn.title = item && String(item.status) === "6"
        ? "该预约已暂离未归结束，续座期限已过"
        : "仅暂离中的预约可以续座";
      updateBookingMeta(item);
    }

    function renderBookings(items) {
      const selectedId = fields.bookingSelect.value;
      currentBookings = Array.isArray(items) ? items : [];
      fields.bookingSelect.innerHTML = "";
      if (!currentBookings.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "暂无预约";
        fields.bookingSelect.appendChild(option);
        updateCancelBookingButton();
        return;
      }
      currentBookings.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = item.label;
        fields.bookingSelect.appendChild(option);
      });
      if (currentBookings.some((item) => String(item.id) === selectedId)) {
        fields.bookingSelect.value = selectedId;
      } else {
        const firstCancelable = currentBookings.find((item) => item.cancelable);
        fields.bookingSelect.value = String((firstCancelable || currentBookings[0]).id);
      }
      updateCancelBookingButton();
    }

    async function requestJson(url, options = {}, timeoutMs = 0) {
      const controller = timeoutMs > 0 ? new AbortController() : null;
      const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
      try {
        const response = await fetch(url, {
          headers: { "Content-Type": "application/json" },
          ...options,
          ...(controller ? { signal: controller.signal } : {}),
        });
        const data = await response.json();
        if (!response.ok) {
          const error = new Error(data.error || "请求失败");
          error.status = response.status;
          error.code = data.code;
          throw error;
        }
        return data;
      } finally {
        if (timer !== null) clearTimeout(timer);
      }
    }

    async function loadConfig() {
      try {
        setNotice("");
        const path = encodeURIComponent(fields.configPath.value.trim());
        const data = await requestJson(`/api/config?path=${path}`);
        fields.configPath.value = data.config_path;
        fields.roomType.value = data.room_type;
        fields.floorId.value = data.floor_id;
        if (String(data.floor_id) !== fields.floorId.value) {
          fields.floorId.value = "";
        }
        fields.seatNum.value = data.seat_num;
        fields.fallbackSeats.value = data.fallback_seats || "";
        fields.startHour.value = data.start_hour;
        fields.durationHours.value = data.duration_hours;
        fields.executeAt.value = data.execute_at || "";
        fields.holdBeforeMinutes.value = data.hold_before_minutes || 0;
        fields.maxTrials.value = data.max_trials;
        fields.retryDelay.value = data.retry_delay;
        fields.dryRun.checked = Boolean(data.dry_run);
        setDays(data.days);
        updatePlanSummary();
        loadSeatMap();
        setNotice(fields.floorId.value ? "配置已载入" : `配置中的楼层 ID ${data.floor_id} 不在列表中，请重新选择楼层`, fields.floorId.value ? "ok" : "error");
        loadBookings({ quiet: true }).catch((error) => {
          if (fields.floorId.value) {
            setNotice(`配置已载入；预约列表刷新失败：${error.message}`, "error");
          }
        });
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    async function loadBookings(options = {}) {
      const quiet = Boolean(options.quiet);
      try {
        if (!quiet) setNotice("");
        const path = encodeURIComponent(fields.configPath.value.trim());
        const data = await requestJson(`/api/bookings?path=${path}`);
        renderBookings(data.items);
        if (!quiet) setNotice(`已刷新 ${data.items.length} 条预约`, "ok");
        return data;
      } catch (error) {
        if (!quiet) setNotice(error.message, "error");
        throw error;
      }
    }

    async function measureClock() {
      clockMeasuring = true;
      fields.measureClockBtn.disabled = true;
      fields.clockResult.hidden = false;
      fields.clockResult.className = "clock-result";
      fields.clockResult.textContent = "正在实时测量，约需几秒…";
      try {
        const path = encodeURIComponent(fields.configPath.value.trim());
        const data = await requestJson(`/api/clock-offset?path=${path}`);
        const recommendation = data.recommendation || {};
        const target = recommendation.execute_at
          ? `建议执行时间：${recommendation.execute_at}` : "暂不建议设置执行时间";
        const current = fields.executeAt.value.trim();
        const currentText = current ? `\n当前填写：${current}` : "";
        fields.clockResult.textContent = `测量于 ${data.measured_at || "刚刚"}\n${data.message}\n${target}${currentText}\n${recommendation.basis || ""}`
          + (busyState ? "\n已启动的任务仍按原执行时间运行。" : "");
        fields.clockResult.className = recommendation.execute_at ? "clock-result" : "clock-result error";
      } catch (error) {
        fields.clockResult.className = "clock-result error";
        fields.clockResult.textContent = `测量失败：${error.message}`;
      } finally {
        clockMeasuring = false;
        fields.measureClockBtn.disabled = false;
      }
    }

    async function savePlan() {
      if (!fields.floorId.value) {
        setNotice("请先选择楼层", "error");
        fields.floorId.focus();
        return;
      }
      try {
        setNotice("");
        const data = await requestJson("/api/save", {
          method: "POST",
          body: JSON.stringify(payload()),
        });
        setNotice(data.message, "ok");
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    function bookingTargetText() {
      const start = numberValue(fields.startHour, NaN);
      const duration = numberValue(fields.durationHours, NaN);
      const end = Number.isFinite(start) && Number.isFinite(duration) ? start + duration : NaN;
      const fallback = fields.fallbackSeats.value.trim();
      const fallbackText = fallback ? ` · 备选 ${fallback}` : "";
      const floor = fields.floorId.value
        ? fields.floorId.selectedOptions[0].textContent : "--";
      return `${selectedDayText()} · ${floor} · `
        + `${fields.seatNum.value.trim() || "--"} 座${fallbackText} · ${hourText(start)}-${hourText(end)}`;
    }

    async function startBooking(immediate = false) {
      const requestPayload = payload();
      if (!requestPayload.floor_id) {
        setNotice("请先选择楼层", "error");
        fields.floorId.focus();
        return;
      }
      if (!immediate && !requestPayload.execute_at) {
        setNotice("定时预约需要填写执行时间；如需马上提交，请点击“立即预约”", "error");
        return;
      }
      if (!fields.dryRun.checked) {
        const holdMinutes = Number(requestPayload.hold_before_minutes) || 0;
        const prompt = immediate
          ? `确认立即预约？\n${bookingTargetText()}\n确认后将马上向预约接口提交。`
          : `确认定时预约？\n${bookingTargetText()}\n将在 ${requestPayload.execute_at} 提交。`
            + (holdMinutes > 0 ? `\n提前 ${holdMinutes} 分钟尝试临时预留主座位；预留成功仍须到点提交正式预约。` : "");
        if (!confirm(prompt)) return;
      }
      try {
        setNotice("");
        fields.logBox.textContent = "";
        setBusy(true);
        appendLog(immediate ? "开始立即预约流程..." : "开始定时预约流程...");
        const data = await requestJson(immediate ? "/api/run-now" : "/api/run", {
          method: "POST",
          body: JSON.stringify(requestPayload),
        });
        currentJobId = data.job_id;
        pollFailures = 0;
        pollJob(data.job_id);
      } catch (error) {
        setBusy(false);
        setStatus("失败", "error");
        setNotice(error.message, "error");
      }
    }

    async function runBooking() {
      return startBooking(false);
    }

    async function runBookingNow() {
      return startBooking(true);
    }

    async function cancelJob() {
      if (!currentJobId) return;
      try {
        const data = await requestJson("/api/cancel", {
          method: "POST",
          body: JSON.stringify({ job_id: currentJobId }),
        });
        setNotice(data.message, "ok");
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    async function cancelSelectedBooking() {
      const item = selectedBooking();
      if (!item) return;
      if (!item.cancelable) {
        setNotice("当前预约状态不可取消", "error");
        return;
      }
      const ok = confirm(`确认取消 ${item.label}？`);
      if (!ok) return;
      try {
        setNotice("");
        const data = await requestJson("/api/cancel-booking", {
          method: "POST",
          body: JSON.stringify({
            config_path: fields.configPath.value.trim(),
            booking_id: item.id,
          }),
        });
        setNotice(data.message, "ok");
        await loadBookings({ quiet: true });
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    async function checkInSelectedBooking() {
      const item = selectedBooking();
      if (!item) return;
      if (String(item.status) !== "0") {
        setNotice("只允许测试待签到预约", "error");
        return;
      }
      const ok = confirm(
        `确认发送签到接口测试？\n\n当前预约：${item.label}\n\n`
        + "这会真实请求 checkIn 接口；若后端放行，预约状态会变为签到成功。"
      );
      if (!ok) return;
      try {
        setNotice("");
        fields.checkInTestBtn.disabled = true;
        appendLog(`发送签到接口测试：bookingId=${item.id}`);
        const data = await requestJson("/api/check-in-test", {
          method: "POST",
          body: JSON.stringify({
            config_path: fields.configPath.value.trim(),
            booking_id: item.id,
          }),
        });
        appendLog(JSON.stringify(data, null, 2));
        if (data.sent) {
          const result = data.response && data.response.DATA && data.response.DATA.result;
          const msg = data.response && data.response.DATA && (data.response.DATA.msg || data.response.DATA.message);
          setNotice(msg || (result === "success" ? "签到接口返回成功" : "签到接口已返回"), result === "success" ? "ok" : "error");
        } else {
          setNotice(data.message || "预检未通过，未发送签到请求", "error");
        }
        await loadBookings({ quiet: true });
      } catch (error) {
        setNotice(error.message, "error");
      } finally {
        updateCancelBookingButton();
      }
    }

    async function autoCheckInSelectedBooking() {
      const item = selectedBooking();
      if (!item) return;
      if (String(item.status) !== "0") {
        setNotice("只允许对待签到预约启用自动签到", "error");
        return;
      }
      const autoTime = item.auto_check_in_text || "预约开始 5 分钟后";
      const ok = confirm(
        `确认自动签到？\n\n当前预约：${item.label}\n\n`
        + `系统会等到 ${autoTime} 后真实请求 checkIn 接口。`
      );
      if (!ok) return;
      try {
        setNotice("");
        fields.logBox.textContent = "";
        setBusy(true);
        appendLog(`开始自动签到任务：bookingId=${item.id}`);
        const data = await requestJson("/api/auto-check-in", {
          method: "POST",
          body: JSON.stringify({
            config_path: fields.configPath.value.trim(),
            booking_id: item.id,
            delay_minutes: 5,
          }),
        });
        currentJobId = data.job_id;
        pollFailures = 0;
        pollJob(data.job_id);
      } catch (error) {
        setBusy(false);
        setStatus("失败", "error");
        setNotice(error.message, "error");
      }
    }

    async function continueSelectedSeat() {
      const item = selectedBooking();
      if (!item) return;
      if (!item.continuable) {
        const message = String(item.status) === "6"
          ? "该预约已暂离未归结束，续座期限已过，服务器不允许续座"
          : `当前状态为${item.status_label || "未知"}，只有“暂离中”的预约可以续座`;
        setNotice(message, "error");
        return;
      }
      const ok = confirm(
        `确认续座？\n\n当前预约：${item.label}\n\n`
        + "这会真实请求服务器的 comeBack 接口，并在完成后复核预约状态。"
      );
      if (!ok) return;
      try {
        setNotice("");
        fields.continueSeatBtn.disabled = true;
        appendLog(`发送续座请求：bookingId=${item.id}`);
        const data = await requestJson("/api/continue-seat", {
          method: "POST",
          body: JSON.stringify({
            config_path: fields.configPath.value.trim(),
            booking_id: item.id,
          }),
        });
        appendLog(JSON.stringify(data, null, 2));
        setNotice(data.message || data.status_label_after || "续座成功", "ok");
        await loadBookings({ quiet: true });
      } catch (error) {
        setNotice(error.message, "error");
      } finally {
        updateCancelBookingButton();
      }
    }

    async function pollJob(jobId) {
      if (jobId !== currentJobId) return;
      clearTimeout(pollTimer);
      pollTimer = null;
      try {
        const data = await requestJson(`/api/job?id=${encodeURIComponent(jobId)}`, {}, 5000);
        if (jobId !== currentJobId) return;
        if (pollFailures > 0) {
          setStatus("执行中", "running");
          setNotice("连接已恢复，已接回任务状态", "ok");
        }
        pollFailures = 0;
        fields.logBox.textContent = data.logs.join("\n") + (data.logs.length ? "\n" : "");
        fields.logBox.scrollTop = fields.logBox.scrollHeight;
        if (data.status === "running") {
          pollTimer = setTimeout(() => pollJob(jobId), data.poll_after_ms || 1000);
          return;
        }
        setBusy(false);
        currentJobId = "";
        loadBookings({ quiet: true }).catch(() => {});
        if (data.status === "error") {
          setStatus("失败", "error");
          setNotice(data.error || "执行失败", "error");
        } else if (data.status === "uncertain") {
          setStatus("待确认", "error");
          setNotice(data.error || "操作结果待确认，请刷新预约列表", "error");
        } else if (data.status === "cancelled") {
          setStatus("已取消", "");
          setNotice("任务已取消", "ok");
        } else {
          setStatus("完成", "");
          setNotice(data.message || "执行完成", "ok");
        }
      } catch (error) {
        if (jobId !== currentJobId) return;
        if (error.code === "job_not_found") {
          setBusy(false);
          currentJobId = "";
          pollFailures = 0;
          setStatus("待确认", "error");
          setNotice("服务中已找不到此任务，无法确认是否提交；请刷新预约列表核实结果", "error");
          loadBookings({ quiet: true }).catch(() => {});
          return;
        }
        pollFailures = Math.min(pollFailures + 1, 5);
        const delay = Math.min(1000 * (2 ** (pollFailures - 1)), 10000);
        setBusy(true);
        setStatus("连接恢复中", "running");
        setNotice(`暂时无法读取任务状态，${delay / 1000} 秒后重连；后台任务可能仍在运行`, "error");
        pollTimer = setTimeout(() => pollJob(jobId), delay);
      }
    }

    async function resumeActiveJob() {
      const data = await requestJson("/api/active-job");
      if (!data.job) return;
      currentJobId = data.job.id;
      pollFailures = 0;
      fields.logBox.textContent = data.job.logs.join("\n") + (data.job.logs.length ? "\n" : "");
      fields.logBox.scrollTop = fields.logBox.scrollHeight;
      setBusy(true);
      setNotice("已接回正在运行的任务", "ok");
      pollJob(currentJobId);
    }

    fields.loadBtn.addEventListener("click", loadConfig);
    fields.saveBtn.addEventListener("click", savePlan);
    fields.instantRunBtn.addEventListener("click", runBookingNow);
    fields.runBtn.addEventListener("click", runBooking);
    fields.cancelBtn.addEventListener("click", cancelJob);
    fields.refreshBookingsBtn.addEventListener("click", () => loadBookings());
    fields.measureClockBtn.addEventListener("click", measureClock);
    fields.cancelBookingBtn.addEventListener("click", cancelSelectedBooking);
    fields.checkInTestBtn.addEventListener("click", checkInSelectedBooking);
    fields.autoCheckInBtn.addEventListener("click", autoCheckInSelectedBooking);
    fields.continueSeatBtn.addEventListener("click", continueSelectedSeat);
    fields.bookingSelect.addEventListener("change", updateCancelBookingButton);
    fields.clearBtn.addEventListener("click", () => { fields.logBox.textContent = ""; });
    fields.refreshMapBtn.addEventListener("click", loadSeatMap);
    fields.pickPrimaryBtn.addEventListener("click", () => setMapMode("primary"));
    fields.pickFallbackBtn.addEventListener("click", () => setMapMode("fallback"));
    fields.zoomOutBtn.addEventListener("click", () => {
      if (!seatMapData) return;
      mapZoom = Math.max(0.6, Math.round((mapZoom - 0.2) * 10) / 10);
      renderSeatMap();
    });
    fields.zoomInBtn.addEventListener("click", () => {
      if (!seatMapData) return;
      mapZoom = Math.min(2.4, Math.round((mapZoom + 0.2) * 10) / 10);
      renderSeatMap();
    });
    formControls.forEach((control) => {
      control.addEventListener("input", updatePlanSummary);
      control.addEventListener("change", updatePlanSummary);
    });
    [fields.floorId, fields.roomType, fields.startHour, fields.durationHours,
      fields.configPath, ...document.querySelectorAll("input[name='days']")]
      .forEach((control) => control.addEventListener("change", () => {
        if (!busyState) loadSeatMap();
      }));
    [fields.seatNum, fields.fallbackSeats].forEach((control) => {
      control.addEventListener("input", () => renderSeatMap());
    });
    document.querySelectorAll("input[type='number']").forEach((input) => {
      input.addEventListener("focus", () => input.select());
      input.addEventListener("wheel", (event) => event.preventDefault(), { passive: false });
    });

    fields.configPath.value = "__DEFAULT_CONFIG__";
    updateClock();
    setInterval(updateClock, 1000);
    setDays(0);
    loadConfig().then(() => resumeActiveJob().catch(() => {}));
  </script>
</body>
</html>
"""


def booking_form_from_config(config_path):
    path = web_config_path(config_path)
    config = load_config(path)
    booking = config.get("booking") or {}
    plan = parse_plan(str(booking.get("plan") or ""))
    fallback_seats = ",".join(
        parse_fallback_seats(booking.get("fallback_seats"), primary_seat=plan["seat_num"])
    )
    return {
        "config_path": str(path),
        "room_type": plan["room_type"],
        "floor_id": plan["floor_id"],
        "seat_num": plan["seat_num"],
        "fallback_seats": fallback_seats,
        "start_hour": plan["start_hour"],
        "duration_hours": plan["duration_hours"],
        "execute_at": normalize_execute_at(booking.get("execute_at")),
        "max_trials": normalize_max_trials(booking.get("max_trials", DEFAULT_MAX_TRIALS)),
        "retry_delay": normalize_retry_delay(booking.get("retry_delay", DEFAULT_RETRY_DELAY)),
        "hold_before_minutes": normalize_hold_before_minutes(booking.get("hold_before_minutes", DEFAULT_HOLD_BEFORE_MINUTES)),
        "days": normalize_days(int(booking.get("book_days", DEFAULT_BOOK_DAYS))),
        "dry_run": bool(booking.get("dry_run")),
    }


def bookings_from_config(config_path):
    path = web_config_path(config_path)
    return {
        "config_path": str(path),
        "items": get_current_bookings(path),
    }


def seat_map_from_config(config_path, room_type, floor_id, days, start_hour, duration_hours):
    """Return only the floor plan and seat coordinates needed by the UI."""
    with JOBS_LOCK:
        job = running_job_locked()
        if (
            job and job["job_type"] == "booking"
            and job.get("execute_timestamp") is not None
            and job["execute_timestamp"] - time.time() < 20
        ):
            raise ValueError("距离预约提交不足 20 秒，请稍后刷新座位图")
    path = web_config_path(config_path)
    config = load_config(path)
    room_type = int(room_type)
    floor_id = str(floor_id).strip()
    days = int(days)
    start_hour = int(start_hour)
    duration_hours = int(duration_hours)
    if not floor_id.isdigit() or days not in (0, 1, 2):
        raise ValueError("请选择有效的楼层和预约日期")
    if not 0 <= start_hour <= 23 or not 1 <= duration_hours <= 24 or start_hour + duration_hours > 24:
        raise ValueError("请选择有效的预约时段")

    booker = InstantBooker(config)
    try:
        booker.load_cookies()
        room_items = booker.query_room_items()
        if not 1 <= room_type <= len(room_items):
            raise ValueError("房间类型不存在")
        detail = booker.query_room_detail(room_items[room_type - 1])
        begin_time = build_begin_time(start_hour, days)
        # The exact slot supplies availability. If it is not yet open, a
        # fallback query still supplies the real floor plan and coordinates.
        exact = True
        try:
            floors = booker._query_seat_map_once(detail, begin_time, duration_hours)
            if not any(str((f.get("seatMap") or {}).get("info", {}).get("id")) == floor_id for f in floors):
                exact = False
                floors = booker.query_seat_map(detail, begin_time, duration_hours, target_floor_id=floor_id)
        except Exception:
            exact = False
            floors = booker.query_seat_map(detail, begin_time, duration_hours, target_floor_id=floor_id)
        floor = next((f for f in floors if str((f.get("seatMap") or {}).get("info", {}).get("id")) == floor_id), None)
        if floor is None:
            raise ValueError("所选区域没有座位图，请检查房间类型和楼层")
        seat_map = floor.get("seatMap") or {}
        info = seat_map.get("info") or {}
        width, height = float(info["width"]), float(info["height"])
        if not 0 < width <= 1000 or not 0 < height <= 1000:
            raise ValueError("座位图尺寸无效")
        seats = []
        for item in seat_map.get("POIs") or []:
            try:
                seat = {
                    "number": str(item["title"]),
                    "x": float(item["x"]), "y": float(item["y"]),
                    "w": float(item.get("w") or 2), "h": float(item.get("h") or 2),
                    "state": str(item.get("state", "")) if exact else "",
                }
            except (KeyError, TypeError, ValueError):
                continue
            if all(0 <= seat[key] <= 1000 for key in ("x", "y", "w", "h")):
                seats.append(seat)
        return {
            "floor_id": floor_id,
            "floor_name": str(floor.get("roomName") or info.get("title") or ""),
            "plan_url": str(info.get("plan") or ""),
            "width": width, "height": height,
            "seats": seats, "availability_exact": exact,
            "fetched_at": datetime.now().astimezone().strftime("%H:%M:%S"),
        }
    finally:
        booker.session.close()


def clock_offset_from_config(config_path):
    with JOBS_LOCK:
        job = running_job_locked()
        if (
            job and job["job_type"] == "booking"
            and job.get("execute_timestamp") is not None
            and job["execute_timestamp"] - time.time() < 20
        ):
            raise ValueError("距离预约提交不足 20 秒，已跳过测量以免影响发包")
    return measure_server_clock(web_config_path(config_path))


def normalize_days(days):
    return days if days in (0, 1, 2) else 1


def normalize_hold_before_minutes(value):
    try:
        return max(0, min(int(value), 14))
    except (TypeError, ValueError):
        return DEFAULT_HOLD_BEFORE_MINUTES


def normalize_max_trials(value):
    try:
        return max(1, min(int(value), 20))
    except Exception:
        return DEFAULT_MAX_TRIALS


def booking_execute_at_from_payload(payload, force_immediate=False):
    if force_immediate:
        return ""
    return normalize_execute_at(payload.get("execute_at"))


def fallback_seats_from_payload(payload, primary_seat):
    return ",".join(
        parse_fallback_seats(payload.get("fallback_seats"), primary_seat=primary_seat)
    )


def plan_from_payload(payload):
    required = ("room_type", "floor_id", "seat_num", "start_hour", "duration_hours")
    values = {}
    for key in required:
        value = str(payload.get(key, "")).strip()
        if not value:
            raise ValueError(f"{key} 不能为空")
        values[key] = value

    plan_text = (
        f"{values['room_type']}:{values['floor_id']}:{values['seat_num']}:"
        f"{values['start_hour']}:{values['duration_hours']}"
    )
    plan = parse_plan(plan_text)
    if not 0 <= plan["start_hour"] <= 23:
        raise ValueError("开始小时必须在 0 到 23 之间")
    if plan["duration_hours"] <= 0:
        raise ValueError("时长必须大于 0")
    return plan_text


def config_path_from_payload(payload):
    raw_path = str(payload.get("config_path") or DEFAULT_CONFIG).strip()
    return web_config_path(raw_path)


def web_config_path(raw_path):
    """Resolve the UI config path and lock remote sessions to the default file."""
    path = Path(str(raw_path or DEFAULT_CONFIG).strip()).expanduser()
    if WEB_AUTH_PASSWORD and path.resolve() != DEFAULT_CONFIG.resolve():
        raise ValueError("远程访问仅允许使用默认配置文件")
    return path


def atomic_write_config(path, text):
    config = yaml.safe_load(text)
    if not isinstance(config, dict) or not isinstance(config.get("booking"), dict):
        raise ValueError("配置必须包含 booking 配置项")
    parse_plan(str(config["booking"].get("plan") or ""))
    path = Path(path).resolve()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as file:
            temp_path = Path(file.name)
            os.fchmod(file.fileno(), path.stat().st_mode & 0o600)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_booking_values(
    path,
    plan_text,
    fallback_seats,
    days,
    dry_run,
    execute_at,
    max_trials,
    retry_delay,
    hold_before_minutes=DEFAULT_HOLD_BEFORE_MINUTES,
):
    primary_seat = parse_plan(plan_text)["seat_num"]
    fallback_text = fallback_seats_from_payload(
        {"fallback_seats": fallback_seats},
        primary_seat,
    )
    values = {
        "plan": plan_text,
        "fallback_seats": f"'{fallback_text}'",
        "execute_at": f"'{normalize_execute_at(execute_at)}'",
        "max_trials": str(normalize_max_trials(max_trials)),
        "retry_delay": f"{normalize_retry_delay(retry_delay):g}",
        "hold_before_minutes": str(normalize_hold_before_minutes(hold_before_minutes)),
        "dry_run": "true" if dry_run else "false",
    }
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    booking_start = find_booking_section(lines)

    if booking_start is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        block = (
            f"{prefix}booking:\n"
            f"  plan: {values['plan']}\n"
            f"  fallback_seats: {values['fallback_seats']}\n"
            f"  execute_at: {values['execute_at']}\n"
            f"  max_trials: {values['max_trials']}\n"
            f"  retry_delay: {values['retry_delay']}\n"
            f"  hold_before_minutes: {values['hold_before_minutes']}\n"
            f"  dry_run: {values['dry_run']}\n"
        )
        atomic_write_config(path, text + block)
        return

    booking_end = find_section_end(lines, booking_start)
    seen = set()
    for index in range(booking_start + 1, booking_end):
        match = re.match(
            r"^(\s+)(plan|fallback_seats|book_days|execute_at|max_trials|retry_delay|hold_before_minutes|dry_run)\s*:",
            lines[index],
        )
        if not match:
            continue
        indent, key = match.groups()
        if key == "book_days":
            lines[index] = ""
            continue
        seen.add(key)
        lines[index] = f"{indent}{key}: {values[key]}{inline_comment(lines[index])}\n"

    missing = [
        key
        for key in (
            "plan",
            "fallback_seats",
            "execute_at",
            "max_trials",
            "retry_delay",
            "hold_before_minutes",
            "dry_run",
        )
        if key not in seen
    ]
    if missing:
        if booking_end > 0 and lines[booking_end - 1] and not lines[booking_end - 1].endswith("\n"):
            lines[booking_end - 1] += "\n"
        lines[booking_end:booking_end] = [f"  {key}: {values[key]}\n" for key in missing]
    atomic_write_config(path, "".join(lines))


def find_booking_section(lines):
    for index, line in enumerate(lines):
        if re.match(r"^booking\s*:", line):
            return index
    return None


def find_section_end(lines, section_start):
    for index in range(section_start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t", "#")) and ":" in line:
            return index
    return len(lines)


def inline_comment(line):
    body = line.rstrip("\r\n")
    if "#" not in body:
        return ""
    before, after = body.split("#", 1)
    return f" #{after}" if before.strip() else ""


def job_poll_after_ms(job):
    target = job.get("execute_timestamp")
    if job["status"] == "running" and target is not None:
        remaining = target - time.time()
        if 0 < remaining <= 2:
            # Let the browser wait across the send window without polling logs.
            return int((remaining + 0.5) * 1000) + 1
        if remaining > 2:
            return int(min(2000, max(500, (remaining - 2) * 1000)))
    return 1000


def job_snapshot_locked(job):
    return {
        "id": job["id"],
        "job_type": job["job_type"],
        "mode": job["mode"],
        "status": job["status"],
        "logs": list(job["logs"]),
        "error": job["error"],
        "message": job.get("message", ""),
        "poll_after_ms": job_poll_after_ms(job),
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "log_path": str(job["log_path"]),
    }


def running_job_locked():
    running = [job for job in JOBS.values() if job["status"] == "running"]
    if not running:
        return None
    return max(running, key=lambda job: job["started_at"])


def create_job_record(job_type="generic", mode=""):
    job_id = str(uuid.uuid4())
    cancel_event = threading.Event()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with JOBS_LOCK:
        running = running_job_locked()
        if running:
            raise RuntimeError(f"已有任务正在运行：{running['id']}")
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        LOG_DIR.chmod(0o700)
        log_path = LOG_DIR / f"job-{timestamp}-{job_id[:8]}.log"
        log_path.touch(mode=0o600, exist_ok=False)
        log_path.chmod(0o600)
        job = {
            "id": job_id,
            "job_type": str(job_type),
            "mode": str(mode),
            "status": "running",
            "logs": [],
            "error": "",
            "started_at": time.time(),
            "finished_at": None,
            "cancel_event": cancel_event,
            "log_path": log_path,
        }
        JOBS[job_id] = job
    return job


def append_job_log(job, message):
    with JOBS_LOCK:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{stamp}] {message}"
        job["logs"].append(line)
        try:
            with job["log_path"].open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError:
            pass


def execute_job(job, action):
    """Use the actual outcome, not the cancellation flag, as the final status."""
    error = ""
    try:
        result = action()
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(result.get("message") or "操作未完成")
    except TaskCancelled:
        status, message = "cancelled", "任务已停止"
    except ResultUncertain as exc:
        status, error, message = "uncertain", str(exc), str(exc)
    except Exception as exc:
        status, error, message = "error", str(exc), f"失败：{exc}"
    else:
        status, message = "done", "执行完成"
        if isinstance(result, dict):
            if result.get("dry_run"):
                message = "测试完成，未提交预约"
            elif result.get("confirmed_booking"):
                booking = result["confirmed_booking"]
                message = f"预约成功：{booking.get('label') or booking['id']}"
            elif job["job_type"] == "auto_check_in":
                message = result.get("message") or "自动签到成功，已复核为使用中"
    # Publish the terminal state only after its final log has been appended.
    append_job_log(job, message)
    with JOBS_LOCK:
        job.update(status=status, error=error, message=message, finished_at=time.time())


def start_booking_job(payload, force_immediate=False):
    config_path = config_path_from_payload(payload)
    load_config(config_path)
    plan_text = plan_from_payload(payload)
    primary_seat = parse_plan(plan_text)["seat_num"]
    fallback_seats = fallback_seats_from_payload(payload, primary_seat)
    days = normalize_days(int(payload.get("days", 1)))
    dry_run = bool(payload.get("dry_run"))
    execute_at = booking_execute_at_from_payload(payload, force_immediate=force_immediate)
    max_trials = normalize_max_trials(payload.get("max_trials", DEFAULT_MAX_TRIALS))
    retry_delay = normalize_retry_delay(payload.get("retry_delay", DEFAULT_RETRY_DELAY))
    hold_before_minutes = 0 if force_immediate else normalize_hold_before_minutes(payload.get("hold_before_minutes", DEFAULT_HOLD_BEFORE_MINUTES))
    mode = "immediate" if not execute_at else "scheduled"
    planned_execution = build_execute_time(execute_at)
    job = create_job_record(job_type="booking", mode=mode)
    job["execute_timestamp"] = planned_execution.timestamp() if planned_execution else None
    job_id = job["id"]
    cancel_event = job["cancel_event"]

    def append(message):
        append_job_log(job, message)

    def worker():
        execute_job(
            job,
            lambda: run_booking(
                config_path=config_path,
                plan_text=plan_text,
                fallback_seats=fallback_seats,
                days=days,
                dry_run_override=dry_run,
                execute_at=execute_at,
                max_trials=max_trials,
                retry_delay=retry_delay,
                hold_before_minutes=hold_before_minutes,
                logger=append,
                should_cancel=cancel_event.is_set,
            ),
        )

    append_job_log(job, f"持久化日志：{job['log_path']}")
    append_job_log(job, "执行模式：立即预约" if mode == "immediate" else f"执行模式：定时预约 {execute_at}")
    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_auto_check_in_job(payload):
    config_path = config_path_from_payload(payload)
    load_config(config_path)
    booking_id = str(payload.get("booking_id") or "").strip() or None
    delay_minutes = normalize_check_in_delay_minutes(
        payload.get("delay_minutes", DEFAULT_AUTO_CHECK_IN_DELAY_MINUTES)
    )
    job = create_job_record(job_type="auto_check_in", mode="scheduled")
    job_id = job["id"]
    cancel_event = job["cancel_event"]

    def append(message):
        append_job_log(job, message)

    def worker():
        execute_job(
            job,
            lambda: run_auto_check_in(
                config_path=config_path,
                booking_id=booking_id,
                delay_minutes=delay_minutes,
                logger=append,
                should_cancel=cancel_event.is_set,
            ),
        )

    append_job_log(job, f"持久化日志：{job['log_path']}")
    threading.Thread(target=worker, daemon=True).start()
    return job_id


def cancel_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise KeyError("任务不存在")
        if job["status"] != "running":
            return {"message": "任务已经结束"}
        job["cancel_event"].set()
    message = "已请求停止任务；已经发出的请求会先完成结果复核"
    append_job_log(job, message)
    return {"message": message}


def job_snapshot(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise JobNotFound("任务不存在，可能已重启服务")
        return job_snapshot_locked(job)


def active_job_snapshot():
    with JOBS_LOCK:
        job = running_job_locked()
        return {"job": job_snapshot_locked(job) if job else None}


class WebHandler(BaseHTTPRequestHandler):
    server_version = "HDULibraryInstant/1.0"

    def do_GET(self):
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = INDEX_HTML.replace("__DEFAULT_CONFIG__", str(DEFAULT_CONFIG))
            self.send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/config":
            query = parse_qs(parsed.query)
            path = query.get("path", [str(DEFAULT_CONFIG)])[0] or str(DEFAULT_CONFIG)
            self.handle_json(lambda: booking_form_from_config(path))
            return
        if parsed.path == "/api/job":
            query = parse_qs(parsed.query)
            job_id = query.get("id", [""])[0]
            self.handle_json(lambda: job_snapshot(job_id))
            return
        if parsed.path == "/api/active-job":
            self.handle_json(active_job_snapshot)
            return
        if parsed.path == "/api/bookings":
            query = parse_qs(parsed.query)
            path = query.get("path", [str(DEFAULT_CONFIG)])[0] or str(DEFAULT_CONFIG)
            self.handle_json(lambda: bookings_from_config(path))
            return
        if parsed.path == "/api/seat-map":
            query = parse_qs(parsed.query)
            value = lambda key, default="": query.get(key, [default])[0]
            self.handle_json(lambda: seat_map_from_config(
                value("path", str(DEFAULT_CONFIG)), value("room_type"), value("floor_id"),
                value("days"), value("start_hour"), value("duration_hours"),
            ))
            return
        if parsed.path == "/api/clock-offset":
            query = parse_qs(parsed.query)
            path = query.get("path", [str(DEFAULT_CONFIG)])[0] or str(DEFAULT_CONFIG)
            self.handle_json(lambda: clock_offset_from_config(path))
            return
        self.send_error(404)

    def do_POST(self):
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/save":
            self.handle_json(self.save_plan)
            return
        if parsed.path == "/api/run":
            self.handle_json(self.run_booking)
            return
        if parsed.path == "/api/run-now":
            self.handle_json(self.run_booking_now)
            return
        if parsed.path == "/api/auto-check-in":
            self.handle_json(self.auto_check_in)
            return
        if parsed.path == "/api/cancel":
            self.handle_json(self.cancel_booking)
            return
        if parsed.path == "/api/cancel-booking":
            self.handle_json(self.cancel_seat_booking)
            return
        if parsed.path == "/api/check-in-test":
            self.handle_json(self.check_in_test)
            return
        if parsed.path == "/api/continue-seat":
            self.handle_json(self.continue_seat)
            return
        self.send_error(404)

    def save_plan(self):
        payload = self.read_json()
        path = config_path_from_payload(payload)
        with CONFIG_LOCK:
            load_config(path)
            plan_text = plan_from_payload(payload)
            write_booking_values(
                path,
                plan_text,
                payload.get("fallback_seats"),
                payload.get("days", 1),
                bool(payload.get("dry_run")),
                payload.get("execute_at"),
                payload.get("max_trials", DEFAULT_MAX_TRIALS),
                payload.get("retry_delay", DEFAULT_RETRY_DELAY),
                payload.get("hold_before_minutes", DEFAULT_HOLD_BEFORE_MINUTES),
            )
        return {"message": "计划已保存"}

    def run_booking(self):
        payload = self.read_json()
        job_id = start_booking_job(payload)
        mode = "immediate" if not booking_execute_at_from_payload(payload) else "scheduled"
        return {"job_id": job_id, "mode": mode}

    def run_booking_now(self):
        payload = self.read_json()
        job_id = start_booking_job(payload, force_immediate=True)
        return {"job_id": job_id, "mode": "immediate"}

    def auto_check_in(self):
        payload = self.read_json()
        job_id = start_auto_check_in_job(payload)
        return {"job_id": job_id}

    def cancel_booking(self):
        payload = self.read_json()
        return cancel_job(str(payload.get("job_id") or ""))

    def cancel_seat_booking(self):
        payload = self.read_json()
        path = config_path_from_payload(payload)
        result = cancel_booking_by_id(
            config_path=path,
            booking_id=payload.get("booking_id"),
            logger=lambda message: None,
        )
        return {
            "message": "预约已取消",
            "booking_id": result["booking_id"],
            "result": result["result"],
        }

    def check_in_test(self):
        payload = self.read_json()
        path = config_path_from_payload(payload)
        return check_in_test_by_id(
            config_path=path,
            booking_id=payload.get("booking_id"),
            logger=lambda message: None,
        )

    def continue_seat(self):
        payload = self.read_json()
        path = config_path_from_payload(payload)
        result = continue_seat_by_id(
            config_path=path,
            booking_id=payload.get("booking_id"),
            logger=lambda message: None,
        )
        return {"message": "续座成功", **result}

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def handle_json(self, callback):
        try:
            data = callback()
        except JobNotFound as exc:
            self.send_json({"error": str(exc), "code": "job_not_found"}, status=404)
        except ResultUncertain as exc:
            self.send_json({"error": str(exc), "code": "outcome_uncertain"}, status=409)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=400)
        else:
            self.send_json(data)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        self.wfile.write(body)

    def require_auth(self):
        if not WEB_AUTH_PASSWORD:
            return True

        authorization = self.headers.get("Authorization", "")
        scheme, _, encoded = authorization.partition(" ")
        supplied_username = ""
        supplied_password = ""
        if scheme.lower() == "basic" and encoded:
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                supplied_username, supplied_password = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                pass

        username_ok = hmac.compare_digest(
            supplied_username.encode("utf-8"),
            WEB_AUTH_USERNAME.encode("utf-8"),
        )
        password_ok = hmac.compare_digest(
            supplied_password.encode("utf-8"),
            WEB_AUTH_PASSWORD.encode("utf-8"),
        )
        if username_ok and password_ok:
            return True

        body = "需要登录才能访问。".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="HDU Library", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        return False

    def log_message(self, format_text, *args):
        return


def make_server(host, port):
    return ThreadingHTTPServer((host, port), WebHandler)


def parse_args():
    parser = argparse.ArgumentParser(description="HDU 图书馆即时预约网页控制台")
    parser.add_argument("--host", default=HOST, help="监听地址")
    parser.add_argument("--port", type=int, default=PORT, help="起始端口")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    return parser.parse_args()


def main():
    args = parse_args()
    last_error = None
    for port in range(args.port, args.port + 20):
        try:
            server = make_server(args.host, port)
        except OSError as exc:
            last_error = exc
            continue
        url = f"http://{args.host}:{port}"
        print(f"网页控制台已启动：{url}")
        print("按 Ctrl+C 退出")
        if args.open:
            threading.Timer(0.5, webbrowser.open, args=(url,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")
        finally:
            server.server_close()
        return
    raise RuntimeError(f"没有可用端口：{last_error}")


if __name__ == "__main__":
    main()
