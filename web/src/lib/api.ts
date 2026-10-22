/**
 * 统一后端 API 客户端。
 *
 * 约定：BASE = "/api"，所有 path 写法为 "/admin/xxx" 或 "/v1/xxx"（不带 "/api" 前缀）。
 * request() 自动拼成 "/api/admin/xxx"，即为后端实际路由。
 *
 * 开发环境 vite.config.ts 把 /api 代理到 http://localhost:8000；
 * 生产环境 web/dist 由后端同源托管，/api 直接命中同一个 FastAPI 进程。
 */

const BASE = (import.meta as any).env?.VITE_API_BASE_URL || "/api";

const TOKEN_KEY = "db_access_token";
const REFRESH_KEY = "db_refresh_token";
// 两处都读:勾了「记住我」存 localStorage(关浏览器仍在),没勾存 sessionStorage
// (关标签页即失效)。以前不管勾没勾都写 localStorage,那个勾选框是纯装饰。
let accessToken: string | null =
  localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY);
// access_token 只活 60 分钟,refresh_token 活 14 天。以前登录拿到 refresh_token
// 就直接丢掉了 —— 于是用满一小时必被踢回登录页,哪怕人一直在操作。存下来,
// 由 request() 在 401 时静默换新,用户无感。
let refreshToken: string | null =
  localStorage.getItem(REFRESH_KEY) || sessionStorage.getItem(REFRESH_KEY);

function authHeaders(): Record<string, string> {
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {};
}

/** 会话彻底失效(refresh 也换不动)时通知外层跳登录页,避免各页面各写一套跳转。 */
let onSessionExpired: (() => void) | null = null;
export function setSessionExpiredHandler(fn: (() => void) | null) { onSessionExpired = fn; }

// 单飞:并发请求同时撞到 401 时只发一次 refresh,其余等同一个 Promise。
// 不做单飞的话,滚动刷新会让先到的那个把后到的 refresh_token 作废,
// 结果是「刷新把自己刷登出了」。
let refreshing: Promise<boolean> | null = null;

async function doRefresh(): Promise<boolean> {
  if (!refreshToken) return false;
  try {
    const res = await fetch(`${BASE}/admin/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) return false;
    const body = await res.json();
    const data = body && typeof body === "object" && "data" in body ? body.data : body;
    if (!data?.access_token) return false;
    // 续期沿用原来的存储位置:记住我的留 localStorage,没记住的留 sessionStorage
    setToken(data.access_token, !!localStorage.getItem(TOKEN_KEY), data.refresh_token ?? refreshToken);
    return true;
  } catch {
    return false;
  }
}

/** 401 后尝试续期;成功返回 true,调用方重放原请求。 */
async function tryRefresh(): Promise<boolean> {
  if (!refreshing) refreshing = doRefresh().finally(() => { refreshing = null; });
  const ok = await refreshing;
  if (!ok) { setToken(null, true, null); onSessionExpired?.(); }
  return ok;
}

/** JWT 的过期时间(秒)。解不出来就返回 0,按「不知道」处理,交给 401 兜底。 */
function tokenExp(token: string | null): number {
  if (!token) return 0;
  try {
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    return typeof payload?.exp === 'number' ? payload.exp : 0;
  } catch { return 0; }
}

// 提前这么久就换新令牌。只靠 401 兜底有两个窟窿:SSE 连上之后中途过期不会再有 401 可撞,
// 以及每次过期都要先失败一次再重放(长任务里那一次失败可能就是丢一整轮)。
const REFRESH_AHEAD_SECONDS = 120;

/** 请求发出前:令牌快过期就先换一张。没有 refresh_token 或解不出 exp 时什么都不做。 */
async function ensureFreshToken(): Promise<void> {
  if (!refreshToken || !accessToken) return;
  const exp = tokenExp(accessToken);
  if (!exp) return;
  if (exp - Date.now() / 1000 > REFRESH_AHEAD_SECONDS) return;
  if (!refreshing) refreshing = doRefresh().finally(() => { refreshing = null; });
  await refreshing;   // 换不动也继续发,让服务端用 401 给出权威结论
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/**
 * 统一请求函数。后端所有接口通过 ApiResponseMiddleware 返回信封格式：
 *   { code: 0, message: "success", data: {...}, trace_id: "..." }
 * 本函数自动解包信封：
 *   - code !== 0 时抛出 ApiError，message 来自后端
 *   - code === 0 时只返回 data 部分，调用方直接拿业务数据
 * 对齐后端 DEVELOPMENT.md §4.1 核心准则。
 */
async function request<T>(path: string, options: RequestInit = {}, absolute = false, retried = false): Promise<T> {
  if (!path.startsWith("/admin/auth/login") && !path.startsWith("/admin/auth/refresh")) {
    await ensureFreshToken();
  }
  const res = await fetch(`${absolute ? "" : BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) },
  });
  // 401 = access_token 过期(或被改密作废)。先拿 refresh_token 换一张再重放一次;
  // 换不动才是真的掉线。登录/续期接口本身不参与,否则密码错了会绕进死循环。
  if (res.status === 401 && !retried && !path.startsWith("/admin/auth/login")
      && !path.startsWith("/admin/auth/refresh")) {
    if (await tryRefresh()) return request<T>(path, options, absolute, true);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(body.message || `请求失败 (${res.status})`, res.status);
  }
  if (res.status === 204) return undefined as T;
  const body = await res.json();
  // 解包 ApiResponse 信封：有 code + data 两个字段即为信封格式
  if (body && typeof body === "object" && "code" in body && "data" in body) {
    if (body.code !== 0) {
      throw new ApiError(body.message || `业务错误 (code=${body.code})`, res.status);
    }
    return body.data as T;
  }
  return body as T;
}

export function isAuthed(): boolean {
  // access_token 过期但 refresh_token 还在 => 仍算已登录:进页面后第一个请求
  // 会自动续期,不该因为「本地这张 access 过期了」就先弹登录页。
  return !!accessToken || !!refreshToken;
}

export function setToken(token: string | null, remember = true, refresh?: string | null) {
  accessToken = token;
  localStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
  if (token) (remember ? localStorage : sessionStorage).setItem(TOKEN_KEY, token);
  if (refresh !== undefined) {
    refreshToken = refresh;
    localStorage.removeItem(REFRESH_KEY);
    sessionStorage.removeItem(REFRESH_KEY);
    if (refresh) (remember ? localStorage : sessionStorage).setItem(REFRESH_KEY, refresh);
  }
}

// ── 登录 ──
export interface LoginPolicy { require_sms_on_first_login: boolean; sms_provider: string }
/** 登录前拉取:决定登录页要不要展示验证码区域。无需鉴权。 */
export const getLoginPolicy = () => request<LoginPolicy>("/admin/auth/login-policy");

export async function login(phone: string, password: string, smsCode?: string, remember = true) {
  const { encryptPassword } = await import("./crypto");
  const encrypted = await encryptPassword(password);
  const data = await request<{ access_token: string; refresh_token: string; must_change_password: boolean }>(
    "/admin/auth/login",
    { method: "POST", body: JSON.stringify({ phone, password: encrypted, sms_code: smsCode, rsa_encrypt: true }) }
  );
  setToken(data.access_token, remember, data.refresh_token);
  return data;
}

export const sendSmsCode = (phone: string, purpose: "login" | "new_device" | "reset_password" = "login") =>
  request(`/admin/auth/sms/send?phone=${encodeURIComponent(phone)}&purpose=${purpose}`, { method: "POST" });

export function logout() {
  setToken(null, true, null);
}

export interface HealthInfo { status: string; service: string; version: string }
/** 用于登录页底部展示真实版本号,而不是写死一个字符串。 */
export const getHealth = () => request<HealthInfo>("/health", {}, true);

export interface Me {
  id: string; name: string; account: string; phone: string;
  role: string; scenic_id: string | null; position: string | null;
}
export const getMe = () => request<Me>("/admin/auth/me");

export interface Permissions {
  role: string;
  menus: Record<string, boolean>;        // admin: dashboard/console/... | app: data_overview/ticket_manage/...
  capabilities: Record<string, boolean>; // tool.write / agent.publish / approval.approve ...（仅 admin 域有值）
}
/** 后端 Casbin 策略算出的能力清单。前端据此隐藏菜单与按钮,
 *  避免在前端再硬编码一份角色白名单(那份和后端判断迟早会漂移)。
 *  注意:这只是体验优化,真正的拦截在服务端。 */
export const getPermissions = (domain: "admin" | "app" = "admin") =>
  request<Permissions>(`/admin/auth/permissions?domain=${domain}`);

// ── 用户管理(用户中心·成员与角色) ──
export interface MemberListParams {
  keyword?: string; role?: string; scenic_id?: string;
  is_active?: boolean; page?: number; page_size?: number;
}
export const listUsers = (paramsOrKeyword?: MemberListParams | string) => {
  const qs = new URLSearchParams();
  if (typeof paramsOrKeyword === 'string') {
    if (paramsOrKeyword) qs.set('keyword', paramsOrKeyword);
  } else if (paramsOrKeyword) {
    Object.entries(paramsOrKeyword).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') qs.set(k, String(v)); });
  }
  return request<any>(`/admin/users${qs.toString() ? '?' + qs.toString() : ''}`);
};

export interface UserListParams {
  keyword?: string; role?: string; scenic_id?: string;
  is_active?: boolean; page?: number; page_size?: number;
}
export interface PagedUsers {
  items: any[]; total: number; page: number; page_size: number; total_pages: number;
}
export const listUsersPaged = (params: UserListParams) => {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') qs.set(k, String(v)); });
  return request<PagedUsers>(`/admin/users/paged?${qs.toString()}`);
};
export const createUser = (payload: any) =>
  request<any>("/admin/users",
    { method: "POST", body: JSON.stringify(payload) });
