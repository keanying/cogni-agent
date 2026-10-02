// 工具中心「查看/修改」弹窗路由的纯逻辑 —— 直接跑真模块
import { fmtUpdatedAt, isModalRoute, modalNavAction } from '../../web/src/lib/modalNavCore';

const fail = (m: string) => { console.error('FAIL', m); process.exit(1); };
for (const p of ['/tools/tool/detail', '/tools/tool/edit', '/tools/mcp/detail', '/tools/mcp/edit',
                 '/tools/skill/detail', '/tools/skill/edit', '/tools/agent/detail', '/tools/agent/edit']) {
  if (!isModalRoute(p)) fail('应以弹窗打开: ' + p);
}
for (const p of ['/tools/tool', '/tools/tool/new', '/tools/agent/distribution', '/tools/quickbi', '/tools/agent/detail/x']) {
  if (isModalRoute(p)) fail('不应以弹窗打开: ' + p);
}
// 详情 → 编辑:留在弹窗
if (modalNavAction('/tools/tool/edit?id=1', '/tools/tool') !== 'stay') fail('详情→编辑应 stay');
// Agent 详情里点技能:也留在弹窗
if (modalNavAction({ pathname: '/tools/skill/detail' }, '/tools/agent') !== 'stay') fail('跨资源详情应 stay');
// 保存后回列表:关闭弹窗
if (modalNavAction('/tools/tool', '/tools/tool') !== 'close') fail('回列表应 close');
if (modalNavAction('/tools/tool?x=1#a', '/tools/tool') !== 'close') fail('带查询串也应 close');
// 去分发中心 / 评测:整页跳
if (modalNavAction('/tools/agent/distribution?id=1', '/tools/agent') !== 'leave') fail('分发应 leave');
if (modalNavAction('/audit/evaluation', '/tools/agent') !== 'leave') fail('评测应 leave');

const now = new Date(2026, 8, 21, 15, 0);
if (fmtUpdatedAt(new Date(2026, 8, 21, 14, 5).toISOString(), now) !== '09-21 14:05') fail('今年内: ' + fmtUpdatedAt(new Date(2026, 8, 21, 14, 5).toISOString(), now));
if (fmtUpdatedAt(new Date(2025, 0, 2, 3, 4).toISOString(), now) !== '2025-01-02 03:04') fail('跨年带年份');
if (fmtUpdatedAt(null, now) !== '—' || fmtUpdatedAt('bad', now) !== '—') fail('无效值');
console.log('OK');
