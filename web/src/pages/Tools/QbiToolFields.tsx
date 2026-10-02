import React, { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import * as api from '../../lib/api';

/**
 * Quick BI 工具的三个选择器。
 *
 * 这三项原先都是「让人自己填 ID」的文本框,而这些 ID 是 UUID —— 人得先去另一个页面
 * 找到它、复制、切回来粘上。**填错了不会当场报错**,要等到真发起提问才炸一句
 * 「资产不存在或无权访问:<一串 id>」,那时候已经不知道是哪一步填错的了。
 *
 * 能选就不让填。存进配置的仍是同样的 ID,只是不用人去记、去抄。
 * 三个控件都保留「手工填 ID」的退路:跨景区、或者资产还没登记时仍要能配。
 */

const selectCls =
  'h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none focus:border-blue-400';

/** 连接选择器:列当前账号能用的连接,留空 = 默认连接 */
export const QbiConnectionSelect: React.FC<{
  value: string; onChange: (v: string) => void; placeholder?: string;
}> = ({ value, onChange, placeholder }) => {
  const [items, setItems] = useState<api.QbiConnection[] | null>(null);

  useEffect(() => {
    api.qbiListConnections().then((r) => setItems(r.items || [])).catch(() => setItems([]));
  }, []);

  if (items === null) {
    return <div className="flex h-10 items-center gap-2 text-xs text-slate-400">
      <Loader2 size={13} className="animate-spin" />正在读取可用连接…
    </div>;
  }
  // 填了一个不在列表里的 ID(跨景区、或连接刚被删)时,把它保留成一个选项 ——
  // 直接吞掉的话,人一打开编辑页就把原来的配置弄丢了,而且毫无察觉
  const unknown = value && !items.some((c) => c.id === value);
  return (
    <div className="space-y-1">
      <select value={value || ''} onChange={(e) => onChange(e.target.value)} className={selectCls}>
        <option value="">{placeholder || '留空使用默认连接'}</option>
        {items.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}{c.scenic_id ? `（${c.scenic_id}）` : '（集团级）'}
            {c.active === false ? ' · 已停用' : ''}
          </option>
        ))}
        {unknown && <option value={value}>{value}（当前账号看不到这条连接）</option>}
      </select>
      {unknown && (
        <p className="text-[11px] text-amber-600">
          配置里这条连接当前账号看不到 —— 可能属于别的景区,或已被删除。留着不动即可;要换请重选。
        </p>
      )}
    </div>
  );
};

/**
 * 目标数据选择器:已登记的仪表板 / 数据集 / 上传文件,选一个。
 *
 * 存进配置的形状与执行链一致:仪表板/数据集存 {asset_id, asset_kind},文件存 {file_id}。
 * 手工模式保留原来的 JSON —— 有些场景要填 page_id 或 dashboard 地址。
 */
