/**
 * Client-side attachment type allow-list, mirroring the backend's
 * ``ATTACHMENT_ALLOWED_EXT`` (app/core/config.py). Keep both sides in sync.
 *
 * Purpose: reject unsupported files IMMEDIATELY with a clear message instead
 * of letting the upload start and fail server-side after the bytes were
 * already transferred. The server remains the authority — this is UX only.
 */

export const ATTACHMENT_EXTENSIONS = [
  // Documents
  ".pdf", ".docx", ".doc", ".txt", ".md", ".markdown", ".rtf", ".html", ".htm", ".epub",
  // Spreadsheets / data
  ".csv", ".xlsx", ".xls", ".json",
  // Slides
  ".pptx", ".ppt",
  // OpenDocument
  ".odt", ".ods", ".odp",
  // Images
  ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
  // Audio
  ".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac", ".aac",
] as const;

/** Native file-picker accept attribute (kept from the same source). */
export const ATTACHMENT_ACCEPT = ATTACHMENT_EXTENSIONS.join(",");

const ALLOWED = new Set<string>(ATTACHMENT_EXTENSIONS);

export function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

export function isAllowedAttachment(file: { name: string }): boolean {
  return ALLOWED.has(extOf(file.name));
}

/** Friendly rejection message naming the offending files + the allow-list. */
export function attachmentRejectionMessage(files: { name: string }[]): string | null {
  const bad = files.filter((f) => !isAllowedAttachment(f)).map((f) => f.name);
  if (bad.length === 0) return null;
  const shown = bad.slice(0, 3).join("、");
  const more = bad.length > 3 ? ` 等 ${bad.length} 个文件` : "";
  return `不支持的文件类型：${shown}${more}。支持文档（PDF/Word/Excel/PPT/Markdown）、图片、音频。`;
}
