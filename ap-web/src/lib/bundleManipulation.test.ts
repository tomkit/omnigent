import { describe, expect, it } from "vitest";
import {
  parseTar,
  rebuildTar,
  normalizeName,
  removeInlineMcpFromYaml,
  buildMcpYaml,
  isValidMcpServerName,
  type McpServerInput,
} from "./bundleManipulation";

// ── Mock CompressionStream/DecompressionStream for jsdom ──────────

class PassthroughStream {
  readable: ReadableStream;
  writable: WritableStream;
  constructor() {
    let controller: ReadableStreamDefaultController;
    this.readable = new ReadableStream({
      start(c) {
        controller = c;
      },
    });
    this.writable = new WritableStream({
      write(chunk) {
        controller.enqueue(new Uint8Array(chunk));
      },
      close() {
        controller.close();
      },
    });
  }
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).CompressionStream = PassthroughStream;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).DecompressionStream = PassthroughStream;

// ── Helpers to build server-shaped tar fixtures ────────────────────

/** Build a tar header for a directory entry (typeflag '5'). */
function dirHeader(name: string): Uint8Array {
  const h = new Uint8Array(512);
  const enc = new TextEncoder();
  const dirName = name.endsWith("/") ? name : name + "/";
  h.set(enc.encode(dirName).slice(0, 100), 0);
  // mode 0755
  h.set(enc.encode("0000755\0"), 100);
  // size 0
  h.set(enc.encode("00000000000\0"), 124);
  // mtime
  h.set(enc.encode("00000000000\0"), 136);
  // typeflag '5' = directory
  h[156] = 0x35;
  // magic + version
  h.set(enc.encode("ustar\0"), 257);
  h.set(enc.encode("00"), 263);
  // checksum
  for (let i = 148; i < 156; i++) h[i] = 0x20;
  let sum = 0;
  for (let i = 0; i < 512; i++) sum += h[i];
  const sumStr = sum.toString(8).padStart(6, "0");
  h.set(enc.encode(sumStr), 148);
  h[154] = 0;
  h[155] = 0x20;
  return h;
}

/** Build a tar header for a regular file (typeflag '0'). */
function fileHeader(name: string, size: number): Uint8Array {
  const h = new Uint8Array(512);
  const enc = new TextEncoder();

  let entryName = name;
  let prefix = "";
  if (entryName.length > 100) {
    const sep = entryName.lastIndexOf("/", 99);
    if (sep > 0) {
      prefix = entryName.slice(0, sep);
      entryName = entryName.slice(sep + 1);
    }
  }
  h.set(enc.encode(entryName).slice(0, 100), 0);
  if (prefix) h.set(enc.encode(prefix).slice(0, 155), 345);

  h.set(enc.encode("0000644\0"), 100);
  const sizeStr = size.toString(8).padStart(11, "0");
  h.set(enc.encode(sizeStr + "\0"), 124);
  h.set(enc.encode("00000000000\0"), 136);
  h[156] = 0x30; // typeflag '0'
  h.set(enc.encode("ustar\0"), 257);
  h.set(enc.encode("00"), 263);
  // checksum
  for (let i = 148; i < 156; i++) h[i] = 0x20;
  let sum = 0;
  for (let i = 0; i < 512; i++) sum += h[i];
  const sumStr2 = sum.toString(8).padStart(6, "0");
  h.set(enc.encode(sumStr2), 148);
  h[154] = 0;
  h[155] = 0x20;
  return h;
}

/**
 * Build a server-shaped tar archive with ./ prefixed entries,
 * directory members, and a config.yaml file.
 */
