"""全局配置：从 .env 读取，提供 .env.example 模板。

约定：所有密钥/路径/模型分层均来自环境变量，代码中不出现硬编码密钥。
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# M8：env_file 用绝对路径（相对项目根 backend/.env），从任意目录启动不再静默回落默认值
_BASE_DIR = Path(__file__).resolve().parents[2]  # config.py 位于 backend/app/core/ → backend/

# M4：API 前缀默认值（api_prefix 与 skill_api_base 共用，保证两处默认值同步；均仍可被 env 覆盖）
_DEFAULT_API_PREFIX = "/api/v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # 安全
    secret_key: str = "dev-insecure-secret-change-me"
    # R1（红队三修复）：每日自动轮换的双密钥窗口——prev=上一枚密钥（保留 8 天，
    # token 7 天有效期内不掉线）；由 scripts/rotate_secret.py 维护 .env 两行
    secret_key_prev: str = ""
    jwt_expire_days: int = 7
    captcha_ttl_seconds: int = 300
    captcha_fail_threshold: int = 3          # 同 IP 失败 3 次后要求验证码
    ip_blacklist_threshold: int = 10         # 10 次失败封 24h
    # L11：登录 token 走 httpOnly cookie（JS 不可读防 XSS 窃取）；生产 HTTPS 时置 True
    cookie_secure: bool = False
    # E-01(API)：反代场景取真实 IP 的可信代理白名单（逗号分隔；空=不信任 XFF，防伪造）。
    # 未配置反代时保持取直连 IP，失败计数键为 ip+username 双因子（见 security.py）
    trusted_proxies: str = ""
    # R2（红队三修复）：伪造 token ver 试探失败封禁（IP 维度 20 次/15 分钟封 1h）。
    # 正常用户成功访问即清计数——共享出口 IP 场景不会误封；测试/压测可经环境变量关闭
    ver_block_enabled: bool = True
    # SEC-26：LLM 输出侧恶意载荷屏蔽（命令注入/SQL RCE/eval 形态替换为占位符；True=开）
    output_guard_malicious: bool = True
    # SEC-05：通用限流（次/分钟；压测/fuzz 期经环境变量调高）
    rate_limit_ask_per_min: int = 30       # /chat/ask 按用户（防 LLM 成本滥用）
    rate_limit_captcha_per_min: int = 30   # /auth/captcha 按 IP
    rate_limit_depts_per_min: int = 60     # /auth/departments 按 IP（F22：防未登录批量枚举；登录页只请求一次）
    rate_limit_login_per_min: int = 60     # /auth/login 按 IP（防爆破计数仍为主防线）

    # 数据库（PostgreSQL）
    global_db_url: str = "postgresql+asyncpg://aip:aip_dev_pass@localhost:5432/ai_platform_tardis"
    dept_db_url_template: str = "postgresql+asyncpg://aip:aip_dev_pass@localhost:5432/tardis_dept_{dept_id}_db"
    ceo_db_url: str = "postgresql+asyncpg://aip:aip_dev_pass@localhost:5432/tardis_ceo_db"
    # 库名前缀：代码里拼 CREATE/DROP/备份库名时统一加前缀（同一台 PostgreSQL 上跑多实例时避免撞库）。
    # 注意：连接 URL 走上三个模板（.env 需带同一前缀），本项只管代码拼名处。
    db_name_prefix: str = "tardis_"
    # 2026-09-17：personal_db_url_template 随下线删除（存量 personal_*_db 库原地保留，无人读）

    # Redis
    redis_url: str = "redis://localhost:6379/1"
    redis_password: str = ""               # SEC-18：Redis 密码（redis.conf requirepass 场景；空=无密码）

    # DeepSeek（文本模型；思考模式参数见 docs/官方接口规范/deepseek接口规范/思考模式.md）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # 2026-09-10（官方文档更新）：模型名统一为 deepseek-flash；deepseek-v4-pro 将于 09-14 下线
    # （其后请求路由到 V4.1-Flash），候选目录已下架 pro，此处默认值同步
    deepseek_model_ceo: str = "deepseek-flash"
    deepseek_model_employee: str = "deepseek-flash"
    # 2026-09-01：辅助链路 fallback 模型名（llm_client 平台兜底，env 可覆盖——原硬编码 agnes-2.5-flash/glm-4-flash-250414）
    agnes_fallback_model: str = "agnes-2.5-flash"
    glm_fallback_model: str = "glm-4.7-flash"
    # 2026-09-08：免费档定义（前端「免费档」按钮的开发机开关值 = 后端唯一维护点；
    # /models/options 与 /admin/model-catalog 均下发 free 字段，前端不硬编码模型名）
    free_llm_platform: str = "agnes"
    free_llm_model: str = "agnes-3.0-flash"
    # 2026-09-01：验证码字体路径进 config（原 security.py 硬编码 DejaVu 绝对路径，精简系统缺字体时登录验证码失败）
    captcha_font_path: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    reasoning_effort_ceo: str = "max"
    reasoning_effort_employee: str = "high"

    # 智谱 GLM（视觉/图片生成；并发配额低：视觉 5、图片 1）
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_vision_model: str = "glm-4.1v-thinking-flash"   # 支持视频（模型选择.md：视频+图片，并发 5）
    glm_image_model: str = "cogview-3-flash"
    # 2026-08-21（glm-vision-mcp 机制实测）：视频 base64 data URI 直传（免公网隧道）——
    # auto=≤glm_video_base64_max_mb 走 base64、大视频回退 URL 隧道；url=强制隧道；base64=强制直传
    glm_video_upload_mode: str = "auto"
    # 2026-08-21 实测极限（glm-4.1v-thinking-flash，4.5Mbps 测试视频）：
    #   ≤20MB/40s 全成功；25MB/45s 起失败（GLM 400 code=1261 Prompt 超长，token 按时长/帧数计）
    #   ~42s 边界抖动（同 24MB 一次成一次败）——单段 base64 只适合短视频
    glm_video_base64_max_mb: int = 20
    # 2026-08-21 长视频切分（用户决策：完全弃用临时隧道）：超长视频 ffmpeg 每段
    # glm_video_split_segment_s 秒逐段 base64 调用，按段拼接；总时长超
    # split_max_segments×segment_s（默认 20 分钟）报错提示截取
    glm_video_split_segment_s: int = 30
    # 2026-09-15（走查）：GLM 免费档视频理解并发（原硬编码 5；用户决策"免费档并发很低"→ 2，可调 1 串行）
    glm_video_concurrency: int = 2
    glm_video_split_max_segments: int = 40

    # 公网入口（2026-08-14 cloudflared 隧道）：配置后媒体理解走原生 video_url（GLM 直拉临时 URL），
    # 未配置时视频分析返回明确错误（用户决策：不做抽帧回退）。例：https://your-domain.example.com
    public_base_url: str = ""

    # 目录（WSL ext4）
    upload_dir: str = "/data/uploads"
    output_dir: str = "/data/outputs"
    backup_dir: str = "/data/backups"
    log_dir: str = "/data/logs"
    temp_dir: str = "/data/tmp"
    max_upload_size_mb: int = 100
    # ↑ 2026-09-15（用户要求放宽）：原 20MB 太小——知识库/简历/团队数据上传共用此上限；
    # 注意这些入口是"整读进内存 + 解析入库"路径（非问答的流式+分片）——再往上调需先评估解析/入库能力

    # 2026-09-15（媒体工具）：音视频单文件上限（2026-09-15 用户定 600MB；>50MB 前端自动走分片上传绕开
    # 单请求大 body 的三层门槛——portproxy >650MB 空体/nginx body 限制/浏览器不可续传）。
    # 注意：GLM 视频理解原生输入硬顶 200M，超限的视频只能转写（工具内会返回明确提示）
    media_upload_max_mb: int = 600
    # 2026-09-15（用户要求放宽）：问答会话上传的**文档类**上限（原共用 max_upload_size_mb=20 太小）；
    # 问答路径全部走流式落盘（不整读进内存），>50MB 前端自动切片上传
    session_doc_upload_max_mb: int = 200
    # zip 自动解压的体积上限（超过只存原件不自动解压——解压需整包读入内存，防大 zip 打爆）
    session_zip_auto_unzip_max_mb: int = 100
    file_chunk_size_mb: int = 5

    # 团队技能文件（2026-08-20 zip 技能）：{skill_files_dir}/{dept_id}/{skill_id}/md/ 与 /scripts/
    # （注意与 skill_cache_dir 区分：本项是团队技能上传落盘，skill_cache 是内置技能 npm 持久缓存）
    skill_files_dir: str = "/data/skills"
    skill_zip_max_bytes: int = 5 * 1024 * 1024        # zip 上传大小上限
    skill_zip_max_total_bytes: int = 20 * 1024 * 1024  # 解压总大小上限
    skill_zip_max_files: int = 200                    # 解压文件数上限

    # 会话 zip（2026-08-21）：上传 zip 自动解压到上传目录子文件夹的限值（防 zip bomb）
    session_zip_max_total_bytes: int = 100 * 1024 * 1024  # 解压总大小上限
    session_zip_max_files: int = 500                    # 解压文件数上限
    session_zip_max_depth: int = 10                     # 成员路径最大目录深度
    session_zip_pack_max_bytes: int = 500 * 1024 * 1024  # zip_pack 打包产物大小上限

    # 大文件分片上传（2026-09-10 通用化——单请求大 body 有三层门槛：本机 nginx 600m /
    # Windows portproxy >650MB 缺陷 / 后端业务上限；单片 <64MB 走旧链路无压力，服务端合并校验后
    # 交业务管线，见 services/upload_chunks.py）
    # 2026-09-10：file_parse 解析结果缓存（键含 mtime+size，天然失效；同一文件重复解析是最大纯浪费）
    file_parse_cache_ttl_s: int = 900
    # 2026-09-17（缓存审计 P0-5）：图片识别结果缓存 TTL——图片内容不变则识别结果稳定，
    # 同图重复识别（换 recognition_type/重试/多轮追问）原先每次都真调视觉模型（付费/限流）
    image_recognition_cache_ttl_s: int = 7 * 86400
    upload_chunk_dir: str = "/data/upload_chunks"                 # 分片临时目录（mkdir 创建；每日 03:45 清理 TTL）
    upload_chunk_max_mb: int = 64                                 # 单片大小上限（前端约定 50MB 切分）
    upload_chunk_ttl_hours: int = 24                              # 未完成分片目录清理阈值（超时丢弃重传）

    # API 前缀（M4：生产环境随机化，经 API_PREFIX 注入；消费方统一走 core/url_utils.py）
    api_prefix: str = _DEFAULT_API_PREFIX

    # 告警 webhook（空则跳过告警）；机器人开启签名时配置 ALERT_SIGN_SECRET
    alert_webhook_url: str = ""
    alert_sign_secret: str = ""
    # 2026-08-21：环境标识（告警标题前缀，如 [开发机]）——默认空=无前缀；
    # 开发机启动注入 ENV_NAME（不改 .env，见 deploy/start.sh）
    env_name: str = ""

    # CORS 允许来源（M7：逗号分隔，进配置而非代码硬编码；生产域名变化无需改代码）
    cors_origins: str = ("http://localhost:5173,http://127.0.0.1:5173,"
                         "http://localhost:5174,http://127.0.0.1:5174")

    # 2026-09-17：dashboard_fallback_dept 随 /dashboard/data-freshness 接口下线删除

    # 知识库检索（KB-REDESIGN：两级检索——DB 关键词预筛 + LLM 精排）
    kb_fts_engine: str = "ILIKE"             # ILIKE 兜底；"zhparser" 需安装扩展（默认不引入）
    kb_candidate_blocks: int = 20            # 阶段一候选块上限
    # 知识库表格文件会话复制（2026-08-24 归属三态）：新会话最多复制几个 xlsx 到会话可读目录
    # （0=关闭复制；截断时 skill_router 注入说明提示不全）
    kb_xlsx_copy_max: int = 10

    # Agent 引擎（2026-08-07 重设计：主要限制=连续 4 轮无有效输出即终止；2026-08-10：max_tool_rounds
    # 接入 graph 作硬兜底，取 max(配置, 20)——沙盒调试类任务每轮有输出不受限；
    # v2 框架：授权卡/checkpoint 卡已移除，无 20 轮确认间隔约束；.env MAX_TOOL_ROUNDS 可再上调）
    max_tool_rounds: int = 40
    # 2026-08-18：图表生成同轮硬上限（prompt 交付克制为软约束，LLM 可能一轮画几十张；
    # 默认 6——多数需求 1-2 张，但用户可能一次要多种图，留足余量）
    chart_per_round_limit: int = 6
    # 模拟 LLM 模式（2026-08-17）：LLM_MOCK=1 时主 LLM 返回确定性模拟响应（首轮固定工具调用
    # file_search → 收到工具结果后返回固定回答）+ 模拟延迟——高并发压测（mock_load_test 50 并发）
    # 压测平台完整管线（队列/图路由/DB/SSE/落库）而零 LLM 费用/零 API 配额占用
    llm_mock: bool = False
    llm_mock_delay_s: float = 1.0        # 模拟单轮 LLM 耗时（贴近真实首 token 延迟量级）
    plan_expire_seconds: int = 180             # D20 文字计划过期时长（原复用 confirm_timeout_seconds；v2 计划批准卡用 plan_approve_timeout_seconds）
    # v2 交互框架（2026-08-14）
    question_timeout_seconds: int = 120        # 反问浮窗超时（超时按推荐项自动提交，agent 不悬挂）
    plan_approve_timeout_seconds: int = 900    # 计划批准卡超时（超时收尾+plan_json 持久化可重开）
    subagent_registry_ttl: int = 7200          # 后台子代理注册表 TTL（秒；超时惰性清理，resume 失效）
    quick_default_thinking: bool = False       # D11：快速任务默认关思考（复杂任务用平台默认=开）
    llm_call_timeout_seconds: int = 150      # 单轮 LLM 调用总时长上限（思考模式长任务防挂起，httpx 空闲超时不生效）
    agent_total_timeout_s: float = 1800      # 2026-09-01：单次 ask 的 Agent 图总超时（DB/Redis/事件循环挂起时兜底收尾，防"永久思考中"）
    tool_result_max_chars: int = 200000      # 单工具结果回填 LLM 上限（2026-08-07 适配 DeepSeek 1M 上下文：30000→200000，agent 经 max_chars 参数自选读取量；曾 8000 截断 90 行表格 43% 数据）
    history_max_chars: int = 150000          # 历史注入字符预算（2026-08-10 D11：超预算裁剪更早轮次，至少保留最新一轮；不注入"已裁剪"标记防破坏缓存前缀）
    history_layers_enabled: bool = True      # 支柱 2（2026-08-10）：历史分层压缩（原文层 + 早期轮次 llm_aux 语义摘要）开关
    history_raw_ratio: float = 0.7           # 支柱 2：原文层占 history_max_chars 比例（剩余给摘要层）
    history_summary_max_chars: int = 1500    # 支柱 2：摘要层注入上限（早期轮次语义摘要）
    history_summary_input_max_chars: int = 10000  # 支柱 2：摘要 LLM 输入上限（被丢轮次轻量表征）
    history_summary_timeout: int = 60        # 支柱 2：摘要调用超时（失败跳过降级为现状整轮丢弃）
    history_summary_cache_ttl: int = 1814400 # 支柱 2：摘要 Redis 缓存 TTL（秒，=21 天与会话保留一致）
    # 2026-09-15（上下文进度条 + 主动/被动压缩）：
    context_budget_tokens: int = 700000      # 进度条分母（2026-09-15 用户定：700K；DeepSeek 1M 为硬顶，
                                             # 预算留 300K 余量给本轮工具结果增长与估算误差）
    context_compact_pct: int = 50            # 被动触发：水位 ≥ 该百分比（700K × 50% = 35 万 tokens）→ 下一次提问前自动压缩
    context_compact_idle_hours: int = 2      # 被动触发：会话空闲 ≥ 该小时数 → 每小时扫描补压
    context_compact_idle_min_tokens: int = 20000  # 空闲触发内容下限（水位低于此不白跑 LLM）
    context_compact_keep_rounds: int = 3     # 压缩后保留原文的最近轮数
    context_compact_input_max_chars: int = 30000  # 压缩输入预算（上次摘要 + 新增轮次轻量表征）
    context_compact_max_tokens: int = 2000   # 结构化摘要输出上限
    context_compact_lock_ttl: int = 180      # 压缩互斥锁 TTL（秒）——压缩最长约 70s（LLM 60s 超时），
                                             # 留 2.5× 余量；TTL 也是"压缩中途崩溃"后该会话被锁的上限
    delivery_stall_note_rounds: int = 5      # M1（2026-08-10；2026-08-14 放宽 4→5）：连续 N 轮无交付进展 → 注入收敛注记（不打断）
    delivery_stall_final_rounds: int = 8     # M1（v2）：连续 N 轮无交付进展 → 卡点收尾（停止盲试向用户汇报，原弹确认卡行为下线）
    repeat_stall_note_rounds: int = 2        # 2026-08-20（P3b）：连续 N 轮"同参重复且零交付" → 注入重复收敛注记（软性，默认值真实档观察后可调）
    repeat_stall_final_rounds: int = 3       # 2026-08-20（P3b）：连续 N 轮 → 卡点收尾（停止盲试向用户汇报）
    strip_reasoning_history: bool = True     # 支柱 5（2026-08-10 实测验证）：回传 LLM 前丢弃历史轮次 reasoning_content（DeepSeek API 实测不强制，仅保留最近 N 轮）
    reasoning_keep_last: int = 1             # 支柱 5：保留最近 N 条 assistant 消息的 reasoning_content（上下文连续性）
    # 2026-09-17：sql_query_timeout_seconds/sql_query_max_rows 随数据查询线下线删除

    # 联网搜索
    websearch_deepseek_path: str = "npx"     # 可改为本地安装路径

    # 数据同步 → 2026-09-17 数据查询线下线：sync_retry_hour/backup_hour/data_stale_days/ceo_sync_* 已删除
    session_cleanup_hour: str = "03:30"
    session_ttl_days: int = 21               # 业务记录保留 21 天（会话/消息/上传/产出物；audit_log 不清理）

    # 工具集（媒体工具 / 简历初筛数据目录与限制）
    tools_data_dir: str = "/data/tools"
    # 工具下载（2026-09-04）：运维上传的 zip 存储目录（文件名列表由 DB tool_downloads 管理）
    tool_downloads_dir: str = "/data/tools_downloads"
    tool_download_max_size_mb: int = 500       # 单包上传上限
    tools_data_ttl_days: int = 7             # 简历初筛数据保留天数（每天 03:45 清理）
    av_cache_ttl_days: int = 30              # 媒体工具缓存保留天数（转写稿 JSON / 段级视觉文本）
    video_max_size_mb: int = 500
    # 2026-08-25（媒体理解 agent 上下文护栏）：
    video_transcript_inject_chars: int = 60000   # 转写稿注入 LLM 上下文预算（落盘文件不受限）
    video_glm_result_chars: int = 30000          # 单条 GLM 理解输出回传 tool 消息预算
    video_max_frames: int = 12               # 单视频最多抽帧数（视觉 API 逐帧）
    # 2026-09-15（智能助手媒体工具 video_understand/audio_transcribe）：单文件转写时长上限（分钟）
    media_max_duration_min: int = 120
    # 2026-08-24（用户决策）：语音转文字默认本地 FunASR（一次模型调用出完整口播转写，
    # 替代 GLM 视觉逐帧看字幕）；api_vision 保留可回切（配置值 local_funasr / api_vision）
    transcribe_backend: str = "local_funasr"
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    resume_max_count: int = 20
    resume_top_k_default: int = 10
    resume_batch_size: int = 5               # 每批送 LLM 评分的简历数
    # 2026-08-25（会议纪要工具）：录音时长上限 + 总结注入预算（转写全文落盘不受限）
    meeting_max_duration_min: int = 120
    meeting_summary_inject_chars: int = 50000
    meeting_audio_ttl_days: int = 1           # 会议录音缓存保留天数（1 天后删音频，保留文本与 DB）
    meeting_text_ttl_days: int = 30           # 会议文本产物保留天数（超期整体删除 DB + 目录）
    # 2026-08-25：HTTPS 入口端口（浏览器录音需 secure context）——部署机配置（backend/.env HTTPS_PORT=24443），
    # 前端 /tools/access 下发：http 协议下点「会议纪要」卡片自动跳转 https 端口；None=不跳转（开发机）
    https_port: int | None = None
    # 2026-08-25：启动后台预热 Cam++ 说话人模型（懒加载首次 ~20s → 预热后常驻秒级）；
    # 部署机 .env WARMUP_CAMPPUS=true（开发机不开，省内存与启动时间）
    warmup_campplus: bool = False

    # LibreOffice 文档预览 / PDF 导出（二期）
    libreoffice_path: str = "/usr/bin/soffice"
    libreoffice_port: int = 6002             # 预留常驻服务模式；当前实现 subprocess 直调

    # 记忆库（二期）
    memory_scan_minute: int = 10             # 每小时第 10 分钟扫描候选记忆
    memory_extract_enabled: bool = True      # 四期：记忆自动提取开关（会话空闲后提炼个人记忆）
    memory_extract_idle_hours: int = 2       # 四期：会话空闲 N 小时后触发提取（用户设定默认 2h）
    memory_extract_min_new_messages: int = 20  # D12（2026-08-10）：上次提取后新增消息 >= N 条可再提取（一次性抑制修复）

    # Celery 渐进式（三期 M19）：True=后台任务走 Celery worker+beat（API 进程不再启动 APScheduler）
    celery_enabled: bool = False

    # 沙盒工具（三期 M17：脚本执行隔离环境；2026-08-10：+FSIZE 单文件写入上限 D5）
    sandbox_enabled: bool = True
    # SEC-02：沙盒网络开关（默认断网 --unshare-net；可被 system_config sandbox_net_enabled 覆盖，
    # admin 配置页在线改，60s 生效；True 时恢复联网——仅调试/白名单场景开启）
    sandbox_net_enabled: bool = False
    sandbox_timeout_seconds: int = 30        # 脚本执行总时长上限（超时 killpg）
    sandbox_memory_mb: int = 768             # 地址空间上限（RLIMIT_AS；D9：512→768 给数据处理类任务留裕量）
    sandbox_node_memory_mb: int = 2048       # SEC-11：node 沙盒地址空间上限（原 unlimited；V8 CodeRange 预留后给足余量同时封顶）
    sandbox_fsize_mb: int = 512              # 单文件写入上限（RLIMIT_FSIZE，超限 SIGXFSZ 杀进程）
    sandbox_dir: str = "/data/sandbox"
    sandbox_langs: str = "python"
    sandbox_ttl_days: int = 1                # 沙箱工作目录保留天数（每日 03:45 清理）
    sandbox_stdout_cap_chars: int = 8000     # T2（2026-08-10）：run_script stdout 回填预截断（首 800 + 尾 4000，防 21 轮累计挤爆历史预算）
    sandbox_workfile_preview_chars: int = 150  # T3：work 小文件（≤2KB）内容预览字符数（≤8 个文件带预览）
    sandbox_write_max_chars: int = 100000   # 2026-08-10：write 模式内容上限（原 20000 硬编码——DeepSeek 输出上限 384K token，
                                            # 中小型 HTML（≤10 万字符 ≈ 3-5 万 token）可一次直接写入，无需脚本绕行；
                                            # 建议单次 ≤5 万字符受 150s 调用超时约束，更大分块或脚本生成）
    sandbox_deliver_max_bytes: int = 20 * 1024 * 1024  # T4：mode=deliver 单文件大小上限（20MB）
    # 支柱 3（2026-08-10）：总量护栏
    dynamic_context_max_chars: int = 30000   # dynamic_context 组装预算（超限按优先级整块丢弃：记忆→工具摘要→文件清单）
    ask_tool_chars_max: int = 600000         # 单 ask 工具结果回填累积护栏（超限后续结果降级 meta+尾 2K，保持合法 JSON）
    plan_checklist_enabled: bool = True      # 任务清单注入开关（plan_status=confirmed 时）
    # Agnes AI 平台（2026-08-10 接入：LLM/图片/视频生成；key 在 .env AGNES_API_KEY）
    agnes_base_url: str = "https://apihub.agnes-ai.com/v1"   # OpenAI 兼容端点（chat/images）
    agnes_api_key: str = ""                                  # 从 .env AGNES_API_KEY 读
    agnes_video_base: str = "https://apihub.agnes-ai.com"    # 视频端点在根域：POST /v1/videos + GET /agnesapi?video_id=
    agnes_video_max_wait_s: int = 600        # 视频生成单次轮询等待上限（短视频 1-3 分钟，队列满需重试）
    agnes_video_poll_interval: int = 10      # 轮询间隔
    agnes_video_daily_seconds: int = 500     # 每日视频秒数配额（TokenPlan；免费档更严，本地记账防超限）
    agnes_image_max_bytes: int = 5 * 1024 * 1024  # 图生视频 base64 图片上限（5MB）
    # 2026-09-17（用户决策）：数据库查询/数据导入线整线下线——sql_query_enabled 开关随工具删除
    # 支柱 1（2026-08-10）：subagent 子代理
    subagent_enabled: bool = True            # 关闭即不注册/不注入（一键回滚）
    subagent_max_rounds: int = 8             # 子代理内部轮次预算
    subagent_no_progress_limit: int = 4      # 子代理连续无有效输出轮数 → 强制出报告
    subagent_result_chars_max: int = 400000  # 子代理内部消息累积护栏（超出结果降级 meta+stdout 尾 2000）
    subagent_use_aux: bool = False           # True 时子代理走 llm_aux 档（task key "subagent"）；False 用主 LLM 同档

    # F1（2026-08-19 统一）：视频/视觉 API 超时预算——防单次调用无限挂起
    # （GLM 视频直传分析合理 30-120s；帧级 transcriber 已 30s；图片识别 <30s 一般）
    video_api_timeout_s: int = 120        # GLM 视频单次调用超时（原 600s 挂起风险）
    video_api_retry_budget_s: int = 480   # 视频重试总预算（4 次 × 120s）
    vision_api_timeout_s: int = 60        # 图片识别单次超时（原 120s 偏大）
    # 4.1 skill 执行环境（专用 conda env skillenv；node 用绝对路径，避开 WSL 下 npx 指向 Windows 版的问题）
    aip_python: str = "/opt/conda/envs/aip/bin/python3.12"   # AIP 运行环境解释器（沙盒文件级挂载/隧道子进程用）
    skillenv_python: str = "/opt/conda/envs/skillenv/bin/python"
    node_path: str = "/opt/node/bin/node"
    # F-01 收窄（2026-08-19）：沙盒只读挂载逃生门——收窄后盘点遗漏的依赖路径可在此追加
    # （文件/目录均可，逐个 --ro-bind；SANDBOX_EXTRA_RO 环境变量 JSON 列表可覆盖）
    sandbox_extra_ro: list = []
    officecli_bin_dir: str = "/opt/bin"      # officecli 二进制目录（沙盒 PATH 追加）
    cloudflared_bin: str = "/opt/bin/cloudflared"   # 临时隧道二进制（G2：env 可覆盖）
    skill_cache_dir: str = "/data/skill_cache"           # skill 持久缓存（npm 依赖等，不被沙盒 TTL 清理）
    skill_timeout_seconds: int = 180         # skill 脚本执行总时长上限（比 run_script 宽松）
    skill_memory_mb: int = 1024              # skill 内存上限（pandas/matplotlib 在 512MB 会 OOM）
    skill_token_ttl_seconds: int = 600       # skill 调平台工具的内部令牌有效期
    # F 扩展（红队二次）：tmp_media 临时 URL TTL（原 1h 偏长——token 明文在 URL 且公网隧道可达；
    # 链路为"签发→随 video_url 给 GLM→秒级拉取"，15min 窗口充裕）
    tmp_media_ttl_seconds: int = 900
    # 临时隧道兜底（2026-08-18 用户方案）：无命名隧道（PUBLIC_BASE_URL 未配置）时，
    # 任务需要公网拉视频 → 懒启动"隧道专用迷你服务 + cloudflared quick tunnel"（只暴露
    # tmp-media 单端点，不挂平台）；空闲 tmp_tunnel_idle_seconds 无拉取自动释放。命名隧道
    # 优先，此为兜底。延迟设计：懒启动+复用（只付一次 3-5s 启动）、就绪等待（签发前等 URL）、
    # 预热（首个子任务触发与 GLM 请求并行）。env TMP_TUNNEL_ENABLED 可关。
    tmp_tunnel_enabled: bool = True
    tmp_tunnel_port: int = 8123           # 隧道专用迷你服务端口（仅 127.0.0.1 监听，cloudflared 指向它）
    tmp_tunnel_idle_seconds: int = 1800   # 空闲回收：无 tmp_media 拉取超过该时长 → 释放隧道与迷你服务
    tmp_tunnel_start_timeout: int = 15    # 隧道就绪等待上限（cloudflared 启动+域名注册通常 2-5s）
    skill_api_base: str = f"http://127.0.0.1:8001{_DEFAULT_API_PREFIX}"   # skill 脚本回调平台内部接口的基地址（前缀与 api_prefix 同源，可经 SKILL_API_BASE 整体覆盖）
    # F2（红队二次，2026-08-18）：skill 执行 bwrap 隔离开关（代码默认值，可 env 覆盖）——
    # True=skill 进程关进 bwrap（宿主 FS 只读白名单 + 仅回环网络）；False=一键回退红队前行为（argv 白名单收益保留）
    skill_bwrap_enabled: bool = True
    # 会话任务后台化（2026-08-18，第 15 项）：任务与 SSE 请求解耦——断连任务继续在后台执行，
    # 重连从事件流（Redis sse_events:{session}）断点续播；False=回退断连即取消旧行为（legacy_stream_ask）
    task_bg_enabled: bool = True
    sse_events_ttl_seconds: int = 1800    # 事件流保留时长（任务完成后 30 分钟内可回放收尾）


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # G1（架构 P2，2026-08-19）：dev 凭据默认值保留（env 可覆盖）——加载后显式警告
    # （不 fail：本地开发/测试形态合法；红线：生产必须注入真实密钥）
    if s.secret_key == "dev-insecure-secret-change-me" or "aip_dev_pass" in s.global_db_url:
        print("[config] 警告：正在使用开发默认密钥/数据库凭据（secret_key 或 aip_dev_pass）——"
              "生产部署必须通过环境变量注入真实值")
    return s