export const updateUser = (id: string, payload: object) => request<any>(`/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
export const deleteUser = (id: string) => request<any>(`/admin/users/${id}`, { method: "DELETE" });
export const resetPassword = (userId: string, password?: string) =>
  request<any>(
    `/admin/users/${userId}/reset-password`,
    { method: "POST", body: JSON.stringify({ password: password || null }) });
export const listUsersByRole = () => request<Record<string, any[]>>("/admin/users/by-role");
export const batchUpdateRole = (userIds: string[], role: string) =>
  request<any>("/admin/users/batch-role",
    { method: "POST", body: JSON.stringify({ user_ids: userIds, role }) });

// ── 角色与权限 ──
export interface RoleInfo { role: string; label: string; permissions?: Record<string, any> }
export const listRoles = (withPermissions = false, domain = 'admin') =>
  request<RoleInfo[]>(`/admin/roles?with_permissions=${withPermissions}&domain=${domain}`);
export const listPermissions = (domain = 'admin') => request<any>(`/admin/permissions?domain=${domain}`);
export const createPermission = (sub: string, obj: string, act: string, domain = 'admin') =>
  request("/admin/permissions", { method: "POST", body: JSON.stringify({ sub, obj, act, domain }) });
export const deletePermission = (sub: string, obj: string, act: string, domain = 'admin') =>
  request("/admin/permissions", { method: "DELETE", body: JSON.stringify({ sub, obj, act, domain }) });

// ── 用户中心：模型厂商 & 模型配置 ──
export interface ModelProviderItem {
  id: string; name: string; provider_type: string; base_url: string;
  description: string; is_enabled: boolean; api_key?: string;
}
export interface ModelConfigItem {
  id: string; name: string; provider_id: string; model_name: string;
  description: string; is_enabled: boolean; is_default: boolean;
  extra_params?: Record<string, unknown>; provider_name?: string;
}
export const listModelProviders = (params?: { keyword?: string; is_enabled?: boolean; page_size?: number }) => {
  const qs = new URLSearchParams();
  if (params?.keyword) qs.set('keyword', params.keyword);
  if (params?.is_enabled !== undefined) qs.set('is_enabled', String(params.is_enabled));
  if (params?.page_size !== undefined) qs.set('page_size', String(params.page_size));
  return request<{ items: any[] }>(`/admin/model-providers${qs.toString() ? '?' + qs.toString() : ''}`);
};
export const createModelProvider = (payload: object) =>
  request<any>("/admin/model-providers", { method: "POST", body: JSON.stringify(payload) });
export const updateModelProvider = (id: string, payload: object) =>
  request(`/admin/model-providers/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
export const deleteModelProvider = (id: string) =>
  request(`/admin/model-providers/${id}`, { method: "DELETE" });
export const listModelConfigsV2 = (params?: { keyword?: string; provider_id?: string; is_enabled?: boolean }) => {
  const qs = new URLSearchParams();
  if (params?.keyword) qs.set('keyword', params.keyword);
  if (params?.provider_id) qs.set('provider_id', params.provider_id);
  if (params?.is_enabled !== undefined) qs.set('is_enabled', String(params.is_enabled));
  return request<{ items: any[] }>(`/admin/model-configs${qs.toString() ? '?' + qs.toString() : ''}`);
};
export const createModelConfig = (payload: object) =>
  request<any>("/admin/model-configs", { method: "POST", body: JSON.stringify(payload) });
export const updateModelConfig = (id: string, payload: object) =>
  request(`/admin/model-configs/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
export const deleteModelConfig = (id: string) =>
  request(`/admin/model-configs/${id}`, { method: "DELETE" });

// ── 操练台:建任务 + SSE 流式订阅 ──
export interface ChatOverrides {
  knowledge_sources?: { mode: "replace" | "extend"; sources: string[] };
  tool_params?: Record<string, Record<string, unknown>>;
  allowed_tool_ids?: string[];
  model?: string;
}

export async function sendChat(opts: {
  executorType: "agent" | "skill" | "tool" | "mcp";
  executorId: string;
  message?: string;
  params?: Record<string, unknown>;
  overrides?: ChatOverrides;
  overrideMode?: string;
  /** 15 号两层版本选择:显式钉到某 published 版本行 id;不传 = 自动解析(按当前用户指派命中,无默认版本兜底) */
  executorVersionId?: string;
  conversationId?: string | null;
  newConversation?: boolean;
}) {
  return request<{ task_id: string; status: string; conversation_id: string | null; diagnostics: string[] }>("/admin/chat", {
    method: "POST",
    body: JSON.stringify({
      executor_type: opts.executorType, executor_id: opts.executorId,
      message: opts.message, params: opts.params, overrides: opts.overrides, override_mode: opts.overrideMode,
      executor_version_id: opts.executorVersionId ?? null,
      conversation_id: opts.conversationId ?? null, new_conversation: opts.newConversation ?? false,
    }),
  });
}

// ── L4 执行计划 ──
// 动手前整份下发:界面一开始就能列出全部步骤(后面的显示「等待中」),
// 用户知道总共几步、现在第几步 —— 这是「等待」和「知道在等什么」的区别。
export interface PlanStep {
  id: string; title: string;
  kind: "tool" | "script" | "knowledge" | "analyze" | "compose";
  detail: string; tool_name: string | null; skill_id: string | null;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  duration_ms: number | null; result_summary: string; findings: string[];
}
export interface ExecutionPlan { summary: string; steps: PlanStep[] }

export type ChatEvent =
  | { event: "timeline"; data: Record<string, any> }
  // 计划整份到达(L4 模式,执行开始前);步骤状态更新走 timeline 的 plan_update
  | { event: "plan"; data: ExecutionPlan }
  | { event: "token"; data: { delta: string } }
  | { event: "usage"; data: { prompt_tokens: number; completion_tokens: number } }
  | { event: "waiting_human"; data: Record<string, any> }
  // E 方案(v0.1.5):请求保存凭证,任务挂起等「对话内用户确认」。data 只含
  // 工具名/场景等信息,绝不携带账号密码明文;确认/忽略走 resolveCredentialConfirm。
  | { event: "credential_confirm"; data: {
      task_id?: string; tool_id?: string; tool_name?: string;
      target_tool_id?: string; scope?: string; created_at?: string } }
  // 结构化输出:后端在 done 之前按 cards → chart → insight → recommend 顺序广播。
  // **chart 是单数** —— 一张一张推,不是一次一个数组。事件名写错的表现是图凭空消失,
  // 而回答正文里还写着「图已生成」,看的人只会觉得系统坏了。
  | { event: "cards"; data: { items: Record<string, any>[] } }
  | { event: "chart"; data: { title?: string; type?: string; option?: Record<string, any>;
      // table:中性的行列形态。APP 用 option(echarts),这边用它(recharts)——
      // 同一次服务端计算出的两种形态,不会出现两边数字不一样
      table?: { chart_type: string; title?: string; row_count?: number;
                fields: { name: string; type: string; role: 'metric' | 'dimension' }[];
                rows: Record<string, any>[] } } }
  | { event: "insight"; data: { level?: string; content?: string } }
  | { event: "recommend"; data: { items: Record<string, any>[] } }
  | { event: "done"; data: Record<string, any> }
  | { event: "error"; data: Record<string, any> }
  | { event: "cancelled"; data: Record<string, any> }
  // 后端执行阶段切换(understanding→querying→generating),data.text 是人类可读提示。
  | { event: "stage"; data: { stage: string; text?: string } }
  // 后端每隔几秒回报一次任务状态。它同时是心跳和"任务其实已经结束了"的兜底,
  // 没有它的话任务卡在队列里时前端只有一个转圈图标,看不出在等什么。
  // credential_confirm 非空 = 任务挂在对话内凭证确认上(重连后据此恢复确认卡片)。
  | { event: "status"; data: { status: string; message?: string;
      credential_confirm?: {
        task_id?: string; tool_id?: string; tool_name?: string;
        target_tool_id?: string; scope?: string; created_at?: string } | null } };

/** 手写 fetch + ReadableStream 解析 SSE(而不是浏览器内置 EventSource),
 * 因为 EventSource 不支持自定义 Authorization 请求头。 */
export async function streamChat(taskId: string, onEvent: (e: ChatEvent) => void, signal?: AbortSignal, lastEventId?: string): Promise<void> {
  // Last-Event-ID:断线续传。不传时后端 subscribe(last_id=None) 会从内存流**开头全量重放**
  // 本任务所有事件——刷新/重连场景下前端会一次性刷出整段执行链,且 steps 重复累积。
  // 传了之后后端只推送 last_id 之后的新事件,前端按 _sseId 去重作为双保险。
  await ensureFreshToken();
  const open = async () => {
    const headers: Record<string, string> = { ...authHeaders() };
    if (lastEventId) headers['Last-Event-ID'] = lastEventId;
    return fetch(`${BASE}/admin/chat/stream/${taskId}`, { headers, signal });
  };
  let res = await open();
  // 长任务跨过 access_token 有效期时,重连这一下正好撞上 401。续期后重开,
  // 带着 Last-Event-ID 从断点继续,用户看不出中间断过。
  if (res.status === 401 && await tryRefresh()) res = await open();
  if (!res.ok || !res.body) throw new ApiError(`SSE 连接失败 (${res.status})`, res.status);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // 是否已收到终局事件(done/error/cancelled)。SSE 流可能因后端某条路径提前关闭而
  // 结束,导致终局事件丢失——此时前端会一直停在"正在思考",光标闪个不停。
  // 所以在流收尾(EOF/超时断开)时兜底查一次任务状态,补发终局事件。
  let sawTerminal = false;
  const wrapOnEvent = (e: ChatEvent) => {
    if (e.event === 'done' || e.event === 'error' || e.event === 'cancelled') sawTerminal = true;
    onEvent(e);
  };
  // 任务已终态但终局事件没到(流提前关闭):主动查一次,把消息收敛到正确状态。
  const ensureTerminal = async (): Promise<boolean> => {
    if (sawTerminal) return true;
    try {
      const t = await getChatResult(taskId);
      if (!t?.status) return false;
      if (t.status === 'COMPLETED') {
        // structured 必须一起补:SSE 断线后走这条兜底补终态时,实时 chart 事件已经
        // 错过了,不带 structured 的话这一轮的图就永远出不来。
        wrapOnEvent({ event: 'done', data: { answer: t.result?.answer ?? '', usage: t.result?.usage, timeline: t.result?.timeline ?? [], citations: t.result?.citations ?? [], structured: t.result?.structured ?? null } });
        return true;
      }
      if (t.status === 'FAILED') {
        wrapOnEvent({ event: 'error', data: { message: t.result?.error || '任务执行失败' } });
        return true;
      }
      if (t.status === 'CANCELLED') {
        wrapOnEvent({ event: 'cancelled', data: { message: '任务已取消', answer: t.result?.answer ?? '' } });
        return true;
      }
      return false;
    } catch {
      return false;
    }
  };

  /**
   * 逐块解析 SSE。
   *
   * **这里踩过一个很隐蔽的坑**:早期实现用 `buffer.split("\n\n")` 切事件,但
   * sse-starlette 发出来的分隔符是 `\r\n\r\n` —— 字节序列是 \r \n \r \n,
   * 其中**根本不存在 `\n\n` 子串**。于是一个事件都切不出来,buffer 只会无限增长,
   * 前端收不到任何 token,表现为"流式输出完全没有,内容最后一次性蹦出来"
   * (最后那下是别的兜底路径补的)。
   * 现在按正则同时匹配两种分隔符,兼容不同 SSE 服务端实现。
   */
  const flush = (raw: string) => {
    let event = "message";
    let eventId: string | undefined;
    const dataLines: string[] = [];
    for (const rawLine of raw.split(/\r\n|\n|\r/)) {
      if (rawLine.startsWith(":")) continue;                  // 注释行(心跳)
      if (rawLine.startsWith("id:")) { eventId = rawLine.slice(3).trim(); continue; }
      if (rawLine.startsWith("event:")) { event = rawLine.slice(6).trim(); continue; }
      if (rawLine.startsWith("data:")) {
        // 按 SSE 规范:只去掉紧跟冒号的**一个**空格,不能 trim()——
        // token 增量里的空格和缩进都是内容的一部分,trim 掉会让输出粘成一团。
        const v = rawLine.slice(5);
        dataLines.push(v.startsWith(" ") ? v.slice(1) : v);
      }
    }
    if (!dataLines.length) return;
    // 多行 data 按规范用 \n 拼接
    const data = dataLines.join("\n");
    try {
      const parsed = JSON.parse(data) as Record<string, any>;
      // 附带 SSE 事件 id(后端按 id: 行下发):前端据此去重/记录断点续传位置,
      // 断线重连后历史事件不会重复渲染。
      if (eventId) parsed._sseId = eventId;
      wrapOnEvent({ event, data: parsed } as ChatEvent);
    } catch {
      /* 非 JSON 的心跳/注释,忽略 */
    }
  };

  const SEP = /\r\n\r\n|\n\n|\r\r/;
  // 后端正常每 ~5s 发一条心跳(注释行),所以长时间读不到任何字节只可能是后端挂了
  // 或连接半开。必须超时兜底:否则 handleSend 里 `await consumeStream` 永远不返回,
  // busy 标记不释放 → 发送按钮点不动、光标一直闪(第二轮发不出)。
  const IDLE_TIMEOUT_MS = 120_000;
  while (true) {
    let timer: number | undefined;
    const read = Promise.race([
      reader.read(),
      new Promise<never>((_, reject) => {
        timer = window.setTimeout(() => {
          reader.cancel().catch(() => {});
          // 超时前先看一眼任务是否其实已经终态(比如流在任务完成后提前关闭,终局事件
          // 丢了):已终态就补发终局事件并正常结束,而不是一律误报"连接超时"。
          // 只有确实还在执行时才按超时断开处理,让 handleSend 走 catch 释放 busy。
          ensureTerminal().then((terminal) => {
            if (!terminal) reject(new Error(`SSE 连接超过 ${IDLE_TIMEOUT_MS / 1000}s 无数据,已断开`));
          });
        }, IDLE_TIMEOUT_MS);
      }),
    ]);
    const { done, value } = await read;
    clearTimeout(timer);
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let m: RegExpExecArray | null;
    // 每次从头找分隔符,把完整事件切出来,剩余部分留在 buffer 等下一块数据
    while ((m = SEP.exec(buffer)) !== null) {
      const raw = buffer.slice(0, m.index);
      buffer = buffer.slice(m.index + m[0].length);
      if (raw.trim()) flush(raw);
    }
  }
  // 流结束时 buffer 里可能还剩最后一个没有以分隔符收尾的事件
  if (buffer.trim()) flush(buffer);
  // EOF:流已经关闭,但可能一直没收到终局事件(后端某条路径提前关流、终局事件丢在
  // 队列里)。兜底查一次任务状态,把消息从"正在思考"收敛到 done/error/cancelled。
  await ensureTerminal();
}

// ── 多轮会话(持久化到 Postgres,刷新不丢)──
export interface Conversation {
  id: string; title: string; executor_type: string; executor_id: string;
  turn_count: number; scenic_id: string | null; created_at: string; updated_at: string;
}
export interface ConversationTurn {
  task_id: string; status: string; message: string;
  answer: string | null; error: string | null;
  usage: { prompt_tokens: number; completion_tokens: number;
           cost_cny?: number | null; model_cost_cny?: number | null; cost_report?: any[];
           total_duration_ms?: number;
           // 该轮实际生效的 Token 预算(0 = 不限制)。右侧执行详情据此决定画不画进度条;
           // 旧数据没有这个字段,按「不限制」处理,而不是前端自己编一个上限出来。
           token_budget?: number | null } | null;
  timeline: any[];          // 执行链:这一轮调了哪些工具、走了哪个模式
  // 引用溯源:知识检索来源明细(回答里 [n] 标注对应),历史消息回显仍可核验
  citations?: { ref: string | number; source: string; type: string; url?: string }[];
  // 结构化问数结果(与 SSE 结构化事件同构)。图表/卡片/解读随 result 落库,
  // 历史回放据此重建 —— 不读它,刷新后图就没了(APP 侧一直在读)。
  structured?: {
    cards?: any[] | null; charts?: any[]; insight?: any; recommends?: any[] | null;
  } | any[] | null;
  in_progress: boolean;     // 仍在执行中 -> 前端重新挂回 SSE 流继续接收
  cancelled?: boolean;      // 用户主动停止的轮次 -> 渲染「已停止生成」+ 部分回答,非 error
  created_at: string;
}
export const listConversations = () => request<{ items: Conversation[] }>("/admin/chat/conversations");
export const getConversationTurns = (id: string) =>
  request<{ items: ConversationTurn[] }>(`/admin/chat/conversations/${id}/turns`);
export const archiveConversation = (id: string) =>
  request(`/admin/chat/conversations/${id}`, { method: "DELETE" });

export interface ChatTaskResult {
  status: string;
  result?: {
    answer?: string;
    error?: string;
    usage?: Record<string, unknown>;
    timeline?: Record<string, unknown>[];
    citations?: { ref: string | number; source: string; type: string; url?: string }[];
    structured?: Record<string, unknown> | unknown[] | null;
  } | null;
}

export const getChatResult = (taskId: string) => request<ChatTaskResult>(`/admin/chat/result/${taskId}`);

/** E 方案(v0.1.5):对话内用户确认/忽略保存凭证。确认卡片只展示工具名+场景,不展示明文。 */
export const resolveCredentialConfirm = (taskId: string, action: "approve" | "ignore") =>
  request<{ ok: boolean; action: string; message?: string }>(
    `/admin/chat/${encodeURIComponent(taskId)}/credential-confirm`,
    { method: "POST", body: JSON.stringify({ action }) });

// ── 任务中心 ──
export const listTasks = (params?: { status?: string; executor_type?: string }) => {
  const qs = new URLSearchParams(params as Record<string, string>).toString();
  return request<{ items: any[] }>(`/admin/tasks${qs ? `?${qs}` : ""}`);
};
export const getTask = (taskId: string) => request<any>(`/admin/tasks/${taskId}`);
export const cancelTask = (taskId: string) => request(`/admin/tasks/${taskId}/cancel`, { method: "POST" });

// ── 工具中心:Tool ──
// ── 工具配置模板 / 请求预览(说明与示例由后端下发,前端不硬编码)──
export interface ToolField {
  key: string; label: string; widget: string; required: boolean;
  description: string; default?: any; options?: string[];
  // 下拉选项的中文显示名(值 → 名)。`dataset_qa` 这种值只有开发看得懂,
  // 运营选的时候全靠猜 —— 存的仍是原值,只改显示。
  option_labels?: Record<string, string>;
  placeholder?: string; example?: any; language?: string;
  section?: string;                       // 归属分区,前端左侧导航按它分组
  // Python 插件的可选骨架(按用途)。前端提供「插入骨架」而不是让人从零写。
  skeletons?: { id: string; label: string; desc: string; code: string; input_schema: any }[];
}
export interface ToolSection { id: string; label: string; desc: string }
export interface ToolTemplate {
  type: string; label: string; summary: string;
  fields: ToolField[]; sections?: ToolSection[]; example: any;
  tutorial: { title: string; body: string }[];
}
export interface ToolTemplatesResp { templates: ToolTemplate[] }
export const listToolTemplates = () => request<ToolTemplatesResp>("/admin/tools/templates");
export const previewToolRequest = (payload: { type: string; type_config: any; params?: any }) =>
  request<any>("/admin/tools/preview", { method: "POST", body: JSON.stringify(payload) });

export const listTools = (keyword?: string) => request<{ items: any[] }>(`/admin/tools${keyword ? `?keyword=${encodeURIComponent(keyword)}` : ""}`);
export const getTool = (id: string) => request<any>(`/admin/tools/${id}`);

// ── 工具中心:按 Markdown 接口文档建工具 ──
// 为什么要有「先解析、再保存」两步:解析结果(哪些参数被认成必传、数据体在哪一层、
// 认证 header 有没有从 curl 里取到)要先给人看一眼。看过再保存,比保存完再去猜
// 「为什么模型老是传错参数」省事得多。
export interface ApiDocField { name: string; type: string; required: boolean; example: string; desc: string }
export interface ApiDocParsed {
  title: string; uri: string; method: string; auth: string; summary: string; base_url: string;
  headers: Record<string, string>;
  request_fields: ApiDocField[];
  response_fields: ApiDocField[];
  data_path: string[];
  sample_response: any;
  warnings: string[];
  type_config: any;
  preview_schema: any;
}
/**
 * 解析**一段** Markdown 字段表(不是整份文档)。给工具编辑页的「参数定义」「响应字段说明」用。
 *
 * 走后端那一套表格解析器,与整份文档解析同源 —— 不在前端再写一份:
 * 两份解析器迟早对不上,而对不上的表现是「预览看着对、存进去不对」,最难查。
 */
export const parseFieldTable = (payload: { markdown: string; kind: 'request' | 'response' }) =>
  request<{ fields: { name: string; type: string; required: boolean; example: string; desc: string }[];
            warnings: string[] }>(
    "/admin/tools/parse-fields", { method: "POST", body: JSON.stringify(payload) });

export const parseApiDoc = (payload: { markdown: string; default_base_url?: string }) =>
  request<ApiDocParsed>("/admin/tools/from-api-doc/parse", { method: "POST", body: JSON.stringify(payload) });
/** 按随服务打包的《数据指标接口文档》同步 HTTP 工具(缺的新建;update_existing 时刷新已有的)。
 *  服务启动时已自动补过一次,这里是手动入口。仅集团角色。 */
export interface DataApiSyncResult {
  created: string[]; updated: string[]; skipped: string[]; errors: string[];
  total: number; message: string; key_configured: boolean;
}
export const syncDataApiTools = (update_existing = false) =>
  request<DataApiSyncResult>("/admin/tools/sync-data-api", {
    method: "POST", body: JSON.stringify({ update_existing }) });

export const createToolFromApiDoc = (payload: {
  markdown: string; default_base_url?: string; name?: string; description?: string; scenic_id?: string | null;
}) => request<{ id: string; name: string; warnings: string[]; message: string }>(
  "/admin/tools/from-api-doc", { method: "POST", body: JSON.stringify(payload) });

export const createTool = (payload: object) => request<any>("/admin/tools", { method: "POST", body: JSON.stringify(payload) });
export const updateTool = (id: string, payload: object) => request<any>(`/admin/tools/${id}`, { method: "PUT", body: JSON.stringify(payload) });
export const deleteTool = (id: string, force = false) =>
  request(`/admin/tools/${id}${force ? "?force=true" : ""}`, { method: "DELETE" });
export const toggleTool = (id: string) => request<any>(`/admin/tools/${id}/toggle`, { method: "POST" });
export const lockTool = (id: string) => request<any>(`/admin/tools/${id}/lock`, { method: "POST" });
export const shareTool = (id: string, payload: object) => request(`/admin/tools/${id}/share`, { method: "POST", body: JSON.stringify(payload) });
export const testTool = (id: string, params: object) => request<{ task_id: string }>(`/admin/tools/${id}/test`, { method: "POST", body: JSON.stringify({ params }) });
// 共享授权:授权列表 / 回收 / 用户搜索源(权限放宽到管理员+工具开发者,见后端 search_tool_users)
export const listToolGrants = (toolId: string) => request<{ items: any[] }>(`/admin/tools/${toolId}/grants`);
export const deleteToolGrant = (toolId: string, grantId: string) =>
  request<{ ok: boolean }>(`/admin/tools/${toolId}/grants/${grantId}`, { method: "DELETE" });
export const searchToolUsers = (keyword?: string) =>
  request<{ items: any[] }>(`/admin/tools/users/search${keyword ? `?keyword=${encodeURIComponent(keyword)}` : ""}`);

// ── 工具中心:Skill ──
export const listSkills = () => request<{ items: any[] }>("/admin/skills");
export const getSkill = (id: string) => request<any>(`/admin/skills/${id}`);
export const createSkill = (payload: object) => request<any>("/admin/skills", { method: "POST", body: JSON.stringify(payload) });
export const updateSkill = (id: string, payload: object) => request<any>(`/admin/skills/${id}`, { method: "PUT", body: JSON.stringify(payload) });
export const deleteSkill = (id: string, force = false) =>
  request(`/admin/skills/${id}${force ? "?force=true" : ""}`, { method: "DELETE" });

// ── APP 运营:固定问题库(19号文档) ──
export const listAppQuestions = (params?: { theme?: string; priority?: string; active?: boolean }) =>
  request<{ items: any[] }>(`/admin/app-ops/questions${params ? '?' + new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null)
      .map(([k, v]) => [k, String(v)])).toString() : ''}`);
export const createAppQuestion = (payload: object) => request("/admin/app-ops/questions", { method: "POST", body: JSON.stringify(payload) });
export const updateAppQuestion = (id: string, payload: object) => request(`/admin/app-ops/questions/${id}`, { method: "PUT", body: JSON.stringify(payload) });
export const deleteAppQuestion = (id: string) => request(`/admin/app-ops/questions/${id}`, { method: "DELETE" });
export const listAppMetrics = () => request<{ items: any[] }>("/admin/app-ops/metrics");

// ── APP 运营:景区配置(25号方案:承载量与景区级参数) ──
export interface ScenicConfigItem {
  scenic_id: string;
  scenic_name: string | null;      // 主数据只读展示
  scenic_grade_level: string | null;
  is_enabled: boolean | null;
  synced_at: string | null;
  bound_users: number;             // 该景区 active 绑定用户数
  configured: boolean;
  daily_capacity: number | null;   // 日最大承载量(人次/天)
  extra: Record<string, unknown>;
  updated_at: string | null;
  updated_by: string | null;
}
export const listScenicConfigs = (kw?: string, state?: 'all' | 'configured' | 'unconfigured') =>
  request<{ items: ScenicConfigItem[] }>(`/admin/app-ops/scenic-configs${
    (kw || (state && state !== 'all')) ? '?' + new URLSearchParams(
      Object.entries({ kw: kw || undefined, state: state === 'all' ? undefined : state })
        .filter(([, v]) => v !== undefined && v !== null)
        .map(([k, v]) => [k, String(v)]) as [string, string][]).toString() : ''}`);
export const getScenicConfig = (scenicId: string) =>
  request<ScenicConfigItem>(`/admin/app-ops/scenic-configs/${scenicId}`);
export const saveScenicConfig = (scenicId: string, dailyCapacity: number | null) =>
  request<ScenicConfigItem>(`/admin/app-ops/scenic-configs/${scenicId}`, {
    method: "PUT", body: JSON.stringify({ daily_capacity: dailyCapacity }),
  });

/** 系统参数(0090):系统设置 · 参数配置里定义,景区可覆盖取值;Tool / MCP / Skill 里写 {{param_key}} 引用。 */
export type ParamMultiMode = 'single' | 'fanout' | 'list' | 'join';
export interface ParamValue { value: string; label: string }
export interface SysParamDef {
  param_key: string;
  ref: string;                   // "{{param_key}}"
  label: string;
  description: string;
  multi_mode: ParamMultiMode;    // single 单值 / fanout 每个值各调一次 / list 数组一次传入 / join 逗号拼接
  default_values: ParamValue[];  // 敏感参数回显为「••••末四位」
  arg_names: string[];           // 命中即自动填入的工具入参名
  fill_mode: 'fill' | 'force';
  is_secret: boolean;
  enabled: boolean;
  scenic_overrides?: number;     // 多少个景区覆盖了取值
  /** 列表用:按值汇总,每个值分配到哪些景区(景区名)、是否默认值 */
  value_groups?: { value: string; label: string; scenics: string[]; is_default: boolean }[];
  updated_at?: string | null;
  updated_by?: string | null;
}
export type SysParamSave = Omit<SysParamDef, 'param_key' | 'ref' | 'default_values' | 'scenic_overrides' | 'updated_at' | 'updated_by'>
  & { default_values: ParamValue[] | null };   // null = 不改默认值(敏感参数改说明时不必重填)
export interface ParamRef { param_key: string; ref: string; label: string; description: string; multi_mode: ParamMultiMode; is_secret: boolean; arg_names: string[] }
export const listParams = () => request<{ items: SysParamDef[] }>(`/admin/params`);
export const saveParam = (key: string, body: SysParamSave) =>
  request<SysParamDef>(`/admin/params/${key}`, { method: "PUT", body: JSON.stringify(body) });
export const deleteParam = (key: string) => request<{ ok: boolean }>(`/admin/params/${key}`, { method: "DELETE" });
export const listParamRefs = () => request<{ items: ParamRef[] }>(`/admin/params/refs`);
/** 按景区分配:同一个 {{参数}},各景区各自的值(执行时只按本次任务所属景区取) */
export interface ParamAssignment { scenic_id: string; scenic_name?: string; values: ParamValue[]; updated_at?: string | null }
export const listParamAssignments = (key: string) =>
  request<{ param_key: string; items: ParamAssignment[] }>(`/admin/params/${key}/assignments`);
export const saveParamAssignments = (key: string, items: { scenic_id: string; values: ParamValue[] }[]) =>
  request<{ param_key: string; items: ParamAssignment[] }>(`/admin/params/${key}/assignments`, {
    method: "PUT", body: JSON.stringify({ items }),
  });

/** 景区的参数取值视图:系统默认 + 本景区覆盖 */
export interface ScenicParamView {
  param_key: string; ref: string; label: string; description: string; multi_mode: ParamMultiMode;
  is_secret: boolean; enabled: boolean; arg_names: string[];
  default_values: ParamValue[]; overridden: boolean; values: ParamValue[];
  updated_at?: string | null; updated_by?: string | null;
}
/** values=null → 不覆盖(用系统默认);keep_secret=true → 敏感参数保持原值 */
export interface ScenicParamOverride { param_key: string; values: ParamValue[] | null; keep_secret?: boolean }
export const getScenicParams = (scenicId: string) =>
  request<{ scenic_id: string; items: ScenicParamView[] }>(`/admin/app-ops/scenic-configs/${scenicId}/params`);
export const saveScenicParams = (scenicId: string, items: ScenicParamOverride[]) =>
  request<{ scenic_id: string; items: ScenicParamView[] }>(`/admin/app-ops/scenic-configs/${scenicId}/params`, {
    method: "PUT", body: JSON.stringify({ items }),
  });

// ── 工具中心:Agent ──
export const listAgents = () => request<{ items: any[] }>("/admin/agents");
export const getAgent = (id: string) => request<any>(`/admin/agents/${id}`);
export const createAgent = (payload: object) => request<any>("/admin/agents", { method: "POST", body: JSON.stringify(payload) });
export const updateAgent = (id: string, payload: object) => request<any>(`/admin/agents/${id}`, { method: "PUT", body: JSON.stringify(payload) });
export const deleteAgent = (id: string, force = false) =>
  request(`/admin/agents/${id}${force ? "?force=true" : ""}`, { method: "DELETE" });
export const toggleAgent = (id: string) => request<any>(`/admin/agents/${id}/toggle`, { method: "POST" });
// ── 工具中心:MCP ──
export const listMcpServers = () => request<{ items: any[] }>("/admin/mcp-servers");
export const createMcpServer = (payload: object) => request<any>("/admin/mcp-servers", { method: "POST", body: JSON.stringify(payload) });
export const discoverMcpTools = (id: string) => request<{ imported_count: number; tool_ids: string[] }>(`/admin/mcp-servers/${id}/discover`, { method: "POST" });

// ── 知识中心:数据库知识 ──
export const listDatasources = () => request<{ items: any[] }>("/admin/knowledge/datasources");
export const createDatasource = (payload: object) => request<any>("/admin/knowledge/datasources", { method: "POST", body: JSON.stringify(payload) });
export const deleteDatasource = (id: string) => request(`/admin/knowledge/datasources/${id}`, { method: "DELETE" });
export const listDatasourceTables = (id: string) => request<{ items: any[] }>(`/admin/knowledge/datasources/${id}/tables`);
export const getTableDdl = (id: string, table: string) => request<{ ddl: string; preview: { columns: string[]; rows: any[] } }>(`/admin/knowledge/datasources/${id}/tables/${encodeURIComponent(table)}/ddl`);
export const registerTable = (id: string, payload: object) => request(`/admin/knowledge/datasources/${id}/tables`, { method: "POST", body: JSON.stringify(payload) });
export const listCustomSources = () => request<{ items: any[] }>("/admin/knowledge/custom-sources");
export const createCustomSource = (payload: object) => request("/admin/knowledge/custom-sources", { method: "POST", body: JSON.stringify(payload) });

// ── 知识中心:网络知识 ──
export interface WebSearchConfig {
  id: string; name: string; provider: string; base_url: string | null;
  cache_ttl_seconds: number; scope: string | null; active: boolean;
  has_api_key: boolean;   // 只回传"有没有配",绝不回传密钥本身
}
export const listWebSearchConfigs = () => request<{ items: WebSearchConfig[] }>("/admin/knowledge/web-search-config");
export const updateWebSearchConfig = (id: string, payload: object) =>
  request(`/admin/knowledge/web-search-config/${id}`, { method: "PUT", body: JSON.stringify(payload) });
export const deleteWebSearchConfig = (id: string) =>
  request(`/admin/knowledge/web-search-config/${id}`, { method: "DELETE" });
/** 真实发一次检索验证配置可用。配完密钥能当场验证,而不是等某次对话里悄悄失败。 */
export const testWebSearchConfig = (id: string) =>
  request<{ ok: boolean; count: number; sample: any[]; hint: string }>(
    `/admin/knowledge/web-search-config/${id}/test`, { method: "POST" });
export const createWebSearchConfig = (payload: object) => request("/admin/knowledge/web-search-config", { method: "POST", body: JSON.stringify(payload) });
export const listWhitelist = () => request<{ items: any[] }>("/admin/knowledge/web-whitelist");
export const addWhitelist = (domain: string) => request("/admin/knowledge/web-whitelist", { method: "POST", body: JSON.stringify({ domain }) });
export const deleteWhitelist = (id: string) => request(`/admin/knowledge/web-whitelist/${id}`, { method: "DELETE" });
export const listSiteArchive = () => request<{ items: any[] }>("/admin/knowledge/web-site-archive");
export const addSiteArchive = (payload: object) => request("/admin/knowledge/web-site-archive", { method: "POST", body: JSON.stringify(payload) });

// ── 知识中心:向量知识 ──
export const listVectorStorageConfigs = () => request<{ items: any[] }>("/admin/knowledge/vector-storage-configs");
export const createVectorStorageConfig = (payload: object) => request<any>("/admin/knowledge/vector-storage-configs", { method: "POST", body: JSON.stringify(payload) });
export const createVectorDbConfig = (payload: object) => request<any>("/admin/knowledge/vector-db-configs", { method: "POST", body: JSON.stringify(payload) });
export const listKnowledgeBases = () => request<{ items: any[] }>("/admin/knowledge/bases");
export const createKnowledgeBase = (payload: object) => request<any>("/admin/knowledge/bases", { method: "POST", body: JSON.stringify(payload) });
export interface KbDocument { id: string; file_name: string; status: string; chunk_count: number; created_at: string }
export const listKbDocuments = (kbId: string) => request<{ items: KbDocument[] }>(`/admin/knowledge/bases/${kbId}/documents`);
export const ingestDocument = (kbId: string, payload: object) => request<any>(`/admin/knowledge/bases/${kbId}/documents`, { method: "POST", body: JSON.stringify(payload) });
export const retrieveTest = (kbId: string, query: string, topK = 5) =>
  request<{ items: any[] }>(`/admin/knowledge/bases/${kbId}/retrieve-test`, { method: "POST", body: JSON.stringify({ query, top_k: topK }) });

// ── 评测中心 ──
export const listEvalCases = (targetType?: string, targetId?: string) => {
  const qs = new URLSearchParams({ ...(targetType ? { target_type: targetType } : {}), ...(targetId ? { target_id: targetId } : {}) }).toString();
  return request<{ items: any[] }>(`/admin/eval-cases${qs ? `?${qs}` : ""}`);
};
export const createEvalCase = (payload: object) => request<any>("/admin/eval-cases", { method: "POST", body: JSON.stringify(payload) });
export const deleteEvalCase = (id: string) => request(`/admin/eval-cases/${id}`, { method: "DELETE" });
export const triggerEvalRun = (payload: object) => request<any>("/admin/eval-runs", { method: "POST", body: JSON.stringify(payload) });
export const listEvalRunBatches = (targetType?: string, targetId?: string) => {
  const qs = new URLSearchParams({ ...(targetType ? { target_type: targetType } : {}), ...(targetId ? { target_id: targetId } : {}) }).toString();
  return request<{ items: any[] }>(`/admin/eval-runs${qs ? `?${qs}` : ""}`);
};
export const getEvalRunDetail = (batchId: string) => request<any>(`/admin/eval-runs/${batchId}`);

// ── 审批中心 ──
export const listApprovals = (status: string | null = "pending") => request<{ items: any[] }>(`/admin/approvals${status ? `?status=${status}` : ""}`);
export const getApproval = (id: string) => request<any>(`/admin/approvals/${id}`);
export const resolveApproval = (id: string, approved: boolean, opinion?: string) =>
  request(`/admin/approvals/${id}/resolve`, { method: "POST", body: JSON.stringify({ approved, opinion }) });

// ── 系统日志(审计) ──
export const listAuditLogs = (eventType?: string) => request<{ items: any[] }>(`/admin/audit-logs${eventType ? `?event_type=${eventType}` : ""}`);

// ── 链路可视化(langgraph 执行图 + 执行链)──
export interface GraphNode { id: string; label: string }
export interface GraphEdge { from: string; to: string }
export interface GraphVizData {
  task_id: string; status: string; executor_type: string; executor_id: string;
  conversation_id: string; mode: string; error: string; usage: any;
  graph: { nodes: GraphNode[]; edges: GraphEdge[]; executed: string[] };
  steps: Array<Record<string, any>>;
}
export const getGraphTrace = (taskId: string) => request<GraphVizData>(`/admin/graph/trace/${taskId}`);

// ── 工作台看板 ──
export const dashboardSummary = (periodDays = 7) => request<any>(`/admin/dashboard/summary?period_days=${periodDays}`);
export const dashboardRecentTasks = (limit = 20) => request<{ items: any[] }>(`/admin/dashboard/recent-tasks?limit=${limit}`);

// ── 系统设置:模型/预算/组织 ──
/** 预置模型厂商清单（含默认 Base URL 与填写提示），新增模型弹窗的下拉据此渲染。 */
export const listBuiltinModelProviders = () =>
  request<{ items: any[] }>("/admin/settings/model-providers");
export const listModelConfigs = (params?: { page_size?: number; provider_id?: string }) => {
  const qs = new URLSearchParams();
  if (params?.page_size !== undefined) qs.set('page_size', String(params.page_size));
  if (params?.provider_id) qs.set('provider_id', params.provider_id);
  return request<{ items: any[] }>(`/admin/settings/models${qs.toString() ? '?' + qs.toString() : ''}`);
};
/** 把某个模型设为全局默认(按 id 定位)。API 调用与操练台未指定模型时都走它。 */
export const setDefaultModel = (id: string) =>
  request<{ ok: boolean; config_id: string }>(
    `/admin/settings/models/${encodeURIComponent(id)}/set-default`, { method: "POST" });
export const upsertModelConfig = (payload: object) => request("/admin/settings/models", { method: "POST", body: JSON.stringify(payload) });

/** 平台共用密钥(目前只有数据平台 xpftkey)。只写不读:接口只回配没配 / 来源 / 末四位。 */
export interface PlatformSecret {
  name: string; label: string; env: string; used_by: string;
  configured: boolean; source: 'admin' | 'env' | ''; masked: string;
  updated_at: string | null; updated_by: string | null;
}
export const listPlatformSecrets = () =>
  request<{ items: PlatformSecret[] }>("/admin/settings/platform-secrets");
export const setPlatformSecret = (name: string, value: string) =>
  request<{ ok: boolean; items: PlatformSecret[] }>(`/admin/settings/platform-secrets/${encodeURIComponent(name)}`,
    { method: "PUT", body: JSON.stringify({ value }) });
export const clearPlatformSecret = (name: string) =>
  request<{ ok: boolean; items: PlatformSecret[] }>(`/admin/settings/platform-secrets/${encodeURIComponent(name)}`,
    { method: "DELETE" });
export const toggleModelConfig = (id: string) => request(`/admin/settings/models/${encodeURIComponent(id)}/toggle`, { method: "POST" });
export const deleteSettingsModel = (id: string) => request(`/admin/settings/models/${encodeURIComponent(id)}`, { method: "DELETE" });
// ── 模型单价配置(gov_llm_model_prices,替代 litellm 实时拉取的计费表)──
export const listModelPrices = () => request<{ items: any[] }>("/admin/settings/model-prices");
export const upsertModelPrice = (payload: { model_id: string; input_price_cny_per_m?: number | null; output_price_cny_per_m?: number | null }) =>
  request("/admin/settings/model-prices", { method: "POST", body: JSON.stringify(payload) });
export const deleteModelPrice = (modelId: string) =>
  request(`/admin/settings/model-prices/${encodeURIComponent(modelId)}`, { method: "DELETE" });
export const listBudgetThresholds = () => request<{ items: any[] }>("/admin/settings/budgets");
export const createBudgetThreshold = (payload: object) => request("/admin/settings/budgets", { method: "POST", body: JSON.stringify(payload) });
export const listScenicOrgs = (params?: { keyword?: string; registered?: boolean;
                                          page?: number; page_size?: number }) => {
  const qsp = new URLSearchParams();
  if (params?.keyword) qsp.set('keyword', params.keyword);
  if (params?.registered !== undefined) qsp.set('registered', String(params.registered));
  qsp.set('page', String(params?.page ?? 1));
  qsp.set('page_size', String(params?.page_size ?? 50));
  return request<{ items: any[]; total: number; page: number; page_size: number }>(
    `/admin/settings/orgs?${qsp.toString()}`);
};
/** 登记景区(从主数据里挑);名称以主数据为准,不接受手填 */
export const registerScenicOrg = (payload: { scenic_ids?: string[]; scenic_id?: string; enable?: boolean }) =>
  request("/admin/settings/orgs", { method: "POST", body: JSON.stringify(payload) });
export const toggleScenicOrg = (scenicId: string) => request(`/admin/settings/orgs/${scenicId}/toggle`, { method: "POST" });

// ── 向量库后端配置(向量存 Milvus,元数据仍在 Postgres)──
export interface VectorDbConfig {
  id: string; name: string; backend: string; has_token: boolean;
  uri?: string; host?: string; port?: number; db_name?: string;
  collection?: string; dim?: number; metric?: string; index_type?: string;
}
export const listVectorDbConfigs = () =>
  request<{ items: VectorDbConfig[] }>("/admin/knowledge/vector-db-configs");
/** token 留空表示不修改已有密钥,不必每次改别的字段都重输一遍。 */
export const saveVectorDbConfig = (payload: Partial<VectorDbConfig> & { token?: string }) =>
  request<{ id: string; backend: string }>("/admin/knowledge/vector-db-configs",
    { method: "POST", body: JSON.stringify(payload) });
/** 连通性自检:连上向量库、确认集合存在(不存在会自动创建)、回报当前向量条数。 */
export const testVectorDbConfig = () =>
  request<{ ok: boolean; detail: string }>("/admin/knowledge/vector-db-configs/test",
    { method: "POST" });

// ── 统一版本管理(Tool / Skill / Agent 共用)──
export type VersionStatus = "draft" | "published" | "offline";
export interface ResourceVersion {
  id: string; resource_type: string; resource_id: string;
  /** 推荐方案:草稿不预占版本号,version 为 null 表示草稿;发布时才分配正式版本号 */
  version: number | null; status: VersionStatus; note: string;
  snapshot: Record<string, any>; created_by: string;
  created_at: string; updated_at: string;
  published_at: string | null; offlined_at: string | null;
  /** 15 号文档已删除默认版本概念(is_default 列随 0038 迁移物理删除) */
  /** 推荐方案:发布时冻结的依赖锁(agent->skill->tool 完整闭包) */
  locked_deps?: { deps: PrecheckDependency[]; frozen_at?: string } | null;
  /** agent 版本:被指派的用户数(分发中心按人维度);skill/tool 恒无 */
  assigned_count?: number;
}
/** 发布预检 / 依赖锁中的单个依赖项 */
export interface PrecheckDependency {
  resource_type: string; resource_id: string; name: string;
  /** null 表示走主表 published(存量叶子资源兼容);否则为固化版本号 */
  version: number | null; status: string;
}
export interface PrecheckResult {
  ok: boolean; version_id: string;
  resource_type: string; resource_id: string;
  will_version: number | null;
  deps: PrecheckDependency[]; problems: string[];
  pass: boolean; snapshot: Record<string, any>;
}
export const listResourceVersions = (type: string, id: string) =>
  request<{ items: ResourceVersion[]; available: boolean; unavailable_reason: string }>(
    `/admin/resources/${type}/${id}/versions`);
/** 基于某版本(或当前状态)派生一个草稿版本。 */
export const createResourceVersion = (type: string, id: string,
    payload: { snapshot?: any; note?: string; base_version_id?: string | null }) =>
  request<{ id: string; version: number; status: VersionStatus }>(
    `/admin/resources/${type}/${id}/versions`, { method: "POST", body: JSON.stringify(payload) });
/** 编辑草稿版本（只允许 draft；保存仅更新内容，版本号不变、不留痕）。 */
export const editResourceVersion = (versionId: string, payload: { snapshot?: any; note?: string }) =>
  request<{ id: string; version: number; status: VersionStatus; message?: string }>(
    `/admin/versions/${versionId}`, { method: "PUT", body: JSON.stringify(payload) });
/** 发布预检(dry-run):不改变任何状态,返回依赖闭包与问题清单;正式发布内部同样校验。 */
export const precheckResourceVersion = (versionId: string) =>
  request<PrecheckResult>(`/admin/versions/${versionId}/precheck`, { method: "POST" });
export const publishResourceVersion = (versionId: string) =>
  request<{ ok: boolean; version: number | null; message: string; status: VersionStatus;
            /** 15 号:零 user 指派时的黄条提醒(去分发中心按用户指派,否则 fail-closed) */
            hint: string; assigned_users: number;
            locked_deps?: PrecheckResult['deps'] }>(
    `/admin/versions/${versionId}/publish`, { method: "POST" });
export const discardResourceVersion = (versionId: string) =>
  request<{ ok: boolean }>(
    `/admin/versions/${versionId}`, { method: "DELETE" });
/** 15 号文档:下线任意 published 版本(无「默认版本」保护/前置校验拦截);
 *  后端对「被更高版本/上层资源引用」的版本会拒绝下线。 */
export const offlineResourceVersion = (versionId: string) =>
  request<{ ok: boolean; version: number | null; message: string; status: VersionStatus }>(
    `/admin/versions/${versionId}/offline`, { method: "POST" });
/** 15 号文档:基于历史 published 版本回滚为新草稿并重新发布(无「设为默认」语义,
 *  旧版本冻结不改,回滚发布对谁生效仍由 user 指派决定)。 */
export const rollbackResourceVersion = (versionId: string) =>
  request<{ ok: boolean; version: number | null; message: string; status: VersionStatus }>(
    `/admin/versions/${versionId}/rollback`, { method: "POST" });
export const refreshVersionPins = (versionId: string) =>
  request<{ ok: boolean; version: number; snapshot: Record<string, any>; changed: string[] }>(
    `/admin/versions/${versionId}/refresh-pins`, { method: "POST" });

// ── 版本编辑区:pin 选择器数据 ──
// v1.1.5:未发布版本也可被选到 —— 版本可含 draft(status='draft',version 可为 null),
// 只有已发布才能生产生效 / 被钉版本 pin(门禁在 publish 预检与指派校验)。
/** 15 号已删 is_default:版本项仅 version/version_id/status(发布选择时标注状态) */
export interface PinOptionVersion { version: number | null; version_id: string; status?: string; }
export interface PinOptionBinding {
  resource_type: string; resource_id: string; name: string;
  versions: PinOptionVersion[];
}
/** 编辑区版本下拉数据：每个绑定资源 + 其全部版本(draft/published 都可选,标注状态)。
 *  不传 version_id 基于主表（默认版本）；传了基于该版本快照的绑定列表。 */
// ── 版本指派(D9-A:分发中心收敛到「人」,scope 仅 user;scenic 维度已下线) ──
export interface ResourceAssignment {
  id: string;
  /** 0074:指派的资源类型与 id(agent 指派时 resource_id === agent_id) */
  resource_type?: AssignableType;
  resource_id?: string;
  agent_id: string;
  /** 15 号起仅 user:分发粒度收敛到「人」,scope_id 即用户 id */
  scope_type: 'user';
  scope_id: string;
  version_id: string; version: number | null; version_status: VersionStatus | null;
  note: string; created_by: string;
  created_at: string; updated_at: string;
}
/** 可分发的资源类型(0074:技能与 Agent 同构地按用户指派) */
export type AssignableType = 'agent' | 'skill';
const assignBase = (type: AssignableType, id: string) =>
  `/admin/${type === 'skill' ? 'skills' : 'agents'}/${id}/assignments`;

export const listAssignments = (resourceId: string, type: AssignableType = 'agent') =>
  request<{ items: ResourceAssignment[] }>(assignBase(type, resourceId));
export const upsertAssignment = (resourceId: string, payload: {
  /** 目标用户 id(分发粒度=人);15 号不再有 scope_type=scenic */
  user_id: string; version_id: string; note?: string;
}, type: AssignableType = 'agent') =>
  request<{ ok: boolean; assignment: ResourceAssignment; message: string }>(
    assignBase(type, resourceId),
    { method: "POST", body: JSON.stringify(payload) });
export const removeAssignment = (resourceId: string, assignmentId: string,
                                 type: AssignableType = 'agent') =>
  request<{ ok: boolean; message: string }>(
    `${assignBase(type, resourceId)}/${assignmentId}`, { method: "DELETE" });

// ── 分发中心-人视角(15 号:按用户维度查看/指派) ──
export type AgentByUserState =
  /** 已指派:当前用户命中该 agent 的某指派版本,正常可用 */
  | 'assigned'
  /** 有已发布版本但未指派给该用户:调用将报 agent_not_distributed(fail-closed) */
  | 'not_distributed'
  /** 无任何已发布版本 */
  | 'no_published';

export interface AgentByUserItem {
  agent_id: string; name: string; avatar_url: string | null;
  visibility: string | null; scenic_id: string | null; active: boolean;
  published_count: number;
  state: AgentByUserState;
  assigned_version_id?: string | null;
  assigned_version?: number | null;
  assignment_id?: string | null;
  note?: string | null;
}
export interface AgentByUserResult {
  user: { id: string; name: string; account: string; phone: string | null;
          role: string | null; scenic_id: string | null };
  items: AgentByUserItem[];
}
/** 按用户维度列出其可见/可用的全部 agent(三态:assigned / not_distributed / no_published) */
export const listAgentsByUser = (userId: string) =>
  request<AgentByUserResult>(`/admin/agents/by-user/${encodeURIComponent(userId)}`);

export interface SkillByUserItem {
  skill_id: string; name: string; description?: string | null;
  visibility: string | null; scenic_id: string | null;
  published_count: number;
  state: AgentByUserState;
  assigned_version_id?: string | null;
  assigned_version?: number | null;
  assignment_id?: string | null;
  note?: string | null;
}
export interface SkillByUserResult {
  user: AgentByUserResult['user'];
  items: SkillByUserItem[];
}
/** 按用户维度列出其可见/可用的全部 skill(0074:技能独立分发,APP 可直接调用) */
export const listSkillsByUser = (userId: string) =>
  request<SkillByUserResult>(`/admin/skills/by-user/${encodeURIComponent(userId)}`);

export const getPinOptions = (type: string, id: string, versionId?: string) => {
  const qs = versionId ? `?version_id=${encodeURIComponent(versionId)}` : '';
  return request<{ bindings: PinOptionBinding[] }>(
    `/admin/resources/${type}/${id}/pin-options${qs}`);
};

// ── 登录方式(管理员后台) ──
export const loginByPhonePassword = async (phone: string, password: string) => {
  const { encryptPassword } = await import("./crypto");
  const encrypted = await encryptPassword(password);
  return request<any>("/admin/auth/login", { method: "POST", body: JSON.stringify({ phone, password: encrypted, rsa_encrypt: true }) });
};
export const loginByAccountPassword = async (account: string, password: string) => {
  const { encryptPassword } = await import("./crypto");
  const encrypted = await encryptPassword(password);
  return request<any>("/admin/auth/login", { method: "POST", body: JSON.stringify({ account, password: encrypted, rsa_encrypt: true }) });
};

// ── 景区/角色/组织(管理员后台·成员管理) ──
export const getScenics = (params?: { keyword?: string; is_enabled?: boolean; page_size?: number }) => {
  const qs = new URLSearchParams();
  if (params?.keyword) qs.set('keyword', params.keyword);
  if (params?.is_enabled !== undefined) qs.set('is_enabled', String(params.is_enabled));
  if (params?.page_size) qs.set('page_size', String(params.page_size));
  const s = qs.toString();
  return request<any>(`/admin/scenics${s ? '?' + s : ''}`);
};

// ── 景区主数据目录(dim_prd_scenic_base_yr_f) ──
// 景区叫什么、几A、在哪个省市一律以主数据为准;sys_scenics 只存平台侧状态
// (是否开通、套餐)。所以「登记景区」「给用户绑景区」「给 Quick BI 连接绑景区」
// 都从这里挑,而不是手敲 scenic_id —— 敲错一个字符,后面所有关联都对不上。
export interface ScenicCatalogItem {
  scenic_id: string; scenic_name: string; scenic_alias: string | null;
  scenic_grade_level: string | null;
  province_name: string | null; city_name: string | null; district_name: string | null;
  scenic_main_type: string | null; scenic_sub_type: string | null;
  open_status_type: string | null; daily_capacity_max_qty: number | null;
  scenic_operator_name: string | null; scenic_tel_consult: string | null;
  /** registered=已登记到平台;is_enabled=已开通服务;user_count=绑在该景区的账号数 */
  registered: boolean; is_enabled: boolean; user_count: number;
}
// ── 三级维度标签(主题→维度→指标) ──
// 问题挂在标签上,前台按它组织问题树。管理端 Quick BI 固定问题、APP 问题库共用这份。
export interface DimensionLabelTopic {
  topic: string; topic_id: string;
  dimensions: { dimension: string; dimension_id: string;
    metrics: { metric: string; metric_id: string; label_id: number }[] }[];
}
// ── 主题解读(概览/客群/票务/舆情/OTA直销 五条报告类问题) ──
// 前台那 5 张主题卡片点下去要出一份该主题的解读报告 —— 前提是这 5 条问题绑了 Quick BI。
// 没绑的话点下去只会走通用对话,回一句「请告诉我您需要查询什么」。
export interface ThemeIssueStatus {
  theme_code: string; theme: string;
  /** false = 还没绑 Quick BI,前台点了不会出报告 */
  ready: boolean;
  issue_id: string | null;
  scope: string;                 // 景区专属 / 集团级 / 未配置
  connection_id: string | null;
  target_data_name: string | null;
  last_run_status: string | null;   // success / failed / running
  last_run_at: string | null;
  /** 报告提示词(口径说明)。面板上就地编辑,改完不会被「全部重绑」冲掉 */
  model_instruction: string | null;
}
export const getThemeIssues = (scenicId?: string) =>
  request<{ scenic_id: string | null; connection_id: string | null; connection_name: string | null;
            items: ThemeIssueStatus[] }>(
    `/admin/app-ops/theme-issues${scenicId ? `?scenic_id=${encodeURIComponent(scenicId)}` : ''}`);
/** 面板一进来该落在哪个景区:有可用连接的优先,其中又优先已配过主题解读的。
 *  前端自己取「第一个已登记的景区」的话,挑中的往往是个还没绑连接的景区 ——
 *  一进来就是 5 条全红 + 「没有可用连接」,看着像坏了 */
export const getThemeDefaultScenic = () =>
  request<{ scenic_id: string | null; reason: string }>('/admin/app-ops/theme-issues/default-scenic');
/** 现在就把主题解读报告跑一遍(不等它跑完,报告一条要 3~10 分钟) */
export const prewarmThemeIssues = (payload: { scenic_id?: string; theme_code?: string; force?: boolean }) =>
  request<{ scenic_id: string | null; started: string[]; skipped: string[]; message: string }>(
    '/admin/app-ops/theme-issues/prewarm', { method: 'POST', body: JSON.stringify(payload) });
/** 一键生成/刷新:按景区绑连接、挑该主题的仪表板、挂三级标签 */
export const generateThemeIssues = (payload: { scenic_id?: string; connection_id?: string; overwrite?: boolean }) =>
  request<{ connection_id: string; created: string[]; updated: string[]; skipped: string[] }>(
    '/admin/app-ops/theme-issues', { method: 'POST', body: JSON.stringify(payload) });

export const getDimensionLabelTree = (activeOnly = true) =>
  request<{ topics: DimensionLabelTopic[] }>(
    `/admin/app-ops/dimension-labels?tree=true${activeOnly ? '&active=true' : ''}`);
/** 一条三级标签(一行 = 一条完整的 主题/维度/指标 路径)。
 *  中文名给人看,编码给程序分组 —— 两者各有一道唯一约束。 */
export interface DimensionLabel {
  id: number;
  topic: string; topic_id: string;
  dimension: string; dimension_id: string;
  metric: string; metric_id: string;
  sort_order: number; active: boolean;
  /** 挂在这个标签下的问题数(with_usage=true 时才有)。>0 时删不掉,改名会同步到这些问题 */
  issue_count?: number;
  create_time?: string; update_time?: string;
}
export type DimensionLabelDraft = Omit<DimensionLabel, 'id' | 'issue_count' | 'create_time' | 'update_time'>;

export const getDimensionLabels = (params?: { active?: boolean; topic_id?: string; with_usage?: boolean }) => {
  const qsp = new URLSearchParams();
  if (params?.active !== undefined) qsp.set('active', String(params.active));
  if (params?.topic_id) qsp.set('topic_id', params.topic_id);
  if (params?.with_usage) qsp.set('with_usage', 'true');
  const q = qsp.toString();
  return request<{ items: DimensionLabel[] }>(`/admin/app-ops/dimension-labels${q ? '?' + q : ''}`);
};
export const createDimensionLabel = (body: DimensionLabelDraft) =>
  request<DimensionLabel>('/admin/app-ops/dimension-labels', { method: 'POST', body: JSON.stringify(body) });
/** 改标签。名称/编码变了,已挂在它下面的问题会被后端一并改过去 */
export const updateDimensionLabel = (id: number, body: Partial<DimensionLabelDraft>) =>
  request<DimensionLabel>(`/admin/app-ops/dimension-labels/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
