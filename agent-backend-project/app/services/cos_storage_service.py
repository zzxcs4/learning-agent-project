"""腾讯云 COS 存储通用服务：统一客户端、路径解析、上传、预签名与删除。"""

from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosClientError, CosServiceError

from app.core.settings import settings


class CosStorageService:
    """统一封装 COS 常用操作，减少业务服务重复代码。"""

    @staticmethod
    def get_client() -> CosS3Client:
        """创建 COS 客户端（固定走 HTTPS）。"""
        config = CosConfig(
            Region=settings.cos_region,
            SecretId=settings.cos_secret_id,
            SecretKey=settings.cos_secret_key,
            Scheme="https",
        )
        return CosS3Client(config)

    @staticmethod
    def is_cos_storage_path(storage_path: str) -> bool:
        """判断路径是否为 cos://bucket/object_key 格式。"""
        return bool(storage_path and storage_path.startswith("cos://"))

    @staticmethod
    def parse_storage_path(storage_path: str) -> tuple[str, str]:
        """解析 cos://bucket/object_key 路径。"""
        if not CosStorageService.is_cos_storage_path(storage_path):
            raise ValueError(f"非法 COS 存储路径: {storage_path}")
        path_body = storage_path[len("cos://") :]
        parts = path_body.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"非法 COS 存储路径: {storage_path}")
        return parts[0], parts[1]

    @staticmethod
    def put_object(
        client: CosS3Client,
        bucket_name: str,
        object_name: str,
        data,
        length: int,
        content_type: str,
    ) -> None:
        """
        上传对象到 COS（简单上传，单对象上限 5GB）。

        注意：存储桶必须预先在 COS 控制台创建，代码不做自动建桶。

        `ContentLength` 必须显式转成 str：COS SDK 1.9.44 会把它**原样**当成
        `Content-Length` 请求头（`cos_comm.py` 里有 `ContentLength -> Content-Length`
        的映射，不做类型转换），而新版 requests 拒绝非 str/bytes 的 header 值，
        会抛 `InvalidHeader` → 被 SDK 包成 `CosClientError` → 上传接口只会报
        "对象存储服务不可用"。转成 str 后请求头与签名值都一致，行为不变。
        """
        client.put_object(
            Bucket=bucket_name,
            Key=object_name,
            Body=data,
            ContentLength=str(length),
            ContentType=content_type,
        )

    @staticmethod
    def get_presigned_url(
        storage_path: str,
        expires_seconds: int = 3600,
    ) -> str | None:
        """
        通过 cos:// 路径生成预签名访问 URL。
        非 COS 路径返回 None；生成失败同样返回 None。

        ⚠️ 别指望用它做"浏览器内预览"：**2024-01-01 之后创建的桶，用 COS 默认域名访问
        一律被强制下载**（COS 会加上 `x-cos-force-download: true` 与
        `Content-Disposition: attachment`），`response-content-disposition=inline`
        这个覆盖参数会被直接忽略——本机实测确认过。要预览只有两条路：
        配自定义域名，或**由后端代理转发**（本项目走后者，见 `resource_download` 路由）。
        """
        if not CosStorageService.is_cos_storage_path(storage_path):
            return None
        try:
            bucket_name, object_name = CosStorageService.parse_storage_path(storage_path)
            client = CosStorageService.get_client()
            url = client.get_presigned_url(
                Bucket=bucket_name,
                Key=object_name,
                Method="GET",
                Expired=expires_seconds,
            )
            return url
        except (CosServiceError, CosClientError):
            return None

    @staticmethod
    def delete_object_by_storage_path(
        client: CosS3Client, storage_path: str, ignore_not_found: bool = True
    ) -> bool:
        """
        通过 cos:// 路径删除对象。

        返回：
        - True: 删除成功或对象不存在(幂等成功)
        - False: 非 COS 路径（跳过）
        """
        if not CosStorageService.is_cos_storage_path(storage_path):
            return False

        bucket_name, object_name = CosStorageService.parse_storage_path(storage_path)
        try:
            client.delete_object(Bucket=bucket_name, Key=object_name)
            return True
        except CosServiceError as exc:
            # COS 删除不存在的对象会返回 404，按幂等成功处理。
            if ignore_not_found and str(exc.get_status_code()) == "404":
                return True
            raise
