/** ECharts option 前端净化（SEC-24，2026-08-17）：
 * 递归删除事件钩子键（on*）、危险协议串（javascript:/<script）、原型污染键。
 * 与 SEC-20（echarts ≥6.1.0 CVE 修复）互为纵深。
 */
const DANGEROUS_KEYS = new Set(['__proto__', 'constructor', 'prototype'])

// 2026-08-20（P4 修复）：后端写入的 JS 源码字符串 formatter → 真实函数的精确匹配还原表。
// 不用 new Function——部署形态 CSP script-src 无 'unsafe-eval'（deploy/aip.nginx），浏览器抛
// EvalError 被 catch 静默回退，Y 轴仍显示函数源码（走查实测）。formatter 来源唯一：
// backend/app/services/chart_builder.py:27（Y_AXIS_FORMATTER）——新增 formatter 串时同步
// 这里与 backend/app/static/templates/report.html.j2 的 restoreFns（互指注释）。
const FORMATTER_RESTORE: Record<string, (v: number) => number | string> = {
  "function(v){return v>=10000?(v/10000).toFixed(1)+'万':v;}": (v) =>
    v >= 10000 ? (v / 10000).toFixed(1) + '万' : v,
}

export function sanitizeChartOption(option: unknown): unknown {
  if (Array.isArray(option)) return option.map((v) => sanitizeChartOption(v))
  if (option && typeof option === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(option as Record<string, unknown>)) {
      if (DANGEROUS_KEYS.has(k)) continue
      if (k.startsWith('on')) continue // 事件钩子（onclick/onerror/onmouseover…）
      if (typeof v === 'string') {
        if (/javascript:/i.test(v) || /<script/i.test(v)) continue
        // P4（2026-08-20）：formatter 字符串按精确匹配表还原为真实函数（免 eval——
        // 原 new Function 在 CSP 下必失败且 dev/prod 行为不一致，属隐藏 bug）。
        // 未命中匹配表的 formatter 保持原样（与还原失败等价，不更糟）。
        if (k === 'formatter' && FORMATTER_RESTORE[v]) {
          out[k] = FORMATTER_RESTORE[v]
          continue
        }
        out[k] = v
      } else {
        out[k] = sanitizeChartOption(v)
      }
    }
    return out
  }
  return option
}


// 深色玻璃主题下的图表默认样式（2026-09-28）：agent 生成的 option 多为浅底默认色，
// 在近黑页面上坐标轴/图例会看不清。仅在**未显式指定**时注入深色默认值，不覆盖 agent 的选择。
const DARK_AXIS_DEFAULT = {
  axisLabel: { color: '#8a95a8' },
  axisLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.18)' } },
  splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.06)' } },
  nameTextStyle: { color: '#8a95a8' },
}

function mergeAxis(ax: any): any {
  if (Array.isArray(ax)) return ax.map(mergeAxis)
  if (!ax || typeof ax !== 'object') return ax
  return {
    ...DARK_AXIS_DEFAULT,
    ...ax,
    axisLabel: { ...DARK_AXIS_DEFAULT.axisLabel, ...(ax.axisLabel || {}) },
    axisLine: { ...DARK_AXIS_DEFAULT.axisLine, ...(ax.axisLine || {}) },
    splitLine: { ...DARK_AXIS_DEFAULT.splitLine, ...(ax.splitLine || {}) },
  }
}

export function withDarkChartTheme(option: any): any {
  if (!option || typeof option !== 'object' || Array.isArray(option)) return option
  const out: Record<string, any> = { ...option }
  out.backgroundColor = out.backgroundColor ?? 'transparent'
  out.textStyle = { color: '#a9b4c6', ...(out.textStyle || {}) }
  if (out.title) out.title = { textStyle: { color: '#f2f5fa', ...(out.title.textStyle || {}) }, ...out.title }
  if (out.legend) out.legend = { textStyle: { color: '#a9b4c6', ...(out.legend.textStyle || {}) }, ...out.legend }
  out.tooltip = {
    backgroundColor: 'rgba(20, 23, 31, 0.96)',
    borderColor: 'rgba(255, 255, 255, 0.12)',
    textStyle: { color: '#f2f5fa' },
    ...(out.tooltip || {}),
  }
  if (out.xAxis) out.xAxis = mergeAxis(out.xAxis)
  if (out.yAxis) out.yAxis = mergeAxis(out.yAxis)
  if (out.radar) out.radar = { ...out.radar, axisName: { color: '#a9b4c6', ...(out.radar.axisName || {}) }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.08)' }, ...(out.radar.splitLine || {}) } }
  return out
}
