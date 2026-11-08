"""比赛提醒（赛事开赛前自动 @全体成员 提醒）。

数据源：
1. 牛客竞赛（ac.nowcoder.com）—— 抓取比赛页内嵌数据，无需任何配置；
2. clist.by —— Codeforces / AtCoder / LeetCode / CodeChef / 洛谷 等，需要免费 API Key；
3. 自定义比赛 —— 手动填写名称、链接、开赛时间。

行为：
- 每隔 check_interval_seconds 秒检查一次，比赛开赛前 remind_before_hours 小时内提醒一次；
- 同一时间开赛的多场比赛合并成一条消息；
- 已提醒过的比赛记录在 state.json，不会重复；
- 比赛已开始时跳过；
- 支持 dry_run 演练（只写日志、不发送）。
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import AtAll, Plain
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_contest_reminder"
DATA_DIR = os.path.join("data", "plugin_data", PLUGIN_NAME)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 配置项 -> clist.by 里的 host
CLIST_PLATFORMS = {
    "include_cf": "codeforces.com",
    "enable_atcoder": "atcoder.jp",
    "enable_leetcode": "leetcode.com",
    "enable_codechef": "codechef.com",
    "enable_luogu": "luogu.com.cn",
}

DEFAULT_CFG = {
    "groups": [],
    "remind_before_hours": 2,
    "at_all": True,
    # 平台开关：默认只开牛客（无需配置），其余需要 clist.by API Key
    "include_nowcoder": True,
    "nowcoder_keywords": ["周赛"],
    "include_cf": False,
    "enable_atcoder": False,
    "enable_leetcode": False,
    "enable_codechef": False,
    "enable_luogu": False,
    "extra_hosts": [],
    "custom_contests": "",
    # clist.by 凭据
    "clist_username": "",
    "clist_api_key": "",
    # 运行参数
    "max_items": 8,
    "check_interval_seconds": 60,
    "fetch_cache_minutes": 10,
    "dry_run": False,
}


def _http_get(url: str, headers: dict | None = None, timeout: int = 25) -> str:
    """同步 GET，返回文本。调用方应放到线程里执行。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _parse_datetime(value: str):
    """把用户填写的时间字符串解析成 datetime，失败返回 None。"""
    text = str(value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.year == 1900:  # 没写年份
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


class ContestReminder(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self._dir = DATA_DIR
        os.makedirs(self._dir, exist_ok=True)
        self._cfg_path = os.path.join(self._dir, "config.json")
        self._state_path = os.path.join(self._dir, "state.json")
        self.cfg = self._merge_cfg(config)
        self.state = self._load_state()
        self._cache: list | None = None
        self._cache_at: datetime | None = None
        self._busy = False
        self._warned_missing_key = False
        self._warned_no_group = False
        self._task = None
        try:
            self._task = asyncio.create_task(self._loop())
        except RuntimeError as exc:  # 理论上不会发生
            logger.warning(f"[contest] 后台任务启动失败: {exc}")
        self._log_startup()

    # ---------------------------------------------------------------- 配置
    def _merge_cfg(self, injected) -> dict:
        """优先级：AstrBot 插件配置 > 旧版 JSON 配置 > 默认值。"""
        cfg = dict(DEFAULT_CFG)
        try:
            cfg.update(self._load_legacy_cfg())
        except Exception:
            pass
        if injected is not None and hasattr(injected, "items"):
            try:
                for key, value in injected.items():
                    if value is not None:
                        cfg[key] = value
            except Exception:
                pass
        return cfg

    def _load_legacy_cfg(self) -> dict:
        if not os.path.exists(self._cfg_path):
            return {}
        try:
            with open(self._cfg_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _load_state(self) -> dict:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            now = datetime.now()
            kept = {}
            for key, value in (self.state.get("reminded") or {}).items():
                try:
                    ts = datetime.fromisoformat(str(value))
                except ValueError:
                    continue
                if (now - ts).days < 7:
                    kept[key] = value
            self.state["reminded"] = kept
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"[contest] 写状态失败: {exc}")

    def _log_startup(self) -> None:
        platforms = ["牛客"] if self.cfg.get("include_nowcoder") else []
        platforms += [host for key, host in CLIST_PLATFORMS.items() if self.cfg.get(key)]
        platforms += list(self.cfg.get("extra_hosts") or [])
        logger.info(
            f"[contest] 已加载：提前 {self.cfg.get('remind_before_hours')} 小时提醒，"
            f"@全体成员={self.cfg.get('at_all')}，平台={platforms or '无'}，"
            f"群={len(self.cfg.get('groups') or [])} 个，dry_run={self.cfg.get('dry_run')}"
        )
        if not (self.cfg.get("groups") or []):
            logger.warning("[contest] 未配置发送群（groups），插件不会发送任何消息")
        self._check_key_warning()

    def _check_key_warning(self) -> None:
        """启用了 clist 平台但没填 key 时给一次明确提示。"""
        uses_clist = any(self.cfg.get(key) for key in CLIST_PLATFORMS) or bool(self.cfg.get("extra_hosts"))
        has_key = bool(str(self.cfg.get("clist_username") or "").strip()) and bool(
            str(self.cfg.get("clist_api_key") or "").strip()
        )
        if uses_clist and not has_key and not self._warned_missing_key:
            self._warned_missing_key = True
            logger.warning(
                "[contest] 已开启 Codeforces/AtCoder 等平台，但没有填写 clist.by 用户名和 API Key，"
                "这些平台会被跳过。请到 https://clist.by/ 免费注册后在 https://clist.by/api/ 获取。"
            )

    async def terminate(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ---------------------------------------------------------------- 数据源
    def _fetch_nowcoder(self, keywords: list) -> list:
        """解析牛客比赛页内嵌的 data-json。"""
        try:
            page = _http_get("https://ac.nowcoder.com/acm/contest/vip-index")
        except Exception as exc:
            logger.warning(f"[contest] 牛客页面抓取失败: {exc}")
            return []
        kws = [str(k) for k in (keywords or []) if str(k).strip()]
        items = []
        for raw in re.findall(r'data-json="([^"]+)"', page):
            try:
                data = json.loads(html_mod.unescape(html_mod.unescape(raw)))
            except ValueError:
                continue
            name = str(data.get("contestName") or "").strip()
            cid = data.get("contestId")
            start_ms = data.get("contestStartTime")
            if not name or not cid or not start_ms:
                continue
            if kws and not any(k in name for k in kws):
                continue
            try:
                start = datetime.fromtimestamp(int(start_ms) / 1000)
            except (ValueError, OSError):
                continue
            items.append(
                {
                    "name": name,
                    "url": f"https://ac.nowcoder.com/acm/contest/{cid}",
                    "start": start,
                    "key": f"nowcoder:{cid}",
                    "source": "nowcoder",
                }
            )
        return items

    def _platform_hosts(self) -> list:
        """按开关汇总需要从 clist 拉取的 host。"""
        hosts = [host for key, host in CLIST_PLATFORMS.items() if self.cfg.get(key)]
        hosts += [str(h).strip() for h in (self.cfg.get("extra_hosts") or []) if str(h).strip()]
        result = []
        for host in hosts:
            low = host.lower()
            if low in ("ac.nowcoder.com", "nowcoder.com"):
                continue  # 牛客走直连，避免重复
            if low not in [x.lower() for x in result]:
                result.append(host)
        return result

    def _fetch_clist(self, hosts: list, days: int = 7) -> list:
        """从 clist.by 拉取指定平台的比赛。"""
        user = str(self.cfg.get("clist_username") or "").strip()
        token = str(self.cfg.get("clist_api_key") or "").strip()
        if not user or not token:
            self._check_key_warning()
            return []
        if not hosts:
            return []
        start_utc = datetime.now(timezone.utc) - timedelta(hours=1)
        end_utc = start_utc + timedelta(days=days)
        params = {
            "start__gt": start_utc.strftime("%Y-%m-%dT%H:%M:%S"),
            "start__lt": end_utc.strftime("%Y-%m-%dT%H:%M:%S"),
            "order_by": "start",
            "limit": 500,
            "format": "json",
        }
        url = "https://clist.by/api/v4/contest/?" + urllib.parse.urlencode(params)
        try:
            body = _http_get(url, headers={"Authorization": f"ApiKey {user}:{token}"}, timeout=30)
            data = json.loads(body)
        except Exception as exc:
            logger.warning(f"[contest] clist.by 请求失败: {exc}")
            return []
        objects = data.get("objects") if isinstance(data, dict) else None
        if not isinstance(objects, list):
            logger.warning(f"[contest] clist.by 返回格式异常: {str(data)[:160]}")
            return []
        lowers = [h.lower() for h in hosts]
        items = []
        for obj in objects:
            try:
                host = str(obj.get("host") or "").lower()
                if not any(host == low or host.endswith("." + low) for low in lowers):
                    continue
                name = str(obj.get("event") or "").strip()
                href = str(obj.get("href") or "").strip()
                raw_start = obj.get("start")
                if not name or not href or not raw_start:
                    continue
                start = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                items.append(
                    {
                        "name": name,
                        "url": href,
                        "start": start.astimezone().replace(tzinfo=None),
                        "key": f"clist:{obj.get('id')}",
                        "source": host,
                    }
                )
            except (ValueError, TypeError):
                continue
        return items

    def _custom_contests(self) -> list:
        """解析「自定义比赛」文本框。

        每行格式：名称 | 链接 | 2026-11-20 19:00 | 提前小时(可选)
        行首加 # 表示临时关闭该条。
        """
        raw = self.cfg.get("custom_contests") or ""
        lines = raw if isinstance(raw, list) else str(raw).splitlines()
        items = []
        for line in lines:
            text = str(line).strip()
            if not text or text.startswith("#"):
                continue
            parts = [p.strip() for p in text.split("|")]
            if len(parts) < 3:
                continue
            name, url, start_text = parts[0], parts[1], parts[2]
            if not name or not url:
                continue
            lead = None
            if len(parts) >= 4 and parts[3]:
                try:
                    lead = float(parts[3])
                except ValueError:
                    lead = None
            start = _parse_datetime(start_text)
            if start is None:
                logger.warning(f"[contest] 自定义比赛时间无法解析: {text}")
                continue
            items.append(
                {
                    "name": name,
                    "url": url,
                    "start": start,
                    "key": f"custom:{name}:{start.strftime('%Y%m%d%H%M')}",
                    "source": "custom",
                    "lead_hours": lead,
                }
            )
        return items

    def _fetch_all_sync(self) -> list:
        items = []
        if self.cfg.get("include_nowcoder", True):
            items += self._fetch_nowcoder(self.cfg.get("nowcoder_keywords") or [])
        hosts = self._platform_hosts()
        if hosts:
            items += self._fetch_clist(hosts)
        items += self._custom_contests()
        unique = {}
        for contest in items:
            unique.setdefault(contest["key"], contest)
        return sorted(unique.values(), key=lambda c: (c["start"], c["name"]))

    async def _all_contests(self, force: bool = False) -> list:
        """带缓存地拉取全部比赛；网络请求放到线程里，避免阻塞事件循环。"""
        now = datetime.now()
        try:
            cache_minutes = float(self.cfg.get("fetch_cache_minutes", 10) or 10)
        except (TypeError, ValueError):
            cache_minutes = 10
        if (
            not force
            and self._cache is not None
            and self._cache_at is not None
            and (now - self._cache_at).total_seconds() < cache_minutes * 60
        ):
            return list(self._cache)
        items = await asyncio.to_thread(self._fetch_all_sync)
        self._cache = items
        self._cache_at = now
        return list(items)

    # ---------------------------------------------------------------- 调度
    async def _loop(self) -> None:
        while True:
            try:
                interval = int(self.cfg.get("check_interval_seconds", 60) or 60)
            except (TypeError, ValueError):
                interval = 60
            await asyncio.sleep(max(15, interval))
            if self._busy:
                continue
            self._busy = True
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[contest] 定时检查出错")
            finally:
                self._busy = False

    async def _tick(self) -> None:
        await self._check_manual()
        now = datetime.now()
        try:
            default_lead = float(self.cfg.get("remind_before_hours", 2) or 2)
        except (TypeError, ValueError):
            default_lead = 2.0
        contests = await self._all_contests()
        due = []
        for contest in contests:
            lead = contest.get("lead_hours") or default_lead
            try:
                lead = float(lead)
            except (TypeError, ValueError):
                lead = default_lead
            delta = (contest["start"] - now).total_seconds()
            if 0 < delta <= lead * 3600:
                due.append(contest)
        if not due:
            return
        buckets: dict = {}
        for contest in due:
            buckets.setdefault(contest["start"].replace(second=0, microsecond=0), []).append(contest)
        reminded = self.state.setdefault("reminded", {})
        for _, contests_in_bucket in sorted(buckets.items()):
            pending = [c for c in contests_in_bucket if c["key"] not in reminded]
            if not pending:
                continue
            sent = await self._send_items(pending, at_all=True)
            if self.cfg.get("dry_run") or not sent:
                continue
            stamp = now.isoformat(timespec="seconds")
            for contest in pending:
                reminded[contest["key"]] = stamp
            self._save_state()

    # ---------------------------------------------------------------- 发送
    @staticmethod
    def _format(items: list) -> str:
        return "\n\n".join(f"{c['name']}\n{c['url']}" for c in items)

    async def _send_items(self, items: list, at_all: bool = True, prefix: str = "", dry_run=None) -> int:
        try:
            max_items = int(self.cfg.get("max_items", 8) or 8)
        except (TypeError, ValueError):
            max_items = 8
        items = items[:max_items]
        text = prefix + self._format(items)
        if dry_run is None:
            dry_run = bool(self.cfg.get("dry_run", False))
        use_at_all = bool(at_all) and bool(self.cfg.get("at_all", True))
        if dry_run:
            logger.info(
                "[contest][DRY_RUN] at_all=%s 内容:\n%s",
                use_at_all,
                ("@全体成员\n" if use_at_all else "") + text,
            )
            return 0
        groups = self.cfg.get("groups") or []
        if not groups:
            if not self._warned_no_group:
                self._warned_no_group = True
                logger.warning("[contest] 未配置发送群（groups），本次消息未发送")
            return 0
        chain = []
        if use_at_all:
            chain.append(AtAll())
        chain.append(Plain(("\n" if use_at_all else "") + text))
        sent = 0
        for umo in groups:
            try:
                if await self.context.send_message(umo, MessageChain(chain)):
                    sent += 1
            except Exception as exc:
                logger.error(f"[contest] 发送到 {umo} 失败: {exc}")
        logger.info("[contest] 已发送 %s 场比赛提醒到 %s 个群：%s 等", len(items), sent, items[0]["name"])
        return sent

    # ---------------------------------------------------------------- 手动触发
    async def _check_manual(self) -> None:
        """把 manual.json 放进插件数据目录即可手动发一条，发完自动删除。

        支持两种写法：
        {"items": [{"name": "...", "url": "..."}], "at_all": true}
        {"date": "2026-11-08", "at_all": false, "prefix": "[测试] "}
        """
        path = os.path.join(self._dir, "manual.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                conf = json.load(f)
        except Exception as exc:
            logger.warning(f"[contest] 读 manual.json 失败: {exc}")
            conf = {}
        try:
            os.remove(path)
        except OSError:
            pass

        raw_items = conf.get("items")
        if isinstance(raw_items, list) and raw_items:
            items = []
            now = datetime.now()
            for index, entry in enumerate(raw_items):
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                url = str(entry.get("url") or "").strip()
                if name and url:
                    items.append({"name": name, "url": url, "start": now, "key": f"manual:{index}", "source": "manual"})
            sent = await self._send_items(
                items,
                at_all=bool(conf.get("at_all", False)),
                prefix=str(conf.get("prefix") or ""),
                dry_run=conf.get("dry_run"),
            )
            if sent and conf.get("mark_reminded"):
                stamp = datetime.now().isoformat(timespec="seconds")
                reminded = self.state.setdefault("reminded", {})
                for key in conf.get("mark_keys") or []:
                    reminded[str(key)] = stamp
                self._save_state()
            return

        target = _parse_datetime(str(conf.get("date") or "")) or datetime.now()
        items = [c for c in await self._all_contests(force=True) if c["start"].date() == target.date()]
        if not items:
            logger.info(f"[contest] manual: {target.date()} 没有比赛")
            return
        await self._send_items(
            items,
            at_all=bool(conf.get("at_all", False)),
            prefix=str(conf.get("prefix") or ""),
            dry_run=conf.get("dry_run"),
        )

    # ---------------------------------------------------------------- 命令
    @filter.command("比赛提醒测试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_remind(self, event: AstrMessageEvent):
        """预览未来 24 小时内将要提醒的比赛（不发送）。"""
        now = datetime.now()
        contests = await self._all_contests(force=True)
        upcoming = [c for c in contests if 0 < (c["start"] - now).total_seconds() <= 24 * 3600]
        if not upcoming:
            yield event.plain_result("未来 24 小时内没有查到比赛")
            return
        lines = [f"{c['start'].strftime('%m-%d %H:%M')} {c['name']}\n{c['url']}" for c in upcoming]
        yield event.plain_result("未来 24 小时内将提醒：\n\n" + "\n\n".join(lines))
