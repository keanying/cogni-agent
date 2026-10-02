import React, { useEffect, useState } from 'react';
import { Plus, Pencil, Trash2, Info, Braces, Loader2, X } from 'lucide-react';
import { toast } from 'sonner';
import { Card, CardContent } from '../ui/card';
import { Badge } from '../ui/badge';
import { Input } from '../ui/input';
import { Modal } from '../ui/PageHelpers';
import { confirmDialog } from '../ui/confirm';
import * as api from '../../lib/api';
import { toRows, fromRows, type Scenic, type ValueRow } from './paramRows';

/**
 * 系统设置 · 参数配置(0090 sys_param_def)。
 *
 * 参数在这里定义一次:标识(Tool / MCP / Skill 里写 {{标识}} 引用)、说明、单值或多值、
 * 多值怎么用、系统默认值、自动填入的工具入参名。各景区在「APP 运营 · 景区配置 · 接口参数」里覆盖取值,
 * 没覆盖就用这里的默认值。
 */
export const MULTI_LABEL: Record<api.ParamMultiMode, string> = {
  single: '单值',
  fanout: '多值 · 每个值各调用一次',
  list: '多值 · 作为列表一次传入',
  join: '多值 · 逗号拼接一次传入',
};

interface Form extends Omit<api.SysParamSave, 'default_values'> {
  param_key: string; _isNew: boolean; _argsText: string;
  _rows: ValueRow[] | null;    // null = 还在加载
}

const emptyForm = (): Form => ({
  param_key: '', _isNew: true, label: '', description: '', multi_mode: 'single',
  arg_names: [], _argsText: '', fill_mode: 'fill', is_secret: false, enabled: true,
  _rows: [{ value: '', label: '', scenics: [], isDefault: false }],
});

