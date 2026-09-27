/** 语音输入（ASR）/ 语音播报（TTS）的**纯**逻辑层。
 *
 * 这个文件刻意不碰任何 DOM：没有 ``window`` / ``navigator`` / ``MediaRecorder`` /
 * ``AudioContext`` / ``Blob``，连 ``Blob.size`` 都不读（调用方把各分块的字节数
 * 传进来）。原因很实际：前端的 vitest 跑在 ``environment: "node"`` 下，没有
 * jsdom，所以凡是能在浏览器外面算的规则都放在这里算完、测完，DOM 那一半留在
 * ``components/chat/voice-*.tsx``。需要浏览器能力的地方一律做成**注入的回调**
 * （见 :func:`pickRecorderMime` 的 ``isSupported``），而不是直接引用全局对象。
 *
 * 上限**不在这里抄一份数字**：字节/时长/字数全部来自
 * ``GET /api/speech/capabilities``（后端 ``speech_service.capabilities``），
 * 这里只负责「拿到的值和上限怎么比」。边界与后端严格对齐 ——
 * ``speech_service`` 用的是 ``len(...) > limit``，所以**等于**上限是合法的。
 */
import type { SpeechCapabilities } from "./types";

// --------------------------------------------------------------------------- //
// 录音容器 / MIME 选择
// --------------------------------------------------------------------------- //

/** 后端 ``_ALLOWED_AUDIO_MIME`` 的客户端镜像，**只用于兜底**（能力探测失败时）。
 *  运行时应优先用 ``capabilities.audio_mime_types``；这份常量的存在意义是让
 *  「服务端白名单」这个概念在读代码时有个对照物。 */
export const SERVER_AUDIO_MIME_FALLBACK: readonly string[] = [
  "audio/webm",
  "audio/ogg",
  "audio/opus",
  "audio/mpeg",
  "audio/mp3",
  "audio/mp4",
  "audio/aac",
  "audio/flac",
  "audio/wav",
  "audio/x-wav",
];

/** 浏览器 ``MediaRecorder`` 实际能产出的容器，按**推荐度**排序。
 *
 * 顺序即优先级，不能按字母序（服务端返回的白名单就是字母序的，遍历它会把
 * Chrome 推到 ``audio/aac`` 这类次优解上）：
 *  - ``audio/webm;codecs=opus``：桌面 Chrome / Edge / Firefox，48kbps 下质量最好；
 *  - ``audio/webm``：不带 codecs 的同一容器（Safari 的 ``isTypeSupported`` 对它
 *    返回 false，所以必须排在带 codecs 的那条后面）；
 *  - ``audio/ogg;codecs=opus``：老版 Firefox；
 *  - ``audio/mp4``：Safari 14.1+ / iOS（**Android Chrome 也会报这个**）；
 *  - ``audio/aac`` / ``audio/mpeg``：部分 WebView 只给得出这两类。
 *
 * 注意 Android Chrome 偶尔会坚持 ``audio/3gpp``，那不在服务端白名单里 ——
 * 命中不了时 :func:`pickRecorderMime` 返回 null，由组件把按钮降级成禁用态，
 * 而不是录一段后端必然 415 的音频。 */
export const RECORDER_MIME_CANDIDATES: readonly string[] = [
  "audio/webm;codecs=opus",
  "audio/ogg;codecs=opus",
  "audio/ogg",
  "audio/mp4",
  "audio/webm",
  "audio/aac",
  "audio/mpeg",
];

/** ``"audio/webm;codecs=opus"`` → ``"audio/webm"``：去掉参数部分并小写归一。
 *  后端 ``_sniff_media_type`` 也是先 ``split(";")[0]`` 再比白名单的。 */
export function baseMimeType(mimeType: string): string {
  return (mimeType || "").split(";")[0].trim().toLowerCase();
}

