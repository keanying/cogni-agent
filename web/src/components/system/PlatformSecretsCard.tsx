import React, { useEffect, useState } from 'react';
import { KeyRound, Loader2 } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/card';
import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { Input } from '../ui/input';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { confirmDialog } from '../ui/confirm';

/**
 * 系统设置 · 平台密钥(目前只有数据平台 xpftkey)。
 *
 * 以前只能写在服务器环境变量 DATA_PLATFORM_XPFTKEY 里:要改部署、要重启,运营碰不到;
 * 没配的表现是票务 / 客群 / 舆情数据全部查不了,而「在哪里设置」没有一个页面可以指。
 * 这里填了之后优先于环境变量,最迟 30 秒各处生效,不需要重启。**只写不读**:页面只显示末四位。
 */
const SOURCE_LABEL: Record<string, string> = { admin: '后台配置', env: '环境变量', '': '未配置' };

const PlatformSecretsCard: React.FC = () => {
  const [items, setItems] = useState<api.PlatformSecret[] | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const load = () => api.listPlatformSecrets().then((r) => setItems(r.items)).catch((e) => {
    setItems([]);
    toast.error(e?.message || '加载平台密钥失败');
  });
  useEffect(() => { load(); }, []);

  const save = async (name: string) => {
    const v = (draft[name] || '').trim();
    if (!v) { toast.error('请先填入密钥'); return; }
    setBusy(name);
    try {
      const r = await api.setPlatformSecret(name, v);
      setItems(r.items);
      setDraft((d) => ({ ...d, [name]: '' }));
      toast.success('已保存,最迟 30 秒各处生效');
    } catch (e: any) { toast.error(e?.message || '保存失败'); }
    finally { setBusy(null); }
  };

  const clear = async (s: api.PlatformSecret) => {
    if (!(await confirmDialog({
      title: `清除「${s.label}」?`,
      message: `清除后回退到环境变量 ${s.env}${s.source === 'admin' ? '(如果服务器上配了的话)' : ''};都没有的话,${s.used_by}都会调不通。`,
      confirmLabel: '清除', danger: true,
    }))) return;
    setBusy(s.name);
    try { setItems((await api.clearPlatformSecret(s.name)).items); toast.success('已清除'); }
    catch (e: any) { toast.error(e?.message || '清除失败'); }
    finally { setBusy(null); }
  };

  return (
    <Card className="border-slate-200 shadow-sm overflow-hidden">
      <CardHeader className="border-b bg-slate-50/50 py-4 px-6">
        <CardTitle className="text-sm font-black uppercase tracking-widest text-slate-600 flex items-center gap-2">
          <KeyRound size={15} />平台密钥
        </CardTitle>
      </CardHeader>
      <CardContent className="p-6 space-y-4">
        {items === null && <div className="flex items-center gap-2 text-xs text-slate-400"><Loader2 size={14} className="animate-spin" />加载中…</div>}
        {items?.map((s) => (
          <div key={s.name} className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-bold text-slate-700">{s.label}</span>
              <Badge className={s.configured ? 'bg-emerald-50 text-emerald-600 border-emerald-100' : 'bg-rose-50 text-rose-600 border-rose-100'}>
                {s.configured ? `已配置 · ${SOURCE_LABEL[s.source]} · ${s.masked}` : '未配置'}
              </Badge>
            </div>
            <p className="text-xs text-slate-500 leading-relaxed">
              用于:{s.used_by}。这里填的优先于环境变量 <code className="font-mono">{s.env}</code>,
              加密保存、不会回显,最迟 30 秒生效,不需要重启。
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Input type="password" autoComplete="new-password" value={draft[s.name] || ''}
                onChange={(e) => setDraft((d) => ({ ...d, [s.name]: e.target.value }))}
                placeholder={s.configured ? '填入新值以替换' : '填入数据平台开发提供的 xpftkey'}
                className="h-9 max-w-md rounded-lg font-mono text-xs" />
              <Button size="sm" disabled={busy === s.name} onClick={() => save(s.name)}
                className="h-9 bg-[#2563eb] hover:bg-[#1d4ed8] text-white">
                {busy === s.name ? <Loader2 size={14} className="animate-spin" /> : '保存'}
              </Button>
              {s.source === 'admin' && (
                <Button size="sm" variant="outline" disabled={busy === s.name} onClick={() => clear(s)} className="h-9">清除</Button>
              )}
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
};

export default PlatformSecretsCard;
