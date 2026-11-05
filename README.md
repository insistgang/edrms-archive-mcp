# EDRMS Archive MCP

鸿翼 EDRMS 档案检索的轻量 MCP 适配服务。复用原系统接口与用户身份，不替代档案系统、不新建问答门户、不默认建立向量库或复制全量原文。

状态：**联调版本，尚未生产验收**。已实现四工具、标准 MCP HTTP、HTTP JSON 兼容接口、格式解析和模型接入客户端。客户原始方案、录音、截图、档案导出和凭据不纳入此仓库。

## 调用关系

```text
客户问答平台 / OpenAI兼容模型客户端
→ 标准 MCP HTTP /mcp
→ 当前用户身份下的 EDRMS API
→ 目录信息、原文记录、带来源的文本片段
→ 客户模型根据证据生成回答
```

常规使用按需实时读取；本地导出脚本只是辅助工具，MCP 不依赖导出目录。

## 工具与接口

| 工具 | 职责 | HTTP JSON兼容入口 |
|---|---|---|
| archive_search | 查询文件目录，返回文件/门类/原文ID | POST /mcp/archive/rag |
| archive_get_detail | 文件或案卷详情、卷内文件 | POST /mcp/archive/detail |
| archive_list_originals | 原文记录列表，不暴露下载版本ID | POST /mcp/archive/originals |
| archive_read_original | 核对关联、下载、受控解析并返回来源片段 | POST /mcp/archive/original/read |

标准 MCP：`POST /mcp`，支持 initialize、initialized通知、ping、tools/list、tools/call。当前为无会话JSON响应，不支持SSE、stdio或OAuth。普通HTTP JSON端点不是标准MCP。

- 检索兼容响应保留 `data`、`msg`；记录包含 `name/year/attachmentsId/fileId/caseId/archiveType/meta/archiveTypeId`。
- 实机 `archiveType`、`formUnit` 尚无完整权威映射，不伪造业务含义。
- `meta.originalsStatus` 可为 ok、none、incomplete、invalid_association、unavailable；原文列表异常不伪装成“没有原文”。
- 原文读取需要 `archiveTypeId/fileId/attachmentId/query`；返回 fragments、source、truncated、parser、warnings。
- 失败不自动降级为Mock。无身份拒绝；管理员成功或permType=1成功不证明跨用户隔离。

## 本地运行

Python 3.11及以上，核心服务和测试使用标准库，无需安装requirements.txt中的可选依赖。

```bash
RUN_MODE=MOCK python3 app_standalone.py
# http://127.0.0.1:8000：本地检索控制台，仅用于演示
# http://127.0.0.1:8000/mcp：标准MCP入口
```

Mock用户标识仅用于演示：token_normal_user、token_compliance_officer、token_risk_director。Mock数据为合成示例，不是客户档案，不构成权限验收。

```bash
RUN_MODE=MOCK python3 run_demo.py
python3 -m unittest discover -p 'test_*.py'
python3 acceptance_test.py
```

当前基线123项自动化测试、15项本地基础检查。计数随功能更新，不代表123条业务需求验收。

## 实机接入

无硬编码客户地址。默认LIVE模式必须由部署环境注入EDRMS_BASE_URL；未知环境不自动访问客户系统。

```bash
export RUN_MODE=LIVE
export EDRMS_BASE_URL='https://edrms.example.invalid'
python3 app_standalone.py
```

`.env.example`是参考模板，程序不会自动读取`.env`。实际令牌/密钥使用安全环境注入，不写进源码、Issue、终端截图或报告。不由MCP保存账号密码、自动登录或刷新令牌；客户应提供用户态Token或批准的SSO交换机制。

实机验收脚本使用环境指定的非敏感样本条件：

- EDRMS_TOKEN：当前用户令牌。
- EDRMS_ORIGINAL_QUERY：能唯一检索到有原文文件的关键词。
- EDRMS_CASE_QUERY：案卷检索词。
- EDRMS_EXPECTED_ORIGINAL_COUNT：可选原文数量预期。

