// Induschan proxy — Cloudflare Worker
//
// Forwards any GET request to https://induschan.site at the same path.
// CF Worker requests originate from CF's own network, which bypasses the
// datacenter-IP block that hits GitHub Actions runners.
//
// Setup (5 min, no CLI):
//   1. https://dash.cloudflare.com → sign in / sign up free
//   2. Workers & Pages → Create → Create Worker
//   3. Name it (e.g. "induschan-proxy")
//   4. Paste the contents of THIS file into the editor
//   5. Save and Deploy
//   6. Copy the workers.dev URL it gives you (e.g. https://induschan-proxy.yourname.workers.dev)
//   7. Paste that URL back to me; I'll point scrape.py at it via INDUSCHAN_BASE env.

const ORIGIN = "https://induschan.site";

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
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": ORIGIN + "/b/",
      },
      // No edge caching — always fetch fresh from Induschan so freshly posted
      // !eastofindus threads land in the next pipeline run without a stale snapshot.
      cf: { cacheTtl: 0, cacheEverything: false },
    });

    const body = await upstream.arrayBuffer();
    return new Response(body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") || "application/json",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
