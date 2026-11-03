"""新梗采集流水线：从萌娘百科和搜索里捞新梗，喂进 MaiBot 自己的黑话库

三段链路：
  发现  sitemap 每日 diff（主力）/ searxng 搜「最近热梗」（补外部热度）
  释义  萌百 extracts 取首段 -> 取不到就抓 /wiki/ 的 HTML -> 还不行丢给 searxng 搜
  入库  够硬的直接 import 成全局黑话（她聊天时自己就会用），不够的攒进待审队列

萌百把 search / categorymembers / recentchanges 全按 action 名裁掉了，
所以「发现新词」只能靠 sitemap 的标题集合做差集。别指望换个参数能绕过。
"""

import asyncio
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

import xml.etree.ElementTree as ET

from . import store as gengdb  # SQLite 梗库（原 gengdb.py，改名后沿用内部调用名）

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")
_last_hit = 0.0          # 上一次打萌百的时间，用来卡 1 req/s

MARKER_PLAY = "【最近学的梗】"


# ---------------------------------------------------------------- HTTP

def _http(url: str, ua: str, timeout: int = 20, data: bytes | None = None,
          extra: dict | None = None) -> bytes:
    headers = {"User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9"}
    if extra:
        headers.update(extra)
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _pace() -> None:
    """萌百实测连发六次就开始拒，老老实实一秒一次。"""
    global _last_hit
    gap = time.time() - _last_hit
    if gap < 1.05:
        time.sleep(1.05 - gap)
    _last_hit = time.time()


