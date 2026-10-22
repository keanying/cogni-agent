import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { BarChart3, BookOpen, Database, History, ListChecks, Plug } from 'lucide-react';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import { useAppStore } from '../../../store/useAppStore';
import Connections from './Connections';
import Assets from './Assets';
import FixedQuestions from './FixedQuestions';
import Runs from './Runs';
import ApiDocs from './ApiDocs';
import { isGroupAccount, useConnections } from './shared';

// 没有「操练台」页签:验证入口统一在左侧的操练台里(Quick BI 是那里的一类执行目标),
// 免得同一件事有两个长得不一样的入口。
// groupOnly:连接凭证、资产登记、运行记录、API 接入都是集团侧的配置动作,
// 景区账号只需要维护自己景区的固定问题,给它看了反而会去改不属于它的东西。
const TABS = [
  { key: 'fixed', label: '固定问题', icon: ListChecks, groupOnly: false },
  { key: 'assets', label: '数据资产', icon: Database, groupOnly: true },
  { key: 'connections', label: '连接配置', icon: Plug, groupOnly: true },
  { key: 'runs', label: '运行记录', icon: History, groupOnly: true },
  { key: 'api', label: 'API 接入', icon: BookOpen, groupOnly: true },
] as const;

/**
 * 工具中心 · Quick BI(独立模块)。
 * 在这里配置连接与数据资产、维护固定问题、查运行记录;提问与验证走左侧「操练台」。
 */
const QuickBIPage: React.FC = () => {
  const [params, setParams] = useSearchParams();
  const { me } = useAppStore();
  const conns = useConnections();
  const [overview, setOverview] = useState<api.QbiOverview | null>(null);
  const [fixedKey, setFixedKey] = useState(0);
  const isGroup = isGroupAccount(me);
  const tabs = TABS.filter((t) => !t.groupOnly || isGroup);
  // URL 里带了本账号看不到的页签(如景区账号打开 ?tab=assets)时回落到固定问题,
  // 否则会渲染出一页不属于自己权限范围的内容。
  const raw = params.get('tab');
  const tab = tabs.find((t) => t.key === raw)?.key || 'fixed';

  useEffect(() => { api.qbiOverview().then(setOverview).catch(() => setOverview(null)); }, []);
  useEffect(() => {
    // 只在集团账号下引导去建连接:景区账号看不到「连接配置」页签,跳过去是死路。
    if (isGroup && !conns.loading && conns.items.length === 0 && !params.get('tab')) {
      setParams({ tab: 'connections' }, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conns.loading, isGroup]);

  return (
    <div className="space-y-5 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col justify-between gap-3 md:flex-row md:items-end">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-slate-900">
            <BarChart3 className="text-orange-500" />Quick BI
          </h1>
        </div>
        {overview && (
          <div className="flex flex-wrap gap-1.5">
            {overview.capabilities.map((c) => (
              <span key={c.key} title={c.desc} className="rounded-full border border-slate-200 bg-white px-2.5 py-0.5 text-[11px] text-slate-500">{c.label}</span>
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-wrap gap-1 border-b border-slate-200">
        {tabs.map((t) => (
          <button key={t.key} type="button" onClick={() => setParams({ tab: t.key })}
            className={cn('-mb-px inline-flex items-center gap-1.5 border-b-2 px-4 py-2 text-sm font-medium transition-colors',
              tab === t.key ? 'border-blue-600 text-blue-600' : 'border-transparent text-slate-500 hover:text-slate-800')}>
            <t.icon size={15} />{t.label}
          </button>
        ))}
      </div>

      {tab === 'fixed' && <FixedQuestions connections={conns.items} refreshKey={fixedKey} hasAssetsTab={isGroup} />}
      {tab === 'assets' && <Assets connections={conns.items} onAdopted={() => setFixedKey((k) => k + 1)} />}
      {tab === 'connections' && <Connections items={conns.items} loading={conns.loading} reload={conns.reload} overview={overview} />}
      {tab === 'runs' && <Runs />}
      {tab === 'api' && <ApiDocs />}
    </div>
  );
};

export default QuickBIPage;
