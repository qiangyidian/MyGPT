"use client";

import { AttachmentCard, type AttachmentCardData } from "@/components/attachments/attachment-card";

export function AttachmentList({
  attachments,
  onRemove,
  onPreview,
  className,
}: {
  attachments: AttachmentCardData[];
  onRemove?: (id: string) => void;
  onPreview?: (id: string) => void;
  className?: string;
}) {
  if (!attachments.length) return null;
  return (
    <div className={className}>
      {attachments.map((a) => (
        <AttachmentCard key={a.id} attachment={a} onRemove={onRemove} onPreview={onPreview} />
      ))}
    </div>
  );
}
