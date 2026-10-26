import sys
from io import BytesIO
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from uuid import uuid4

# 将项目根目录加入导入路径，避免在不同虚拟环境下出现 `No module named 'app'`。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.main import app


@pytest.fixture
async def async_client() -> AsyncClient:
    # 使用 ASGITransport 进行进程内请求，不依赖真实端口和外部网络。
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test-server") as client:
        yield client


@pytest.mark.anyio
async def test_health_ready(async_client: AsyncClient) -> None:
    """验证服务就绪检查（服务 + 数据库）。"""
    response = await async_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.anyio
async def test_docs_accessible(async_client: AsyncClient) -> None:
    """验证接口文档可访问。"""
    response = await async_client.get("/docs")
    assert response.status_code == 200


@pytest.mark.anyio
async def test_users_list_api(async_client: AsyncClient) -> None:
    """验证用户列表接口可访问。"""
    response = await async_client.get("/users")
    assert response.status_code == 200


@pytest.mark.anyio
async def test_user_crud_apis(async_client: AsyncClient) -> None:
    """验证用户核心接口链路（创建/详情/更新/删除）。"""
    # 追加随机后缀，避免重复执行测试时触发用户名唯一约束冲突。
    suffix = uuid4().hex[:8]
    username = f"smoke_{suffix}"
    updated_username = f"smoke_u_{suffix}"

    create_response = await async_client.post(
        "/users",
        json={"username": username, "password": "smoke123456"},
    )
    assert create_response.status_code == 201
    created_user = create_response.json()["user"]
    user_id = created_user["id"]

    get_response = await async_client.get(f"/users/{user_id}")
    assert get_response.status_code == 200
    assert get_response.json()["username"] == username

    update_response = await async_client.put(
        f"/users/{user_id}",
        json={"username": updated_username, "password": "smoke654321"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["username"] == updated_username

    # delete_response = await async_client.delete(f"/users/{user_id}")
    # assert delete_response.status_code == 200


@pytest.mark.anyio
async def test_upload_file_api(async_client: AsyncClient) -> None:
    suffix = uuid4().hex[:8]
    username = f"upload_{suffix}"
    password = "upload123456"

    register_response = await async_client.post(
        "/auth/register",
        json={"username": username, "password": password},
    )
    assert register_response.status_code == 201

    login_response = await async_client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    access_token = login_response.json()["access_token"]

    files = {"file": ("sample.txt", BytesIO(b"hello upload"), "text/plain")}
    data = {"storage_scene": "1"}
    upload_response = await async_client.post(
        "/upload/file",
        data=data,
        files=files,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert upload_response.status_code == 201
    body = upload_response.json()
    assert body["resource_type"] == 0
    assert body["storage_scene"] == 1
