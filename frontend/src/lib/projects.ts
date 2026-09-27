// 项目改名 / 删除的表单逻辑（条目 30）。
//
// 纯函数放这里的原因和会话分页一样：vitest 没有 jsdom，组件渲染不可测，
// 而「什么算合法的项目名」「删除确认框该写哪些后果」正是会写错的地方。
import type { Project, ProjectImpact } from "@/lib/types";

/** 与后端 `Project.name` 的 String(255) 对齐（app/api/projects.py: NAME_MAX）。 */
export const PROJECT_NAME_MAX = 255;

/** 后端 `_clean_color` 接受的颜色格式：#RGB 或 #RRGGBB。 */
export const PROJECT_COLOR_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

export const DEFAULT_PROJECT_COLOR = "#6366f1";

/** 返回中文错误信息；`null` 表示可以提交。 */
export function validateProjectName(raw: string): string | null {
  const name = raw.trim();
  if (!name) return "项目名称不能为空";
  if (name.length > PROJECT_NAME_MAX) {
    return `项目名称不能超过 ${PROJECT_NAME_MAX} 个字符`;
  }
  return null;
}

export function validateProjectColor(raw: string): string | null {
  if (!raw.trim()) return "请选择一个颜色";
  if (!PROJECT_COLOR_RE.test(raw.trim())) return "项目颜色需为 #RRGGBB 或 #RGB 格式";
  return null;
}

/** 与后端一致：去掉首尾空格后才落库。 */
export function normalizeProjectName(raw: string): string {
  return raw.trim();
}

/**
 * 改名时该不该发请求：空白、超长、与原名相同都不发。
 * 返回 `null` 表示无事可做（而不是一个错误）。
 */
export function projectRenamePatch(
  project: Pick<Project, "name">,
  next: string
): { name: string } | null {
  const error = validateProjectName(next);
  if (error) return null;
  const name = normalizeProjectName(next);
  return name === project.name ? null : { name };
}

/** 删除确认框的后果摘要（服务端算出来的真实数字，不猜）。 */
export interface ProjectDeleteConsequences {
  /** 会话是否会被一起删掉。后端目前是 false：project_id 是软引用，没有 FK。 */
  destructive: boolean;
  lines: string[];
  /** 需要手动输入的确认词 —— 项目名本身，输入完全一致才允许删除。 */
  confirmation: string;
}

export function projectDeleteConsequences(
  project: Pick<Project, "name">,
  impact: ProjectImpact | null | undefined
): ProjectDeleteConsequences {
  const lines: string[] = [];
  const destructive = !!impact?.deletes_conversations;

  if (!impact) {
    return {
      destructive,
      confirmation: project.name,
      lines: ["正在统计影响范围……"],
    };
  }

  if (destructive) {
    lines.push(
      `将连带删除 ${impact.conversation_count} 个会话（含 ${impact.message_count} 条消息），此操作不可恢复。`
    );
    if (impact.archived_conversation_count > 0) {
      lines.push(`其中 ${impact.archived_conversation_count} 个会话处于归档状态。`);
    }
  } else {
    lines.push(
      impact.conversation_count > 0
        ? `将删除项目本身，以及它对 ${impact.conversation_count} 个会话的分组。`
        : "将删除项目本身（该项目下没有会话）。"
    );
    lines.push(
      impact.conversation_count > 0
        ? `${impact.conversation_count} 个会话不会被删除，会变为「未分组」，其中的 ${impact.message_count} 条消息全部保留。`
        : `会话与消息不会被删除，共 ${impact.message_count} 条消息保持原样。`
    );
    if (impact.archived_conversation_count > 0) {
      lines.push(
        `其中 ${impact.archived_conversation_count} 个会话处于归档状态，同样只是变为未分组。`
      );
    }
  }
  lines.push(
    impact.knowledge_base_count > 0
      ? `将连带删除 ${impact.knowledge_base_count} 个知识库。`
      : "知识库不归属项目，不会有任何改动。"
  );
  return { destructive, lines, confirmation: project.name };
}

/** 输入必须与项目名完全一致（去首尾空格）才允许删除。 */
export function projectDeleteConfirmed(projectName: string, typed: string): boolean {
  const expected = projectName.trim();
  if (!expected) return false;
  return typed.trim() === expected;
}
