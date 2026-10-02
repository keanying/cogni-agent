/**
 * RSA 密码加密工具。
 *
 * 前端用公钥加密密码，后端用私钥解密，防止密码在 HTTP 传输和日志中明文出现。
 * 不等同于 HTTPS，不防中间人主动篡改。
 *
 * 算法：RSA-OAEP with SHA-256（与后端 Python cryptography 库的 OAEP + SHA256 对应）。
 */

const PUBLIC_KEY_CACHE_KEY = "db_rsa_public_key";
let cachedPublicKey: string | null = null;

/**
 * 获取 RSA 公钥。优先内存缓存，其次 localStorage，最后从后端 API 获取。
 * 浏览器端用 SubtleCrypto API，无需额外依赖。
 */
export async function getPublicKey(): Promise<string> {
  if (cachedPublicKey) return cachedPublicKey;

  // localStorage 缓存：页面刷新后避免重复请求。
  const stored = localStorage.getItem(PUBLIC_KEY_CACHE_KEY);
  if (stored) {
    cachedPublicKey = stored;
    return cachedPublicKey;
  }

  const res = await fetch("/api/admin/auth/public-key");
  if (!res.ok) throw new Error("获取加密公钥失败，请刷新重试");
  const data = await res.json();
  // 后端 ApiResponseMiddleware 统一包裹为 {code, message, data, trace_id}，
  // public_key 在 data.data 里；兼容旧版直接返回 {public_key, ...} 的结构。
  const pk: string = data?.data?.public_key ?? data?.public_key;
  if (!pk) throw new Error("服务端未返回公钥");
  cachedPublicKey = pk;
  localStorage.setItem(PUBLIC_KEY_CACHE_KEY, pk);
  return pk;
}

/**
 * 将 PEM 格式公钥转为 CryptoKey（SubtleCrypto 所需格式）。
 */
async function importPublicKey(pem: string): Promise<CryptoKey> {
  const pemBase64 = pem
    .replace("-----BEGIN PUBLIC KEY-----", "")
    .replace("-----END PUBLIC KEY-----", "")
    .replace(/\s/g, "");
  const binaryKey = Uint8Array.from(atob(pemBase64), (c) => c.charCodeAt(0));
  return crypto.subtle.importKey(
    "spki",
    binaryKey,
    { name: "RSA-OAEP", hash: "SHA-256" },
    false,
    ["encrypt"]
  );
}

/**
 * 用 RSA 公钥加密明文密码，返回 Base64 编码的密文字符串。
 */
export async function encryptPassword(plainText: string): Promise<string> {
  const pem = await getPublicKey();
  const publicKey = await importPublicKey(pem);
  const encoder = new TextEncoder();
  const encrypted = await crypto.subtle.encrypt(
    { name: "RSA-OAEP" },
    publicKey,
    encoder.encode(plainText)
  );
  return btoa(String.fromCharCode(...new Uint8Array(encrypted)));
}

/** 清除公钥缓存（登出时调用）。 */
export function clearPublicKeyCache() {
  cachedPublicKey = null;
  localStorage.removeItem(PUBLIC_KEY_CACHE_KEY);
}