/** 删标签。下面还挂着问题时后端会拒绝(并说清还有几个) */
export const deleteDimensionLabel = (id: number) =>
  request<{ ok: boolean; message: string }>(`/admin/app-ops/dimension-labels/${id}`, { method: 'DELETE' });

export const getScenicCatalog = (params?: { keyword?: string; province?: string; grade?: string;
  /** 已登记 */ registered?: boolean;
  /** 已开通(绑定场景传 true:停用的景区绑上去不生效,不该出现在下拉里) */ enabled?: boolean;
  page?: number; page_size?: number }) => {
  const qsp = new URLSearchParams();
  if (params?.keyword) qsp.set('keyword', params.keyword);
  if (params?.province) qsp.set('province', params.province);
  if (params?.grade) qsp.set('grade', params.grade);
  if (params?.registered !== undefined) qsp.set('registered', String(params.registered));
  if (params?.enabled !== undefined) qsp.set('enabled', String(params.enabled));
  if (params?.page) qsp.set('page', String(params.page));
  if (params?.page_size) qsp.set('page_size', String(params.page_size));
  const q = qsp.toString();
  return request<{ items: ScenicCatalogItem[]; total: number; page: number; page_size: number }>(
    `/admin/scenics/catalog${q ? '?' + q : ''}`);
};
export const getScenicCatalogFilters = () =>
  request<{ provinces: string[]; grades: string[] }>('/admin/scenics/catalog/filters');
