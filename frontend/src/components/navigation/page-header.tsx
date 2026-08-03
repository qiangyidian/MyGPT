"use client";

import { type ReactNode } from "react";

import { BackLink } from "./back-link";
import { Breadcrumbs, type BreadcrumbItem } from "./breadcrumbs";

export type { BreadcrumbItem };

export interface PageHeaderProps {
  title: string;
  description?: string;
  /** Breadcrumb trail rendered above the title. */
  breadcrumbs?: BreadcrumbItem[];
  /** Page-level action buttons (e.g. "新建知识库"), top-right on desktop. */
  actions?: ReactNode;
  /**
   * Primary deterministic back link (top-left), e.g. "返回对话". `href` should
   * already be validated by the caller (usually via `resolveReturnTo`).
   */
  returnHref?: string;
  returnLabel?: string;
  /** Optional secondary deterministic back link, e.g. "返回知识库". */
  secondaryBack?: { href: string; label: string };
}

/**
 * Presentational page header: a back affordance + optional secondary back,
 * breadcrumbs, title, description, and a responsive actions slot. Pure — no
 * hooks; the caller resolves all destinations.
 */
export function PageHeader({
  title,
  description,
  breadcrumbs,
  actions,
  returnHref,
  returnLabel = "返回对话",
  secondaryBack,
}: PageHeaderProps) {
  return (
    <header className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          {returnHref && <BackLink href={returnHref} label={returnLabel} />}
          {secondaryBack && <BackLink href={secondaryBack.href} label={secondaryBack.label} variant="outline" />}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>

      {breadcrumbs && breadcrumbs.length > 0 && <Breadcrumbs items={breadcrumbs} />}

      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
    </header>
  );
}
