# FastAPI 
## 1) 创建并激活虚拟环境

```bash
python -m venv .venv
```

Windows (cmd):

```bash
.venv\Scripts\activate
```

Windows (PowerShell):

```bash
.venv\Scripts\Activate.ps1
```

## 2) 安装依赖

```bash
pip install -r requirements.txt
```

## 3) 配置环境变量（敏感信息分层）

项目支持按环境分层读取配置，避免将敏感数据写入代码：

1. 复制模板并按环境创建文件。
   - `.env.development`
   - `.env.testing`
   - `.env.production`
2. 通过 `APP_ENV` 选择当前环境（默认 `development`）。

示例（Windows cmd）：

```bash
set APP_ENV=development
```

示例（PowerShell）：

```bash
$env:APP_ENV="development"
```

腾讯云 COS 对象存储配置（可写入 `.env.development`）：

```bash
COS_SECRET_ID=AKIDxxxxxxxxxxxxxxxxxxxxxxxx
COS_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxx
COS_REGION=ap-beijing
COS_BUCKET=your-bucket-1250000000   # 必须带 APPID 后缀，且需预先在控制台创建
```


## 4) 启动服务（自动建表）

```bash
uvicorn app.main:app --reload
```

服务启动时会基于 SQLAlchemy 模型自动执行建表（`create_all`）。

## 5) Alembic 管理表结构变更

初始化/同步数据库：

```bash
alembic upgrade head
```

模型修改后一键同步：

```bash
# 1) 改模型前：确认本地数据
alembic revision --autogenerate -m "对本次变更的精确描述"
alembic upgrade head
```

## 分层结构

- `app/routes/`: API 路由层
- `app/schemas/`: 请求/响应校验层 (Pydantic v2)
- `app/services/`: 业务逻辑层
- `app/db/`: SQLAlchemy 数据层
- `alembic/`: 迁移脚本

### 项目文件结构总览

```text
agent-backend-project/
├── app/                              # 应用主目录
│   ├── agent/                        # 智能体层：模型工厂 / 记忆 / 提示词组装
│   │   ├── llm.py                    # ChatModel 工厂（DeepSeek 等 provider）
│   │   ├── memory.py                 # 历史记忆构建 + 外部检索补充
│   │   └── prompt_layer.py           # Prompt 拼装与个性化注入
│   ├── core/                         # 核心基础设施
│   │   ├── auth.py                   # 鉴权依赖（Bearer Token）
│   │   ├── exception_handlers.py     # 全局异常处理
│   │   ├── exceptions.py             # 业务异常定义
│   │   ├── logging_config.py         # 日志配置
│   │   ├── rate_limit.py             # 限流
│   │   ├── response_models.py        # 统一响应模型
│   │   └── settings.py               # 环境变量与配置加载
│   ├── db/                           # 数据层
│   │   ├── base.py                   # SQLAlchemy Base
│   │   ├── database.py               # 引擎/Session 工厂
│   │   └── models.py                 # ORM 模型（User/Session/ChatMessage 等）
│   ├── rag/                          # RAG 文件入库与检索
│   │   └── core.py                   # 解析→清洗→分块→向量化→写 Qdrant
│   ├── routes/                       # API 路由层
│   │   ├── auth_routes.py            # 登录/注册
│   │   ├── prompt_routes.py          # 提示词模板管理
│   │   ├── session_routes.py         # 会话
│   │   ├── upload_routes.py          # 文件上传
│   │   └── user_routes.py            # 用户
│   ├── schemas/                      # Pydantic v2 请求/响应模型
│   │   ├── auth_schemas.py
│   │   ├── chat_schemas.py
│   │   ├── prompt_schemas.py
│   │   ├── session_schemas.py
│   │   ├── upload_schemas.py
│   │   └── user_schemas.py
│   ├── services/                     # 业务逻辑层
│   │   ├── chat_service.py           # 普通聊天
│   │   ├── stream_chat_service.py    # 流式聊天（SSE）
│   │   ├── session_service.py        # 会话管理
│   │   ├── prompt_template_service.py# 提示词模板（带 Redis 缓存）
│   │   ├── rag_service.py            # RAG 摄入入口
│   │   ├── cos_storage_service.py    # COS 客户端封装
│   │   ├── upload_service_cos.py     # 上传业务（生产路径）
│   │   ├── resource_cleanup_service.py# 过期资源清理
│   │   ├── user_service.py           # 用户业务
│   │   └── user_profile_service.py   # 用户画像 CRUD
│   ├── utils/
│   │   └── prompt_logger.py          # 提示词专用日志
│   ├── main.py                       # FastAPI 应用入口
│   └── security.py                   # 密码哈希 / Token
├── scripts/                          # 运维/一次性脚本
│   ├── insert_sample_records.py      # 插入示例数据
│   └── run_expired_resource_cleanup.py# 过期资源定时清理
├── tests/
│   └── test_smoke.py                 # 冒烟测试
├── uploads/                          # 本地上传目录（兼容旧逻辑）
├── alembic.ini                       # Alembic 配置
├── env.example                       # 环境变量模板
├── requirements.txt                  # Python 依赖
├── prompt.md                         # 设计/提示词笔记
└── README.md                         # 项目说明
```

> 说明：`app/services/upload_service.py`（教学版旧逻辑）不在结构图中列出，实际运行以 `upload_service_cos.py` 为准。

### 项目文件结构总览