export const getScenicCatalogDetail = (scenicId: string) =>
  request<ScenicCatalogItem>(`/admin/scenics/catalog/${scenicId}`);
/** 登记景区:从主数据里挑若干条写进平台。已登记的会被跳过,不报错 */
export const registerScenics = (scenicIds: string[], enable = true) =>
  request<{ registered: number; skipped: number; items: string[] }>('/admin/scenics/register',
    { method: 'POST', body: JSON.stringify({ scenic_ids: scenicIds, enable }) });
/** 用主数据刷新已登记景区的名称/等级(不动开通状态与套餐) */
export const syncScenicsFromMaster = (scenicId?: string) =>
  request<{ updated: number; missing: string[] }>(
    `/admin/scenics/sync${scenicId ? `?scenic_id=${encodeURIComponent(scenicId)}` : ''}`, { method: 'POST' });

/** 当前账号的能力清单(角色 × 景区)。与后端执行的是同一套判定,前端照它显示按钮 */
export const getCapabilities = () => request<import('./capabilities').Capabilities>('/admin/auth/capabilities');

export const getRoles = () => request<any>("/admin/roles");
export const getGroupInfo = () => request<any>("/admin/group-info");

// ── 角色与权限管理 ──
export interface RoleDetailItem {
  value: string; label: string; scope: string;
  permissions: { resource: string; action: string; inherited: boolean; inherit_from?: string }[];
  inherited_from: string[];
}
export interface PolicyItem {
  id?: string; ptype: string; sub: string; obj: string; act: string;
}
export interface PolicyMatrix {
  roles: string[]; resources: string[];
  matrix: Record<string, Record<string, string[]>>;
}
export interface UsersByRoleItem {
  role: string; role_label: string; count: number; users: any[];
}
export const getRoleDetails = () => request<any>("/admin/roles/details");
export const addPolicy = (payload: { ptype: string; sub: string; obj: string; act: string }) =>
  request<any>("/admin/permissions", { method: "POST", body: JSON.stringify(payload) });
