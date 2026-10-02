/**
 * 运行模式的**展示名**。
 *
 * 内部编码一直是 L0 / L1 / L2(落库、接口、配置里都是它,不要改)——
 * 但给人看的时候从 L0 起数很别扭:运营看到「L0」第一反应是「第 0 级是什么意思」,
 * 而 L2 又常被误读成「中间那一档」,实际它是最高的一档。
 *
 * 展示层统一右移一位、并用一眼能懂的两个字:
 *   L0(直接调用)   → L1 直调    不调工具,一次生成
 *   L1(固定编排)   → L2 固排    按设计时定死的顺序调工具
 *   L2(智能体循环) → L3 智能    模型自己决定调什么、调几次
 *
 * 放在一处是刻意的:这层映射散在各个页面里改一次要找七八处,漏掉的那处就是
 * 「同一个任务在两个页面显示成不同模式」—— 比不改更糟。
 */
export interface ModeLabel {
  /** 展示编号,如 L2 */
  code: string;
  /** 两字短名,如 固排 */
  short: string;
  /** 展示全名,如 L2 固排 */
  label: string;
  /** 一句话说明它是什么 */
  desc: string;
}

export const MODE_LABELS: Record<string, ModeLabel> = {
  L0: { code: 'L1', short: '直调', label: 'L1 直调',
        desc: '不调工具,按提示词一次生成结果。最快、最省,适合口径固定的问答与文案。' },
  L1: { code: 'L2', short: '固排', label: 'L2 固排',
        desc: '按设计时定死的顺序调用工具,流程可预期。适合步骤明确的业务(先查天气→再查客流→出建议)。' },
  L2: { code: 'L3', short: '智能', label: 'L3 智能',
        desc: '模型自己决定调哪个工具、调几次,能按中间结果改变策略。适合步骤不固定的复杂任务。' },
};

/** 内部码 → 展示全名(未知码原样返回,不要吞掉) */
export function modeLabel(mode: string | null | undefined): string {
  return MODE_LABELS[(mode || '').toUpperCase()]?.label || (mode || '');
}

/** 内部码 → 展示编号(L0→L1) */
export function modeCode(mode: string | null | undefined): string {
  return MODE_LABELS[(mode || '').toUpperCase()]?.code || (mode || '');
}

/** 内部码 → 两字短名 */
export function modeShort(mode: string | null | undefined): string {
  return MODE_LABELS[(mode || '').toUpperCase()]?.short || (mode || '');
}
