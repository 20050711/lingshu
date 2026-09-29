/**
 * 使用说明页：全功能教学数据 + 内嵌模拟演示脚本。
 *
 * demo.steps 类型：
 * - text        ：打字机文本（模拟 AI 回复流）
 * - tool        ：工具调用提示（图标 + 名称）
 * - table       ：结果表格（columns/rows）
 * - chart       ：图表结果（模拟图表卡片）
 * - done        ：结束标记
 */

import {
  Books, Calculator, ChartLine, ChatCircle, FileText, Image,
  Lightbulb, MagnifyingGlass, NotePencil, Paperclip, VideoCamera,
  type Icon as PhosphorIcon,
} from '@phosphor-icons/react'

export interface DemoStepText { type: 'text'; content: string }
export interface DemoStepTool { type: 'tool'; name: string }
export interface DemoStepTable { type: 'table'; columns: string[]; rows: string[][] }
export interface DemoStepChart { type: 'chart'; title: string; labels: string[]; values: number[] }
export interface DemoStepDoc { type: 'doc'; name: string; file: string }
export interface DemoStepImage { type: 'image'; file: string }
export interface DemoStepVideo { type: 'video'; file: string }
export interface DemoStepStep { type: 'step'; title: string; desc: string }
export interface DemoStepDone { type: 'done' }
export type DemoStep = DemoStepText | DemoStepTool | DemoStepTable | DemoStepChart | DemoStepDoc | DemoStepImage | DemoStepVideo | DemoStepStep | DemoStepDone

export interface Feature {
  id: string
  // 2026-09-18：图标由 emoji 换成 Phosphor Light——此处存组件本体（渲染处用 <Icon as={...} />）
  icon: PhosphorIcon
  title: string
  desc: string          // 一句话人话说明（卡片上）
  group: string         // 分组
  steps: string[]       // 详细步骤（点击位置级别的教学）
  example: string       // 可直接复制的示例提问
  demo: DemoStep[]      // 内嵌模拟演示
  tips: string[]        // 注意事项/常见坑
}

export const GROUPS = ['AI 技能演示', '定制化工具演示', '其他']