export const removePolicy = (payload: { ptype: string; sub: string; obj: string; act: string }) =>
  request<any>("/admin/permissions", { method: "DELETE", body: JSON.stringify(payload) });
export const getUsersByRole = () => request<any>("/admin/users/by-role");

// ── 系统功能说明(管理端文档,只读) ──
export interface FeatureDocItem {
  id: string; title: string; category: string; summary: string;
  related: string[]; updated_at: number; directory: string;
}
export interface FeatureDocDetail extends FeatureDocItem { content: string }
export const getFeatureDocs = () => request<{ items: FeatureDocItem[] }>("/admin/feature-docs");
export const getFeatureDoc = (id: string) => request<FeatureDocDetail>(`/admin/feature-docs/${id}`);

// ── 上下文参数源(28 号:装配器数据源连接器资源化,按端匹配) ──
// ══════════════════════════════════════════════════════════════════════
// 工具中心 · Quick BI(/admin/quickbi/*)
// ══════════════════════════════════════════════════════════════════════
export type QbiCapability = 'dataset_qa' | 'file_qa' | 'dashboard_qa' | 'insight' | 'report';
export interface QbiConnection {
  id: string; name: string; virtual: boolean; server_domain: string; api_key: string; api_secret: string;
  user_token: string; has_api_secret: boolean; has_user_token: boolean; use_trial: boolean; trial_active: boolean;
  default_locale: string; is_default: boolean; api_enabled: boolean; active: boolean; status: string;
  status_message: string | null; checked_at: string | null; visibility: string; scenic_id: string | null;
  owner_user_id: string | null; missing: string[]; created_at: string | null;
  /** 0075 分配:被分配的用户数;restricted=true 表示该连接已转白名单制 */
  granted_count?: number; restricted?: boolean;
}

