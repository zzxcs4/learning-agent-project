from contextlib import asynccontextmanager
from pathlib import Path
import threading

from fastapi import Depends, FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import register_exception_handlers
from app.core.logging_config import main_logger  # 导入日志配置
from app.core.settings import settings
from app.db.base import Base
from app.db.database import engine, get_db
# 导入 Base 和 engine
from app.routes import (
    auth_router,
    knowledge_base_router,
    learning_workflow_router,
    mcp_router,
    note_router,
    orchestrator_router,
    practice_router,
    profile_router,
    prompt_router,
    session_router,
    upload_router,
    user_router,
)
from app.services.prompt_template_service import prompt_template_manager
from app.utils.intent_classifier import SemanticIntentClassifier


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 为简化流程，使用 create_all() 自动创建表
    # 注意：生产环境推荐使用 Alembic 进行数据库版本管理
    # Base.metadata.create_all(engine)

    # 预热 Redis：把生效中的提示词模板加载到缓存（Redis不可用时函数内部会自动跳过）
    db = next(get_db())
    try:
        prompt_template_manager.load_all_effective_to_redis(db)
    finally:
        db.close()

    # 后台预热语义模型；避免模型下载或缓存异常阻塞应用启动。
    threading.Thread(
        target=SemanticIntentClassifier.get().warmup,
        name="semantic-intent-warmup",
        daemon=True,
    ).start()

    main_logger.info("应用启动完成，数据库表已确认就绪，请通过此地址访问: http://127.0.0.1:8000")
    yield


app = FastAPI(title="User CRUD API", version="1.0.0", lifespan=lifespan)
register_exception_handlers(app)

# 挂载静态文件目录。
# ⚠️ 必须在 mount 之前建目录：StaticFiles(directory=...) 在构造时就检查目录存在，
# 不存在会抛 RuntimeError；容器里 `uploads/` 是空卷/未打进镜像，会直接把启动打挂。
# 路径按项目根（app 的上一级）解析，这样不管 uvicorn 的 CWD 是哪里都能定位到。
_UPLOAD_DIR = Path(__file__).resolve().parents[1] / "uploads"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_UPLOAD_DIR)), name="uploads")

# 允许跨域访问：默认值只覆盖本机前端开发端口；生产用 `CORS_ORIGINS`
# （逗号分隔）显式声明，例如 CORS_ORIGINS=https://learn.example.com
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 将用户相关接口统一挂载到 /users 前缀，便于路由分组与后续扩展版本。
app.include_router(user_router, prefix="/users", tags=["users"])
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(upload_router, tags=["upload"])
app.include_router(session_router, prefix="/sessions", tags=["sessions"])
app.include_router(learning_workflow_router, prefix="/learning-workflows", tags=["learning_workflows"])
app.include_router(note_router, prefix="/notes", tags=["notes"])
app.include_router(orchestrator_router, prefix="/agent/tasks", tags=["agent_tasks"])
app.include_router(mcp_router, prefix="/agent/mcp", tags=["mcp"])
app.include_router(prompt_router, prefix="/prompt", tags=["prompt_management"])
app.include_router(practice_router, prefix="/practice", tags=["practice"])
app.include_router(profile_router, prefix="/profile", tags=["profile"])
app.include_router(knowledge_base_router, prefix="/knowledge-bases", tags=["knowledge_bases"])


@app.get("/")
def root() -> dict[str, str]:
    # 入口信息接口：仅确认服务可访问。
    main_logger.debug("根路径接口被访问。")
    return {"message": "FastAPI user service is running"}


@app.get("/health/live", summary="服务存活检查", tags=["stability"])
def health_live() -> dict[str, str]:
    # liveness 仅检查服务进程是否正常对外响应。
    return {"status": "healthy", "service": "running"}


@app.get("/health/ready", summary="服务就绪检查", tags=["stability"])
def health_ready(db: Session = Depends(get_db)) -> dict[str, object]:
    # readiness 检查核心依赖（数据库）是否可用。
    result: dict[str, object] = {
        "status": "healthy",
        "details": {
            "service": "running",
            "database": "connected",
        },
    }
    try:
        db.execute(text("SELECT 1"))
        return result
    except Exception:
        result["status"] = "unhealthy"
        result["details"] = {
            "service": "running",
            "database": "disconnected",
        }
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=result,
        )