function buildServerShapedTar(files: { name: string; content: string }[]): Uint8Array {
  const blocks: Uint8Array[] = [];
  const enc = new TextEncoder();

  // Add directory entries the server would include
  const dirs = new Set<string>();
  for (const f of files) {
    const parts = f.name.split("/");
    for (let i = 1; i < parts.length; i++) {
      dirs.add(parts.slice(0, i).join("/") + "/");
    }
  }
  // Root dir
  blocks.push(dirHeader("./"));
  for (const d of Array.from(dirs).sort()) {
    blocks.push(dirHeader(`./${d}`));
  }

  // Add file entries
  for (const f of files) {
    const content = enc.encode(f.content);
    const header = fileHeader(`./${f.name}`, content.length);
    blocks.push(header);
    const padded = new Uint8Array(Math.ceil(content.length / 512) * 512);
    padded.set(content);
    blocks.push(padded);
  }

  // End-of-archive
  blocks.push(new Uint8Array(1024));

  const total = blocks.reduce((n, b) => n + b.length, 0);
  const result = new Uint8Array(total);
  let off = 0;
  for (const b of blocks) {
    result.set(b, off);
    off += b.length;
  }
  return result;
}

// ── Tests ──────────────────────────────────────────────────────────

describe("normalizeName", () => {
  it("strips leading ./", () => {
    expect(normalizeName("./config.yaml")).toBe("config.yaml");
    expect(normalizeName("./tools/mcp/foo.yaml")).toBe("tools/mcp/foo.yaml");
  });
  it("leaves unprefixed names unchanged", () => {
    expect(normalizeName("config.yaml")).toBe("config.yaml");
  });
});

describe("isValidMcpServerName", () => {
  it("accepts valid names", () => {
    expect(isValidMcpServerName("github")).toBe(true);
    expect(isValidMcpServerName("my-server_2")).toBe(true);
  });
  it("rejects names with special characters", () => {
    expect(isValidMcpServerName("../../evil")).toBe(false);
    expect(isValidMcpServerName("foo/bar")).toBe(false);
    expect(isValidMcpServerName("has spaces")).toBe(false);
    expect(isValidMcpServerName("")).toBe(false);
  });
});

describe("parseTar + rebuildTar round-trip", () => {
  it("preserves a server-shaped tar with directory entries", () => {
    const configYaml = "spec_version: 1\nname: test\n";
    const tar = buildServerShapedTar([{ name: "config.yaml", content: configYaml }]);

    const entries = parseTar(tar);
    // Should have directory entries (./  + root) + file
    const dirs = entries.filter((e) => e.typeflag === "5");
    const files = entries.filter((e) => e.typeflag === "0");
    expect(dirs.length).toBeGreaterThan(0);
    expect(files.length).toBe(1);
    expect(normalizeName(files[0].name)).toBe("config.yaml");

    // Round-trip: rebuild should produce a valid tar
    const rebuilt = rebuildTar(entries);
    const reparsed = parseTar(rebuilt);
    expect(reparsed.length).toBe(entries.length);

    // Directory entries must still have typeflag '5'
    const reDirs = reparsed.filter((e) => e.typeflag === "5");
    expect(reDirs.length).toBe(dirs.length);
    for (const d of reDirs) {
      expect(d.typeflag).toBe("5");
    }
  });

  it("preserves ./-prefixed entry names through round-trip", () => {
    const tar = buildServerShapedTar([
      { name: "config.yaml", content: "name: test\n" },
      { name: "tools/mcp/github.yaml", content: "name: github\ntransport: stdio\n" },
    ]);

    const entries = parseTar(tar);
    const fileNames = entries.filter((e) => e.typeflag === "0").map((e) => e.name);
    expect(fileNames).toContain("./config.yaml");
    expect(fileNames).toContain("./tools/mcp/github.yaml");

    // After rebuild, names are preserved
    const rebuilt = rebuildTar(entries);
    const reparsed = parseTar(rebuilt);
    const reParsedNames = reparsed.filter((e) => e.typeflag === "0").map((e) => e.name);
    expect(reParsedNames).toContain("./config.yaml");
    expect(reParsedNames).toContain("./tools/mcp/github.yaml");
  });

  it("can add an MCP file to a server-shaped tar and preserve dirs", () => {
    const tar = buildServerShapedTar([{ name: "config.yaml", content: "name: test\n" }]);
    const entries = parseTar(tar);

    // Simulate adding a new MCP server file
    const mcpYaml = new TextEncoder().encode("name: fs\ntransport: stdio\ncommand: npx\n");
    const newFileName = "./tools/mcp/fs.yaml";
    const newHeader = fileHeader(newFileName, mcpYaml.length);
    entries.push({
      header: newHeader,
      name: newFileName,
      typeflag: "0",
      content: mcpYaml,
    });

    const rebuilt = rebuildTar(entries);
    const reparsed = parseTar(rebuilt);
    const fileNames = reparsed.filter((e) => e.typeflag === "0").map((e) => normalizeName(e.name));
    expect(fileNames).toContain("config.yaml");
    expect(fileNames).toContain("tools/mcp/fs.yaml");

    // Dirs still have correct typeflag
    for (const e of reparsed.filter((e) => e.typeflag === "5")) {
      expect(e.content.length).toBe(0);
    }
  });

  it("can remove an MCP file using normalized name matching", () => {
    const tar = buildServerShapedTar([
      { name: "config.yaml", content: "name: test\n" },
      { name: "tools/mcp/github.yaml", content: "name: github\ntransport: stdio\n" },
    ]);
    const entries = parseTar(tar);

    // Remove github.yaml using normalized name matching
    const filtered = entries.filter((e) => normalizeName(e.name) !== "tools/mcp/github.yaml");
    expect(filtered.length).toBe(entries.length - 1);

    const rebuilt = rebuildTar(filtered);
    const reparsed = parseTar(rebuilt);
    const fileNames = reparsed.filter((e) => e.typeflag === "0").map((e) => normalizeName(e.name));
    expect(fileNames).toContain("config.yaml");
    expect(fileNames).not.toContain("tools/mcp/github.yaml");
  });
});

