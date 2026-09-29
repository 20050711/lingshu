// AI 外部工具（MCP）API（2026-09-03：员工启停 + 运维管理）
import client from './client'

export const mcpApi = {
  // 员工视图：列表 + 我的启用集
  list: () => client.get('/mcp/tools').then((r) => r.data),
  savePrefs: (toolIds: string[]) =>
    client.put('/mcp/tools/prefs', { tool_ids: toolIds }).then((r) => r.data),
  // 运维管理（admin）
  adminList: () => client.get('/mcp/admin/tools').then((r) => r.data),
  adminCreate: (body: { id: string; name: string; icon?: string; description?: string; url?: string }) =>
    client.post('/mcp/admin/tools', body).then((r) => r.data),
  adminUpdate: (id: string, body: { name?: string; icon?: string; description?: string; url?: string; status?: string }) =>
    client.put(`/mcp/admin/tools/${id}`, body).then((r) => r.data),
  adminDelete: (id: string) => client.delete(`/mcp/admin/tools/${id}`).then((r) => r.data),
  adminTest: (id: string) => client.post(`/mcp/admin/tools/${id}/test`).then((r) => r.data),
  // 2026-09-18：可选图标名清单（后端的 ICON_KEYS）——运维表单下拉读它，
  // 不在前端再存一份，避免两处清单漂移
  iconKeys: (): Promise<{ keys: string[]; default: string }> =>
    client.get('/mcp/icon-keys').then((r) => r.data),
}
