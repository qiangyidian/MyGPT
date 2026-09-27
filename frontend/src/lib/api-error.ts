/** 统一的中文错误映射。
 *
 * 目标：所有面向用户的失败提示走同一个漏斗，而不是每个组件自己 `err.message`。
 * 覆盖后端真实会返回的几种形状：
 *   1. `api.ts` 的 `ApiError`（`status` / `code` / `message` / `detail`）；
 *   2. 后端统一信封 `{"code","message"}`：`AppException` 的额外负载在 `details`
 *      （如 `{"errors": [...]}`、`{"quota": {...}}`），普通 `HTTPException` 会
 *      额外回声一份 `detail`（见 `app/core/exceptions.py`）；
 *   3. 没被处理器包住的 FastAPI 默认形状 `{"detail": ...}` —— `detail` 可能是
 *      字符串，也可能是 pydantic 的校验错误数组；
 *   4. 原生 `fetch` 失败：`TypeError("Failed to fetch")`、`AbortError`、
 *      `AbortSignal.timeout()` 抛出的 `TimeoutError`。
 *
 * 纯函数、不碰 DOM、也不 import `api.ts`（那个模块在浏览器里缺
 * `NEXT_PUBLIC_API_BASE_URL` 时会直接抛错），所以能在 `environment: "node"`
 * 下做单测。原始信息只出现在 `detail` 字段里，供调试面板单独展开。
 */

/** `message` 永远是可直接展示的中文；堆栈 / SQL / 异常类名永不进入它。 */
export interface UserError {
  message: string;
  status?: number;
  code?: string;
  /** 服务端原始文案（英文、含细节都无妨），仅供调试 UI 展示。 */
  detail?: string;
}

const UNKNOWN_ZH = "操作失败，请稍后重试";
const NETWORK_ZH = "网络连接中断，请稍后重试";
const OFFLINE_ZH = "网络连接已断开，请检查网络后重试";
const TIMEOUT_ZH = "请求超时，请稍后重试";
const ABORTED_ZH = "请求已取消";

/** 状态码兜底文案：只在服务端没给出可用文案时使用。 */
const STATUS_ZH: Record<number, string> = {
  0: NETWORK_ZH,
  400: "请求无法处理，请检查填写内容后重试",
  401: "未登录或登录已过期，请重新登录",
  403: "没有权限执行该操作",
  404: "未找到相关内容，可能已被删除",
  409: "操作冲突，请稍后重试",
  413: "内容过大，请精简后重试",
  422: "提交的内容格式不正确，请检查后重试",
  429: "触发限流，请稍后再试",
  500: "服务暂时不可用，请稍后重试",
  502: "服务暂时不可用，请稍后重试",
  503: "服务繁忙，请稍后重试",
  504: "服务响应超时，请稍后重试",
};

