import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";

import { cn } from "@/lib/utils";

// The banner and error states are the `state.*` foreground-on-background pairs,
// used as supplied (contract.md v1.1.1 token mapping).
const alertVariants = cva(
  "relative grid w-full grid-cols-[0_1fr] items-start gap-y-1 rounded-lg border px-4 py-3 text-sm has-[>svg]:grid-cols-[calc(var(--spacing)*4)_1fr] has-[>svg]:gap-x-3",
  {
    variants: {
      variant: {
        info: "border-info-text bg-info-bg text-info-text",
        warning: "border-warning-text bg-warning-bg text-warning-text",
        danger: "border-danger-text bg-danger-bg text-danger-text",
        success: "border-success-indicator bg-success-bg text-success-text",
      },
    },
    defaultVariants: { variant: "info" },
  },
);

function Alert({
  className,
  variant,
  ...props
}: React.ComponentProps<"div"> & VariantProps<typeof alertVariants>) {
  return (
    <div
      role="alert"
      data-slot="alert"
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  );
}

function AlertTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-title"
      className={cn("col-start-2 min-h-4 font-medium tracking-tight", className)}
      {...props}
    />
  );
}

function AlertDescription({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-description"
      className={cn("col-start-2 grid justify-items-start gap-1 text-sm", className)}
      {...props}
    />
  );
}

export { Alert, AlertDescription, AlertTitle };
