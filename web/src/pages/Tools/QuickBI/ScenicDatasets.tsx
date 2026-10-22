import React, { useEffect, useMemo, useState } from 'react';
import { Loader2, Pencil, Plus, RefreshCw, Search, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '../../../components/ui/button';
import { Modal, ConfirmModal } from '../../../components/ui/PageHelpers';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import { ConnectionSelect, ScenicSelect, SearchableSelect, inputCls, labelCls, selectCls } from './shared';

/** 景区数据集映射(tc_qbi_scenic_datasets)的维护页。
 *
 *  一景区一行:用哪条连接 + 哪个数据集取数 + 景区级加速预热开关。它是**显式配置**,所以能被
 *  当默认值用:固定问题没指定目标数据、按某个景区发起时取这里的数据集;问题上绑的连接失效时
 *  优先换成这里的连接。没配映射的景区维持原逻辑(不按名字猜别的景区的资产)。
 *  数据集从连接的实时清单里挑 —— 手填 ID 迟早和 Quick BI 侧对不上。
 */
const ScenicDatasets: React.FC<{ connections: api.QbiConnection[] }> = ({ connections }) => {
  const [items, setItems] = useState<api.QbiScenicDataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [kw, setKw] = useState('');
  const [names, setNames] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<{
    row: api.QbiScenicDataset | null;
    scenic_id: string; connection_id: string; cube_id: string; cube_name: string; prewarm_enabled: boolean;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<api.QbiScenicDataset | null>(null);
  const [switching, setSwitching] = useState('');
  const [cubes, setCubes] = useState<{ cube_id: string; cube_name: string }[] | null>(null);
  const [cubesLoading, setCubesLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try { setItems((await api.qbiListScenicDatasets()).items || []); }
    catch (e: any) { toast.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  // 列表显示景区名而不是一串编号:按用到的景区 ID 拉一次主数据,拉不到就显示 ID
  useEffect(() => {
    const missing = Array.from(new Set(items.map((i) => i.scenic_id))).filter((id) => !(id in names));
    if (!missing.length) return;
    Promise.all(missing.map((id) => api.getScenicCatalogDetail(id).then(
      (r) => [id, r.scenic_name] as const).catch(() => [id, id] as const)))
      .then((pairs) => setNames((m) => ({ ...m, ...Object.fromEntries(pairs) })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);
  const scenicName = (sid: string) => names[sid] || sid;
  const connName = (id: string) => connections.find((c) => c.id === id)?.name || id.slice(0, 8);

  // 景区名 / 连接名 / 数据集名长得像的不少,单字段搜会「明明在列表里却搜不到」,所以一并匹配
  const filtered = useMemo(() => {
    const k = kw.trim().toLowerCase();
    if (!k) return items;
    return items.filter((m) => [scenicName(m.scenic_id), m.scenic_id, m.connection_name || '', m.connection_id,
      m.cube_name, m.cube_id].some((v) => (v || '').toLowerCase().includes(k)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, kw, names]);

  const togglePrewarm = async (m: api.QbiScenicDataset) => {
    setSwitching(m.id);
    try {
      const r = await api.qbiSetScenicDatasetPrewarm(m.scenic_id, !m.prewarm_enabled);
      setItems((ls) => ls.map((x) => (x.id === m.id ? { ...x, ...r, connection_name: x.connection_name } : x)));
    } catch (e: any) { toast.error(e.message); }
    finally { setSwitching(''); }
  };

  // 选连接 → 实时拉这条连接可问数的数据集清单(只查询),再从里面挑一个
  const loadCubes = (connId: string) => {
    setCubes(null);
    if (!connId) return;
    setCubesLoading(true);
    api.qbiListCubes(connId)
      .then((r) => setCubes(r.items || []))
      .catch((e: any) => { toast.error(`拉取数据集清单失败:${e.message}`); setCubes([]); })
      .finally(() => setCubesLoading(false));
  };
  const pickConnection = (connId: string) => {
    if (!editing) return;
    const c = connections.find((x) => x.id === connId);
    // 新增时景区默认取连接的归属景区(可改);编辑时景区固定
    setEditing({ ...editing, connection_id: connId, cube_id: '', cube_name: '',
      scenic_id: editing.row ? editing.row.scenic_id : (editing.scenic_id || c?.scenic_id || '') });
    loadCubes(connId);
  };

  const save = async () => {
    if (!editing) return;
    setSaving(true);
    try {
      const r = await api.qbiUpsertScenicDataset({
        scenic_id: editing.scenic_id, connection_id: editing.connection_id, cube_id: editing.cube_id.trim(),
        cube_name: editing.cube_name.trim() || null, prewarm_enabled: editing.prewarm_enabled,
      });
      toast.success(`已保存:${scenicName(r.scenic_id)} → ${r.cube_name || r.cube_id}`);
      setEditing(null);
      load();
    } catch (e: any) { toast.error(e.message); }
    finally { setSaving(false); }
  };

  const editingConn = connections.find((c) => c.id === editing?.connection_id);
  // 景区与连接归属不一致时提醒(不拦:有的景区就是借集团账号取数)
  const scenicMismatch = !!(editing && editingConn?.scenic_id && editing.scenic_id
    && editingConn.scenic_id !== editing.scenic_id);
  const usable = connections.filter((c) => !c.virtual);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-[280px] flex-1 md:max-w-md">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input className={cn(inputCls, 'pl-8')} placeholder="搜索景区 / 连接 / 数据集"
            value={kw} onChange={(e) => setKw(e.target.value)} />
        </div>
        <Button variant="outline" onClick={load} className="gap-1.5"><RefreshCw size={14} />刷新</Button>
        <Button variant="outline" className="gap-1.5"
          onClick={() => { setEditing({ row: null, scenic_id: '', connection_id: '', cube_id: '', cube_name: '', prewarm_enabled: true }); setCubes(null); }}>
          <Plus size={14} />新增映射
        </Button>
      </div>

      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs text-slate-500">
            <tr>
              <th className="px-3 py-2">景区</th>
              <th className="px-3 py-2">连接</th>
              <th className="px-3 py-2">数据集</th>
              <th className="px-3 py-2" title="景区级加速预热开关:关掉后这个景区的固定问题一条都不预热">加速预热</th>
              <th className="px-3 py-2">更新时间</th>
              <th className="px-3 py-2 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={6} className="py-10 text-center"><Loader2 className="mx-auto animate-spin text-slate-300" /></td></tr>}
            {!loading && filtered.length === 0 && (
              <tr><td colSpan={6} className="py-10 text-center text-xs text-slate-400">
                {kw ? '没有匹配的映射。' : '还没有映射:点「新增映射」为景区指定取数的连接与数据集。'}
              </td></tr>
            )}
            {!loading && filtered.map((m) => (
              <tr key={m.id} className="border-t border-slate-100">
                <td className="px-3 py-2">
                  <div className="font-medium text-slate-800">{scenicName(m.scenic_id)}</div>
                  <div className="font-mono text-[10px] text-slate-400" title={m.scenic_id}>{m.scenic_id}</div>
                </td>
                <td className="px-3 py-2 text-slate-600">{m.connection_name || connName(m.connection_id)}
                  <div className="font-mono text-[10px] text-slate-400">{m.connection_id.slice(0, 8)}</div></td>
                <td className="px-3 py-2 text-slate-700">{m.cube_name || '—'}
                  <div className="font-mono text-[10px] text-slate-400">{m.cube_id}</div></td>
                <td className="px-3 py-2">
                  <button type="button" disabled={switching === m.id}
                    className={cn('rounded-full border px-2.5 py-1 text-[11px] font-medium transition disabled:opacity-50',
                      m.prewarm_enabled
                        ? 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100'
                        : 'border-slate-200 bg-slate-50 text-slate-400 hover:bg-slate-100')}
                    title={m.prewarm_enabled ? '已开:点击关闭 = 这个景区的固定问题不再预热'
                      : '已关:点击开启 = 这个景区恢复预热(仍按每条问题自己的加速设置)'}
                    onClick={() => togglePrewarm(m)}>
                    {switching === m.id ? <Loader2 size={11} className="inline animate-spin" />
                      : m.prewarm_enabled ? '预热开' : '预热关'}
                  </button>
                </td>
                <td className="px-3 py-2 text-xs text-slate-400">{m.updated_at ? new Date(m.updated_at).toLocaleString() : '—'}</td>
                <td className="px-3 py-2">
                  <div className="flex items-center justify-end gap-1">
                    <Button size="sm" variant="ghost" className="gap-1"
                      onClick={() => {
                        setEditing({ row: m, scenic_id: m.scenic_id, connection_id: m.connection_id,
                          prewarm_enabled: m.prewarm_enabled, cube_id: m.cube_id, cube_name: m.cube_name });
                        loadCubes(m.connection_id);
                      }}>
                      <Pencil size={12} />编辑
                    </Button>
                    <Button size="sm" variant="ghost" className="gap-1 text-rose-500"
                      onClick={() => setDeleting(m)}><Trash2 size={12} />删除</Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-[11px] leading-relaxed text-slate-400">
        映射是显式配置:固定问题没指定目标数据、按某个景区发起时,用这里的数据集(只在同一条连接上);
        问题上绑的连接失效时,优先换成这里的连接。没配映射的景区维持原逻辑。
        关掉「预热」= 这个景区的固定问题一条都不预热(每条问题自己的加速设置照旧保留)。
      </p>

      {editing && (
        <Modal title={editing.row ? `编辑映射 · ${scenicName(editing.row.scenic_id)}` : '新增映射'}
          onClose={() => setEditing(null)}
          footer={<>
            <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
            <Button className="gap-1.5 bg-[#2563eb]" onClick={save}
              disabled={saving || !editing.scenic_id || !editing.connection_id || !editing.cube_id.trim()}>
              {saving && <Loader2 size={14} className="animate-spin" />}保存
            </Button>
          </>}>
          <div className="space-y-4 text-sm">
            <div><label className={labelCls}>连接</label>
              <ConnectionSelect items={usable} value={editing.connection_id} onChange={pickConnection} />
              <p className="mt-1 text-[11px] text-slate-400">选连接后实时拉取它可问数的数据集清单;新增时景区默认取连接的归属景区。</p></div>
            <div><label className={labelCls}>景区</label>
              <ScenicSelect value={editing.scenic_id} registeredOnly disabled={!!editing.row}
                placeholder="选择景区" onChange={(v) => setEditing({ ...editing, scenic_id: v })} />
              {scenicMismatch && (
                <p className="mt-1 text-[11px] text-amber-600">
                  这条连接归属景区 {editingConn?.scenic_id},与所选景区不同 —— 确认这个景区确实借用它取数。
                </p>
              )}
              {editing.row && <p className="mt-1 text-[11px] text-slate-400">映射行的景区不可改;要换景区请删除后新增。</p>}</div>
            <div><label className={labelCls}>数据集(从该连接的实时清单里选)</label>
              {cubesLoading
                ? <div className="flex h-10 items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 text-xs text-slate-400"><Loader2 size={13} className="animate-spin" />正在拉取数据集清单…</div>
                : <SearchableSelect className="w-full" value={editing.cube_id}
                    onChange={(v) => setEditing({ ...editing, cube_id: v,
                      cube_name: cubes?.find((c) => c.cube_id === v)?.cube_name || editing.cube_name })}
                    options={(cubes || []).map((c) => ({ value: c.cube_id, label: `${c.cube_name || c.cube_id} · ${c.cube_id.slice(0, 8)}` }))}
                    placeholder={!editing.connection_id ? '先选连接' : cubes && cubes.length === 0 ? '该连接没有可问数的数据集' : '选择数据集(可搜索名称 / ID)'}
                    searchPlaceholder="搜索数据集名称 / ID" />}
              {editing.row && editing.cube_id && !cubes?.some((c) => c.cube_id === editing.cube_id) && !cubesLoading && (
                <p className="mt-1 text-[11px] text-amber-600">当前数据集 {editing.cube_id} 不在这条连接的实时清单里(可能已取消授权)。</p>
              )}</div>
            <div><label className={labelCls}>数据集名称(选填,选定后自动带出,可改)</label>
              <input className={inputCls} placeholder="留空 = 保存时按实时清单补"
                value={editing.cube_name} onChange={(e) => setEditing({ ...editing, cube_name: e.target.value })} /></div>
            <div><label className={labelCls}>景区级加速预热</label>
              <select className={cn(selectCls, 'w-32')} value={editing.prewarm_enabled ? '1' : '0'}
                onChange={(e) => setEditing({ ...editing, prewarm_enabled: e.target.value === '1' })}>
                <option value="1">开启</option><option value="0">关闭</option>
              </select></div>
          </div>
        </Modal>
      )}

      {deleting && (
        <ConfirmModal title="删除映射" danger confirmLabel="删除"
          message={`删除景区「${scenicName(deleting.scenic_id)}」的数据集映射?之后这个景区不再按映射取默认数据,预热开关也随之失效(恢复为允许)。`}
          onCancel={() => setDeleting(null)}
          onConfirm={async () => {
            try { await api.qbiDeleteScenicDataset(deleting.scenic_id); toast.success('已删除'); load(); }
            catch (e: any) { toast.error(e.message); }
            setDeleting(null);
          }} />
      )}
    </div>
  );
};

export default ScenicDatasets;
