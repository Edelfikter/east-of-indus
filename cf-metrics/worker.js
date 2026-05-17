// East of Indus — metrics-only worker
//
// Every 5 minutes, fetches the Induschan catalog (via our existing proxy),
// computes the same activity metrics as scrape.py's compute_metrics(), and
// uploads metrics.json to Supabase. No GH Actions, no Python, no AI tokens.
// The Blogger theme + iOS widget merge this onto pulse.json so the chart and
// numbers feel live while the AI ticker text stays on the hourly pulse cadence.
//
// Secrets required:
//   SUPABASE_SERVICE_ROLE_KEY   service-role key (write to Storage)

// PROXY is now a service binding (env.PROXY). See wrangler.toml.
const SUPABASE_URL = "https://nfpdtjqncwibgyrzvffr.supabase.co";
const BUCKET = "eoi";
const BOARD = "b";

const SPARK_BLOCKS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"];

function sparkline(values) {
  if (!values.length) return "";
  const max = Math.max(...values) || 1;
  return values.map(v => SPARK_BLOCKS[Math.min(7, Math.floor((v / max) * 7))]).join("");
}

// Rating based on bumps in the most recent hour, so it swings through the day.
function rateActivity(bumpsLastHour) {
  if (bumpsLastHour >= 15) return "Crazy";
  if (bumpsLastHour >= 10) return "Hyper";
  if (bumpsLastHour >= 5)  return "Brisk";
  if (bumpsLastHour >= 2)  return "Stale";
  if (bumpsLastHour >= 1)  return "Rotting";
  return "Dead";
}

function parseISO(s) {
  if (!s) return null;
  const t = Date.parse(s);
  return isNaN(t) ? null : t;
}

// Format Date as "HH:00 IST" (UTC+5:30)
function istHourLabel(dateMs) {
  const istMs = dateMs + (5 * 60 + 30) * 60 * 1000;
  const d = new Date(istMs);
  const h = String(d.getUTCHours()).padStart(2, "0");
  return `${h}:00 IST`;
}

function computeMetrics(catalog) {
  const now = Date.now();
  const win24 = now - 24 * 3600 * 1000;
  const win7d = now - 7 * 24 * 3600 * 1000;

  const bumps = catalog
    .map(t => parseISO(t.bumped || t.date))
    .filter(Boolean);

  const bumps24 = bumps.filter(b => b >= win24);
  const bumps7d = bumps.filter(b => b >= win7d);

  // 24 hourly buckets, oldest -> newest
  const hourly = new Array(24).fill(0);
  for (const b of bumps24) {
    const hoursAgo = (now - b) / 3600000;
    const bucket = 23 - Math.floor(hoursAgo);
    if (bucket >= 0 && bucket < 24) hourly[bucket] += 1;
  }

  // Peak / quietest
  let peakBucket = 0, quietBucket = 0;
  if (hourly.some(v => v > 0)) {
    peakBucket = hourly.indexOf(Math.max(...hourly));
    quietBucket = hourly.indexOf(Math.min(...hourly));
  }
  const peakHourMs = now - (23 - peakBucket) * 3600000;
  const quietHourMs = now - (23 - quietBucket) * 3600000;

  return {
    threads_in_catalog: catalog.length,
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
  // The hostname here is ignored; the request is routed directly to the bound worker
  const r = await env.PROXY.fetch(`https://proxy.internal/${BOARD}/catalog.json`);
  if (!r.ok) throw new Error(`catalog fetch failed: ${r.status}`);
  const catalog = await r.json();
  if (!Array.isArray(catalog)) throw new Error("catalog is not an array");
  const m = computeMetrics(catalog);
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