/** 挑一个「浏览器支持 **且** 服务端接受」的录音 MIME；挑不出来返回 null。
 *
 * ``isSupported`` 由调用方注入（真身是 ``MediaRecorder.isTypeSupported``），
 * 这样这个函数在 node 里就能测遍各家浏览器的组合。
 * 两个条件缺一不可：只看浏览器会把不支持的容器发给后端吃 415，只看服务端会
 * ``new MediaRecorder(stream, { mimeType })`` 直接抛 NotSupportedError。 */
export function pickRecorderMime(
  isSupported: (mime: string) => boolean,
  allowedMimes: readonly string[] = SERVER_AUDIO_MIME_FALLBACK,
  candidates: readonly string[] = RECORDER_MIME_CANDIDATES,
): string | null {
  const allowed = new Set(allowedMimes.map(baseMimeType));
  for (const candidate of candidates) {
    if (!allowed.has(baseMimeType(candidate))) continue;
    if (isSupported(candidate)) return candidate;
  }
  return null;
}

/** MIME → 上传文件名的扩展名。后端在 ``Blob.type`` 为空时靠**扩展名**回落判格式
 *  （``_AUDIO_EXT_MIME``），所以文件名不是装饰，是校验链的一环。 */
export function audioFileExtension(mimeType: string): string {
  const base = baseMimeType(mimeType);
  if (base === "audio/webm") return "webm";
  if (base === "audio/ogg" || base === "audio/opus") return "ogg";
  if (base === "audio/mp4") return "m4a";
  if (base === "audio/mpeg" || base === "audio/mp3") return "mp3";
  if (base === "audio/aac") return "aac";
  if (base === "audio/flac") return "flac";
  if (base === "audio/wav" || base === "audio/x-wav") return "wav";
  // 认不出来的容器留 webm：它是这套链路里最普遍的默认，且后端白名单里有。
  return "webm";
}

/** 录音上传用的文件名（带正确的扩展名）。 */
export function recorderFilename(mimeType: string | null): string {
  return `voice.${audioFileExtension(mimeType || "")}`;
}

// --------------------------------------------------------------------------- //
// 预算：字节 / 时长 / 字数
// --------------------------------------------------------------------------- //

export interface BudgetCheck {
  ok: boolean;
  /** 超限时的中文说明；合法时为 null。 */
  message: string | null;
}

/** 累加各 ``MediaRecorder`` 分块的字节数。收 ``readonly number[]`` 而不是
 *  ``Blob[]``：node 测试环境里不必依赖全局 Blob。 */
export function totalBytes(chunkSizes: readonly number[]): number {
  let sum = 0;
  for (const size of chunkSizes) sum += Number.isFinite(size) && size > 0 ? size : 0;
  return sum;
}

/** 字节数 → 「X.Y MB」的人话（用于错误文案），整 MB 时省掉小数。 */
export function formatMb(bytes: number): string {
  const mb = Math.max(0, bytes) / (1024 * 1024);
  const rounded = Math.round(mb * 10) / 10;
  return Number.isInteger(rounded) ? `${rounded}MB` : `${rounded.toFixed(1)}MB`;
}

/** 音频体积是否在后端上限内（边界：``== max`` 合法，与 413 的判定式一致）。 */
export function checkByteBudget(bytes: number, maxBytes: number): BudgetCheck {
  const safeMax = Math.max(0, maxBytes);
  if (!(bytes > safeMax)) return { ok: true, message: null };
  return {
    ok: false,
    message: `录音体积已达上限（约 ${formatMb(safeMax)}），请缩短录音后重试`,
  };
}

/** 录音时长是否还在上限内（到点后组件自动停止并给出中文说明）。 */
export function checkDurationBudget(
  elapsedSeconds: number,
  maxSeconds: number,
): BudgetCheck {
  if (!(elapsedSeconds > maxSeconds)) return { ok: true, message: null };
  return {
    ok: false,
    message: `录音已达 ${Math.floor(maxSeconds)} 秒上限，已自动停止`,
  };
}

