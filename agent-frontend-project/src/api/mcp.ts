import api, { type ApiResponse } from './http'

/**
 * MCP 接入状态（第 3 期）。
 *
 * 为什么前端要看这个：MCP 是**外部能力**，"现在到底挂上了什么"必须能自查——
 * 否则用户只能从模型行为里猜（工具没被调用，是模型不想用，还是根本没连上？）。
 */

/** 一个 MCP server 的连接状态：与后端 `MCPServerStatus.as_dict()` 逐字段对齐 */
export interface McpServerStatus {
  name: string
  connected: boolean
  /** stdio（本地子进程）/ streamable-http（远端服务） */
  transport: string
  tool_count: number
  tools: string[]
  /** 连不上时的原因（原样来自后端，用于排查） */
  error: string
}

export interface McpStatus {
  /** MCP_ENABLED 是否打开 */
  enabled: boolean
  /** 未启用或没配 server 时，这里写明原因（不启用时也可能非空） */
  reason: string
  servers: McpServerStatus[]
  /** 已接入的工具总数（MCP 未启用时后端不返回该字段） */
  tool_count?: number
}

/** 拉取 MCP 接入状态（只读，不返回任何用户数据）。 */
export async function fetchMcpStatus(): Promise<McpStatus> {
  const response = await api.get<ApiResponse<McpStatus>>('/agent/mcp/servers')
  return response.data.data
}