/** 稳定 `code`（`app/core/exceptions.py` + 各 service）→ 中文。 */
const CODE_ZH: Record<string, string> = {
  network_error: NETWORK_ZH,
  offline: OFFLINE_ZH,
  timeout: TIMEOUT_ZH,
  aborted: ABORTED_ZH,
  stream_disconnected: "连接已中断，请重试",
  unauthorized: "未登录或登录已过期，请重新登录",
  forbidden: "没有权限执行该操作",
  not_found: "未找到相关内容，可能已被删除",
  conflict: "操作冲突，请稍后重试",
  payload_too_large: "内容过大，请精简后重试",
  rate_limited: "触发限流，请稍后再试",
  quota_exceeded: "已达用量上限，请稍后再试",
  internal: "服务暂时不可用，请稍后重试",
  // SSE 的错误帧用的是另一套 code（`app/api/chat.py`、`chat_service`）。
  internal_error: "服务暂时不可用，请稍后重试",
  model_config_not_found: "所选模型已不存在，请重新选择",
  bad_request: "请求无法处理，请检查填写内容后重试",
  validation: "提交的内容格式不正确，请检查后重试",
  insufficient_credits: "积分不足，请先兑换后再继续对话",
  turn_in_progress: "上一条回复还在生成中，请先停止或稍候再发送",
  run_queue_unavailable: "后台队列暂不可用，请稍后重试",
  no_model_configured: "还没有可用模型，请先在设置中添加模型",
  model_not_found: "所选模型已不存在，请重新选择",
  conversation_not_found: "会话不存在或已被删除",
  knowledge_base_not_found: "知识库不存在或已被删除",
  document_not_found: "文档不存在或已被删除",
  document_too_large: "文档过大，请精简后重试",
  document_type_not_allowed: "不支持的文件类型，请更换格式后重试",
  attachment_not_found: "附件不存在或已被删除",
  attachment_too_large: "附件过大，请精简后重试",
  attachment_type_not_allowed: "不支持的附件类型，请更换格式后重试",
  attachment_mime_mismatch: "附件内容与所选类型不一致，请重新上传",
  attachment_limit: "附件数量已达上限，请先删除部分附件",
  artifact_not_found: "文件不存在或已被清理",
  artifact_too_large: "文件过大，请精简后重试",
  artifact_checksum_mismatch: "文件校验失败，请重新上传",
  project_not_found: "项目不存在或已被删除",
  user_not_found: "用户不存在",
  connector_not_found: "连接器不存在或已被删除",
  run_not_found: "任务不存在或已被清理",
  approval_not_found: "该审批已失效",
  nothing_to_regenerate: "这条消息没有可重新生成的内容",
  password_too_short: "密码长度不符合要求",
  password_too_weak: "密码需包含大写字母、小写字母和数字",
};

/** 后端残留的英文文案 → 中文（未收录的英文不会直接展示给用户）。 */
const SERVER_MESSAGE_ZH: Record<string, string> = {
  "Invalid email or password": "邮箱或密码错误",
  "Account disabled": "账号已被禁用",
  "Email or username already registered": "邮箱或用户名已被注册",
  "Missing refresh token": "登录状态异常，请重新登录",
  "Invalid or expired refresh token": "未登录或登录已过期，请重新登录",
  "Wrong token type": "登录状态异常，请重新登录",
  "Invalid token payload": "登录状态异常，请重新登录",
  "Token revoked": "登录状态已失效，请重新登录",
  "User not found": "用户不存在",
  "Conversation not found": "会话不存在或已被删除",
  "Conversation or message not found": "会话或消息不存在",
  "Knowledge base not found": "知识库不存在或已被删除",
  "Model config not found": "所选模型已不存在，请重新选择",
  "Model not found": "所选模型已不存在，请重新选择",
  "No model is configured yet": "还没有可用模型，请先在设置中添加模型",
  "Not your conversation": "无权访问该会话",
  "Nothing to regenerate": "这条消息没有可重新生成的内容",
  "Connector not found": "连接器不存在或已被删除",
  "Run not found": "任务不存在或已被清理",
  "Approval not found": "该审批已失效",
  "artifact not found": "文件不存在或已被清理",
  "Internal server error": "服务暂时不可用，请稍后重试",
  "Internal Server Error": "服务暂时不可用，请稍后重试",
  "Service Unavailable": "服务繁忙，请稍后重试",
  "Bad Gateway": "服务暂时不可用，请稍后重试",
  "Gateway Timeout": "服务响应超时，请稍后重试",
  "Not Found": "未找到相关内容，可能已被删除",
  "Method Not Allowed": "该操作不被支持，请刷新后重试",
  "Validation failed": "提交的内容格式不正确，请检查后重试",
};

