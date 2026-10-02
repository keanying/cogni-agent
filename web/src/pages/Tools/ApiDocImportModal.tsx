import React, { useState } from 'react';
import { FileText, Loader2, AlertTriangle, CheckCircle2, Wand2 } from 'lucide-react';
import { Modal } from '../../components/ui/modal';
import { Button } from '../../components/ui/button';
import { Input } from '../../components/ui/input';
import { Badge } from '../../components/ui/badge';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { useCapabilities } from '../../lib/capabilities';
import { ScenicSelect } from './QuickBI/shared';

/**
 * 按 Markdown 接口文档创建工具。
 *
 * **为什么不让人写 JSON Schema**:JSON Schema 是给机器看的,人写起来啰嗦、读起来更啰嗦,
 * 而运维手上现成的东西恰恰是一份接口文档。手工翻译一遍既慢又容易译错 —— 译错的表现
 * 还特别隐蔽:模型照着错的 schema 传参,接口回一个业务错误,看起来像「接口有问题」,
 * 其实是参数名少了个字母。
 *
 * **为什么分「解析预览」「保存」两步**:解析结果要先给人看一眼 —— 哪些参数被认成必传、
 * 数据体被认在哪一层、认证 header 有没有从 curl 里取到。看过再保存,比保存完再去猜
 * 「为什么模型老是传错参数」省事得多。
 */

const SAMPLE = `# 1.17 BI报表配置信息

## 概述
获取景区绑定的 BI 报表地址。

| 接口地址 | /v1/data/digitalBrain/api/quickBiLink |
| --- | --- |
| 请求方式 | POST |
| 认证方式 | header 传 xpftkey |

## 请求参数
| 参数名 | 类型 | 必填 | 示例值 | 说明 |
| --- | --- | --- | --- | --- |
| scenicId | String | 是 | PFTSCA01018088 | 景区ID |

## 返回参数
| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| code | Integer | 状态码 |
| data | Object | 报表配置 |

## 请求示例
\`\`\`bash
curl --location --request POST 'https://<host>/v1/data/digitalBrain/api/quickBiLink' \\
--header 'xpftkey: pfk_xxx' \\
--data '{"scenicId":"PFTSCA01018088"}'
\`\`\`
`;

const Row: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="flex gap-3 text-xs py-1.5 border-b border-slate-50 last:border-0">
    <span className="w-20 shrink-0 text-slate-400 font-medium">{label}</span>
    <span className="flex-1 text-slate-700 break-all">{children}</span>
  </div>
);

