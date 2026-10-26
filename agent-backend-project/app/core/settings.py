"""应用配置：从分层 .env 与操作系统环境变量加载，供全局单例 `settings` 使用。"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


def _normalize_app_env(raw_env: str | None) -> str:
    """将原始 APP_ENV 归一为 `development` 或 `production`（prod/production 视为 production）。"""
    env = (raw_env or "").strip().lower()
    if env in {"prod", "production"}:
        return "production"
    return "development"


def _normalize_agent_entry_mode(raw_mode: str | None) -> str:
    """归一 `AGENT_ENTRY_MODE`；非法值退回默认 `loop`。

    为什么默认是 `loop`：系统该像 DSH 一样只有一种"会自己动手"的入口，
    用户不必理解"普通聊天 / 智能体"两种模式的区别。`gated` 保留下来是为了：
    ① 需要"简单问答完全不受 agent 侧改动影响"时可以切回去；
    ② 便于实测两种模式的首字延迟差。

    字面量刻意**内联**而不 import `orchestrator_service` 里的常量：
    `app.core.settings` 位于导入链的最底层（core → logging_config → settings），
    反向 import 会形成循环。取值口径由 `verify_agent_entry_mode.py` 守着。
    """
    mode = (raw_mode or "").strip().lower()
    if mode in {"loop", "gated"}:
        return mode
    return "loop"


def _as_bool(raw: Any, default: bool = False) -> bool:
    """把环境变量里的布尔写法人性化解析（`1/true/yes/on` 为真，其余按默认）。

    为什么不用 pydantic 的 bool 校验：`.env` 里写 `MCP_ENABLED=` 是常见写法，
    那会被解析成空串。空串应当视为"没配 = 默认"，而不是解析失败或当成 False。
    """
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _as_float(raw: Any, default: float) -> float:
    """浮点配置解析：非法或非正数一律回退默认值（超时配成 0 会让每次调用立刻失败）。"""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# CORS 默认白名单：只覆盖本地前端开发端口，保持"默认最小暴露"。
# 生产必须由部署方通过 `CORS_ORIGINS` 显式声明真实域名——原实现把局域网 IP
# 硬编码在这里，换台机器/换端口就要改代码重新打包，属于典型的部署耦合。
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)


def _as_origins(raw: Any) -> tuple[str, ...]:
    """把 `CORS_ORIGINS` 解析成元组：逗号分隔，去空白、去空项、去掉尾部斜杠。

    为什么去掉尾部斜杠：浏览器发的 `Origin` 头**从不带**尾部斜杠，
    而人写配置时几乎一定会带上（`https://a.com/`），留着就永远匹配不上，
    表现为"配置写了却还是 CORS 报错"。
    """
    if raw is None:
        return DEFAULT_CORS_ORIGINS
    text = str(raw).strip()
    if not text:
        return DEFAULT_CORS_ORIGINS
    origins = [item.strip().rstrip("/") for item in text.split(",")]
    return tuple(item for item in origins if item) or DEFAULT_CORS_ORIGINS


def _as_text(raw: Any, default: str = "") -> str:
    """读取字符串配置：去空白，并剥掉最外层成对的引号。

    为什么必须剥引号：`.env` 里写 `MYSQL_URL="mysql+pymysql://..."` 是常见写法，
    而**引号是否被剥掉取决于变量从哪来**——
      * 走 `dotenv_values`（本地 `.env.development`）时，dotenv 会自己剥引号；
      * 走 `os.environ`（Docker 的 `env_file` / `docker run --env-file`）时，
        **没有任何一层会剥引号**，引号原样进到值里。
    而容器里 `os.environ` 优先级更高，于是同一份配置在本地正常、在容器里直接崩：
        sqlalchemy.exc.ArgumentError:
        Could not parse SQLAlchemy URL from string '"mysql+pymysql://..."'
    （实测踩到）这里统一剥掉，让两种来源行为一致。
    """
    if raw is None:
        return default
    text = str(raw).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text or default


def _project_root() -> Path:
    """返回后端项目根目录（`app` 的上一级，用于定位 `.env.*` 文件）。"""
    return Path(__file__).resolve().parents[2]


def _load_layered_env() -> tuple[str, dict[str, Any]]:
    """
    读取 `项目根/.env.{app_env}` 并与 `os.environ` 合并（后者覆盖文件中的同名键）。

    返回: (标准化后的 app_env, 合并后的键值对)
    """
    app_env = _normalize_app_env(os.getenv("APP_ENV", "development"))
    root = _project_root()
    env_values = dotenv_values(root / f".env.{app_env}")
    merged = {**env_values, **os.environ}
    return app_env, merged


@dataclass(frozen=True)
class Settings:
    """
    不可变应用配置。通过 `from_env` 从环境构建；字段与 `.env` / 启动环境变量名对应见各字段注释。

    必填项在 `from_env` 内显式校验：MYSQL_URL、JWT_SECRET、COS 四元组等。
    """

    app_env: str  # 运行环境：development / production
    mysql_url: str  # SQLAlchemy 风格数据库连接串（如 mysql+pymysql://...）
    cors_origins: tuple[str, ...]  # 允许跨域的前端源列表（`CORS_ORIGINS`，逗号分隔）
    jwt_secret: str  # JWT 签名密钥
    jwt_algorithm: str  # JWT 算法，如 HS256
    jwt_access_expire_minutes: int  # Access Token 有效时长（分钟）
    jwt_refresh_expire_minutes: int  # Refresh Token 有效时长（分钟）
    cos_secret_id: str  # 腾讯云 COS SecretId
    cos_secret_key: str  # 腾讯云 COS SecretKey
    cos_region: str  # 腾讯云 COS 地域，如 ap-beijing
    cos_bucket: str  # 腾讯云 COS 存储桶名，格式 BucketName-APPID
    deepseek_api_key: str  # 兼容 OpenAI 接口的 API Key（如 DeepSeek），流式聊天必填
    deepseek_base_url: str  # 兼容 OpenAI 的 API Base URL
    deepseek_model: str  # 模型名，如 deepseek-chat
    deepseek_temperature: float  # 采样温度
    deepseek_max_tokens: int  # 单次补全最大 token 数
    dashscope_api_key: str  # DashScope API Key（Embedding 用，含密钥必须走环境变量）
    seedream_api_key: str  # 豆包 Seedream 文生图 Key（难点讲解知识卡出图；空=只出文字讲解）
    seedream_base_url: str  # Seedream OpenAI 兼容 Base URL（火山方舟）
    seedream_model: str  # Seedream 模型名，必须是方舟的 API id（如 doubao-seedream-5-0-flash-260915）
    seedream_size: str  # 出图尺寸档位（Seedream 用 "2k" 这类档位，不是 1024x1024）
    seedream_timeout_seconds: float  # 出图调用超时（秒）
    seedream_download_timeout_seconds: float  # 下载生成图超时（秒）
    volc_access_key: str  # 火山引擎 Access Key（OCR 调用，扫描版 PDF 兜底）
    volc_secret_key: str  # 火山引擎 Secret Key
    volc_region: str  # 火山引擎服务地域（如 cn-north-1）
    qdrant_host: str  # Qdrant 服务地址（不同环境会变）
    qdrant_port: int  # Qdrant 服务端口（不同环境会变）
    redis_host: str  # Redis 服务器地址
    redis_port: int  # Redis 服务器端口
    redis_db: int  # Redis 数据库编号
    redis_password: str  # Redis 密码
    working_memory_ttl_seconds: int  # 工作记忆 Redis TTL 秒数，默认 3 天
    instant_memory_max_chars: int  # 短期记忆注入 Prompt 前的字符预算上限
    agent_entry_mode: str  # 默认入口：loop=一律进 agent loop（默认）；gated=门控分流
    log_dir: str  # 日志目录；空=项目根/logs（保持原行为）。容器/只读盘下可指到可写位置
    mcp_enabled: bool  # 是否把外部 MCP server 的工具接进 agent loop（默认关，见升级规划 §八 第 3 期）
    mcp_servers_json: str  # `MCP_SERVERS`：JSON 数组，逐个描述要连接的 MCP server（stdio / streamable-http）
    mcp_connect_timeout_seconds: float  # 连接 server + 列工具的单次超时（秒）
    mcp_call_timeout_seconds: float  # 单次 MCP 工具调用的超时（秒），超时收敛成观察结果而不是打断 loop

    @staticmethod
    def from_env() -> "Settings":
        """
        从分层 env 与 `os.environ` 解析出 `Settings` 实例；缺必填项时抛出 `RuntimeError`。
        大模型相关项允许为空 Key（调用模型时再失败），与 COS/JWT/DB 策略不同处见代码分支。
        """
        app_env, values = _load_layered_env()

        # 统一的字符串读取口径：去空白 + 剥成对引号（见 _as_text 的说明）。
        # 为什么不直接改 `str(values.get(...)).strip()`：那样子写法有二十多处，
        # 漏一处就留一个"引号进到值里"的坑，正是这次的故障形态。
        def text_of(key: str, default: str = "") -> str:
            return _as_text(values.get(key), default)

        mysql_url = text_of("MYSQL_URL")
        if not mysql_url:
            raise RuntimeError("Environment variable MYSQL_URL is required")

        jwt_secret = text_of("JWT_SECRET")
        if not jwt_secret:
            raise RuntimeError("Environment variable JWT_SECRET is required")

        cos_secret_id = text_of("COS_SECRET_ID")
        cos_secret_key = text_of("COS_SECRET_KEY")
        cos_region = text_of("COS_REGION")
        cos_bucket = text_of("COS_BUCKET")
        if not cos_secret_id or not cos_secret_key or not cos_region or not cos_bucket:
            raise RuntimeError("COS config is incomplete")

        deepseek_api_key = text_of("DEEPSEEK_API_KEY")
        deepseek_base_url = text_of("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        deepseek_model = text_of("DEEPSEEK_MODEL", "deepseek-chat")
        deepseek_temperature = float(values.get("DEEPSEEK_TEMPERATURE", 0.7))
        deepseek_max_tokens = int(values.get("DEEPSEEK_MAX_TOKENS", 1024))

        dashscope_api_key = text_of("DASHSCOPE_API_KEY")

        # Seedream 文生图（难点讲解知识卡）：缺省**不报错**，与 DashScope / 火山 OCR 同一策略——
        # 没配 Key 就优雅降级成"只出文字讲解、不出图"，其余链路照常可演示。
        seedream_api_key = text_of("SEEDREAM_API_KEY")
        seedream_base_url = text_of(
            "SEEDREAM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
        )
        # 默认值必须是方舟的 **API id**（`doubao-seedream-5-0-flash-260915`），不能用文档里的
        # 展示名 `doubao-seedream-5.0-260128`：后者带点，调过去是 404
        # InvalidEndpointOrModel.NotFound。实测只有这个 flash 版本对本项目账号直接可调，
        # 5-0-pro / 4-0 需先在方舟控制台开通（否则 404 ModelNotOpen）。
        seedream_model = text_of("SEEDREAM_MODEL", "doubao-seedream-5-0-flash-260915")
        seedream_size = text_of("SEEDREAM_SIZE", "2k")

        # 火山 OCR 凭证：缺省时不强制报错，调到 OCR 时再失败（与 DashScope 策略保持一致）
        volc_access_key = text_of("VOLC_ACCESS_KEY")
        volc_secret_key = text_of("VOLC_SECRET_KEY")
        volc_region = text_of("VOLC_REGION", "cn-north-1")

        qdrant_host = text_of("QDRANT_HOST", "localhost")
        qdrant_port = int(values.get("QDRANT_PORT", 6333))

        return Settings(
            app_env=app_env,
            mysql_url=mysql_url,
            cors_origins=_as_origins(values.get("CORS_ORIGINS")),
            jwt_secret=jwt_secret,
            jwt_algorithm=text_of("JWT_ALGORITHM", "HS256"),
            jwt_access_expire_minutes=int(values.get("JWT_ACCESS_EXPIRE_MINUTES", 30)),
            jwt_refresh_expire_minutes=int(values.get("JWT_REFRESH_EXPIRE_MINUTES", 10080)),
            cos_secret_id=cos_secret_id,
            cos_secret_key=cos_secret_key,
            cos_region=cos_region,
            cos_bucket=cos_bucket,
            deepseek_api_key=deepseek_api_key,
            deepseek_base_url=deepseek_base_url,
            deepseek_model=deepseek_model,
            deepseek_temperature=deepseek_temperature,
            deepseek_max_tokens=deepseek_max_tokens,
            dashscope_api_key=dashscope_api_key,
            seedream_api_key=seedream_api_key,
            seedream_base_url=seedream_base_url,
            seedream_model=seedream_model,
            seedream_size=seedream_size,
            seedream_timeout_seconds=_as_float(
                values.get("SEEDREAM_TIMEOUT_SECONDS"), 120.0
            ),
            seedream_download_timeout_seconds=_as_float(
                values.get("SEEDREAM_DOWNLOAD_TIMEOUT_SECONDS"), 60.0
            ),
            volc_access_key=volc_access_key,
            volc_secret_key=volc_secret_key,
            volc_region=volc_region,
            qdrant_host=qdrant_host,
            qdrant_port=qdrant_port,
            redis_host=text_of("REDIS_HOST", "127.0.0.1"),
            redis_port=int(values.get("REDIS_PORT", 6379)),
            redis_db=int(values.get("REDIS_DB", 0)),
            redis_password=text_of("REDIS_PASSWORD"),
            working_memory_ttl_seconds=int(values.get("WORKING_MEMORY_TTL_SECONDS", 259200)),
            instant_memory_max_chars=int(values.get("INSTANT_MEMORY_MAX_CHARS", 12000)),
            agent_entry_mode=_normalize_agent_entry_mode(values.get("AGENT_ENTRY_MODE")),
            log_dir=text_of("LOG_DIR"),
            # MCP 默认**关闭**：外部 server 会把"读用户资料"的能力暴露给第三方进程，
            # 也可能把第三方工具交给模型执行，必须由部署方显式开启（升级规划 §八 第 3 期）。
            mcp_enabled=_as_bool(values.get("MCP_ENABLED"), default=False),
            # MCP_SERVERS 是 JSON 数组，**不能**剥引号（数组本身以 [ 开头、以 ] 结尾，
            # 不会被 _as_text 误判；但整串被 `"` 包起来时 dotenv/env_file 行为不同，
            # 所以这里只去空白，保持原样交给下游 json.loads）。
            mcp_servers_json=str(values.get("MCP_SERVERS") or "").strip(),
            mcp_connect_timeout_seconds=_as_float(
                values.get("MCP_CONNECT_TIMEOUT_SECONDS"), 20.0
            ),
            mcp_call_timeout_seconds=_as_float(values.get("MCP_CALL_TIMEOUT_SECONDS"), 60.0),
        )


# 进程级单例：import 时即按当前 APP_ENV 加载
settings = Settings.from_env()
