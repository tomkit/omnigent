/**
 * Client-side agent bundle manipulation: download, modify, and re-upload
 * `.tar.gz` bundles via the session agent endpoints.
 *
 * Used by the in-session MCP server editor to add/remove MCP server
 * YAML files (`tools/mcp/<name>.yaml`) from an existing agent bundle
 * without re-authoring the entire spec.
 *
 * The server produces `.`-rooted POSIX tars with directory entries
 * (typeflag `5`) and PAX extended headers (typeflag `x`). The
 * round-trip here **preserves raw headers** for non-regular-file
 * entries so the server's `extract_safe` accepts the re-upload.
 */

import { authenticatedFetch } from "./identity";

// ── Tar helpers ────────────────────────────────────────────────────

/**
 * A raw tar entry — preserves the original 512-byte header verbatim
 * so directory/PAX/link entries round-trip without the builder needing
 * to understand every typeflag.
 */
interface RawTarEntry {
  /** Original 512-byte header, preserved exactly. */
  header: Uint8Array;
  /** Entry name (from header, with prefix). */
  name: string;
  /** Typeflag character from offset 156: '0'=file, '5'=dir, 'x'=PAX, etc. */
  typeflag: string;
  /** Content bytes (empty for directories). */
  content: Uint8Array;
}

/** Decompress gzip bytes using the browser's DecompressionStream. */
async function gunzip(data: ArrayBuffer): Promise<Uint8Array> {
  const ds = new DecompressionStream("gzip");
  const writer = ds.writable.getWriter();
  writer.write(data);
  writer.close();
  const reader = ds.readable.getReader();
  const chunks: Uint8Array[] = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(new Uint8Array(value));
  }
  return concat(chunks);
}

/** Gzip compress bytes. */
async function gzip(data: Uint8Array): Promise<Uint8Array> {
  const cs = new CompressionStream("gzip");
  const writer = cs.writable.getWriter();
  writer.write(data.buffer as ArrayBuffer);
  writer.close();
  const reader = cs.readable.getReader();
  const chunks: Uint8Array[] = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(new Uint8Array(value));
  }
  return concat(chunks);
}

function concat(chunks: Uint8Array[]): Uint8Array {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const result = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    result.set(c, off);
    off += c.length;
  }
  return result;
}

/** Read a null-terminated string from a tar header field. */
function readTarString(tar: Uint8Array, offset: number, length: number): string {
  const slice = tar.slice(offset, offset + length);
  const nullIdx = slice.indexOf(0);
  return new TextDecoder().decode(nullIdx >= 0 ? slice.slice(0, nullIdx) : slice);
}

/** Read an octal number from a tar header field. */
function readTarOctal(tar: Uint8Array, offset: number, length: number): number {
  return parseInt(readTarString(tar, offset, length), 8) || 0;
}

/**
 * Strip the leading `./` prefix from a tar entry name.
 * Server bundles use `./config.yaml`, `./tools/mcp/foo.yaml`, etc.
 */
