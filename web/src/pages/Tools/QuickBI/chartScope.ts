/**
 * 解读 ·「图表范围」的纯逻辑(与后端 capabilities.is_data_chart / run_dashboard_insight 同口径)。
 *
 * - 仪表板结构里除了图表还有容器 / 页签 / 文本等组件,它们没有数据,不算图表、也不可选;
 * - chart_ids 为空(undefined / [])= 全选 —— 看板以后新加的图表也自动算进来,
 *   只有勾掉了一部分,才把勾选的 id 存下来。
 */
export interface ScopeChart {
  component_id: string; name?: string; chart_type?: string; cube_id?: string | null; cube_name?: string | null;
}

const NON_CHART = /(container$|^tab$|^tabs$|^text$|^query2$|iframe|^image$|^picture$|^layout|divider|blank)/i;

export const isDataChart = (c: ScopeChart) => !!c.cube_id && !NON_CHART.test(String(c.chart_type || ''));

export const dataCharts = (charts: ScopeChart[] | undefined | null) => (charts || []).filter(isDataChart);

/** 图表显示名:没起名的(Quick BI 里常见)退回「未命名图表 · 类型」,不把一串 uuid 摊给人看 */
export const chartLabel = (c: ScopeChart) =>
  (c.name && !/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(c.name)) ? c.name : `未命名图表 · ${c.chart_type || '图表'}`;

/** 勾选结果 → 存进 target 的 chart_ids:全选时不存(undefined) */
export function toChartIds(selected: Set<string>, all: ScopeChart[]): string[] | undefined {
  const ids = all.map((c) => c.component_id);
  if (ids.length && ids.every((id) => selected.has(id))) return undefined;
  return ids.filter((id) => selected.has(id));
}

/** target 里的 chart_ids → 勾选集合:没存 = 全选;存了但看板已改掉的 id 自动丢掉 */
export function fromChartIds(chartIds: string[] | undefined | null, all: ScopeChart[]): Set<string> {
  const ids = all.map((c) => c.component_id);
  if (!chartIds || !chartIds.length) return new Set(ids);
  const keep = new Set(chartIds);
  return new Set(ids.filter((id) => keep.has(id)));
}

/** 「全部 59 张」/「已选 12 / 59 张」 */
export function scopeSummary(chartIds: string[] | undefined | null, total: number): string {
  if (!chartIds || !chartIds.length) return `全部 ${total} 张`;
  return `已选 ${Math.min(chartIds.length, total)} / ${total} 张`;
}

/** 按数据集分组(看板上的图常常十几张共用一个集,分组后好找),组内保持看板上的顺序 */
export function groupByCube(charts: ScopeChart[]): { key: string; label: string; items: ScopeChart[] }[] {
  const m = new Map<string, { key: string; label: string; items: ScopeChart[] }>();
  for (const c of charts) {
    const key = c.cube_id || '-';
    if (!m.has(key)) m.set(key, { key, label: c.cube_name || c.cube_id || '未知数据集', items: [] });
    m.get(key)!.items.push(c);
  }
  return Array.from(m.values());
}
