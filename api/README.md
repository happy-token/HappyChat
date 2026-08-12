# HappyChat API

`happychat-api` 是 Open WebUI 的单一配套服务，在同一个进程中提供两个监听端口：

- `8080`：公开策略代理，位于 Cloudflare Worker 和 Open WebUI 之间。
- `8000`：仅容器内网使用的 OpenAI 兼容模型路由。

服务验证 Open WebUI 签名的用户 JWT，将用户映射到 HappyToken/NewAPI 账户，并代表
用户调用模型服务。任何 NewAPI Token 都不能返回浏览器或写入 Open WebUI 用户配置。

Open WebUI 请求 `/api/models?refresh=true` 时，公开监听器会先通过带服务凭证的内网
接口清除模型目录缓存，再让 Open WebUI 重新读取 `/v1/models`。失效接口不通过公开
入口发布，凭证错误时拒绝请求。

生产镜像由仓库工作流发布到 `ghcr.io/happy-token/happychat-api`，生产服务器只拉取
镜像，不从源码构建。
