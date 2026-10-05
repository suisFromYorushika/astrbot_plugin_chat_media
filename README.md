# astrbot_plugin_chat_media

> 给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 用的**群聊历史与媒体助手**：让机器人按**日期 / 时间点 / 谁发的 / 关键词**检索本群历史，并把指定的历史图片 / 视频 / 语音 / 文件发回群里；同时把新消息的媒体自动落盘。
>
> 不需要你事先"归档"过任何东西——从装上那一刻起，它就开始替你留住群里的图；之前的记录它能查多少算多少。
>
> 数据来源是 [NapCat](https://github.com/NapNeko/NapCatQQ) 等 OneBot v11 协议端接入的真实 QQ 群消息——文本由 AstrBot 自己持久化，媒体由本插件补齐。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A5%204.28-4c8bf5)](https://github.com/AstrBotDevs/AstrBot)
[![OneBot v11](https://img.shields.io/badge/OneBot-v11-1f6feb)](https://github.com/botuniverse/onebot-11)
[![NapCat](https://img.shields.io/badge/NapCat-compatible-0aa344)](https://github.com/NapNeko/NapCatQQ)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 它解决什么问题

AstrBot 的群消息持久化（`platform_message_history`）**只存文本**——图片、视频、语音、文件会被替换成 `[Image]` 这样的占位符，而且媒体的临时链接很快过期。结果是：

- 机器人**看不到**历史图片（只能看到「[图片]」三个字）
- 你想让它「把上周三那张图发出来」时，**没有路径可用**——你也不该去记路径

本插件补上这两块。

## 功能

### 1. 给模型的三个工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `query_group_history` | `date` / `clock` / `sender` / `keyword` / `limit` | 按条件检索本群历史消息（含机器人进群之前的） |
| `list_history_media` | `date` / `sender` / `keyword` / `limit` | 列出带图片/视频/语音/文件的历史消息 |
| `send_history_media` | `date` / `clock` / `sender` / `keyword` / `kind` / `index` | **把定位到的历史媒体发到当前群**；`kind` 可选 `image` / `video` / `voice` / `file` / `any` |

- `date` 支持 `2024-03-17`（某天）和 `2024-03`（某月）
- `clock` 支持 `18:24`（精确到分钟，前后各 90 秒）
- `sender` 填群名片或 QQ 号

用户直接说人话就行：

> 「2024 年 3 月 17 号谁发过图」
> 「把 2024-03-17 18:24 那张图发出来」
> 「把 2025-04-19 那个视频发出来」
> 「2024 年 3 月你们聊了什么」

### 2. 手打指令

- `/群史 2024-03-17`、`/群史 2024-03 kumo`
- `/发图 2024-03-17`、`/发图 2024-03-17 18:24 kumo`

### 3. 新消息媒体自动落盘

监听所有消息，把 `Image / Record / Video / File` 段落存到

```
<media_dir>/<YYYY-MM-DD>/
```

并把本地路径**补写回**那条历史记录的 `content._media` 字段。本地路径直接复制；`http(s)` 链接会下载。
建议在 [NapCat](https://github.com/NapNeko/NapCatQQ) 里把 **「启用本地文件到URL」** 打开，这样给的是本地路径而不是会过期的 CDN 链接。

## 安装

1. 把本目录放进 AstrBot 的插件目录：

   ```
   <AstrBot>/data/plugins/astrbot_plugin_chat_media/
   ```

2. 重启 AstrBot（或在 WebUI 插件页点重载）。
3. 在 WebUI → 插件管理里确认 `astrbot_plugin_chat_media` 已加载。

依赖：仅 Python 标准库；下载远程媒体时用 AstrBot 自带的 `aiohttp`。**无第三方依赖。**

## 配置

WebUI → 插件管理 → qq_archive：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `db_path` | `/AstrBot/data/data_v4.db` | AstrBot 数据库路径 |
| `archive_root` | `/AstrBot/data/qqarchive` | 历史媒体根目录（`_media` 里的相对路径以此为基准） |
| `media_archive` | `true` | 是否启用新消息媒体落盘 |
| `media_dir` | `/AstrBot/data/qqarchive/media_new` | 新媒体落盘目录 |
| `max_scan_rows` | `20000` | 单次检索最多扫描的历史行数 |
| `reply_on_send` | `true` | 发图时是否带一句「谁在什么时候发的」 |
| `utc_offset_hours` | `8` | **本地时区相对 UTC 的偏移**，见下文 |

## 两个实现细节（用之前值得知道）

### 时区

实测 AstrBot 存 `created_at` 用的是 **UTC**，而用户说的是**本地时间**。本插件会把「2024-03-17」这类本地日期换算成 UTC 区间再查库，所以你看不到时区错位。

如果你的 AstrBot 版本存的是本地时间，把 `utc_offset_hours` 改成 `0`。
（验证方法：发一条消息，对比 `docker logs` 的时间与库里那行的 `created_at`。）

### 媒体路径的两种来源

- **历史消息**：如果你用外部脚本导入过历史（比如从旧客户端导出），插件约定 `content._media` 是一个数组，元素形如
  ```json
  {"kind": "image", "file": "media/xxx.png", "md5": "…", "thumb_only": false}
  ```
  `file` 可以是绝对路径，也可以是相对 `archive_root` 的路径。
- **新消息**：插件自己落盘后写入绝对路径。

`_media` 是加在官方 `content` JSON 里的自定义键。AstrBot 读取时只遍历 `message` 数组，**额外的键会被忽略**，所以不影响官方功能。

## 兼容性

- AstrBot ≥ 4.28（在 4.28.2 上实测）
- 协议端：任何 OneBot v11 实现（[NapCat](https://github.com/NapNeko/NapCatQQ) 实测通过）
- 只在**已被白名单放行**的会话里工作（沿用 AstrBot 自己的白名单）

## 已知限制

- 只有**本机磁盘上仍然存在**的媒体能发出来；从没下载过的历史媒体只剩占位符
- 视频/语音能定位并发送，但没做转码（`.amr` 在多数客户端里要额外解码）
- 检索是**全表扫描 + 过滤**（`created_at` 有 LIKE/范围条件）。7 万行量级实测全量读约 0.1 秒；上百万行时建议自己加索引

## 许可

MIT

## 相关项目

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 本插件的宿主框架
- [NapCat](https://github.com/NapNeko/NapCatQQ) —— 提供 QQ ↔ OneBot v11 的协议端
- [OneBot v11](https://github.com/botuniverse/onebot-11) —— 协议标准
