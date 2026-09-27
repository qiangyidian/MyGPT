/**
 * 浏览器端下载：把 Blob 落地成文件，并从服务端的 Content-Disposition 里取文件名。
 *
 * 文件名一律由服务端决定（会话标题里有中文，服务端已经按 RFC 5987 做过
 * `filename` + `filename*` 双写），前端只负责解析，不再自己拼一个名字。
 */

/** 解析 Content-Disposition，取不到文件名时返回 null（由调用方兜底）。 */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  // filename* 优先：中文/非 ASCII 标题只有它能无损表达。
  const extended = /filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)/i.exec(header);
  if (extended?.[1]) {
    const decoded = safeDecodeURIComponent(extended[1].trim().replace(/^"|"$/g, ""));
    if (decoded) return decoded;
  }
  const basic = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
  return basic?.[1] ? basic[1].trim() : null;
}

/** 百分号编码坏掉时不能抛异常打断下载，退化成原样字符串。 */
function safeDecodeURIComponent(raw: string): string | null {
  try {
    return decodeURIComponent(raw) || null;
  } catch {
    return raw || null;
  }
}

/** 触发一次浏览器下载并立即释放临时 URL。 */
export function saveBlobToDisk(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
