
import React, { useState } from 'react';
import { ArrowLeft, ShieldCheck, RefreshCw, Loader2, CheckCircle2, Eye, EyeOff, Rocket } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { Badge } from '../../components/ui/badge';
import { cn } from '../../lib/utils';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import * as api from '../../lib/api';

const DatabaseNew: React.FC = () => {
  const [name, setName] = useState('');
  const [dbType, setDbType] = useState('mysql');
  const [host, setHost] = useState('');
  const [port, setPort] = useState('3306');
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [database, setDatabase] = useState('');
  const [scope, setScope] = useState('group');
  const [showPassword, setShowPassword] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<'success' | 'error' | null>(null);
  const [savedId, setSavedId] = useState<string | null>(null);
  const navigate = useNavigate();

  const connectionConfig = () => ({ host, port: Number(port) || undefined, user, password, database });

  const handleTest = async () => {
    if (!name.trim() || !host.trim()) { toast.error('请先填写名称和 Host'); return; }
    setTesting(true);
    setTestResult(null);
    try {
      // 真实测试连接:先创建数据源(如果还没创建),再真实拉一次表列表验证连通性
      let id = savedId;
      if (!id) {
        const created = await api.createDatasource({ name, db_type: dbType, connection_config: connectionConfig(), scope });
        id = created.id;
        setSavedId(id);
      }
      await api.listDatasourceTables(id!);
      setTestResult('success');
      toast.success('连接测试通过');
    } catch (e: any) {
      setTestResult('error');
      toast.error(e.message || '连接失败');
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    if (savedId) { navigate('/knowledge/database'); return; }
    if (!name.trim() || !host.trim()) { toast.error('请填写名称和 Host'); return; }
    try {
      await api.createDatasource({ name, db_type: dbType, connection_config: connectionConfig(), scope });
      toast.success('数据源已成功添加');
      navigate('/knowledge/database');
    } catch (e: any) {
      toast.error(e.message || '保存失败');
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex items-center gap-4 mb-2">
        <Button variant="ghost" size="icon" onClick={() => navigate('/knowledge/database')} className="rounded-full"><ArrowLeft className="h-5 w-5" /></Button>
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">新增数据源</h1>
          <p className="text-slate-500 text-sm">连接您的业务数据库，系统将自动分析表结构供上层调用。</p>
        </div>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardContent className="p-6 space-y-6">
          <div className="grid grid-cols-2 gap-6">
            <div className="space-y-2">
              <label className="text-sm font-semibold text-slate-700">数据源名称</label>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：武夷山门票主库" className="h-11 rounded-xl" />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-semibold text-slate-700">数据库类型</label>
              <select value={dbType} onChange={(e) => setDbType(e.target.value)} className="w-full h-11 px-4 rounded-xl border border-slate-200 bg-white text-sm outline-none focus:border-blue-500">
                <option value="mysql">MySQL</option><option value="selectdb">SelectDB</option><option value="starrocks">StarRocks</option>
                <option value="clickhouse">ClickHouse</option><option value="postgres">PostgreSQL</option>
              </select>
            </div>
          </div>

          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <label className="text-sm font-semibold text-slate-700 uppercase tracking-widest text-[10px]">连接配置</label>
              <Badge variant="outline" className="text-[10px] text-slate-400 gap-1"><ShieldCheck size={12} /> 密码加密存储</Badge>
            </div>
            <div className="grid grid-cols-4 gap-4">
              <div className="col-span-3 space-y-2">
                <label className="text-xs text-slate-500 font-bold">Host / Endpoint</label>
                <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="rm-xxxxx.mysql.rds.aliyuncs.com" className="h-11 rounded-xl font-mono text-sm" />
              </div>
              <div className="col-span-1 space-y-2">
                <label className="text-xs text-slate-500 font-bold">Port</label>
                <Input value={port} onChange={(e) => setPort(e.target.value)} className="h-11 rounded-xl font-mono text-sm" />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <label className="text-xs text-slate-500 font-bold">用户名</label>
                <Input value={user} onChange={(e) => setUser(e.target.value)} placeholder="read_only_user" className="h-11 rounded-xl" />
              </div>
              <div className="space-y-2 relative">
                <label className="text-xs text-slate-500 font-bold">密码</label>
                <div className="relative">
                  <Input type={showPassword ? 'text' : 'password'} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" className="h-11 rounded-xl pr-10" />
                  <button onClick={() => setShowPassword(!showPassword)} className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400">
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </div>
            </div>
            <div className="space-y-2">
              <label className="text-xs text-slate-500 font-bold">默认数据库名</label>
              <Input value={database} onChange={(e) => setDatabase(e.target.value)} placeholder="scenic_main_db" className="h-11 rounded-xl" />
            </div>
          </div>

          <div className="h-px bg-slate-100" />

          <div className="space-y-4">
            <label className="text-sm font-semibold text-slate-700">归属设置</label>
            <div className="flex gap-4">
              {[{ id: 'group', label: '集团级' }, { id: 'scenic', label: '景区级' }].map((s) => (
                <div key={s.id} onClick={() => setScope(s.id)} className={cn('flex-1 p-4 rounded-xl border flex items-center gap-3 cursor-pointer', scope === s.id ? 'border-blue-200 bg-blue-50' : 'bg-slate-50/50 border-slate-200')}>
                  <input type="radio" checked={scope === s.id} onChange={() => setScope(s.id)} className="h-4 w-4 text-blue-600" />
                  <label className="flex-1 cursor-pointer"><div className="text-sm font-bold">{s.label}</div></label>
                </div>
              ))}
            </div>
          </div>

          <Button onClick={handleTest} disabled={testing} className={cn('w-full h-12 rounded-xl font-bold gap-2 transition-all', testResult === 'success' ? 'bg-emerald-500 hover:bg-emerald-600 shadow-emerald-100 shadow-lg' : 'bg-slate-800 hover:bg-slate-900')}>
            {testing ? <Loader2 className="h-5 w-5 animate-spin" /> : testResult === 'success' ? <CheckCircle2 className="h-5 w-5" /> : <RefreshCw size={18} />}
            {testing ? '正在验证连接...' : testResult === 'success' ? '连接测试通过（已保存）' : '真实测试数据库连接'}
          </Button>
        </CardContent>
      </Card>

      <div className="flex gap-4">
        <Button variant="ghost" className="flex-1 h-12 rounded-xl font-bold text-slate-500" onClick={() => navigate('/knowledge/database')}>取消</Button>
        <Button className="flex-1 h-12 rounded-xl bg-blue-600 hover:bg-blue-700 text-white font-bold shadow-lg shadow-blue-100 gap-2" onClick={handleSave}>
          <Rocket size={18} /> {savedId ? '完成' : '确认并保存数据源'}
        </Button>
      </div>
    </div>
  );
};

export default DatabaseNew;
