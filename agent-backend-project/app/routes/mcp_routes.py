"""MCP 状态路由（第 3 期）。

**为什么只读、且只有一条**：MCP 接的是**外部**能力，"现在到底挂上了什么"必须能自查——
否则用户只能从模型行为里猜（工具没被调用，是模型不想用，还是根本没连上？）。
它只回接入状态，不回任何用户数据。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.agent.mcp import mcp_status_payload
from app.core import StandardResponse, common_error_responses
from app.core.auth import get_current_user
from app.db.models import User

router = APIRouter()
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get(
    "/servers",
    responses=common_error_responses,
    summary="查询已接入的 MCP server 与工具",
)
def list_mcp_servers(current_user: CurrentUser) -> Any:
    """返回 MCP 接入状态：是否开启、每个 server 连上没有、各暴露了哪些工具、失败原因。

    需要登录（不对外暴露部署细节），但不区分用户——状态本身与"谁的资料"无关。
    """
    return StandardResponse(code=0, message="MCP 状态查询成功", data=mcp_status_payload())
