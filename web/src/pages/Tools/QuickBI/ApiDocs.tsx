import React, { useEffect, useState } from 'react';
import { BookOpen, Copy, KeyRound, Loader2, Plus } from 'lucide-react';
import { toast } from 'sonner';
import Markdown from '../../../components/common/Markdown';
import { Button } from '../../../components/ui/button';
import * as api from '../../../lib/api';

const origin = typeof window !== 'undefined' ? window.location.origin : 'https://your-host';

const DOC = `
开放平台接口前缀 \`/v1/quickbi\`,与 \`/v1/chat/completions\` 同级(不带 \`/api\`)。

### 鉴权

开放平台接口统一使用 **API Key**(右侧「API Key」面板创建,格式 \`sk-zhilv-...\`):

\`\`\`bash
export HOST=${origin}
export KEY=sk-zhilv-xxxxxxxx
\`\`\`

- 可用连接:勾选了「允许开放平台调用」且可见范围不是 private 的连接。集团级 Key(未绑定景区)可用全部,景区 Key 只能用本景区或 public 连接。
- 限定了可调用 Agent 的 Key 不能调用 Quick BI 接口,请单独创建一个不限定 Agent 的 Key。
- 所有接口返回统一信封 \`{code, message, data, trace_id}\`;\`stream=true\` 时返回 SSE,\`event\` 名即事件类型。
- 可选请求头 \`X-End-User\`:终端用户标识,写入运行记录便于追溯。

### 能力与目标数据

| capability | 说明 | target 常用字段 |
|---|---|---|
| dataset_qa | 问数 · 数据集 | \`cube_id\` / \`cube_name\`(都不填 = 智能选表);\`cube_ids\`(≥2 个)= 多表问数(官方 multipleCubeIds,小Q选一张或多张表) |
| dashboard_qa | 报表 · 仪表板 | \`asset_id\` / \`page_id\` / \`dashboard\`(URL),可加 \`chart_ids\` 限定组件;问题点到某张图 → 用该图的数据集,否则所选组件的数据集一起多表问数 |
| file_qa | 问数 · 上传文件 | \`file_id\`(先调上传接口) |
| insight | 解读 | \`asset_id\`(仪表板或数据集)/ \`page_id\` / \`dashboard\` / \`cube_id\` / \`cube_ids\` / \`file_id\` / \`data\`;仪表板可加 \`chart_ids\`(不传 = 全部组件),快照外的组件按问题多表取数后一起解读;数据集会先按问题取数再解读 |
| report | 报告(异步) | \`file_ids\` / \`asset_id\` / \`web_search\` |

capability 为空时按问题意图自动路由(报告 > 解读 > 问数)。

### 1. 固定问题

\`\`\`bash
# 列出可调用的固定问题
curl -s $HOST/v1/quickbi/fixed-questions -H "Authorization: Bearer $KEY"

# 执行(问题中的 {{变量}} 通过 params 传入)
curl -s -X POST $HOST/v1/quickbi/fixed-questions/<id>/run -H "Authorization: Bearer $KEY" \\
  -H "Content-Type: application/json" -d '{"params": {"month": "2026年8月"}}'
\`\`\`

### 2. 对话场景

\`\`\`bash
# 建会话(目标数据粘在会话上,后续追问沿用;智能选表选中的数据集也会被记住)
curl -s -X POST $HOST/v1/quickbi/sessions -H "Authorization: Bearer $KEY" \\
  -H "Content-Type: application/json" -d '{"capability": "auto", "target": {"asset_id": "<仪表板资产ID>"}}'

# 发消息(stream=true 为 SSE)
curl -N -X POST $HOST/v1/quickbi/sessions/<session_id>/messages -H "Authorization: Bearer $KEY" \\
  -H "Content-Type: application/json" -d '{"question": "本月销售额最高的平台是哪个", "stream": true}'

# 会话历史
curl -s $HOST/v1/quickbi/sessions/<session_id> -H "Authorization: Bearer $KEY"
\`\`\`

### 3. 报告生成

\`\`\`bash
# 上传引用文件(xls/xlsx/csv/doc/docx ≤5MB)
curl -s -X POST $HOST/v1/quickbi/files -H "Authorization: Bearer $KEY" -F "file=@经营数据.xlsx"

# 创建报告(立即返回 id 与 status=running)
curl -s -X POST $HOST/v1/quickbi/reports -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \\
  -d '{"question": "生成8月经营分析报告", "target": {"file_ids": ["<file_id>"], "asset_id": "<仪表板资产ID>"}}'

# 轮询(建议 10 秒一次;完成后 data.report_url 为报告回放链接)
curl -s $HOST/v1/quickbi/reports/<id> -H "Authorization: Bearer $KEY"

# 或 SSE 跟踪到完成
curl -N $HOST/v1/quickbi/reports/<id>/stream -H "Authorization: Bearer $KEY"
\`\`\`

### 4. 一次性提问

\`\`\`bash
curl -s -X POST $HOST/v1/quickbi/query -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \\
  -d '{"question": "近7天每日客流", "capability": "dataset_qa", "stream": false}'
\`\`\`

### SSE 事件

| type | 字段 | 说明 |
|---|---|---|
| run | run_id, capability, routed | 第一个事件 |
| status | stage, message | 阶段提示(选表/上传/快照/报告生成中) |
| reasoning | delta | 思考过程(增量) |
| text | delta, section | 回答(answer/conclusion/summary/report/check) |
| sql / code | delta | 生成的 SQL / Python |
| chart | chart{title, chart_type, fields, rows} | 结构化结果 |
| step | title, desc | 多步骤进度 |
| report | chat_id, report_url, finished | 报告状态 |
| error | message, code | Quick BI 错误码已翻译为中文 |
| done | run_id, status, answer, report_url, duration_ms | 最后一个事件 |

### 其他

- \`GET /v1/quickbi/capabilities\` 能力清单 · \`GET /v1/quickbi/connections\` 可用连接
- \`GET /v1/quickbi/datasets\` 可问数数据集 · \`GET /v1/quickbi/assets\` 已登记仪表板/数据集
- \`GET /v1/quickbi/runs/{id}\` 运行记录(仅本 Key 发起的)
- Agent 也可以直接绑定 **quickbi 类型工具**(「生成工具」按钮),通过 \`/v1/chat/completions\` 对话调用。
`;

