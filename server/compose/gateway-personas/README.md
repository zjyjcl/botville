# gateway-personas（只读挂载到网关 `/etc/botville-gateway`）

`make up GATEWAY=1` 时，compose 总是把本目录以只读方式挂到网关容器的
`/etc/botville-gateway`（可用 `.env` 的 `BOTVILLE_GATEWAY_PERSONAS_DIR` 换成别的
宿主机目录）。目录本身随仓库提供，所以没配人设时挂载和启动都不会报错。

- 仓库里只有 `personas.example.json`（虚构示例）和本说明。
- 真实人设放 `personas.json`（或任何别的文件名），**不进仓库**：`.gitignore` 排除了
  本目录下除示例和 README 以外的所有文件。
- 在 `server/compose/.env` 里设 `GATEWAY_PERSONAS_FILE=/etc/botville-gateway/personas.json`，
  再 `make up GATEWAY=1`（容器重建后才读到新值）。留空 = 不加载人设。
- 网关以 uid 10001 运行：Linux 宿主机上文件要对其他用户可读（如 `chmod 0644`），
  或改属主为 10001。人设不是密钥，但也不要放 token / API key。
- 文件格式以 B 线 PR2（角色引擎回复）的契约为准；示例里的字段只作占位。

只有 `GATEWAY_CONSUMER_ENABLED=true`、配了模型（`GATEWAY_MODEL_BASE_URL` /
`GATEWAY_MODEL_NAME`）**并且**配了人设时，AI 才会开口；否则只记录不发言。
详见 [docs/deploy/appservice.md](../../../docs/deploy/appservice.md)。
