
import React, { useEffect, useState } from 'react';
import { ArrowLeft, Beaker, Loader2, Plus, X, Info } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { Badge } from '../../components/ui/badge';
import { cn } from '../../lib/utils';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import * as api from '../../lib/api';

const EvaluationNew: React.FC = () => {
  const navigate = useNavigate();
  const [targetType, setTargetType] = useState<'agent' | 'skill'>('agent');
  const [targets, setTargets] = useState<any[]>([]);
  const [targetId, setTargetId] = useState('');
  const [name, setName] = useState('');
  const [testInput, setTestInput] = useState('');
  const [keywords, setKeywords] = useState<string[]>([]);
  const [kwInput, setKwInput] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const fn = targetType === 'agent' ? api.listAgents : api.listSkills;
    fn().then((r) => { setTargets(r.items); setTargetId(''); }).catch(() => setTargets([]));
  }, [targetType]);

  const addKeyword = () => {
    const v = kwInput.trim();
    if (!v || keywords.includes(v)) return;
    setKeywords([...keywords, v]);
    setKwInput('');
  };

  const handleSave = async () => {
    if (!name.trim()) { toast.error('请填写用例名称'); return; }
    if (!targetId) { toast.error('请选择评测目标'); return; }
    if (!testInput.trim()) { toast.error('请填写测试输入'); return; }
    setSaving(true);
    try {
      await api.createEvalCase({ name, target_type: targetType, target_id: targetId, test_input: testInput, expected_keywords: keywords });
      toast.success('评测用例已创建');
      navigate('/audit/evaluation');
    } catch (e: any) {
      toast.error(e.message || '创建失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex items-center gap-4 mb-2">
        <Button variant="ghost" size="icon" onClick={() => navigate('/audit/evaluation')} className="rounded-full"><ArrowLeft className="h-5 w-5" /></Button>
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">新建评测用例</h1>
          <p className="text-slate-500 text-sm">用例将真实调用执行引擎运行，按关键词命中判定是否通过。</p>
        </div>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardHeader className="border-b bg-slate-50/50 py-3 px-6">
          <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-500"><Beaker size={16} /> 用例定义</CardTitle>
        </CardHeader>
        <CardContent className="p-6 space-y-5">
          <div className="space-y-2">
            <label className="form-label">用例名称</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：询问天游峰游览路线" className="h-11 rounded-xl" />
          </div>

          <div className="space-y-2">
            <label className="form-label">评测目标类型</label>
            <div className="flex gap-2">
              {(['agent', 'skill'] as const).map((t) => (
                <button key={t} onClick={() => setTargetType(t)}
                  className={cn('flex-1 py-2.5 rounded-xl border text-sm font-bold transition-all uppercase',
                    targetType === t ? 'border-blue-200 bg-blue-50 text-blue-600' : 'border-slate-200 text-slate-500 hover:bg-slate-50')}>
                  {t}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            <label className="form-label">评测目标</label>
            <select value={targetId} onChange={(e) => setTargetId(e.target.value)} className="w-full h-11 px-4 rounded-xl border border-slate-200 bg-white text-sm outline-none focus:border-blue-500">
              <option value="">请选择...</option>
              {targets.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
            {targets.length === 0 && <p className="text-[11px] text-amber-600">当前没有可选的 {targetType}，请先到「工具中心」创建。</p>}
          </div>

          <div className="space-y-2">
            <label className="form-label">测试输入</label>
            <textarea value={testInput} onChange={(e) => setTestInput(e.target.value)} rows={3} placeholder="模拟用户会发送的消息内容..."
              className="w-full p-4 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 transition-all resize-none text-sm" />
          </div>

          <div className="space-y-2">
            <label className="form-label">期望关键词</label>
            <div className="flex gap-2">
              <Input value={kwInput} onChange={(e) => setKwInput(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addKeyword(); } }}
                placeholder="输入关键词后回车添加" className="h-10 rounded-xl" />
              <Button variant="outline" onClick={addKeyword} className="h-10 px-4 gap-1"><Plus size={14} />添加</Button>
            </div>
            <div className="flex flex-wrap gap-2 pt-1">
              {keywords.map((kw) => (
                <Badge key={kw} className="bg-emerald-50 text-emerald-700 border-emerald-100 px-2.5 py-1 rounded-lg flex items-center gap-1.5 cursor-pointer hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => setKeywords(keywords.filter((k) => k !== kw))}>
                  {kw}<X size={11} />
                </Badge>
              ))}
              {keywords.length === 0 && <span className="text-[11px] text-slate-400">未设置关键词时，只要任务执行成功即判定通过</span>}
            </div>
          </div>

          <div className="bg-slate-50 border border-slate-100 rounded-xl p-3.5 flex items-start gap-2.5">
            <Info size={14} className="text-slate-400 shrink-0 mt-0.5" />
            <p className="text-[11px] text-slate-500 leading-relaxed">
              判定规则：任务状态为 COMPLETED，且回答中<span className="font-bold">命中任意一个</span>期望关键词，即视为通过。
              采用确定性规则而非 LLM 打分，避免评测本身引入新的不确定性。
            </p>
          </div>
        </CardContent>
      </Card>

      <div className="flex gap-4 pb-8">
        <Button variant="ghost" className="flex-1 h-12 rounded-xl font-bold text-slate-500" onClick={() => navigate('/audit/evaluation')}>取消</Button>
        <Button disabled={saving} className="flex-1 h-12 rounded-xl bg-blue-600 hover:bg-blue-700 text-white font-bold shadow-lg shadow-blue-100 gap-2" onClick={handleSave}>
          {saving ? <Loader2 size={17} className="animate-spin" /> : <Beaker size={17} />}创建用例
        </Button>
      </div>
    </div>
  );
};

export default EvaluationNew;
