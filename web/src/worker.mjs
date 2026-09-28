import { adminHtml, adminScript } from "./model-admin.mjs";

const DEFAULT_BACKEND_ORIGIN = "https://chat-b.happy-token.cn";

function isNavigation(request) {
  return (
    request.method === "GET" &&
    (request.headers.get("Accept") || "").includes("text/html") &&
    request.headers.get("Upgrade")?.toLowerCase() !== "websocket"
  );
}

function mustProxy(pathname) {
  return ["/api/", "/oauth/", "/ws/", "/openai/", "/ollama/"].some((prefix) =>
    pathname.startsWith(prefix),
  );
}

function withAssetHeaders(response) {
  const headers = new Headers(response.headers);
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("Permissions-Policy", "camera=(), microphone=(), geolocation=()");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function rewriteProxyResponse(response, backendOrigin, publicOrigin) {
  if (response.status === 101) {
    return response;
  }
  const headers = new Headers(response.headers);
  const location = headers.get("Location");
  if (location?.startsWith(backendOrigin)) {
    headers.set("Location", `${publicOrigin}${location.slice(backendOrigin.length)}`);
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

async function proxyRequest(request, env, fetchImpl) {
  const publicUrl = new URL(request.url);
  const backendOrigin = (env.BACKEND_ORIGIN || DEFAULT_BACKEND_ORIGIN).replace(/\/$/, "");
  const upstreamUrl = new URL(`${publicUrl.pathname}${publicUrl.search}`, backendOrigin);
  const headers = new Headers(request.headers);
  headers.set("X-Forwarded-Host", publicUrl.host);
  headers.set("X-Forwarded-Proto", publicUrl.protocol.slice(0, -1));
  const upstreamRequest = new Request(upstreamUrl, {
    method: request.method,
    headers,
    body: request.body,
    ...(request.body ? { duplex: "half" } : {}),
    redirect: "manual",
  });
  const response = await fetchImpl(upstreamRequest);
  return rewriteProxyResponse(response, backendOrigin, publicUrl.origin);
}

export async function handleRequest(request, env, fetchImpl = fetch) {
  const url = new URL(request.url);
  if (["/admin/models", "/admin/models/", "/admin/happychat-models.js"].includes(url.pathname) && ["GET", "HEAD"].includes(request.method)) {
    const script = url.pathname.endsWith(".js");
    return withAssetHeaders(new Response(request.method === "HEAD" ? null : script ? adminScript : adminHtml, {
      headers: { "Content-Type": script ? "text/javascript; charset=utf-8" : "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'" },
    }));
  }
  if (url.pathname === "/__happychat/health") {
    const response = await proxyRequest(
      new Request(new URL("/health", url), { method: "GET", headers: request.headers }),
      env,
      fetchImpl,
    );
    return new Response(
      JSON.stringify({ status: response.ok ? "ok" : "degraded", backend_status: response.status }),
      { status: response.ok ? 200 : 503, headers: { "Content-Type": "application/json" } },
    );
  }

  if (mustProxy(url.pathname)) {
    return proxyRequest(request, env, fetchImpl);
  }

  if ((request.method === "GET" || request.method === "HEAD") && request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
    const assetResponse = await env.ASSETS.fetch(request);
    if (assetResponse.status !== 404) {
      return withAssetHeaders(assetResponse);
    }
    if (isNavigation(request)) {
      const indexRequest = new Request(new URL("/index.html", url), request);
      return withAssetHeaders(await env.ASSETS.fetch(indexRequest));
    }
  }

  return proxyRequest(request, env, fetchImpl);
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },
};
