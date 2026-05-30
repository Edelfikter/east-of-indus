// East of Inch — metrics-only worker
//
// Every 5 minutes, fetches the Indiachan /b/ catalog HTML (via the
// induschan-proxy service binding — still named that for historical
// reasons), regex-extracts the data-bump epoch timestamps from each
// thread card, and computes the same activity metrics that scrape.py's
// compute_metrics() produces. Uploads metrics.json to Supabase.
//
// Indiachan exposes Unix-second bump timestamps as data-bump attributes
// on each <div class="post-container">. A handful of int extractions is
// all this worker needs — much cheaper than pulling a JS HTML parser
// into the 1MB compressed worker budget.
//
// Secrets required:
//   SUPABASE_SERVICE_ROLE_KEY   service-role key (write to Storage)

const SUPABASE_URL = "https://nfpdtjqncwibgyrzvffr.supabase.co";
const BUCKET = "eoi";
const BOARD = "b";

const SPARK_BLOCKS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"];

function sparkline(values) {
  if (!values.length) return "";
  const max = Math.max(...values) || 1;
  return values.map(v => SPARK_BLOCKS[Math.min(7, Math.floor((v / max) * 7))]).join("");
}

// Rating based on bumps in the most recent hour.
// Order (low -> high): Dead, Rotting, Stale, Brisk, Active, Hyper, Crazy.
function rateActivity(bumpsLastHour) {
  if (bumpsLastHour >= 20) return "Crazy";
  if (bumpsLastHour >= 14) return "Hyper";
  if (bumpsLastHour >= 7)  return "Active";
  if (bumpsLastHour >= 4)  return "Brisk";
  if (bumpsLastHour >= 2)  return "Stale";
  if (bumpsLastHour >= 1)  return "Rotting";
  return "Dead";
}

// Format epoch ms as "HH:00 IST" (UTC+5:30)
function istHourLabel(dateMs) {
  const istMs = dateMs + (5 * 60 + 30) * 60 * 1000;
  const d = new Date(istMs);
  const h = String(d.getUTCHours()).padStart(2, "0");
  return `${h}:00 IST`;
}

// Extract bump timestamps (ms) and thread-card count from raw catalog HTML.
// Indiachan format: every thread card is a <div class="post-container" ...
// data-bump="1780147219" ...>. Regex-extract data-bump; count occurrences of
// post-container as the catalog size.
function parseCatalog(html) {
  const bumps = [];
  const re = /data-bump="(\d+)"/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    bumps.push(parseInt(m[1], 10) * 1000); // epoch seconds -> ms
  }
  const cards = (html.match(/class="post-container"/g) || []).length;
  return { bumps, cards };
}

function computeMetrics(bumpsMs, cardCount) {
  const now = Date.now();
  const win24 = now - 24 * 3600 * 1000;
  const win7d = now - 7 * 24 * 3600 * 1000;

  const bumps24 = bumpsMs.filter(b => b >= win24);
  const bumps7d = bumpsMs.filter(b => b >= win7d);

  const hourly = new Array(24).fill(0);
  for (const b of bumps24) {
    const hoursAgo = (now - b) / 3600000;
    const bucket = 23 - Math.floor(hoursAgo);
    if (bucket >= 0 && bucket < 24) hourly[bucket] += 1;
  }

  let peakBucket = 0, quietBucket = 0;
  if (hourly.some(v => v > 0)) {
    peakBucket = hourly.indexOf(Math.max(...hourly));
    quietBucket = hourly.indexOf(Math.min(...hourly));
  }
  const peakHourMs = now - (23 - peakBucket) * 3600000;
  const quietHourMs = now - (23 - quietBucket) * 3600000;

  return {
    threads_in_catalog: cardCount,
    threads_active_24h: bumps24.length,
    threads_active_7d: bumps7d.length,
    bumps_last_hour: hourly[23],
    hourly_buckets_24h: hourly,
    hourly_sparkline: sparkline(hourly),
    peak_hour_ist: istHourLabel(peakHourMs),
    peak_count: hourly[peakBucket],
    quiet_hour_ist: istHourLabel(quietHourMs),
    rating: rateActivity(hourly[23]),
    computed_at: new Date(now).toISOString(),
  };
}

async function uploadMetrics(metrics, env) {
  const url = `${SUPABASE_URL}/storage/v1/object/${BUCKET}/metrics.json`;
  const body = JSON.stringify({
    synced_at: metrics.computed_at,
    metrics: metrics,
  }, null, 2);
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`,
      "apikey": env.SUPABASE_SERVICE_ROLE_KEY,
      "Content-Type": "application/json",
      "Cache-Control": "no-cache, max-age=0",
      "x-upsert": "true",
    },
    body,
  });
  if (!resp.ok) {
    throw new Error(`Supabase upload failed: ${resp.status} ${await resp.text()}`);
  }
}

async function run(env) {
  if (!env.SUPABASE_SERVICE_ROLE_KEY) {
    throw new Error("SUPABASE_SERVICE_ROLE_KEY not set");
  }
  if (!env.PROXY) {
    throw new Error("PROXY service binding not configured");
  }
  const r = await env.PROXY.fetch(`https://proxy.internal/boards/${BOARD}/catalog`);
  if (!r.ok) throw new Error(`catalog fetch failed: ${r.status}`);
  const html = await r.text();
  const { bumps, cards } = parseCatalog(html);
  if (cards === 0) throw new Error("no post-container cards found in catalog HTML");
  const m = computeMetrics(bumps, cards);
  await uploadMetrics(m, env);
  return m;
}

export default {
  async scheduled(event, env, ctx) {
    try {
      const m = await run(env);
      console.log(`metrics updated: ${m.threads_active_24h} threads (24h), rating=${m.rating}`);
    } catch (e) {
      console.error("metrics run failed:", e.message);
    }
  },
  async fetch(request, env) {
    try {
      const m = await run(env);
      return new Response(JSON.stringify(m, null, 2), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    } catch (e) {
      return new Response(`error: ${e.message}`, { status: 500 });
    }
  },
};
