// 契约自检的驱动：用 esbuild 把 TypeScript 自检套件打成 ESM，再在 Node 里跑。
//
//   node scripts/verify.mjs        # 全绿退出码 0，任一失败退出码 1
//
// 为什么不引 vitest/jest：这套断言只依赖字符串/深比较，多装一个测试框架换来的是
// 又一份依赖树和一套配置；而它要证明的东西（契约字段逐字对齐）与框架无关。
//
// 第一步是"逐字比对"：把 CONTRACT §12 的 ```ts 代码块抽出来，和 src/api/types.ts 逐行 diff。
// 契约是单一事实源，types.ts 必须等于它 —— 不多、不少、不重排。

import { build } from "esbuild";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const root = resolve(import.meta.dirname, "..");
const contractPath = resolve(root, "../docs/CONTRACT.md");
const typesPath = resolve(root, "src/api/types.ts");
const outfile = resolve(root, ".verify/verify-suite.mjs");

mkdirSync(dirname(outfile), { recursive: true });

/** 从 CONTRACT.md 抽出 §12 的 ```ts 代码块。 */
function contractSection12() {
  const text = readFileSync(contractPath, "utf8");
  const heading = text.indexOf("## 12. 前端 TS 类型");
  if (heading < 0) return null;
  const fenceStart = text.indexOf("```ts", heading);
  if (fenceStart < 0) return null;
  const fenceEnd = text.indexOf("```", fenceStart + 5);
  if (fenceEnd < 0) return null;
  return text.slice(fenceStart + 5, fenceEnd);
}

/** 去掉文件头部的 `//` 说明行与首尾空行，只留类型正文。 */
function typeBody(source) {
  const lines = source.split(/\r?\n/);
  let index = 0;
  while (index < lines.length && (lines[index].trim() === "" || lines[index].trimStart().startsWith("//"))) index += 1;
  return lines
    .slice(index)
    .join("\n")
    .replace(/[ \t]+$/gm, "")
    .replace(/\n+$/, "");
}

function checkTypesVerbatim() {
  const contract = contractSection12();
  if (contract === null) {
    return { name: "§12 types.ts 逐字等于契约", ok: false, detail: "CONTRACT.md 里找不到 §12 的 ts 代码块" };
  }
  let actual;
  try {
    actual = typeBody(readFileSync(typesPath, "utf8"));
  } catch (err) {
    return { name: "§12 types.ts 逐字等于契约", ok: false, detail: `读不到 ${typesPath}：${String(err)}` };
  }
  const expected = typeBody(contract);
  if (expected === actual) return { name: "§12 types.ts 逐字等于契约", ok: true, detail: "" };

  const expectedLines = expected.split("\n");
  const actualLines = actual.split("\n");
  const diffs = [];
  for (let i = 0; i < Math.max(expectedLines.length, actualLines.length); i += 1) {
    if (expectedLines[i] !== actualLines[i]) {
      diffs.push(
        `第 ${i + 1} 行：契约=${JSON.stringify(expectedLines[i] ?? "<缺行>")} 实现=${JSON.stringify(actualLines[i] ?? "<缺行>")}`,
      );
      if (diffs.length >= 5) break;
    }
  }
  return { name: "§12 types.ts 逐字等于契约", ok: false, detail: diffs.join(" | ") };
}

await build({
  entryPoints: [resolve(root, "scripts/verify-suite.ts")],
  outfile,
  bundle: true,
  format: "esm",
  platform: "node",
  target: "node20",
  logLevel: "warning",
});

const module_ = await import(`${pathToFileURL(outfile).href}?v=${Date.now()}`);
const results = [checkTypesVerbatim(), ...(await module_.run())];

let failed = 0;
for (const result of results) {
  if (result.ok) {
    console.log(`PASS  ${result.name}`);
  } else {
    failed += 1;
    console.log(`FAIL  ${result.name}`);
    console.log(`      ${result.detail}`);
  }
}

console.log("");
console.log(`${results.length - failed}/${results.length} 通过`);
if (failed > 0) process.exit(1);
