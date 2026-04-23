export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/healthz") {
      return json({ ok: true, service: "heartbeat-worker" });
    }

    if (url.pathname === "/heartbeat" && request.method === "POST") {
      const authError = checkAuth(request, env.HEARTBEAT_WRITE_TOKEN);
      if (authError) {
        return authError;
      }

      let payload;
      try {
        payload = await request.json();
      } catch {
        return json({ ok: false, error: "invalid_json" }, 400);
      }

      const nodeId = String(payload?.node_id || "").trim();
      const ts = Number(payload?.ts || 0);
      const hostname = String(payload?.hostname || "").trim();

      if (!nodeId || !Number.isFinite(ts) || ts <= 0) {
        return json({ ok: false, error: "invalid_payload" }, 400);
      }

      const now = Math.floor(Date.now() / 1000);
      const record = {
        node_id: nodeId,
        ts,
        hostname,
        received_at: now,
      };

      await env.HEARTBEAT_KV.put(`hb:${nodeId}`, JSON.stringify(record));
      return json({ ok: true, node_id: nodeId, received_at: now });
    }

    if (url.pathname === "/status" && request.method === "GET") {
      const authError = checkAuth(request, env.HEARTBEAT_READ_TOKEN);
      if (authError) {
        return authError;
      }

      const nodeId = String(url.searchParams.get("node_id") || "").trim();
      const maxAge = Math.max(10, Number(url.searchParams.get("max_age") || "90"));
      if (!nodeId) {
        return json({ ok: false, error: "node_id_required" }, 400);
      }

      const raw = await env.HEARTBEAT_KV.get(`hb:${nodeId}`);
      if (!raw) {
        return json({
          ok: true,
          node_id: nodeId,
          healthy: false,
          reason: "missing",
        });
      }

      let record;
      try {
        record = JSON.parse(raw);
      } catch {
        return json({ ok: false, error: "corrupt_record" }, 500);
      }

      const now = Math.floor(Date.now() / 1000);
      const age = now - Number(record.received_at || record.ts || 0);

      return json({
        ok: true,
        node_id: nodeId,
        healthy: age <= maxAge,
        age_sec: age,
        max_age_sec: maxAge,
        last_seen: Number(record.received_at || record.ts || 0),
        hostname: record.hostname || "",
      });
    }

    return json({ ok: false, error: "not_found" }, 404);
  },
};

function checkAuth(request, expectedToken) {
  if (!expectedToken) {
    return null;
  }
  const header = request.headers.get("authorization") || "";
  if (header !== `Bearer ${expectedToken}`) {
    return json({ ok: false, error: "unauthorized" }, 401);
  }
  return null;
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}