def _load_json(result) -> object:
    """把 LLM 的回复解析成对象，模型爱包代码块这点跟 gossip.py 一样处理。"""
    if not isinstance(result, dict) or not result.get("success"):
        return None
    text = str(result.get("response") or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for left, right in (("[", "]"), ("{", "}")):
        start, end = text.find(left), text.rfind(right)
        if start < 0:
            continue
        # 模型被 max_tokens 掐掉尾巴时这里没有收尾括号，补一个再试一次
        chunk = text[start:end + 1] if end > start else text[start:] + right
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    return None


# ---------------------------------------------------------------- 发现

def fetch_sitemap_titles(cfg, logger) -> list:
    """拉 sitemap 索引，再把 NS_0 的几片全下载，抽出一整批条目名。"""
    try:
        raw = _http(cfg.sitemap_index, cfg.ua, cfg.timeout)
    except (urllib.error.URLError, OSError) as exc:
        logger.warning(f"梗：sitemap 索引拉不下来 {exc}")
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning(f"梗：sitemap 索引解析失败 {exc}")
        return []

    shards = [el.text for el in root.findall(".//sm:loc", _NS) if el.text]
    shards = [s for s in shards if "NS_0" in s][: max(1, cfg.shards)]
    if not shards:
        logger.warning("梗：索引里没找到 NS_0 分片，格式可能变了")
        return []

    titles: list = []
    for shard in shards:
        body = None
        for attempt in range(3):
            try:
                body = _http(shard, cfg.ua, max(cfg.timeout, 60))
                break
            except (urllib.error.URLError, OSError) as exc:
                logger.warning(f"梗：分片没拉下来第 {attempt + 1} 次 {shard[-28:]} {exc}")
            time.sleep(2)
        if not body:
            logger.warning(f"梗：这片放弃了，标题池会缺一块 {shard[-28:]}")
            continue

        # 不整树解析：萌百有一片的 XML 尾巴是坏的（闭合标签都没写完），
        # ElementTree 直接抛 unclosed token。逐条抠 <loc> 能把前面的全捞出来。
        if not body.rstrip().endswith(b"</urlset>"):
            logger.warning(f"梗：这片尾巴不完整（{len(body)} 字节），按能读到的部分抠")

        for loc in _LOC_RE.findall(body.decode("utf-8", "replace")):
            seg = loc.rsplit("/", 1)[-1]
            name = urllib.parse.unquote(seg).replace("_", " ").strip()
            if name:
                titles.append(name)
        logger.info(f"梗：分片到手，累计 {len(titles)} 条标题")
    return titles


_NOISE = re.compile(r"[:：()（）\[\]【】<>《》/\\|*#]")


def prefilter(names) -> list:
    """本地先过一遍，别把十六万个条目名全丢给模型。

    留下的必须：2~12 字、带汉字、不带括号冒号这类命名空间标记。
    """
    out: list = []
    seen = set()
    for name in names:
        text = str(name or "").strip()
        if not (2 <= len(text) <= 12):
            continue
        if _NOISE.search(text):
            continue
        if not re.search(r"[\u4e00-\u9fff]", text):
            continue
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


async def pick_candidates(cfg, names, ctx, logger) -> list:
    """一次四十个标题问模型：里面哪些是梗。"""
    if not names:
        return []
    prompt = (
        "下面是一批中文百科的新增条目标题。请只挑出属于"
        "「网络流行语 / 网络梗 / 网络迷因 / 成句 / 网络用语」的那些。\n"
        "角色名、作品名、人名、组织名、地名、歌曲名、游戏名一律不要。\n"
        "只输出 JSON 数组，元素是命中的标题原文，一个都没有就输出 []。不要解释、不要代码块。\n"
        + json.dumps(list(names), ensure_ascii=False)
    )
    try:
        got = await ctx.llm.generate(
            prompt, task_name=cfg.geng.llm_task, temperature=0.2, max_tokens=1500
        )
    except Exception as exc:
        logger.warning(f"梗：批次判定出错 {exc}")
        return []
    data = _load_json(got)
    if not isinstance(data, list):
        return []
    pool = set(names)
    return [str(x).strip() for x in data if str(x).strip() in pool]


async def _searx_names(cfg, ctx, logger) -> list:
    """搜索那一路：先搜「最近热梗」这类 query，再让模型从摘要里抽词。"""
    ym = time.strftime("%Y年%m月")
    chunks: list = []
    for tpl in cfg.gengsearx.queries:
        query = str(tpl).replace("{ym}", ym)
        rows = await asyncio.to_thread(searx_query, cfg.gengsearx, query, cfg.gengsearx.per_query)
        for row in rows:
            if row["content"]:
                chunks.append(f"{row['title']}｜{row['content']}")
    if not chunks:
        logger.info("梗：搜索那路没捞到东西")
        return []

    prompt = (
        "下面是一批搜索结果的标题和摘要，里面可能夹着最近流行的网络梗。\n"
        "把真正的梗词挑出来：每个 2~8 字、能单独拎出来用，不要整句、不要普通名词短语。\n"
        "只输出 JSON 数组，没有就输出 []。不要解释。\n"
        + "\n".join(chunks)[:4000]
    )
    try:
        got = await ctx.llm.generate(
            prompt, task_name=cfg.geng.llm_task, temperature=0.3, max_tokens=800
        )
    except Exception as exc:
        logger.warning(f"梗：搜索抽词出错 {exc}")
        return []
    data = _load_json(got)
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if str(x).strip()]


# ---------------------------------------------------------------- 释义

def moegirl_extract(cfg, term: str):
    """萌百的 extracts 是唯一没被裁的口子，首段纯文本直接能用。

    返回 (实际标题, 首段)。标题可能和查询词不一样 —— 比如「蚌埠住了」
    会重定向到「谐音梗」，前缀照抄查询词会让下游误以为整段在讲这个词。
    """
    query = urllib.parse.urlencode({
        "action": "query", "prop": "extracts", "explaintext": 1,
        "exintro": 1, "exsentences": 3, "redirects": 1,
        "format": "json", "titles": term,
    })
    _pace()
    try:
        raw = _http(f"https://zh.moegirl.org.cn/api.php?{query}", cfg.ua, cfg.timeout)
    except (urllib.error.URLError, OSError):
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    for page in pages.values():
        text = str(page.get("extract") or "").strip()
        if text:
            return str(page.get("title") or term), text
    return None


