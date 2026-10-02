/** 系统参数「取值与分配景区」的纯函数(按值组织 ↔ 按景区存储),单测见 tests/web/param_rows.check.ts */
import type * as api from '../../lib/api';

/**
 * 一个取值 = 一行:值 + 备注 + 分配到哪些景区(或「未分配景区的默认值」)。
 * 存储上仍是两块(系统默认值 sys_param_def.default_values / 各景区取值 sys_param_value),
 * 这里按「值」重新组织,保存时再拆回去 —— 运营的心智是「这个账号给哪几个景区用」,不是「这个景区有哪些账号」。
 */
export interface Scenic { id: string; name: string }
export interface ValueRow {
  value: string; label: string; scenics: Scenic[]; isDefault: boolean;
  _masked?: string;            // 敏感参数的原值(只有末四位);value 留空 = 不改
  _origScenics?: string[];     // 敏感参数原来分配的景区(保持原值只对这些景区成立)
}
/** 只用到表单里这几项,不依赖组件 */
export interface RowsForm { multi_mode: api.ParamMultiMode; _rows: ValueRow[] | null }

/** 系统默认值 + 各景区取值 → 按值分组的行 */
export function toRows(d: api.SysParamDef, assigns: api.ParamAssignment[]): ValueRow[] {
  const rows: ValueRow[] = []
  const find = (v: api.ParamValue) => rows.find((r) => (r._masked ?? r.value) === v.value && r.label === v.label)
  const add = (v: api.ParamValue): ValueRow => {
    const hit = find(v)
    if (hit) return hit
    const r: ValueRow = d.is_secret
      ? { value: '', label: v.label, scenics: [], isDefault: false, _masked: v.value, _origScenics: [] }
      : { value: v.value, label: v.label, scenics: [], isDefault: false }
    rows.push(r)
    return r
  }
  d.default_values.forEach((v) => { add(v).isDefault = true })
  assigns.forEach((a) => a.values.forEach((v) => {
    const r = add(v)
    r.scenics.push({ id: a.scenic_id, name: a.scenic_name || a.scenic_id })
    r._origScenics?.push(a.scenic_id)
  }))
  return rows.length ? rows : [{ value: '', label: '', scenics: [], isDefault: false }]
}

/** 按值分组的行 → 保存用的两块;有问题返回错误文案 */
export function fromRows(form: RowsForm): { error?: string; defaults?: api.ParamValue[] | null; assigns?: { scenic_id: string; values: api.ParamValue[] }[] } {
  const rows = (form._rows || []).filter((r) => r.value.trim() || r._masked)
  const multi = form.multi_mode !== 'single'
  for (const r of rows) {
    if (!r.isDefault && !r.scenics.length) return { error: `取值「${r.value || r._masked}」还没分配景区` }
    if (r._masked && !r.value.trim() && r.scenics.some((s) => !(r._origScenics || []).includes(s.id))) {
      return { error: `敏感值「${r._masked}」新分配了景区,需要重新填写完整的值` }
    }
  }
  const byScenic = new Map<string, ValueRow[]>()
  rows.forEach((r) => r.scenics.forEach((s) => byScenic.set(s.id, [...(byScenic.get(s.id) || []), r])))
  const defRows = rows.filter((r) => r.isDefault)
  if (!multi) {
    const dup = [...byScenic.entries()].find(([, rs]) => rs.length > 1)
    if (dup) return { error: `单值参数:景区「${rows.flatMap((r) => r.scenics).find((s) => s.id === dup[0])?.name}」分到了多个值` }
    if (defRows.length > 1) return { error: '单值参数只能有一个默认值' }
  }
  const kept = (r: ValueRow) => !!r._masked && !r.value.trim()
  const defaults = defRows.some(kept) ? (defRows.every(kept) ? null : undefined) : defRows.map((r) => ({ value: r.value.trim(), label: r.label.trim() }))
  if (defaults === undefined) return { error: '敏感参数的默认值要么全部保持,要么全部重填' }
  const assigns: { scenic_id: string; values: api.ParamValue[] }[] = []
  for (const [sid, rs] of byScenic) {
    if (rs.some(kept) && !rs.every(kept)) return { error: '同一个景区的敏感值要么全部保持,要么全部重填' }
    // 全部保持 → 传空数组,后端对敏感参数的空值 = 保持原值
    assigns.push({ scenic_id: sid, values: rs.every(kept) ? [] : rs.map((r) => ({ value: r.value.trim(), label: r.label.trim() })) })
  }
  return { defaults, assigns }
}