// ── Quick BI 连接分配(0075:Quick BI 也进分发中心) ──
// 分配的是「能不能用这套凭证」,不是「用哪一版」—— 连接没有版本概念,所以不走版本指派那套。
export interface QbiGrant {
  id: string; connection_id: string; user_id: string;
  user_name: string | null; user_account: string | null;
  user_role: string | null; user_scenic_id: string | null;
  note: string; created_by: string; created_at: string; updated_at: string;
}
/** 该用户对某个连接的状态:granted 已分配 / not_granted 已启用白名单但没他 / open 该连接未启用白名单 */
export type QbiGrantState = 'granted' | 'not_granted' | 'open';
export interface QbiGrantByUserItem {
  connection_id: string; name: string; visibility: string; scenic_id: string | null;
  is_default: boolean; api_enabled: boolean; state: QbiGrantState;
  grant_id: string | null; note: string | null; granted_count: number;
}
export interface QbiOverview {
  capabilities: { key: QbiCapability; label: string; product: string; desc: string; stream: boolean }[];
  env_connection: QbiConnection | null; trial_configured: boolean;
  limits: { max_file_bytes: number; max_insight_chars: number; qa_suffixes: string[]; report_suffixes: string[] };
  scenarios: { key: string; label: string; desc: string }[];
}
export interface QbiChart {
  id: string; title: string; chart_type: string; row_count: number; sql?: string; conclusion?: string;
  fields: { name: string; type: string; role: 'metric' | 'dimension' }[]; rows: Record<string, any>[]; truncated?: boolean;
  /** 服务端配好的 ECharts option(智能体图表才有;Quick BI 自己的图没有)。有就直接用 ECharts 画 */
  option?: any;
}
export interface QbiAsset {
  id: string; connection_id: string; kind: 'dashboard' | 'dataset'; name: string; ref_id: string; url: string | null;
  chart_count: number; cube_count: number; cubes: { cube_id: string; cube_name: string }[];
  // 绑定的景区:空 = 集团级资产(所有景区都能用)。提问时按景区找看板靠的就是它。
  scenic_id?: string | null;
  // 停用的资产照样列出来(不列的话人只会觉得「资产凭空没了」),但选不了,并带上为什么
  active?: boolean; disabled_reason?: string | null;
  // 数据集从哪块板拉出来的:按它精确分组,没有(历史数据)才退回按 cube 反查
  source_asset_id?: string | null;
  /** 自定义分类(运营随手输入);空 = 未分类 */
  category?: string | null;
  created_at: string | null; updated_at: string | null; meta?: any;
}
export interface QbiFile {
  id: string; connection_id: string; file_name: string; size: number; capabilities: string[];
  qa_ready: boolean; report_ready: boolean; insight_ready: boolean; markdown_chars: number;
  created_at: string | null; warnings?: string[];
}
/**
 * Quick BI 固定问题。0079 起它就是统一问题表 scenic_agent_issue_detail 里
 * business_class='quick_bi' 的那一行(老表 tc_qbi_fixed_questions 已删除),
 * 所以 id 形如 issue_xxxx;APP 前台读的就是同一行 —— 后台存完前台立刻能看到。
 */
export interface QbiFixedQuestion {
  id: string; connection_id: string; title: string; question: string; capability: QbiCapability;
  capability_label: string; capability_source?: string;
  /** 与 connection_id 同值,文档/前端口径叫 quickbiLinkId */
  quickbi_link_id?: string;
  business_class?: string;
  /** 目标数据摊开的两列:编码(pageId/cubeId)与名称,列表直接显示,不必解析 target */
  target_data_id?: string | null; target_data_name?: string | null;
  /** 老表 ID(迁移过来的行才有),用于与历史运行记录/工具配置对账 */
  source_question_id?: string | null;
  target: Record<string, any>; locale: string | null; category: string | null;
  /** 数据域:票务/财务/舆情/客群/核销/营销/流量/画像…,取值不固定,可现场新增 */
  domain: string | null;
  /** 请求类别:qa 问数 / insight 解读 / report 报告。比 capability 粗一档,外部调用按它拉清单 */
  ask_type: string; ask_type_label: string;
  /** 口径说明:真正发给模型的那段约束(取哪个窗口、什么算异常、答多长)。
   *  发起时**追加**在问题后面,不替换用户原话 —— 按钮上的文案要短,模型要的恰恰是啰嗦的约束 */
  model_instruction?: string | null;
  /** 三级标签(主题→维度→指标)。APP 端的问题树按它挂载:三级为空的问题前台看不到 */
  topic: string | null; topic_id: string | null;
  dimension: string | null; dimension_id: string | null;
  metric: string | null; metric_id: string | null;
  scenic_id: string | null;
  sort_order: number; active: boolean; visibility: string; last_run_id: string | null;
  last_run_status: string | null; last_run_at: string | null; created_at: string | null;
  /** 加速(预热):开了之后后台按「刷新时间」提前跑好,前台/APP 再问同一景区的同一条问题
   *  直接回放,不必现跑。两个分钟数为空 = 跟随全局默认(刷新 30 / 存储 60) */
  prewarm_enabled?: boolean;
  prewarm_interval_minutes?: number | null;
  prewarm_ttl_minutes?: number | null;
  /** 当前账号能否修改 / 删除这条(与后端 save/delete 鉴权同口径):不能写的行切只读视图 */
  can_write?: boolean;
  can_delete?: boolean;
}

/** 目录:某景区能问什么(固定问题)、能问哪些数据(数据集/仪表板)。对外同名接口 /v1/quickbi/catalog */
export interface QbiCatalog {
  scenic_id: string | null;
  ask_types: { key: string; label: string }[];
  domains: string[];
  questions: {
    id: string; title: string; question: string; domain: string | null;
    ask_type: string; ask_type_label: string; capability: QbiCapability; capability_label: string;
    category: string | null; scenic_id: string | null; connection_id: string;
    /** 可直接拿去发起提问的数据标识:仪表板给 page_id、数据集给 cube_id,都附 asset_id */
    target: { kind?: string; asset_id?: string; name?: string; ref_id?: string; page_id?: string;
              cube_id?: string; file_id?: string; scenic_id?: string | null;
              cubes?: { cube_id: string; cube_name: string }[] };
  }[];
  datasets: { asset_id: string; name: string; cube_id: string; scenic_id: string | null }[];
  dashboards: { asset_id: string; name: string; page_id: string; scenic_id: string | null;
                cubes: { cube_id: string; cube_name: string }[] }[];
}
export interface QbiRun {
  id: string; connection_id: string; capability: QbiCapability; capability_label: string;
  /** 数据来源(数据集 / 上传文件 / 仪表板):名字只说「干了什么」,来源单独一栏 */
  capability_source?: string;
  mode: string;
  question: string; target: Record<string, any>; status: 'running' | 'success' | 'failed';
  session_id: string | null; fixed_question_id: string | null; chat_id: string | null; report_url: string | null;
  answer: string | null; error: string | null; error_code: string | null; trace_id: string | null;
  event_count: number; duration_ms: number | null; source: string; user_id: string;
  created_at: string | null; finished_at: string | null;
  /** 列表专用:所属固定问题的标题(运行问句已替换过变量、拼过口径,与标题不是同一段字) */
  fixed_question_title?: string | null;
  /** 触发这次运行的任务(APP 对话 / 执行引擎);管理台直接问为空 */
  task_id?: string | null;
  result?: { answer?: string; sections?: Record<string, string>; reasoning?: string; sql?: string; code?: string;
             charts?: QbiChart[]; steps?: any[]; errors?: any[]; usage?: any; report?: any; meta?: any };
}
export interface QbiSession {
  id: string; connection_id: string; title: string; capability: string; target: Record<string, any>;
  turn_count: number; archived: boolean; scenic_id?: string | null; source?: string;
  created_at: string | null; updated_at: string | null;
  messages?: QbiRun[];
}
/** Quick BI 统一事件(与后端 quickbi/events.py 一致) */
export interface QbiEvent { type: string; [k: string]: any }