def moegirl_search(cfg, term: str, limit: int = 3):
    """全文搜一把，返回前几条 [(标题, 摘要)]。

    用 generator=search 而不是 list=search —— 后者被萌百按 action 名裁掉了
    （action-notallowed），前者挂在 action=query 底下，一直能用。

    这里不做相关性过滤，全交给下游模型挑。全文搜是分词匹配，
    「遥遥领先」能匹到《直觉No Problem！》，代码层按标题硬筛会两头不讨好：
    筛严了漏掉「谐音梗」这种通用条目，筛松了喂进去一堆不相干的。
    """
    query = urllib.parse.urlencode({
        "action": "query", "generator": "search",
        "gsrsearch": term, "gsrlimit": str(max(1, limit)), "gsrnamespace": 0,
        "prop": "info|extracts", "inprop": "url",
        "exintro": 1, "explaintext": 1, "format": "json",
    })
    _pace()
    try:
        raw = _http(f"https://zh.moegirl.org.cn/api.php?{query}", cfg.ua, cfg.timeout)
    except (urllib.error.URLError, OSError):
        return []
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return []

    pages = list(((data.get("query") or {}).get("pages") or {}).values())
    pages.sort(key=lambda page: page.get("index") or 999)

    hits = []
    for page in pages:
        text = str(page.get("extract") or "").strip()
        title = str(page.get("title") or "").strip()
        if not text or len(text) < 15:
            continue
        hits.append((title, text))
    return hits


def moegirl_html(cfg, term: str):
    """extracts 空着的时候退一步抓干净 URL，自己剥标签。"""
    path = urllib.parse.quote(str(term).replace(" ", "_"))
    _pace()
    try:
        raw = _http(f"https://zh.moegirl.org.cn/wiki/{path}", cfg.ua, cfg.timeout)
    except (urllib.error.URLError, OSError):
        return None
    html = raw.decode("utf-8", "replace")
    idx = html.find("mw-parser-output")
    if idx < 0:
        return None
    body = html[idx:idx + 24000]
    body = re.sub(r"<(script|style).*?</\1>", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:400] or None


def _moegirl_text(cfg, term: str):
    """精确标题 → 全文搜，把拿到的几条都拼上，让模型自己挑。

    prefer_html 是给「连 api.php 都不想碰」的场合留的 —— 站点的 robots
    把 /*action= 一并 Disallow 了，虽然 llms.txt 允许摘要，但想彻底干净就切这个。
    """
    if getattr(cfg, "prefer_html", False):
        return moegirl_html(cfg, term)

    chunks = []
    hit = moegirl_extract(cfg, term)
    if hit and len(hit[1]) >= 15:
        chunks.append(f"【{hit[0]}】{hit[1]}")
    if not chunks:
        # 新梗大多没有独立条目，全文搜兜一手，命中几条就交几条
        for title, body in moegirl_search(cfg, term):
            chunks.append(f"【{title}】{body}")
    if not chunks:
        return moegirl_html(cfg, term)
    return "\n".join(chunks)


