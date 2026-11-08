# bili-live-code

B 站直播推流码 WebUI + 无头三栏推流器。用网页完成扫码登录、设置标题分区、开播和下播；`streamer.py` 不依赖 OBS，直接把电脑画面、手机画面和活动回执合成一帧推送到 B 站 RTMP。

> **风险提示**
>
> 本项目通过 B 站网页端接口获取第三方推流码，不是官方直播姬。平台明确第三方开播有粉丝门槛，使用本项目可能绕过该限制并导致账号被限制；请使用**专用小号**，风险自担。
>
> Cookie 等同账号凭据，只应保存在本机 `/data`；不要提交到仓库、网盘或聊天里。

## 功能

- 扫码登录（B 站 passport 二维码），失败时可手动粘贴 Cookie
- 自动/手动获取房间号，设置直播标题与分区
- 调用 `startLive` / `stopLive`，显示 RTMP/SRT 地址与推流码
- 人脸验证：`60024` 显示二维码，`60043` 给出外部验证链接
- WebUI 密码保护、CSRF 校验、Cookie 以 `0600` 保存
- 无头推流：1920×1080、三栏布局，画面来自桌面/手机截图代理
- 推流码超过 4 分钟未使用会自动忽略，避免用旧码反复重试
- 可选活动回执栏（SQLite）和可选 Live2D 透明 PNG 叠加

## 目录结构

```text
app.py                     WebUI / B 站接口封装
templates/                 登录页与操作页
streamer.py                无头推流器（截图、合成、ffmpeg）
Dockerfile                 WebUI 镜像
docker-compose.yml         WebUI 部署
requirements.txt           WebUI 依赖
requirements-streamer.txt  推流器额外依赖（Pillow）
```

## 快速开始（WebUI）

```bash
cp .env.example .env
# 编辑 .env，设置一个足够长的随机密码
docker compose up -d --build
```

打开 `http://<服务器IP>:8090`，输入 `.env` 里的 `BILI_WEBUI_PASSWORD`。

使用流程：

1. 扫码登录 B 站账号（建议专用小号）。
2. 填写或自动获取房间号；设置标题和分区（默认 `624` 虚拟主播）。
3. 点“开始直播”，复制返回的服务器地址和推流码到 OBS，或交给 `streamer.py`。
4. 下播时回到本页点“停止直播”。OBS 停止推流不会关闭直播间。

### 人脸验证

- 返回 `60024`：页面显示二维码，用哔哩哔哩 App 扫码完成验证，再点“继续开播”。
- 返回 `60043`：页面给出 B 站验证链接，在浏览器完成验证后点“继续开播”。

开播请求使用公开的 `pc_link` 参数和签名（`appkey/sign/ts`）。默认静态版本 `7.47.0.10269`，平台要求升级时可覆盖：

```bash
BILI_LIVE_VERSION=7.55.0.10548
BILI_LIVE_BUILD=10548
```

### WebUI 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BILI_WEBUI_PASSWORD` | 无（必填） | WebUI 登录密码；不设置时会自动生成并写入 `/data/webui_password` |
| `BILI_DEFAULT_TITLE` | `我的直播间` | 首次打开时预填的直播标题 |
| `BILI_LIVE_VERSION` / `BILI_LIVE_BUILD` | `7.47.0.10269` / `10269` | 直播姬接口版本号 |
| `BILI_DATA_DIR` | `/data` | Cookie、配置、推流码的存储目录 |
| `PORT` | `8090` | 监听端口 |

## 无头推流器

`streamer.py` 直接运行在 Linux 主机上（不需要 Docker），读取 WebUI 里的推流码，抓取两路截图并合成推流。

### 依赖

```bash
sudo apt install ffmpeg fonts-wqy-microhei
python3 -m pip install -r requirements-streamer.txt
```

### 截图代理接口

推流器不内置桌面/手机控制，只要求两个只读截图接口：

**桌面端** `HER_PC_URL`（默认 `http://127.0.0.1:19990/action`）

```http
POST /action
Authorization: Bearer <token>
Content-Type: application/json

{"action": "screen"}
```

返回 JSON：

```json
{"ok": true, "image": "<base64 JPEG>"}
```

**手机端** `HER_ANDROID_URL`（默认 `http://127.0.0.1:19992/shot`）

```http
POST /shot
Authorization: Bearer <token>
Content-Type: application/json

{}
```

返回 `image/png` 原始字节。

Token 从文件读取，权限建议 `0600`：

- `HER_PC_TOKEN_FILE`，默认 `<STREAM_DATA_DIR>/her-agent.token`
- `ANDROID_TOKEN_FILE`，默认 `<STREAM_DATA_DIR>/android.token`

### 运行