const qbi = (p: string) => `/admin/quickbi${p}`;
const qs = (o: Record<string, any>) => {
  const s = new URLSearchParams();
  Object.entries(o).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') s.set(k, String(v)); });
  const t = s.toString();
  return t ? `?${t}` : '';
};
const jpost = (body: any, method = 'POST'): RequestInit => ({ method, body: JSON.stringify(body) });

export const qbiOverview = () => request<QbiOverview>(qbi('/overview'));
export const qbiListConnections = () => request<{ items: QbiConnection[] }>(qbi('/connections'));
export const qbiListGrants = (connectionId: string) =>
  request<{ items: QbiGrant[]; restricted: boolean }>(qbi(`/connections/${connectionId}/grants`));
export const qbiGrantConnection = (connectionId: string, payload: { user_id: string; note?: string }) =>
  request<{ ok: boolean; grant: QbiGrant; message: string; restricted: boolean }>(
    qbi(`/connections/${connectionId}/grants`), { method: 'POST', body: JSON.stringify(payload) });
export const qbiRevokeGrant = (connectionId: string, grantId: string) =>
  request<{ ok: boolean; message: string; restricted: boolean }>(
    qbi(`/connections/${connectionId}/grants/${grantId}`), { method: 'DELETE' });
export const qbiGrantsByUser = (userId: string) =>
  request<{ user: AgentByUserResult['user']; items: QbiGrantByUserItem[] }>(
    qbi(`/grants/by-user/${encodeURIComponent(userId)}`));
export const qbiCreateConnection = (p: Partial<QbiConnection> & { api_secret?: string; user_token?: string }) =>
  request<QbiConnection>(qbi('/connections'), jpost(p));
export const qbiUpdateConnection = (id: string, p: Record<string, any>) =>
  request<QbiConnection>(qbi(`/connections/${id}`), jpost(p, 'PUT'));
export const qbiPasteConnection = (text: string, name?: string, connection_id?: string, scenic_id?: string) =>
  request<QbiConnection & { parsed_keys: string[] }>(qbi('/connections/paste'),
    jpost({ text, name, connection_id, scenic_id }));
// ── 删除连接前的「转移」──
// 两个不同的「转移给谁」:接手的**连接**(必选,决定资产还能不能打开 —— 一条连接 = 一套凭证)
// 和**归属人**(可选,纯记账)。只改归属人不改连接的话,资产照样打不开。
export interface QbiTransferPlan {
  connection: { id: string; name: string; scenic_id: string | null };
  references: Record<string, number>;
  transferable: Record<string, number>;
  history: Record<string, number>;
  assets: { id: string; name: string; kind: string }[];
  // can_inherit:与原连接是同一个 Quick BI 工作空间(同域名 + 同 api_key)。
  // 资产存的 ref_id 是那边的 pageId/cubeId,只在同一空间里有效 —— 不同空间就只能停用,不能转。
  targets: { id: string; name: string; scenic_id: string | null; active: boolean; can_inherit: boolean }[];
  needs_transfer: boolean;
}
export const qbiConnectionTransferPlan = (id: string) =>
  request<QbiTransferPlan>(qbi(`/connections/${id}/transfer-plan`));
export const qbiTransferConnection = (id: string, payload: { to_connection_id: string; owner_user_id?: string }) =>
  request<{ ok: boolean; from: string; to: string; inherited: boolean;
            moved: Record<string, number>; disabled: Record<string, number>; message: string }>(
    qbi(`/connections/${id}/transfer`), { method: 'POST', body: JSON.stringify(payload) });

// force:还被问题/资产引用时默认拒绝并说清被谁引用;强制删除会把问题上的连接清空,
// 之后按景区自动重挑一条 —— 而不是留下一个指着已删除连接的死 id
export const qbiDeleteConnection = (id: string, force = false) =>
  request<{ ok: boolean; references: Record<string, number>; issues_cleared: number }>(
    qbi(`/connections/${id}${force ? '?force=true' : ''}`), { method: 'DELETE' });
export const qbiSetDefault = (id: string) => request(qbi(`/connections/${id}/default`), { method: 'POST' });
export const qbiCheckConnection = (id: string) =>
  request<{ ok: boolean; message: string; cube_count?: number; elapsed_ms: number; trial: boolean }>(
    qbi(`/connections/${id}/check`), { method: 'POST' });
export const qbiListCubes = (id: string, keyword?: string) =>
  request<{ items: { cube_id: string; cube_name: string }[] }>(qbi(`/connections/${id}/cubes${qs({ keyword })}`));

export const qbiParseDashboard = (url: string, connection_id?: string) =>
  request<any>(qbi('/dashboards/parse'), jpost({ url, connection_id }));
/** 改已登记资产的名称/归属景区。改的是同一行(asset_id 不变),
 *  所以指着这个资产的问题一条都不用动 —— 「删了重登记」会把它们全部打断 */
export const qbiUpdateAsset = (id: string, p: { name?: string; scenic_id?: string | null }) =>
  request<QbiAsset>(qbi(`/assets/${id}`), { method: 'PUT', body: JSON.stringify(p) });
export const qbiListAssets = (params: { connection_id?: string; kind?: string; scenic_id?: string } = {}) =>
  request<{ items: QbiAsset[] }>(qbi(`/assets${qs(params)}`));
export const qbiGetAsset = (id: string) => request<QbiAsset>(qbi(`/assets/${id}`));
/** 批量设数据集分类:空串 = 清掉分类 */
export const qbiSetAssetsCategory = (ids: string[], category: string) =>
  request<{ items: QbiAsset[]; category: string | null }>(qbi('/assets/category'), jpost({ ids, category }));
export const qbiRegisterDashboard = (url: string, connection_id?: string, name?: string, scenic_id?: string | null) =>
  request<QbiAsset>(qbi('/assets/dashboards'), jpost({ url, connection_id, name, scenic_id }));
export const qbiRegisterDataset = (cube_id: string, connection_id?: string, name?: string, scenic_id?: string | null) =>
  request<QbiAsset>(qbi('/assets/datasets'), jpost({ cube_id, connection_id, name, scenic_id }));
export const qbiRefreshAsset = (id: string) => request<QbiAsset>(qbi(`/assets/${id}/refresh`), { method: 'POST' });
// 按仪表板拉它用到的数据集 —— 比拉整条连接下的全部 cube 准:一条连接挂着两块板、几十个 cube,
// 人得自己认哪个属于哪块板,认错了就是拿 A 板的集去问 B 板的问题(答得出来,还不报错)
export const qbiBoardCubes = (assetId: string) =>
  request<{ board: { id: string; name: string; scenic_id: string | null };
            items: { cube_id: string; cube_name: string; registered: boolean }[] }>(
    qbi(`/assets/${assetId}/cubes`));
export const qbiRegisterDatasets = (p: {
  connection_id?: string; cubes: { cube_id: string; name?: string }[];
  scenic_id?: string | null; source_asset_id?: string | null;
  /** 一键登记全部:不传 cubes,后端实时拉该连接下全部可问数数据集(已登记的跳过) */
  all?: boolean;
}) => request<{ items: QbiAsset[]; created: number; skipped: number; message: string }>(
  qbi('/assets/datasets/batch'), jpost(p));
/** 清空这条连接下已登记的数据集(仪表板与上传文件不动);还被问题引用的按条跳过 */
export const qbiClearDatasets = (connection_id: string) =>
  request<{ deleted: number; skipped: number; reasons: string[] }>(
    qbi(`/assets/datasets/bulk${qs({ connection_id })}`), { method: 'DELETE' });
// 删资产 = 连带删掉它下面的数据集与问题(APP 与管理端随即都不再显示)。
// 先 qbiAssetDeletePlan 拿清单给人看,确认后再带 cascade 执行 ——
// 一句「确定删除吗」不说清会连带删掉 17 个集、12 条问题,人按下确定时并不知道自己同意了什么。
export interface QbiAssetDeletePlan {
  asset: { id: string; name: string; kind: string; scenic_id: string | null };
  datasets: { id: string; name: string }[];
  questions: { id: string; title: string; scenic_id: string | null }[];
  counts: Record<string, number>;
  needs_confirm: boolean;
}
export const qbiAssetDeletePlan = (id: string) =>
  request<QbiAssetDeletePlan>(qbi(`/assets/${id}/delete-plan`));
export const qbiDeleteAsset = (id: string, cascade = false) =>
  request<{ ok: boolean; deleted: Record<string, number>; message?: string }>(
    qbi(`/assets/${id}${cascade ? '?cascade=true' : ''}`), { method: 'DELETE' });
export const qbiAdoptQuestions = (id: string, questions?: any[]) =>
  request<{ items: QbiFixedQuestion[] }>(qbi(`/assets/${id}/adopt-questions`), jpost({ questions }));

export async function qbiUploadFile(file: File, connection_id?: string): Promise<QbiFile> {
  const fd = new FormData();
  fd.append('file', file);
  if (connection_id) fd.append('connection_id', connection_id);
  await ensureFreshToken();
  let res = await fetch(`${BASE}${qbi('/files')}`, { method: 'POST', body: fd, headers: authHeaders() });
  if (res.status === 401 && await tryRefresh()) {
    res = await fetch(`${BASE}${qbi('/files')}`, { method: 'POST', body: fd, headers: authHeaders() });
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok || (body && body.code !== undefined && body.code !== 0)) {
    throw new ApiError(body.message || `上传失败 (${res.status})`, res.status);
  }
  return (body && 'data' in body ? body.data : body) as QbiFile;
}
export const qbiListFiles = (connection_id?: string) => request<{ items: QbiFile[] }>(qbi(`/files${qs({ connection_id })}`));
export const qbiDeleteFile = (id: string) => request(qbi(`/files/${id}`), { method: 'DELETE' });

export const qbiListFixed = (params: { connection_id?: string; category?: string; keyword?: string;
  domain?: string; ask_type?: string; scenic_id?: string;
  topic_id?: string; dimension_id?: string; metric_id?: string;
  /** true = 只看还没挂三级标签的问题(这些在 APP 的问题树上没有挂载点,前台看不到) */
  unlabeled?: boolean;
  /** 加速:true 已开 / false 未开 */
  prewarm?: boolean;
  /** 最近运行:success / failed / running;none = 从未运行 */
  last_status?: string } = {}) =>
  request<{ items: QbiFixedQuestion[] }>(qbi(`/fixed-questions${qs(params)}`));
/** 数据域候选(预置 + 现场已用过的)与请求类别清单 */
export const qbiListDomains = () =>
  request<{ items: string[]; presets: string[]; ask_types: { key: string; label: string }[] }>(
    qbi('/fixed-questions/domains'));
export const qbiCatalog = (params: { connection_id?: string; scenic_id?: string; ask_type?: string;
  domain?: string; include_assets?: boolean } = {}) =>
  request<QbiCatalog>(qbi(`/catalog${qs(params)}`));
export const qbiSaveFixed = (p: Partial<QbiFixedQuestion>, id?: string) =>
  request<QbiFixedQuestion>(qbi(id ? `/fixed-questions/${id}` : '/fixed-questions'), jpost(p, id ? 'PUT' : 'POST'));
export const qbiDeleteFixed = (id: string) => request(qbi(`/fixed-questions/${id}`), { method: 'DELETE' });
/** 系统内置工具:不落库、不走版本、免配置,对所有技能默认可用 */
export interface BuiltinTool {
  name: string; description: string;
  parameters: string[]; required: string[];
  schema: Record<string, any>;
  /** 展示分组:时间 / 数据分析 / 计算 / 降级兜底 */
  group: string;
}
/** 统一问题表的一行。Quick BI 固定问题与自研智能体的快捷问题共用这张表,
 *  business_class 决定发起时走哪条链路 —— 两类问题在 APP 的问题树上是并排的。 */
export interface ScenicIssue {
  issue_id: string; business_class: 'quick_bi' | 'local_system';
  scenic_id: string | null; title: string; issue: string;
  topic: string | null; topic_id: string | null;
  dimension: string | null; dimension_id: string | null;
  metric: string | null; metric_id: string | null;
  /** 本地问答:绑定的执行目标。两个都为空 = 走意图路由挑默认 Agent */
  agent_id?: string | null; skill_id?: string | null;
  /** 口径说明:真正发给模型的那段约束,发起时追加在问题后面 */
  model_instruction?: string | null;
  param_template?: Record<string, any> | null;
  time_period?: string | null;
  connection_id?: string | null; capability?: string | null;
  target?: Record<string, any> | null; target_data_name?: string | null;
  sort_order: number; active: boolean;
  last_run_status?: string | null; last_run_time?: string | null;
  create_time?: string | null;
}
export const listScenicIssues = (p: { business_class?: string; scenic_id?: string;
  topic_id?: string; keyword?: string; active?: boolean } = {}) =>
  request<{ items: ScenicIssue[] }>(`/admin/app-ops/issues${qs(p)}`);
