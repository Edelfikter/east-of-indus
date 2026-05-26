// East of Indus — cron-driven workflow trigger
//
// Cloudflare Workers cron is reliable (unlike free-tier GitHub Actions cron, which
// can be delayed for hours or skipped entirely). This worker fires on schedule and
// POSTs to GitHub's workflow_dispatch API to force-start our pipeline workflows.
//
// Required secret: GITHUB_TOKEN  (classic PAT with `repo` + `workflow` scopes)

const OWNER = "Edelfikter";
const REPO = "east-of-indus";

// One cron pattern → one workflow filename. Multiple patterns may map to the same workflow.
const SCHEDULES = {
  "7 * * * *":   "pulse.yml",  // hourly pulse: ticker + live metrics
  "30 1 * * *":  "eoi.yml",    // 01:30 UTC = 07:00 IST  morning issue
  "30 12 * * *": "eoi.yml",    // 12:30 UTC = 18:00 IST  evening issue
};

async function trigger(workflow, token) {
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "eoi-cron-trigger",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  return resp;
}

export default {
  async scheduled(event, env, ctx) {
    const workflow = SCHEDULES[event.cron];
    if (!workflow) {
      console.log("No mapping for cron:", event.cron);
      return;
    }
    if (!env.GITHUB_TOKEN) {
      console.error("GITHUB_TOKEN secret not set");
      return;
    }
    const resp = await trigger(workflow, env.GITHUB_TOKEN);
    if (!resp.ok) {
      const body = await resp.text();
      console.error(`dispatch ${workflow} failed: ${resp.status} ${body}`);
    } else {
      console.log(`dispatched ${workflow} for cron ${event.cron}`);
    }
  },

  // HTTP handler for manual testing: hit https://eoi-cron.../trigger?w=pulse.yml
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/trigger") {
      return new Response("eoi-cron alive. Use /trigger?w=<workflow.yml>", { status: 200 });
    }
    const w = url.searchParams.get("w");
    if (!w || !["pulse.yml", "eoi.yml"].includes(w)) {
      return new Response("bad workflow", { status: 400 });
    }
    const resp = await trigger(w, env.GITHUB_TOKEN);
    // GitHub returns 204 No Content on success. Constructing a Response with a
    // body AND a null-body status (204/205/304) throws in Workers, so collapse
    // success to 200 and only forward non-success codes verbatim.
    const status = resp.ok ? 200 : resp.status;
    return new Response(`dispatched ${w}: ${resp.status}`, { status });
  },
};
