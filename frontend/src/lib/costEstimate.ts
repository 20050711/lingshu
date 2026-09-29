/** token 费用估算（需求 4，2026-08-17）：DeepSeek 价格按北京时间时段（高峰=9-12、14-18，
 * 其余空闲=半价）与缓存命中/未命中换算（docs/官方接口规范/deepseek接口规范/价格.md）。
 * 非 deepseek 模型显示未知（不估算）。 */

// 元/百万 tokens [空闲, 高峰]（2026-09-10 按官方价格.md 更新；deepseek-v4-pro 已下架）
// 旧名 deepseek-v4-flash / deepseek-v4-flash-vision-exp 仍可调用但按 Flash 计费 → 同价
const DEEPSEEK_PRICE: Record<string, { hit: number[]; miss: number[]; out: number[] }> = {
  'deepseek-flash': { hit: [0.02, 0.04], miss: [1, 2], out: [4, 8] },
  'deepseek-v4-flash': { hit: [0.02, 0.04], miss: [1, 2], out: [4, 8] },
  'deepseek-v4-flash-vision-exp': { hit: [0.02, 0.04], miss: [1, 2], out: [4, 8] },
}

function isPeakHour(atMs?: number): boolean {
  // 北京时间 = UTC+8（不依赖浏览器时区）
  const d = atMs ?? Date.now()
  const bj = (new Date(d).getUTCHours() + 8) % 24
  return (bj >= 9 && bj < 12) || (bj >= 14 && bj < 18)
}

export interface CostInfo {
  cost_tokens: number
  cost_prompt_hit: number
  cost_prompt_miss: number
  cost_completion: number
  cost_model?: string | null
  last_activity_at?: string | null  // 2026-09-08 计费定档：按会话最后活动时间的时段价（历史金额不随查看时间漂移）
}

/** 返回（估算金额字符串, 是否可估算）。非 deepseek 模型返回 ('未知', false)。 */
export function estimateCost(s: CostInfo | undefined | null): { text: string; known: boolean } {
  if (!s || !s.cost_model) return { text: '未知', known: false }
  const p = DEEPSEEK_PRICE[s.cost_model]
  if (!p) return { text: '未知', known: false }
  // 计时档位：优先会话最后活动时间（历史金额稳定）；新会话（未落库）回退当前时间
  const atMs = s.last_activity_at ? new Date(s.last_activity_at).getTime() : undefined
  const peak = isPeakHour(atMs) ? 1 : 0
  const yuan =
    ((s.cost_prompt_hit ?? 0) * p.hit[peak] +
      (s.cost_prompt_miss ?? 0) * p.miss[peak] +
      (s.cost_completion ?? 0) * p.out[peak]) / 1e6
  return { text: `¥${yuan.toFixed(4)}`, known: true }
}