export const createScenicIssue = (p: Partial<ScenicIssue>) =>
  request<ScenicIssue>('/admin/app-ops/issues', jpost(p));
export const updateScenicIssue = (id: string, p: Partial<ScenicIssue>) =>
  request<ScenicIssue>(`/admin/app-ops/issues/${id}`, jpost(p, 'PATCH'));
export const deleteScenicIssue = (id: string) =>
  request(`/admin/app-ops/issues/${id}`, { method: 'DELETE' });

export const listBuiltinTools = () =>
  request<{ items: BuiltinTool[]; note: string }>('/admin/tools/builtin');

export const qbiBatchRun = (ids: string[], params: Record<string, any> = {}) =>
  request<{ items: any[] }>(qbi('/fixed-questions/batch-run'), jpost({ ids, params }));
/** 立刻重新跑一遍并刷新缓存(列表上的「加速」按钮)。报告类创建即返回,其余等结果。 */
export const qbiPrewarmFixed = (id: string, scenic_id?: string) =>
  request<{ ok: boolean; run_id: string | null; status: string; error?: string | null;
    duration_ms: number; scenic_id: string | null;
    prewarm_interval_minutes: number; prewarm_ttl_minutes: number }>(
    qbi(`/fixed-questions/${id}/prewarm`), jpost({ scenic_id: scenic_id || null }));

export const qbiListSessions = () => request<{ items: QbiSession[] }>(qbi('/sessions'));
export const qbiCreateSession = (p: { connection_id?: string; capability?: string; target?: any; title?: string;
  scenic_id?: string }) =>
  request<QbiSession>(qbi('/sessions'), jpost(p));
export const qbiGetSession = (id: string) => request<QbiSession>(qbi(`/sessions/${id}`));
export const qbiArchiveSession = (id: string) => request(qbi(`/sessions/${id}`), { method: 'DELETE' });

export const qbiStartReport = (p: { connection_id?: string; question: string; target?: any; locale?: string }) =>
  request<QbiRun>(qbi('/reports'), jpost({ ...p, stream: false }));
export const qbiGetReport = (id: string, refresh = true) => request<QbiRun>(qbi(`/reports/${id}${qs({ refresh })}`));

export const qbiListRuns = (params: { capability?: string; mode?: string; status?: string; session_id?: string;
  fixed_question_id?: string;
  /** 按问题名称搜:问句正文或所属固定问题的标题命中都算 */
  q?: string;
  /** 按触发任务查(APP 对话 / 执行引擎的任务号) */
  task_id?: string; limit?: number; offset?: number } = {}) =>
  request<{ items: QbiRun[] }>(qbi(`/runs${qs(params)}`));
export const qbiGetRun = (id: string) => request<QbiRun>(qbi(`/runs/${id}`));

export interface QbiAbortResult { terminated: number; ids: string[]; hint?: string; status?: string }
/** 终止单条进行中的运行(后端落 failed + error_code=aborted) */
export const qbiAbortRun = (id: string) => request<QbiAbortResult>(qbi(`/runs/${id}/abort`), { method: 'POST' });
/** 一键终止:可见范围内所有「进行中」的运行 */
export const qbiAbortAllRuns = () => request<QbiAbortResult>(qbi('/runs/abort-all'), { method: 'POST' });

/** 批量动作的逐条事件(终止运行 / 固定问题批量运维共用):start → item × N → done */
export interface QbiBatchEvent {
  type: 'start' | 'item' | 'done';
  op?: string; total?: number; ignored?: number; message?: string;
  index?: number; id?: string; title?: string | null; ok?: boolean; new_id?: string | null;
  succeeded?: number; failed?: number;
}
/** 批量终止(流式):逐条回报结果,事件喂给日志弹窗 */
export const qbiAbortRunsStream = (body: { ids?: string[] }, onEvent: (e: QbiBatchEvent) => void,
  signal?: AbortSignal) => qbiStream('/runs/abort-batch', body, onEvent as (e: QbiEvent) => void, signal);

/** 固定问题批量运维的操作类型(批量执行另走 qbiBatchRun) */
export type QbiBatchOp = 'set_interval' | 'copy' | 'insight' | 'delete';
/** 批量运维(流式):逐条回报结果,一条失败不中断其余 */
export const qbiBatchOpStream = (body: { op: QbiBatchOp; ids: string[];
  /** set_interval:刷新 / 存储时间(分钟);存储不填 = 原值,比刷新短时自动抬到刷新时间 */
  interval_minutes?: number; ttl_minutes?: number;
  /** set_interval:true 一并开启加速 / false 关闭 / 不传 = 不动开关 */
  prewarm_enabled?: boolean;
  /** copy:复制到哪条连接 */
  connection_id?: string },
  onEvent: (e: QbiBatchEvent) => void, signal?: AbortSignal) =>
  qbiStream('/fixed-questions/batch-op', body, onEvent as (e: QbiEvent) => void, signal);

export interface QbiThinking {
  /** false = 这条问题还没有成功的解读/报告类运行,reason 里是说给人听的下一步 */
  found: boolean; reason?: string;
  run_id?: string; created_at?: string | null; finished_at?: string | null;
  /** 只取解读 / 报告:问数类的 thinking 是 NL2SQL 取数日志,不是成文思考 */
  capability?: string; capability_label?: string; duration_ms?: number | null;
  /** 当次实际发出去的问句:改了口径之后,靠它核对这份思考答的是哪一版 */
  question?: string;
  text?: string;
}
export const qbiLatestThinking = (fqId: string) => request<QbiThinking>(qbi(`/fixed-questions/${fqId}/thinking`));

/** 任务队列水位(全局一条队:APP 预热与 APP / 开放平台 / 操练台共用) */
export interface QbiQueueStats {
  queued: number; running: number;
  prewarm: { queued: number; running: number };
  others: { queued: number; running: number };
  oldest_queued_seconds: number;
  updated_at: string;
}
export const qbiQueueStats = () => request<QbiQueueStats>(qbi('/fixed-questions/queue-stats'));

/** 口径说明库:一段可复用的口径 / 约束。导入 = 抄进固定问题的口径说明(不是引用,改库不改已配的问题) */
export interface QbiPrompt {
  id: string; name: string; content: string; description: string | null;
  owner_user_id: string; scenic_id: string | null; visibility: string;
  created_at: string | null; updated_at: string | null;
  can_write?: boolean;
}
export const qbiListPrompts = (keyword?: string) =>
  request<{ items: QbiPrompt[] }>(qbi(`/prompts${qs({ keyword })}`));
export const qbiSavePrompt = (p: { name?: string; content?: string; description?: string; visibility?: string }, id?: string) =>
  request<QbiPrompt>(qbi(id ? `/prompts/${id}` : '/prompts'), jpost(p, id ? 'PUT' : 'POST'));
export const qbiDeletePrompt = (id: string) => request(qbi(`/prompts/${id}`), { method: 'DELETE' });

/** 景区数据集映射:一景区一行 = 用哪条连接 + 哪个数据集取数 + 景区级加速预热开关 */
export interface QbiScenicDataset {
  id: string; scenic_id: string; connection_id: string; connection_name: string | null;
  cube_id: string; cube_name: string; prewarm_enabled: boolean; source?: string | null;
  updated_by?: string | null; updated_at: string | null; created_at: string | null;
}
export const qbiListScenicDatasets = () => request<{ items: QbiScenicDataset[] }>(qbi('/scenic-datasets'));
export const qbiUpsertScenicDataset = (p: { scenic_id: string; connection_id: string; cube_id: string;
  cube_name?: string | null; prewarm_enabled?: boolean }) =>
  request<QbiScenicDataset>(qbi('/scenic-datasets'), jpost(p));
export const qbiSetScenicDatasetPrewarm = (scenicId: string, enabled: boolean) =>
  request<QbiScenicDataset>(qbi(`/scenic-datasets/${encodeURIComponent(scenicId)}/prewarm`), jpost({ enabled }, 'PUT'));
export const qbiDeleteScenicDataset = (scenicId: string) =>
  request(qbi(`/scenic-datasets/${encodeURIComponent(scenicId)}`), { method: 'DELETE' });
/** 导出一次运行的执行详情(Markdown,可直接发给研发排障)。凭证不在导出内容里。 */
export async function qbiRunDebugMarkdown(runId: string): Promise<string> {
  await ensureFreshToken();
  const res = await fetch(`${BASE}${qbi(`/runs/${runId}/debug?format=md`)}`, { headers: authHeaders() });
  if (!res.ok) throw new ApiError(`导出失败 (${res.status})`, res.status);
  return res.text();
}
export const qbiRunDebug = (runId: string) => request<any>(qbi(`/runs/${runId}/debug`));

/**
 * 导出一次**智能体**执行的完整链条(Markdown)。
 *
 * 与 Quick BI 的 qbiRunDebugMarkdown 对称 —— 自己这条链此前没有对应的东西,
 * 排障只能靠截图,而截图里恰恰缺最要紧的那几样:每一步的提示词、工具入参、结果摘要。
 * 凭证已脱敏,可以直接转发。
 */
export async function taskDebugMarkdown(taskId: string): Promise<string> {
  await ensureFreshToken();
  const res = await fetch(`${BASE}/admin/tasks/${taskId}/debug?format=md`, { headers: authHeaders() });
  if (!res.ok) throw new ApiError(`导出失败 (${res.status})`, res.status);
  return res.text();
}
export const taskDebug = (taskId: string) => request<any>(`/admin/tasks/${taskId}/debug`);
export const qbiPublishTool = (p: { name: string; description?: string; connection_id?: string; capability: string;
  target?: any; fixed_question_id?: string; report_wait?: boolean; max_rows?: number; visibility?: string }) =>
  request<any>(qbi('/tools'), jpost(p));

/** POST + SSE:Quick BI 执行类接口统一的流式读取(fetch + ReadableStream,支持 Authorization 头)。 */
export async function qbiStream(path: string, body: any, onEvent: (e: QbiEvent) => void,
                                signal?: AbortSignal, method: 'POST' | 'GET' = 'POST'): Promise<void> {
  await ensureFreshToken();
  const open = () => fetch(`${BASE}${qbi(path)}`, {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...authHeaders() },
    body: method === 'POST' ? JSON.stringify({ ...body, stream: true }) : undefined,
    signal,
  });
  let res = await open();
  if (res.status === 401 && await tryRefresh()) res = await open();
  if (!res.ok || !res.body) {
    const err = await res.json().catch(() => ({}));
    throw new ApiError(err.message || `请求失败 (${res.status})`, res.status);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const flush = (raw: string) => {
    const data: string[] = [];
    for (const line of raw.split(/\r\n|\n|\r/)) {
      if (line.startsWith('data:')) data.push(line.slice(5).startsWith(' ') ? line.slice(6) : line.slice(5));
    }
    if (!data.length) return;
    try { onEvent(JSON.parse(data.join('\n'))); } catch { /* 心跳等非 JSON 行忽略 */ }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split(/\r\n\r\n|\n\n|\r\r/);
    buffer = parts.pop() ?? '';
    parts.forEach(flush);
  }
  if (buffer.trim()) flush(buffer);
}
export interface QbiRunBody {
  connection_id?: string;
  capability?: string;
  question?: string;
  target?: any;
  locale?: string;
  /** 本次提问归属的景区;不传按当前账号所属景区。没指定 target 时按该景区挑已登记的看板/数据集 */
  scenic_id?: string;
  /** 按固定问题发起:只给 id 也能问,问题文本/能力/目标数据从那条配置带出 */
  fixed_question_id?: string;
  /** 归属会话:传了这一轮才会出现在该会话的历史里(操练台按会话组织对话) */
  session_id?: string;
}
export const qbiRunStream = (body: QbiRunBody, onEvent: (e: QbiEvent) => void, signal?: AbortSignal) =>
  qbiStream('/run', body, onEvent, signal);
export const qbiFixedStream = (id: string, params: Record<string, any>, onEvent: (e: QbiEvent) => void, signal?: AbortSignal) =>
  qbiStream(`/fixed-questions/${id}/run`, { params }, onEvent, signal);
export const qbiChatStream = (sessionId: string, body: { question: string; capability?: string; target?: any },
  onEvent: (e: QbiEvent) => void, signal?: AbortSignal) => qbiStream(`/sessions/${sessionId}/messages`, body, onEvent, signal);
export const qbiReportStream = (body: { connection_id?: string; question: string; target?: any },
  onEvent: (e: QbiEvent) => void, signal?: AbortSignal) => qbiStream('/reports', body, onEvent, signal);
export const qbiTrackReport = (runId: string, onEvent: (e: QbiEvent) => void, signal?: AbortSignal) =>
  qbiStream(`/reports/${runId}/stream`, undefined, onEvent, signal, 'GET');

// ── 开放平台 API Key(/admin/api-keys) ──
export interface ApiKeyItem { id: string; prefix_display: string; scenic_id: string | null; agent_allowed_ids: string[];
  revoked: boolean; expires_at: string | null; created_at: string | null }
export const listApiKeys = () => request<ApiKeyItem[]>('/admin/api-keys');
export const createApiKey = (p: { agent_allowed_ids?: string[]; expires_in_days?: number | null }) =>
  request<ApiKeyItem & { key: string }>('/admin/api-keys', { method: 'POST', body: JSON.stringify(p) });
export const revokeApiKey = (id: string) => request<{ ok: boolean }>(`/admin/api-keys/${id}/revoke`, { method: 'POST' });
