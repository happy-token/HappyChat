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

## 管理聊天模型

管理员在公开前端的 `/admin/models` 管理平台统一的聊天模型规则：

- 启用或停用 NewAPI 分组，用上移/下移调整显示顺序。
- 自动开放某组的新模型，或取消自动开放后逐项设置模型白名单。
- 选择完整的 `分组::模型` ID 作为新聊天默认模型；个人默认模型仍优先。
- 保存后新出现的分组默认禁用；列表刷新会清除网关目录缓存。

API 为 `GET/PUT /api/happychat/admin/models`，每次向 Open WebUI 校验当前
会话的管理员角色。PUT 另要求会话 Bearer Token，拒绝仅 Cookie 的请求。
`HAPPYCHAT_PUBLIC_ORIGIN` 默认 `https://chat.happy-token.cn`；浏览器保存请求必须
来自该 Origin，本地开发需按预览地址覆盖。普通用户不能读取或修改规则。模型目录和实际 Chat Completions 请求均应用规则，
因此停用模型后旧会话也不能继续调用；图片、语音接口保留原有能力配置。

`HAPPYCHAT_MODEL_POLICY_PATH` 默认 `/data/model-policy.json`。部署时必须挂载可写的
持久卷，容器 UID 为 10001。文件采用原子替换保存，缺少文件时沿用原有目录；
文件损坏时阻断聊天目录，禁止静默恢复成全部开放。

该功能设置平台统一范围，不实现按用户或用户组分别隐藏，也不调整 NewAPI 用户
余额、Token 分组或渠道配置。模型仍需通过 NewAPI 原有权限和额度校验。
前端和 API 必须一起更新，HappyServices Compose 已加入独立的设置持久卷。
恢复之前保存的 JSON 可回滚规则；回滚镜像版本时新规则不再由旧代码执行，
应先确认原有目录范围符合预期。开发或静态验证不发布生产资源。
