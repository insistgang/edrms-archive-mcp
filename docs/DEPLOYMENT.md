# Docker部署与现场验证

当前本机未安装Docker/Podman，不能在此声明镜像已运行。GitHub容器CI执行通用Linux镜像的构建与Mock冒烟；其成功也不等于客户银河麒麟/鲲鹏、东方通、网关及生产环境已经验证。

## 构建与演示

```bash
docker build -t edrms-archive-mcp:local .
RUN_MODE=MOCK docker compose up -d --build
docker compose ps
docker compose logs --tail=50
curl http://127.0.0.1:8000/health
docker compose down
```

镜像默认非root（UID/GID10001），应用监听容器0.0.0.0:8000，Compose仅映射宿主127.0.0.1。文件系统只读，/tmp为64MiB临时文件系统，丢弃Linux capabilities，禁止提权。Docker运行时/内核与镜像架构仍需客户核对。

/health当前是活性/上游版本探测，不证明用户鉴权、原文和模型都健康；必须额外调用MCP工具验收。

## 现场配置

由批准的服务管理器或安全环境注入：RUN_MODE=LIVE、EDRMS_BASE_URL、超时。请求携带每个真实用户的Token；不要把管理员密码、模型密钥或用户Token放入镜像层/compose.yaml/仓库。服务不会自动登录或刷新会话。

默认关闭服务端模型生成msg；客户模型独立接入MCP。当前compose不传模型密钥，避免无意外发档案。

若开启审计，使用现场独立override显式配置EDRMS_AUDIT_LOG，挂载专用可写日志卷并确保10001用户可写。默认compose不落审计盘；原文不持久化。OCR可写临时目录与执行组件需客户批准并限制资源。

原文导出目录不默认挂载。若仅人工检查文件，可以只读挂载到单独路径；当前MCP**不会因为挂载目录就具备本地检索能力**。目录同步、版本、权限撤销和用户隔离没有设计验收前，不可将管理员导出副本作为多用户知识库。

## 离线与架构

- 获批基础镜像用PYTHON_IMAGE指定，生产建议按digest固定并经过客户漏洞/许可证审查。
- x86_64/ARM64须分别验证镜像、Python和可选PDF/OCR依赖，不能把容器当作跨架构或信创认证保证。
- 在有网络且获批的构建机构建目标平台镜像后，docker save到客户批准介质；现场docker load。不要把客户数据打进镜像。
- 目标镜像名称/内网仓库/拉取策略/资源限额/网关TLS/开放端口/停机回退窗口待现场确认。

## 验收与回退

在现场执行四工具闭环、普通A/B账号、真实业务格式、令牌失效/权限撤销、资源/并发/日志检查。写操作不在此服务范围。记录镜像digest及配置版本，不记凭据。未通过则停止新容器并回到前一个已验收镜像，不修改EDRMS数据。
