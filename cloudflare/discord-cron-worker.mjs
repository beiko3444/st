async function sendInventoryReport(env) {
  const appUrl = String(env.APP_URL || "").replace(/\/+$/, "");
  const cronSecret = String(env.CRON_SECRET || "");
  if (!appUrl) {
    throw new Error("APP_URL is required");
  }
  if (!cronSecret) {
    throw new Error("CRON_SECRET is required");
  }

  const response = await fetch(`${appUrl}/api/reports/inventory/discord`, {
    method: "GET",
    headers: {
      Authorization: `Bearer ${cronSecret}`,
    },
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`inventory report failed: ${response.status} ${body}`);
  }
}

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(sendInventoryReport(env));
  },

  async fetch() {
    return new Response("SmartInventory Discord cron worker", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
