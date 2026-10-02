import React, { useEffect, useRef, useState } from 'react';
import {
  User, Lock, Eye, EyeOff, ArrowRight, Loader2, CheckCircle2, MessageSquare,
} from 'lucide-react';
import * as api from '../lib/api';
import { toast } from 'sonner';

/**
 * 登录页。
 *
 * 视觉:深色玻璃拟态 + 3D 悬浮卡片 + 扫描线 + 鼠标视差。
 * 这里刻意没有用 Tailwind 的主题色变量,而是直接写十六进制 —— 这套配色只服务于登录页,
 * 塞进全局 theme 会污染其余 30+ 个页面的语义色。
 *
 * 两个容易被忽略的可访问性处理:
 * - 尊重 `prefers-reduced-motion`:悬浮动画、扫描线、鼠标视差全部关闭。前庭功能敏感的
 *   用户看这种持续漂浮 + 跟随倾斜的界面会不适,而登录页是**必经**页面,躲不开。
 * - 视差用 rAF 节流:mousemove 触发频率很高,每次都直接改 transform 会掉帧。
 */

const REDUCED_MOTION =
  typeof window !== 'undefined' &&
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

const Login: React.FC<{ onSuccess: () => void }> = ({ onSuccess }) => {
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [smsCode, setSmsCode] = useState('');
  const [remember, setRemember] = useState(true);

  const [requireSms, setRequireSms] = useState(false);
  const [policyLoaded, setPolicyLoaded] = useState(false);
  const [version, setVersion] = useState('');

  const [busy, setBusy] = useState(false);
  const [succeeded, setSucceeded] = useState(false);
  const [smsBusy, setSmsBusy] = useState(false);
  const [smsCooldown, setSmsCooldown] = useState(0);
  const [videoFailed, setVideoFailed] = useState(false);

  const cardRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number | null>(null);

  // 登录策略决定要不要展示验证码区域(登录前就要知道,所以这个接口不需要鉴权)
  useEffect(() => {
    api.getLoginPolicy()
      .then((p) => setRequireSms(p.require_sms_on_first_login))
      .catch(() => setRequireSms(false))   // 拿不到策略时按"不需要"处理,不挡住登录
      .finally(() => setPolicyLoaded(true));
    api.getHealth().then((h) => setVersion(h.version)).catch(() => setVersion(''));
  }, []);

  // 鼠标视差(rAF 节流;reduced-motion 下完全不注册监听)
  useEffect(() => {
    if (REDUCED_MOTION) return;
    const onMove = (e: MouseEvent) => {
      if (rafRef.current !== null) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        const el = cardRef.current;
        if (!el) return;
        const rx = (window.innerHeight / 2 - e.clientY) / 55;
        const ry = (e.clientX - window.innerWidth / 2) / 55;
        el.style.transform = `rotateX(${rx}deg) rotateY(${ry}deg) translateY(-10px)`;
      });
    };
    const onLeave = () => {
      const el = cardRef.current;
      if (el) el.style.transform = 'rotateX(0deg) rotateY(0deg) translateY(0px)';
    };
    window.addEventListener('mousemove', onMove);
    document.body.addEventListener('mouseleave', onLeave);
    return () => {
      window.removeEventListener('mousemove', onMove);
      document.body.removeEventListener('mouseleave', onLeave);
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  const handleSendSms = async () => {
    if (!phone.trim()) { toast.error('请先输入账户或手机号'); return; }
    setSmsBusy(true);
    try {
      await api.sendSmsCode(phone.trim(), 'login');
      toast.success('验证码已发送');
      setSmsCooldown(60);
      const timer = setInterval(
        () => setSmsCooldown((s) => { if (s <= 1) { clearInterval(timer); return 0; } return s - 1; }),
        1000,
      );
    } catch (e: any) {
      toast.error(e.message || '验证码发送失败');
    } finally {
      setSmsBusy(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || succeeded) return;
    if (!phone.trim() || !password) { toast.error('请输入账户和密码'); return; }
    setBusy(true);
    try {
      const data = await api.login(phone.trim(), password, smsCode || undefined, remember);
      // 成功态短暂停留一下再跳转,否则按钮从「验证中」瞬间消失,人来不及感知结果
      setSucceeded(true);
      if (data.must_change_password) {
        toast.warning('当前使用的是初始密码,请尽快在「系统中心 · 成员与角色」中修改');
      }
      setTimeout(onSuccess, 550);
    } catch (err: any) {
      // 后端会区分「账号/手机号或密码错误」和「需要短信验证码」,原样透出,
      // 不要用一句笼统的"登录失败"盖掉——那会让人反复试密码却不知道其实是缺验证码
      toast.error(err.message || '登录失败');
      setBusy(false);
    }
  };

  const notWired = (name: string) => () =>
    toast.info(`「${name}」尚未接入。忘记密码可让管理员执行 seed_superadmin.py --reset-password 重置`);

  return (
    <div className="relative min-h-screen overflow-hidden bg-[#101415] font-['Inter',sans-serif] text-[#e0e3e5]">
      {/* ── 背景层 ── */}
      <div className="fixed inset-0 z-0 overflow-hidden">
        {/* 视频槽位:替换 web/public/media/login-bg.webm 即可,无需改代码;
            文件缺失时 onError 隐藏它,只保留下面的渐变与光晕,不会裂图 */}
        {!videoFailed && (
          <video
            className="absolute inset-0 h-full w-full object-cover opacity-40 mix-blend-luminosity"
            src="/media/login-bg.webm"
            poster="/media/login-bg.jpg"
            autoPlay loop muted playsInline preload="auto"
            onError={() => setVideoFailed(true)}
            aria-hidden="true"
          />
        )}
        {/* 无视频时的兜底氛围:两团青紫光晕 */}
        <div className="absolute -left-40 -top-40 h-[520px] w-[520px] rounded-full bg-[#00dbe7] opacity-[0.07] blur-[120px]" aria-hidden="true" />
        <div className="absolute -bottom-40 -right-32 h-[560px] w-[560px] rounded-full bg-[#7000ff] opacity-[0.10] blur-[130px]" aria-hidden="true" />
        {/* 径向压暗:保证中央文字始终可读,不受背景素材明暗影响 */}
        <div className="absolute inset-0" aria-hidden="true"
             style={{ background: 'radial-gradient(circle at center, transparent 0%, rgba(16,20,21,0.82) 100%)' }} />
      </div>

      <main className="relative z-10 mx-auto flex min-h-screen w-full max-w-md flex-col items-center justify-center px-6">
        <header className="mb-8 text-center">
          <h1
            className="font-['Space_Grotesk',sans-serif] text-[44px] font-bold leading-[1.1] tracking-tight text-[#00dbe7]"
            style={{
              textShadow: '0 1px 0 #00dbe7, 0 2px 0 #006a71, 0 3px 0 #004f54, 0 4px 0 #002022, 0 10px 20px rgba(0,0,0,0.5)',
              transform: REDUCED_MOTION ? undefined : 'perspective(500px) rotateX(10deg)',
            }}
          >
            景区数字大脑
          </h1>
          <p className="mt-4 font-['JetBrains_Mono',monospace] text-[12px] uppercase tracking-[0.2em] text-[#00f2ff] opacity-60">
            Scenic Area Digital Brain System
          </p>
        </header>

        <div className="w-full" style={{ perspective: 1200 }}>
          <div
            ref={cardRef}
            className={`relative overflow-hidden rounded-2xl p-9 ${REDUCED_MOTION ? '' : 'animate-[loginFloat_6s_ease-in-out_infinite]'}`}
            style={{
              transformStyle: 'preserve-3d',
              transition: 'transform 260ms cubic-bezier(0.4,0,0.2,1)',
              background: 'rgba(255,255,255,0.03)',
              backdropFilter: 'blur(25px)',
              WebkitBackdropFilter: 'blur(25px)',
              border: '1px solid rgba(255,255,255,0.1)',
              boxShadow: '0 25px 50px -12px rgba(0,0,0,0.5), inset 0 1px 1px rgba(255,255,255,0.1), inset 0 0 20px rgba(0,242,255,0.05)',
            }}
          >
            {!REDUCED_MOTION && (
              <div aria-hidden="true"
                   className="pointer-events-none absolute left-0 h-[2px] w-full opacity-30 animate-[loginScan_4s_linear_infinite]"
                   style={{ background: 'linear-gradient(to right, transparent, #00f2ff, transparent)' }} />
            )}

            <form className="space-y-7" onSubmit={handleSubmit}>
              <Field icon={<User size={14} />} label="账户或手机号">
                <input
                  value={phone} onChange={(e) => setPhone(e.target.value)}
                  autoComplete="username" placeholder="请输入账户或手机号"
                  className="w-full border-none bg-transparent text-[#e1fdff] placeholder:text-[#b9cacb]/40 focus:outline-none focus:ring-0"
                />
              </Field>

              <Field icon={<Lock size={14} />} label="密码">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={password} onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password" placeholder="请输入密码"
                  className="w-full border-none bg-transparent text-[#e1fdff] placeholder:text-[#b9cacb]/40 focus:outline-none focus:ring-0"
                />
                <button type="button" onClick={() => setShowPassword((v) => !v)}
                        aria-label={showPassword ? '隐藏密码' : '显示密码'}
                        className="text-[#b9cacb]/60 transition-colors hover:text-[#00f2ff]">
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </Field>

              {requireSms && (
                <Field icon={<MessageSquare size={14} />} label="短信验证码(首次登录必填)">
                  <input
                    value={smsCode} onChange={(e) => setSmsCode(e.target.value)}
                    inputMode="numeric" autoComplete="one-time-code" placeholder="6 位验证码"
                    className="w-full border-none bg-transparent text-[#e1fdff] placeholder:text-[#b9cacb]/40 focus:outline-none focus:ring-0"
                  />
                  <button type="button" onClick={handleSendSms} disabled={smsBusy || smsCooldown > 0}
                          className="shrink-0 whitespace-nowrap font-['JetBrains_Mono',monospace] text-[11px] uppercase tracking-widest text-[#00f2ff] transition-opacity hover:opacity-80 disabled:opacity-40">
                    {smsCooldown > 0 ? `${smsCooldown}s` : '获取'}
                  </button>
                </Field>
              )}

              <div className="flex items-center justify-between text-[13px] text-[#b9cacb]/60">
                <label className="flex cursor-pointer items-center gap-2 transition-colors hover:text-[#e0e3e5]">
                  <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)}
                         className="rounded border-white/20 bg-white/10 text-[#7000ff] focus:ring-[#7000ff] focus:ring-offset-0" />
                  记住我
                </label>
                <button type="button" onClick={notWired('忘记密码')} className="transition-colors hover:text-[#00f2ff]">
                  忘记密码?
                </button>
              </div>

              <button
                type="submit" disabled={busy || succeeded}
                className="group flex w-full items-center justify-center gap-3 rounded-xl py-4 font-['Space_Grotesk',sans-serif] text-[20px] font-semibold text-[#292f48] transition-all duration-300 disabled:cursor-not-allowed"
                style={{
                  background: succeeded ? 'linear-gradient(135deg,#16a34a 0%,#22c55e 100%)'
                                        : 'linear-gradient(135deg,#7000ff 0%,#00dbe7 100%)',
                  color: succeeded ? '#fff' : '#292f48',
                  opacity: busy ? 0.8 : 1,
                }}
                onMouseEnter={(e) => { if (!busy && !succeeded) { e.currentTarget.style.boxShadow = '0 0 30px rgba(0,242,255,0.6)'; e.currentTarget.style.transform = 'translateY(-2px)'; } }}
                onMouseLeave={(e) => { e.currentTarget.style.boxShadow = 'none'; e.currentTarget.style.transform = 'none'; }}
              >
                {succeeded ? (<><CheckCircle2 size={22} />成功</>)
                  : busy ? (<><Loader2 size={22} className="animate-spin" />验证中…</>)
                  : (<>登录<ArrowRight size={22} className="transition-transform group-hover:translate-x-1" /></>)}
              </button>
            </form>

            {/* 系统状态条:版本号取自后端 /health,不写死 */}
            <div className="mt-9 flex items-center justify-between border-t border-white/10 pt-6 font-['JetBrains_Mono',monospace] text-[10px] uppercase tracking-widest opacity-40">
              <div className="flex items-center gap-2">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#00f2ff] shadow-[0_0_8px_#00f2ff]" />
                System Active
              </div>
              <div>{version ? `V${version}` : '—'}</div>
            </div>
          </div>
        </div>

        {policyLoaded && !requireSms && (
          <p className="mt-6 text-center font-['JetBrains_Mono',monospace] text-[10px] tracking-wider text-[#b9cacb]/35">
            当前部署未开启短信验证,账户 + 密码即可登录
          </p>
        )}

        <footer className="mt-9 flex items-center gap-6 font-['JetBrains_Mono',monospace] text-[11px] tracking-widest text-[#b9cacb]/40">
          {['隐私协议', '技术支持', '管理员中心'].map((label, i) => (
            <React.Fragment key={label}>
              {i > 0 && <span className="h-1 w-1 rounded-full bg-white/20" />}
              <button onClick={notWired(label)} className="transition-colors hover:text-[#00f2ff]">{label}</button>
            </React.Fragment>
          ))}
        </footer>
      </main>
    </div>
  );
};

/** 带图标标签 + 聚焦发光的输入行。抽出来是因为三个字段的外壳完全一样。 */
const Field: React.FC<{ icon: React.ReactNode; label: string; children: React.ReactNode }> = ({ icon, label, children }) => (
  <div className="space-y-2">
    <label className="flex items-center gap-2 font-['JetBrains_Mono',monospace] text-[12px] uppercase tracking-[0.1em] text-[#b9cacb]">
      {icon}{label}
    </label>
    <div className="flex items-center gap-3 rounded-lg border-b border-white/20 bg-white/5 px-4 py-3 transition-all focus-within:border-[#00f2ff] focus-within:shadow-[0_0_15px_rgba(0,242,255,0.2)]">
      {children}
    </div>
  </div>
);

export default Login;