const KeyManager: React.FC = () => {
  const [items, setItems] = useState<api.ApiKeyItem[]>([]);
  const [created, setCreated] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => api.listApiKeys().then(setItems).catch(() => setItems([]));
  useEffect(() => { load(); }, []);
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="mb-2 flex items-center justify-between">
        <span className="flex items-center gap-2 text-sm font-bold text-slate-800"><KeyRound size={15} className="text-blue-500" />API Key</span>
        <Button size="sm" className="gap-1 bg-[#2563eb]" disabled={busy} onClick={async () => {
          setBusy(true);
          try { const k = await api.createApiKey({}); setCreated(k.key); load(); }
          catch (e: any) { toast.error(e.message); } finally { setBusy(false); }
        }}>{busy ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}创建</Button>
      </div>
      {created && (
        <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
          新 Key(仅显示这一次,请立即保存):
          <code className="ml-1 break-all font-mono font-bold">{created}</code>
          <button type="button" className="ml-2 inline-flex items-center gap-0.5 text-amber-700 underline"
            onClick={() => { navigator.clipboard?.writeText(created); toast.success('已复制'); }}><Copy size={11} />复制</button>
        </div>
      )}
      <div className="divide-y divide-slate-100 text-xs">
        {items.length === 0 && <div className="py-2 text-slate-400">暂无 Key(需要集团管理员/开发者权限创建)</div>}
        {items.map((k) => (
          <div key={k.id} className="flex items-center justify-between py-1.5">
            <span className="font-mono text-slate-600">{k.prefix_display}</span>
            <span className="text-slate-400">{k.created_at ? new Date(k.created_at).toLocaleDateString() : ''}{k.expires_at ? ` · 到期 ${new Date(k.expires_at).toLocaleDateString()}` : ''}</span>
            {k.revoked ? <span className="text-slate-300">已吊销</span> : (
              <button type="button" className="text-rose-500 hover:underline" onClick={async () => {
                await api.revokeApiKey(k.id).catch((e) => toast.error(e.message)); load();
              }}>吊销</button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
};

const ApiDocs: React.FC = () => (
  <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_360px]">
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-blue-100 bg-blue-50/60 px-4 py-3 text-sm text-blue-800">
        <span>下面是一份速查。<b>逐接口的完整说明</b>(API 概览 / 请求参数 / 返回参数 / 可直接粘进终端的示例)在开发者文档里 ——
          对接方没有后台账号,把右边这个地址发给他们就行。</span>
        {/* 对接方通常没有后台账号,给他们一个可以直接转发的免登录地址 */}
        <a href="/developer" target="_blank" rel="noreferrer"
           className="inline-flex items-center gap-1 rounded-lg bg-white px-3 py-1.5 text-xs font-semibold text-blue-600 ring-1 ring-blue-200 hover:bg-blue-50">
          <BookOpen size={13} />开发者 API 文档 /developer(免登录)
        </a>
      </div>
      <div className="rounded-2xl border border-slate-200 bg-white p-5"><Markdown content={DOC} /></div>
    </div>
    <div><KeyManager /></div>
  </div>
);

export default ApiDocs;
