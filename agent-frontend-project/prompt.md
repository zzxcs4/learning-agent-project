# 项目初始化提示

初始化一个名为 `{{agent-frontend-project}}` 的新项目。

**项目配置:**

- **框架:** `{{Vue}}`
- **语言:** `{{TypeScript}}`
- **包管理器:** `{{npm}}`
- **UI 框架:** `{{Element Plus}}`
- **状态管理:** `{{pinia}}`
- **构建工具:** `{{Vite}}`

**操作指令:**

1.  创建项目目录。
2.  使用指定的框架和语言初始化项目。
3.  安装并配置所有必要的依赖项，包括 UI 框架。
4.  配置代码检查和格式化工具。
5.  确保项目可以成功构建和运行。

# 实现前端登录功能：

1. 引入 axios 并二次封装 axios（请求头带 token，401 自动刷新并重试，失败清理 token 并跳登录）。
2. 统一错误提示：全局捕获 axios 错误信息；
3. 调用接口文档中的登录注册接口，处理登录和注册逻辑。
4. 路由前置守卫拦截未登录：在路由守卫中检查 token，未登录则跳转到登录页。
5. 登录成功后，保存 token 到 localStorage。
6. 头部按钮支持退出登录：添加退出登录按钮，点击后清理 token 并跳转到登录页。

# 用户注册时，实现头像上传功能

1. 用户选择头像时， 只在本地预览 ，不上传。
2. 点击注册时，先用文本信息创建账号，成功后 自动登录 以获取 Token。
3. 登录成功后，再发起 已认证 的头像上传请求。”
4. “使用 Pinia 全局管理用户信息”

# 实现聊天页面会话管理，对话数据回显：

- 会话管理：支持会话列表分页（`GET /sessions`）、新建（`POST /sessions`）、编辑标题（`PUT /sessions/{session_id}`）、删除（`DELETE /sessions/{id}`，操作后刷新并切换会话）。
- 聊天信息：按会话ID分页获取消息（`GET /sessions/{session_id}/messages`），正文渲染 `request_text/response_text`，附件渲染 `request_segments/response_segments`（只含 `file/image/audio`，字段容错，结构为 `type/name/url/resource_id`）。
- 消息区：正文与附件分区显示，附件用独立组件（如 `MsgFile.vue`），图片失败显示占位图，音频失败有提示。
- 消息加载：上拉分页（距离顶部100px触发），loading锁防重入，增量追加，按 `created_at` 排序。

全部需对接接口如下：

- `GET /sessions`
- `POST /sessions`
- `PUT /sessions/{session_id}`
- `DELETE /sessions/{id}`
- `GET /sessions/{session_id}/messages`

# 请按“API层 + 复用逻辑层 + 页面层”实现 Vue 流式聊天：

目标：

- 使用 fetch 发送请求
- 收到实时数据流拼接文本

架构要求：

1. API层（streamChat.ts）：只负责请求与SSE解析
2. 复用逻辑层（useSessionStreamSender.ts）
3. 页面层（StudyRoom.vue、NotebookRoom.vue）：只处理会话UI与占位消息

# 请在前端 Vue 项目中开发通用型AI对话答案Markdown渲染美化方案，**独立封装 markdownUtil.ts 工具文件**，核心功能如下：

1. 基于MarkdownIt解析Markdown，兼容基础语法；
2. 集成Prism.js实现代码高亮，代码块带语言标识；
3. 美化表格，支持横向滚动、斑马纹、悬浮高亮；
4. 支持代码主题高亮显示；
5. 代码低耦合，可直接迁移至任意系统。

# 阅读 【ChatInput.vue】 和 【upload.ts】，
在聊天输入框的回形针按钮上接入文档上传：
- 弹出对话框，使用 el-upload（drag）支持点击 + 拖拽
- 对话框内含文件类型下拉框（简历/学习资料/通用文档），默认未选中
  - 📌 **2026-10-18 变更**：该下拉框**已删除**（连同 `DocCategory` 类型与后端 `doc_category` 字段）。
    原因见 `升级规划.md` 第八节附记：这个分类从上线起没有任何读取方，选它只是白多一步操作。
    本文其余部分是当时的需求存档，未改动。
- 仅支持 pdf  / docx / txt
- 用户选完文件 + 选完类型后点"提交"才调用 POST /upload/file
  - 📌 **2026-10-18 变更**：现在只需"选完文件"，提交按钮不再要求选类型。
- 复用项目 axios 实例，不用 el-upload 内置 action
- TypeScript 严格类型，仅改这两个文件
先给我修改方案，我确认后再改。

# 请在现有流式对话页面中增加“参考知识片段”展示功能。

后端会在 SSE done 事件和历史消息接口中返回 sources。

实现思路：
1. 保留现有流式 delta 渲染逻辑。
2. done 事件到达时，把 data.sources 保存到当前 AI 消息。
3. 历史消息中的 message.sources 也使用同一组件展示。
4. sources 为空时不展示。
5. sources 有数据时，在 AI 回答下方显示默认收起的折叠面板：
   “参考了 N 个知识片段”
6. 展开后展示 file_name、score 百分比、text。
7. 不把 sources 拼进 response_text。

1. 测试实时sources 展示
2. 测试历史消息sources回显
3. 测试闲聊短句跳过检索、不展示sources
4. 测试无关问题无RAG溯源
5. 长文本折叠交互



### 聊天问答接口改造，接入总控路由接口

- 接口：`POST /agent/tasks/start` 
- 响应处理：SSE 沿用原事件；JSON 视为工作流启动，返回统一结果展。
- 输入框底部加智能体按钮（学习室：`quiz_generate_workflow`、`difficulty_learner`、`note_archiver`），支持选中/取消。
- 发送消息升级为 `{ text, agentName }`，沿 `ChatInput → ChatWindow → 页面 → useSessionStreamSender → streamChat` 透传。
- JSON 响应刷新会话，SSE 不刷新；加 loading 提示；旧接口用注释保留。

---

### 学习测评工作流

- 类型：`SessionRecord`/`UiMessage` 增加 `request_id`、`response_payload`，历史映射给 assistant。
- 入口：assistant 消息满足 `requestId` 含 `quiz` 且 `responsePayload` 存在 → 展示“查看并填写测评题目”，首次渲染自动弹窗。
- 弹窗：独立组件，根据 `response_payload.quiz.questions` 渲染单选/多选/文本域，提交前校验必答题。
- 提交：取 `run_id`（从 `run_id` 或 `request_id` 冒号前半段），调用 `POST /learning-workflows/{run_id}/submit`，格式 `{ answers: [{ question_id, value }] }`。
- 提交时 loading“正在提交答案并生成评分...”，成功后刷新会话，原消息变为评分文本，入口消失。

