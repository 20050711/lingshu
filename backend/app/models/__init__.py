"""全局库 SQLAlchemy 模型（ai_platform_tardis）。

设计对齐 docs/mvp/mvp技术设计文档-v1.1.md 数据表；表结构动态管理，
此处仅定义核心表；团队业务表由 sync_schema.py 动态创建。
"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    # S5：账号团队内唯一（四期设计）；DB 约束已由 migrate_refactor.py 迁移为复合唯一，
    # 此处 ORM 声明对齐（原全局 unique=True 若重建库会退回旧行为——声明漂移修复）
    __table_args__ = (UniqueConstraint("department_id", "username", name="uq_users_dept_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    department_id: Mapped[str] = mapped_column(String(50), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # L21：登出/吊销 +1，旧 token 全失效
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class Department(Base):
    """团队注册表（三期 M12：单一事实源，替代 DEPT_NAMES 硬编码；新团队=新库+新账号）。"""
    __tablename__ = "departments"

    dept_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("idx_sessions_client", "client_id", "last_activity_at"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(100))
    department_id: Mapped[str] = mapped_column(String(50), nullable=False)
    is_readonly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    # 需求 3（2026-08-17）：token 计费累计（deepseek 平台 usage；迁移 scripts/migrate_cost.py）
    cost_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # 2026-08-17（价格换算）：细分缓存命中/未命中输入 + 输出 + 最近模型（flash/pro 价格不同）
    cost_prompt_hit: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_prompt_miss: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_completion: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_model: Mapped[str | None] = mapped_column(String(64))
    memory_extracted_at: Mapped[datetime | None] = mapped_column(DateTime)  # 四期：记忆自动提取标记
    # D20/D27（2026-08-10）：计划确认状态机 + 原子轮号分配（migrate_plan.py 迁移）
    plan_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'none'"))
    plan_text: Mapped[str | None] = mapped_column(Text)
    plan_round_id: Mapped[int | None] = mapped_column(Integer)
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # v2 交互框架（2026-08-14，migrate_agent_v2.py 迁移）：
    # mode=quick/complex 双模式；plan_json=批准卡计划（JSONB 直接传 dict）；plan_revision_count=修订循环计数
    mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'quick'"))
    plan_json: Mapped[dict | None] = mapped_column(JSONB)
    plan_revision_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_round: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # 任务后台化（2026-08-18，migrate_task_status.py 迁移）：会话任务执行状态
    # none=无任务 / running=执行中 / completed=已完成 / error=异常 / interrupted=服务重启标记中断
    task_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'none'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("idx_messages_session_round", "session_id", "round_id"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)   # user / assistant / system
    content: Mapped[str | None] = mapped_column(Text)
    round_id: Mapped[int | None] = mapped_column(Integer)
    outputs: Mapped[dict | None] = mapped_column(JSONB)            # [{type,label,chart_id,file_path,...}]
    tool_events: Mapped[dict | None] = mapped_column(JSONB)        # 工具调用记录 [{tool_name,status,brief,detail}]
    files: Mapped[dict | None] = mapped_column(JSONB)              # 本条消息关联的上传文件 [{file_name,...}]
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class ChartOutput(Base):
    __tablename__ = "chart_outputs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    round_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chart_label: Mapped[str | None] = mapped_column(String(100))
    chart_type: Mapped[str | None] = mapped_column(String(20))
    option_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    png_path: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class ChatFile(Base):
    __tablename__ = "chat_files"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int | None] = mapped_column(Integer)
    file_type: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class SkillConfig(Base):
    """（四期重构后为死表：仅历史数据，无消费代码；团队技能改存 skill_files）"""
    __tablename__ = "skill_configs"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(10))
    description: Mapped[str | None] = mapped_column(Text)
    trigger_conditions: Mapped[str | None] = mapped_column(Text)
    tool_mapping: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class SkillFile(Base):
    """团队技能（四期重构：SKILL.md 文件，frontmatter + Markdown 正文）。

    tools/trigger 列存 JSON 数组字符串（text() 原生 SQL 写入须 json.dumps，踩坑 12/28）。
    """
    __tablename__ = "skill_files"
    __table_args__ = (Index("idx_skillfile_dept", "dept_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dept_id: Mapped[str] = mapped_column(String(50), nullable=False)
    skill_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)   # SKILL.md 正文（指令文本）
    tools: Mapped[str | None] = mapped_column(Text)              # JSON 数组字符串（可空=纯指令型）
    # 2026-08-20：技能文件相对路径 "{dept_id}/{skill_id}"（NULL=存量纯文本技能，无磁盘文件；经迁移脚本加列）
    skill_dir: Mapped[str | None] = mapped_column(String(255))
    # server_default 必须（2026-08-20 部署机事故：create_skill 走原生 SQL INSERT 不走 ORM default，
    # 新装环境表无 DB 默认值 → NotNullViolation 500；开发机历史表带默认侥幸正常）
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", server_default=text("'active'"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_by: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class UserSkillPref(Base):
    """用户技能勾选（三期 M15：按用户持久化，QA 功能栏与 /skills 页同源）。

    skill_id='__auto__' 行表示自动选择开关（enabled 为其值）。
    """
    __tablename__ = "user_skill_prefs"
    __table_args__ = (Index("idx_uskill_user", "user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


# 2026-09-17：SyncJob / SyncLog 两个模型随数据查询线下线删除（表 sync_jobs / sync_log 原地保留）


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str | None] = mapped_column(String(50))
    client_id: Mapped[str | None] = mapped_column(String(64))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    matched_rule: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str | None] = mapped_column(String(10))          # blocked / warned
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class Feedback(Base):
    """用户反馈（四期重构：细分类型 + 来源页面 + 截图，告警推送）。"""
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    department_id: Mapped[str | None] = mapped_column(String(50))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    contact: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    user_id: Mapped[int | None] = mapped_column(Integer)              # 三期 M18：提交人（细到个人）
    reply: Mapped[str | None] = mapped_column(Text)                  # 三期 M18：处理回复
    operator: Mapped[str | None] = mapped_column(String(50))         # 三期 M18：处理人
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)  # 三期 M18：处理时间
    feedback_type: Mapped[str | None] = mapped_column(String(80))    # 四期：细分类型（如"功能异常-工具执行失败"）
    page: Mapped[str | None] = mapped_column(String(100))            # 四期：来源页面路径
    screenshots: Mapped[list | None] = mapped_column(JSONB)          # 四期：截图相对路径列表（/uploads/feedback/...）
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class SystemConfig(Base):
    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operator: Mapped[str | None] = mapped_column(String(50))
    action: Mapped[str | None] = mapped_column(String(100))
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class KbCategory(Base):
    __tablename__ = "kb_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("kb_categories.id"))
    dept_id: Mapped[str | None] = mapped_column(String(50))  # 三期 M14：NULL=全局分类，非空=团队分类
    # 归属三态（2026-08-24，migrate_kb_user_scope.py）：user_id 非空=个人分类（dept_id 恒 NULL）
    user_id: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class KbDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("kb_categories.id"))
    file_type: Mapped[str | None] = mapped_column(String(20))
    file_size: Mapped[int | None] = mapped_column(Integer)
    department_id: Mapped[str | None] = mapped_column(String(50))
    # KB-REDESIGN（2026-08-07）：物理文件路径入库（删文档级联删盘 M15）+ 上传人 + 状态（下架预留）
    file_path: Mapped[str | None] = mapped_column(String(500))
    uploaded_by: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'active'"))
    # 归属三态（2026-08-24，migrate_kb_user_scope.py）：user_id 非空=个人文档（department_id 恒 NULL）
    user_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class KbChunk(Base):
    """知识文档分块（KB-REDESIGN：检索核心粒度；embedding 列为向量升级预留，未引 pgvector）。"""

    __tablename__ = "kb_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    block_title: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    para_loc: Mapped[str | None] = mapped_column(String(50))
    embedding: Mapped[list | None] = mapped_column(ARRAY(REAL))  # 向量升级预留
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class McpTool(Base):
    """AI 外部工具（MCP server）注册表（2026-09-03 起从规划展示升级为真实接入）。

    id: 平台侧工具标识（agent 注册与权限层使用）
    url: MCP server 地址（Streamable HTTP，如 http://127.0.0.1:5556/mcp/）；空=未接入（规划中）
    status: planning（规划展示）/ active（运维启用）/ disabled（运维停用）
    """

    __tablename__ = "mcp_tools"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 2026-09-18：图标由 emoji 改为**语义名**（取值见 app/agent/tools/__init__.py 的 ICON_KEYS），
    # 列宽 10→32（keys 最长 9；留余量防日后加长名，PG 加宽是元数据操作、不改数据）
    icon: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planning")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DepartmentMemory(Base):
    """团队共识记忆（二期 M9，PRD 第七章）。

    status: candidate（候选）→ active（达成共识激活）→ replaced（被新记忆替换）
    聚合键 (dept_id, content_hash)：同内容跨 client 累积 round_count 与 client_ids。
    """
    __tablename__ = "department_memory"
    __table_args__ = (Index("idx_mem_dept_status_hash", "dept_id", "status", "content_hash"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dept_id: Mapped[str] = mapped_column(String(50), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, server_default=text("'general'"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)   # sha256 聚合键
    client_ids: Mapped[dict | None] = mapped_column(JSONB)                 # 提出过该记忆的 client_id 列表
    round_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'candidate'"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class UserMemory(Base):
    """个人记忆库（四期重构：全员个人记忆，显式保存立即生效，无共识机制）。

    原 ceo_memory 泛化（表 user_memory）；mem_type: profile（个人画像）/
    knowledge（知识型：术语/缩写）/ workflow（任务流程模板）。
    """
    __tablename__ = "user_memory"
    __table_args__ = (Index("idx_user_mem_user_type", "user_id", "mem_type"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    mem_type: Mapped[str] = mapped_column(String(20), nullable=False, default="knowledge")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class MemoryThresholds(Base):
    """记忆库共识阈值配置（单行，id=1）。"""
    __tablename__ = "memory_thresholds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    breadth_min_clients: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    strength_min_rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    strength_min_clients: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("7"))
    candidate_ttl_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    max_active_per_dept: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class ResumeBatch(Base):
    """简历初筛批次（二期工具集页）。

    status: pending / extracting / scoring / done / failed
    weights: 7 维度权重 JSONB（前端已归一化）
    """
    __tablename__ = "resume_batches"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    jd: Mapped[str | None] = mapped_column(Text)                     # JD 描述
    weights: Mapped[dict | None] = mapped_column(JSONB)              # {professional:..., ...}
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class ResumeItem(Base):
    """批次内单份简历。

    status: uploaded / extracted / scored / failed
    dim_scores: 7 维度得分 JSONB；total_score = Σ(w·s)/Σw；rank 按总分排序
    """
    __tablename__ = "resume_items"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    batch_id: Mapped[str] = mapped_column(ForeignKey("resume_batches.id", ondelete="CASCADE"), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha1: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploaded")
    text_content: Mapped[str | None] = mapped_column(Text)   # 列名 text_content（避免遮蔽 sqlalchemy.text 函数）
    dim_scores: Mapped[dict | None] = mapped_column(JSONB)
    total_score: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)                # LLM 评语
    error_msg: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class MeetingRecording(Base):
    """会议纪要工具录音记录（2026-08-25 定制化工具）。

    status: uploaded / transcribing / ready（转写完成，等总结）/ summarizing / done / failed
    目录：{tools_data_dir}/meeting/{id}/（录音.{ext} / 语音转写.md / 总结.md）
    """
    __tablename__ = "meeting_recordings"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    server_path: Mapped[str | None] = mapped_column(String(500))           # 原始录音落盘路径（本地轨=麦克风）
    sys_path: Mapped[str | None] = mapped_column(String(500))              # 2026-08-25 双轨：线上轨（系统声音）落盘路径；空=单轨
    merged_path: Mapped[str | None] = mapped_column(String(500))            # 2026-08-25 双轨合成音频（zip 完整音频）
    duration_s: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploaded")
    transcript: Mapped[str | None] = mapped_column(Text)                   # 转写全文（带 [mm:ss]）
    transcript_path: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)                      # LLM 总结
    summary_path: Mapped[str | None] = mapped_column(String(500))
    scene: Mapped[str | None] = mapped_column(String(50))                  # 预设场景 key（MEETING_SCENES）
    custom_prompt: Mapped[str | None] = mapped_column(Text)                # 自定义提示词（非空优先于 scene）
    error_msg: Mapped[str | None] = mapped_column(Text)
    dept_id: Mapped[str | None] = mapped_column(String(50))                # llm_aux 分层（总结模型按团队解析）
    user_role: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))


class ToolDownload(Base):
    """工具下载包（2026-09-04）：运维上传离线工具包 zip + 简介说明。

    文件本体存 {tool_downloads_dir}/{stored_path}（UUID 前缀防重名/注入），
    原始名经 filename 展示；员工端仅列 enabled 且大小写敏感行文件名白名单下载。
    """

    __tablename__ = "tool_downloads"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    title: Mapped[str] = mapped_column(String(100), nullable=False)       # 展示名（如 数据导出工具）
    description: Mapped[str | None] = mapped_column(Text)                        # 简介说明
    filename: Mapped[str] = mapped_column(String(200), nullable=False)     # 原始文件名（展示/下载名）
    stored_path: Mapped[str] = mapped_column(String(260), nullable=False)  # 存储相对文件名（uuid8_原始名）
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=text("NOW()"))