export const ApiDocImportModal: React.FC<{
  open: boolean;
  onClose: () => void;
  /** 保存成功后回调(列表刷新 / 跳详情) */
  onCreated: (toolId: string) => void;
}> = ({ open, onClose, onCreated }) => {
  const cap = useCapabilities();
  const [markdown, setMarkdown] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [scenicId, setScenicId] = useState('');
  const [parsed, setParsed] = useState<api.ApiDocParsed | null>(null);
  const [parsing, setParsing] = useState(false);
  const [saving, setSaving] = useState(false);

  const reset = () => {
    setMarkdown(''); setBaseUrl(''); setName(''); setDescription('');
    setScenicId(''); setParsed(null);
  };
  const close = () => { if (parsing || saving) return; reset(); onClose(); };

  const doParse = async () => {
    if (!markdown.trim()) { toast.error('先把接口文档贴进来'); return; }
    setParsing(true);
    try {
      const r = await api.parseApiDoc({ markdown, default_base_url: baseUrl.trim() });
      setParsed(r);
      // 名称/说明留空时按文档自动带出来,人可以再改
      if (!name) setName(r.title || '');
      if (!description) setDescription(r.summary || '');
      toast.success('解析完成,确认下面的结果无误再保存');
    } catch (e: any) { setParsed(null); toast.error(e.message || '解析失败'); }
    finally { setParsing(false); }
  };

  const doSave = async () => {
    if (!parsed) { toast.error('先解析预览一次'); return; }
    setSaving(true);
    try {
      const r = await api.createToolFromApiDoc({
        markdown, default_base_url: baseUrl.trim(),
        name: name.trim(), description: description.trim(),
        scenic_id: scenicId || null,
      });
      toast.success(r.message || `已创建工具「${r.name}」`);
      reset();
      onCreated(r.id);
    } catch (e: any) { toast.error(e.message || '创建失败'); }
    finally { setSaving(false); }
  };

  const required = (parsed?.request_fields || []).filter((f) => f.required);

  return (
    <Modal
      open={open}
      onClose={close}
      width="max-w-4xl"
      title={<span className="flex items-center gap-2"><FileText size={15} className="text-blue-500" />按接口文档新建 Tool</span>}
      footer={
        <>
          <Button variant="outline" className="rounded-xl" onClick={close} disabled={parsing || saving}>取消</Button>
          <Button variant="outline" className="rounded-xl gap-2" onClick={doParse} disabled={parsing || saving}>
            {parsing ? <Loader2 size={14} className="animate-spin" /> : <Wand2 size={14} />}解析预览
          </Button>
          <Button className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl gap-2"
            onClick={doSave} disabled={!parsed || parsing || saving || !cap.can_create}
            title={!cap.can_create ? `${cap.role_label || '当前角色'}为只读角色,不能新建工具` : ''}>
            {saving ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}保存并发布 v1
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <p className="text-xs text-slate-500 leading-relaxed bg-slate-50 rounded-xl p-3">
          把一份 Markdown 接口文档整段贴进来,系统解析出请求参数、认证 header、数据体所在层级,
          直接生成可被 Skill / Agent 调用的 http 工具。
          <span className="text-slate-400">
            {' '}提示:文档里那张**返回参数表**不是可有可无的 —— 结果压缩层靠它知道「数据体在哪一层、
            每一列是什么」,才谈得上按列筛选与统计;没有它,上千行的返回只能原样截断。
          </span>
        </p>

        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-xs font-bold text-slate-700">接口文档(Markdown)</label>
            <button type="button" className="text-[11px] text-blue-600 hover:underline"
              onClick={() => setMarkdown(SAMPLE)}>填入示例</button>
          </div>
          <textarea
            value={markdown}
            onChange={(e) => { setMarkdown(e.target.value); setParsed(null); }}
            rows={12}
            placeholder="# 1.17 BI报表配置信息&#10;&#10;## 请求参数&#10;| 参数名 | 类型 | 必填 | 示例值 | 说明 |&#10;..."
            className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-mono leading-relaxed text-slate-700 focus:outline-none focus:ring-2 focus:ring-[#2563eb]/30"
          />
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <label className="text-xs font-bold text-slate-700">默认域名(可选)</label>
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="文档里只写了 URI 没写域名时填,如 https://api.example.com"
              className="mt-1.5 h-10 rounded-xl border-slate-200 text-xs" />
          </div>
          <div>
            <label className="text-xs font-bold text-slate-700">工具名(留空取文档标题)</label>
            <Input value={name} onChange={(e) => setName(e.target.value)}
              placeholder="如 BI报表配置信息"
              className="mt-1.5 h-10 rounded-xl border-slate-200 text-xs" />
          </div>
          <div className="md:col-span-2">
            <label className="text-xs font-bold text-slate-700">给模型看的说明(留空取文档概述)</label>
            <Input value={description} onChange={(e) => setDescription(e.target.value)}
              placeholder="一句话说清「什么时候该调它」—— 模型是照着这句决定用不用的"
              className="mt-1.5 h-10 rounded-xl border-slate-200 text-xs" />
          </div>
          {cap.manage_all && (
            <div className="md:col-span-2">
              <label className="text-xs font-bold text-slate-700">归属景区(留空 = 集团级,所有景区共用)</label>
              <div className="mt-1.5"><ScenicSelect value={scenicId} onChange={setScenicId} registeredOnly /></div>
            </div>
          )}
        </div>

        {parsed && (
          <div className="rounded-xl border border-slate-200 bg-white p-4 space-y-3">
            <div className="text-xs font-black text-slate-800">解析结果</div>
            <div>
              <Row label="标题">{parsed.title || <span className="text-slate-300">(未识别)</span>}</Row>
              <Row label="请求">
                <Badge variant="outline" className="rounded-md text-[10px] mr-2 border-blue-100 bg-blue-50 text-blue-600">{parsed.method}</Badge>
                <span className="font-mono">{parsed.base_url}{parsed.uri}</span>
              </Row>
              <Row label="认证 header">
                {Object.keys(parsed.headers || {}).length
                  ? Object.entries(parsed.headers).map(([k, v]) => (
                      <span key={k} className="font-mono mr-3">{k}: {v}</span>))
                  : <span className="text-slate-300">(curl 里没取到,保存后可在工具详情里补)</span>}
              </Row>
              <Row label="必传参数">
                {required.length
                  ? required.map((f) => (
                      <Badge key={f.name} variant="outline" className="rounded-md text-[10px] mr-1.5 border-rose-100 bg-rose-50 text-rose-600">
                        {f.name}:{f.type}
                      </Badge>))
                  : <span className="text-slate-300">(无)</span>}
              </Row>
              <Row label="可选参数">
                {(parsed.request_fields || []).filter((f) => !f.required).length
                  ? parsed.request_fields.filter((f) => !f.required).map((f) => (
                      <Badge key={f.name} variant="outline" className="rounded-md text-[10px] mr-1.5 border-slate-100 bg-slate-50 text-slate-500">
                        {f.name}:{f.type}
                      </Badge>))
                  : <span className="text-slate-300">(无)</span>}
              </Row>
              <Row label="数据体层级">
                {/* 结果压缩层就是从这一层往下找表格数据的,认错了压缩会退化成原样截断 */}
                <span className="font-mono">{(parsed.data_path || []).length ? parsed.data_path.join(' → ') : '(整个响应体)'}</span>
              </Row>
              <Row label="返回字段">
                {(parsed.response_fields || []).length
                  ? `${parsed.response_fields.length} 个:${parsed.response_fields.slice(0, 12).map((f) => f.name).join('、')}${parsed.response_fields.length > 12 ? '…' : ''}`
                  : <span className="text-amber-600">未识别到返回参数表 —— 大结果只能原样截断,建议补一张</span>}
              </Row>
            </div>

            {(parsed.warnings || []).length > 0 && (
              <div className="rounded-lg bg-amber-50 border border-amber-100 p-3 space-y-1">
                {parsed.warnings.map((w, i) => (
                  <div key={i} className="flex gap-2 text-[11px] text-amber-700">
                    <AlertTriangle size={13} className="shrink-0 mt-0.5" />{w}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
};

export default ApiDocImportModal;