/** pydantic `loc` 末段字段名 → 中文标签（表单里能一眼看出是哪一栏）。 */
const FIELD_LABEL_ZH: Record<string, string> = {
  body: "提交内容",
  email: "邮箱",
  username: "用户名",
  password: "密码",
  code: "兑换码",
  verification_code: "邮箱验证码",
  wechat_code: "公众号验证码",
  name: "名称",
  title: "标题",
  description: "描述",
  content: "内容",
  new_content: "新内容",
  file: "文件",
  files: "文件",
  delta: "调整分值",
  note: "备注",
  count: "数量",
  credits_per_code: "每码积分",
  expires_at: "有效期",
  role: "角色",
  is_active: "启用状态",
  knowledge_base_id: "知识库",
  knowledge_base_ids: "知识库",
  model_id: "模型",
  embedding_model_id: "向量模型",
  provider: "Provider",
  api_key: "API 密钥",
  api_base_url: "API 地址",
  model_name: "模型名",
  embedding_model_name: "向量模型名",
  temperature: "温度",
  top_p: "采样阈值",
  top_k: "召回条数",
  max_tokens: "最大输出长度",
  max_context_tokens: "最大上下文长度",
  chunk_size: "分块长度",
  chunk_overlap: "分块重叠",
  score_threshold: "相似度阈值",
  rerank_enabled: "重排序",
  system_prompt: "系统提示词",
  instruction: "补充指令",
  steps: "步骤",
  summary: "摘要",
  message_id: "消息",
  query: "检索内容",
  credentials: "凭证",
};

/** 原始文案里一旦混进这些，就绝不展示（宁可退回通用文案）。 */
const INTERNAL_PATTERNS: RegExp[] = [
  /traceback \(most recent call last\)/i,
  /\b\w+(?:Error|Exception|Timeout)\b\s*:/,
  /select\s+.+\s+from\s+/i,
  /insert into\s|update\s+\w+\s+set\s|delete from\s/i,
  /psycopg|asyncpg|sqlalchemy|qdrant|redis|httpx|openai|anthropic/i,
  /unique constraint|foreign key|operationalerror|integrityerror|programmingerror/i,
  /connection (?:refused|reset|closed|failed)/i,
  /externalprotocolerror|fetcheddataerror|certificate verify failed/i,
  /\.py",? line \d+/i,
  /File "[\w./\\-]+"/,
  /\bat\s+\w+\s+\([\w./-]+:\d+:\d+\)/,
];

/** 网络层失败（拿不到 HTTP 状态）的识别。 */
const NETWORK_PATTERNS: RegExp[] = [
  /failed to fetch/i,
  /load failed/i,
  /networkerror/i,
  /network request failed/i,
  /network is (?:in )?reachable/i,
  /err_connection/i,
  /socket hang up/i,
];

const TIMEOUT_PATTERNS: RegExp[] = [/timeout/i, /timed out/i];

/**
 * 上游模型服务的英文异常串（`/api/models/{id}/test` 会把 `type(exc): exc`
 * 原样带回）。这些不是我们的业务错误，但也不能把 `ConnectError: …` 直接甩给
 * 用户，所以按现象归类成可执行的中文。
 */
const UPSTREAM_PATTERNS: Array<[RegExp, string]> = [
  [/invalid[^\n]{0,20}(api[ _-]?key)|incorrect api[ _-]?key|no api key|authentication(?:failed| error)|invalid_?api_?key/i,
    "模型服务鉴权失败，请检查 API 密钥"],
  [/insufficient[^\n]{0,20}(quota|balance|credit)|billing|exceeded your current quota/i,
    "模型账户额度或余额不足"],
  [/rate.?limit|too many requests|slow down/i, "模型服务触发限流，请稍后再试"],
  [/connecterror|connection refused|getaddrinfo|name or service not known|couldn.?t connect|failed to establish a new connection|\bdns\b/i,
    "无法连接到模型服务，请检查 API 地址"],
  [/ssl|certificate verify failed|handshake/i, "模型服务证书校验失败"],
  [/(model|engine)[^\n]{0,30}(not found|does not exist|invalid|not exist)/i,
    "模型名不存在，请检查模型名"],
  [/content_filter|content policy|safety/i, "内容被模型服务的安全策略拦截"],
];

function classifyUpstreamText(text: string): string | null {
  if (hasCjk(text)) return null; // 中文文案在前面就已经用掉了
  for (const [re, zh] of UPSTREAM_PATTERNS) if (re.test(text)) return zh;
  return null;
}

function hasCjk(text: string): boolean {
  // 汉字 + 日文假名：后端开发者手写的文案一定是中文，可信且优先。
  return /[㐀-䶿一-鿿぀-ヿ]/.test(text);
}

function isInternalText(text: string): boolean {
  return INTERNAL_PATTERNS.some((re) => re.test(text));
}

