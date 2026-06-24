import { useState } from "react";
import { isValidMcpServerName } from "@/lib/bundleManipulation";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export interface McpServerFormResult {
  name: string;
  transport: "http" | "stdio";
  url?: string;
  headers?: Record<string, string>;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
}

/** Parse "KEY=VALUE" lines into a Record. */
function parseKVLines(text: string): Record<string, string> | undefined {
  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  if (lines.length === 0) return undefined;
  const result: Record<string, string> = {};
  for (const line of lines) {
    const eq = line.indexOf("=");
    if (eq > 0) {
      result[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
    }
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

/**
 * Dialog for adding an MCP server to a session's agent mid-session.
 *
 * Collects server name, transport (stdio/http), and transport-specific
 * fields. On submit, passes the result back via `onAdd`.
 */
export function AddMcpServerDialog({
  open,
  onOpenChange,
  onAdd,
  submitting,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdd: (server: McpServerFormResult) => void;
  submitting?: boolean;
}) {
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"http" | "stdio">("stdio");
  const [url, setUrl] = useState("");
  const [headers, setHeaders] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");

  function reset() {
    setName("");
    setTransport("stdio");
    setUrl("");
    setHeaders("");
    setCommand("");
    setArgs("");
    setEnv("");
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset();
    onOpenChange(next);
  }

  function handleSubmit() {
    const trimmedName = name.trim();
    if (!trimmedName) return;

    const result: McpServerFormResult = { name: trimmedName, transport };
    if (transport === "stdio") {
      result.command = command.trim() || undefined;
      result.args = args.trim().split(/\s+/).filter(Boolean);
      if (result.args.length === 0) result.args = undefined;
      result.env = parseKVLines(env);
    } else {
      result.url = url.trim() || undefined;
      result.headers = parseKVLines(headers);
    }

    onAdd(result);
  }

  const nameValid = isValidMcpServerName(name.trim());
  const canSubmit =
    nameValid &&
    !submitting &&
    (transport === "stdio" ? command.trim().length > 0 : url.trim().length > 0);

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="add-mcp-server-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-md"
      >
        <DialogHeader>
          <DialogTitle>Add MCP server</DialogTitle>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
          <div className="flex gap-2">
            <div className="flex flex-1 flex-col gap-1.5">
              <label className="text-xs font-medium text-muted-foreground">
                Name <span className="text-destructive">*</span>
              </label>
              <Input
                data-testid="add-mcp-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="server-name"
                autoFocus
              />
              {name.trim().length > 0 && !nameValid && (
                <p className="text-[10px] text-destructive">
                  Letters, digits, hyphens, underscores only
                </p>
              )}
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-muted-foreground">Transport</label>
              <Select value={transport} onValueChange={(v: "http" | "stdio") => setTransport(v)}>
                <SelectTrigger data-testid="add-mcp-transport" className="w-24">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="stdio">stdio</SelectItem>
                  <SelectItem value="http">http</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          {transport === "stdio" ? (
            <>
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-medium text-muted-foreground">
                  Command <span className="text-destructive">*</span>
                </label>
                <Input
                  data-testid="add-mcp-command"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="npx"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-medium text-muted-foreground">Arguments</label>
                <Input
                  data-testid="add-mcp-args"
                  value={args}
                  onChange={(e) => setArgs(e.target.value)}
                  placeholder="-y @modelcontextprotocol/server-filesystem /tmp"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-medium text-muted-foreground">
                  Environment variables
                </label>
                <Textarea
                  data-testid="add-mcp-env"
                  value={env}
                  onChange={(e) => setEnv(e.target.value)}
                  placeholder={"KEY=VALUE per line\ne.g. GITHUB_TOKEN=ghp_..."}
                  className="min-h-[60px] font-mono text-xs"
                />
              </div>
            </>
          ) : (
            <>
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-medium text-muted-foreground">
                  URL <span className="text-destructive">*</span>
                </label>
                <Input
                  data-testid="add-mcp-url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://mcp.example.com/sse"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-medium text-muted-foreground">Headers</label>
                <Textarea
                  data-testid="add-mcp-headers"
                  value={headers}
                  onChange={(e) => setHeaders(e.target.value)}
                  placeholder={"KEY=VALUE per line\ne.g. Authorization=Bearer tok_..."}
                  className="min-h-[60px] font-mono text-xs"
                />
              </div>
            </>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button data-testid="add-mcp-submit" onClick={handleSubmit} disabled={!canSubmit}>
            {submitting ? "Adding…" : "Add"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
