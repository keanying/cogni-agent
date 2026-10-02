/**
 * 页面展示用的密钥打码(只影响显示,不改数据)。
 *
 * 现场:工具详情页「请求头」直接显示了完整的数据平台 key(xpftkey = pfk_…)。
 * 工具配置里应当写 `{{data_platform_key}}` 这类占位、由服务端调用时注入;
 * 写成明文的(老数据、手工粘贴)至少不要在页面上整把摊出来,并提示改成占位。
 */

const SECRET_KEY_RE = /(key|token|secret|passw|pwd|auth|xpft|signature|credential)/i;
const SECRET_VALUE_RE = /^(pfk_|sk-|sk_|ak_|Bearer\s+\S{12,})/i;

/** 占位符(`{{...}}`)不是密钥;字段名像密钥、或值本身带已知密钥前缀,且足够长,才算明文密钥 */
export function looksLikeSecret(name: string, value: unknown): boolean {
  if (typeof value !== 'string') return false;
  const v = value.trim();
  if (!v || v.includes('{{') || /^https?:\/\//i.test(v)) return false;
  if (SECRET_VALUE_RE.test(v)) return true;
  return SECRET_KEY_RE.test(name) && v.length >= 12;
}

/** 露首 4 位 + 末 4 位,中间打码 */
export function maskSecret(name: string, value: string): string {
  if (!looksLikeSecret(name, value)) return value;
  const v = value.trim();
  return `${v.slice(0, 4)}••••••${v.slice(-4)}`;
}

/** 递归打码(type_config 原文展示用) */
export function maskSecretsDeep(obj: any, name = ''): any {
  if (Array.isArray(obj)) return obj.map((x) => maskSecretsDeep(x, name));
  if (obj && typeof obj === 'object') {
    return Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, maskSecretsDeep(v, k)]));
  }
  return typeof obj === 'string' ? maskSecret(name, obj) : obj;
}