const SystemParamsPanel: React.FC = () => {
  const [items, setItems] = useState<api.SysParamDef[] | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [saving, setSaving] = useState(false);

  const load = () => api.listParams().then((r) => setItems(r.items)).catch((e) => { toast.error(e.message || '加载失败'); setItems([]); });
  useEffect(() => { load(); }, []);

  const edit = (d: api.SysParamDef) => {
    setForm({
      param_key: d.param_key, _isNew: false, label: d.label, description: d.description, multi_mode: d.multi_mode,
      arg_names: d.arg_names, _argsText: d.arg_names.join(', '), fill_mode: d.fill_mode, is_secret: d.is_secret,
      enabled: d.enabled, _rows: null,
    });
    api.listParamAssignments(d.param_key)
      .then((r) => setForm((f) => (f && f.param_key === d.param_key ? { ...f, _rows: toRows(d, r.items) } : f)))
      .catch((e) => toast.error(e.message || '加载景区分配失败'));
  };

  const save = async () => {
    if (!form || !form._rows) return;
    const key = form.param_key.trim();
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(key)) { toast.error('参数标识:小写字母开头,只能用小写字母、数字、下划线'); return; }
    const out = fromRows(form);
    if (out.error) { toast.error(out.error); return; }
    setSaving(true);
    try {
      await api.saveParam(key, {
        label: form.label, description: form.description, multi_mode: form.multi_mode,
        default_values: out.defaults ?? null,
        arg_names: form._argsText.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean),
        fill_mode: form.fill_mode, is_secret: form.is_secret, enabled: form.enabled,
      });
      await api.saveParamAssignments(key, out.assigns || []);
      toast.success(`已保存 {{${key}}}`);
      setForm(null);
      load();
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSaving(false); }
  };

  const remove = async (d: api.SysParamDef) => {
    if (!await confirmDialog({ title: '删除参数', message: `删除「${d.label}」(${d.ref})?各景区的取值会一起删掉;引用了它的工具再执行会报缺参数。` })) return;
    try { await api.deleteParam(d.param_key); toast.success('已删除'); load(); }
    catch (e: any) { toast.error(e.message || '删除失败'); }
  };

  const preset = () => setForm({
    ...emptyForm(), param_key: 'ota_account', label: 'OTA 商户账号', multi_mode: 'fanout',
    arg_names: ['account'], _argsText: 'account',
    description: 'OTA 直销接口按商户账号取数(携程 vbk_ 开头)。一个景区有多个账号时逐个查询再合并。找运营 / 景区要。',
  });

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3 rounded-xl border border-blue-100 bg-blue-50/50 p-4">
        <Info size={16} className="mt-0.5 shrink-0 text-blue-500" />
        <div className="text-xs leading-relaxed text-blue-800">
          在这里<b>定义</b>参数,各景区在「APP 运营 · 景区配置 · 接口参数」里<b>覆盖取值</b>(没覆盖就用这里的默认值)。
          编辑 Tool / MCP / Skill 时写 <code className="font-mono">{'{{参数标识}}'}</code> 引用:
          地址 / 请求头 / SQL / 提示词里直接替换;工具参数定义里把某个参数的 default 写成引用,该参数就由系统填、模型看不到;
          填了「自动填入入参名」的,工具入参名一致时模型没填也会自动补上。
        </div>
      </div>

      <Card className="border-slate-200 shadow-sm">
        <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50/60 px-5 py-3">
          <span className="flex items-center gap-2 text-sm font-bold text-slate-700"><Braces size={15} />系统参数</span>
          <span className="flex items-center gap-2">
            {!(items || []).some((d) => d.param_key === 'ota_account') && (
              <button onClick={preset} className="rounded-lg border border-slate-200 px-2.5 py-1 text-[11px] font-bold text-slate-500 hover:bg-slate-50">+ OTA 商户账号</button>
            )}
            <button onClick={() => setForm(emptyForm())}
              className="flex items-center gap-1 rounded-lg bg-blue-600 px-2.5 py-1 text-[11px] font-bold text-white hover:bg-blue-700">
              <Plus size={12} />新增参数
            </button>
          </span>
        </div>
        <CardContent className="p-0">
          {items === null ? <div className="flex justify-center py-10"><Loader2 className="animate-spin text-slate-300" /></div>
            : items.length === 0 ? <div className="py-10 text-center text-xs text-slate-400">还没有参数。点右上角「+ OTA 商户账号」快速添加一个。</div>
            : (
              <table className="w-full text-[12px]">
                <thead className="bg-slate-50/40 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                  <tr>
                    <th className="px-5 py-2 text-left">参数</th><th className="px-3 py-2 text-left">引用</th>
                    <th className="px-3 py-2 text-left">取值方式</th><th className="px-3 py-2 text-left">取值 → 分配景区</th>
                    <th className="px-3 py-2 text-left">自动填入</th><th className="px-3 py-2 text-left">景区数</th><th />
                  </tr>
                </thead>
                <tbody>
                  {items.map((d) => (
                    <tr key={d.param_key} className="border-t border-slate-100 align-top">
                      <td className="px-5 py-3">
                        <div className="font-bold text-slate-700">{d.label}
                          {!d.enabled && <Badge className="ml-2 border-none bg-slate-100 text-[10px] text-slate-500">停用</Badge>}
                          {d.is_secret && <Badge className="ml-2 border-none bg-amber-50 text-[10px] text-amber-600">敏感</Badge>}
                        </div>
                        <div className="mt-0.5 max-w-xs text-[11px] text-slate-400">{d.description}</div>
                      </td>
                      <td className="px-3 py-3 font-mono text-[11px] text-blue-600">{d.ref}</td>
                      <td className="px-3 py-3 text-slate-600">{MULTI_LABEL[d.multi_mode]}</td>
                      <td className="px-3 py-3 font-mono text-[11px] text-slate-600">
                        {(d.value_groups || []).length ? (d.value_groups || []).map((v, i) => (
                          <div key={i} className="mb-0.5">
                            {v.value}{v.label && <span className="ml-1 font-sans text-slate-400">({v.label})</span>}
                            <span className="ml-1 font-sans text-slate-500">→ {[...v.scenics, ...(v.is_default ? ['默认'] : [])].join('、') || '未分配'}</span>
                          </div>
                        )) : <span className="font-sans text-slate-300">还没有取值</span>}
                      </td>
                      <td className="px-3 py-3 text-[11px] text-slate-500">
                        {d.arg_names.length ? <>{d.arg_names.join(' / ')}<div className="text-slate-400">{d.fill_mode === 'force' ? '强制覆盖' : '模型没填才填'}</div></> : '-'}
                      </td>
                      <td className="px-3 py-3 text-slate-600">{d.scenic_overrides || 0} 个景区</td>
                      <td className="px-3 py-3 text-right">
                        <button onClick={() => edit(d)} className="mr-2 text-slate-400 hover:text-blue-600" title="编辑"><Pencil size={14} /></button>
                        <button onClick={() => remove(d)} className="text-slate-300 hover:text-red-500" title="删除"><Trash2 size={14} /></button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
        </CardContent>
      </Card>

      {form && (
        <Modal wide title={form._isNew ? '新增系统参数' : `编辑 {{${form.param_key}}}`} onClose={() => setForm(null)}
          footer={<>
            <button onClick={() => setForm(null)} className="rounded-xl border border-slate-200 px-4 py-2 text-[12px] font-bold text-slate-500">取消</button>
            <button onClick={save} disabled={saving} className="rounded-xl bg-blue-600 px-4 py-2 text-[12px] font-bold text-white disabled:opacity-50">{saving ? '保存中…' : '保存'}</button>
          </>}>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-[12px] font-bold text-slate-600">参数标识</label>
              <Input value={form.param_key} disabled={!form._isNew} className="h-9 font-mono text-[12px]" placeholder="ota_account"
                onChange={(e) => setForm({ ...form, param_key: e.target.value })} />
              <div className="mt-1 text-[10px] text-slate-400">引用写法:<span className="font-mono">{`{{${form.param_key || '标识'}}}`}</span>;创建后不能改</div>
            </div>
            <div>
              <label className="mb-1 block text-[12px] font-bold text-slate-600">显示名</label>
              <Input value={form.label} className="h-9 text-[12px]" placeholder="OTA 商户账号" onChange={(e) => setForm({ ...form, label: e.target.value })} />
            </div>
          </div>
          <div>
            <label className="mb-1 block text-[12px] font-bold text-slate-600">说明</label>
            <Input value={form.description} className="h-9 text-[12px]" placeholder="干什么用、值找谁要、哪些工具用"
              onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
          <div>
            <label className="mb-1 block text-[12px] font-bold text-slate-600">取值方式</label>
            <div className="grid grid-cols-2 gap-2">
              {(Object.keys(MULTI_LABEL) as api.ParamMultiMode[]).map((m) => (
                <button key={m} type="button" onClick={() => setForm({ ...form, multi_mode: m })}
                  className={`rounded-xl border px-3 py-2 text-left ${form.multi_mode === m ? 'border-blue-400 bg-blue-50' : 'border-slate-200 hover:bg-slate-50'}`}>
                  <div className="text-[12px] font-bold text-slate-700">{MULTI_LABEL[m]}</div>
                </button>
              ))}
            </div>
          </div>
          <ValueRowsEditor form={form} onChange={(rows) => setForm({ ...form, _rows: rows })} />
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-[12px] font-bold text-slate-600">自动填入的工具入参名</label>
              <Input value={form._argsText} className="h-9 font-mono text-[12px]" placeholder="account, otaAccount"
                onChange={(e) => setForm({ ...form, _argsText: e.target.value })} />
              <div className="mt-1 text-[10px] text-slate-400">工具入参名一致时,模型没填也会自动补上;留空 = 只能用 {'{{…}}'} 引用</div>
            </div>
            <div>
              <label className="mb-1 block text-[12px] font-bold text-slate-600">模型填了值时</label>
              <select value={form.fill_mode} onChange={(e) => setForm({ ...form, fill_mode: e.target.value as 'fill' | 'force' })}
                className="h-9 w-full rounded-lg border border-slate-200 px-2 text-[12px]">
                <option value="fill">尊重模型填的</option>
                <option value="force">一律用配置值</option>
              </select>
            </div>
          </div>
          <div className="flex items-center gap-5 text-[12px] text-slate-600">
            <label className="flex items-center gap-1.5"><input type="checkbox" checked={form.is_secret} onChange={(e) => setForm({ ...form, is_secret: e.target.checked })} />敏感(不写进提示词,只显示末四位)</label>
            <label className="flex items-center gap-1.5"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />启用</label>
          </div>
        </Modal>
      )}
    </div>
  );
};

/** 景区搜索多选:已选的是标签,输入框搜索添加 */
const ScenicMultiPicker: React.FC<{ value: Scenic[]; onChange: (v: Scenic[]) => void }> = ({ value, onChange }) => {
  const [kw, setKw] = useState('');
  const [hits, setHits] = useState<api.ScenicConfigItem[]>([]);
  useEffect(() => {
    if (!kw.trim()) { setHits([]); return; }
    const t = setTimeout(() => api.listScenicConfigs(kw.trim()).then((r) => setHits(r.items.slice(0, 8))).catch(() => setHits([])), 250);
    return () => clearTimeout(t);
  }, [kw]);
  return (
    <div className="relative flex min-h-8 flex-wrap items-center gap-1 rounded-lg border border-slate-200 px-1.5 py-1">
      {value.map((s) => (
        <span key={s.id} className="flex items-center gap-1 rounded-md bg-blue-50 px-1.5 py-0.5 text-[11px] text-blue-700">
          {s.name}
          <button type="button" className="text-blue-300 hover:text-red-500" onClick={() => onChange(value.filter((x) => x.id !== s.id))}><X size={10} /></button>
        </span>
      ))}
      <input value={kw} onChange={(e) => setKw(e.target.value)} placeholder={value.length ? '+ 景区' : '搜索景区名称或 ID'}
        className="min-w-[90px] flex-1 bg-transparent px-1 text-[11px] outline-none" />
      {hits.length > 0 && (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 rounded-xl border border-slate-200 bg-white p-1 shadow-lg">
          {hits.map((h) => (
            <button key={h.scenic_id} type="button"
              onClick={() => { if (!value.some((x) => x.id === h.scenic_id)) onChange([...value, { id: h.scenic_id, name: h.scenic_name || h.scenic_id }]); setKw(''); setHits([]); }}
              className="flex w-full items-center justify-between rounded-lg px-3 py-1.5 text-left text-[12px] hover:bg-blue-50">
              <span className="text-slate-700">{h.scenic_name || h.scenic_id}</span>
              <span className="font-mono text-[10px] text-slate-400">{h.scenic_id}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

/**
 * 取值:每个值一行 —— 值 + 备注 + 分配到哪些景区。
 * 同一个 {{参数}} 景区 A、景区 B 各取各的;执行时只按本次任务所属景区取,景区之间互不可见。
 * 勾「默认」= 没分配到任何值的景区用它(商户账号这类按景区区分的值不要勾)。
 */
const ValueRowsEditor: React.FC<{ form: Form; onChange: (rows: ValueRow[]) => void }> = ({ form, onChange }) => {
  const rows = form._rows;
  if (rows === null) return <div className="text-[12px] text-slate-400">加载取值…</div>;
  const set = (i: number, p: Partial<ValueRow>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...p } : r)));
  return (
    <div>
      <label className="mb-1 block text-[12px] font-bold text-slate-600">取值与分配景区</label>
      <div className="space-y-2">
        <div className="grid grid-cols-12 gap-2 px-1 text-[10px] font-bold text-slate-400">
          <div className="col-span-3">值</div><div className="col-span-2">备注</div><div className="col-span-6">分配景区</div><div className="col-span-1 text-center">默认</div>
        </div>
        {rows.map((r, i) => (
          <div key={i} className="grid grid-cols-12 items-start gap-2">
            <Input className="col-span-3 h-8 font-mono text-[11px]" value={r.value} type={form.is_secret ? 'password' : 'text'}
              placeholder={r._masked ? `${r._masked}(留空不改)` : '取值'} onChange={(e) => set(i, { value: e.target.value })} />
            <Input className="col-span-2 h-8 text-[11px]" value={r.label} placeholder="如 携程主账号" onChange={(e) => set(i, { label: e.target.value })} />
            <div className="col-span-6"><ScenicMultiPicker value={r.scenics} onChange={(v) => set(i, { scenics: v })} /></div>
            <div className="col-span-1 flex h-8 items-center justify-center gap-2">
              <input type="checkbox" checked={r.isDefault} title="没分配到值的景区用它" onChange={(e) => set(i, { isDefault: e.target.checked })} />
              {rows.length > 1 && (
                <button type="button" className="text-slate-300 hover:text-red-500" title="删除这个值"
                  onClick={() => onChange(rows.filter((_, j) => j !== i))}><Trash2 size={13} /></button>
              )}
            </div>
          </div>
        ))}
        {(
          <button type="button" onClick={() => onChange([...rows, { value: '', label: '', scenics: [], isDefault: false }])}
            className="flex items-center gap-1 text-[11px] font-bold text-blue-600 hover:text-blue-700"><Plus size={12} />再加一个值</button>
        )}
      </div>
    </div>
  );
};

export default SystemParamsPanel;
