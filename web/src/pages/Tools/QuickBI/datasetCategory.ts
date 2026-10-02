/**
 * 数据集的自定义分类与多选 —— 纯逻辑(无 React 依赖,便于单测)。
 *
 * - 分类是运营在数据集上随手输入的(回车即存,不需要先建分类),空 = 未分类;
 * - 编辑问题时按分类多选 / 全选 / 单选数据集:选 0 个 = 自动选表,1 个 = cube_id,
 *   多个 = cube_ids(后端走官方 multipleCubeIds 多表问数,由小Q选一张或多张表)。
 */
export interface CatItem { id: string; ref_id: string; name: string; category?: string | null; active?: boolean }

export const UNCATEGORIZED = '未分类';

export const categoryOf = (d: { category?: string | null }) => (d.category || '').trim() || UNCATEGORIZED;

/** 已有的分类名(去重、按名称排序),给输入框做候选 */
export function categoriesOf(items: { category?: string | null }[]): string[] {
  return Array.from(new Set(items.map((d) => (d.category || '').trim()).filter(Boolean)))
    .sort((a, b) => a.localeCompare(b, 'zh'));
}

/** 按分类分组;「未分类」排最后,组内按名称排序 */
export function groupByCategory<T extends { category?: string | null; name: string }>(items: T[]):
  { key: string; label: string; items: T[] }[] {
  const m = new Map<string, T[]>();
  for (const d of items) {
    const k = categoryOf(d);
    m.set(k, [...(m.get(k) || []), d]);
  }
  return Array.from(m.entries())
    .sort(([a], [b]) => (a === UNCATEGORIZED ? 1 : b === UNCATEGORIZED ? -1 : a.localeCompare(b, 'zh')))
    .map(([k, list]) => ({ key: k, label: k, items: [...list].sort((x, y) => x.name.localeCompare(y.name, 'zh')) }));
}

/** target → 已选的 cubeId 列表 */
export function selectedCubes(target: Record<string, any> | null | undefined): string[] {
  const t = target || {};
  if (Array.isArray(t.cube_ids) && t.cube_ids.length) return t.cube_ids.map(String);
  return t.cube_id ? [String(t.cube_id)] : [];
}

/** 已选的 cubeId 列表 → target(保留 target 里与数据集无关的字段,如 web_search) */
export function cubesToTarget(ids: string[], base: Record<string, any> = {}): Record<string, any> {
  const { cube_id: _a, cube_ids: _b, cube_name: _c, ...rest } = base || {};
  const uniq = Array.from(new Set(ids.filter(Boolean)));
  if (uniq.length === 0) return rest;
  if (uniq.length === 1) return { ...rest, cube_id: uniq[0] };
  return { ...rest, cube_ids: uniq };
}
