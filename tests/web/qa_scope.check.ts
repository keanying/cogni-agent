// 仪表板问数 / 解读的组件范围 —— 直接跑真模块
import { directTarget, fixedTargetValue, savedScope, scopeFields } from '../../web/src/pages/Tools/QuickBI/fixedTarget';

const fail = (m: string) => { console.error('FAIL', m); process.exit(1); };
const eq = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b);
if (!eq(savedScope({ asset_id: 'a' }), { legacyPickOne: false })) fail('没存范围 = 全部组件、新做法');
if (!eq(savedScope({ qa_scope: 'dataset', chart_ids: ['c1'] }), { chartIds: ['c1'], legacyPickOne: true })) fail('回填');
if (!eq(scopeFields('dashboard_qa', true, undefined), {})) fail('全部组件不带字段');
if (!eq(scopeFields('dashboard_qa', true, ['c1']), { chart_ids: ['c1'] })) fail('部分组件');
if (!eq(scopeFields('dashboard_qa', true, undefined, true), { qa_scope: 'dataset' })) fail('旧配置原样带回');
if (!eq(scopeFields('insight', true, ['c1'], true), { chart_ids: ['c1'] })) fail('解读不带 qa_scope');
if (!eq(scopeFields('dashboard_qa', false, ['c1']), {})) fail('非仪表板不带');
if (!eq(scopeFields('dataset_qa', true, ['c1']), {})) fail('其它能力不带');
// 多选数据集的固定问题:操练台原样回填、原样发回
const v = fixedTargetValue({ cube_ids: ['k1', 'k2'] }, []);
if (v !== 'cubes:k1,k2') fail('多选回填: ' + v);
if (!eq(directTarget(v), { cube_ids: ['k1', 'k2'] })) fail('多选发回');
console.log('OK');
