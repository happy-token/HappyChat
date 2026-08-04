# HappyChat

HappyChat 是 HappyToken 的工作助手产品仓库。产品复用固定版本的 Open WebUI 官方
镜像，不修改其源码；本仓库只维护 HappyToken 自有的 API 配套服务和 Cloudflare
前端入口。

## 部署结构

```text
浏览器
  -> chat.happy-token.cn
  -> Cloudflare Worker happychat-web（静态前端和同源代理）
  -> chat-b.happy-token.cn
  -> tx1 上的 happychat-api
  -> tx1 上的 Open WebUI 官方镜像
```

- `api/`：`happychat-api` 源码、测试和 Dockerfile。CI 发布
  `ghcr.io/happy-token/happychat-api`，服务器不从源码构建。
- `web/`：Cloudflare Worker。构建时从固定 Open WebUI 镜像提取原生静态前端，
  动态请求代理到 `https://chat-b.happy-token.cn`。
- `.github/workflows/`：后端镜像发布和 Cloudflare Worker 发布流程。

服务器 Compose、私有环境变量、数据库初始化和回滚操作由相邻的
`HappyServices/happychat/` 管理。Cloudflare 资源和域名总表位于
`HappyServices/docs/architecture/cloudflare.md`。

## 本地验证

```sh
python3 -m unittest discover -s api/tests -p 'test_*.py'
docker build -t happychat-api:local api

cd web
npm ci
npm run build:assets
npm test
npm run deploy:dry-run
```

真实密钥只能存放在 GitHub Secrets、Cloudflare Secrets 或部署环境中，不能提交到
本仓库。
