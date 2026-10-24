// 契约 §12 快照的读写逻辑（被 scripts/verify.mjs 与 scripts/sync-contract-snapshot.mjs 共用）。
//
// 为什么要有这一层：验收门以前直接 diff **活的** docs/CONTRACT.md §12 与 types.ts，
// 于是任何契约文档编辑（哪怕只是往代码块里插一行中文注释）都会让前端验收变红。
// 那是把别人的写权限偶合进我的验收里 —— 文档编辑变成构建失败，是最不该有的耦合。
//
// 现在的三层判定（性质分开，互不掩盖）：
//   ① 快照自身完整：快照里记录的 sha256 == 快照正文实际 sha256（防止有人手改快照却没更新指纹）
//   ② 类型漂移门（硬门）：types.ts 正文 == 快照正文 → 漂移必红，并给出行级 diff（这是我自己文件的问题，diff 有用）
//   ③ 契约变更检测：契约当前 §12 正文 == 快照正文 → 不一致时给"可执行"的处置步骤（不是行级 diff，
//      因为那不是我的错，而是一次需要走变更流程的事件）
//
// 归一化规则（决定"什么算变更"）：剥掉注释行 / 空行 / 行首缩进，只留有效类型行。
// 因此：往 §12 里加注释 = 不算变更（绿）；改任何一个类型字面量 = 变更（红）。

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

export const SNAPSHOT_MARKER_SHA = "section12_sha256:";
export const SNAPSHOT_MARKER_VERSION = "contract_version:";

/** 抽取 CONTRACT.md 里 §12 的 ```ts 代码块正文；找不到返回 null。 */
export function extractSection12(contractText) {
  const heading = contractText.indexOf("## 12. 前端 TS 类型");
  if (heading < 0) return null;
  const fenceStart = contractText.indexOf("```ts", heading);
  if (fenceStart < 0) return null;
  const fenceEnd = contractText.indexOf("```", fenceStart + 5);
  if (fenceEnd < 0) return null;
  return contractText.slice(fenceStart + 5, fenceEnd);
}

/** 抽取契约版本号（`- 版本：**v1.2.0**（冻结）` 那一行）；找不到返回 "unknown"。 */
export function extractContractVersion(contractText) {
  const match = /-\s*版本：\*\*([^*]+)\*\*/.exec(contractText);
  return match?.[1]?.trim() ?? "unknown";
}

/** 归一化：去注释、去空行、去行首行尾空白 —— 只留"有效类型行"。 */
export function normalizeTypes(source) {
  const lines = [];
  let inBlockComment = false;
  for (const rawLine of source.split(/\r?\n/)) {
    let line = rawLine.replace(/[ \t]+$/g, "");
    if (inBlockComment) {
      const end = line.indexOf("*/");
      if (end < 0) continue;
      line = line.slice(end + 2);
      inBlockComment = false;
    }
    let trimmed = line.trim();
    if (trimmed === "") continue;
    if (trimmed.startsWith("//")) continue;
    if (trimmed.startsWith("/*")) {
      const end = trimmed.indexOf("*/");
      if (end < 0) {
        inBlockComment = true;
        continue;
      }
      trimmed = trimmed.slice(end + 2).trim();
      if (trimmed === "") continue;
    }
    // JSDoc / 块注释的续行（` * ...`）
    if (trimmed.startsWith("*")) continue;
    lines.push(trimmed);
  }
  return lines.join("\n");
}

export function sha256(text) {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

/** 读快照文件：返回 { body, recordedSha, recordedVersion, raw }。 */
export function readSnapshot(snapshotPath) {
  const raw = readFileSync(snapshotPath, "utf8");
  const recordedSha = new RegExp(`${SNAPSHOT_MARKER_SHA}\\s*([0-9a-f]{64})`).exec(raw)?.[1] ?? null;
  const recordedVersion = new RegExp(`${SNAPSHOT_MARKER_VERSION}\\s*(\\S+)`).exec(raw)?.[1] ?? null;
  return { raw, body: normalizeTypes(raw), recordedSha, recordedVersion };
}

/** 行级 diff（只在"自己文件 vs 快照"时用；契约变更不甩 diff）。 */
export function diffLines(expected, actual, limit = 5) {
  const expectedLines = expected.split("\n");
  const actualLines = actual.split("\n");
  const diffs = [];
  for (let i = 0; i < Math.max(expectedLines.length, actualLines.length); i += 1) {
    if (expectedLines[i] !== actualLines[i]) {
      diffs.push(`第 ${i + 1} 行：快照=${JSON.stringify(expectedLines[i] ?? "<缺行>")} 实现=${JSON.stringify(actualLines[i] ?? "<缺行>")}`);
      if (diffs.length >= limit) break;
    }
  }
  return diffs.join(" | ");
}
