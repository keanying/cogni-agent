// 由 tests/test_console_fixed_question_target.py 用 tsx 跑:直接测 web 里的真实模块。
import { directTarget, fixedTargetValue } from '../../web/src/pages/Tools/QuickBI/fixedTarget';

const eq = (a: unknown, b: unknown, msg: string) => {
  if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(`FAIL ${msg}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`); process.exit(1); }
};
const assets = [
  { id: 'as-set', kind: 'dataset', ref_id: 'cube-ticket' },
  { id: 'as-board', kind: 'dashboard', ref_id: 'page-1' },
];
eq(fixedTargetValue({ cube_id: 'cube-ticket' }, assets), 'asset:as-set', '数据集登记过 → 用资产');
eq(fixedTargetValue({ cube_id: 'cube-other' }, assets), 'cube:cube-other', '没登记 → 直连数据集(现场那条)');
eq(fixedTargetValue({ page_id: 'page-1' }, assets), 'asset:as-board', '仪表板 pageId 登记过');
eq(fixedTargetValue({ dashboard: 'page-9' }, assets), 'page:page-9', '仪表板没登记');
eq(fixedTargetValue({ asset_id: 'x', cube_id: 'cube-ticket' }, assets), 'asset:x', 'asset_id 优先');
eq(fixedTargetValue({ file_id: 'f1' }, assets), 'file:f1', '文件');
eq(fixedTargetValue({}, assets), '', '没绑目标 → 按景区自动关联');
eq(fixedTargetValue(null, []), '', 'null');
eq(directTarget('cube:cube-other'), { cube_id: 'cube-other' }, '直连数据集原样传');
eq(directTarget('page:p'), { page_id: 'p' }, '直连仪表板');
eq(directTarget('asset:a'), undefined, '资产由调用方按列表解析');
eq(directTarget(''), undefined, '空');
console.log('OK');