export const FEATURES: Feature[] = [
  {
    id: 'chat',
    icon: ChatCircle,
    title: '对话问答',
    group: 'AI 技能演示',
    desc: '以自然语言提问，助手自动调用工具完成任务',
    steps: [
      '在左侧导航点击「智能助手」，进入对话页',
      '点击「+ 新建会话」，开始一段新对话',
      '在底部输入框输入你的问题，按回车发送',
      'AI 会边思考边回复；需要工具时会先弹出「确认卡片」，点「同意」继续',
      '回复完成后，结果（表格/图表/文件）会出现在右侧预览区',
    ],
    example: '你好，请介绍一下你自己',
    demo: [
      { type: 'text', content: '你好，我是智能助手：可以检索资料、分析数据、生成图表与报告。' },
      { type: 'text', content: '比如你可以问我："知识库里有没有差旅报销的规定？"或者"按这份表格画一张销售柱状图"——我会自动调用文件检索、图表生成等工具来完成。' },
      { type: 'done' },
    ],
    tips: ['新问题请新建会话，保持对话主题清晰', '每轮提问后可以继续追问，AI 记得前文内容'],
  },
  {
    id: 'search',
    icon: MagnifyingGlass,
    title: '资料检索与统计',
    group: 'AI 技能演示',
    desc: '在知识库里找资料，AI 读原件并直接算给你看',
    steps: [
      '进入「智能助手」页，新建会话',
      '直接说你要什么，例如："知识库里那份销售明细按区域汇总一下"',
      'AI 用文件检索定位资料，读取原件后用脚本统计',
      '结果以表格形式展示，可直接阅读',
      '需要更多口径就继续追问，例如："按客户类型汇总金额前 5 名"',
    ],
    example: '把知识库里那份销售表按区域汇总金额',
    demo: [
      { type: 'tool', name: '文件检索' },
      { type: 'tool', name: '文件解析' },
      { type: 'text', content: '已在知识库找到「销售明细.xlsx」，按区域汇总完成。' },
      { type: 'table', columns: ['区域', '销售额（元）'], rows: [['华东', '7,792.61'], ['华北', '6,795.88'], ['华南', '2,543.55']] },
      { type: 'text', content: '销售额最高的是「华东」，共 7,792.61 元。' },
      { type: 'done' },
    ],
    tips: ['资料由知识库提供，找不到可直接问 AI 它检索了哪些范围', '用业务语言提问即可，不用会写公式'],
  },
  {
    id: 'chart',
    icon: ChartLine,
    title: '图表生成',
    group: 'AI 技能演示',
    desc: '一句话让 AI 把数据画成柱状图、折线图、饼图',
    steps: [
      '新建会话后，直接要求画图',
      '示例："按区域画销售额柱状图"',
      'AI 取到数据后自动生成图表，显示在预览区',
      '不满意可继续调整："换成饼图"、"只看前 5 名"',
      '图表可以下载 PNG 图片用于汇报',
    ],
    example: '按区域画销售额柱状图',
    demo: [
      { type: 'tool', name: '文件解析' },
      { type: 'tool', name: '图表生成' },
      { type: 'chart', title: '各区域销售额对比', labels: ['华东', '华北', '华南'], values: [7793, 6796, 2544] },
      { type: 'text', content: '已生成柱状图：华东销售额最高（7,793 元）。' },
      { type: 'done' },
    ],
    tips: ['画图前先让 AI 取数（读文件/上传表格），或直接一步完成', '支持柱状图/折线图/饼图/散点图等常见类型'],
  },
  {
    id: 'report',
    icon: FileText,
    title: '报告与文档',
    group: 'AI 技能演示',
    desc: '一句话生成 Word/PPT/网页报告，可直接下载',
    steps: [
      '新建会话，说出你要的报告类型和内容',
      '示例："生成一份销售分析 docx 报告（含销售额汇总表）"',
      'AI 会取数并生成文档，产出文件出现在预览区',
      '点击文件可下载（docx/pptx/html）',
      '可以继续要求修改："标题改成季度汇报"',
    ],
    example: '生成一份销售分析 docx 报告（含销售额汇总表）',
    demo: [
      { type: 'tool', name: '文件解析' },
      { type: 'tool', name: '文档产出' },
      { type: 'text', content: '已生成「销售分析报告.docx」，包含销售概览、销售额汇总表与结论建议。' },
      { type: 'doc', name: '销售分析报告.docx', file: '/samples/sample.docx' },
      { type: 'done' },
    ],
    tips: ['docx 用 Word 打开、pptx 用 PPT 打开、html 用浏览器打开', '报告内容基于真实数据，请核对关键数字'],
  },
  {
    id: 'upload',
    icon: Paperclip,
    title: '文件上传与分析',
    group: 'AI 技能演示',
    desc: '上传 Excel/Word/PDF 等文件，由助手解析与分析',
    steps: [
      '在「智能助手」页新建会话',
      '点击输入框上方的回形针图标，选择文件（支持 xlsx/docx/pdf 等）',
      '上传后直接提问，例如："分析这个表格，各工作表多少行？"',
      'AI 会读取文件内容并回答',
      '后续提问不用重复上传，AI 记得这个文件',
    ],
    example: '分析上传的表格，各工作表多少行？',
    demo: [
      { type: 'tool', name: '文件解析' },
      { type: 'table', columns: ['工作表', '行数'], rows: [['订单类型', '63'], ['客户类型', '1,865'], ['时段', '1,911']] },
      { type: 'text', content: '文件共 7 个工作表，明细数据总计 3,839 行。' },
      { type: 'done' },
    ],
    tips: ['超过 50MB 的文件自动分片上传，中途断网可续传', '支持 xlsx/xls/csv/docx/pdf/pptx/html/md/txt 等格式'],
  },
  {
    id: 'kb',
    icon: Books,
    title: '知识库检索',
    group: 'AI 技能演示',
    desc: '问制度、流程、规范——AI 从团队知识库找答案',
    steps: [
      '新建会话，直接问知识问题',
      '示例："报销制度是什么？"',
      'AI 会检索团队知识库（制度文档、FAQ 等）',
      '答案会注明信息来源，可点开查看全文',
      '管理员可以在「定制化工具 → 知识库」里上传新文档',
    ],
    example: '报销制度是什么？',
    demo: [
      { type: 'tool', name: '知识库检索' },
      { type: 'text', content: '根据团队知识库《费用报销审批流程》：单笔报销超过 5,000 元需团队负责人审批；新增费用科目需提前 3 个工作日申请。' },
      { type: 'done' },
    ],
    tips: ['知识库由团队管理员维护，上传后立即生效', '检索不到时换个说法再问，或联系管理员补充文档'],
  },
  {
    id: 'video_gen',
    icon: VideoCamera,
    title: '视频生成',
    group: 'AI 技能演示',
    desc: '用一句话生成短视频（AI 文生视频）',
    steps: [
      '新建会话，描述你要的视频画面',
      '示例："生成一段夜空下平静湖面的短视频"',
      'AI 调用视频生成服务，生成约 3-18 秒的视频（需 1-2 分钟）',
      '完成后视频在预览区可直接播放/下载',
      '也可以上传一张图片做「图生视频」',
    ],
    example: '生成一段夜空下平静湖面的短视频',
    demo: [
      { type: 'tool', name: '视频生成' },
      { type: 'text', content: '视频生成中（约 60 秒）……' },
      { type: 'text', content: '已生成 3 秒视频，可在预览区播放。' },
      { type: 'video', file: '/samples/sample.mp4' },
      { type: 'done' },
    ],
    tips: ['生成需要排队，耐心等待', '视频每日有生成配额，用完次日恢复'],
  },
  {
    id: 'image',
    icon: Image,
    title: '图片生成与识别',
    group: 'AI 技能演示',
    desc: '让 AI 画图，或识别图片里的内容/文字',
    steps: [
      '画图：直接说"画一张深海蓝科技封面图"',
      'AI 生成图片后在预览区展示，可下载 PNG',
      '识别：上传图片后问"这张图里有什么？"',
      'AI 会识别图片内容、图表或文字',
      '识别图表还能帮你读出关键数据',
    ],
    example: '画一张深海蓝科技感封面图',
    demo: [
      { type: 'tool', name: '图片生成' },
      { type: 'text', content: '已生成图片（1024×1024），可在预览区查看并下载。' },
      { type: 'image', file: '/samples/sample.jpg' },
      { type: 'done' },
    ],
    tips: ['图片生成约 10-30 秒', '识别支持图片中的文字、图表、场景'],
  },
  {
    id: 'resume',
    icon: NotePencil,
    title: '简历批量评分',
    group: '定制化工具演示',
    desc: '上传一批简历，按岗位要求自动评分排序',
    steps: [
      '点击「定制化工具」→「简历初筛」',
      '上传简历（pdf/docx/图片，每批 ≤20 份）',
      '填写岗位要求（JD）与评分权重',
      'AI 逐份评分并排序，可查看评分明细',
      '可导出 ZIP 包带走结果',
    ],
    example: '（在工具页操作，无需提问）',
    demo: [
      { type: 'step', title: '打开工具', desc: '左侧导航点「定制化工具」→「简历初筛」' },
      { type: 'step', title: '上传简历', desc: '上传 pdf/docx/图片简历（每批 ≤20 份，重复文件自动去重）' },
      { type: 'step', title: '填写要求', desc: '填写岗位要求（JD）与评分权重' },
      { type: 'step', title: '查看评分', desc: 'AI 逐份评分并排序，可导出 ZIP 带走结果' },
      { type: 'done' },
    ],
    tips: ['评分依据你填写的 JD 与权重，越具体越准', '重复上传同一份简历会自动去重'],
  },
  {
    id: 'sandbox',
    icon: Calculator,
    title: '沙盒脚本计算',
    group: 'AI 技能演示',
    desc: '让 AI 用 Python 做计算、处理数据（安全隔离环境）',
    steps: [
      '新建会话，直接说需求',
      '示例："用 Python 计算 1 到 100 的和"',
      'AI 在隔离沙盒里运行 Python 脚本，返回结果',
      '可以处理文件、算统计量、生成表格',
      '沙盒环境只读访问你的会话文件，安全隔离',
    ],
    example: '用 Python 计算 1 到 100 的和',
    demo: [
      { type: 'tool', name: '沙盒脚本' },
      { type: 'text', content: '已在沙盒中运行 Python 脚本，计算结果：1 + 2 + … + 100 = 5,050。' },
      { type: 'done' },
    ],
    tips: ['沙盒运行有 60 秒超时，大计算请简化', '不要让它执行删除/写库等危险操作（会被拦截）'],
  },
  {
    id: 'feedback',
    icon: Lightbulb,
    title: '反馈与建议',
    group: '其他',
    desc: '遇到问题、有建议，告诉我们并附上截图',
    steps: [
      '点击「反馈」页',
      '选择反馈类型（功能建议/Bug/体验问题等）',
      '填写内容，可上传最多 4 张截图',
      '提交后可在「我的反馈」查看处理进度',
      '提交后如需修改可先「撤销」，再重新提交',
    ],
    example: '（在反馈页填写，无需提问）',
    demo: [
      { type: 'step', title: '打开反馈页', desc: '左侧导航点「反馈」，进入反馈中心' },
      { type: 'step', title: '选择类型', desc: '选择反馈类型（功能建议 / Bug / 体验问题等）' },
      { type: 'step', title: '填写内容', desc: '描述问题或建议，可上传最多 4 张截图帮助定位' },
      { type: 'step', title: '提交与跟进', desc: '提交后在「我的反馈」查看处理进度，管理员会回复' },
      { type: 'done' },
    ],
    tips: ['截图有助于快速定位问题', '紧急问题可直接找管理员'],
  },
  // 2026-09-17：CEO 全局看板卡随数据查询线下线删除（看板从未落地，数据源已撤）
]