function classifyNetworkText(text: string): string | null {
  if (TIMEOUT_PATTERNS.some((re) => re.test(text))) return TIMEOUT_ZH;
  if (NETWORK_PATTERNS.some((re) => re.test(text))) return NETWORK_ZH;
  return null;
}

/** 服务端文案能否直接给用户：已收录的英文，或「中文且不像内部信息」。 */
function usableServerMessage(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const known = SERVER_MESSAGE_ZH[trimmed];
  if (known) return known;
  if (!hasCjk(trimmed)) return null;
  if (isInternalText(trimmed)) return null;
  return trimmed;
}

interface ValidationItem {
  loc?: unknown;
  msg?: unknown;
  type?: unknown;
}

/** `loc` 末段：`["body","email"]` → 「邮箱」；数组下标折进「第 N 项」。 */
function fieldNameOf(loc: unknown): string {
  if (!Array.isArray(loc)) return "提交内容";
  let name = "";
  let index = "";
  for (const part of loc) {
    if (typeof part === "number") {
      index = String(part);
    } else if (
      typeof part === "string" &&
      part !== "body" &&
      part !== "query" &&
      part !== "path"
    ) {
      name = part;
    }
  }
  const base = FIELD_LABEL_ZH[name] ?? (name ? `字段 ${name}` : "提交内容");
  return index ? `${base}第 ${Number(index) + 1} 项` : base;
}

/** 校验规则翻译结果：谓语（拼在字段名后）或完整短句（自带语义，用「：」连接）。 */
type ValidationRule = { predicate: string } | { sentence: string };

/** 已知英文校验规则 → 中文；认不出的返回 null（由调用方按字段名兜底）。 */
function translateValidationMsg(raw: string): ValidationRule | null {
  const msg = raw.trim();
  if (!msg) return null;
  const predicate = (text: string): ValidationRule => ({ predicate: text });
  const grab = (re: RegExp): string | null => {
    const m = re.exec(msg);
    return m ? m[1] : null;
  };
  if (/^(Field required|Missing|none is not an allowed value)$/i.test(msg)) {
    return predicate("不能为空");
  }
  const charsMin = grab(/\b(?:at least|more than) (\d+) character/i);
  if (charsMin !== null) return predicate(`长度至少 ${charsMin} 个字符`);
  const charsMax = grab(/\b(?:at most|less than) (\d+) character/i);
  if (charsMax !== null) return predicate(`长度最多 ${charsMax} 个字符`);
  const ge = grab(/greater than or equal to (-?[\d.]+)/i);
  if (ge !== null) return predicate(`不能小于 ${ge}`);
  const le = grab(/less than or equal to (-?[\d.]+)/i);
  if (le !== null) return predicate(`不能大于 ${le}`);
  const gt = grab(/greater than (-?[\d.]+)/i);
  if (gt !== null) return predicate(`必须大于 ${gt}`);
  const lt = grab(/less than (-?[\d.]+)/i);
  if (lt !== null) return predicate(`必须小于 ${lt}`);
  const min = grab(/\bat least (-?[\d.]+)/i);
  if (min !== null) return predicate(`不能少于 ${min}`);
  const max = grab(/\bat most (-?[\d.]+)/i);
  if (max !== null) return predicate(`不能超过 ${max}`);
  if (/extra inputs are not permitted/i.test(msg)) return predicate("包含不支持的项");
  if (/string should match pattern/i.test(msg)) return predicate("格式不正确");
  // 枚举类：`Input should be 'speed' or 'expert'`（v2）/ 非法取值（v1）。
  if (/^Input should be (?!a valid )/i.test(msg)) {
    return predicate("取值不在允许范围内");
  }
  const kind =
    grab(/^Input should be a valid ([a-z ]+?)(?:\s*\(.*\))?$/i) ??
    grab(/^value is not a valid ([a-z ]+)$/i);
  if (kind) {
    const VALID_KINDS: Record<string, string> = {
      "email address": "格式不正确",
      url: "应为合法链接",
      integer: "应为整数",
      number: "应为数字",
      "finite number": "应为数字",
      string: "应为文本",
      boolean: "应为是/否",
      list: "应为列表",
      set: "应为列表",
      dictionary: "应为对象",
      object: "应为对象",
      json: "应为合法的 JSON",
    };
    const hit = VALID_KINDS[kind.trim()];
    if (hit) return predicate(hit);
  }
  // 自定义校验器抛的中文（`Value error, 该邮箱已被注册`）本身就是完整短句。
  if (/^Value error, /i.test(msg)) {
    const inner = usableServerMessage(msg.replace(/^Value error, /i, ""));
    return inner ? { sentence: inner } : null;
  }
  return null;
}

