import React, { useEffect, useState } from 'react';
import { Info, KeyRound } from 'lucide-react';
import { toast } from 'sonner';
import { useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import ParamValuesEditor from '../../components/common/ParamValuesEditor';
import { MULTI_LABEL } from '../../components/system/SystemParamsPanel';
import * as api from '../../lib/api';

/**
 * 景区配置 → 接口参数(0090 sys_param_value)。
 *
 * 参数在「系统设置 · 参数配置」里定义;这里按本景区覆盖取值 —— 一个景区可以有多个值
 * (多个 OTA 商户账号),各带备注。不覆盖 = 用系统默认值。
 * 敏感参数看不到原值:不动就保持原值,填了新值才覆盖。
 */
interface Row extends api.ScenicParamView {
  _override: boolean;          // 本景区是否覆盖
  _values: api.ParamValue[];   // 编辑中的覆盖值
  _dirty: boolean;
}

const toRow = (p: api.ScenicParamView): Row => ({
  ...p, _override: p.overridden, _values: p.is_secret ? [] : p.values, _dirty: false,
});

const ScenicParamsCard: React.FC<{ scenicId: string }> = ({ scenicId }) => {
  const navigate = useNavigate();
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    try { setRows((await api.getScenicParams(scenicId)).items.map(toRow)); }
    catch (e: any) { toast.error(e.message || '加载接口参数失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, [scenicId]);

  const patch = (i: number, p: Partial<Row>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...p, _dirty: true } : r)));

  const save = async () => {
    const items: api.ScenicParamOverride[] = rows.filter((r) => r._dirty).map((r) => {
      const vals = r._values.filter((v) => v.value.trim());
      if (!r._override) return { param_key: r.param_key, values: null };
      // 敏感参数:已覆盖、这次没填新值 → 保持原值
      if (r.is_secret && r.overridden && vals.length === 0) return { param_key: r.param_key, values: null, keep_secret: true };
      return { param_key: r.param_key, values: vals };
    });
    if (!items.length) { toast.message('没有改动'); return; }
    setSaving(true);
    try {
      setRows((await api.saveScenicParams(scenicId, items)).items.map(toRow));
      toast.success('已保存,下一次工具调用即生效');
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSaving(false); }
  };

  return (
    <Card className="scroll-mt-6 border-slate-200 shadow-sm">
      <CardHeader className="border-b bg-slate-50/50 py-3 px-6">
        <CardTitle className="flex items-center justify-between text-sm font-black text-slate-700">
          <span className="flex items-center gap-2">
            <KeyRound size={14} className="text-slate-400" />接口参数
            <Badge className="border-none bg-slate-100 text-[10px] font-bold text-slate-500">
              {rows.filter((r) => r.overridden).length} / {rows.length} 项已覆盖
            </Badge>
          </span>
          <button onClick={() => navigate('/system/settings?tab=params')}
            className="text-[11px] font-bold text-blue-600 hover:text-blue-700">管理参数定义 →</button>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 p-6">
        {loading ? <div className="text-[12px] text-slate-400">加载中…</div> : rows.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 p-4 text-center text-[12px] text-slate-400">
            还没有定义任何系统参数。先到「系统设置 · 参数配置」里添加(如 OTA 商户账号),再回这里填本景区的值。
          </div>
        ) : rows.map((r, i) => (
          <div key={r.param_key} className="rounded-xl border border-slate-100 p-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[12px] font-bold text-slate-700">
                  {r.label} <span className="ml-1 font-mono text-[11px] font-normal text-blue-600">{r.ref}</span>
                  {!r.enabled && <Badge className="ml-2 border-none bg-slate-100 text-[10px] text-slate-500">已停用</Badge>}
                </div>
                <div className="mt-0.5 text-[11px] text-slate-400">{MULTI_LABEL[r.multi_mode]}{r.description ? ` · ${r.description}` : ''}</div>
              </div>
              <div className="flex shrink-0 overflow-hidden rounded-lg border border-slate-200 text-[11px] font-bold">
                <button onClick={() => patch(i, { _override: false })}
                  className={`px-2.5 py-1 ${!r._override ? 'bg-slate-700 text-white' : 'text-slate-500'}`}>用系统默认</button>
                <button onClick={() => patch(i, { _override: true })}
                  className={`px-2.5 py-1 ${r._override ? 'bg-blue-600 text-white' : 'text-slate-500'}`}>本景区单独配</button>
              </div>
            </div>
            <div className="mt-2">
              {r._override ? (
                <ParamValuesEditor values={r._values} multi={r.multi_mode !== 'single'} secret={r.is_secret}
                  placeholder={r.is_secret && r.overridden ? `已配置 ${r.values.map((v) => v.value).join('、')},留空不改` : '本景区的值'}
                  onChange={(v) => patch(i, { _values: v })} />
              ) : (
                <div className="font-mono text-[11px] text-slate-500">
                  系统默认:{r.default_values.length
                    ? r.default_values.map((v) => v.value + (v.label ? `(${v.label})` : '')).join('、')
                    : <span className="font-sans text-slate-300">无 —— 本景区不配就没有值,引用它的工具会报缺参数</span>}
                </div>
              )}
            </div>
          </div>
        ))}

        {rows.length > 0 && (
          <button onClick={save} disabled={saving}
            className="rounded-xl bg-blue-600 px-4 py-2 text-[12px] font-bold text-white shadow-sm transition-all hover:bg-blue-700 disabled:opacity-50">
            {saving ? '保存中…' : '保存本景区取值'}
          </button>
        )}

        <div className="rounded-xl border border-blue-100 bg-blue-50/60 p-3 text-[11px] leading-relaxed text-slate-600">
          <div className="mb-1 flex items-center gap-1 font-bold text-blue-700"><Info size={12} />怎么生效</div>
          本景区的工具调用、Skill 提示词里的 <code className="font-mono">{'{{参数}}'}</code> 按这里的值替换;
          没单独配的用系统默认值。多值参数按定义里的方式用:每个值各调用一次再合并,或作为列表 / 逗号拼接一次传入。
        </div>
      </CardContent>
    </Card>
  );
};

export default ScenicParamsCard;