```bash
python3 acceptance_test.py --live
python3 live_check.py --catalog --case-query '<样本词>' --max-chars 100000 --out /tmp/report.json
```

缺配置/跳过不算全部通过（退出2）。实机执行只能在授权测试环境进行，不在GitHub CI执行。

## 模型接入

客户平台支持MCP时，直接注册服务URL并按请求传用户身份。附带的OpenAI兼容命令行客户端实际经过MCP HTTP，不直读本地导出目录：

```bash
# 使用获批模型接口，由安全环境注入LLM_API_BASE、LLM_API_KEY、LLM_MODEL及EDRMS_TOKEN
python3 ask_archive.py '查询相关档案及原文要求' --mcp-url http://127.0.0.1:8000/mcp --json
```

模拟模型→真实本地MCP HTTP→Mock档案的四工具闭环已测；MCPClient→实际MCP HTTP→EDRMS测试样本也已实测。**未完成客户真实模型/Client验收。** EDRMS令牌不发送给模型；不要将档案发往未经客户批准的模型服务。

服务端msg默认由模板生成。`LLM_GENERATE_MSG=true`才允许适配服务另外调用模型生成msg，避免嵌套调用。单次工具结果超过模型上下文上限时返回合法JSON提示缩小范围，不返回无效半截JSON。

## 原文处理与边界

- ZIP下载字节在内存中校验、解压，不执行JS、宏或其他内容。
- docx/xlsx/UTF-8/ZIP及其内嵌docx/xlsx支持本地解析；真实业务样本仍待验收。
- PDF依赖获批pypdf/PyPDF2；图片依赖客户批准OCR（MCP_OCR_COMMAND）；旧doc/xls/ppt、pptx、嵌套普通ZIP不支持。
- 混合文档中的图片完整性检测尚未全部实现，不能把文字提取成功理解成整份文档读取完整。
- 统一解析输入20MiB、ZIP最多500成员、展开总量100MiB、单成员20MiB、压缩比200:1；工具最多100000字符、100片段。
- 拒绝危险路径、加密/特殊ZIP、DTD/实体及不安全工作簿引用；空文本明确报错，截断标志透传。
- 无MD5样本仅核对大小和格式结构，不宣称来源真实性校验。

## 辅助工具

| 文件 | 用途 |
|---|---|
| mcp_client.py | 标准MCP HTTP客户端，拒绝重定向和未知工具 |
| ask_archive.py / model_agent.py | 模型工具调用循环与命令行入口 |
| live_check.py | 当前身份可见的索引目录巡检和耗时采样 |
| permission_check.py | 两用户预期矩阵对照，未提供预期仅REVIEW |
| export_visible.py | 授权下只读导出目录/详情/附件，不覆盖已有输出 |
| audit.py | 可选最小JSONL审计，不记录Token或原文 |

权限脚本需不同用户Token A/B和业务方确认的预期矩阵；上游模糊“不可见”不算权限拒绝证明。输出PASS才退出0，FAIL退出1，REVIEW/SKIP退出2。

导出目录包含真实档案，应留在客户批准环境，不能加入Git或分享给其他身份。本仓库gitignore屏蔽导出、ZIP、原始文档、日志和环境凭据。

## 安全与验收

默认只监听本机。HTTP JSON默认同源，CORS仅允许显式精确来源。EDRMS授权语义、TLS、真实userId/对象级审计、日志留存、生产并发、信创平台均待现场验收。标准库依赖不等于信创已适配。

最小审计需EDRMS_AUDIT_LOG启用；只记录身份指纹、工具/计数/耗时，不能代替客户完整审计制度。

后续流程见：

- [开发实施流程](docs/WORKFLOW.md)
- [当前状态与验证边界](docs/STATUS.md)
- [变更记录](CHANGELOG.md)

新需求先建Issue，写清用户场景、身份、数据样本、接口变化与可验证的验收条件；实施、测试、记录和客户确认后再关闭。客户补充资料不自动上传，新增需求不等于扩大授权范围。
