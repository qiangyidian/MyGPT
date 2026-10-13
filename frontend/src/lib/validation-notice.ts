// 生成后校验（RAG-13）结论的展示层归一化。
//
// 同一份结论从**两条路**到达界面，形状不同：
//   * 实时 —— SSE 的 `validation` 帧，camelCase，带面向用户的 `message` 与
//     `corrected_text`；
//   * 刷新后 —— 消息 metadata 里的 `validation` 块（`PostprocessOutcome.to_metadata()`），
//     snake_case，且按设计**不含答案正文、也不含那句文案**（metadata 里不放可能泄
//     漏的正文）。
//
// 归一化收敛在这一处，组件只认一种输入。否则极易出现「实时有提示、刷新后提示消失」
// 这种只在一条路径上存在的差异 —— 而用户会在刷新之后才来问「刚才那句提示是什么意思」。
import type { ValidationCheck, ValidationNotice } from "@/lib/types";

/** 检查名 → 用户能看懂的名字。没有条目的一律用原名兜底，不静默吞掉。 */
const CHECK_LABELS: Record<string, string> = {
  schema: "回答格式",
  citation_ids_exist: "引用来源存在性",
  authorization_recheck: "证据权限复核",
  pii_policy: "敏感信息",
  length: "长度限制",
  high_risk_grounding: "高风险问题的依据",
  grounding: "依据一致性（审计）",
};

export function checkLabel(name: string): string {
  return CHECK_LABELS[name] ?? name;
}

/** 未通过的检查（`skip` / `audit` 不算未通过 —— 审计不参与判定）。 */
export function failedChecks(checks: ValidationCheck[]): ValidationCheck[] {
  return checks.filter((c) => c.status === "fail");
}

export interface ValidationBadge {
  tone: "danger" | "warning" | "info";
  label: string;
  /** 折叠展示的细节（未通过的检查名等）。为空时不渲染展开入口。 */
  details: string[];
  /** 服务端原文（流式帧带；刷新后为空）。仅作 title 提示，不重复渲染。 */
  title?: string;
}

/**
 * 结论 → 角标。干净的回合返回 `null`（**不**显示「已校验」—— 每个回答都挂一个
 * 绿色的勾只会把人训练到无视它）。
 *
 * 文案按 `status` / `action` 派生，而不是用服务端那句 `message`：流式档下正文
 * 已经被那句文案顶掉了，角标再原样重复一遍就是同一段话出现两次；刷新后 metadata
 * 里又没有那句文案。按状态派生两条路渲染一致。
 */
export function validationBadge(notice: ValidationNotice | null): ValidationBadge | null {
  if (!notice) return null;
  const failed = failedChecks(notice.checks).map((c) => checkLabel(c.name));
  const details: string[] = [];
  const title = notice.message || undefined;

  if (notice.status === "refused") {
    details.push(...failed);
    if (notice.refusalReason) details.push(`原因代码：${notice.refusalReason}`);
    return {
      tone: "danger",
      label: "已拒答：依据不足以支撑回答",
      details,
      title,
    };
  }

  if (notice.status === "insufficient_evidence") {
    details.push(...failed);
    if (notice.refusalReason) details.push(`原因代码：${notice.refusalReason}`);
    return {
      tone: "danger",
      label: "知识库中没有找到可依据的内容",
      details,
      title,
    };
  }

  if (notice.droppedClaimCount > 0) {
    details.push(...failed);
    details.push(`已移除 ${notice.droppedClaimCount} 条缺少依据的主张`);
    return {
      tone: "warning",
      label: `有 ${notice.droppedClaimCount} 条内容未通过校验，已从回答中移除`,
      details,
      title,
    };
  }

  if (notice.action === "retry_format_once") {
    return {
      tone: "warning",
      label: "回答未通过格式校验",
      details: [...failed, ...(title ? [title] : [])],
      title,
    };
  }

  if (failed.length > 0) {
    // 兜底：状态说「没事」但有检查失败（例如拒答之外的降级动作）。不静默丢弃。
    return { tone: "warning", label: "回答未完全通过校验", details: failed, title };
  }

  return null;
}

/**
 * 从消息 metadata 还原结论（刷新后的历史消息）。没有 `validation` 块（未跑门禁的
 * 回合、或门禁上线之前的旧消息）返回 `null` —— 不猜。
 */
export function noticeFromMetadata(metadata: unknown): ValidationNotice | null {
  if (!metadata || typeof metadata !== "object") return null;
  const md = metadata as Record<string, unknown>;
  const raw = md.validation;
  if (!raw || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  const risk = (md.risk ?? {}) as ValidationNotice["risk"];
  return {
    traceId: typeof md.trace_id === "string" ? md.trace_id : "",
    status: typeof v.status === "string" ? v.status : "",
    action: typeof v.action === "string" ? v.action : "",
    checks: Array.isArray(v.checks) ? (v.checks as ValidationCheck[]) : [],
    // metadata 里没有那句文案（不落正文），角标本来也不靠它。
    message: "",
    correctedText: null,
    droppedClaimCount:
      typeof v.dropped_claim_count === "number" ? v.dropped_claim_count : 0,
    refusalReason: typeof v.refusal_reason === "string" ? v.refusal_reason : null,
    risk,
  };
}