/** 校验错误数组 → 一行中文（最多 3 条，多余的汇总）。 */
function formatValidationErrors(errors: readonly unknown[]): string | null {
  const lines: string[] = [];
  for (const item of errors) {
    if (!item || typeof item !== "object") continue;
    const { loc, msg, type } = item as ValidationItem;
    const name = fieldNameOf(loc);
    const rule = typeof msg === "string" ? translateValidationMsg(msg) : null;
    if (!rule) {
      const missing = typeof type === "string" && /missing/i.test(type);
      lines.push(`${name}${missing ? "不能为空" : "格式不正确"}`);
    } else if ("predicate" in rule) {
      lines.push(`${name}${rule.predicate}`);
    } else {
      // 完整短句自带语义时不再拼字段名，否则会出现「邮箱邮箱格式不正确」。
      lines.push(
        name === "提交内容" || rule.sentence.includes(name)
          ? rule.sentence
          : `${name}：${rule.sentence}`
      );
    }
  }
  if (!lines.length) return null;
  const shown = lines.slice(0, 3);
  if (lines.length > shown.length) {
    shown.push(`另有 ${lines.length - shown.length} 项填写有误`);
  }
  return shown.join("；");
}

/** 取出校验数组：`detail`（FastAPI 默认）或 `details.errors`（后端信封）。 */
function validationErrorsOf(source: Record<string, unknown>): unknown[] | null {
  const direct = source.detail ?? source.errors;
  if (Array.isArray(direct) && direct.length > 0) return direct;
  const details = source.details;
  if (details && typeof details === "object" && !Array.isArray(details)) {
    const nested = (details as Record<string, unknown>).errors;
    if (Array.isArray(nested)) return nested.length ? nested : null;
  }
  if (Array.isArray(source.message) && source.message.length > 0) {
    return source.message as unknown[];
  }
  return null;
}

// --- 形状拆解 ---------------------------------------------------------------

interface Shape {
  status?: number;
  code?: string;
  texts: string[];
  errors?: unknown[];
}