```bash
export BILI_WEBUI_URL=http://127.0.0.1:8090
export BILI_WEBUI_PASSWORD='与 WebUI 相同的密码'
export HER_PC_URL=http://127.0.0.1:19990/action
export HER_ANDROID_URL=http://127.0.0.1:19992/shot
python3 streamer.py
```

也可以在 `<STREAM_DATA_DIR>/webui.env` 写一行 `BILI_WEBUI_PASSWORD=...`，避免在命令行传密码。

只生成一张预览图（不推流）：

```bash
python3 streamer.py --preview
# 输出 <STREAM_DATA_DIR>/preview.jpg
```

### 推流器配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STREAM_DATA_DIR` | `./data` | 日志、预览图、默认 token 目录 |
| `BILI_WEBUI_URL` | `http://127.0.0.1:8090` | WebUI 地址 |
| `BILI_WEBUI_ENV` | `<STREAM_DATA_DIR>/webui.env` | 读取密码的文件（也可直接用环境变量） |
| `HER_PC_URL` | `http://127.0.0.1:19990/action` | 桌面截图接口 |
| `HER_ANDROID_URL` | `http://127.0.0.1:19992/shot` | 手机截图接口 |
| `HER_PC_TOKEN_FILE` | `<STREAM_DATA_DIR>/her-agent.token` | 桌面代理 Bearer token |
| `ANDROID_TOKEN_FILE` | `<STREAM_DATA_DIR>/android.token` | 手机代理 Bearer token |
| `SPACE_DB` | 空 | 活动回执 SQLite 路径；不设置则右栏只显示提示 |
| `STREAM_FPS` | `5` | 合成与推流帧率 |
| `STREAM_STALE_SECONDS` | `240` | 推流码过期阈值（秒） |
| `STREAM_TITLE` | `B站直播 · 三栏画面` | 画面左上角标题 |
| `STREAM_SUBTITLE` | 空 | 画面右下角小字 |
| `STREAM_FONT` | 自动查找 | 中文字体路径 |
| `STREAM_TIMEZONE` | `Asia/Shanghai` | 活动回执时间显示时区 |
| `STREAM_LIVE2D_DIR` | `<STREAM_DATA_DIR>/live2d` | Live2D 透明帧目录；不存在则关闭 |
| `STREAM_LIVE2D_FPS` | `10` | Live2D 序列帧率 |
| `LIVE2D_CREDIT` | 空 | 画面底部署名/版权行 |

推流参数固定为 1920×1080、H.264 3500k、静音 AAC，可在 `StreamSession.start()` 里调整。

### systemd 示例

```ini
[Unit]
Description=bili-live-code headless streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/bili-live-code
EnvironmentFile=/etc/bili-live-streamer.env
ExecStart=/usr/bin/python3 streamer.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

`/etc/bili-live-streamer.env` 里放上表中的变量，权限设为 `0600`。

## 可选：活动回执

设置 `SPACE_DB` 后，右栏会显示任务与事件。数据库需包含：

- `tasks(id, time, status, persona, request_key, text)`
- `events(seq, time, kind, persona, task_id, payload)`

未配置或文件不存在时，右栏显示提示文字，不影响推流。

## 可选：Live2D 叠加

把一段透明 PNG 序列放到 `STREAM_LIVE2D_DIR`，命名为 `frame_0000.png`、`frame_0001.png`……即可在电脑画面右下角叠加循环。建议：

- 300×300、RGBA、10 fps、首尾无缝
- 帧数 × 帧率 = 循环长度
- 用 `LIVE2D_CREDIT` 写明模型作者与授权信息

**本仓库不包含任何 Live2D 模型或预渲染帧。** 模型版权与公开直播授权由使用者自行确认；多数免费模型要求署名，但禁止重新分发模型文件。

## 安全

- 只绑定局域网地址，前面加 HTTPS + 访问认证后再考虑公网。
- Cookie 保存在 `./data/cookies.json`，推流码保存在 `./data/stream.json`，均为 `0600`。
- 不要把 `.env`、`data/`、截图、代理 token 提交到仓库。
- 建议专用小号；下播后可在 WebUI 退出账号。

## 限制

- 能否开播、是否需要人脸验证由 B 站决定，接口变化可能导致失效。
- 画面来自外部截图代理；代理需要自行部署并保证只允许推流器访问。
- 预渲染 Live2D 只有固定循环，没有实时口型同步。

## 致谢

开播/下播字段参考 [ChaceQC/bilibili_live_stream_code](https://github.com/ChaceQC/bilibili_live_stream_code)（Apache-2.0）。本项目只保留无头 HTTP 服务与 WebUI，不包含其桌面 GUI。

## License

[MIT](LICENSE)
