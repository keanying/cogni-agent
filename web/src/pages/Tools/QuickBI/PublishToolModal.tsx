import React, { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { Button } from '../../../components/ui/button';
import { Modal } from '../../../components/ui/PageHelpers';
import * as api from '../../../lib/api';
import { CAPABILITY_OPTIONS, ConnectionSelect, TargetPicker, inputCls, labelCls, selectCls } from './shared';

export interface PublishInit {
  name?: string; description?: string; connection_id?: string; capability?: string;
  target?: Record<string, any>; fixed_question_id?: string;
}

/** 把「连接 + 能力 + 目标数据(或固定问题)」生成为 type=quickbi 的工具,供 Skill / Agent 绑定。 */
const PublishToolModal: React.FC<{ connections: api.QbiConnection[]; initial: PublishInit; onClose: () => void }> = (
  { connections, initial, onClose }) => {
  const navigate = useNavigate();
  const [form, setForm] = useState({
    name: (initial.name || 'qbi_tool').replace(/[^\w一-龥-]/g, '_'), description: initial.description || '',
    connection_id: initial.connection_id || connections[0]?.id || '', capability: initial.capability || 'dataset_qa',
    target: initial.target || {}, fixed_question_id: initial.fixed_question_id || '', report_wait: false,
    max_rows: 30, visibility: 'group',
  });
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    setSaving(true);
    try {
      const t = await api.qbiPublishTool({ ...form, fixed_question_id: form.fixed_question_id || undefined });
      toast.success(`工具「${t.name}」已创建并发布 v1`);
      onClose();
      navigate(`/tools/tool/detail?id=${t.id}`);
    } catch (e: any) { toast.error(e.message); }
    finally { setSaving(false); }
  };

  return (
    <Modal title="生成 Quick BI 工具" wide onClose={onClose}
      footer={<>
        <Button variant="outline" onClick={onClose}>取消</Button>
        <Button className="bg-[#2563eb]" disabled={saving || !form.name} onClick={submit}>
          {saving && <Loader2 size={14} className="mr-1 animate-spin" />}创建并发布
        </Button>
      </>}>
      <p className="text-xs text-slate-500">生成后在「工具中心 · Skill」里绑定即可被 Agent 调用;模型只需提供问题,连接与目标数据固定在工具配置里。</p>
      <div className="grid grid-cols-2 gap-3">
        <div><label className={labelCls}>工具名 *</label>
          <input className={inputCls} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
        <div><label className={labelCls}>连接</label>
          <ConnectionSelect items={connections} value={form.connection_id} onChange={(v) => setForm({ ...form, connection_id: v })} /></div>
        <div className="col-span-2"><label className={labelCls}>描述(模型据此判断何时调用,务必写清数据范围)</label>
          <textarea className="min-h-[60px] w-full rounded-lg border border-slate-200 p-2 text-sm outline-none focus:border-blue-400"
            value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="如:查询经营驾驶舱中的销售、客流指标;适用于用户询问销售额、客流量、同比环比等问题" /></div>
        {!form.fixed_question_id ? (
          <>
            <div><label className={labelCls}>能力</label>
              <select className={selectCls} value={form.capability} onChange={(e) => setForm({ ...form, capability: e.target.value, target: {} })}>
                {CAPABILITY_OPTIONS.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
              </select></div>
            <div />
            <div className="col-span-2"><label className={labelCls}>固定目标数据(留空 = 由模型/智能选表决定)</label>
              <TargetPicker connectionId={form.connection_id} capability={form.capability} value={form.target}
                onChange={(t) => setForm({ ...form, target: t })} /></div>
          </>
        ) : (
          <div className="col-span-2 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">绑定固定问题 {form.fixed_question_id}</div>
        )}
        <div><label className={labelCls}>返回给模型的最大行数</label>
          <input type="number" className={inputCls} value={form.max_rows} onChange={(e) => setForm({ ...form, max_rows: Number(e.target.value) })} /></div>
        <div><label className={labelCls}>可见范围</label>
          <select className={selectCls} value={form.visibility} onChange={(e) => setForm({ ...form, visibility: e.target.value })}>
            <option value="private">private</option><option value="scenic">scenic</option><option value="group">group</option><option value="public">public</option>
          </select></div>
        {form.capability === 'report' && (
          <label className="col-span-2 flex items-center gap-2 text-xs text-slate-600">
            <input type="checkbox" checked={form.report_wait} onChange={(e) => setForm({ ...form, report_wait: e.target.checked })} />
            等待报告生成完成再返回(可能需要数分钟,默认只创建任务)
          </label>
        )}
      </div>
    </Modal>
  );
};

export default PublishToolModal;
