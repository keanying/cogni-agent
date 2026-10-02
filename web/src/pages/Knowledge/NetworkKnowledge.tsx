import React, { useEffect, useState } from 'react';
import { Globe, Plus, Trash2, Loader2, CheckCircle2, XCircle, KeyRound, Power } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { Badge } from '../../components/ui/badge';
import { FieldLabel, Tutorial } from '../../components/common/FormBits';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { confirmDialog } from '../../components/ui/confirm';

/**
 * 网络知识:搜索引擎配置。
 *
 * 密钥全部在页面维护(Fernet 加密存库、接口不回显明文),不用再登服务器改 .env 重启。
 */

const PROVIDERS = [
  { id: 'bochaai', label: '博查 AI 搜索', needsBase: false,
    hint: '国内可直连,返回结果带摘要。到 open.bochaai.com 申请 API Key。' },
  { id: 'bing', label: 'Bing Web Search', needsBase: false,
    hint: 'Azure 认知服务的 Bing 搜索,需要 Azure 订阅。国内访问可能需要代理。' },
  { id: 'searxng', label: 'SearXNG(自建)', needsBase: true,
    hint: '自建元搜索引擎,不需要 API Key,但**必须填接口地址**(如 http://10.0.0.5:8080)。' },
];

const NetworkKnowledge: React.FC = () => {
  const [items, setItems] = useState<api.WebSearchConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [editing, setEditing] = useState<api.WebSearchConfig | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: '', provider: 'bochaai', api_key: '', base_url: '', cache_ttl_seconds: 21600, active: true });

  const load = async () => {
    setLoading(true);
    try { setItems((await api.listWebSearchConfigs()).items); }
    catch (e: any) { toast.error(e.message || '加载失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const openNew = () => {
    setEditing(null);
    setForm({ name: '', provider: 'bochaai', api_key: '', base_url: '', cache_ttl_seconds: 21600, active: true });
    setShowForm(true);
  };
  const openEdit = (c: api.WebSearchConfig) => {
    setEditing(c);
    // api_key 留空:后端不回显明文,留空表示「不修改密钥」
    setForm({ name: c.name, provider: c.provider, api_key: '', base_url: c.base_url || '',
              cache_ttl_seconds: c.cache_ttl_seconds, active: c.active });
    setShowForm(true);
  };

  const save = async () => {
    const meta = PROVIDERS.find((p) => p.id === form.provider);
    if (meta?.needsBase && !form.base_url.trim()) {
      toast.error(`${meta.label} 必须填写接口地址`); return;
    }
    if (!editing && !meta?.needsBase && !form.api_key.trim()) {
      toast.error('请填写 API Key'); return;
    }
    setSaving(true);
    try {
      const payload = { ...form, api_key: form.api_key.trim() || null, base_url: form.base_url.trim() || null };
      if (editing) await api.updateWebSearchConfig(editing.id, payload);
      else await api.createWebSearchConfig(payload);
      toast.success(editing ? '已保存' : '已添加');
      setShowForm(false);
      load();
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSaving(false); }
  };

  const test = async (c: api.WebSearchConfig) => {
    setTesting(c.id);
    try {
      const r = await api.testWebSearchConfig(c.id);
      if (r.ok) toast.success(`连通正常,返回 ${r.count} 条结果`);
      else toast.error(r.hint || '没有返回结果');
    } catch (e: any) { toast.error(e.message || '测试失败'); }
    finally { setTesting(null); }
  };

  const remove = async (c: api.WebSearchConfig) => {
    if (!await confirmDialog({ title: `删除搜索引擎配置「${c.name}」`,
      message: '删除后走网络知识的检索将改用其余已启用的引擎;都没有时检索会返回空。',
      confirmLabel: '删除', danger: true })) return;
    try { await api.deleteWebSearchConfig(c.id); load(); }
    catch (e: any) { toast.error(e.message || '删除失败'); }
  };

  const meta = PROVIDERS.find((p) => p.id === form.provider);

  return (
    <div className="mx-auto max-w-5xl space-y-5 pb-12">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-bold text-slate-900">
            <Globe size={20} className="text-blue-500" />网络知识
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            配置搜索引擎。Skill 启用「websearch」知识源后,会通过这里配置的引擎检索实时信息。
          </p>
        </div>
        <Button onClick={openNew} className="gap-1.5 bg-[#1677ff] hover:bg-[#4096ff]">
          <Plus size={15} />添加搜索引擎
        </Button>
      </div>

      <Card className="border-slate-200 shadow-sm">
        <CardContent className="p-0">
          {loading ? (
            <div className="flex justify-center py-16"><Loader2 className="animate-spin text-slate-300" size={24} /></div>
          ) : items.length === 0 ? (
            <div className="py-16 text-center">
              <Globe size={32} className="mx-auto mb-3 text-slate-200" />
              <p className="text-sm text-slate-400">还没有配置搜索引擎</p>
              <p className="mt-1 text-xs text-slate-300">配置后,Skill 才能使用「网络知识」检索实时信息</p>
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="border-b border-slate-100 bg-slate-50/60 text-[11px] uppercase tracking-wider text-slate-400">
                <tr>
                  <th className="px-5 py-3 text-left font-semibold">名称</th>
                  <th className="px-3 py-3 text-left font-semibold">引擎</th>
                  <th className="px-3 py-3 text-left font-semibold">密钥</th>
                  <th className="px-3 py-3 text-center font-semibold">缓存</th>
                  <th className="px-3 py-3 text-center font-semibold">状态</th>
                  <th className="px-5 py-3 text-right font-semibold">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {items.map((c) => (
                  <tr key={c.id} className="hover:bg-slate-50/50">
                    <td className="px-5 py-3">
                      <button onClick={() => openEdit(c)} className="font-semibold text-slate-800 hover:text-blue-600">{c.name}</button>
                      {c.base_url && <div className="mt-0.5 font-mono text-[10px] text-slate-400">{c.base_url}</div>}
                    </td>
                    <td className="px-3 py-3 text-slate-500">
                      {PROVIDERS.find((p) => p.id === c.provider)?.label || c.provider}
                    </td>
                    <td className="px-3 py-3">
                      {c.has_api_key
                        ? <span className="inline-flex items-center gap-1 text-[11px] text-emerald-600"><KeyRound size={11} />已配置</span>
                        : <span className="text-[11px] text-slate-300">未配置</span>}
                    </td>
                    <td className="px-3 py-3 text-center text-[11px] text-slate-400">
                      {Math.round(c.cache_ttl_seconds / 3600)} 小时
                    </td>
                    <td className="px-3 py-3 text-center">
                      <Badge variant="outline" className={cn('text-[10px] font-bold',
                        c.active ? 'border-emerald-100 bg-emerald-50 text-emerald-600' : 'bg-slate-100 text-slate-400')}>
                        {c.active ? '启用中' : '已停用'}
                      </Badge>
                    </td>
                    <td className="px-5 py-3 text-right">
                      <Button variant="ghost" size="sm" className="h-8 text-[11px]" disabled={testing === c.id}
                              onClick={() => test(c)}>
                        {testing === c.id ? <Loader2 size={12} className="animate-spin" /> : '测试'}
                      </Button>
                      <Button variant="ghost" size="icon" className="h-8 w-8 text-slate-300 hover:text-rose-500"
                              onClick={() => remove(c)}><Trash2 size={14} /></Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Tutorial items={[
        { title: '1. 密钥存在哪', body: '页面填写的 API Key 以 Fernet 加密存库,接口永不回显明文,前端和大模型都拿不到。改完立即生效,不用重启服务。' },
        { title: '2. 与 .env 的关系', body: '.env 里的 WEBSEARCH_API_KEY 只是兜底——数据库里有启用中的配置时优先用数据库的。推荐全部在页面维护,多实例部署时不用每台机器都改文件。' },
        { title: '3. 域名白名单', body: '只有命中白名单的检索结果才会进入模型上下文。没有配置白名单时不做过滤,建议生产环境务必配置,避免把不可信站点的内容喂给模型。' },
      ]} />

      {showForm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={() => setShowForm(false)}>
          <div className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <h3 className="mb-4 text-base font-bold text-slate-900">{editing ? '编辑搜索引擎' : '添加搜索引擎'}</h3>
            <div className="space-y-4">
              <div>
                <FieldLabel label="名称" required hint="仅用于在列表里区分,可随意起名" />
                <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="如:博查生产" />
              </div>
              <div>
                <FieldLabel label="搜索引擎" required hint={meta?.hint} />
                <select value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })}
                        className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none focus:border-blue-400">
                  {PROVIDERS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                </select>
                {meta && <p className="mt-1.5 text-[11px] leading-relaxed text-slate-400">{meta.hint}</p>}
              </div>
              {meta?.needsBase && (
                <div>
                  <FieldLabel label="接口地址" required hint="自建 SearXNG 实例的地址" />
                  <Input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                         placeholder="http://10.0.0.5:8080" className="font-mono text-sm" />
                </div>
              )}
              <div>
                <FieldLabel label="API Key" required={!editing && !meta?.needsBase}
                  hint="加密存储,保存后不再回显。编辑时留空表示不修改。" />
                <Input type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                       placeholder={editing ? '留空则不修改' : (meta?.needsBase ? '自建实例通常不需要' : '必填')} />
              </div>
              <div>
                <FieldLabel label="结果缓存时长" hint="相同查询在此时间内直接用缓存,省调用次数也更快。设 0 表示不缓存。" />
                <div className="flex items-center gap-2">
                  <Input type="number" value={Math.round(form.cache_ttl_seconds / 3600)}
                         onChange={(e) => setForm({ ...form, cache_ttl_seconds: Number(e.target.value) * 3600 })}
                         className="w-24" />
                  <span className="text-sm text-slate-500">小时</span>
                </div>
              </div>
              <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-600">
                <input type="checkbox" checked={form.active} onChange={(e) => setForm({ ...form, active: e.target.checked })}
                       className="h-4 w-4 rounded border-slate-300 text-blue-600" />
                <Power size={13} className="text-slate-400" />启用(同时只有一个启用中的配置会被使用)
              </label>
            </div>
            <div className="mt-6 flex gap-3">
              <Button variant="outline" className="flex-1" onClick={() => setShowForm(false)}>取消</Button>
              <Button className="flex-1 bg-[#1677ff]" disabled={saving} onClick={save}>
                {saving ? <Loader2 size={15} className="animate-spin" /> : '保存'}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default NetworkKnowledge;
