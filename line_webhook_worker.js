export default {
  async fetch(request, env, ctx) {
    if (request.method === "GET") {
      return jsonResponse({ ok: true, message: "LINE webhook worker is ready." });
    }
    if (request.method !== "POST") {
      return jsonResponse({ ok: false, error: "method_not_allowed" }, 405);
    }
    if (!env.LINE_CHANNEL_SECRET || !env.APPS_SCRIPT_WEBHOOK_URL) {
      return jsonResponse({ ok: false, error: "missing_worker_env" }, 500);
    }

    const signature = request.headers.get("x-line-signature") || "";
    const body = await request.text();
    const isValid = await verifyLineSignature(body, env.LINE_CHANNEL_SECRET, signature);
    if (!isValid) {
      return jsonResponse({ ok: false, error: "invalid_signature" }, 401);
    }

    ctx.waitUntil(forwardToAppsScript(env.APPS_SCRIPT_WEBHOOK_URL, body));
    return jsonResponse({ ok: true });
  },
};

async function verifyLineSignature(body, channelSecret, signature) {
  if (!signature) {
    return false;
  }
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(channelSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  const expected = arrayBufferToBase64(digest);
  return timingSafeEqual(expected, signature);
}

async function forwardToAppsScript(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body,
    redirect: "follow",
  });
  if (!response.ok) {
    throw new Error(`Apps Script webhook failed: ${response.status}`);
  }
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
    },
  });
}

function arrayBufferToBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function timingSafeEqual(left, right) {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  if (leftBytes.length !== rightBytes.length) {
    return false;
  }
  let diff = 0;
  for (let index = 0; index < leftBytes.length; index += 1) {
    diff |= leftBytes[index] ^ rightBytes[index];
  }
  return diff === 0;
}
