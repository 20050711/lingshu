// API 前缀统一出口（2026-08-19，架构 P1）：后端 /api/v1 前缀此前散落 16 处硬编码
// （client.ts baseURL + 各页面 fetch 拼接）——改后端前缀只需改此常量 + vite 代理同步
export const API_PREFIX = '/api/v1'
