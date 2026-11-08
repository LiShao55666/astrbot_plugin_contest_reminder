# 比赛提醒（AstrBot 插件）

在比赛**开赛前 N 小时**自动把比赛信息发到群里，带 `@全体成员`。

支持的比赛：**牛客竞赛**、**Codeforces**、**AtCoder**、**LeetCode**、**CodeChef**、**洛谷**，以及手动添加的自定义比赛。

## 效果

```
@全体成员
牛客周赛 Round 169
https://ac.nowcoder.com/acm/contest/143199

Codeforces Round 1134 (Div. 4)
https://codeforces.com/contests/2290
```

同一时间开赛的多场比赛（例如 CF 的 Div.1 / Div.2）会合并成一条消息。

## 安装

1. 把整个 `astrbot_plugin_contest_reminder` 文件夹放到 AstrBot 的 `data/plugins/` 下；
2. 在 AstrBot 网页「插件」页重载插件；
3. 打开插件设置，按需配置。

## 配置说明

| 配置项 | 说明 |
| --- | --- |
| 开赛前多少小时提醒 | 默认 2 小时 |
| 提醒时 @全体成员 | 默认开启 |
| 发送到哪些群 | 会话 ID，格式 `平台ID:GroupMessage:群号`，**必须填写**才会发送 |
| 牛客竞赛 | 默认开启，不需要 API Key |
| 牛客只提醒哪些比赛 | 默认只提醒名称含「周赛」的 |
| Codeforces / AtCoder / LeetCode / CodeChef / 洛谷 | 默认关闭，需要 clist.by API Key |
| 自定义比赛 | 手动添加的比赛，每行一条 |
| clist.by 用户名 / API Key | 启用上述平台前必须填写 |
| 演练模式 | 只写日志不发送，方便验证 |

## 获取 clist.by API Key

Codeforces 等平台的官方 API 有 Cloudflare 拦截，因此本插件通过 [clist.by](https://clist.by/) 获取赛程：

1. 注册 https://clist.by/ （免费）
2. 登录后打开 https://clist.by/api/
3. 复制其中的 **API key**，连同用户名一起填到插件设置里

> clist.by 免费账号限制 10 次/分钟，插件默认每 10 分钟才请求一次，不会超限。

## 自定义比赛

```
校赛热身 | https://example.com/c/1 | 2026-11-20 19:00 | 1
# 这场先不提醒 | https://example.com/c/2 | 2026-11-21 19:00
```

字段依次为：名称 | 链接 | 开赛时间 | 提前小时（可选，省略则用全局值）。
行首 `#` 表示临时停用。时间支持 `2026-11-20 19:00`、`2026/11/20 19:00`、`11-20 19:00`（省略年份按今年）。

## 常见问题

**Q：为什么配置了 Codeforces 却没收到提醒？**
检查是否填了 clist.by 用户名和 API Key，以及「发送到哪些群」是否填写。插件启动时会在日志里提示缺少的配置。

**Q：提醒时间点机器人没在线怎么办？**
只要在开赛前重新上线，下一次检查会补发；比赛已经开始则不再提醒。

**Q：会不会重复提醒？**
同一场比赛只会提醒一次，记录保存在 `data/plugin_data/astrbot_plugin_contest_reminder/state.json`。

## 命令

- `/比赛提醒测试`（管理员）：预览未来 24 小时内将要提醒的比赛，不发送。

## 许可证

MIT
