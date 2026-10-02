// 数据集分类与多选 —— 直接跑真模块
import { categoriesOf, cubesToTarget, groupByCategory, selectedCubes, UNCATEGORIZED } from '../../web/src/pages/Tools/QuickBI/datasetCategory';

const fail = (m: string) => { console.error('FAIL', m); process.exit(1); };
const eq = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b);
const ds = [
  { name: '票务-销售报表', category: '票务' }, { name: '客群-画像', category: '客群' },
  { name: '舆情-词云', category: null }, { name: '票务-核销', category: ' 票务 ' },
];
if (!eq(categoriesOf(ds), ['客群', '票务'])) fail('分类候选去重、去空白: ' + JSON.stringify(categoriesOf(ds)));
const g = groupByCategory(ds);
if (g[g.length - 1].label !== UNCATEGORIZED) fail('未分类排最后');
if (g.find((x) => x.label === '票务')!.items.length !== 2) fail('前后空白归到同一类');
if (!eq(selectedCubes({ cube_id: 'a' }), ['a']) || !eq(selectedCubes({ cube_ids: ['a', 'b'] }), ['a', 'b'])) fail('selectedCubes');
if (!eq(cubesToTarget([], { cube_id: 'a', web_search: false }), { web_search: false })) fail('0 个 = 自动选表,其它字段保留');
if (!eq(cubesToTarget(['a']), { cube_id: 'a' })) fail('1 个 = cube_id');
if (!eq(cubesToTarget(['a', 'b', 'a'], { cube_name: 'x' }), { cube_ids: ['a', 'b'] })) fail('多个 = cube_ids,去重,清掉按名称');
console.log('OK');
