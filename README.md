# newera-ai-worker

新启年 AI（https://ai.newera2625.top）的 GitHub Actions 工具机群。

- 每次运行启动 **5 台 Linux + 5 台 Windows**（矩阵任务）
- 每台机器：
  - 用 Playwright/Chromium 执行浏览器操作（打开网页、点击、输入、滚动、截图）
  - 提供临时终端（bash / cmd / powershell），可执行命令
  - 通过 SSH `-R` 把本地 `127.0.0.1:8080` 映射到香港 VPS `127.0.0.1:2500x / 2600x`
  - 主站 ai-center 用 SSH `-L` 连接香港端口，把工具调用转发到对应机器
- 运行时长默认 330 分钟，到期自动退出；网站调度器会每 5 小时 30 分自动续期一批

## 必需的仓库 Secrets

| Secret | 说明 |
| --- | --- |
| `AI_WORKER_TOKEN` | 网站 `AI_WORKER_TOKEN`，工具机接口鉴权 |
| `TUNNEL_SSH_KEY` | 香港 VPS `tunnel` 用户的私钥，仅用于 SSH 端口转发（权限限制到 25xxx/26xxx） |
| `TUNNEL_HOST` | 香港 VPS 地址（不写入代码） |
| `TUNNEL_PORT` | 香港 VPS SSH 端口（不写入代码） |

## 端口约定

| 系统 | 槽位 | 香港端口 |
| --- | --- | --- |
| Linux | 1-5 | 25001-25005 |
| Windows | 1-5 | 26001-26005 |

## 手动触发

管理员在网站「工具机群」面板点击「启动 5 + 5 工具机」，网站调用 GitHub API 触发本 workflow。
也可以在本仓库 Actions 页面手动 Run workflow。

## 安全说明

- 工具机是 GitHub 托管的临时虚拟机，任务结束即销毁。
- 终端命令经过敏感/危险操作过滤，环境变量中的令牌会被清除。
- 隧道用户没有 shell 权限，只能做端口转发。
