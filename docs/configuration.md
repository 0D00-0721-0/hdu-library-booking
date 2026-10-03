# 配置与登录态

[返回首页](../README.md)

## 自动获取 Cookie（推荐）

先复制示例为 `config.yaml`，再在项目虚拟环境中运行：

```bash
python -m pip install -r requirements-login.txt
python instant_book.py --login
```

也可以在本机网页点击“登录并获取 Cookie”。工具优先打开已安装的 Chrome；找不到时使用 Playwright 的 Chromium。没有可用浏览器时运行 `python -m playwright install chromium`，Linux 桌面缺少系统依赖时可使用 `python -m playwright install --with-deps chromium`。浏览器支持见 [Playwright 官方说明](https://playwright.dev/python/docs/browsers)。

在新窗口完成官方登录和验证码，等待控制台提示“登录成功，Cookie 已保存，配置已更新”。工具每 5 秒最多发起一轮只读登录校验，只有查询接口确认已登录才保存。不会自动预约，也不替你输入密码或完成验证码。

- 最长等待 5 分钟；网页“停止任务”、命令行 `Ctrl+C` 或关闭登录窗口可以结束等待。正在进行的校验请求可能需要几秒退出。
- 使用独立、非持久浏览器会话，不读取日常浏览器的个人资料。只导出影响图书馆域名的 Cookie，包括 HttpOnly Cookie；学校统一认证网站等其他域名的 Cookie 不保存。实现依据 [BrowserContext.cookies](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-cookies)。
- Cookie 写入配置文件旁的 `cookies/session-随机编号.json`，配置中的 `auth.cookie_file` 更新为该文件绝对路径。POSIX 系统下文件权限为 `600`，新建目录为 `700`。
- 成功后清空旧的内联 `auth.cookie`、`user_info.uid` 和 `name`，后续请求从当前登录态重新识别用户。预约计划保持不变；`auth`、`user_info` 区块会重写。
- 新文件写完后才切换配置引用。失败、取消或配置保存失败时保留旧配置；原 Cookie 文件保留在本机，确认不再需要后可自行清理。
- 移动项目目录后重新获取一次 Cookie，或手动调整绝对路径。`cookies/`、配置与日志不应提交 Git。
- 自动登录仅支持示例中的官方房间和座位查询接口。已有预约任务运行时，先等待其结束或停止任务，再更新登录态。

## 手动准备登录态

先在浏览器登录 [HDU 慧图系统](https://hdu.huitu.zhishulib.com/)，确认官方页面能读取你的预约信息。下面两种方式任选一种：

- **Cookie JSON 文件**：将该域名的 Cookie 保存为 `cookies/session.json`，保留模板中的 `auth.cookie_file`。支持 Cookie 数组，或包含 `cookies` 数组的对象；每项需有 `name`、`value`，可带 `domain`、`path`、`secure`。
- **Cookie 请求头**：在浏览器开发者工具的 Network 面板刷新官方页面，选择发往 `hdu.huitu.zhishulib.com` 的已登录请求，从 Headers 的 Request Headers 复制 `Cookie` 值。将其填入 `auth.cookie`，同时把 `auth.cookie_file` 设为空字符串。Chrome 操作可参考 [官方 Network 文档](https://developer.chrome.com/docs/devtools/network/reference#headers)。

Cookie JSON 的结构如下。这里的名称和值只是占位符，不能登录：

```json
{
  "cookies": [
    {
      "name": "REPLACE_WITH_COOKIE_NAME",
      "value": "REPLACE_WITH_COOKIE_VALUE",
      "domain": "hdu.huitu.zhishulib.com",
      "path": "/",
      "secure": true
    }
  ]
}
```

请求头方式对应的 YAML 配置：

```yaml
auth:
  cookie_file: ""
  cookie: "在此粘贴你自己的完整 Cookie 值"
```

`user_info.uid` 和 `name` 默认留空，工具会尝试自动识别。若提示未识别 UID，它指慧图内部用户 ID，不能直接用学号替代。Cookie 过期后需要重新登录并更新；不要将 Cookie、配置或浏览器导出文件发到 Issue。

## 配置字段

完整字段及默认值见 [config.example.yaml](../config.example.yaml)。修改配置后可再次运行 `--check-config`。

| 字段 | 用途 |
| --- | --- |
| `auth.cookie_file` / `auth.cookie` | Cookie 文件或请求头，通常选一种 |
| `session.verify` | HTTPS 证书验证，默认 `true` |
| `session.trust_env` | 是否使用环境中的代理配置，默认 `false` |
| `request.timeout` | 单次请求超时，单位秒 |
| `request.keepalive_interval` | 定时等待时的心跳间隔；`0` 关闭 |
| `booking.plan` | 房间类型、楼层 ID、座位号、开始小时、时长 |
| `booking.book_days` | 日期偏移：`0` 今天、`1` 明天、`2` 后天 |
| `booking.execute_at` | 发送时间，可带毫秒；空字符串表示立即 |
| `booking.fallback_seats` | 备选座位号，逗号分隔，最多 5 个 |
| `booking.max_trials` / `retry_delay` | 有界重试次数与响应后的等待秒数 |
| `booking.hold_before_minutes` | 提前锁座分钟数，`0` 关闭 |
| `booking.dry_run` | `true` 只查询，`false` 允许真实预约 |

布尔值使用不带引号的 `true` / `false`；时间字符串带引号。Cookie 文件的相对路径从当前工作目录解析，因此运行前请进入项目目录。自定义配置文件可使用 `--config /path/to/config.yaml`。

执行时间和预约日期使用电脑的本地时区。预约 HDU 座位时，请确认系统时区为 `Asia/Shanghai`，并保持系统时间准确。

## 参数范围与检查规则

离线检查、命令行预约参数和网页表单共用预约参数校验。配置错误会在创建预约客户端或后台任务之前返回；`--check-config` 不联网。

| 参数 | 接受的值 |
| --- | --- |
| `booking.max_trials` | 1–20 的整数；越界会报错 |
| `booking.retry_delay` | 有限数字；小于 3 秒按 3 秒，大于 10 秒按 10 秒 |
| `booking.hold_before_minutes` | 0–14 的整数；大于 0 时必须设置执行时间 |
| `booking.book_days` | 0、1、2 的整数 |
| `booking.execute_at` | 时间字符串或空值；格式为 `HH:MM`、`HH:MM:SS` 或 `HH:MM:SS.sss` |
| `request.timeout` | 至少 0.001 秒的有限数字，支持小数 |
| `request.keepalive_interval` | 非负有限数字；0 关闭，正数至少按 10 秒执行 |
| 布尔字段 | YAML 的 `true` / `false`；字符串、数字不能替代布尔值 |

整数参数接受数字字符串，例如 `"2"`，不接受 `true` 或 `2.5`。`NaN`、无穷大、非法文本会被拒绝。重试间隔保持原有 3–10 秒规范化规则。

配置模板默认 `dry_run: true`。需要真实预约时，显式设置为 `false`；网页请求必须明确携带布尔类型的 `dry_run`。取消、签到、续座是独立操作，不受预约 dry-run 开关控制。

## plan 格式

`roomType:floorId:seatNum:startHour:durationHours`

- `roomType`：接口返回的房间类型顺序，通常 `1` 是自习室。
- `floorId`：楼层/区域 id，例如六楼杭韵数阁是 `1559`。
    杭韵数阁（六楼） = 1559
    宋韵云图（四楼） = 1558
    格物E堂（二楼东） = 1557
    数智渊阁（二楼 信息检索室） = 1554
    芯灵驿站（十二楼） = 1543
    比特庭园（二楼西） = 1524
- `seatNum`：座位号，例如 `130`。
- `startHour`：开始小时，例如 `8` 表示 08:00。
- `durationHours`：预约时长，单位小时。

这个工具只在配置的小窗口内做有限重试，不会持续高频请求。
