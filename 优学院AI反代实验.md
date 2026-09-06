# 优学院 AI 反代实验

本实验位于 `codex/ulearning-ai-bridge` 分支。独立 AI 工作台和普通网页端刷课的自动答题已经接入课程 AI；现有 Agent CLI 答题链、签到逻辑与固定答案降级流程保持独立。

## 已确认的协议

- AI 页面位于 `aijx.dgut.edu.cn` 的跨域 frame 中。
- 对话接口为 `POST /api/kbChat/chat`，响应为 SSE (`text/event-stream`)。
- `sessionId` 与 `requestId` 由网页本地生成，不依赖预创建接口。
- 模型列表从 `GET /api/kbChat/getModelListByOrgId` 动态读取，用户选择的 `modelId` 同时用于独立对话和刷课自动答题；首次请求使用 `sessionSign=1`。
- 课程与短期授权可从尚未“进入对话”的 Workbench frame 读取，再通过 `GET /api/workbenches/assistantListByOcId` 获取助手标识，因此无需代替用户点击“进入对话”。
- LMS Workbench 官方脚本直接使用 `AUTHORIZATION` Cookie 作为 `auth` 参数。因此程序可以优先复用内存中的登录缓存和课程列表构造相同上下文，完全不打开优学院；浏览器 frame 发现仅作为兼容降级路径。
- 活动 frame 的短期授权、浏览器 Cookie、User-Agent 和 Referer 均只在内存中使用，不写配置或日志。
- 课程暴露的 instruction 与 instructionGraph 当前均为空数组。这不能证明服务端不存在统一的隐藏系统提示。
- `toolsContentDTOS` 是服务器下发工具调用执行后的结果回传字段，并非客户端自定义工具声明入口。

## 程序页面

主程序网页不内嵌聊天界面。展开主程序侧栏并点击“AI 工作台”，浏览器会打开独立的 `/ai.html` 页面；该页面复用主程序的本地服务和生命周期。

只要程序已经通过登录缓存读取到课程，独立页面就能直接连接 AI，无需打开优学院。缓存失效时可沿用程序现有的登录恢复流程，或退回同一调试浏览器中的课程 AI 上下文。页面会动态显示该组织当前启用的模型；所选模型会保存为程序设置，不会新增长期授权缓存。

## 独立调试服务

下列命令仅保留给协议调试，不是正常程序入口：

```powershell
python tools/ulearning_ai_bridge_server.py
```

服务仅监听 `127.0.0.1:8786`。启动时会输出一次 JSON，其中包含临时 `baseUrl` 和 `apiKey`。关闭进程后该密钥失效。

当前提供：

- `GET /health`
- `POST /v1/chat/completions`
- OpenAI 风格的非流式响应
- `system`、`assistant`、`user` 文本消息折叠为一次课程 AI 请求

当前明确不提供：

- 流式本地响应
- 图片和文件消息
- 客户端自定义 tools
- 模型直接执行签到或任意程序操作

刷课模块只把当前测验的结构化题目交给模型，并严格校验 JSON 答案；真正的页面输入和单次提交仍由本地受控流程完成。AI 生成失败时可在尚未操作页面的前提下整卷降级到固定答案。
- GUI 入口、开机自启或发布包集成

## 安全边界

服务只允许带启动时临时 Bearer 密钥的本机请求。请求正文不会由 HTTP 服务写日志，上游授权值的对象表示也已隐藏。

程序操作应沿用现有 Agent 工具的校验、任务所有权和状态机制。后续若增加工具调度，应先让模型生成候选调用，再由本地白名单解析、参数校验和确认策略决定是否执行；不能把任意模型文本当命令运行。

`yxy_capture_fixed.py` 和它生成的原始抓包可能含 Cookie、Token、用户与课程信息，仅用于本机调查，不属于该实验的提交内容。

## 下一阶段

1. 给本地接口增加 SSE 流式输出。
2. 设计“模型提出工具调用、本地 Agent 层验证并执行、结果回传模型”的独立调度协议。
3. 验证授权失效后的重新发现与恢复，不保存长期凭据。
