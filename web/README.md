# HappyChat Cloudflare 前端

此 Worker 使用 Cloudflare Static Assets 承载官方固定版本 Open WebUI 的原生前端，
并把 API、OIDC、文件及 WebSocket 请求代理到 `https://chat-b.happy-token.cn`。
它不复制或修改 Open WebUI 源码，也不移除 Open WebUI 品牌标识。

`build-assets.sh` 从 `ghcr.io/open-webui/open-webui:v0.11.0-slim` 提取
`/app/build` 到 Git 忽略的 `dist/`。部署前依次运行：

```sh
cd web
npm ci
npm run build:assets
npm test
npm run deploy:dry-run
npm run deploy
```

回滚使用 Cloudflare Worker 的上一版本；后端源站在 Worker 回滚期间保持不变。