def searx_query(cfg, query: str, n: int = 6) -> list:
    """走容器里的 searxng，注意别写成宿主侧的 127.0.0.1:8800。"""
    url = f"{cfg.base_url.rstrip('/')}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json"}
    )
    try:
        raw = _http(url, "maibot-geng/1.0", 20)
        data = json.loads(raw.decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []
    out: list = []
    for item in (data.get("results") or [])[:n]:
        out.append({
            "title": str(item.get("title") or ""),
            "content": str(item.get("content") or ""),
            "url": str(item.get("url") or ""),
        })
    return out


def _read_card(text: str):
    """先按三行纯文本读，读不出来再试 JSON。

    不强制模型吐 JSON 是有原因的：gemini 这类会把思考写进正文，
    一不小心就撑断在 max_tokens 上，三行格式截掉尾巴也还能救回前两行。
    """
    meaning = ""
    conf = None
    fun = None

    hit = re.search(r"释义\s*[:：]\s*(.+)", text)
    if hit:
        meaning = hit.group(1).strip().splitlines()[0].strip("「」\"' ")
    hit = re.search(r"把握\s*[:：]\s*([0-9.]+)", text)
    if hit:
        conf = hit.group(1)
    hit = re.search(r"好玩\s*[:：]\s*([0-9]+)", text)
    if hit:
        fun = hit.group(1)

    if not meaning:
        data = _load_json({"success": True, "response": text})
        if isinstance(data, dict):
            meaning = str(data.get("meaning") or "").strip()
            conf = data.get("confidence", conf)
            fun = data.get("fun", fun)

    if not meaning:
        return None

    try:
        confidence = float(conf) if conf is not None else 0.5
    except (TypeError, ValueError):
        confidence = 0.5
    try:
        fun_score = int(fun) if fun is not None else 3
    except (TypeError, ValueError):
        fun_score = 3

    return {
        "meaning": meaning[:120],
        "confidence": max(0.0, min(1.0, confidence)),
        "fun": max(1, min(5, fun_score)),
    }


async def distill(cfg, term: str, raw: str, ctx, logger):
    """把素材压成一张梗卡：什么意思、有多确定、好不好玩。"""
    prompt = (
        "你在给一个 QQ 聊天机器人整理网络梗，帮她把这个词变成一句人话。\n"
        f"词：{term}\n"
        f"参考材料：{raw[:1600]}\n\n"
        f"材料可能是好几条词条拼起来的，只采纳真正在解释「{term}」的内容，"
        "其余无关的（同音字、相近的名字、刚好匹配到同一个字的作品）一律不要用。\n"
        "只输出三行，不要解释、不要空行、不要代码块：\n"
        "释义：（这个梗什么意思，40字以内，口语化，说清出处和使用场合）\n"
        "把握：（0到1的小数，材料越确定越高；全都对不上就写 0.3 以下，别硬编）\n"
        "好玩：（1到5的整数。有反差、能随口接、日常能用上的写 4 到 5；"
        "需要长篇解释、专业术语、地域性太强的写 1 到 2）"
    )
    try:
        got = await ctx.llm.generate(
            prompt, task_name=cfg.geng.llm_task, temperature=0.4, max_tokens=1200
        )
    except Exception as exc:
        logger.warning(f"梗：{term} 提炼出错 {exc}")
        return None

    text = str((got or {}).get("response") or "")
    if not (got or {}).get("success") or not text.strip():
        logger.info(f"梗：{term} 模型没给东西")
        return None

    card = _read_card(text)
    if not card:
        logger.warning(f"梗：{term} 的回复读不出三段，原文前 120 字：{text[:120]}")
    return card


async def lookup(cfg, term: str, ctx, logger):
    """/梗找、冷启动、LLM 工具共用的一条查询链。"""
    raw = ""
    source = ""
    if cfg.gengmoegirl.enabled:
        raw = await asyncio.to_thread(_moegirl_text, cfg.gengmoegirl, term) or ""
        if raw:
            source = "moegirl"
    if not raw and cfg.gengsearx.enabled:
        rows = await asyncio.to_thread(
            searx_query, cfg.gengsearx, f"{term} 是什么梗", cfg.gengsearx.per_query
        )
        if rows:
            raw = "\n".join(f"{r['title']}：{r['content']}" for r in rows if r["content"])
            source = "searxng"
    if not raw:
        return None
    card = await distill(cfg, term, raw, ctx, logger)
    if not card:
        return None
    card["term"] = term
    card["source"] = source
    return card


# ---------------------------------------------------------------- 入库

def _token(cfg) -> str:
    if cfg.token:
        return str(cfg.token)
    try:
        with open(cfg.webui_json, encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return ""
    return str(data.get("access_token") or "")


def _as_jargon(card: dict) -> dict:
    """凑成 WebUI 导入接口要的形状。

    is_jargon 一定给 true —— 接口自己还会跟 meaning 做一次与运算，
    含义空着的话它也落不成黑话，这里给 false 反而是明确表态「不是梗」。
    """
    conf = float(card.get("confidence") or 0)
    return {
        "content": card["term"],
        "meaning": card["meaning"],
        "count": 5 + int(round(conf * 5)),
        "is_jargon": True,
        "is_complete": True,
        "is_global": True,
    }


def push_global(cfg, items: list, logger):
    """一次性把攒好的词灌进全局黑话库，返回 (进库数, 跳过数)。"""
    if not items:
        return 0, 0
    token = _token(cfg)
    if not token:
        logger.warning("梗：拿不到 WebUI 的 token，这批先留着下轮再说")
        return 0, len(items)

    payload = json.dumps({
        "target_session_ids": [],
        "is_global": True,
        "conflict_strategy": "skip",
        "jargons": items,
    }, ensure_ascii=False).encode("utf-8")

    url = cfg.webui.rstrip("/") + "/api/webui/jargon/import"
    try:
        raw = _http(url, "maibot-geng/1.0", 25, data=payload, extra={
            "Content-Type": "application/json",
            "Cookie": f"maibot_session={token}",
        })
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        logger.warning(f"梗：导入接口没通 {exc}")
        return 0, len(items)

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return 0, len(items)
    return int(data.get("imported_count") or 0), int(data.get("skipped_count") or 0)


# ---------------------------------------------------------------- 主流程

async def run_round(cfg, ctx, logger, mode: str = "daily", limit: int = 0) -> dict:
    """跑一轮。mode 只影响候选从哪来：
    daily 走 sitemap 差集 + 搜索；cold 从标题池里随机翻旧账。
    """
    stat = {"titles": 0, "fresh": 0, "pool": 0, "tried": 0,
            "pushed": 0, "skipped": 0, "pending": 0, "dropped": 0}

    known = await asyncio.to_thread(gengdb.known_terms, db_path)
    names: list = []

    if mode == "cold":
        # 标题池还没建过就先拉一遍，不然冷启动无米下锅
        if await asyncio.to_thread(gengdb.title_count, db_path) == 0:
            logger.info("梗：标题池还是空的，先拉一遍 sitemap 垫着（这步慢，耐心等）")
            titles = await asyncio.to_thread(fetch_sitemap_titles, cfg.gengmoegirl, logger)
            if titles:
                await asyncio.to_thread(gengdb.sync_titles, db_path, titles)
        pool = await asyncio.to_thread(gengdb.all_titles, db_path, 0)
        names = [t for t in prefilter(pool) if t not in known]
        stat["pool"] = len(names)
        logger.info(f"梗：冷启动候选 {len(names)} 个（库里标题 {gengdb.title_count(db_path)} 条）")
    else:
        if cfg.gengmoegirl.enabled:
            titles = await asyncio.to_thread(fetch_sitemap_titles, cfg.gengmoegirl, logger)
            stat["titles"] = len(titles)
            if titles:
                before = await asyncio.to_thread(gengdb.title_count, db_path)
                fresh = await asyncio.to_thread(gengdb.sync_titles, db_path, titles)
                stat["fresh"] = len(fresh)
                if before == 0:
                    logger.info(f"梗：标题池刚建好（{len(titles)} 条），今天不算新增，明天起才有 diff")
                elif len(fresh) > 2000:
                    # 上一轮有分片没拉下来，这次补齐会把整片当新增，别拿它去喂模型
                    logger.info(f"梗：一次冒出 {len(fresh)} 条，像标题池补全区，这次只灌库不算候选")
                    fresh = []
                names = [t for t in prefilter(fresh) if t not in known]
                logger.info(f"梗：今天新增 {len(fresh)} 条，过完本地筛剩 {len(names)} 个")
        if cfg.gengsearx.enabled:
            extra = await _searx_names(cfg, ctx, logger)
            names = list(dict.fromkeys(names + [t for t in prefilter(extra) if t not in known]))
            logger.info(f"梗：加上搜索那路一共 {len(names)} 个候选")

    if not names:
        logger.info("梗：这轮没候选，收工")
        return stat

    # 模型判定：daily 只判五批够用了；冷启动目标大，多判几批留余量
    picked: list = []
    batch = max(5, int(cfg.gengmoegirl.batch))
    rounds = 5 if mode != "cold" else max(3, (limit or 40) // 10)
    for start in range(0, min(len(names), batch * rounds), batch):
        picked += await pick_candidates(cfg, names[start:start + batch], ctx, logger)
    names = picked
    logger.info(f"梗：模型判定后剩 {len(names)} 个")

    cap = limit or (cfg.geng.round_cap if mode != "cold" else 40)
    room = int(cfg.geng.lib_cap) - int((await asyncio.to_thread(gengdb.stats, db_path, cfg.gengplay.max_times))["pushed"])
    if room <= 0:
        logger.info(f"梗：库里已经有 {cfg.geng.lib_cap} 条了，这轮只补不增")
        return stat
    cap = min(cap, room)

    cards: list = []
    for term in names:
        if len(cards) >= cap:
            break
        stat["tried"] += 1
        card = await lookup(cfg, term, ctx, logger)
        if not card:
            await asyncio.to_thread(gengdb.mark_term, db_path, term, "", 0, 0, "", "dropped")
            stat["dropped"] += 1
            continue

        conf, meaning = card["confidence"], card["meaning"]
        if conf >= cfg.geng.push_threshold and 8 <= len(meaning) <= 60:
            cards.append(card)
        elif conf >= cfg.geng.pending_floor:
            await asyncio.to_thread(
                gengdb.add_pending, db_path, term, meaning, conf, card["fun"], card["source"]
            )
            await asyncio.to_thread(
                gengdb.mark_term, db_path, term, meaning, conf, card["fun"], card["source"], "pending"
            )
            stat["pending"] += 1
        else:
            await asyncio.to_thread(gengdb.mark_term, db_path, term, meaning, conf, card["fun"], card["source"], "dropped")
            stat["dropped"] += 1

    if not cards:
        logger.info(f"梗：这轮没有够格的（试了 {stat['tried']} 个）")
        return stat

    items = [_as_jargon(c) for c in cards]
    done, skipped = await asyncio.to_thread(push_global, cfg.gengpush, items, logger)
    stat["pushed"] = done
    stat["skipped"] = skipped

    for card in cards:
        await asyncio.to_thread(
            gengdb.mark_term, db_path, card["term"], card["meaning"],
            card["confidence"], card["fun"], card["source"], "pushed",
        )
        if card["fun"] >= cfg.gengplay.min_fun:
            await asyncio.to_thread(
                gengdb.add_play, db_path, card["term"], card["meaning"], card["fun"]
            )

    logger.info(
        f"梗：这轮进库 {done} 条、重复跳过 {skipped} 条、"
        f"待审 {stat['pending']} 条、丢掉 {stat['dropped']} 个"
    )
    return stat


async def push_one(cfg, ctx, logger, db_path: str, term: str, meaning: str,
                   confidence: float, fun: int, source: str) -> bool:
    """单条入库，给 /梗收 和 /梗找 直接收录用。"""
    card = {"term": term, "meaning": meaning, "confidence": confidence,
            "fun": fun, "source": source}
    done, _ = await asyncio.to_thread(push_global, cfg.gengpush, [_as_jargon(card)], logger)
    if done <= 0:
        return False
    await asyncio.to_thread(
        gengdb.mark_term, db_path, term, meaning, confidence, fun, source, "pushed"
    )
    if fun >= cfg.gengplay.min_fun:
        await asyncio.to_thread(gengdb.add_play, db_path, term, meaning, fun)
    return True


# ---------------------------------------------------------------- 玩梗注入

def system_item(text: str) -> dict:
    """新建一条独立的 SystemMessageItem。

    字段跟 waimai.py 里的那份一致，但**不能复用它的 upsert**：
    那套是把内容合并进「首个 system 项」并按自己的 MARKER 切旧文本，
    两个模块共用会互相把对方的内容剪掉。
    """
    return {
        "item_type": "SystemMessageItem",
        "meta": {
            "item_id": uuid.uuid4().hex,
            "logical_turn_id": None,
            "timestamp": datetime.now().isoformat(),
        },
        "parts": [{"type": "text", "text": text}],
    }


def _carries_marker(item, marker: str) -> bool:
    if not isinstance(item, dict):
        return False
    for part in item.get("parts") or []:
        if isinstance(part, dict) and marker in str(part.get("text") or ""):
            return True
    return False


def upsert_item(kwargs: dict, block: str, marker: str = MARKER_PLAY) -> bool:
    """先删掉自己上一轮塞的那条，再追加一条新的——天然幂等。"""
    items = kwargs.get("items")
    if not isinstance(items, list) or not items:
        return False
    kept = [item for item in items if not _carries_marker(item, marker)]
    kept.append(system_item(block))
    kwargs["items"] = kept
    return True


def play_block(row: dict) -> str:
    return (
        f"{MARKER_PLAY}「{row['term']}」——{row['meaning']}\n"
        "如果这轮对话里正好顺得上，就随口用一次，像本来就会；"
        "不搭就别硬塞，也别把这段念出来。"
    )




# ---------------------------------------------------------------- 给命令用的薄封装






