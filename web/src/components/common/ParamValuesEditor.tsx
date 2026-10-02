import React from 'react';
import { Plus, X } from 'lucide-react';
import { Input } from '../ui/input';
import type { ParamValue } from '../../lib/api';

/**
 * 参数取值编辑:单值 = 一行;多值 = 多行,每行「值 + 备注」(如 vbk_569202 · 携程主账号)。
 * 系统默认值(系统设置 · 参数配置)和景区覆盖值(景区配置 · 接口参数)共用。
 */
const ParamValuesEditor: React.FC<{
  values: ParamValue[];
  multi: boolean;
  secret?: boolean;
  placeholder?: string;
  onChange: (v: ParamValue[]) => void;
}> = ({ values, multi, secret, placeholder, onChange }) => {
  const rows = values.length ? values : [{ value: '', label: '' }];
  const set = (i: number, p: Partial<ParamValue>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...p } : r)));
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <Input className="h-8 flex-1 font-mono text-[11px]" value={r.value} type={secret ? 'password' : 'text'}
            placeholder={placeholder || '取值'} onChange={(e) => set(i, { value: e.target.value })} />
          {multi && (
            <Input className="h-8 w-32 text-[11px]" value={r.label} placeholder="备注(如 携程主账号)"
              onChange={(e) => set(i, { label: e.target.value })} />
          )}
          {multi && rows.length > 1 && (
            <button type="button" className="text-slate-300 hover:text-red-500" title="删除这个值"
              onClick={() => onChange(rows.filter((_, j) => j !== i))}><X size={14} /></button>
          )}
        </div>
      ))}
      {multi && (
        <button type="button" onClick={() => onChange([...rows, { value: '', label: '' }])}
          className="flex items-center gap-1 text-[11px] font-bold text-blue-600 hover:text-blue-700">
          <Plus size={12} />再加一个值
        </button>
      )}
    </div>
  );
};

export default ParamValuesEditor;
