# WeFlow-MaiBot-Bridge

让 Windows 微信个人号接入 [MaiBot](https://github.com/MaiM-with-u/MaiBot) 的独立双向桥接程序。

微信消息由 [WeFlow](https://weflow.top/) 读取并转发给 MaiBot，MaiBot 的回复再通过 Windows UI Automation 发回微信。项目单独运行，**不是 MaiBot 插件，不要放进 MaiBot 的 `plugins` 目录**。

```text
微信 4.x <-> WeFlow <-> 本项目 <-> MaiBot
              读取消息       思考和生成回复
微信 4.x <------- Windows UIA ------- 本项目
                         发送回复
```

## 主要功能

- 微信私聊和群聊双向回复
- 群聊仅 @ 回复、全部回复、多人消息批处理
- 接收图片和动画表情，并把原图交给 MaiBot 识别
- 发送 MaiBot 返回的文字、图片和表情
- 多条连续消息自动合并，减少模型调用次数
- 断线重连、SQLite 持久队列、失败重试和死信记录
- 稳定的用户及群聊 ID，重启后不会因 Python 随机哈希改变
- 微信发送严格串行，避免多条回复抢占窗口和剪贴板

## 开始前确认

本项目目前只适合以下环境：

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 或 Windows 11 |
| Python | 3.12 或 3.13，推荐 3.12 |
| 微信 | 微信桌面版 4.x，已经登录 |
| WeFlow | 已安装、已登录微信数据，并开启本地 API |
| MaiBot | 1.0 系列，已能正常调用模型 |
| 部署位置 | 微信、WeFlow、MaiBot、本项目在同一台 Windows 电脑 |

运行期间必须保持 Windows 用户处于登录状态。锁屏、退出远程桌面、切换用户或同时操作微信，都可能让 UI 自动化发送失败。

建议先使用微信小号和测试群验证，不要一上来就在重要账号或群聊中使用。

## 新手安装教程

下面按顺序操作，不要跳步。

### 第 1 步：安装并配置 MaiBot

先按照 [MaiBot 官方安装文档](https://docs.mai-mai.org/manual/deployment/installation) 安装 MaiBot，并确认 MaiBot 本身可以正常启动、模型配置正确。

打开 MaiBot 的 `config/bot_config.toml`，找到 `[bot]` 和 `[maim_message]`。不要重复创建同名配置节，只修改已有字段：

```toml
[bot]
platform = "weflow"
platforms = ["weflow:机器人微信昵称或wxid"]
nickname = "你的机器人昵称"

[maim_message]
ws_server_host = "127.0.0.1"
ws_server_port = 8000
auth_token = []
```

说明：

- `platform` 必须是 `weflow`，并与本项目配置保持一致。
- `platforms` 中冒号后填写机器人自己的微信昵称或 wxid。
- `nickname` 是 MaiBot 在聊天中使用的名字。
- `ws_server_port` 默认是 `8000`，不是 MaiBot WebUI 的 `8001`。
- `auth_token = []` 表示本机连接不启用 token。如果你的 MaiBot 已配置认证，请同步填写本项目的 `maibot.token`。
- MaiBot 1.0.12 官方使用 `maim-message 0.6.8`。本项目也固定为 `0.6.8`，不要单独升级到 `0.7.x`。

修改后重启 MaiBot。正常情况下，它会在 `127.0.0.1:8000` 提供 maim-message WebSocket 服务。

### 第 2 步：安装并配置 WeFlow

1. 从 [WeFlow 官网](https://weflow.top/) 下载并安装 WeFlow。
2. 启动微信桌面版并登录机器人微信账号。
3. 启动 WeFlow，完成微信数据读取。
4. 在 WeFlow 中开启 HTTP API 服务。
5. 记下 WeFlow 显示的 Access Token。

本项目默认使用：

```text
WeFlow API：http://127.0.0.1:5031
```

如果你的 WeFlow 使用了其他端口，稍后修改本项目配置中的 `weflow.base_url`。

### 第 3 步：下载本项目

安装 Git 后，在 PowerShell 中运行：

```powershell
git clone https://github.com/zhx57/WeFlow-MaiBot-Bridge.git
cd WeFlow-MaiBot-Bridge
```

也可以在 GitHub 页面点击 `Code -> Download ZIP`，解压后进入项目目录。

### 第 4 步：创建 Python 环境

在项目目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
```

如果 PowerShell 提示禁止运行脚本，可以只在当前窗口临时允许：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.venv\Scripts\Activate.ps1
```

### 第 5 步：创建本项目配置

复制配置模板：

```powershell
Copy-Item config.example.toml config.toml
```

用记事本或 VS Code 打开 `config.toml`。第一次只需要重点修改以下内容：

```toml
[weflow]
base_url = "http://127.0.0.1:5031"
access_token = "粘贴你的 WeFlow Access Token"

[maibot]
url = "ws://127.0.0.1:8000/ws"
platform = "weflow"
token = ""

[bridge]
bot_nicknames = ["机器人在微信中显示的昵称"]
bot_wxid = "机器人自己的wxid"
group_mode = "mention"

[uia]
dry_run = false
```

字段解释：

| 字段 | 怎么填 |
|---|---|
| `weflow.access_token` | WeFlow API 页面显示的 Access Token |
| `bridge.bot_nicknames` | 机器人微信昵称，可填写多个，例如 `["麦麦", "小麦"]` |
| `bridge.bot_wxid` | 机器人自己的 wxid；不知道时可暂时留空，但建议填写 |
| `bridge.group_mode` | 新手建议先用 `mention`，避免机器人回复群内所有消息 |
| `uia.dry_run` | Windows 正式发送必须是 `false` |

`config.toml` 已被 `.gitignore` 忽略，不会在正常 Git 提交中上传 Access Token。

### 第 6 步：检查配置

```powershell
python -m weflow_maibot_bridge --config config.toml --check-config
```

看到下面内容说明 TOML 格式正确：

```text
配置有效
```

这一步只检查配置格式，不代表微信、WeFlow 和 MaiBot 已经连接成功。

### 第 7 步：首次启动

按下面顺序启动：

1. 登录微信桌面版。
2. 启动 WeFlow 并确认 API 已开启。
3. 启动 MaiBot。
4. 双击本项目的 `start.bat`。

也可以在已激活虚拟环境的 PowerShell 中运行：

```powershell
python -m weflow_maibot_bridge --config config.toml
```

首次测试建议这样做：

1. 先让一个好友给机器人发一条私聊文字。
2. 确认 MaiBot 控制台收到 `weflow` 平台消息。
3. 确认微信窗口被自动切换，并发出 MaiBot 回复。
4. 再创建测试群，在群里发送 `@机器人昵称 你好`。
5. 最后测试图片和动画表情。

正常启动后应依次看到类似日志：

```text
WeFlow-MaiBot-Bridge 正在启动
正在检查 WeFlow API: http://127.0.0.1:5031
WeFlow API 检查通过
正在连接 WeFlow SSE: http://127.0.0.1:5031/api/v1/push/messages
WeFlow SSE 已连接，等待微信消息
微信 UIA 发送器已就绪
```

收到消息时会立即显示：

```text
收到微信消息 [联系人或群聊] 消息内容
微信消息已进入处理队列
消息已进入 3.0 秒合并缓冲
```

如果微信窗口暂时没启动，会显示“微信 UIA 暂未就绪，不影响接收消息”。此时 WeFlow 消息仍会继续接收；之后产生回复时，发送器会再次尝试寻找微信窗口。

停止程序请在控制台按 `Ctrl+C`。不要直接连续关闭窗口，正常停止会尽量排空持久队列。

## 群聊模式

在 `config.toml` 中修改：

```toml
[bridge]
group_mode = "mention"
```

可选值：

| 模式 | 行为 | 适合场景 |
|---|---|---|
| `mention` | 只有明确 @ 机器人时才处理 | 推荐，新手和普通群聊 |
| `all` | 群内每个人的消息都交给 MaiBot | 小型测试群 |
| `batch` | 同一群短时间内的多人消息合并处理 | 活跃群、降低模型调用次数 |

`mention` 模式下，如果成员先发送图片，再在 15 秒内发送 @ 机器人的文字，项目会尝试把图片和文字关联起来。

修改 `config.toml` 后需要重启本项目。

## 图片识别

默认配置是：

```toml
[caption]
provider = "off"
```

这不是关闭图片功能。图片仍会以 Base64 发送给 MaiBot，由 MaiBot 配置的视觉模型处理。通常推荐保持 `off`，避免图片被识别两次。

如果你的 MaiBot 没有视觉模型，可以让本项目先把图片转换成描述。

使用本机 Ollama：

```toml
[caption]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "llava"
prompt = "请用中文简短描述这张图片。"
```

使用 OpenAI 兼容接口：

```toml
[caption]
provider = "openai"
base_url = "https://你的接口地址/v1"
api_key = "你的 API Key"
model = "支持视觉的模型名"
```

图片描述失败不会丢弃原图，项目仍会把图片发送给 MaiBot。

## 完整配置说明

配置模板位于 `config.example.toml`。

### `[weflow]`

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `base_url` | `http://127.0.0.1:5031` | WeFlow API 地址 |
| `access_token` | 无 | WeFlow Access Token |
| `connect_timeout` | `5.0` | HTTP 建连超时秒数 |
| `read_timeout` | `45.0` | SSE 读取超时秒数 |
| `retry_min_seconds` | `1.0` | 断线后最短重连等待 |
| `retry_max_seconds` | `30.0` | 断线后最长重连等待 |

也可通过环境变量提供 Token，而不写入文件：

```powershell
$env:WEFLOW_ACCESS_TOKEN = "实际 Token"
```

### `[maibot]`

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `url` | `ws://127.0.0.1:8000/ws` | MaiBot maim-message 地址 |
| `platform` | `weflow` | 平台名，必须和 MaiBot 一致 |
| `token` | 空 | MaiBot WebSocket 认证 Token |
| `reconnect_max_seconds` | `30.0` | Router 重建最大等待时间 |

### `[bridge]`

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `bot_nicknames` | 无 | 机器人微信昵称列表 |
| `bot_wxid` | 空 | 机器人自己的 wxid，用于防止回复循环 |
| `group_mode` | `mention` | 群聊处理模式 |
| `debounce_seconds` | `3.0` | 连续消息合并等待时间 |
| `queue_size` | `1000` | 持久队列上限 |
| `media_concurrency` | `4` | 同时下载/处理图片的上限 |
| `max_attempts` | `10` | 可确认尚未投递时的最大重试次数 |

### `[media]`

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `directory` | `data/media` | 临时媒体目录 |
| `max_bytes` | `10485760` | 单张图片最大 10 MiB |
| `download_timeout` | `20.0` | 图片下载超时秒数 |
| `max_redirects` | `3` | 远程图片最大重定向次数 |
| `local_roots` | `[]` | 可选的本地图片目录限制；本机默认空列表表示不限制 |

本机使用默认不限制 MaiBot 返回的本地图片路径。如果希望只允许指定目录，可填写 `local_roots`：

```toml
[media]
local_roots = ["C:/MaiBot/data", "C:/MaiBot/temp"]
```

路径建议使用 `/`，避免 TOML 中 Windows 反斜杠转义问题。

### `[uia]`

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `dry_run` | `false` | 为 `true` 时只记录发送，不操作微信 |
| `search_enabled` | `true` | 发送前自动搜索联系人或群聊 |
| `window_titles` | `微信, WeChat` | 可识别的微信窗口标题 |
| `operation_timeout` | `20.0` | 单次 UIA 操作超时秒数 |

### `[storage]`

`database = "data/bridge.sqlite3"` 是本地持久数据库。它保存待发送消息、失败记录和微信会话映射。不要在程序运行时删除它。

## 常见问题

### 提示“配置文件不存在”

确认项目根目录存在 `config.toml`：

```powershell
Copy-Item config.example.toml config.toml
```

### 提示“No module named weflow_maibot_bridge”

这表示项目没有安装进 `.venv`，常见原因是创建虚拟环境后漏掉了 `pip install -e .`。在项目目录运行：

```powershell
.venv\Scripts\python.exe -m pip install -e .
```

最新版 `start.bat` 会自动设置源码路径，并在依赖缺失时尝试执行上面的安装命令。通过 ZIP 下载旧版本的用户，请重新下载最新版或执行 `git pull`。

不要在 `C:\Windows\System32` 中手动执行项目命令。先进入项目目录：

```powershell
cd "C:\Program Files\WeFlow-MaiBot-Bridge"
```

项目位于 `Program Files` 时，安装或更新可能遇到权限问题。更省事的安装位置是当前用户目录，例如：

```text
C:\Users\你的用户名\WeFlow-MaiBot-Bridge
```

### 提示“weflow.access_token 为空”

在 `config.toml` 填写 Token，或设置环境变量 `WEFLOW_ACCESS_TOKEN`。

### 提示“UIA 发送仅支持 Windows”

项目只能在 Windows 上正式发送微信消息。Linux、Docker、WSL 和 Termux 不能直接控制 Windows 微信窗口。

### 提示“未找到微信 4.x 主窗口”

检查：

- 微信桌面版已经启动并登录。
- 微信窗口没有运行在另一个 Windows 用户会话中。
- 微信、本项目使用相同权限启动，不要一个管理员运行、一个普通运行。
- 将微信主窗口打开到桌面后再启动本项目。

### MaiBot 连不上，出现 404 或 Router 未连接

检查连接的是：

```text
ws://127.0.0.1:8000/ws
```

`8000` 是 `[maim_message].ws_server_port`，不是 MaiBot WebUI 的 `8001`。修改 `bot_config.toml` 后必须重启 MaiBot。

如果日志不断出现 HTTP `404`，检查桥接虚拟环境中的 maim-message 版本：

```powershell
.venv\Scripts\python.exe -c "import maim_message; print(maim_message.__version__)"
```

MaiBot 1.0.12 对应的正确版本是：

```text
0.6.8
```

如果显示 `0.7.x`，执行：

```powershell
.venv\Scripts\python.exe -m pip install --force-reinstall "maim-message==0.6.8"
.venv\Scripts\python.exe -m pip install -e .
```

原因是 `0.6.8` 使用原生 WebSocket `/ws`，而 `0.7.x` 改成了 Socket.IO。Socket.IO 会先发 HTTP polling 请求，连接 MaiBot 1.0.12 的原生 WebSocket 服务时必然得到 404。单纯把 URL 改成 `http://`、删除 `/ws` 或改成 `/socket.io` 都不能解决。

### MaiBot 收到消息但不回复

检查：

- MaiBot 的模型服务是否正常。
- `[bot].platform` 是否为 `weflow`。
- `[bot].platforms` 是否包含当前微信机器人账号。
- MaiBot 的聊天频率配置是否允许回复。
- 群聊使用 `mention` 时，是否真的 @ 了 `bot_nicknames` 中的昵称。

### 能收到微信消息，但回复没有发出去

检查微信是否锁屏、最小化到异常状态、被其他窗口抢占，以及 PowerShell 是否被企业策略禁用。运行期间不要同时操作微信和剪贴板。

### 图片能收到但识别失败

确认 MaiBot 配置了视觉模型，并检查 `[visual]` 配置。也可以临时启用本项目的 Ollama/OpenAI caption。

### 如何查看失败消息

失败记录保存在 `data/bridge.sqlite3` 的 `dead_letters` 表中。项目不会自动重试已经粘贴或按下 Enter、结果无法确认的微信发送，以免造成重复消息。

### 可以同时启动两个 Bridge 吗

不可以。同一个数据库只允许一个实例运行。第二个实例会提示：

```text
另一个 Bridge 实例正在使用同一数据库
```

## 更新项目

如果是通过 Git 克隆：

```powershell
cd C:\path\to\WeFlow-MaiBot-Bridge
git pull
.venv\Scripts\Activate.ps1
pip install -e .
```

更新不会覆盖 `config.toml` 和 `data/bridge.sqlite3`。升级前仍建议备份这两个文件。

## 开发和测试

```powershell
pip install -e ".[test]"
pytest
python -m compileall -q src tests
```

测试不要求微信、WeFlow 或 MaiBot 在线，覆盖配置、消息规范化、群策略、SSE、消息缓冲、SQLite 队列、图片校验、回复路由和单实例锁。

## 实现说明

微信侧功能参考 [Akasha-WeChat v1.0.1](https://github.com/alingalingling/Akasha-WeChat/releases/tag/v1.0.1)，MaiBot 通信使用与 MaiBot 1.0.12 官方依赖一致的 `maim_message==0.6.8`，通过原生 WebSocket `Router` 和 `MessageBase` 交互。本项目是独立实现，不复用 Akasha-WeChat 的 OneBot/AstrBot 层，也不复用其他已有微信适配器。

数据流：

```text
WeFlow SSE
  -> 原始事件写入 SQLite
  -> 私聊/群聊规范化与过滤
  -> 图片下载和消息合并
  -> 持久 outbox
  -> maim-message Router
  -> MaiBot

MaiBot 回复
  -> 持久微信出站队列
  -> 展开 text/image/emoji/seglist
  -> 专用 COM/UIA 线程串行执行
  -> 微信
```

需要理解的限制：

- UI 自动化只能确认本机操作是否执行，不能获得微信服务器的最终送达回执。
- WeFlow 或微信更新后可能改变消息字段、媒体接口、窗口标题或快捷键，需要重新验证。
- 微信 UI 搜索最终依赖显示名；稳定 ID 无法解决两个联系人或群完全同名的问题。
- 锁屏、远程桌面断开、输入法、企业安全软件等 Windows 环境差异无法通过纯代码完全消除。
- `maim_message 0.6.8` 的发送成功表示底层发送调用成功，不是 MaiBot 业务处理完成的 ACK。

## 项目结构

```text
WeFlow-MaiBot-Bridge/
├── config.example.toml       配置模板
├── start.bat                 Windows 启动脚本
├── pyproject.toml            Python 项目和依赖
├── src/weflow_maibot_bridge/
│   ├── app.py                生命周期和双向编排
│   ├── buffer.py             消息合并
│   ├── caption.py            可选图片描述
│   ├── config.py             TOML 配置验证
│   ├── media.py              图片读取、校验和下载
│   ├── messages.py           MaiBot 消息构造和回复路由
│   ├── models.py             WeFlow 消息规范化和群策略
│   ├── outbound.py           MaiBot 回复消息段解析
│   ├── process_lock.py       单实例锁
│   ├── router.py             maim-message Router 生命周期
│   ├── sse.py                SSE 解析器
│   ├── storage.py            SQLite 队列、映射和死信
│   ├── uia.py                Windows UIA 发送线程
│   └── weflow.py             WeFlow SSE 和媒体客户端
└── tests/                    离线自动化测试
```

## 许可证与致谢

本项目使用 [MIT License](LICENSE)。

感谢：

- [MaiBot](https://github.com/MaiM-with-u/MaiBot)
- [WeFlow](https://github.com/hicccc77/WeFlow)
- [Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat)
