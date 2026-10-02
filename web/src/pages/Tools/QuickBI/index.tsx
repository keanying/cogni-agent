import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { BarChart3, BookOpen, Database, History, ListChecks, Plug } from 'lucide-react';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import Connections from './Connections';
import Assets from './Assets';
import FixedQuestions from './FixedQuestions';
import Runs from './Runs';
import ApiDocs from './ApiDocs';
import { useConnections } from './shared';

// 没有「操练台」页签:验证入口统一在左侧的操练台里(Quick BI 是那里的一类执行目标),
// 免得同一件事有两个长得不一样的入口。
const TABS = [
  { key: 'fixed', label: '固定问题', icon: ListChecks },
  { key: 'assets', label: '数据资产', icon: Database },
  { key: 'connections', label: '连接配置', icon: Plug },
  { key: 'runs', label: '运行记录', icon: History },
  { key: 'api', label: 'API 接入', icon: BookOpen },
] as const;

/**
 * 工具中心 · Quick BI(独立模块)。
 * 在这里配置连接与数据资产、维护固定问题、查运行记录;提问与验证走左侧「操练台」。
 */
const QuickBIPage: React.FC = () => {
  const [params, setParams] = useSearchParams();
  const tab = (params.get('tab') as (typeof TABS)[number]['key']) || 'fixed';
  const conns = useConnections();
  const [overview, setOverview] = useState<api.QbiOverview | null>(null);
  const [fixedKey, setFixedKey] = useState(0);

  useEffect(() => { api.qbiOverview().then(setOverview).catch(() => setOverview(null)); }, []);
  useEffect(() => {
    if (!conns.loading && conns.items.length === 0 && !params.get('tab')) setParams({ tab: 'connections' }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conns.loading]);

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
        {TABS.map((t) => (
          <button key={t.key} type="button" onClick={() => setParams({ tab: t.key })}
            className={cn('-mb-px inline-flex items-center gap-1.5 border-b-2 px-4 py-2 text-sm font-medium transition-colors',
              tab === t.key ? 'border-blue-600 text-blue-600' : 'border-transparent text-slate-500 hover:text-slate-800')}>
            <t.icon size={15} />{t.label}
          </button>
        ))}
      </div>

      {tab === 'fixed' && <FixedQuestions connections={conns.items} refreshKey={fixedKey} />}
      {tab === 'assets' && <Assets connections={conns.items} onAdopted={() => setFixedKey((k) => k + 1)} />}
      {tab === 'connections' && <Connections items={conns.items} loading={conns.loading} reload={conns.reload} overview={overview} />}
      {tab === 'runs' && <Runs />}
      {tab === 'api' && <ApiDocs />}
    </div>
  );
};

export default QuickBIPage;
