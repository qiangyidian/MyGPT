"use client";

import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";

import { cn } from "@/lib/utils";
import { Input, type InputProps } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

/**
 * Password input with a show/hide eye toggle.
 *
 * Keyboard + a11y details:
 * - The eye is a real <button> (focusable, labelled) so keyboard users can
 *   toggle it; the wrapper div is a flex row so the button never covers text.
 * - ``tabIndex={-1}`` keeps it OUT of the tab order — Tab should move to the
 *   next form field, not a visibility toggle; it stays reachable by click and
 *   via arrow-focus patterns used by password managers.
 * - ``aria-label`` reflects the CURRENT action (显示/隐藏), and
 *   ``aria-pressed`` carries the state.
 * - ``type`` switches password ⇄ text; ``autoComplete`` is preserved from
 *   props so password-manager heuristics keep working.
 */
export function PasswordInput({ className, ...props }: InputProps) {
  const [visible, setVisible] = useState(false);

  return (
    <div className={cn("relative flex items-center", className)}>
      <Input
        {...props}
        type={visible ? "text" : "password"}
        className="pr-10"
      />
      <Button
        type="button"
        variant="ghost"
        size="icon"
        tabIndex={-1}
        aria-label={visible ? "隐藏密码" : "显示密码"}
        aria-pressed={visible}
        onClick={() => setVisible((v) => !v)}
        onMouseDown={(e) => e.preventDefault()} // don't steal focus from the input on click
        className="absolute right-1 h-7 w-7 text-muted-foreground hover:bg-transparent hover:text-foreground"
      >
        {visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </Button>
    </div>
  );
}