export const QbiTargetSelect: React.FC<{
  value: any; onChange: (v: any) => void; connectionId?: string;
}> = ({ value, onChange, connectionId }) => {
  const [assets, setAssets] = useState<api.QbiAsset[]>([]);
  const [files, setFiles] = useState<api.QbiFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [manual, setManual] = useState(false);

  useEffect(() => {
    setLoading(true);
    // 不按连接过滤:资产可能登记在另一条连接下,按连接筛会让它凭空消失
    // (现场撞过:两块仪表板,操练台下拉里只出现一块)。选定之后执行链会按资产自动切连接。
    Promise.all([
      api.qbiListAssets({}).then((r) => r.items || []).catch(() => []),
      api.qbiListFiles(connectionId || undefined).then((r) => r.items || []).catch(() => []),
    ]).then(([a, f]) => { setAssets(a); setFiles(f); }).finally(() => setLoading(false));
  }, [connectionId]);

  const cur = value || {};
  const selected = cur.asset_id ? `asset:${cur.asset_id}` : cur.file_id ? `file:${cur.file_id}` : '';
  const hasOther = !selected && Object.keys(cur).length > 0;

  if (manual || hasOther) {
    return (
      <div className="space-y-1.5">
        <textarea
          value={typeof cur === 'string' ? cur : JSON.stringify(cur, null, 2)}
          onChange={(e) => { try { onChange(JSON.parse(e.target.value || '{}')); } catch { /* 输入中,先不改 */ } }}
          rows={4} spellCheck={false}
          className="w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-blue-400" />
        <button type="button" onClick={() => setManual(false)}
          className="text-[11px] font-medium text-blue-600 hover:underline">← 回到选择模式</button>
      </div>
    );
  }

  if (loading) {
    return <div className="flex h-10 items-center gap-2 text-xs text-slate-400">
      <Loader2 size={13} className="animate-spin" />正在读取已登记的数据…
    </div>;
  }

  // 按仪表板分组:数据集本来就是某块板下面的表,混成一个大列表之后
  // 「这个集属于哪块板」只能靠名字猜,很容易拿 A 板的集去问 B 板的问题
  const boards = assets.filter((a) => a.kind === 'dashboard');
  const sets = assets.filter((a) => a.kind === 'dataset');
  const taken = new Set<string>();
  const groups = boards.map((b) => {
    const cubeIds = new Set((b.cubes || []).map((c) => c.cube_id));
    const mine = sets.filter((d) => (d.source_asset_id ? d.source_asset_id === b.id : cubeIds.has(d.ref_id)));
    mine.forEach((d) => taken.add(d.id));
    return { key: b.id, label: `${b.name}${b.scenic_id ? '' : '（集团级）'}`, items: [b, ...mine] };
  });
  const orphans = sets.filter((d) => !taken.has(d.id));
  if (orphans.length) groups.push({ key: '__orphan__', label: '未归属仪表板的数据集', items: orphans });

  return (
    <div className="space-y-1.5">
      <select
        value={selected}
        onChange={(e) => {
          const v = e.target.value;
          if (!v) { onChange({}); return; }
          if (v.startsWith('file:')) { onChange({ file_id: v.slice(5) }); return; }
          const id = v.slice(6);
          const a = assets.find((x) => x.id === id);
          // asset_kind 一起存:执行链据此决定走仪表板还是数据集,少了它要多查一次
          onChange({ asset_id: id, asset_kind: a?.kind || 'dashboard' });
        }}
        className={selectCls}>
        <option value="">不固定（由模型按问题提供 / 自动智能选表）</option>
        {groups.map((g) => (
          <optgroup key={g.key} label={g.label}>
            {g.items.map((a) => (
              <option key={a.id} value={`asset:${a.id}`} disabled={a.active === false}>
                {a.kind === 'dashboard' ? '板' : '　集'}｜{a.name}{a.active === false ? '（已停用）' : ''}
              </option>
            ))}
          </optgroup>
        ))}
        {files.length > 0 && (
          <optgroup label="上传文件">
            {files.map((f) => <option key={f.id} value={`file:${f.id}`}>文件｜{f.file_name}</option>)}
          </optgroup>
        )}
      </select>
      <button type="button" onClick={() => setManual(true)}
        className="text-[11px] text-slate-400 hover:text-blue-600 hover:underline">
        手工填 JSON（要填 page_id / dashboard 地址时用）
      </button>
    </div>
  );
};

/** 固定问题:从已有问题里绑一条,或直接填 ID */
export const QbiFixedQuestionSelect: React.FC<{
  value: string; onChange: (v: string) => void; connectionId?: string;
}> = ({ value, onChange, connectionId }) => {
  const [items, setItems] = useState<api.QbiFixedQuestion[] | null>(null);
  const [manual, setManual] = useState(false);

  useEffect(() => {
    api.qbiListFixed({ connection_id: connectionId || undefined })
      .then((r) => setItems(r.items || [])).catch(() => setItems([]));
  }, [connectionId]);

  if (manual) {
    return (
      <div className="space-y-1.5">
        <input value={value || ''} onChange={(e) => onChange(e.target.value)} spellCheck={false}
          placeholder="固定问题 ID" className={`${selectCls} font-mono`} />
        <button type="button" onClick={() => setManual(false)}
          className="text-[11px] font-medium text-blue-600 hover:underline">← 从列表里选</button>
      </div>
    );
  }
  if (items === null) {
    return <div className="flex h-10 items-center gap-2 text-xs text-slate-400">
      <Loader2 size={13} className="animate-spin" />正在读取固定问题…
    </div>;
  }
  const unknown = value && !items.some((q) => q.id === value);
  return (
    <div className="space-y-1.5">
      <select value={value || ''} onChange={(e) => onChange(e.target.value)} className={selectCls}>
        <option value="">不绑定（模型自己组问题）</option>
        {items.map((q) => (
          <option key={q.id} value={q.id}>
            {q.title || q.question}{q.scenic_id ? `（${q.scenic_id}）` : '（集团级）'}
          </option>
        ))}
        {unknown && <option value={value}>{value}（列表里没有这条）</option>}
      </select>
      <button type="button" onClick={() => setManual(true)}
        className="text-[11px] text-slate-400 hover:text-blue-600 hover:underline">手工填 ID</button>
    </div>
  );
};
