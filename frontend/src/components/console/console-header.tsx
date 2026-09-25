import { CircleAlert, Info, Laptop, Moon, Sun } from "lucide-react";

import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui";
import type { Health } from "@/lib/schemas";
import type { Resource } from "@/lib/use-resource";
import { useTheme, type ThemeMode } from "@/lib/theme";

const MODES: { mode: ThemeMode; label: string; icon: typeof Sun }[] = [
  { mode: "light", label: "Light", icon: Sun },
  { mode: "dark", label: "Dark", icon: Moon },
  { mode: "system", label: "System", icon: Laptop },
];

function ThemeMenu() {
  const { mode, resolved, setMode } = useTheme();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm">
          {resolved === "dark" ? <Moon aria-hidden="true" /> : <Sun aria-hidden="true" />}
          {resolved === "dark" ? "Dark" : "Light"}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Colour mode</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup value={mode} onValueChange={(value) => setMode(value as ThemeMode)}>
          {MODES.map(({ mode: option, label, icon: Icon }) => (
            <DropdownMenuRadioItem key={option} value={option}>
              <Icon aria-hidden="true" />
              {label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function HealthStatus({ health }: { health: Resource<Health> }) {
  if (health.status === "loading") {
    return <Badge variant="neutral">Checking API</Badge>;
  }
  if (health.status === "error") {
    return (
      <Badge variant="danger">
        <CircleAlert aria-hidden="true" />
        API unreachable
      </Badge>
    );
  }
  return (
    <Badge variant="success">
      API {health.data.status}
    </Badge>
  );
}

/**
 * The header states the two things a reader must not be able to miss: the
 * console is a read-only demo over in-memory fixture state, and whether the
 * Python API it reads is answering at all.
 */
export function ConsoleHeader({ health }: { health: Resource<Health> }) {
  return (
    <header className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="grid gap-1">
          <h1 className="text-2xl font-semibold tracking-tight">Atlas ERP console</h1>
          <p className="text-sm text-muted-foreground">
            Read-only operator surface. Every value below is read from the Python API.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <HealthStatus health={health} />
          <ThemeMenu />
        </div>
      </div>

      {/* A banner that is present on every load is a polite status, not an
          assertive alert: an error Alert below is the one that must interrupt. */}
      <Alert role="status" variant="warning">
        <Info aria-hidden="true" />
        <AlertTitle>Demo fixture, in memory.</AlertTitle>
        <AlertDescription>
          <p>
            The catalogue mirrors the committed example seed, and the items, stock, sales, and
            journals are process-local state rebuilt on every start. Nothing is read from or
            written to a database, no connected peer is involved, and everything here is gone
            when the process exits. This console writes nothing.
          </p>
          {health.status === "ready" ? (
            <p>
              The API reports <code>{health.data.app_id}</code> on{" "}
              <code>{health.data.bind}</code>, data <code>{health.data.data}</code> from{" "}
              <code>{health.data.catalog_source}</code>, persistence{" "}
              <code>{health.data.persistence}</code>, {health.data.capabilities} capabilities. The
              Connect API runs separately on {health.data.connect_api}.
            </p>
          ) : null}
        </AlertDescription>
      </Alert>
    </header>
  );
}
