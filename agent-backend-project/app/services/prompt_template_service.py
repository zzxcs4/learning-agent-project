"""提示词模板版本管理服务：处理模板读取、版本控制、回滚和Redis缓存。"""

import json
from typing import Optional

import redis
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db.models import PromptTemplate
from app.utils.prompt_logger import log_redis_status


AGENT_CONFIG = {
    "flow_controller": {"scene": "workflow", "name": "流程总控Agent"},
    "quiz_generate_workflow": {"scene": "quiz", "name": "出题题库Agent"},
    "learning_evaluator": {"scene": "quiz_evaluation", "name": "学习测评评分Agent"},
    "difficulty_learner": {"scene": "study", "name": "难点学习Agent"},
    "note_archiver": {"scene": "note", "name": "笔记归档Agent"},
}


class PromptTemplateManager:
    """提示词模板管理器：负责模板持久化、版本控制、Redis缓存、公共模板拼接。"""

    def __init__(self):
        """初始化Redis客户端，如果连接失败则使用数据库直接查询。"""
        self.redis_client = None
        self.use_redis = True
        error_msg = None
        try:
            self.redis_client = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password,
                decode_responses=True,
                protocol=2,
            )
            self.redis_client.ping()
        except Exception as e:
            error_msg = str(e)
            self.redis_client = None
            self.use_redis = False
        
        log_redis_status(self.use_redis, error_msg)
        self.templates_key = "prompt:templates:active"

    def get_next_version(self, db: Session, agent_name: str, scene: str) -> int:
        """自动获取下一版本号（整数自增）。"""
        filter_conditions = [PromptTemplate.scene == scene]
        if agent_name:
            filter_conditions.append(PromptTemplate.agent_name == agent_name)
        else:
            filter_conditions.append(PromptTemplate.agent_name.is_(None))

        max_version = (
            db.query(PromptTemplate)
            .filter(*filter_conditions)
            .with_entities(db.func.max(PromptTemplate.version))
            .scalar()
        )
        return (max_version or 0) + 1

    def load_all_effective_to_redis(self, db: Session) -> None:
        """启动时，把所有生效模板加载到Redis。"""
        templates = (
            db.query(PromptTemplate)
            .filter(PromptTemplate.is_active == 1)
            .all()
        )

        if self.use_redis and self.redis_client:
            pipe = self.redis_client.pipeline()
            pipe.delete(self.templates_key)

            for template in templates:
                field = f"{template.agent_name}:{template.scene}" if template.agent_name else f"__common__:{template.scene}"
                value = json.dumps(
                    {
                        "id": template.id,
                        "agent_name": template.agent_name,
                        "scene": template.scene,
                        "template_type": template.template_type,
                        "name": template.name,
                        "description": template.description,
                        "template_content": template.template_content,
                        "variables": template.variables,
                        "version": template.version,
                    },
                    ensure_ascii=False,
                )
                pipe.hset(self.templates_key, field, value)

            pipe.execute()

    def get_template_by_agent_scene(
        self, agent_name: str, scene: str, db: Optional[Session] = None
    ) -> Optional[dict]:
        """从Redis或数据库读取指定智能体的模板。"""
        template = None
        
        if self.use_redis and self.redis_client:
            try:
                field = "{}:{}".format(agent_name, scene)
                value = self.redis_client.hget(self.templates_key, field)
                template = json.loads(value) if value else None
            except Exception:
                template = None
        
        if not template and db:
            try:
                row = db.query(PromptTemplate).filter(
                    PromptTemplate.agent_name == agent_name,
                    PromptTemplate.scene == scene,
                    PromptTemplate.is_active == 1,
                ).first()
                
                if row:
                    template = {
                        "id": row.id,
                        "agent_name": row.agent_name,
                        "scene": row.scene,
                        "template_type": row.template_type,
                        "name": row.name,
                        "description": row.description,
                        "template_content": row.template_content,
                        "variables": row.variables,
                        "version": row.version,
                    }
            except Exception:
                template = None
        
        return template

    def get_full_prompt(
        self, agent_name: str, scene: str, db: Optional[Session] = None
    ) -> Optional[str]:
        """拼接公共模板和私有模板，得到最终发给AI的完整提示词。"""
        private = self.get_template_by_agent_scene(agent_name, scene, db)
        if not private:
            return None

        private_content = private["template_content"]
        common_content = ""
        
        if self.use_redis and self.redis_client:
            try:
                common_key = "__common__:{}".format(scene)
                common_value = self.redis_client.hget(self.templates_key, common_key)
                if common_value:
                    common_data = json.loads(common_value)
                    common_content = common_data.get("template_content", "")
            except Exception:
                common_content = ""
        
        if not common_content and db:
            try:
                common_row = db.query(PromptTemplate).filter(
                    PromptTemplate.agent_name.is_(None),
                    PromptTemplate.scene == scene,
                    PromptTemplate.is_active == 1,
                ).first()
                
                if common_row:
                    common_content = common_row.template_content
            except Exception:
                pass

        if common_content:
            return common_content + "\n---\n" + private_content

        return private_content

    def rollback_prompt_version(self, db: Session, template_id: int) -> bool:
        """版本一键回滚。"""
        target = db.query(PromptTemplate).filter(
            PromptTemplate.id == template_id
        ).first()

        if not target:
            return False

        agent_display = target.agent_name if target.agent_name else "公共"
        print(f"\n[回滚前] {agent_display}:{target.scene} v{target.version} -> is_active: {target.is_active}")

        filter_conditions = [PromptTemplate.scene == target.scene]
        if target.agent_name:
            filter_conditions.append(PromptTemplate.agent_name == target.agent_name)
        else:
            filter_conditions.append(PromptTemplate.agent_name.is_(None))

        db.query(PromptTemplate).filter(*filter_conditions).update(
            {PromptTemplate.is_active: 0}
        )

        db.query(PromptTemplate).filter(
            PromptTemplate.id == template_id
        ).update({PromptTemplate.is_active: 1})

        db.commit()
        
        target_refreshed = db.query(PromptTemplate).filter(
            PromptTemplate.id == template_id
        ).first()
        
        print(f"[回滚后] {agent_display}:{target.scene} v{target_refreshed.version} -> is_active: {target_refreshed.is_active}")
        print(f"[✓] 版本回滚成功!\n")

        if self.use_redis and self.redis_client:
            self.load_all_effective_to_redis(db)

        return True


prompt_template_manager = PromptTemplateManager()
