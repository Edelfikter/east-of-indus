// Imageboard proxy — Cloudflare Worker
//
// Forwards any GET request to the configured ORIGIN at the same path. Worker
// requests originate from CF's network, which bypasses datacenter-IP blocks
// that catch GH Actions runners. Indiachan.top doesn't currently gate behind
// Cloudflare anti-bot, but the proxy is kept for consistency, freshness
// control, and source-of-IP independence in case that ever changes.
//
// The worker is generic — point ORIGIN at any imageboard. Currently:
//   ORIGIN = https://indiachan.top  (Simplechan engine, HTML responses)

const ORIGIN = "https://indiachan.top";

export default {
  async fetch(request) {
    const incoming = new URL(request.url);
    const target = ORIGIN + incoming.pathname + incoming.search;

    const upstream = await fetch(target, {
      method: request.method,
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": ORIGIN + "/",
      },
      // No edge caching — Indiachan serves fresh HTML on every request and
      // we never want a stale catalog snapshot to bleed into the next pipeline run.
      cf: { cacheTtl: 0, cacheEverything: false },
    });

    const body = await upstream.arrayBuffer();
    return new Response(body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") || "text/html; charset=utf-8",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