```text
agent-backend-project/
├── app/                              # 应用主目录
│   ├── agent/                        # 智能体层：模型工厂 / 记忆 / 提示词组装
│   │   ├── llm.py                    # ChatModel 工厂（DeepSeek 等 provider）
│   │   ├── memory.py                 # 历史记忆构建 + 外部检索补充
│   │   └── prompt_layer.py           # Prompt 拼装与个性化注入
│   ├── core/                         # 核心基础设施
│   │   ├── auth.py                   # 鉴权依赖（Bearer Token）
│   │   ├── exception_handlers.py     # 全局异常处理
│   │   ├── exceptions.py             # 业务异常定义
│   │   ├── logging_config.py         # 日志配置
│   │   ├── rate_limit.py             # 限流
│   │   ├── response_models.py        # 统一响应模型
│   │   └── settings.py               # 环境变量与配置加载
│   ├── db/                           # 数据层
│   │   ├── base.py                   # SQLAlchemy Base
│   │   ├── database.py               # 引擎/Session 工厂
│   │   └── models.py                 # ORM 模型（User/Session/ChatMessage 等）
│   ├── rag/                          # RAG 文件入库与检索
│   │   └── core.py                   # 解析→清洗→分块→向量化→写 Qdrant
│   ├── routes/                       # API 路由层
│   │   ├── auth_routes.py            # 登录/注册
│   │   ├── prompt_routes.py          # 提示词模板管理
│   │   ├── session_routes.py         # 会话
│   │   ├── upload_routes.py          # 文件上传
│   │   └── user_routes.py            # 用户
│   ├── schemas/                      # Pydantic v2 请求/响应模型
│   │   ├── auth_schemas.py
│   │   ├── chat_schemas.py
│   │   ├── prompt_schemas.py
│   │   ├── session_schemas.py
│   │   ├── upload_schemas.py
│   │   └── user_schemas.py
│   ├── services/                     # 业务逻辑层
│   │   ├── chat_service.py           # 普通聊天
│   │   ├── stream_chat_service.py    # 流式聊天（SSE）
│   │   ├── session_service.py        # 会话管理
│   │   ├── prompt_template_service.py# 提示词模板（带 Redis 缓存）
│   │   ├── rag_service.py            # RAG 摄入入口
│   │   ├── cos_storage_service.py    # COS 客户端封装
│   │   ├── upload_service_cos.py     # 上传业务（生产路径）
│   │   ├── resource_cleanup_service.py# 过期资源清理
│   │   ├── user_service.py           # 用户业务
│   │   └── user_profile_service.py   # 用户画像 CRUD
│   ├── utils/
│   │   └── prompt_logger.py          # 提示词专用日志
│   ├── main.py                       # FastAPI 应用入口
│   └── security.py                   # 密码哈希 / Token
├── scripts/                          # 运维/一次性脚本
│   ├── insert_sample_records.py      # 插入示例数据
│   └── run_expired_resource_cleanup.py# 过期资源定时清理
├── tests/
│   └── test_smoke.py                 # 冒烟测试
├── uploads/                          # 本地上传目录（兼容旧逻辑）
├── alembic.ini                       # Alembic 配置
├── env.example                       # 环境变量模板
├── requirements.txt                  # Python 依赖
├── prompt.md                         # 设计/提示词笔记
└── README.md                         # 项目说明
```

> 说明：`app/services/upload_service.py`（教学版旧逻辑）不在结构图中列出，实际运行以 `upload_service_cos.py` 为准。

## 文件资源上传管理

项目已支持统一资源管理（文件/图片/音频）：

- 接口：`POST /upload/file`（需 Bearer Token）
- 上传字段（`multipart/form-data`）：
  - `file`: 上传文件（必填）
  - `storage_scene`: 存储场景（可选，默认 `0`）
    - `0` 长过期（30天）
    - `1` 短过期（2小时）
    - `2` 仅提取内容，不落盘原文件
    - `3` 永久存储
  - `upload_purpose`: 上传用途（可选，默认 `0`）
    - `0` 普通资源（不更新头像）
    - `1` 用户头像（图片类型时更新 `users.avatar`）

上传行为：

- 按扩展名识别资源类型：文件（0）/图片（1）/音频（2）
- 使用 `MD5 + user_id` 做用户级去重
- 原文件上传到 COS（`storage_scene=2` 除外）
- 资源元数据写入 `resources` 表（`storage_path` 为 `cos://bucket/object_key`）
- 仅当 `upload_purpose=1` 且图片类型时，更新 `users.avatar`
- 教学版旧逻辑保留在 `app/services/upload_service.py`（不参与运行）
- 实际运行逻辑在 `app/services/upload_service_cos.py`

详细实现说明见：[docs/resource_upload_design.md](docs/resource_upload_design.md)
数据完整性验收清单见：[docs/resource_data_integrity_acceptance.md](docs/resource_data_integrity_acceptance.md)

## 过期资源定时清理（每天 03:00）

已提供定时清理脚本：`scripts/run_expired_resource_cleanup.py`

- 运行一次（用于手动验证）：

```bash
python scripts/run_expired_resource_cleanup.py --once
```

- 持续运行（每天凌晨 3 点执行）：

```bash
python scripts/run_expired_resource_cleanup.py
```

可选参数：

- `--hour`：执行小时（默认 `3`）
- `--minute`：执行分钟（默认 `0`）
- `--timezone`：时区（默认 `Asia/Shanghai`）
