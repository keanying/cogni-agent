import React, { useEffect, useRef, useState } from 'react';
// 与 APP(uni)同一份 ECharts 6.1.0 构建(src/static/js/echarts.min.js,Apache-2.0)。
//
// 为什么管理端也要 ECharts:服务端给每张图配的是 ECharts option,APP 直接画;
// 管理端原来装的是 recharts,只能拿一份中性行列表**另画一遍** —— 双轴、极值标注、
// 热力/日历/箱线/旭日/河流这些 recharts 画不了,后台只能退成一张表。
// 同一份 option 两端各画各的,迟早出现「APP 上是这样、后台是那样」。
//
// 用 `?url` 按需加载而不是 npm 依赖:不进主包(1MB),只在第一次出现图表时拉一次;
// 地址由 Vite 按 base 解析,部署在子路径下也对。
import echartsUrl from '../../vendor/echarts-6.1.0.min.js?url';

declare global { interface Window { echarts?: any } }

let loading: Promise<any> | null = null;

/** 全页只加载一次;失败时下次还能重试(不把失败的 Promise 缓存住)。 */
export function loadECharts(): Promise<any> {
  if (window.echarts) return Promise.resolve(window.echarts);
  if (!loading) {
    loading = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = echartsUrl;
      s.async = true;
      s.onload = () => (window.echarts ? resolve(window.echarts) : reject(new Error('echarts 未注册')));
      s.onerror = () => reject(new Error('echarts 加载失败'));
      document.head.appendChild(s);
    }).catch((e) => { loading = null; throw e; });
  }
  return loading;
}

/**
 * 直接渲染服务端给的 ECharts option。
 * 加载或渲染失败时调用 onFail —— 调用方退回原来的 recharts / 数据表,绝不留一块空白画布。
 */
const EChartsView: React.FC<{ option: any; height?: number; onFail?: (err: Error) => void }> = (
  { option, height = 300, onFail }) => {
  const box = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let chart: any = null;
    let ro: ResizeObserver | null = null;
    let disposed = false;
    loadECharts().then((echarts) => {
      if (disposed || !box.current) return;
      try {
        // locale ZH:读屏用的图表描述(aria)和内置文案用中文
        chart = echarts.init(box.current, null, { renderer: 'canvas', locale: 'ZH' });
        chart.setOption(option, true);
        setReady(true);
        ro = new ResizeObserver(() => chart && chart.resize());
        ro.observe(box.current);
      } catch (e: any) {
        onFail?.(e instanceof Error ? e : new Error(String(e)));
      }
    }).catch((e) => { if (!disposed) onFail?.(e); });
    return () => {
      disposed = true;
      ro?.disconnect();
      chart?.dispose();
    };
  }, [option]);

  return (
    <div className="relative w-full" style={{ height }}>
      <div ref={box} className="h-full w-full" />
      {!ready && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-slate-300">图表加载中…</div>
      )}
    </div>
  );
};

export default EChartsView;
