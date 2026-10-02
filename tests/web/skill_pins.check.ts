// 由 tests/test_skill_edit_page.py 用 tsx 跑:直接测 web 里的真实模块,不是抄一份逻辑。
import { fillLatestPins, latestPublished } from '../../web/src/pages/Tools/skillPins';

const eq = (a: unknown, b: unknown, msg: string) => {
  if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(`FAIL ${msg}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`); process.exit(1); }
};
// 倒序列表里排第一的是已下线的 v3 —— 不能钉它
const opts = {
  a: [{ version: 3, version_id: 'x3', status: 'offline' }, { version: 2, version_id: 'x2', status: 'published' },
      { version: 1, version_id: 'x1', status: 'published' }, { version: null, version_id: 'xd', status: 'draft' }],
  b: [{ version: null, version_id: 'yd', status: 'draft' }],   // 从没发布过
  c: [{ version: 5, version_id: 'z5', status: 'published' }],
};
eq(latestPublished(opts.a), 2, '跳过已下线,取最新已发布');
eq(latestPublished(opts.b), null, '没有已发布版本');
eq(latestPublished(undefined), null, '没有候选');
const r = fillLatestPins([{ tool_id: 'a', version: null }, { tool_id: 'b', version: null },
                          { tool_id: 'c', version: 4 }, { tool_id: 'zz', version: null }], opts);
eq(r.pins, [{ tool_id: 'a', version: 2 }, { tool_id: 'b', version: null },
            { tool_id: 'c', version: 4 }, { tool_id: 'zz', version: null }], '只填未钉版的;已钉的不动');
eq(r.filled, 1, '填了几个');
console.log('OK');
