import React, { useMemo } from 'react';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import { selectCls } from './shared';

/**
 * 三级标签级联选择器:主题 → 维度 → 指标。
 *
 * 为什么是级联而不是三个独立下拉:标签表里存的是**完整路径**(一行 = 一条
 * 主题/维度/指标),维度只在所属主题下有意义。三个独立下拉可以选出
 * 「票务 / 在园人员 / 实时人数」这种库里根本不存在的组合,保存时才报错。
 *
 * 只往上传编码(topic_id/dimension_id/metric_id),中文名由后端按标签表回填 ——
 * 名字是会被改的,前端存一份就会和库里对不上。
 */
const DimensionLabelPicker: React.FC<{
  tree: api.DimensionLabelTopic[];
  value: { topic_id?: string | null; dimension_id?: string | null; metric_id?: string | null;
           topic?: string | null; dimension?: string | null; metric?: string | null };
  onChange: (v: { topic_id: string | null; dimension_id: string | null; metric_id: string | null;
                  topic: string | null; dimension: string | null; metric: string | null }) => void;
}> = ({ tree, value, onChange }) => {
  const topic = useMemo(() => tree.find((t) => t.topic_id === value.topic_id), [tree, value.topic_id]);
  const dimension = useMemo(
    () => topic?.dimensions.find((d) => d.dimension_id === value.dimension_id),
    [topic, value.dimension_id]);

  const clearAll = { topic_id: null, dimension_id: null, metric_id: null,
                     topic: null, dimension: null, metric: null };

  return (
    <div className="space-y-1">
      <div className="grid grid-cols-3 gap-2">
        <select className={selectCls} value={value.topic_id || ''}
          onChange={(e) => {
            const t = tree.find((x) => x.topic_id === e.target.value);
            // 换了主题,下面两级必然失效 —— 一起清掉,别留一个对不上的维度
            onChange(t ? { ...clearAll, topic_id: t.topic_id, topic: t.topic } : clearAll);
          }}>
          <option value="">选择主题</option>
          {tree.map((t) => <option key={t.topic_id} value={t.topic_id}>{t.topic}</option>)}
        </select>

        <select className={cn(selectCls, !topic && 'text-slate-300')} value={value.dimension_id || ''}
          disabled={!topic}
          onChange={(e) => {
            const d = topic?.dimensions.find((x) => x.dimension_id === e.target.value);
            onChange({
              ...clearAll,
              topic_id: value.topic_id || null, topic: value.topic || null,
              dimension_id: d?.dimension_id ?? null, dimension: d?.dimension ?? null,
            });
          }}>
          <option value="">选择维度</option>
          {(topic?.dimensions || []).map((d) => (
            <option key={d.dimension_id} value={d.dimension_id}>{d.dimension}</option>
          ))}
        </select>

        <select className={cn(selectCls, !dimension && 'text-slate-300')} value={value.metric_id || ''}
          disabled={!dimension}
          onChange={(e) => {
            const m = dimension?.metrics.find((x) => x.metric_id === e.target.value);
            onChange({
              topic_id: value.topic_id || null, topic: value.topic || null,
              dimension_id: value.dimension_id || null, dimension: value.dimension || null,
              metric_id: m?.metric_id ?? null, metric: m?.metric ?? null,
            });
          }}>
          <option value="">选择指标</option>
          {(dimension?.metrics || []).map((m) => (
            <option key={m.metric_id} value={m.metric_id}>{m.metric}</option>
          ))}
        </select>
      </div>

      <p className="text-[11px] text-slate-400">
        {value.metric_id
          ? <>已挂在 <span className="text-slate-600">{value.topic} / {value.dimension} / {value.metric}</span> 下,APP 端可见。
              <button type="button" className="ml-2 text-blue-500 hover:underline"
                onClick={() => onChange(clearAll)}>清空</button></>
          : '三级都不选也能保存,但这条问题不会出现在 APP 的问题树里(管理端仍可执行)。标签在「知识中心 - 维度标签」维护。'}
      </p>
    </div>
  );
};

export default DimensionLabelPicker;
