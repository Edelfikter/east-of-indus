// East of Indus — Scriptable widget for iOS
//
// Setup (5 min, free):
//   1. Install Scriptable from the App Store (free, by Simon Støvring)
//   2. Open Scriptable → tap "+" → paste this entire file → name it "East of Indus"
//   3. Long-press your home screen → "+" → Scriptable → pick widget size
//   4. Tap the placeholder widget → "Edit Widget" → Script: "East of Indus"
//   5. Done. Widget will fetch latest.json + pulse.json from Supabase.
//
// Tap the widget to open the full paper at iac-press.blogspot.com/?eoi.
// Widget refreshes ~every 15 min (iOS controls the actual schedule).

const ISSUE_URL = "https://nfpdtjqncwibgyrzvffr.supabase.co/storage/v1/object/public/eoi/latest.json";
const PULSE_URL = "https://nfpdtjqncwibgyrzvffr.supabase.co/storage/v1/object/public/eoi/pulse.json";
const OPEN_URL  = "https://iac-press.blogspot.com/?eoi";

// Palette matches the paper
const PAPER = new Color("#e6e0c8");
const INK   = new Color("#171513");
const MUTED = new Color("#5b554a");
const BRICK = new Color("#b04a30");

async function fetchJSON(url) {
  try {
    const req = new Request(url + "?t=" + Date.now());
    req.headers = { "Cache-Control": "no-cache" };
    return await req.loadJSON();
  } catch (e) {
    return null;
  }
}

function addText(parent, text, opts) {
  opts = opts || {};
  const t = parent.addText(String(text || ""));
  if (opts.font)  t.font = opts.font;
  if (opts.color) t.textColor = opts.color;
  if (opts.lines) t.lineLimit = opts.lines;
  if (opts.align === "right")  t.rightAlignText();
  if (opts.align === "center") t.centerAlignText();
  return t;
}

function brickRule(parent, height) {
  const stack = parent.addStack();
  stack.backgroundColor = BRICK;
  stack.size = new Size(0, height || 3);
  return stack;
}

function renderSmall(w, issue, pulse) {
  const m = (pulse && pulse.metrics) || (issue && issue.metrics) || {};

  addText(w, "EAST OF INDUS", {
    font: Font.boldSystemFont(9),
    color: INK,
  });

  w.addSpacer(4);
  addText(w, m.rating || "—", {
    font: Font.boldSystemFont(24),
    color: BRICK,
    lines: 1,
  });

  w.addSpacer(6);
  // 12-hour sparkline fits a small widget's width
  if (m.hourly_sparkline) {
    const spark = m.hourly_sparkline.slice(-12);
    addText(w, spark, {
      font: Font.boldMonospacedSystemFont(11),
      color: BRICK,
      lines: 1,
    });
  }

  w.addSpacer(4);
  addText(w, `${m.threads_active_24h ?? "—"} thr · ${m.peak_hour_ist || ""}`, {
    font: Font.systemFont(9),
    color: MUTED,
    lines: 1,
  });

  w.addSpacer();
  addText(w, issue ? issue.issue_no : "", {
    font: Font.monospacedSystemFont(8),
    color: MUTED,
    align: "right",
  });
}

function renderMedium(w, issue, pulse) {
  const m = (pulse && pulse.metrics) || (issue && issue.metrics) || {};

  // Masthead row
  const head = w.addStack();
  head.layoutHorizontally();
  head.bottomAlignContent();
  addText(head, "East of Indus", {
    font: new Font("Times New Roman", 18),
    color: INK,
  });
  head.addSpacer();
  addText(head, m.rating || "—", {
    font: Font.boldSystemFont(12),
    color: BRICK,
  });

  w.addSpacer(4);
  brickRule(w, 3);
  w.addSpacer(8);

  // Stats + sparkline
  const cols = w.addStack();
  cols.layoutHorizontally();
  cols.spacing = 12;

  const left = cols.addStack();
  left.layoutVertically();
  addText(left, `${m.threads_active_24h ?? "—"} threads (24h)`, {
    font: Font.systemFont(11),
    color: INK,
  });
  addText(left, `Catalog  ${m.threads_in_catalog ?? "—"}`, {
    font: Font.systemFont(10),
    color: MUTED,
  });
  addText(left, `Peak  ${m.peak_hour_ist || "—"}`, {
    font: Font.systemFont(10),
    color: MUTED,
  });

  cols.addSpacer();

  const right = cols.addStack();
  right.layoutVertically();
  if (m.hourly_sparkline) {
    addText(right, m.hourly_sparkline, {
      font: Font.boldMonospacedSystemFont(13),
      color: BRICK,
      lines: 1,
    });
    addText(right, "24h ago        now", {
      font: Font.systemFont(7),
      color: MUTED,
    });
  }

  w.addSpacer();
  if (pulse && pulse.ticker) {
    addText(w, pulse.ticker, {
      font: Font.italicSystemFont(10),
      color: INK,
      lines: 2,
    });
  }
}

