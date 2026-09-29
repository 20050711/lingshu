import {
  ArrowsClockwise, ArrowsInLineVertical, Book, Brain, Buildings, CalendarBlank, ChartLine,
  ChatCircle, CheckCircle, Database, Desktop, Envelope, FileText, FilmSlate, FolderOpen,
  Globe, Headphones, Image, Link as LinkIcon, MagnifyingGlass, NotePencil, Package, Palette,
  PuzzlePiece, Question, Tray, VideoCamera,
  type Icon as PhosphorIcon, type IconProps,
} from '@phosphor-icons/react'

/**
 * 全站统一图标封装（2026-09-18 图标替换批次）。
 *
 * 为什么要有它：替换前全站用 emoji 当图标（🏠🧩🎯📦 等 150+ 处）——跨平台字形不一致、
 * 无法跟随文字色、读屏会把"拼图块"按字面念出来。统一收口到 Phosphor Light 细线风格。
 *
 * 约定：
 * - `weight` 默认 "light"——Phosphor 的粗细是**烘焙进 path 数据**的，没有 stroke-width 属性可调
 * - `size` 默认 16（与正文行高对齐）；标题区/功能卡可传 18 / 20 / 24
 * - **装饰性图标**（旁边已有文字）默认 `aria-hidden`；有独立语义时传 `label`，
 *   此时自动补 `role="img"` + `aria-label`（WCAG：图标不能裸奔）
 * - 颜色继承 `currentColor`，不传 `color` 即跟随父级文字色
 *
 * 用法：
 *   <Icon as={House} />                     // 装饰：旁有文字
 *   <Icon as={Warning} label="警告" />      // 语义：独立承载信息
 *   <Icon as={VideoCamera} size={20} />
 *   配置数组里存组件本体：{ icon: House, label: '首页' }
 */
export default function Icon({
  as: Component,
  size = 16,
  weight = 'light',
  label,
  className,
  ...rest
}: IconProps & { as: PhosphorIcon; label?: string }) {
  return (
    <Component
      size={size}
      weight={weight}
      // 2026-09-18：统一挂 `.ph-icon`——行内图标的垂直对齐在 theme.css 里**一处**收口
      // （原先没有任何规则管它，SVG 按 baseline 摆、整体偏高 2-4px，全站实测 18 处）。
      // 调用方传的 className 原样保留（合并而非覆盖）。
      className={className ? `ph-icon ${className}` : 'ph-icon'}
      aria-hidden={label ? undefined : true}
      aria-label={label}
      role={label ? 'img' : undefined}
      {...rest}
    />
  )
}

/**
 * 后端下发的 icon 是**语义名**（工具注册表 `ToolSpec.icon` / `MCP_DEFAULTS`，
 * 取值见 backend/app/agent/tools/__init__.py 的 `ICON_KEYS`）。
 *
 * 后端只声明"这是什么类别的工具"，画成什么图标由这里决定——**不要在两端各存一份图标字典**，
 * 更不要让后端存 emoji：那是把表现塞进数据，前端只能靠一张 emoji 对照表兜底，
 * 后端换个 emoji 就漏到界面上（2026-09-18 由 emoji 方案改回语义名，MCP 那份已跑迁移转换）。
 *
 * 新增图标：先在 `ICON_KEYS` 加名字（后端有白名单校验、运维下拉也读它），再来这里加映射。
 * 未收录的一律落到 `PuzzlePiece`——后端先行上线也不会让界面出错。
 */
const KEY_ICONS: Record<string, PhosphorIcon> = {
  tool: PuzzlePiece, chart: ChartLine, video: VideoCamera, audio: Headphones, image: Image,
  film: FilmSlate, screen: Desktop, code: Desktop, brain: Brain, file: FileText,
  note: NotePencil, book: Book, 'book-open': Book, folder: FolderOpen, database: Database,
  package: Package, compress: ArrowsInLineVertical, search: MagnifyingGlass, link: LinkIcon,
  download: Tray, globe: Globe, mail: Envelope, calendar: CalendarBlank, building: Buildings,
  chat: ChatCircle, check: CheckCircle, question: Question, refresh: ArrowsClockwise,
  palette: Palette,
}

export function iconFromKey(key: string | null | undefined): PhosphorIcon {
  return (key && KEY_ICONS[key]) || PuzzlePiece
}
