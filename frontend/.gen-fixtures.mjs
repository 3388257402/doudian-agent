// 已停用的一次性 fixtures 生成器。
//
// 历史：这个脚本在 v1.2.0 之前生成 mock fixtures，写出的 node.trace 用 elapsed_ms、
// 没有 data.outcome —— 那是旧口径。契约 §7.6.1 在 v1.2.0 把形状冻结成
// {outcome, duration_ms, input_digest, output_digest, error}，所以这个生成器**不能再跑**：
// 一跑就会把 fixtures 退回不合规的形状，而 mock 模式的演示与自检都依赖新形状。
//
// fixtures 现在是仓库里的正式文件（frontend/mock/*.json），由下面两条命令守住：
//   npm run verify        # 断言 logs/metrics/alerts fixtures 的字段集与 §7.3/§7.5/§7.6.1 同形
//   npm run verify:dom    # 断言三个页面能对着它们渲染
//
// 需要改 fixtures 时直接改 JSON，然后跑上面两条命令；不要"重新生成"。

console.error(
  [
    "这个生成器已停用（一次性脚本，写完即完成使命）。",
    "原因：它产出的是契约 v1.2.0 之前的 node.trace 形状（elapsed_ms、缺 data.outcome）。",
    "改 fixtures 请直接编辑 frontend/mock/*.json，并用 `npm run verify` 校验形状。",
  ].join("\n"),
);
process.exitCode = 1;
