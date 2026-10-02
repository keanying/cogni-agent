import React, { useEffect, useRef, useState } from 'react';
import { Mountain, ArrowLeft, Loader2, Info, Users } from 'lucide-react';
import { toast } from 'sonner';
import { useParams, useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import * as api from '../../lib/api';
import ScenicParamsCard from './ScenicParamsCard';

/**
 * APP 运营 → 景区配置:详情编辑页(25 号方案)。
 *
 * 布局:左侧窄列 = 定位导航(锚点跳转,滚动高亮);右侧主区 = 卡片从上往下排
 * ——① 景区基础信息卡(主数据只读,来自 sys_scenics 不落配置表)、
 * ② 日承载量卡(>0 整数;清空 = 删配置行回到未配置态,消费侧维持现状
 * 绝不猜默认值)、③ 接口参数卡(OTA 商户账号等,见 ScenicParamsCard)。说明文案:当前分色规则(本批不接消费,纯说明)。
 */
const SECTIONS = [
  { key: 'basic', label: '景区基础信息' },
  { key: 'capacity', label: '日承载量' },
  { key: 'params', label: '接口参数' },
] as const;

const ScenicConfigDetail: React.FC = () => {
  const { scenicId } = useParams<{ scenicId: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<api.ScenicConfigItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [capacity, setCapacity] = useState('');
  const [active, setActive] = useState<string>('basic');
  const sectionRefs = useRef<Record<string, HTMLDivElement | null>>({});

  useEffect(() => {
    (async () => {
      try {
        const r = await api.getScenicConfig(scenicId!);
        setDetail(r);
        setCapacity(r.daily_capacity ? String(r.daily_capacity) : '');
      } catch (e: any) { toast.error(e.message || '加载景区详情失败'); }
      finally { setLoading(false); }
    })();
  }, [scenicId]);

  // 滚动高亮当前卡片(IntersectionObserver,少卡片场景轻量够用)
  useEffect(() => {
    const ob = new IntersectionObserver(
      (entries) => entries.forEach((en) => {
        if (en.isIntersecting) setActive((en.target as HTMLElement).dataset.key || 'basic');
      }),
      { rootMargin: '-30% 0px -60% 0px' });
    Object.values(sectionRefs.current).forEach((el) => el && ob.observe(el));
    return () => ob.disconnect();
  }, [detail]);

  const jump = (key: string) => {
    const el = sectionRefs.current[key];
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  const save = async (clear = false) => {
    const v = clear ? null : (capacity.trim() === '' ? null : Number(capacity));
    if (!clear && capacity.trim() !== '' && (!Number.isInteger(v) || (v as number) <= 0)) {
      toast.error('日承载量必须是大于 0 的整数');
      return;
    }
    setSaving(true);
    try {
      const r = await api.saveScenicConfig(scenicId!, v);
      setDetail(r);
      setCapacity(r.daily_capacity ? String(r.daily_capacity) : '');
      toast.success(clear || v === null ? '已清空配置' : '保存成功');
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSaving(false); }
  };

  if (loading) {
    return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  }
  if (!detail) {
    return <div className="p-6 text-sm text-slate-400">景区不存在</div>;
  }

  return (
    <div className="p-6">
      <div className="mb-4 flex items-center gap-2">
        <button onClick={() => navigate('/app-ops/scenic-config')}
          className="flex items-center gap-1 text-[12px] font-bold text-slate-400 transition-colors hover:text-blue-600">
          <ArrowLeft size={14} />景区配置
        </button>
        <span className="text-slate-300">/</span>
        <h1 className="flex items-center gap-2 text-lg font-black text-slate-900">
          <Mountain size={18} className="text-slate-500" />
          {detail.scenic_name || detail.scenic_id}
        </h1>
      </div>

      <div className="grid grid-cols-12 gap-4">
        {/* 左:定位导航(窄列) */}
        <div className="col-span-2">
          <div className="sticky top-6 space-y-1">
            <div className="mb-2 px-2 text-[10px] font-black uppercase tracking-widest text-slate-300">本页内容</div>
            {SECTIONS.map((s) => (
              <button key={s.key} onClick={() => jump(s.key)}
                className={cn('block w-full rounded-lg px-3 py-2 text-left text-[12px] font-bold transition-all',
                  active === s.key
                    ? 'bg-blue-50 text-blue-600 shadow-[inset_2px_0_0_0_theme(colors.blue.600)]'
                    : 'text-slate-500 hover:bg-slate-50 hover:text-slate-700')}>
                {s.label}
              </button>
            ))}
          </div>
        </div>

        {/* 右:详情主区,卡片从上往下排 */}
        <div className="col-span-10 space-y-4">
          {/* 卡片一:景区基础信息(主数据只读) */}
          <div ref={(el) => { sectionRefs.current['basic'] = el; }} data-key="basic">
            <Card className="scroll-mt-6 border-slate-200 shadow-sm">
              <CardHeader className="border-b bg-slate-50/50 py-3 px-6">
                <CardTitle className="flex items-center gap-2 text-sm font-black text-slate-700">
                  景区基础信息
                  <span className="text-[10px] font-normal text-slate-400">主数据只读</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="p-6">
                <div className="grid grid-cols-3 gap-x-8 gap-y-4 text-[13px]">
                  <div>
                    <div className="mb-1 text-[11px] text-slate-400">景区 ID</div>
                    <div className="font-mono text-[11px] text-slate-600">{detail.scenic_id}</div>
                  </div>
                  <div>
                    <div className="mb-1 text-[11px] text-slate-400">景区名称</div>
                    <div className="text-slate-700">{detail.scenic_name || '(主数据缺失)'}</div>
                  </div>
                  <div>
                    <div className="mb-1 text-[11px] text-slate-400">等级</div>
                    <div className="text-slate-700">{detail.scenic_grade_level || '-'}</div>
                  </div>
                  <div>
                    <div className="mb-1 text-[11px] text-slate-400">主数据状态</div>
                    <div>{detail.is_enabled
                      ? <Badge className="border-none bg-emerald-100 text-[10px] font-bold text-emerald-700">启用</Badge>
                      : <Badge className="border-none bg-red-50 text-[10px] font-bold text-red-500">停用</Badge>}</div>
                  </div>
                  <div>
                    <div className="mb-1 text-[11px] text-slate-400">绑定用户数</div>
                    <div className="flex items-center gap-1 text-slate-700"><Users size={12} className="text-slate-400" />{detail.bound_users}</div>
                  </div>
                  <div>
                    <div className="mb-1 text-[11px] text-slate-400">主数据同步时间</div>
                    <div className="text-[11px] text-slate-500">
                      {detail.synced_at ? new Date(detail.synced_at).toLocaleString('zh-CN', { hour12: false }) : '-'}
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>

          {/* 卡片二:日承载量 */}
          <div ref={(el) => { sectionRefs.current['capacity'] = el; }} data-key="capacity">
            <Card className="scroll-mt-6 border-slate-200 shadow-sm">
              <CardHeader className="border-b bg-slate-50/50 py-3 px-6">
                <CardTitle className="flex items-center justify-between text-sm font-black text-slate-700">
                  <span className="flex items-center gap-2">
                    日承载量
                    {detail.configured
                      ? <Badge className="border-none bg-emerald-100 text-[10px] font-bold text-emerald-700">已配置</Badge>
                      : <Badge className="border-none bg-slate-100 text-[10px] font-bold text-slate-500">未配置</Badge>}
                  </span>
                  {detail.updated_at && (
                    <span className="text-[11px] font-normal text-slate-400">
                      更新于 {new Date(detail.updated_at).toLocaleString('zh-CN', { hour12: false })}
                    </span>
                  )}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4 p-6">
                <div>
                  <label className="mb-1.5 block text-[12px] font-bold text-slate-600">
                    日最大承载量(人次/天)
                  </label>
                  <div className="flex items-center gap-2">
                    <Input value={capacity} onChange={(e) => setCapacity(e.target.value)}
                      placeholder="未配置" type="number" min={1} step={1}
                      className="h-9 w-56 rounded-xl" />
                    <span className="text-[11px] text-slate-400">大于 0 的整数</span>
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <button onClick={() => save(false)} disabled={saving}
                    className="rounded-xl bg-blue-600 px-4 py-2 text-[12px] font-bold text-white shadow-sm transition-all hover:bg-blue-700 disabled:opacity-50">
                    {saving ? '保存中…' : '保存'}
                  </button>
                  <button onClick={() => save(true)} disabled={saving}
                    className="rounded-xl border border-slate-200 px-4 py-2 text-[12px] font-bold text-slate-500 transition-all hover:bg-slate-50 disabled:opacity-50">
                    清空配置
                  </button>
                </div>

                <div className="rounded-xl border border-blue-100 bg-blue-50/60 p-3 text-[11px] leading-relaxed text-slate-600">
                  <div className="mb-1 flex items-center gap-1 font-bold text-blue-700">
                    <Info size={12} />这个数将来影响什么
                  </div>
                  概览页客流维度分色规则:在园人数 / 最大承载量 ≤75% 正常(蓝)、
                  ≤90% 警戒(橙)、&gt;90% 严重(红)。承载量未配置时该维度仅展示
                  文本、不参与分色(不猜默认值)。参数注入与分色消费为后续批次,
                  当前配置先行管理。
                </div>
              </CardContent>
            </Card>
          </div>

          {/* 卡片三:接口参数(OTA 商户账号等,工具执行时按景区自动填入) */}
          <div ref={(el) => { sectionRefs.current['params'] = el; }} data-key="params">
            <ScenicParamsCard scenicId={scenicId!} />
          </div>
        </div>
      </div>
    </div>
  );
};

export default ScenicConfigDetail;
