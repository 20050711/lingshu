"""模型候选目录（B4/B4.2：配置页四栏——LLM 对话 / LLM 辅助任务 / 视觉识别 / 多模态生成）。

唯一维护点：未来新增厂商/模型只改此文件，前端候选列表经 /admin/model-catalog 接口下发（不硬编码）。
"""

MODEL_CATALOG = {
    # 2026-08-17（用户决策）：生产/正式形态主对话 LLM 仅 deepseek——Agnes 文本档（agnes-2.5-flash）
    # 仅用于测试免费化（测试期经 switch_test_model.sh 直接写 system_config，绕过候选限制），
    # 配置页不再可选防误配；Agnes 平台仅保留多模态能力（视觉/图片/视频见下方 vision/image/video）
    "llm": [
        {"platform": "deepseek", "models": ["deepseek-flash"]},
    ],
    # LLM 辅助任务（kb 检索精排/摘要、记忆提取、简历评分等简单操作）：
    # 候选先复用 LLM 列表，后续后端接入免费/低价模型时在此追加
    "llm_aux": [
        {"platform": "deepseek", "models": ["deepseek-flash"]},
        # 2026-09-01：免费辅助候选三档——glm-4.7-flash（默认，思考强制开）/ glm-4-flash-250414（旧档，
        # 无思考，限流更稳）/ agnes-2.5-flash（agnes 平台，2.0 已废弃）；2026-09-08 新增 agnes-3.0-flash（$0，支持文本+图像 URL，工具调用已验证）；均严格 1 并发 + 限流重试 8 次
        {"platform": "glm", "models": ["glm-4.7-flash", "glm-4-flash-250414"]},
        {"platform": "agnes", "models": ["agnes-3.0-flash", "agnes-2.5-flash"]},
    ],
    "vision": [
        {"platform": "glm", "models": ["glm-4.1v-thinking-flash"]},
        # 2026-08-18（C1）：agnès 视觉（图片 URL 输入）——图片识别链路
        # 2026-09-01 勘误：agnes 平台模型仍是 agnes-* 系列（/models 实测）；此前"deepseek-v4-flash-vision-exp"
        # 成功是 media_tools 旧 bug（改 platform 属性不改 client）打到 DeepSeek 官方的假象——已修 bug 还原；
        # agnes-2.0-flash 已废弃（平台通知），仅保留 2.5
        {"platform": "agnes", "models": ["agnes-3.0-flash", "agnes-2.5-flash"]},
        # 2026-09-09：DeepSeek 官方图片理解（OpenAI 兼容 image_url / file 内容块；base64 data URL 或公网 URL，
        # 仅 user 消息可带图；消费侧 media_tools 走 platform_override="deepseek"——文档
        # docs/官方接口规范/deepseek接口规范/图片理解.md）
        # 2026-09-10（官方文档更新）：deepseek-flash 支持图像理解；旧名 deepseek-v4-flash-vision-exp
        # 对应模型已下线（请求由 V4.1-Flash 承接）→ 统一用 deepseek-flash
        {"platform": "deepseek", "models": ["deepseek-flash"]},
    ],
    "image": [
        {"platform": "glm", "models": ["cogview-3-flash"]},
        # 2026-08-10（Agnes 接入）：档位式 size（1K-4K）+ ratio（media_tools 自动转换参数格式）
        {"platform": "agnes", "models": ["agnes-image-2.1-flash", "agnes-image-2.0-flash"]},
    ],
    # 2026-08-10：视频生成栏（Agnes 异步任务 API：POST /v1/videos → 轮询 /agnesapi?video_id=）
    "video": [
        {"platform": "agnes", "models": ["agnes-video-v2.0"]},
    ],
    # 2026-09-02：定制化工具独立模型档（llm_tools）——简历/会议纪要/视频理解等工具任务独立配置
    # 工具任务的模型配置（候选与 llm_aux 一致：工具任务多为免费档辅助调用）
    "llm_tools": [
        {"platform": "deepseek", "models": ["deepseek-flash"]},
        {"platform": "glm", "models": ["glm-4.7-flash", "glm-4-flash-250414"]},
        {"platform": "agnes", "models": ["agnes-3.0-flash", "agnes-2.5-flash"]},
    ],
}

# 配置页各栏中文名（与 KIND 顺序一致；2026-08-10 加 video 视频生成栏；2026-09-02 加 llm_tools）
KIND_LABELS = {
    "llm": "LLM 对话",
    "llm_aux": "LLM 辅助任务",
    "vision": "图片识别模型",
    "image": "图片生成",
    "video": "视频生成",
    "llm_tools": "定制化工具模型配置",
}

# 辅助/工具任务目录（"使用方向"下拉选项——key 与消费侧 get_aux_model_cfg_for_task 的任务 key 一致）
# group（2026-09-02）：aux=纯辅助任务（llm_aux 档）；tools=定制化工具任务（llm_tools 独立档，
# 配置页「定制化工具模型配置」栏勾选；未配档的团队回退 llm_aux）
AUX_TASKS = [
    {"key": "kb_rank", "label": "知识库检索（关键词提取+精排）", "group": "aux"},
    {"key": "kb_summary", "label": "知识库文档摘要", "group": "aux"},
    {"key": "memory_extract", "label": "记忆自动提取", "group": "aux"},
    {"key": "history_summary", "label": "历史对话摘要（分层压缩）", "group": "aux"},
    {"key": "video", "label": "视频理解", "group": "tools"},
    {"key": "resume", "label": "简历评分", "group": "tools"},
    {"key": "meeting", "label": "会议纪要总结", "group": "tools"},  # 2026-08-25
]


def get_model_catalog() -> dict:
    return MODEL_CATALOG
