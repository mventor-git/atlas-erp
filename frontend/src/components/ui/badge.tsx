import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";

import { cn } from "@/lib/utils";

// The four `state.*` pairs plus neutral. Success carries a non-text indicator
// dot because the supplied light success foreground is 3.38:1 and must not be
// used as the status text colour (contract.md v1.2.0).
const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
  {
    variants: {
      variant: {
        neutral: "border-input bg-background text-foreground",
        success: "border-success-indicator bg-success-bg text-success-text",
        warning: "border-warning-text bg-warning-bg text-warning-text",
        danger: "border-danger-text bg-danger-bg text-danger-text",
        info: "border-info-text bg-info-bg text-info-text",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
);

function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return (
    <span
      data-slot="badge"
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  );
}

export { Badge, badgeVariants };
export type BadgeVariants = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;
