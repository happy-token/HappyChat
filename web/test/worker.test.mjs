import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest } from "../src/worker.mjs";


function environment(assetHandler) {
  return {
    BACKEND_ORIGIN: "https://chat-b.happy-token.cn",
    ASSETS: { fetch: assetHandler },
  };
}


test("serves existing Open WebUI assets without contacting the backend", async () => {
  const env = environment(async () => new Response("asset", { status: 200 }));
  const response = await handleRequest(
    new Request("https://chat.happy-token.cn/_app/app.js"),
    env,
    () => assert.fail("backend fetch should not run"),
  );
  assert.equal(await response.text(), "asset");
  assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
});


test("falls back to index.html for browser navigation", async () => {
  const requestedPaths = [];
  const env = environment(async (request) => {
    const path = new URL(request.url).pathname;
    requestedPaths.push(path);
    return path === "/index.html" ? new Response("index") : new Response("missing", { status: 404 });
  });
  const response = await handleRequest(
    new Request("https://chat.happy-token.cn/c/chat-id", { headers: { Accept: "text/html" } }),
    env,
    () => assert.fail("backend fetch should not run"),
  );
  assert.equal(await response.text(), "index");
  assert.deepEqual(requestedPaths, ["/c/chat-id", "/index.html"]);
});


test("proxies API requests and rewrites backend redirects", async () => {
  const env = environment(async () => new Response("missing", { status: 404 }));
  let upstreamRequest;
  const response = await handleRequest(
    new Request("https://chat.happy-token.cn/oauth/oidc/login?next=%2F", {
      headers: { Accept: "text/html" },
    }),
    env,
    async (request) => {
      upstreamRequest = request;
      return new Response(null, {
        status: 302,
        headers: { Location: "https://chat-b.happy-token.cn/oauth/oidc/callback" },
      });
    },
  );
  assert.equal(upstreamRequest.url, "https://chat-b.happy-token.cn/oauth/oidc/login?next=%2F");
  assert.equal(upstreamRequest.headers.get("X-Forwarded-Host"), "chat.happy-token.cn");
  assert.equal(response.headers.get("Location"), "https://chat.happy-token.cn/oauth/oidc/callback");
});


test("health endpoint reports the proxied backend status", async () => {
  const env = environment(async () => new Response("missing", { status: 404 }));
  const response = await handleRequest(
    new Request("https://chat.happy-token.cn/__happychat/health"),
    env,
    async () => new Response("ok"),
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: "ok", backend_status: 200 });
});
