// Shared helpers for the library and fiction pages.
"use strict";

const RR = (() => {
  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: options.body ? { "Content-Type": "application/json" } : {},
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    if (res.status === 204) return null;
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = data && data.detail;
      const message = (detail && (detail.message || (typeof detail === "string" ? detail : null))) || `HTTP ${res.status}`;
      const err = new Error(message);
      err.status = res.status;
      err.detail = detail;
      throw err;
    }
    return data;
  }

  // Build DOM nodes; strings become text nodes, so titles from RoyalRoad are never parsed as HTML.
  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (key === "class") el.className = value;
      else el.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  const dateFmt = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });
  const dateTimeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "short" });
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const UNITS = [["year", 31536e6], ["month", 2592e6], ["week", 6048e5], ["day", 864e5],
                 ["hour", 36e5], ["minute", 6e4]];

  function relative(iso) {
    const diff = new Date(iso) - Date.now();
    for (const [unit, ms] of UNITS) {
      if (Math.abs(diff) >= ms) return rtf.format(Math.round(diff / ms), unit);
    }
    return "just now";
  }

  function duration(fromIso, toIso) {
    const diff = new Date(toIso) - new Date(fromIso);
    for (const [unit, ms] of UNITS) {
      if (diff >= ms) {
        const n = Math.round(diff / ms);
        return `${n} ${unit}${n === 1 ? "" : "s"}`;
      }
    }
    return "under a minute";
  }

  // A date, with the exact local time and a relative time on hover.
  function dateEl(iso, { relativeText = false } = {}) {
    if (!iso) return null;
    const d = new Date(iso);
    return h("time", { datetime: iso, title: `${dateTimeFmt.format(d)} (${relative(iso)})` },
      relativeText ? relative(iso) : dateFmt.format(d));
  }

  function progressEl(run) {
    if (!run) return null;
    if (run.status === "queued") return h("div", { class: "progress queued" }, h("span", {}, "Queued…"));
    const total = run.chapters_total || 0;
    const pct = total ? Math.round((run.chapters_checked / total) * 100) : 0;
    const label = total ? `Checking ${run.chapters_checked} / ${total} chapters` : "Reading chapter list…";
    return h("div", { class: "progress", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 },
      h("div", { class: "bar" }, h("div", { class: "fill", style: `width:${pct}%` })),
      h("span", {}, label));
  }

  function runSummaryEl(run) {
    if (!run) return null;
    const parts = [];
    if (run.new_count) parts.push(h("span", { class: "badge new" }, `+${run.new_count} new`));
    if (run.edited_count) parts.push(h("span", { class: "badge edited" }, `${run.edited_count} edited`));
    if (run.removed_count) parts.push(h("span", { class: "badge removed" }, `${run.removed_count} removed`));
    if (run.error_count) parts.push(h("span", { class: "badge error" }, `${run.error_count} failed to load`));
    if (!parts.length) parts.push(h("span", { class: "muted small" }, "No changes"));
    return h("span", { class: "badges" }, parts);
  }

  return { api, h, relative, duration, dateEl, progressEl, runSummaryEl };
})();