export const FAQS = [
  {
    q: '资料是不是最新的？',
    a: 'AI 读的是知识库里的资料原件，看到什么就是什么——资料什么时候更新的，取决于它是什么时候放进知识库的。发现资料陈旧，请联系资料维护人更新。',
  },
  {
    q: '为什么我的提问被拒绝了？',
    a: '平台有安全防护：涉及泄露密钥、越权、危险操作等会被自动拦截（显示"检测到疑似提示词注入"）。请用正常业务语言提问即可。',
  },
  {
    q: '上传的文件放在哪里？安全吗？',
    a: '文件只属于你的会话，其他用户无法看到；每个会话的文件只在该会话内使用，新会话需要重新上传。',
  },
  {
    q: '图表能下载吗？',
    a: '可以。图表生成后，预览区有下载按钮（PNG 图片），也可以让 AI 把图表放进 docx/pptx 报告里。',
  },
  {
    q: '生成报告/视频要多久？',
    a: '报告一般 30 秒内；视频生成需要排队，约 1-2 分钟。等待时请保持页面打开，不要刷新（刷新会中断当前轮次，但已生成的内容不会丢）。',
  },
  {
    q: '我的账号能访问其他团队的数据吗？',
    a: '不能。每个账号只能查询本团队的数据。跨团队访问会被自动拦截。',
  },
  {
    q: '忘记密码怎么办？',
    a: '联系系统管理员重置密码。',
  },
  {
    q: 'AI 回答错了怎么办？',
    a: '可以在同一会话继续追问纠正（"不对，我要的是按城市汇总"）；如果 AI 反复答错，点「反馈」告诉我们，并附上对话截图。',
  },
]
