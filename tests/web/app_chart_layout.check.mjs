// APP 聊天气泡图表高度 —— 直接跑 uni 工程里的真模块
import { chartHeight } from '/home/claude/work/uni/digital-brain-uni-v1.1.1/src/utils/chartLayout.js';

const fail = (m) => { console.error('FAIL', m); process.exit(1); };
if (chartHeight({ series: [{ type: 'gauge' }] }) !== 200) fail('gauge');
if (chartHeight({ legend: {}, series: [{ type: 'pie' }] }) !== 260) fail('pie+legend');
const rank = { xAxis: { type: 'value' }, yAxis: { type: 'category', data: Array.from({ length: 15 }, (_, i) => 'x' + i) }, series: [{ type: 'bar' }] };
if (chartHeight(rank) !== 15 * 26 + 70) fail('排名图按项数加高: ' + chartHeight(rank));
const big = { ...rank, yAxis: { type: 'category', data: Array.from({ length: 40 }, (_, i) => 'x' + i) } };
if (chartHeight(big) !== 520) fail('排名图封顶');
if (chartHeight({ xAxis: { type: 'category', data: ['a'] }, series: [{ type: 'bar' }] }) !== 230) fail('普通柱图');
if (chartHeight({ xAxis: { data: [] }, dataZoom: [{}], series: [{ type: 'line' }] }) !== 260) fail('带滑块');
if (chartHeight(null) !== 220) fail('空');
console.log('OK');
