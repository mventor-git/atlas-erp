import { AlertCircle, RefreshCw } from "lucide-react";
import type * as React from "react";

import { Alert, AlertDescription, AlertTitle, Button, Skeleton } from "@/components/ui";
import type { Resource } from "@/lib/use-resource";

type SectionProps<T> = {
  resource: Resource<T>;
  /** Noun for the loading and empty copy, e.g. "stock by warehouse". */
  label: string;
  isEmpty?: (data: T) => boolean;
  children: (data: T) => React.ReactNode;
};

/**
 * The three states every API-backed panel needs, in one place: a loading
 * placeholder that a screen reader announces, a failure that keeps the message
 * and offers a retry, and an explicit empty result.
 */
export function AsyncSection<T>({ resource, label, isEmpty, children }: SectionProps<T>) {
  if (resource.status === "loading") {
    return (
      <div role="status" aria-live="polite" className="grid gap-2">
        <span className="sr-only">Loading {label}.</span>
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-8 w-2/3" />
      </div>
    );
  }

  if (resource.status === "error") {
    return (
      <Alert variant="danger">
        <AlertCircle aria-hidden="true" />
        <AlertTitle>Could not load {label}</AlertTitle>
        <AlertDescription>
          <p>{resource.error}</p>
          <Button variant="outline" size="sm" onClick={resource.reload}>
            <RefreshCw aria-hidden="true" />
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    );
  }

  if (isEmpty?.(resource.data)) {
    return (
      <p className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
        The API reported no {label}.
      </p>
    );
  }

  return <>{children(resource.data)}</>;
}