function normalizeName(name: string): string {
  return name.replace(/^\.\//, "");
}

/**
 * Parse a POSIX tar archive into raw entries, preserving headers
 * verbatim. Directory, PAX, and link entries are kept as-is so the
 * round-trip doesn't corrupt them.
 */
function parseTar(tar: Uint8Array): RawTarEntry[] {
  const entries: RawTarEntry[] = [];
  let pos = 0;
  while (pos + 512 <= tar.length) {
    // Check for end-of-archive (all-zero block)
    let allZero = true;
    for (let i = 0; i < 512; i++) {
      if (tar[pos + i] !== 0) {
        allZero = false;
        break;
      }
    }
    if (allZero) break;

    const header = tar.slice(pos, pos + 512);
    const nameField = readTarString(tar, pos, 100);
    const size = readTarOctal(tar, pos + 124, 12);
    const typeflag = String.fromCharCode(tar[pos + 156] || 0x30);
    const prefix = readTarString(tar, pos + 345, 155);
    const fullName = prefix ? `${prefix}/${nameField}` : nameField;

    const contentStart = pos + 512;
    const contentBlocks = size > 0 ? Math.ceil(size / 512) : 0;
    const content = tar.slice(contentStart, contentStart + size);

    entries.push({
      header: new Uint8Array(header),
      name: fullName,
      typeflag,
      content: new Uint8Array(content),
    });

    pos = contentStart + contentBlocks * 512;
  }
  return entries;
}

/** Write a number as null-terminated octal string into a tar header. */
function writeOctal(header: Uint8Array, offset: number, length: number, value: number): void {
  const str = value.toString(8).padStart(length - 1, "0");
  const bytes = new TextEncoder().encode(str);
  header.set(bytes.slice(0, length - 1), offset);
  header[offset + length - 1] = 0;
}

/** Compute and write the checksum for a tar header. */
function writeChecksum(header: Uint8Array): void {
  // Fill checksum field with spaces first
  for (let i = 148; i < 156; i++) header[i] = 0x20;
  let checksum = 0;
  for (let i = 0; i < 512; i++) checksum += header[i];
  writeOctal(header, 148, 7, checksum);
  header[155] = 0x20;
}

/**
 * Build a fresh tar header for a new regular file entry.
 * Used only for newly added files (MCP YAML); existing entries
 * keep their original headers verbatim.
 */
function buildFileHeader(name: string, size: number): Uint8Array {
  const header = new Uint8Array(512);
  const encoder = new TextEncoder();

  // Split long names into prefix (345) + name (100)
  let entryName = name;
  let prefix = "";
  if (entryName.length > 100) {
    const sep = entryName.lastIndexOf("/", 99);
    if (sep > 0) {
      prefix = entryName.slice(0, sep);
      entryName = entryName.slice(sep + 1);
    }
  }
  header.set(encoder.encode(entryName).slice(0, 100), 0);
  if (prefix) header.set(encoder.encode(prefix).slice(0, 155), 345);

  writeOctal(header, 100, 8, 0o644); // mode
  writeOctal(header, 108, 8, 0); // uid
  writeOctal(header, 116, 8, 0); // gid
  writeOctal(header, 124, 12, size); // size
  writeOctal(header, 136, 12, Math.floor(Date.now() / 1000)); // mtime
  header[156] = 0x30; // typeflag '0' = regular file
  header.set(encoder.encode("ustar\0"), 257); // magic
  header.set(encoder.encode("00"), 263); // version

  writeChecksum(header);
  return header;
}

/**
 * Build a fresh tar header for a directory entry.
 */
function buildDirHeader(name: string): Uint8Array {
  const header = new Uint8Array(512);
  const encoder = new TextEncoder();
  const dirName = name.endsWith("/") ? name : name + "/";
  header.set(encoder.encode(dirName).slice(0, 100), 0);

  writeOctal(header, 100, 8, 0o755); // mode
  writeOctal(header, 108, 8, 0); // uid
  writeOctal(header, 116, 8, 0); // gid
  writeOctal(header, 124, 12, 0); // size = 0 for dirs
  writeOctal(header, 136, 12, Math.floor(Date.now() / 1000)); // mtime
  header[156] = 0x35; // typeflag '5' = directory
  header.set(encoder.encode("ustar\0"), 257);
  header.set(encoder.encode("00"), 263);

  writeChecksum(header);
  return header;
}

/**
 * Reassemble raw entries into a tar archive. Existing entries use
 * their preserved headers; content is padded to 512-byte blocks.
 */
function rebuildTar(entries: RawTarEntry[]): Uint8Array {
  const blocks: Uint8Array[] = [];
  for (const entry of entries) {
    blocks.push(entry.header);
    if (entry.content.length > 0) {
      const contentBlocks = Math.ceil(entry.content.length / 512);
      const padded = new Uint8Array(contentBlocks * 512);
      padded.set(entry.content);
      blocks.push(padded);
    }
  }
  // End-of-archive: two zero blocks
  blocks.push(new Uint8Array(1024));
  return concat(blocks);
}

// ── Public API ─────────────────────────────────────────────────────

/** Validate an MCP server name: alphanumeric, hyphens, underscores only. */
const MCP_NAME_RE = /^[A-Za-z0-9_-]+$/;

export function isValidMcpServerName(name: string): boolean {
  return MCP_NAME_RE.test(name) && name.length <= 64;
}

/** YAML quote a string value if it contains special characters. */
function yamlQuote(s: string): string {
  if (/[:\n"'#{}[\],&*?|>!%@`]/.test(s) || s.trim() !== s) {
    return JSON.stringify(s);
  }
  return s;
}

export interface McpServerInput {
  name: string;
  transport: "http" | "stdio";
  url?: string;
  headers?: Record<string, string>;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
}

/** Build a tools/mcp/<name>.yaml content for an MCP server. */
function buildMcpYaml(server: McpServerInput): string {
  const lines: string[] = [];
  lines.push(`name: ${server.name}`);
  lines.push(`transport: ${server.transport}`);
  if (server.transport === "stdio") {
    if (server.command) lines.push(`command: ${yamlQuote(server.command)}`);
    if (server.args?.length) {
      lines.push(`args: [${server.args.map((a) => yamlQuote(a)).join(", ")}]`);
    }
    if (server.env && Object.keys(server.env).length > 0) {
      lines.push("env:");
      for (const [k, v] of Object.entries(server.env)) {
        lines.push(`  ${k}: ${yamlQuote(v)}`);
      }
    }
  } else {
    if (server.url) lines.push(`url: ${yamlQuote(server.url)}`);
    if (server.headers && Object.keys(server.headers).length > 0) {
      lines.push("headers:");
      for (const [k, v] of Object.entries(server.headers)) {
        lines.push(`  ${k}: ${yamlQuote(v)}`);
      }
    }
  }
  return lines.join("\n") + "\n";
}

/**
 * Download the current agent bundle, add an MCP server YAML file,
 * and re-upload via PUT.
 */
export async function addMcpServerToSession(
  sessionId: string,
  server: McpServerInput,
): Promise<void> {
  if (!isValidMcpServerName(server.name)) {
    throw new Error(`Invalid server name: use only letters, digits, hyphens, underscores`);
  }

  // 1. Download current bundle
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/agent/contents`,
  );
  if (!res.ok) throw new Error(`Failed to download bundle: ${res.status}`);
  const bundleBytes = await res.arrayBuffer();

  // 2. Decompress and parse tar (preserving raw headers)
  const tar = await gunzip(bundleBytes);
  const entries = parseTar(tar);

  // 3. Determine the path prefix used by the bundle (./tools/mcp/ or tools/mcp/)
  const hasPrefix = entries.some((e) => e.name.startsWith("./"));
  const mcpDir = hasPrefix ? "./tools/mcp/" : "tools/mcp/";
  const toolsDir = hasPrefix ? "./tools/" : "tools/";
  const fileName = `${mcpDir}${server.name}.yaml`;

  // Remove existing entry with same normalized name if present
  const filtered = entries.filter((e) => normalizeName(e.name) !== normalizeName(fileName));

  // Ensure directory entries exist for ./tools/ and ./tools/mcp/
  const dirNames = new Set(filtered.map((e) => e.name));
  const newEntries: RawTarEntry[] = [];

  if (!dirNames.has(toolsDir) && !dirNames.has(toolsDir.replace(/\/$/, ""))) {
    const dirHeader = buildDirHeader(toolsDir);
    newEntries.push({
      header: dirHeader,
      name: toolsDir,
      typeflag: "5",
      content: new Uint8Array(0),
    });
  }
  if (!dirNames.has(mcpDir) && !dirNames.has(mcpDir.replace(/\/$/, ""))) {
    const dirHeader = buildDirHeader(mcpDir);
    newEntries.push({ header: dirHeader, name: mcpDir, typeflag: "5", content: new Uint8Array(0) });
  }

  // Add the new MCP server file
  const yamlContent = new TextEncoder().encode(buildMcpYaml(server));
  const fileHeader = buildFileHeader(fileName, yamlContent.length);
  newEntries.push({
    header: fileHeader,
    name: fileName,
    typeflag: "0",
    content: yamlContent,
  });

  // Insert new entries before the end (after existing entries)
  const allEntries = [...filtered, ...newEntries];

  // 4. Rebuild tar.gz and upload
  const newTar = rebuildTar(allEntries);
  const newGz = await gzip(newTar);
  await uploadBundle(sessionId, newGz);
}

/**
 * Download the current agent bundle, remove an MCP server YAML file
 * (and any inline declaration), and re-upload via PUT.
 */
export async function removeMcpServerFromSession(
  sessionId: string,
  serverName: string,
): Promise<void> {
  // 1. Download current bundle
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/agent/contents`,
  );
  if (!res.ok) throw new Error(`Failed to download bundle: ${res.status}`);
  const bundleBytes = await res.arrayBuffer();

  // 2. Decompress and parse tar
  const tar = await gunzip(bundleBytes);
  const entries = parseTar(tar);

  // 3. Remove the MCP server file (matching with normalized names)
  const mcpFileName = `tools/mcp/${serverName}.yaml`;
  let removed = false;
  const filtered = entries.filter((e) => {
    if (normalizeName(e.name) === mcpFileName) {
      removed = true;
      return false;
    }
    return true;
  });

  // Also remove inline MCP declarations from config.yaml
  const configIdx = filtered.findIndex((e) => normalizeName(e.name) === "config.yaml");
  if (configIdx >= 0) {
    const configText = new TextDecoder().decode(filtered[configIdx].content);
    const cleaned = removeInlineMcpFromYaml(configText, serverName);
    if (cleaned !== configText) {
      removed = true;
      const newContent = new TextEncoder().encode(cleaned);
      // Build a fresh header with the updated size
      const newHeader = buildFileHeader(filtered[configIdx].name, newContent.length);
      filtered[configIdx] = {
        ...filtered[configIdx],
        header: newHeader,
        content: newContent,
      };
    }
  }

  if (!removed) return; // Nothing changed — skip the PUT

  // 4. Rebuild tar.gz and upload
  const newTar = rebuildTar(filtered);
  const newGz = await gzip(newTar);
  await uploadBundle(sessionId, newGz);
}

/** Upload a rebuilt bundle via PUT. */
async function uploadBundle(sessionId: string, gzBytes: Uint8Array): Promise<void> {
  const form = new FormData();
  form.append(
    "bundle",
    new File([gzBytes.buffer as ArrayBuffer], "agent.tar.gz", { type: "application/gzip" }),
  );
  const putRes = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/agent`, {
    method: "PUT",
    body: form,
  });
  if (!putRes.ok) {
    const text = await putRes.text();
    throw new Error(`Failed to update agent: ${putRes.status} ${text}`);
  }
}

/**
 * Remove an inline MCP server block from config.yaml text.
 * Handles the `tools:` block format where MCP servers are declared as:
 *   tools:
 *     servername:
 *       type: mcp
 *       ...
 */
function removeInlineMcpFromYaml(yaml: string, serverName: string): string {
  const lines = yaml.split("\n");
  const result: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    // Match "  <serverName>:" at the tools indentation level (2 spaces)
    if (line.match(new RegExp(`^  ${escapeRegex(serverName)}:\\s*$`))) {
      // Peek ahead to check if this block contains "type: mcp" anywhere
      // in its immediate children (indented 4+ spaces)
      let j = i + 1;
      let isMcp = false;
      while (j < lines.length && (lines[j].match(/^    \S/) || lines[j].trim() === "")) {
        if (lines[j].trim() === "type: mcp") {
          isMcp = true;
        }
        j++;
      }
      if (isMcp) {
        // Skip the entire block
        i = j;
        continue;
      }
    }
    result.push(line);
    i++;
  }
  return result.join("\n");
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Re-export for tests
export { parseTar, rebuildTar, normalizeName, removeInlineMcpFromYaml, buildMcpYaml };
export type { RawTarEntry };