/** 剩余可录秒数（不小于 0；上限为 0/负数时视为 0，即立刻该停）。 */
export function remainingRecordingSeconds(
  elapsedSeconds: number,
  maxSeconds: number,
): number {
  const left = maxSeconds - Math.max(0, elapsedSeconds);
  return left > 0 ? left : 0;
}

/** 是否已经到达时长硬上限（用 ``>=``：到点即停，不等超）。 */
export function isAtDurationCap(
  elapsedSeconds: number,
  maxSeconds: number,
): boolean {
  return maxSeconds > 0 && elapsedSeconds >= maxSeconds;
}

/** 一个字节都没录到（用户秒点秒停 / 设备静音且编码器没产出）。 */
export function isEmptyRecording(bytes: number): boolean {
  return bytes <= 0;
}

/** 秒 → ``m:ss``（``h:mm:ss`` 只在破一小时时出现）。
 *
 *  录音计时既要显示整数上限（``max_duration_seconds``），也要显示浮点的已录时长，
 *  所以这里向下取整：进度条上「多显示一秒」会让用户以为还能录，少显示不会。
 *  非有限值与负数一律当 0，免得 NaN 渗进界面。 */
export function formatRecordingClock(seconds: number): string {
  const total = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${ss}`;
  return `${m}:${ss}`;
}

/** Markdown → 可朗读的纯文本。
 *
 *  助手消息是 Markdown，直接喂给 TTS 会念出「井号 标题 星号星号」和整段代码。
 *  这里做的是**朗读取向**的清洗，不是渲染：代码块整段丢弃（念 ``for i in
 *  range(10)`` 没有任何信息量），链接只留锚文本（URL 念出来是噪音），引用标记
 *  ``[1]`` / ``[source 3]`` 去掉。**必须先过这一道再算字数预算** —— 后端计的
 *  是真正送去合成的那个字符串的长度。
 *
 *  一处已知的粗疏：全局剥离 ``*`` / ``_`` 会连带去掉行内普通文本里的下划线
 *  （如 ``a_b_c``）。含这类串的内容通常在代码块里、已经被丢弃，所以代价可以
 *  接受；要精确切 Markdown 得引一个 AST 解析器，不值得为朗读路径引入。 */
export function plainTextForSpeech(markdown: string): string {
  let text = markdown || "";
  // 围栏代码块（``` 与 ~~~）整段丢弃。
  text = text.replace(/```[\s\S]*?```/g, " ");
  text = text.replace(/~~~[\s\S]*?~~~/g, " ");
  // 图片取 alt、链接取锚文本（顺序不能反：先图片，否则 ! 会被链接规则漏掉）。
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ");
  text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ");
  // 行内引用标记 [1] / [source 3]。
  text = text.replace(/\[(?:source\s+)?\d+\]/gi, " ");
  // 行内代码：留内容、去反引号。
  text = text.replace(/`([^`]*)`/g, "$1");
  // 块级前缀：标题、引用、无序/有序列表、水平线。
  text = text.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  text = text.replace(/^\s{0,3}>\s?/gm, "");
  text = text.replace(/^\s*[-*_]{3,}\s*$/gm, " ");
  text = text.replace(/^\s*[-*+]\s+/gm, "");
  text = text.replace(/^\s*\d+\.\s+/gm, "");
  // 强调/删除线标记与表格竖线。
  text = text.replace(/(\*\*|__|~~|[*_])/g, "");
  text = text.replace(/\|/g, " ");
  // HTML 标签（Markdown 允许内联 HTML）。
  text = text.replace(/<[^>]+>/g, " ");
  return text
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/ {2,}/g, " ")
    .trim();
}

/** 按**码点**计长度。后端是 Python ``len(cleaned)``，一个汉字算 1 个，
 *  一个 emoji（代理对）也算 1 个；JS 的 ``string.length`` 会把 emoji 算成 2，
 *  用它做预算会比服务端提前一个字截断，所以这里数的是码点。 */
export function charLength(text: string): number {
  return Array.from(text || "").length;
}

/** 播报文本的字数预算（与后端 ``normalize_text`` 同口径：先去空白再比长度）。 */
export function checkTextBudget(
  text: string,
  maxChars: number,
  label = "播报文本",
): BudgetCheck {
  const cleaned = (text || "").trim();
  const limit = Math.max(0, maxChars);
  const chars = charLength(cleaned);
  if (!cleaned) return { ok: false, message: `${label}不能为空` };
  if (chars > limit) {
    return { ok: false, message: `${label}最长 ${limit} 字，当前 ${chars} 字` };
  }
  return { ok: true, message: null };
}

/** 裁到预算内（按码点切，绝不留下半个代理对），并去掉首尾空白。
 *  ``maxChars <= 0`` 时返回空串 —— 让调用方自己决定是拒绝还是提示。 */
export function clipToCharBudget(text: string, maxChars: number): string {
  const cleaned = (text || "").trim();
  const limit = Math.max(0, maxChars);
  if (limit === 0) return "";
  const chars = Array.from(cleaned);
  if (chars.length <= limit) return cleaned;
  // 省略号算在预算内：留 1 个码点给它，播报时语义上明确表示「后文未播」。
  const body = limit > 1 ? chars.slice(0, limit - 1) : chars.slice(0, limit);
  return (limit > 1 ? `${body.join("")}…` : body.join("")).trim();
}

/** 句读集合：在中/英句号、问号、感叹号、分号、换行与省略号处优先断开。 */
const SENTENCE_ENDINGS = "。！？；!?;\n…";

/** 按句读切句（terminator 留在句尾），返回码点数组的数组。 */
function splitSentences(chars: readonly string[]): string[][] {
  const sentences: string[][] = [];
  let current: string[] = [];
  for (const ch of chars) {
    current.push(ch);
    if (SENTENCE_ENDINGS.includes(ch)) {
      sentences.push(current);
      current = [];
    }
  }
  if (current.length) sentences.push(current);
  return sentences;
}

/** 长回复 → 若干条不超过 ``maxChars`` 的**播报段**（按句读打包，整句超长才硬切）。
 *
 *  后端一次合成受 ``SPEECH_MAX_TEXT_CHARS`` 限制，而一条助手回复可以远长于此。
 *  这里切块而不是截断：少播一段是功能缺失，多请求一次只是钱 —— 而且切块后
 *  第一段拿到就能开播，用户不必等整条答案合成完。
 *
 *  不变式（都在 node 里钉了）：每段 ``charLength(seg) <= maxChars``，与后端
 *  ``len(...) > limit`` 同口径；除首尾空白外不丢字；空文本/``maxChars<=0`` 返回
 *  ``[]``，让调用方走「不可用」分支而不是发一个必然被拒的请求。
 *  必须先过 :func:`plainTextForSpeech` 再切：后端计的是真正送进合成的那个串。 */
export function splitTextForSpeech(text: string, maxChars: number): string[] {
  const cleaned = (text || "").trim();
  const limit = Math.floor(maxChars);
  if (!cleaned || limit <= 0) return [];
  const chars = Array.from(cleaned);
  if (chars.length <= limit) return [cleaned];

  const out: string[] = [];
  const push = (piece: readonly string[]) => {
    const joined = piece.join("").trim();
    if (joined) out.push(joined);
  };
  let buffer: string[] = [];
  for (const sentence of splitSentences(chars)) {
    if (sentence.length > limit) {
      // 单句就超上限（无句读的长段落）：只能按 limit 硬切，不补省略号（它也算预算）。
      push(buffer);
      buffer = [];
      for (let i = 0; i < sentence.length; i += limit) {
        push(sentence.slice(i, i + limit));
      }
      continue;
    }
    if (buffer.length + sentence.length > limit) {
      push(buffer);
      buffer = sentence.slice();
    } else {
      buffer = buffer.concat(sentence);
    }
  }
  push(buffer);
  return out;
}

// --------------------------------------------------------------------------- //
// 音量计
// --------------------------------------------------------------------------- //

function rms(values: ArrayLike<number>, center: number, scale: number): number {
  const n = values.length;
  if (n === 0) return 0;
  let sum = 0;
  for (let i = 0; i < n; i += 1) {
    const v = (values[i] - center) / scale;
    sum += v * v;
  }
  const level = Math.sqrt(sum / n);
  return level > 1 ? 1 : level;
}

/** ``AnalyserNode.getByteTimeDomainData`` 的 ``Uint8Array`` → 0..1 电平。
 *  无符号字节域的中心是 128、满幅 128。 */
export function levelFromByteDomain(samples: ArrayLike<number>): number {
  return rms(samples, 128, 128);
}

/** ``getFloatTimeDomainData`` 的 ``Float32Array``（中心 0、满幅 1）→ 0..1 电平。 */
export function levelFromSampleDomain(samples: ArrayLike<number>): number {
  return rms(samples, 0, 1);
}

/** 电平指数平滑（EMA）。直接画原始 RMS 会跳得看不清，10Hz 采样也够用。
 *  ``factor`` 越大越跟手；越界输入钳到 [0,1]。 */
export function smoothLevel(
  previous: number,
  next: number,
  factor = 0.3,
): number {
  const a = Math.min(1, Math.max(0, factor));
  const target = Math.min(1, Math.max(0, next));
  const prev = Math.min(1, Math.max(0, previous));
  return prev + (target - prev) * a;
}

// --------------------------------------------------------------------------- //
// 播报状态机
// --------------------------------------------------------------------------- //

export type PlaybackState = "idle" | "loading" | "playing" | "paused";

export type PlaybackEvent =
  | { type: "load" }
  | { type: "ready" }
  | { type: "pause" }
  | { type: "resume" }
  | { type: "stop" }
  | { type: "ended" }
  | { type: "error" };

/** 合法迁移表。表里没有的一律视为非法（组件应忽略而不是硬切状态，
 *  否则一个迟到的 ``onended`` 能把「正在加载 B」的界面打回 idle）。
 *
 *  ``load`` 从任何状态都能进 loading —— 这是「切换到另一条消息」的现实场景；
 *  ``stop`` / ``error`` 是通用逃生口；``ended`` 只从出声的状态来。 */
export const PLAYBACK_TRANSITIONS: Record<
  PlaybackState,
  Partial<Record<PlaybackEvent["type"], PlaybackState>>
> = {
  idle: { load: "loading", stop: "idle", error: "idle" },
  loading: { load: "loading", ready: "playing", stop: "idle", error: "idle" },
  playing: {
    load: "loading",
    ready: "playing",
    pause: "paused",
    stop: "idle",
    ended: "idle",
    error: "idle",
  },
  paused: {
    load: "loading",
    resume: "playing",
    stop: "idle",
    ended: "idle",
    error: "idle",
  },
};

/** 这个状态能否接受该事件（组件用它决定按钮是否可点）。 */
export function canTransition(
  state: PlaybackState,
  event: PlaybackEvent["type"],
): boolean {
  return PLAYBACK_TRANSITIONS[state][event] !== undefined;
}

/** 迁移函数：非法事件原样返回当前状态（全函数，不抛异常）。 */
export function nextPlaybackState(
  state: PlaybackState,
  event: PlaybackEvent["type"],
): PlaybackState {
  return PLAYBACK_TRANSITIONS[state][event] ?? state;
}

/** 是否正占着音频资源（加载中或出声中）——用于「同一时刻只播一条」。 */
export function isPlaybackActive(state: PlaybackState): boolean {
  return state !== "idle";
}

// --------------------------------------------------------------------------- //
// 错误码 → 中文文案
// --------------------------------------------------------------------------- //

/** 后端 ``speech_service`` 抛出的 ``AppException`` code 全集 → 中文兜底文案。
 *
 *  正常路径下后端已经返回中文 ``message``（:func:`speechErrorMessage` 会优先
 *  用它）；这张表兜的是「message 缺失或是英文/网关 HTML」的情况 —— 反向代理
 *  把自己的 502 页面盖在 FastAPI 的信封上时，``code`` 仍是 ``error`` 而 message
 *  是一段 HTML，那时只有这里能给出一句人话。 */
export const SPEECH_ERROR_ZH: Record<string, string> = {
  speech_disabled: "语音功能未开启，请联系管理员在服务端启用后再试",
  speech_model_unconfigured:
    "尚未配置可用的语音模型，请在「设置 → 模型」新增 OpenAI 兼容端点并勾选音频能力",
  speech_bad_config: "服务端语音配置有误，请联系管理员检查 SPEECH_TTS_RESPONSE_FORMAT",
  speech_payload_too_large: "音频或文本超过了允许的上限，请缩短后重试",
  speech_bad_audio: "音频格式不支持或已损坏，请重新录制（建议用 Chrome / Edge）",
  speech_text_required: "没有可播报的内容",
  speech_no_speech: "没有听清内容，请靠近麦克风再说一次",
  speech_provider_error: "语音模型调用失败，请稍后重试或改用文本输入",
  speech_provider_empty: "语音合成返回空音频，请稍后重试",
  speech_provider_oversized: "合成音频过大，请缩短播报内容",
  insufficient_credits: "积分不足，请先兑换后再使用语音功能",
  // 以下是**浏览器侧**才会出现的失败（后端不会返回这些 code），走同一张表，
  // 免得组件里散落字符串 —— 录音与播报两侧的失败共用一条文案链路。
  mic_unsupported: "当前浏览器不支持录音，请更换浏览器或改用文本输入",
  mic_denied: "麦克风权限被拒绝，请在浏览器地址栏的权限设置里允许后重试",
  mic_unavailable: "找不到可用的麦克风，请检查输入设备后重试",
  mic_insecure_origin:
    "当前页面不是安全上下文，浏览器禁止访问麦克风：请改用 HTTPS 或 localhost",
  recorder_failed: "录音启动失败，请重试",
  playback_aborted: "播报已中止",
  playback_network: "语音数据下载失败，请检查网络后重试",
  playback_decode: "音频解码失败，请重试或改用文本阅读",
  playback_unsupported: "当前浏览器无法播放该音频格式，请改用文本阅读",
};

/** ``getUserMedia`` / ``MediaRecorder`` 抛出的 ``DOMException.name`` → 本表的 code。
 *
 *  各家浏览器的 message 措辞不一且是英文，所以按 **name** 分类（规范里的
 *  ``DOMException.name`` 是稳定的），再由 :func:`speechErrorMessage` 出中文。
 *  认不出的 name 返回 null，调用方保留原始 message 走通用兜底，不硬套一个错的原因。 */
export const MIC_ERROR_CODE_BY_NAME: Record<string, string> = {
  NotAllowedError: "mic_denied",
  PermissionDeniedError: "mic_denied",
  // 非安全上下文（http 访问）下 Chrome 抛的也是 SecurityError，表现就是「不给权限」。
  SecurityError: "mic_insecure_origin",
  AbortError: "recorder_failed",
  NotFoundError: "mic_unavailable",
  DevicesNotFoundError: "mic_unavailable",
  NotReadableError: "mic_unavailable",
  OverconstrainedError: "mic_unavailable",
  // Safari 早期的自定义名字。
  TrackStartError: "mic_unavailable",
};

export function micErrorCodeFromName(name: string | null | undefined): string | null {
  if (!name) return null;
  return MIC_ERROR_CODE_BY_NAME[name] ?? null;
}

/** ``HTMLMediaElement.error.code``（``MEDIA_ERR_*``）→ 本表的 code。 */
export const AUDIO_ERROR_CODE_BY_CODE: Record<number, string> = {
  1: "playback_aborted",
  2: "playback_network",
  3: "playback_decode",
  4: "playback_unsupported",
};

export function audioErrorCodeFromMediaError(code: number | null | undefined): string | null {
  if (typeof code !== "number" || !Number.isFinite(code)) return null;
  return AUDIO_ERROR_CODE_BY_CODE[code] ?? null;
}

/** 文案里是否已经有一句中文（后端正常信封的标志）。 */
function looksLikeChineseCopy(text: string): boolean {
  return /[一-鿿]/.test(text || "");
}

/** 把一次失败翻译成中文：优先服务端的中文 message，其次 code 表，最后通用文案。 */
export function speechErrorMessage(
  error: { code?: string | null; message?: string | null } | null | undefined,
  fallback = "语音请求失败，请稍后重试",
): string {
  const message = (error?.message || "").trim();
  if (message && looksLikeChineseCopy(message)) return message;
  const code = (error?.code || "").trim();
  if (code && SPEECH_ERROR_ZH[code]) return SPEECH_ERROR_ZH[code];
  // 英文 message 也不能直接丢给用户，但比「未知错误」有用：原样带上。
  if (message) return `${fallback}（${message.slice(0, 80)}）`;
  return fallback;
}

// --------------------------------------------------------------------------- //
// 能力探测的读法
// --------------------------------------------------------------------------- //

const MODEL_UNCONFIGURED_ASR_ZH =
  "尚未配置语音转写模型：请在「设置 → 模型」新增 OpenAI 兼容端点并勾选「支持音频输入」";
const MODEL_UNCONFIGURED_TTS_ZH =
  "尚未配置语音播报模型：请在「设置 → 模型」新增 OpenAI 兼容端点并勾选「支持音频输出」";
const CAPABILITY_UNKNOWN_ZH = "暂不可用：未能读取到语音能力配置，请刷新后重试";
const SPEECH_OFF_ZH = "语音功能未开启（服务端 SPEECH_ENABLED=false）";

/** ASR 是否真的可用（开关 + 后端确实挑到了模型）。 */
export function isAsrAvailable(caps: SpeechCapabilities | null | undefined): boolean {
  return caps?.enabled === true && caps.asr_enabled === true;
}

/** TTS 是否真的可用。 */
export function isTtsAvailable(caps: SpeechCapabilities | null | undefined): boolean {
  return caps?.enabled === true && caps.tts_enabled === true;
}

/** 不可用时给用户的中文提示（可用时返回 null）。
 *
 *  后端把「开关关」和「没配模型」分开报（``reason``），这里也分开说：前者是
 *  运维的事，后者用户自己在设置页就能解决，混成一句「不可用」等于没提示。 */
export function speechDisabledHint(
  caps: SpeechCapabilities | null | undefined,
  target: "asr" | "tts",
): string | null {
  const available = target === "asr" ? isAsrAvailable(caps) : isTtsAvailable(caps);
  if (available) return null;
  if (!caps) return CAPABILITY_UNKNOWN_ZH;
  if (!caps.enabled) return (caps.disabled_message || "").trim() || SPEECH_OFF_ZH;
  if (caps.reason === "model_unconfigured") {
    return target === "asr" ? MODEL_UNCONFIGURED_ASR_ZH : MODEL_UNCONFIGURED_TTS_ZH;
  }
  return target === "asr" ? MODEL_UNCONFIGURED_ASR_ZH : MODEL_UNCONFIGURED_TTS_ZH;
}

/** 录音时的展示用上限（秒）。能力没拿到时返回 0，组件据此判断「先别开录音」。 */
export function maxRecordingSeconds(
  caps: SpeechCapabilities | null | undefined,
): number {
  const value = caps?.max_duration_seconds ?? 0;
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}