describe("removeInlineMcpFromYaml", () => {
  it("removes an inline MCP block from config.yaml", () => {
    const yaml = [
      "tools:",
      "  builtins:",
      "    - web_search",
      "  github:",
      "    type: mcp",
      "    command: npx",
      "    args: [-y]",
      "",
    ].join("\n");

    const result = removeInlineMcpFromYaml(yaml, "github");
    expect(result).toContain("builtins:");
    expect(result).not.toContain("github:");
    expect(result).not.toContain("type: mcp");
  });

  it("preserves non-MCP blocks", () => {
    const yaml = [
      "tools:",
      "  builtins:",
      "    - web_search",
      "  github:",
      "    type: mcp",
      "    command: npx",
      "",
    ].join("\n");

    const result = removeInlineMcpFromYaml(yaml, "other");
    expect(result).toContain("github:");
    expect(result).toContain("type: mcp");
  });

  it("handles type: mcp not being the first child key", () => {
    const yaml = [
      "tools:",
      "  myserver:",
      "    command: npx",
      "    type: mcp",
      "    args: [-y]",
      "",
    ].join("\n");

    const result = removeInlineMcpFromYaml(yaml, "myserver");
    expect(result).not.toContain("myserver:");
  });
});

describe("buildMcpYaml", () => {
  it("generates valid YAML for a stdio server", () => {
    const server: McpServerInput = {
      name: "github",
      transport: "stdio",
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-github"],
      env: { GITHUB_TOKEN: "ghp_test" },
    };
    const yaml = buildMcpYaml(server);
    expect(yaml).toContain("name: github");
    expect(yaml).toContain("transport: stdio");
    expect(yaml).toContain("command: npx");
    expect(yaml).toContain("GITHUB_TOKEN: ghp_test");
  });

  it("generates valid YAML for an HTTP server", () => {
    const server: McpServerInput = {
      name: "search",
      transport: "http",
      url: "https://mcp.example.com/sse",
    };
    const yaml = buildMcpYaml(server);
    expect(yaml).toContain("name: search");
    expect(yaml).toContain("transport: http");
    expect(yaml).toContain('url: "https://mcp.example.com/sse"');
  });
});
