# gateway/ — AI 网关与角色引擎（B 线，Claude Code）

- 网关契约与 OpenAPI 初稿、完整接入 SDK、简单接入角色引擎
- 三层限额与 token 预算、防刷屏与 AI 互聊熔断、不可信内容隔离（防注入）
- 心情、需求驱动、主动发言；四层记忆；默认性格人设模板
- AI 关系策略与多 AI 群发言调度
- 通知多口吻改写；中枢合并/静默/续办逻辑
- 小镇行为：规则作息、社交判断、事件联动、素材生成管线

## 目录 / Layout

| 路径 | 内容 |
|---|---|
| `openapi/gateway.v1.yaml` | 网关契约（OpenAPI 3.1，唯一事实源） |
| `openapi/examples/` | 18 个请求/响应示例，全部对 schema 校验 |
| `docs/contract.md` | 人类可读契约说明：功能清单规范、卡片描述规范、版本规则、评审清单 |
| `scripts/validate_openapi.py` | 离线校验脚本（文档结构 + 示例 schema） |

## 契约校验 / Validate the contract

任务 B0.1 的验收方式：C/D 线评审前、以及每次改契约后，在**仓库根目录**运行：

```bash
python3 gateway/scripts/validate_openapi.py
```

需要 Python 3.9+ 与 PyYAML（`python3 -m pip install --user pyyaml`）。
脚本完全离线，不需要网络、不需要 Node。

退出码 `0` = 全部通过；`1` = 有检查失败（逐条打印原因）；`2` = 环境或路径问题。

它检查什么：

1. **文档结构** — OpenAPI 3.1、`info.license` 为 Apache-2.0、`servers` 在 `/v1` 下、
   bearer 占位鉴权、全部 `$ref` 可解析、operationId 唯一、每个操作有 summary/description/tag、
   路径参数已声明且 `required: true`、错误响应统一 `application/problem+json`
   （RFC 9457）、YAML 无重复键。
2. **示例** — `openapi/examples/*.json` 逐个对契约里声明的 schema 校验
   （映射表在脚本的 `EXAMPLE_TARGETS`，指向契约本身，所以 schema 一改示例就会暴露问题）；
   每个示例文件必须有映射，每个映射必须有文件。
3. **占位扫描** — 疑似真实密钥（`sk-…`、`AKIA…`、私钥块、长字面量密码等）即失败。
   所有示例只允许 `<change-me>` 与 `example.invalid`。

常用变体：

```bash
# 指定别的契约文件
python3 gateway/scripts/validate_openapi.py --contract gateway/openapi/gateway.v1.yaml

# 可选：外部 lint（需要网络，非 CI 必需）
npx --yes @redocly/cli lint gateway/openapi/gateway.v1.yaml
```

示例：

```
[1/3] OpenAPI document structure
  PASS  all $refs resolve (293 found)
  PASS  path operations are well formed (26 ops, unique operationIds)
[2/3] Example payloads against contract schemas
  PASS  card.json matches GET /cards/{card_id} 200
  ...
[3/3] Summary
  All 29 checks passed. Contract is consistent with its examples.
```

## 约定 / Conventions

- 只增不删、破坏性变更换主版本，详见 [`docs/contract.md#版本规则`](docs/contract.md#5-版本规则)。
- 发言被限额拦截是 `200` + `outcome: "blocked"`，不是 `429`/`403`
  （见 [`docs/contract.md#14-️-策略结论不是-http-错误`](docs/contract.md#14-️-策略结论不是-http-错误)）。
- 不写任何真实密钥、token、密码，只用 `<change-me>` 占位（见 `CONTRIBUTING.md`）。
- 新文件加 Apache-2.0 SPDX 头。
