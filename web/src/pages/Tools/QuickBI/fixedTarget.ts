/** 操练台 · 固定问题 → 「目标数据」下拉框的值。纯函数,无 React 依赖(便于单测)。
 *
 * 固定问题的目标数据有几种存法:登记过的资产(asset_id)、直接从 Quick BI 数据集列表选的
 * (cube_id,编辑固定问题时「数据集」页签就是这么存的)、仪表板 pageId、上传文件(file_id)。
 * 以前只认 asset_id / file_id —— 配了数据集的问题一选中,下拉框停在「按景区自动关联」,
 * 控件显示的和这一问实际要跑的对不上。
 *
 * 取值:asset:<id> / file:<id> / cube:<cubeId> / page:<pageId> / ''。
 * cube / page 优先映射回登记过的同一份资产(下拉里有名字、按板分组);没登记才用直连值。
 */
export interface AssetLike { id: string; kind: string; ref_id: string; name?: string }

export function fixedTargetValue(target: Record<string, any> | null | undefined, assets: AssetLike[]): string {
  const t = target || {};
  if (t.asset_id) return `asset:${t.asset_id}`;
  if (t.file_id) return `file:${t.file_id}`;
  // 多选了数据集(多表问数):原样回填,发起时还是这几个一起问
  if (Array.isArray(t.cube_ids) && t.cube_ids.length > 1) return `cubes:${t.cube_ids.join(',')}`;
  if (t.cube_id) {
    const a = assets.find((x) => x.kind === 'dataset' && x.ref_id === t.cube_id);
    return a ? `asset:${a.id}` : `cube:${t.cube_id}`;
  }
  const page = t.page_id || t.dashboard;
  if (page) {
    const a = assets.find((x) => x.kind === 'dashboard' && x.ref_id === page);
    return a ? `asset:${a.id}` : `page:${page}`;
  }
  return '';
}

/** 下拉值 → 发给后端的 target。cube / page 直连;资产、文件另由调用方按列表解析。 */
export function directTarget(value: string): Record<string, string | string[]> | undefined {
  const i = value.indexOf(':');
  if (i < 0) return undefined;
  const kind = value.slice(0, i), id = value.slice(i + 1);
  if (kind === 'cube' && id) return { cube_id: id };
  if (kind === 'cubes' && id) {
    const ids = id.split(',').filter(Boolean);
    return ids.length > 1 ? { cube_ids: ids } : ids.length ? { cube_id: ids[0] } : undefined;
  }
  if (kind === 'page' && id) return { page_id: id };
  return undefined;
}

/**
 * 仪表板问数 / 解读的「图表范围」(组件范围)。
 *
 * 仪表板问数对齐官方「小Q报表技能」:问题点到某张图 → 用它的数据集;否则看板(或所选组件)
 * 的全部数据集一起走 multipleCubeIds 多表问数。所以范围只剩「哪些组件」这一件事 ——
 * chart_ids 为空 = 全部组件。
 *
 * qa_scope=dataset 是 v3.56 存量配置里显式选过的「挑一份数据集」老跑法:原样保留、原样回传,不去改它。
 */
export function savedScope(target: Record<string, any> | null | undefined): { chartIds?: string[]; legacyPickOne: boolean } {
  const t = target || {};
  const ids = Array.isArray(t.chart_ids) && t.chart_ids.length ? t.chart_ids.map(String) : undefined;
  return { chartIds: ids, legacyPickOne: t.qa_scope === 'dataset' };
}

/** 发给后端的 target 里要追加的范围字段(只对「仪表板 + 解读 / 报表问数」有意义) */
export function scopeFields(capability: string, isDashboard: boolean, chartIds: string[] | undefined,
                            legacyPickOne = false): Record<string, any> {
  if (!isDashboard || !['insight', 'dashboard_qa'].includes(capability)) return {};
  const out: Record<string, any> = chartIds && chartIds.length ? { chart_ids: chartIds } : {};
  if (capability === 'dashboard_qa' && legacyPickOne) out.qa_scope = 'dataset';
  return out;
}
