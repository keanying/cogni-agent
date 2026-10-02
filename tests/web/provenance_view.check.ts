// 思考过程「版本溯源」只列实际调用到的工具 —— 直接跑真模块
import { splitProvenanceTools, usedToolKeys } from '../../web/src/lib/provenanceView';

const fail = (m: string) => { console.error('FAIL', m); process.exit(1); };
const tools = {
  t1: { name: '雷达图指标', source: 'pinned_snapshot', version: 2 },
  t2: { name: '客群-财富消费画像', source: 'pinned_snapshot', version: 1 },
  t3: { name: '票务-产品报表汇总', source: 'latest_published', version: 5 },
};
// 没有任何调用:全部归「未用到」
let r = splitProvenanceTools(tools, usedToolKeys([]));
if (r.used.length !== 0 || r.unused.length !== 3) fail('无调用时应 0/3');
// 按展示名命中;tool_disabled 不算用到;其它步骤忽略
const steps = [
  { step: 'resolve_provenance', data: { provenance: {} } },
  { step: 'tool_call', data: { tool_name: 'ticket_report', tool_display_name: '票务-产品报表汇总' } },
  { step: 'tool_disabled', data: { tool_name: 'x', tool_display_name: '雷达图指标' } },
];
r = splitProvenanceTools(tools, usedToolKeys(steps));
if (r.used.map((x) => x.id).join() !== 't3') fail('只应命中 t3: ' + JSON.stringify(r.used));
if (r.unused.map((x) => x.id).join() !== 't1,t2') fail('未用到应为 t1,t2 且保持顺序');
// 按工具 ID 命中;跳过的重复调用也算用到
r = splitProvenanceTools(tools, usedToolKeys([{ step: 'tool_call_skipped', data: { tool_id: 't2' } }]));
if (r.used.map((x) => x.id).join() !== 't2') fail('ID 命中');
if (splitProvenanceTools(undefined, new Set()).unused.length !== 0) fail('空工具');
console.log('OK');
