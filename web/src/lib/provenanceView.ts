/**
 * 思考过程「版本溯源」的展示裁剪。
 *
 * resolve_provenance 事件里带着技能下**所有**已绑定工具的版本来源(解析阶段就全部定好了),
 * 原先思考过程一次性把它们全列出来 —— 绑了十几个工具的技能,一展开就是一整屏 pin 记录,
 * 真正调到的那一两个反而淹没在里面。
 *
 * 这里改为:只列本轮**实际调用到**的工具(随 tool_call 步骤逐步出现),
 * 其余已绑定但没用到的只给一个数量,需要时再展开。完整数据仍在事件里,导出/排查不受影响。
 */

export interface ProvTool { source?: string; version?: number | string; version_id?: string; name?: string }
export interface ProvToolRow { id: string; tool: ProvTool }

/** 从时间线步骤里收集本轮调用过的工具标识(函数名 + 展示名 + 工具 ID)。 */
export function usedToolKeys(steps: { step: string; data?: any }[] | undefined): Set<string> {
  const keys = new Set<string>();
  for (const s of steps || []) {
    // 只认真的发起过调用的步骤;tool_disabled(被拦下、没执行)不算「用到」
    if (s.step !== 'tool_call' && s.step !== 'tool_call_skipped') continue;
    const d = s.data || {};
    for (const k of [d.tool_name, d.tool_display_name, d.tool_id]) {
      if (k) keys.add(String(k));
    }
  }
  return keys;
}

/** 把某个技能的工具溯源拆成「用到的」和「未用到的」两组,保持原顺序。 */
export function splitProvenanceTools(
  tools: Record<string, ProvTool> | undefined,
  used: Set<string>,
): { used: ProvToolRow[]; unused: ProvToolRow[] } {
  const out = { used: [] as ProvToolRow[], unused: [] as ProvToolRow[] };
  for (const [id, tool] of Object.entries(tools || {})) {
    const hit = used.has(id) || (!!tool?.name && used.has(tool.name));
    (hit ? out.used : out.unused).push({ id, tool });
  }
  return out;
}
