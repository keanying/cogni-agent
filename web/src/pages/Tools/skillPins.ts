/** Skill 编辑页 · 工具版本钉选的纯逻辑(无 React 依赖,便于单测)。 */
import type { PinOptionVersion } from '../../lib/api';

/** 工具绑定:tool_id + 版本 pin。15 号起 version=null 不再表示「跟随默认」,
 *  而是「尚未钉版」:保存草稿允许,但发布时会被预检拦截(需显式钉已发布版本)。 */
export interface ToolPin { tool_id: string; version: number | null; }

/** 版本列表里最新的**已发布**版本号。
 *  不能直接取第一个:列表按版本号倒序,排在最前的可能是已下线的版本 —— 钉上去发布预检会拦。 */
export const latestPublished = (list: PinOptionVersion[] | undefined): number | null => {
  const pub = (list || []).filter((v) => v.status === 'published' && v.version != null);
  return pub.length ? Math.max(...pub.map((v) => v.version as number)) : null;
};

/** 未钉版的绑定一律默认填最新已发布版本;已经钉了的(哪怕不是最新)不动 —— 升级走「刷新到最新依赖」。 */
export const fillLatestPins = (pins: ToolPin[], options: Record<string, PinOptionVersion[]>) => {
  let filled = 0;
  const next = pins.map((p) => {
    if (p.version != null) return p;
    const v = latestPublished(options[p.tool_id]);
    if (v == null) return p;
    filled += 1;
    return { ...p, version: v };
  });
  return { pins: next, filled };
};

