"use client";

/** 提示词模板的编辑表单（设置页与「把当前草稿存成模板」共用一份）。
 *
 * 校验与字段上限都在 `@/lib/prompt-apply`（选择弹窗用的是同一份规则），状态从这里
 * 的 `usePromptForm` 出，两个入口的差别只剩「提交给谁」。同一个模板在设置页存得
 * 进去、在对话里存不进去这种事，就是这么长出来的。
 */
import { useMemo, useState } from "react";
import { CircleAlert } from "lucide-react";

import {
  DEFAULT_PROMPT_CATEGORY,
  PROMPT_LIMITS,
  validatePromptForm,
  type PromptForm,
  type PromptFormErrors,
} from "@/lib/prompt-apply";
import { extractVariables } from "@/lib/prompt-library";
import { cn } from "@/lib/utils";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

/**
 * 表单状态 + 校验。
 *
 * 初值只在挂载时生效 —— 切换编辑对象时用 ``key`` 重挂一次（React 的标准做法），比在
 * effect 里 setState 回滚干净，也不会出现「看到上一个模板的一帧」。
 */
export function usePromptForm(initial: PromptForm) {
  const [form, setForm] = useState<PromptForm>(initial);
  const errors = useMemo(() => validatePromptForm(form), [form]);
  return {
    form,
    setForm,
    /** 单字段更新：key 收在 ``PromptForm`` 的字段名里，写错编译期就报错。 */
    setField: (key: keyof PromptForm, value: string) =>
      setForm((prev) => ({ ...prev, [key]: value })),
    errors,
    hasErrors: Object.keys(errors).length > 0,
  };
}

export interface PromptTemplateFormProps {
  form: PromptForm;
  errors: PromptFormErrors;
  onFieldChange: (key: keyof PromptForm, value: string) => void;
  onSubmit: () => void;
  onCancel?: () => void;
  submitting?: boolean;
  submitLabel?: string;
  /** 分类输入框的候选：来自后端聚合，不是前端常量。 */
  categoryOptions?: string[];
  /** 表单顶部的一次性说明（例如「正文就是你输入框里的内容」）。 */
  hint?: string;
  /** 多实例共存时（设置页 + 弹窗）区分 id / htmlFor，免得浏览器 autofill 串台。 */
  idPrefix?: string;
}

export function PromptTemplateForm({
  form,
  errors,
  onFieldChange,
  onSubmit,
  onCancel,
  submitting = false,
  submitLabel = "保存",
  categoryOptions = [],
  hint,
  idPrefix = "prompt",
}: PromptTemplateFormProps) {
  const variables = extractVariables(form.content);
  const id = (name: string) => `${idPrefix}-${name}`;

  return (
    <form
      className="grid gap-4"
      onSubmit={(e) => {
        // 回车（尤其中文输入法确认时）不该顺手提交这张表单。
        e.preventDefault();
        onSubmit();
      }}
    >
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}

      <div className="grid gap-1.5">
        <Label htmlFor={id("title")}>
          标题
          <Counter current={form.title.trim().length} max={PROMPT_LIMITS.title.max} />
        </Label>
        <Input
          id={id("title")}
          value={form.title}
          maxLength={PROMPT_LIMITS.title.max}
          placeholder="例如：周报整理"
          onChange={(e) => onFieldChange("title", e.target.value)}
          aria-invalid={!!errors.title}
        />
        <FieldError message={errors.title} />
      </div>

      <div className="grid gap-1.5">
        <Label htmlFor={id("content")}>
          提示词正文
          <Counter current={form.content.length} max={PROMPT_LIMITS.content.max} />
        </Label>
        <Textarea
          id={id("content")}
          value={form.content}
          rows={8}
          className="min-h-[160px] font-mono text-xs leading-relaxed"
          placeholder={
            "发给模型的完整内容。占位符写 {{语气}} 或 ${字数} 都行，\n" +
            "例如：请用{{语气}}改写下面这段，控制在 ${字数} 字以内：\n"
          }
          onChange={(e) => onFieldChange("content", e.target.value)}
          aria-invalid={!!errors.content}
        />
        <FieldError message={errors.content} />
        {variables.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-muted-foreground">识别到占位符：</span>
            {variables.map((v) => (
              <Badge key={v.raw} variant="secondary" className="font-mono text-[11px]">
                {v.raw}
              </Badge>
            ))}
          </div>
        ) : (
          <p className="text-[11px] text-muted-foreground">
            占位符可选：{"{{变量}}"} 与 {"${变量}"} 都认。用「提示词库」插入时会先让你填好，
            填完的内容就是发出去的内容。
          </p>
        )}
      </div>

      <div className="grid gap-1.5 sm:grid-cols-2">
        <div className="grid gap-1.5">
          <Label htmlFor={id("category")}>
            分类
            <span className="ml-1 font-normal text-muted-foreground">（用于筛选）</span>
          </Label>
          <Input
            id={id("category")}
            list={categoryOptions.length > 0 ? id("category-list") : undefined}
            value={form.category}
            maxLength={PROMPT_LIMITS.category.max}
            placeholder={DEFAULT_PROMPT_CATEGORY}
            onChange={(e) => onFieldChange("category", e.target.value)}
            aria-invalid={!!errors.category}
          />
          {/* datalist 而不是 Select：分类可以自建，硬编码下拉会把用户关在既有取值里。 */}
          {categoryOptions.length > 0 ? (
            <datalist id={id("category-list")}>
              {categoryOptions.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          ) : null}
          <FieldError message={errors.category} />
        </div>

        <div className="grid gap-1.5">
          <Label htmlFor={id("tags")}>
            标签
            <span className="ml-1 font-normal text-muted-foreground">
              （逗号分隔，最多 {PROMPT_LIMITS.tags.maxCount} 个）
            </span>
          </Label>
          <Input
            id={id("tags")}
            value={form.tags}
            placeholder="写作, 汇报"
            onChange={(e) => onFieldChange("tags", e.target.value)}
            aria-invalid={!!errors.tags}
          />
          <FieldError message={errors.tags} />
        </div>
      </div>

      <div className="grid gap-1.5">
        <Label htmlFor={id("description")}>
          一句话说明（可选）
          <Counter
            current={form.description.trim().length}
            max={PROMPT_LIMITS.description.max}
          />
        </Label>
        <Textarea
          id={id("description")}
          value={form.description}
          rows={2}
          maxLength={PROMPT_LIMITS.description.max}
          placeholder="用在什么场合、要注意什么；列表里会显示这一行"
          onChange={(e) => onFieldChange("description", e.target.value)}
          aria-invalid={!!errors.description}
        />
        <FieldError message={errors.description} />
      </div>

      <div className="flex items-center justify-end gap-2">
        {onCancel ? (
          <Button type="button" variant="outline" onClick={onCancel}>
            取消
          </Button>
        ) : null}
        <Button type="submit" disabled={submitting}>
          {submitting ? "保存中…" : submitLabel}
        </Button>
      </div>
    </form>
  );
}

function Counter({ current, max }: { current: number; max: number }) {
  return (
    <span
      className={cn(
        "ml-2 text-[11px] font-normal text-muted-foreground",
        current > max && "text-destructive",
      )}
    >
      {current}/{max}
    </span>
  );
}

function FieldError({ message }: { message?: string }) {
  if (!message) return null;
  return (
    <p className="flex items-center gap-1 text-[11px] text-destructive">
      <CircleAlert className="h-3 w-3 shrink-0" />
      {message}
    </p>
  );
}
