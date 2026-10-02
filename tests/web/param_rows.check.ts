// 系统参数「取值与分配景区」:按值组织 ↔ 按景区存储 的往返
import { toRows, fromRows } from '../../web/src/components/system/paramRows'

let bad = 0
const fail = (m: string) => { bad++; console.log('FAIL', m) }
const eq = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b)

const def: any = { param_key: 'ota_account', is_secret: false, multi_mode: 'fanout',
  default_values: [] }
const assigns: any = [
  { scenic_id: 'A', scenic_name: '九龙潭', values: [{ value: 'vbk_1', label: '携程主' }, { value: 'vbk_2', label: '' }] },
  { scenic_id: 'B', scenic_name: '大金湖', values: [{ value: 'vbk_1', label: '携程主' }] },
]
const rows = toRows(def, assigns)
if (!eq(rows.map(r => [r.value, r.scenics.map(s => s.name)]), [['vbk_1', ['九龙潭', '大金湖']], ['vbk_2', ['九龙潭']]])) fail('按值分组: ' + JSON.stringify(rows))
const out = fromRows({ multi_mode: 'fanout', _rows: rows })
if (out.error || !eq(out.defaults, [])) fail('无默认值')
if (!eq(out.assigns, [{ scenic_id: 'A', values: [{ value: 'vbk_1', label: '携程主' }, { value: 'vbk_2', label: '' }] },
  { scenic_id: 'B', values: [{ value: 'vbk_1', label: '携程主' }] }])) fail('拆回按景区: ' + JSON.stringify(out.assigns))

// 值没分配景区也没勾默认 → 报错
if (!fromRows({ multi_mode: 'fanout', _rows: [{ value: 'x', label: '', scenics: [], isDefault: false }] }).error) fail('未分配要报错')
// 勾了默认 → 进系统默认值
const d = fromRows({ multi_mode: 'single', _rows: [{ value: 'x', label: '', scenics: [], isDefault: true }] })
if (d.error || !eq(d.defaults, [{ value: 'x', label: '' }]) || !eq(d.assigns, [])) fail('默认值: ' + JSON.stringify(d))
// 单值:同一景区分到两个值 → 报错
const s1 = fromRows({ multi_mode: 'single', _rows: [
  { value: 'a', label: '', scenics: [{ id: 'A', name: '九龙潭' }], isDefault: false },
  { value: 'b', label: '', scenics: [{ id: 'A', name: '九龙潭' }], isDefault: false }] })
if (!s1.error || !s1.error.includes('九龙潭')) fail('单值重复分配')
// 敏感:留空保持原值(原景区)→ 传空数组;新加景区却没重填 → 报错
const sec: any = { ...def, is_secret: true, default_values: [] }
const srows = toRows(sec, [{ scenic_id: 'A', scenic_name: '九龙潭', values: [{ value: '••••9202', label: '' }] }])
const keep = fromRows({ multi_mode: 'fanout', _rows: srows })
if (keep.error || !eq(keep.assigns, [{ scenic_id: 'A', values: [] }]) || keep.defaults !== null && !eq(keep.defaults, [])) fail('敏感保持: ' + JSON.stringify(keep))
srows[0].scenics.push({ id: 'B', name: '大金湖' })
if (!fromRows({ multi_mode: 'fanout', _rows: srows }).error) fail('敏感新分配要重填')

if (bad) process.exit(1)
console.log('OK')
