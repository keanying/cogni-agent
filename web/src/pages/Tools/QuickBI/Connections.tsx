import React, { useEffect, useState } from 'react';
import {
  CheckCircle2, ClipboardPaste, KeyRound, Loader2, Plus, Plug, RefreshCw, Settings2, Star, Trash2, XCircle, AlertTriangle,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '../../../components/ui/button';
import { Badge } from '../../../components/ui/badge';
import { Modal, ConfirmModal, CopyId } from '../../../components/ui/PageHelpers';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import { inputCls, labelCls, selectCls, ScenicSelect } from './shared';
import { canDeleteRow, canEditRow, useCapabilities, whyDisabled } from '../../../lib/capabilities';

const STATUS: Record<string, { label: string; cls: string; icon: any }> = {
  ok: { label: '连接正常', cls: 'text-emerald-600', icon: CheckCircle2 },
  failed: { label: '连接失败', cls: 'text-rose-600', icon: XCircle },
  pending: { label: '未检测', cls: 'text-slate-400', icon: Plug },
  env: { label: '环境变量', cls: 'text-blue-500', icon: KeyRound },
};

const EMPTY = { name: '', server_domain: 'https://bi.aliyun.com', api_key: '', api_secret: '', user_token: '',
  use_trial: false, default_locale: 'zh_CN', is_default: false, api_enabled: true, visibility: 'group',
  scenic_id: '' };


/**
 * 删除连接的三步流程:**先看账 → 转移 → 再删**。
 *
 * 为什么不能只放一个「确定删除吗」:连接 id 在各张表里是裸字符串,没有外键级联。
 * 直接删掉,指着它的资产/问题原样留着,不报错也不失效 —— 直到有人去点,
 * 才炸一句「连接不存在:<一串 id>」,而那串 id 已经查不到了。
 * 现场删掉一条连接之后,24 个仪表板资产全成了死引用,谁都没察觉。
 *
 * 这里有两个不同的「转移给谁」,都做了,但要分清:
 *   - **接手的连接**(必选):资产绑的是一套 Quick BI 凭证(user_token)。能不能打开
 *     取决于那套凭证有没有这个仪表板的权限 —— 所以真正决定「还能不能用」的是这一项。
 *   - **归属人**(可选):谁负责这批资产。纯记账,不影响能不能打开。
 * 只改归属人不改连接的话,资产照样打不开 —— 所以连接那一项是必填的。
 */
const DeleteConnectionFlow: React.FC<{
  conn: api.QbiConnection; onClose: () => void; onDone: () => void;
}> = ({ conn, onClose, onDone }) => {
  const [plan, setPlan] = useState<api.QbiTransferPlan | null>(null);
  const [target, setTarget] = useState('');
  const [step, setStep] = useState<'loading' | 'transfer' | 'confirm'>('loading');
  const [busy, setBusy] = useState(false);
  const [moved, setMoved] = useState<string>('');
  const [owner, setOwner] = useState('');
  const [people, setPeople] = useState<any[]>([]);

  useEffect(() => {
    // 归属人是可选项:取不到成员列表(权限不够等)就不显示这一栏,不挡着主流程
    api.listUsers({ page_size: 200 })
      .then((r: any) => setPeople(r?.items || r || []))
      .catch(() => setPeople([]));
  }, []);

  useEffect(() => {
    api.qbiConnectionTransferPlan(conn.id)
      .then((p) => { setPlan(p); setStep(p.needs_transfer ? 'transfer' : 'confirm'); })
      // 账算不出来时不能默认「那就直接删吧」—— 那正是现在这个故障的来路
      .catch((e: any) => { toast.error(e.message || '读取不到这条连接名下的内容'); onClose(); });
  }, [conn.id]);

  const picked = plan?.targets.find((t) => t.id === target) || null;
  const assetCount = plan?.transferable['数据资产'] || 0;

  const doTransfer = async () => {
    if (!target) { toast.error('先选一个转移目标'); return; }
    setBusy(true);
    try {
      const r = await api.qbiTransferConnection(conn.id, {
        to_connection_id: target, owner_user_id: owner || undefined });
      setMoved(r.message);
      toast.success(r.message);
      setStep('confirm');
    } catch (e: any) { toast.error(e.message || '转移失败'); }
    finally { setBusy(false); }
  };

  const doDelete = async () => {
    setBusy(true);
    try {
      const r = await api.qbiDeleteConnection(conn.id);
      toast.success(r.issues_cleared ? `已删除(${r.issues_cleared} 条问题已解绑)` : '已删除');
      onDone();
    } catch (e: any) { toast.error(e.message || '删除失败'); }
    finally { setBusy(false); }
  };

  if (step === 'loading') {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
        <div className="rounded-2xl bg-white px-8 py-6 shadow-2xl flex items-center gap-3 text-sm text-slate-500">
          <Loader2 size={16} className="animate-spin" />正在查这条连接名下有什么…
        </div>
      </div>
    );
  }

  if (step === 'transfer' && plan) {
    const summary = Object.entries(plan.transferable).map(([k, v]) => `${k} ${v} 条`).join('、');
    return (
      <Modal title={`「${conn.name}」名下还有内容,需要先处理`} onClose={onClose} wide>
        <div className="space-y-4 text-sm">
          <div className="rounded-xl border border-amber-100 bg-amber-50 p-3 text-amber-800 text-xs leading-relaxed">
            <div className="flex gap-2">
              <AlertTriangle size={14} className="shrink-0 mt-0.5" />
              <div>
                名下有 <b>{summary}</b>。直接删除的话,它们会指向一条已不存在的连接 ——
                不报错、也不失效,直到有人点开才发现打不开。
              </div>
            </div>
          </div>

          {plan.assets.length > 0 && (
            <div>
              <div className={labelCls}>要转移的数据资产({plan.assets.length})</div>
              <div className="mt-1 max-h-40 overflow-y-auto rounded-xl border border-slate-200 divide-y divide-slate-50">
                {plan.assets.map((a) => (
                  <div key={a.id} className="px-3 py-1.5 text-xs text-slate-600 flex justify-between gap-2">
                    <span className="truncate">{a.name}</span>
                    <span className="text-slate-300 shrink-0">{a.kind}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div>
            <div className={labelCls}>转移到哪条连接 <span className="text-rose-500">*</span></div>
            <select className={selectCls} value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">请选择接手的连接…</option>
              {plan.targets.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}{t.scenic_id ? `（${t.scenic_id}）` : '（集团级）'}
                  {t.can_inherit ? ' · 资产可继承' : ' · 不同工作空间,资产将停用'}
                </option>
              ))}
            </select>
            {picked && (
              <div className={cn('mt-2 rounded-xl border p-3 text-xs leading-relaxed',
                picked.can_inherit ? 'border-emerald-100 bg-emerald-50 text-emerald-800'
                                   : 'border-amber-100 bg-amber-50 text-amber-800')}>
                {picked.can_inherit ? (
                  <>选「{picked.name}」:与原连接是<b>同一个 Quick BI 工作空间</b>,
                  {assetCount} 个数据资产<b>整体继承</b>过去,继续可用。</>
                ) : (
                  <>选「{picked.name}」:<b>不是同一个 Quick BI 工作空间</b>。
                  资产存的是那边的仪表板/数据集 id,换一套凭证指不到任何东西 ——
                  所以这 {assetCount} 个资产会被<b>停用</b>(不是删除:列表里仍看得到、写明原因,
                  以后接回可用的连接还能启用)。</>
                )}
                {(plan.references['上传文件'] || 0) > 0 && (
                  <> 上传文件（{plan.references['上传文件']} 个）一律停用 ——
                  它的远端 fileId 直接绑在原凭证上,换连接继承不了,需要重新上传。</>
                )}
              </div>
            )}
            <p className="mt-1.5 text-[11px] text-slate-400 leading-relaxed">
              问题 / 主题解读只存了一个连接指针,任何情况下都能转。
              会话与运行记录不动:那是「当时用哪条连接跑的」,改了就不是史实了。
            </p>
          </div>

          {people.length > 0 && (
            <div>
              <div className={labelCls}>归属人（可选）</div>
              <select className={selectCls} value={owner} onChange={(e) => setOwner(e.target.value)}>
                <option value="">不改，保持原来的归属人</option>
                {people.map((u: any) => (
                  <option key={u.id} value={u.id}>{u.name}{u.account ? `（${u.account}）` : ''}</option>
                ))}
              </select>
              <p className="mt-1.5 text-[11px] text-slate-400">
                只是记「这批资产归谁管」,不影响能不能打开 —— 那由上面那条连接决定。
              </p>
            </div>
          )}

          {Object.keys(plan.history).length > 0 && (
            <p className="text-[11px] text-slate-400">
              历史留痕不转移:{Object.entries(plan.history).map(([k, v]) => `${k} ${v} 条`).join('、')}
            </p>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="outline" onClick={onClose} disabled={busy}>取消</Button>
            <Button className="bg-[#2563eb] hover:bg-[#1d4ed8]" onClick={doTransfer} disabled={busy || !target}>
              {busy && <Loader2 size={14} className="animate-spin mr-1" />}
              {picked && !picked.can_inherit ? '转移并停用,然后继续' : '转移并继续'}
            </Button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <ConfirmModal
      title="删除连接" danger confirmLabel="确认删除" loading={busy}
      message={(moved ? `${moved}。` : '这条连接名下没有需要转移的内容。')
        + `确定删除「${conn.name}」?删除后这套凭证不可恢复,需要重新填写 API Key / Token。`}
      onCancel={onClose}
      onConfirm={doDelete}
    />
  );
};

const Connections: React.FC<{ items: api.QbiConnection[]; loading: boolean; reload: () => void; overview: api.QbiOverview | null }> = (
  { items, loading, reload, overview }) => {
  const [editing, setEditing] = useState<api.QbiConnection | null | 'new'>(null);
  const [form, setForm] = useState<Record<string, any>>(EMPTY);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [pasteName, setPasteName] = useState('');
  // 粘贴建连接时就把景区选了 —— 建完再进编辑补一次,是最容易忘的一步
  const [pasteScenic, setPasteScenic] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<api.QbiConnection | null>(null);
  // 卡片上显示景区名而不是一串 PFTSC… 编号:按当前用到的景区 ID 去主数据取一次名字
  const [scenicNames, setScenicNames] = useState<Record<string, string>>({});
  const scenicName = (id: string) => scenicNames[id] || id;
  // 增删改的判定统一来自后端,按钮可点性 = 后端会放行的范围
  const cap = useCapabilities();
  useEffect(() => {
    const ids = Array.from(new Set(items.map((c) => c.scenic_id).filter(Boolean))) as string[];
    const missing = ids.filter((id) => !scenicNames[id]);
    if (!missing.length) return;
    Promise.all(missing.map((id) => api.getScenicCatalogDetail(id)
      .then((r) => [id, r.scenic_name] as const).catch(() => [id, id] as const)))
      .then((pairs) => setScenicNames((prev) => ({ ...prev, ...Object.fromEntries(pairs) })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  // 景区角色新建连接只能挂在自己景区下,预填上,免得存的时候才被打回来
  const openNew = () => {
    setForm({ ...EMPTY, scenic_id: cap.manage_all || cap.is_group ? '' : (cap.scope_scenic_id || '') });
    setEditing('new');
  };
  const openEdit = (c: api.QbiConnection) => {
    setForm({ name: c.name, server_domain: c.server_domain, api_key: '', api_secret: '', user_token: '',
      use_trial: c.use_trial, default_locale: c.default_locale, is_default: c.is_default,
      api_enabled: c.api_enabled, visibility: c.visibility, scenic_id: c.scenic_id || '' });
    setEditing(c);
  };

  const save = async () => {
    setBusy('save');
    try {
      const payload: Record<string, any> = { ...form };
      if (editing !== 'new') {
        // 机密字段留空 = 不修改
        ['api_key', 'api_secret', 'user_token'].forEach((k) => { if (!payload[k]) delete payload[k]; });
      }
      const saved = editing === 'new' ? await api.qbiCreateConnection(payload) : await api.qbiUpdateConnection((editing as api.QbiConnection).id, payload);
      toast.success('已保存,正在检测连通性…');
      setEditing(null);
      reload();
      check(saved.id);
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setBusy(null); }
  };

  const paste = async () => {
    setBusy('paste');
    try {
      const r = await api.qbiPasteConnection(pasteText, pasteName || undefined, undefined,
                                             pasteScenic || undefined);
      toast.success(`已识别 ${r.parsed_keys.join(' / ')},连接已保存`);
      setPasteOpen(false); setPasteText(''); setPasteScenic('');
      reload();
      check(r.id);
    } catch (e: any) { toast.error(e.message || '解析失败'); }
    finally { setBusy(null); }
  };

  const check = async (id: string) => {
    setBusy(`check:${id}`);
    try {
      const r = await api.qbiCheckConnection(id);
      (r.ok ? toast.success : toast.error)(`${r.message}(${r.elapsed_ms}ms)`);
      reload();
    } catch (e: any) { toast.error(e.message || '检测失败'); }
    finally { setBusy(null); }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-slate-500">
          在 Quick BI 控制台右上角头像 →「一键复制 skill 配置」,粘贴到这里即可。密钥与 user_token 加密存储,不回显。
        </p>
        <div className="flex gap-2">
          <Button variant="outline" onClick={reload} className="gap-1.5"><RefreshCw size={14} />刷新</Button>
          <Button variant="outline" onClick={() => {
            // 粘贴建连接走的也是新建,景区角色一样只能落在自己景区
            setPasteScenic(cap.manage_all || cap.is_group ? '' : (cap.scope_scenic_id || ''));
            setPasteOpen(true);
          }} disabled={!cap.can_create}
            title={cap.can_create ? undefined : whyDisabled(cap, cap.scope_scenic_id, '修改')}
            className="gap-1.5"><ClipboardPaste size={14} />粘贴 skill 配置</Button>
          <Button onClick={openNew} disabled={!cap.can_create}
            title={cap.can_create ? undefined : whyDisabled(cap, cap.scope_scenic_id, '修改')}
            className="gap-1.5 bg-[#2563eb] text-white hover:bg-[#1d4ed8]"><Plus size={14} />新建连接</Button>
        </div>
      </div>

      {overview && !overview.trial_configured && items.some((c) => c.use_trial) && (
        <div className="flex items-start gap-2 rounded-xl border border-amber-100 bg-amber-50 px-3 py-2 text-xs text-amber-700">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          有连接勾选了「使用试用凭证」,但服务端未配置 QUICKBI_TRIAL_API_KEY / QUICKBI_TRIAL_API_SECRET,调用会因缺少 api_key 失败。
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-slate-300" /></div>
      ) : items.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-slate-200 py-16 text-center text-sm text-slate-400">
          还没有 Quick BI 连接。点击「粘贴 skill 配置」最快完成接入。
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {items.map((c) => {
            const st = STATUS[c.status] || STATUS.pending;
            return (
              <div key={c.id} className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
                <div className="mb-3 flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="truncate font-bold text-slate-900">{c.name}</span>
                      {c.is_default && <Badge variant="info" className="gap-1"><Star size={10} />默认</Badge>}
                      {c.virtual && <Badge variant="secondary">只读</Badge>}
                      {c.trial_active && <Badge variant="warning">试用凭证</Badge>}
                      {!c.api_enabled && <Badge variant="outline">未开放 API</Badge>}
                    </div>
                    <CopyId id={c.id} className="mt-0.5" />
                  </div>
                  <span className={cn('inline-flex shrink-0 items-center gap-1 text-xs font-semibold', st.cls)}>
                    <st.icon size={13} />{st.label}
                  </span>
                </div>
                <dl className="grid grid-cols-[88px_1fr] gap-y-1 text-xs">
                  <dt className="text-slate-400">server_domain</dt><dd className="truncate font-mono text-slate-700">{c.server_domain}</dd>
                  <dt className="text-slate-400">api_key</dt><dd className="font-mono text-slate-700">{c.api_key || '—'}</dd>
                  <dt className="text-slate-400">api_secret</dt><dd className="font-mono text-slate-700">{c.has_api_secret ? '已配置' : '—'}</dd>
                  <dt className="text-slate-400">user_token</dt><dd className="font-mono text-slate-700">{c.user_token || '—'}</dd>
                  <dt className="text-slate-400">可见范围</dt><dd className="text-slate-700">{c.visibility}</dd>
                  <dt className="text-slate-400">归属景区</dt>
                  <dd className="truncate text-slate-700">{c.scenic_id ? scenicName(c.scenic_id) : '集团级(全景区可见)'}</dd>
                </dl>
                {c.missing.length > 0 && (
                  <p className="mt-2 text-[11px] text-rose-500">缺少:{c.missing.join('、')}</p>
                )}
                {c.status_message && (
                  <p className={cn('mt-2 line-clamp-3 text-[11px]', c.status === 'failed' ? 'text-rose-500' : 'text-slate-400')}>{c.status_message}</p>
                )}
                <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-100 pt-3">
                  <Button size="sm" variant="outline" disabled={busy === `check:${c.id}`} onClick={() => check(c.id)} className="gap-1">
                    {busy === `check:${c.id}` ? <Loader2 size={12} className="animate-spin" /> : <Plug size={12} />}检测连接
                  </Button>
                  {!c.virtual && (
                    <>
                      <Button size="sm" variant="outline" disabled={!canEditRow(cap, c.scenic_id)}
                        title={canEditRow(cap, c.scenic_id) ? undefined : whyDisabled(cap, c.scenic_id, '修改')}
                        onClick={() => openEdit(c)} className="gap-1"><Settings2 size={12} />编辑</Button>
                      {!c.is_default && (
                        <Button size="sm" variant="outline" className="gap-1" disabled={!canEditRow(cap, c.scenic_id)}
                          title={canEditRow(cap, c.scenic_id) ? undefined : whyDisabled(cap, c.scenic_id, '修改')}
                          onClick={async () => { await api.qbiSetDefault(c.id).catch((e) => toast.error(e.message)); reload(); }}>
                          <Star size={12} />设为默认
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" className="gap-1 text-rose-500" disabled={!canDeleteRow(cap, c.scenic_id)}
                        title={canDeleteRow(cap, c.scenic_id) ? undefined : whyDisabled(cap, c.scenic_id, '删除')}
                        onClick={() => setDeleting(c)}><Trash2 size={12} />删除</Button>
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {pasteOpen && (
        <Modal title="粘贴「一键复制 skill 配置」" onClose={() => setPasteOpen(false)} wide
          footer={<>
            <Button variant="outline" onClick={() => setPasteOpen(false)}>取消</Button>
            <Button className="bg-[#2563eb]" disabled={!pasteText.trim() || busy === 'paste'} onClick={paste}>
              {busy === 'paste' && <Loader2 size={14} className="mr-1 animate-spin" />}解析并保存
            </Button>
          </>}>
          <div>
            <label className={labelCls}>连接名称</label>
            <input className={inputCls} value={pasteName} onChange={(e) => setPasteName(e.target.value)} placeholder="如:羚羊 Quick BI" />
          </div>
          <div>
            <label className={labelCls}>归属景区(留空 = 集团级)</label>
            <ScenicSelect value={pasteScenic} onChange={setPasteScenic} registeredOnly
              disabled={!cap.manage_all && !cap.is_group} />
          </div>
          <div className="space-y-1">
            <label className={labelCls}>skill 配置文本</label>
            <textarea className="min-h-[140px] w-full rounded-lg border border-slate-200 p-3 font-mono text-xs outline-none focus:border-blue-400"
              value={pasteText} onChange={(e) => setPasteText(e.target.value)}
              placeholder={'api_key: xxxx\napi_secret: xxxx\nuser_token: xxxx\nserver_domain: https://bi.aliyun.com'} />
            <p className="mt-1 text-[11px] text-slate-400">api_key / api_secret 为空时自动启用「试用凭证」(需服务端配置 QUICKBI_TRIAL_*)。</p>
          </div>
        </Modal>
      )}

      {editing && (
        <Modal title={editing === 'new' ? '新建 Quick BI 连接' : `编辑「${(editing as api.QbiConnection).name}」`} onClose={() => setEditing(null)} wide
          footer={<>
            <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
            <Button className="bg-[#2563eb]" disabled={busy === 'save' || !form.name} onClick={save}>
              {busy === 'save' && <Loader2 size={14} className="mr-1 animate-spin" />}保存
            </Button>
          </>}>
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2"><label className={labelCls}>名称 *</label>
              <input className={inputCls} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
            <div className="col-span-2"><label className={labelCls}>server_domain</label>
              <input className={inputCls} value={form.server_domain} onChange={(e) => setForm({ ...form, server_domain: e.target.value })} />
              {editing !== 'new' && <p className="mt-1 text-[11px] text-amber-600">修改服务地址时需同时重新填写 api_secret 与 user_token,否则会被清空。</p>}</div>
            <div><label className={labelCls}>api_key</label>
              <input className={inputCls} value={form.api_key} placeholder={editing !== 'new' ? '留空不修改' : ''}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })} /></div>
            <div><label className={labelCls}>api_secret</label>
              <input className={inputCls} type="password" value={form.api_secret} placeholder={editing !== 'new' ? '留空不修改' : ''}
                onChange={(e) => setForm({ ...form, api_secret: e.target.value })} /></div>
            <div className="col-span-2"><label className={labelCls}>user_token(决定可访问的数据范围)</label>
              <input className={inputCls} type="password" value={form.user_token} placeholder={editing !== 'new' ? '留空不修改' : ''}
                onChange={(e) => setForm({ ...form, user_token: e.target.value })} /></div>
            <div><label className={labelCls}>默认语言</label>
              <select className={selectCls} value={form.default_locale} onChange={(e) => setForm({ ...form, default_locale: e.target.value })}>
                <option value="zh_CN">简体中文 zh_CN</option><option value="en_US">English en_US</option>
              </select></div>
            <div><label className={labelCls}>可见范围</label>
              <select className={selectCls} value={form.visibility} onChange={(e) => setForm({ ...form, visibility: e.target.value })}>
                <option value="private">private 仅自己</option><option value="scenic">scenic 本景区</option>
                <option value="group">group 集团内</option><option value="public">public 全平台</option>
              </select></div>
            <div className="col-span-2"><label className={labelCls}>归属景区(可见范围为 scenic 时按它判定)</label>
              {/* 从景区主数据里挑,而不是手填 ID:绑错了的表现只是「那个景区看不到这个连接」,
                  没有报错、也没人会往「景区 ID 敲错一位」上想 */}
              <ScenicSelect value={form.scenic_id || ''} registeredOnly disabled={!cap.manage_all && !cap.is_group}
                onChange={(v) => setForm({ ...form, scenic_id: v })} />
              {!cap.manage_all && !cap.is_group && (
                <p className="mt-1 text-[11px] text-slate-400">景区角色只能维护本景区({cap.scope_scenic_id || '未绑定'})的连接</p>
              )}
            </div>
            <label className="flex items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={form.use_trial}
              onChange={(e) => setForm({ ...form, use_trial: e.target.checked })} />api_key 为空时使用试用凭证</label>
            <label className="flex items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={form.api_enabled}
              onChange={(e) => setForm({ ...form, api_enabled: e.target.checked })} />允许开放平台 /v1/quickbi 调用(private 连接除外)</label>
            <label className="flex items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={form.is_default}
              onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />设为默认连接</label>
          </div>
        </Modal>
      )}

      {deleting && (
        <DeleteConnectionFlow
          conn={deleting}
          onClose={() => setDeleting(null)}
          onDone={() => { setDeleting(null); reload(); }}
        />
      )}
    </div>
  );
};

export default Connections;
