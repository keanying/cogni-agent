
import React, { useState } from 'react';
import { ArrowLeft, Zap, Layout, Layers, Rocket, Loader2 } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import { MODE_LABELS } from '../../lib/modes';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import * as api from '../../lib/api';

const SkillNew: React.FC = () => {
  const [mode, setMode] = useState<'L0' | 'L1' | 'L2'>('L1');
  const [name, setName] = useState('');
  // 能力一句话概述(面向意图识别路由器的能力清单)。留空时运行时回退 system_prompt 前120字
  const [description, setDescription] = useState('');
  const [prompt, setPrompt] = useState('');
  const [visibility, setVisibility] = useState('private');
  const [saving, setSaving] = useState(false);
  const navigate = useNavigate();

  const handleSave = async () => {
    if (!name.trim()) { toast.error('请填写技能名称'); return; }
    setSaving(true);
    try {
      const created = await api.createSkill({ name, description, suggested_mode: mode, visibility, prompt: prompt || `你是一个专注于「${name}」的智能助手。` });
      toast.success('Skill 已创建，请继续配置 Prompt 与工具绑定');
      navigate(`/tools/skill/edit?id=${created.id}`);
    } catch (e: any) {
      toast.error(e.message || '创建失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center gap-4 mb-2">
        <Button variant="ghost" size="icon" onClick={() => navigate('/tools/skill')} className="rounded-full">
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">新建 Skill</h1>
          <p className="text-slate-500 text-sm">定义业务技能，包含 Prompt 模板和工具集合。</p>
        </div>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardContent className="p-6 space-y-6">
          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-700">技能名称</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：游客投诉自动处理" className="h-11 rounded-xl" />
          </div>

          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-700">能力描述（一句话概述，供意图识别路由器识别）</label>
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="如：舆情监控与分析,定时抓取全网舆情并生成分析报告(留空自动用提示词前120字)"
              className="w-full min-h-[60px] p-4 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 transition-all resize-none text-sm" />
          </div>

          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-700">初始 Prompt（可稍后在编辑页调整）</label>
            <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="描述这个技能应该如何行动"
              className="w-full min-h-[100px] p-4 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 transition-all resize-none text-sm" />
          </div>

          <div className="space-y-4">
            <label className="text-sm font-semibold text-slate-700">可见范围</label>
            <div className="grid grid-cols-2 gap-3">
              {[{ id: 'public', name: '全平台' }, { id: 'group', name: '集团级' }, { id: 'scenic', name: '景区级' }, { id: 'private', name: '私有' }].map((item) => (
                <div key={item.id} onClick={() => setVisibility(item.id)}
                  className={cn('flex items-center gap-3 p-3 rounded-xl border cursor-pointer', visibility === item.id ? 'border-blue-300 bg-blue-50' : 'bg-slate-50/50 border-slate-200')}>
                  <input type="radio" checked={visibility === item.id} onChange={() => setVisibility(item.id)} className="h-4 w-4 text-blue-600" />
                  <label className="text-sm font-bold text-slate-700 cursor-pointer">{item.name}</label>
                </div>
              ))}
            </div>
          </div>

          <div className="space-y-4">
            <label className="text-sm font-semibold text-slate-700">运行模式建议值</label>
            <div className="grid grid-cols-1 gap-4">
              {[
                // 名称与说明都取 lib/modes:这层映射散在各页面里,改一次要找七八处,
                // 漏掉的那处就是「同一个模式在两个页面叫不同名字」
                { id: 'L0', name: MODE_LABELS.L0.label, icon: Zap, desc: MODE_LABELS.L0.desc },
                { id: 'L1', name: MODE_LABELS.L1.label, icon: Layout, desc: MODE_LABELS.L1.desc },
                { id: 'L2', name: MODE_LABELS.L2.label, icon: Layers, desc: MODE_LABELS.L2.desc },
              ].map((item) => (
                <div key={item.id} onClick={() => setMode(item.id as any)}
                  className={cn('flex items-center gap-4 p-4 rounded-xl border-2 cursor-pointer transition-all', mode === item.id ? 'border-blue-600 bg-blue-50' : 'border-slate-100 hover:border-slate-200 bg-white')}>
                  <div className={cn('h-12 w-12 rounded-xl flex items-center justify-center shrink-0', mode === item.id ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-400')}>
                    <item.icon size={24} />
                  </div>
                  <div className="flex-1">
                    <div className="text-sm font-bold">{item.name}</div>
                    <div className="text-xs text-slate-500 mt-0.5">{item.desc}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="flex gap-4">
        <Button variant="ghost" className="flex-1 h-12 rounded-xl font-bold text-slate-500" onClick={() => navigate('/tools/skill')}>取消</Button>
        <Button disabled={saving} className="flex-1 h-12 rounded-xl bg-blue-600 hover:bg-blue-700 text-white font-bold shadow-lg shadow-blue-100 gap-2" onClick={handleSave}>
          {saving ? <Loader2 size={18} className="animate-spin" /> : <Rocket size={18} />} 保存并进入编辑
        </Button>
      </div>
    </div>
  );
};

export default SkillNew;