function textOf(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

/** `{"detail":"…"}` / `{"code","message","details"}` / ApiError 实例。 */
function shapeOfObject(source: Record<string, unknown>): Shape {
  // `status` 只在真的是数字/数字串时才算 HTTP 状态码：`Number(null)` 是 0，
  // 会把「没有状态码」错认成网络失败。
  const rawStatus = source.status ?? source.statusCode;
  const parsed =
    typeof rawStatus === "number" || typeof rawStatus === "string"
      ? Number(rawStatus)
      : NaN;
  const status = Number.isFinite(parsed) && parsed >= 0 ? Math.trunc(parsed) : undefined;
  const code = textOf(source.code);
  const texts: string[] = [];
  for (const key of ["message", "detail", "error", "reason"] as const) {
    const t = textOf(source[key]);
    if (t) texts.push(t);
  }
  const errors = validationErrorsOf(source) ?? undefined;
  return { status, code: code ?? undefined, texts, errors };
}

/** 字符串形态：可能就是一句文案，也可能整段是 JSON body。 */
function shapeOfString(text: string): Shape {
  const trimmed = text.trim();
  if (/^[{[]/.test(trimmed)) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return { texts: [], errors: parsed };
      if (parsed && typeof parsed === "object") {
        return shapeOfObject(parsed as Record<string, unknown>);
      }
    } catch {
      /* 不是 JSON：当普通文案处理 */
    }
  }
  return { texts: [text] };
}

function shapeOf(error: unknown): Shape {
  if (error === null || error === undefined) return { texts: [] };
  if (typeof error === "string") return shapeOfString(error);
  if (typeof error !== "object") return { texts: [] };

  const source = error as Record<string, unknown>;
  const shape = shapeOfObject(source);
  const name = textOf(source.name) ?? "";
  const message = textOf(source.message) ?? "";

  // 取消 vs 超时：`AbortSignal.timeout()` 抛 TimeoutError，主动 abort 抛
  // AbortError。Chrome 里是 DOMException、node 里是 Error，都按 name 判定。
  if (name === "TimeoutError" || classifyNetworkText(message) === TIMEOUT_ZH) {
    return { ...shape, code: shape.code ?? "timeout", texts: [message || TIMEOUT_ZH, ...shape.texts] };
  }
  if (name === "AbortError") {
    return { ...shape, code: shape.code ?? "aborted", texts: [message || ABORTED_ZH, ...shape.texts] };
  }
  // 网络失败：api.ts 会包成 status 0 + code；裸 TypeError("Failed to fetch")
  // 没有状态码，补成 0 让后面的分派只看一处。
  if (shape.status === undefined && !shape.code) {
    const networkish = shape.texts.some((t) => classifyNetworkText(t));
    if (networkish) return { ...shape, status: 0, code: "network_error" };
  }
  return shape;
}

/** 原始服务端信息（截断，避免把整段堆栈带给任何展示层）。 */
function rawDetail(shape: Shape): string | undefined {
  const raw = shape.texts.find((t) => t.trim().length > 0);
  if (raw) return raw.slice(0, 500);
  return shape.errors ? safeStringify(shape.errors) : undefined;
}

function safeStringify(value: unknown): string | undefined {
  try {
    return JSON.stringify(value)?.slice(0, 500);
  } catch {
    return undefined;
  }
}

/**
 * 任何失败 → 中文。优先级：服务端自己的文案 > 校验数组的中文化 > 网络/超时/取消
 * > 稳定 code > HTTP 状态 > 通用兜底。认不出的绝不硬猜，也不会漏英文给用户。
 */
export function toUserError(error: unknown): UserError {
  const shape = shapeOf(error);
  const { status, code, texts, errors } = shape;

  // 1) 服务端自己的中文（或已收录的英文）：信息量最大，优先于任何通用文案。
  for (const text of texts) {
    const usable = usableServerMessage(text);
    if (usable) return { message: usable, status, code, detail: rawDetail(shape) };
  }
  // 2) pydantic 422 数组：整体中文化。
  if (errors) {
    const formatted = formatValidationErrors(errors);
    if (formatted) {
      return { message: formatted, status, code, detail: rawDetail(shape) };
    }
  }
  // 3) 网络 / 超时 / 取消。
  for (const text of texts) {
    const network = classifyNetworkText(text);
    if (network) {
      return {
        message: network,
        status: status ?? 0,
        code: code ?? (network === TIMEOUT_ZH ? "timeout" : "network_error"),
      };
    }
  }
  // 3.5) 上游模型服务的英文异常串（按现象归类，绝不原样展示）。只在没有任何
  // HTTP 上下文时启用 —— 有状态码/code 时走下面两条更准，也不会把 Redis 连接
  // 失败误判成"连不上模型服务"。
  if (status === undefined && !code) {
    for (const text of texts) {
      const upstream = classifyUpstreamText(text);
      if (upstream) return { message: upstream, detail: rawDetail(shape) };
    }
  }
  // 4) 稳定 code。
  if (code && CODE_ZH[code]) {
    return { message: CODE_ZH[code], status, code, detail: rawDetail(shape) };
  }
  // 5) HTTP 状态（5xx 一律合并成「服务暂时不可用」，不区分子类）。
  if (status !== undefined) {
    const byStatus = STATUS_ZH[status] ?? (status >= 500 ? STATUS_ZH[500] : undefined);
    if (byStatus) return { message: byStatus, status, code, detail: rawDetail(shape) };
  }
  return { message: UNKNOWN_ZH, status, code };
}

/** 只要文案时用：`toast.error(userErrorMessage(err))`。 */
export function userErrorMessage(error: unknown): string {
  return toUserError(error).message;
}
