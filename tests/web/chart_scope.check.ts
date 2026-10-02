// 解读 ·「图表范围」纯逻辑 —— 直接跑真模块
import { chartLabel, dataCharts, fromChartIds, groupByCube, scopeSummary, toChartIds } from '../../web/src/pages/Tools/QuickBI/chartScope';

const fail = (m: string) => { console.error('FAIL', m); process.exit(1); };
const all = dataCharts([
  { component_id: 'a', name: '总评趋势', chart_type: 'indicator-trend', cube_id: 'k1', cube_name: '舆情' },
  { component_id: 'b', name: '623945c5-183d-433e-b303-d82d306bb66c', chart_type: 'bar', cube_id: 'k1', cube_name: '舆情' },
  { component_id: 'c', name: '年龄分布', chart_type: 'cross-table', cube_id: 'k2', cube_name: '客群' },
  { component_id: 'box', name: 'x', chart_type: 'concat-container', cube_id: null },
  { component_id: 't', name: '标签页', chart_type: 'tab', cube_id: 'k1' },
]);
if (all.map((c) => c.component_id).join() !== 'a,b,c') fail('容器/页签不算图表,交叉表算: ' + all.map((c) => c.component_id));
if (chartLabel(all[1]) !== '未命名图表 · bar') fail('uuid 名字不直接摊给人看');
if (toChartIds(new Set(['a', 'b', 'c']), all) !== undefined) fail('全选不存 id');
if (toChartIds(new Set(['c']), all)?.join() !== 'c') fail('部分选择存 id');
if (fromChartIds(undefined, all).size !== 3) fail('没存 = 全选');
if ([...fromChartIds(['c', 'gone'], all)].join() !== 'c') fail('看板改掉的 id 自动丢掉');
if (scopeSummary(undefined, 59) !== '全部 59 张' || scopeSummary(['a', 'b'], 59) !== '已选 2 / 59 张') fail('summary');
const g = groupByCube(all);
if (g.length !== 2 || g[0].label !== '舆情' || g[0].items.length !== 2) fail('按数据集分组');
console.log('OK');