function renderLarge(w, issue, pulse) {
  const m = (pulse && pulse.metrics) || (issue && issue.metrics) || {};

  // Masthead row
  const head = w.addStack();
  head.layoutHorizontally();
  head.bottomAlignContent();
  addText(head, "East of Indus", {
    font: new Font("Times New Roman", 22),
    color: INK,
  });
  head.addSpacer();
  addText(head, m.rating || "—", {
    font: Font.boldSystemFont(13),
    color: BRICK,
  });

  w.addSpacer(4);
  brickRule(w, 3);

  // Sparkline (sized to fit large widget width: 24 chars × ~12px = 288px, fits)
  w.addSpacer(10);
  if (m.hourly_sparkline) {
    addText(w, m.hourly_sparkline, {
      font: Font.boldMonospacedSystemFont(12),
      color: BRICK,
      lines: 1,
    });
  }
  addText(w, `${m.threads_active_24h ?? "?"} threads · peak ${m.peak_hour_ist || "—"}`, {
    font: Font.systemFont(10),
    color: MUTED,
  });

  // Quote of the day
  w.addSpacer(12);
  const quote = issue && issue.quote_of_day;
  if (quote && quote.text) {
    addText(w, "QUOTE OF THE DAY", {
      font: Font.boldSystemFont(8),
      color: MUTED,
    });
    w.addSpacer(2);
    const q = quote.text.length > 180 ? quote.text.slice(0, 177).trim() + "…" : quote.text;
    addText(w, '"' + q + '"', {
      font: Font.italicSystemFont(11),
      color: INK,
      lines: 5,
    });
    w.addSpacer(2);
    addText(w, `— ${quote.name || "Anon"} · No. ${quote.post_no || "—"}`, {
      font: Font.systemFont(9),
      color: MUTED,
    });
  }

  // Leading headline
  w.addSpacer(10);
  const lead = issue && (issue.articles || []).find(function (a) { return a.section === "Leading"; });
  if (lead) {
    addText(w, "LEADING", {
      font: Font.boldSystemFont(8),
      color: MUTED,
    });
    w.addSpacer(2);
    addText(w, lead.title, {
      font: Font.semiboldSystemFont(13),
      color: INK,
      lines: 2,
    });
  }

  w.addSpacer();
  // Issue marker at the very bottom
  if (issue) {
    addText(w, `${(issue.date || "")} · ${(issue.issue_no || "")}`, {
      font: Font.monospacedSystemFont(9),
      color: MUTED,
      align: "right",
    });
  }
}

async function build() {
  const [issue, pulse] = await Promise.all([fetchJSON(ISSUE_URL), fetchJSON(PULSE_URL)]);

  const w = new ListWidget();
  w.backgroundColor = PAPER;
  w.setPadding(12, 14, 12, 14);
  w.url = OPEN_URL; // tap opens the paper in Safari

  const family = config.widgetFamily || "medium";
  if (family === "small")       renderSmall(w, issue, pulse);
  else if (family === "large")  renderLarge(w, issue, pulse);
  else                          renderMedium(w, issue, pulse);

  // Hint to iOS — actual refresh cadence is at iOS's discretion (typically 15-30 min)
  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
  return w;
}

const widget = await build();

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  // Local preview when running directly inside the Scriptable app
  await widget.presentMedium();
}

Script.complete();
