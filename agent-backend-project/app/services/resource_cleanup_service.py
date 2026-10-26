"""过期资源清理服务：扫描过期记录并清理 COS 对象与元数据。"""

from datetime import datetime

from qcloud_cos.cos_exception import CosServiceError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_config import main_logger
from app.db.models import Resource
from app.services.cos_storage_service import CosStorageService


class ResourceCleanupService:
    """封装过期资源清理流程，供脚本或任务调度调用。"""

    @staticmethod
    def cleanup_expired_resources(db: Session, batch_size: int = 500) -> dict[str, int]:
        """
        清理过期资源：
        1) 查询 expire_time <= now 的资源；
        2) 先删除对象存储文件（若有）；
        3) 再删除数据库元数据。
        """
        now = datetime.utcnow()
        deleted_count = 0
        failed_count = 0

        while True:
            stmt = (
                select(Resource)
                .where(Resource.expire_time.is_not(None), Resource.expire_time <= now)
                .order_by(Resource.id.asc())
                .limit(batch_size)
            )
            expired_resources = db.scalars(stmt).all()
            if not expired_resources:
                break

            for resource in expired_resources:
                try:
                    ResourceCleanupService._delete_original_file(resource.storage_path)
                    db.delete(resource)
                    db.commit()
                    deleted_count += 1
                except Exception as exc:
                    db.rollback()
                    failed_count += 1
                    main_logger.error(
                        "清理过期资源失败: resource_id={}, storage_path={}, error={}",
                        resource.id,
                        resource.storage_path,
                        str(exc),
                    )

        return {"deleted": deleted_count, "failed": failed_count}

    @staticmethod
    def _delete_original_file(storage_path: str) -> None:
        """删除原文件：仅对 cos:// 路径执行对象删除。"""
        if not CosStorageService.is_cos_storage_path(storage_path):
            return

        client = CosStorageService.get_client()
        try:
            CosStorageService.delete_object_by_storage_path(
                client=client, storage_path=storage_path, ignore_not_found=True
            )
        except CosServiceError as exc:
            # 对象已不存在时视为幂等成功，避免影响元数据清理。
            if str(exc.get_status_code()) == "404":
                return
            raise
