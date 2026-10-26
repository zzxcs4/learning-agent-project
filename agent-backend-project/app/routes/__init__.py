from .auth_routes import router as auth_router
from .knowledge_base_routes import router as knowledge_base_router
from .learning_workflow_routes import router as learning_workflow_router
from .mcp_routes import router as mcp_router
from .note_routes import router as note_router
from .orchestrator_routes import router as orchestrator_router
from .practice_routes import router as practice_router
from .profile_routes import router as profile_router
from .session_routes import router as session_router
from .user_routes import router as user_router
from .upload_routes import router as upload_router
from .prompt_routes import router as prompt_router

__all__ = [
    "auth_router",
    "knowledge_base_router",
    "learning_workflow_router",
    "mcp_router",
    "note_router",
    "orchestrator_router",
    "practice_router",
    "profile_router",
    "session_router",
    "user_router",
    "upload_router",
    "prompt_router",
]
