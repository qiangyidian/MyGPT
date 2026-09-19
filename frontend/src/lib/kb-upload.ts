/**
 * Knowledge-base upload UX: the file picker's ``accept`` and the hint line.
 *
 * The server is the authority — ``GET /api/upload-capabilities`` returns the
 * effective allow-list (configured list ∩ formats the parser registry can
 * actually read). A hard-coded copy of that list is what let the two sides
 * drift: the picker kept greyed out formats the backend had gained, and offered
 * formats it had dropped. ``FALLBACK_UPLOAD_EXTENSIONS`` is only the pre-response
 * placeholder, so it deliberately stays a conservative subset.
 */

export interface UploadCapabilitiesLike {
  allowed_extensions: string[];
  max_upload_mb: number;
}

export const FALLBACK_UPLOAD_EXTENSIONS = [
  ".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".xlsx", ".xls",
];

/** Friendly group names, so the hint reads 「PDF / Word / Excel」 not 20 extensions. */
const FRIENDLY_NAMES: Record<string, string> = {
  ".pdf": "PDF",
  ".docx": "Word",
  ".doc": "Word",
  ".odt": "OpenDocument",
  ".pptx": "PPT",
  ".ppt": "PPT",
  ".odp": "OpenDocument",
  ".xlsx": "Excel",
  ".xls": "Excel",
  ".ods": "OpenDocument",
  ".csv": "CSV",
  ".json": "JSON",
  ".log": "日志",
  ".txt": "TXT",
  ".md": "Markdown",
  ".markdown": "Markdown",
  ".html": "HTML",
  ".htm": "HTML",
  ".epub": "EPUB",
  ".rtf": "RTF",
};

export function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

/** Native file-picker accept attribute; empty string means "don't filter". */
export function kbAcceptAttribute(caps: UploadCapabilitiesLike | undefined): string {
  return effectiveExtensions(caps).join(",");
}

export function effectiveExtensions(
  caps: UploadCapabilitiesLike | undefined,
): string[] {
  const list = caps?.allowed_extensions;
  return list && list.length > 0 ? list : FALLBACK_UPLOAD_EXTENSIONS;
}

export function isUploadableToKb(
  file: { name: string },
  caps: UploadCapabilitiesLike | undefined,
): boolean {
  return effectiveExtensions(caps).includes(extOf(file.name));
}

/** 「支持 PDF / Word / …；单个文件不超过 20MB」 */
export function describeKbUpload(caps: UploadCapabilitiesLike | undefined): string {
  const groups: string[] = [];
  for (const ext of effectiveExtensions(caps)) {
    const label = FRIENDLY_NAMES[ext] ?? ext.replace(".", "").toUpperCase();
    if (!groups.includes(label)) groups.push(label);
  }
  const size = caps?.max_upload_mb;
  return `支持 ${groups.join(" / ") || "（服务端未声明类型）"}${
    size ? `；单个文件不超过 ${size}MB` : ""
  }`;
}

/** Pre-flight rejection message, so a drag-drop failure doesn't wait for the round trip. */
export function kbUploadRejectionMessage(
  file: { name: string },
  caps: UploadCapabilitiesLike | undefined,
): string | null {
  if (isUploadableToKb(file, caps)) return null;
  const ext = extOf(file.name);
  return `不支持的文件类型${ext ? ` ${ext}` : ""}。${describeKbUpload(caps)}`;
}
