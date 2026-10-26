# 页面结构拆分

## 目标
- 高度还原参考图页面结构。
- 采用 `layout / chat / history` 三域拆分，命名与职责更直观。
- 保留轻量交互，不引入后端耦合。

## 目录结构
```text
src/
  components/
    layout/
      AppHeader.vue       # 顶部主导航栏
      SideBar.vue         # 左侧边栏
    chat/
      ChatWindow.vue      # 聊天主窗口
      MessageList.vue     # 消息列表
      MessageBubble.vue   # 单条消息气泡
      ChatInput.vue       # 聊天输入区（工具栏：模式 → 附件 → 知识库 → 发送）
      ModePicker.vue      # 模式选择器（聊天 / 出题测评 / 知识卡片）
      KnowledgeBaseIcon.vue # 知识库图标（数据库圆柱，内联 SVG）
    history/
      HistoryList.vue     # 历史会话列表
      HistoryItem.vue     # 单条历史会话
  views/
    StudyRoom.vue         # 学习室主视图（整合所有组件 + 状态管理）
    NotebookRoom.vue      # 笔记本主视图
```

## 组件职责
- `StudyRoom.vue` / `NotebookRoom.vue`
  - 管理路由 Tab、高亮态、会话列表与消息数据。
  - 统一处理会话切换、新建会话、发送消息、附件点击事件。

- `AppHeader.vue`
  - 展示 Logo 与顶部导航。
  - 暴露 `tabClick` 事件。

- `SideBar.vue`
  - 展示“新建会话”按钮与历史区标题。
  - 组合 `HistoryList` 并透传 `createSession / sessionClick` 事件。

- `HistoryList.vue`
  - 渲染历史会话集合。
  - 组合 `HistoryItem`，处理激活项与图标分配。

- `HistoryItem.vue`
  - 单条会话 UI 与 hover/active 状态表现。

- `ChatWindow.vue`
  - 组合消息区与输入区。
  - 透传 `send / attach` 事件，并把模式清单、当前模式、示例提问透给输入区
    （`agentNameChange` 把用户换的模式回给 `StudyRoom`）。

- `MessageList.vue`
  - 渲染消息集合并监听消息变化自动滚底。

- `MessageBubble.vue`
  - 单条消息气泡，区分用户与 AI 样式。

- `ChatInput.vue`
  - 输入框 + 一条工具栏（**模式选择器 → 附件上传 → 知识库挂载 →（右侧）示例提问 → 发送**，
    模式选择器见 `ModePicker.vue`）。
  - 示例提问在工具栏**右侧一组**里横向排列、紧挨发送按钮左侧（不占输入框下方的一片空间）。
  - 知识库挂载是**会话级**的：按会话 id 分开记，切会话各自保留。
  - 支持回车发送（`Shift+Enter` 换行）。

- `ModePicker.vue`
  - 收起态只显示"当前模式 ▾"，点开是一行一个模式的候选面板（图标 + 名字 + 一句说明 + 勾选）。
  - 单选语义：默认「聊天」是面板里的一个正式选项（`value` 为空 = 不传 `agent_name`）。

## 轻量交互清单
- 顶部 Tab 点击切换并高亮。
- 左侧历史会话切换高亮。
- 新建会话并自动激活。
- 输入区回车/点击发送，立即追加用户消息，并模拟 AI 回复。
- 消息列表新增内容后自动滚动到底部。
