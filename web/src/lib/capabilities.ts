import { useEffect, useState } from 'react';
import * as api from './api';

/**
 * 当前账号能做什么(角色 × 景区)。
 *
 * 这份判定**来自后端**(GET /admin/auth/capabilities),与后端真正执行的是同一套 ——
 * 前端自己按角色硬编码的话,每加一个角色要改三处页面,而且改漏了是「按钮能点、点了报错」,
 * 比按钮不显示更糟。
 *
 *   manage_all   集团三角色:跨景区增删改
 *   can_create   能新增/修改(集团只读为 false)
 *   can_delete   能删除(景区操作员为 false)
 *   scopeScenic  景区角色的作用范围;集团角色为 null(不受限)
 */
export interface Capabilities {
  role: string;
  role_label: string;
  scenic_id: string | null;
  is_group: boolean;
  manage_all: boolean;
  can_create: boolean;
  can_delete: boolean;
  scope_scenic_id: string | null;
}

const FALLBACK: Capabilities = {
  role: '', role_label: '', scenic_id: null, is_group: false,
  // 取不到时一律按**最小权限**兜底:宁可按钮不显示,也不要让人点了才知道没权限
  manage_all: false, can_create: false, can_delete: false, scope_scenic_id: null,
};

let cached: Capabilities | null = null;

export function useCapabilities(): Capabilities {
  const [cap, setCap] = useState<Capabilities>(cached || FALLBACK);
  useEffect(() => {
    // 有缓存先用缓存(不闪一下空态),但**每次挂载都再拉一次**并按新值刷新。
    //
    // 原先是「拉到一次就再也不拉」。管理员在组织管理里改了某人的角色或归属景区,
    // 后端当场生效了,这个人的界面却还按旧角色显示按钮 —— 要整页刷新才对得上,
    // 而表现是「该能点的点不了 / 不该有的还在」,没人会想到是前端缓存。
    let alive = true;
    api.getCapabilities()
      .then((r) => {
        cached = r;
        if (alive) setCap((prev) => (JSON.stringify(prev) === JSON.stringify(r) ? prev : r));
      })
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  return cap;
}

/** 改完自己的角色/归属后立即作废缓存(下一次挂载会重新拉,不必整页刷新) */
export function invalidateCapabilities(): void {
  cached = null;
}

/** 这条记录(按它的归属景区)当前账号能不能改 */
export function canEditRow(cap: Capabilities, rowScenicId: string | null | undefined): boolean {
  if (!cap.can_create) return false;
  if (cap.manage_all) return true;
  if (cap.is_group) return false;               // 集团只读
  // 景区角色:只能动本景区的;集团级(归属为空)的内容是所有景区共用的,只能用不能改
  return !!rowScenicId && rowScenicId === cap.scope_scenic_id;
}

/** 这条记录当前账号能不能删(在能改的基础上再要求删除权) */
export function canDeleteRow(cap: Capabilities, rowScenicId: string | null | undefined): boolean {
  return cap.can_delete && canEditRow(cap, rowScenicId);
}

/** 按钮为什么不能点 —— 直接当 title 提示用,省得用户去猜 */
export function whyDisabled(cap: Capabilities, rowScenicId: string | null | undefined,
                            action: '修改' | '删除'): string {
  if (!cap.can_create) return `${cap.role_label || '当前角色'}为只读角色,不能${action}`;
  if (action === '删除' && !cap.can_delete) return `景区操作员不能删除,请联系本景区的景区管理员`;
  if (!cap.manage_all && !cap.is_group) {
    if (!rowScenicId) return '这是集团级内容(所有景区共用),景区角色只能使用、不能修改';
    if (rowScenicId !== cap.scope_scenic_id) return `该内容属于景区 ${rowScenicId},当前账号无权操作`;
  }
  return '';
}
