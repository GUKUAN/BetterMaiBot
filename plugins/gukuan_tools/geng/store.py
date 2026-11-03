"""梗库的本地账本

四张表：
  titles   萌百 sitemap 的标题快照，每天和上一次做差集，差出来的就是新增条目
  terms    处理过的词，免得同一个词反复烧 LLM
  pending  待审队列 —— 置信度不够的梗先攒着，等 /梗收
  play     待玩队列 —— 觉得好玩的梗排在这里，等她哪句话顺得上就抛一次

全部走同步 sqlite3，跟瓜报一个路子。这个库只在采集轮次和命令里用，
不在消息钩子里碰，所以不用操心并发。
"""

import sqlite3
import time


def init_db(path: str) -> None:
    """建表，已经有的跳过。"""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS titles (
                   title TEXT PRIMARY KEY,
                   first_seen REAL NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS terms (
                   term TEXT PRIMARY KEY,
                   meaning TEXT DEFAULT '',
                   confidence REAL DEFAULT 0,
                   fun INTEGER DEFAULT 0,
                   source TEXT DEFAULT '',
                   state TEXT DEFAULT 'new',
                   ts REAL NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pending (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   term TEXT NOT NULL,
                   meaning TEXT DEFAULT '',
                   confidence REAL DEFAULT 0,
                   fun INTEGER DEFAULT 0,
                   source TEXT DEFAULT '',
                   from_ai INTEGER DEFAULT 0,
                   ts REAL NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS play (
                   term TEXT PRIMARY KEY,
                   meaning TEXT DEFAULT '',
                   fun INTEGER DEFAULT 0,
                   played INTEGER DEFAULT 0,
                   last_at REAL DEFAULT 0,
                   ts REAL NOT NULL
               )"""
        )


# ---------------------------------------------------------------- 标题快照

def sync_titles(path: str, titles: list) -> list:
    """把这一轮抓到的标题灌进去，返回库里有、这次新冒出来的。

    第一次跑（库是空的）只灌不返回 —— 不然十六万条会一次性全丢给 LLM。
    """
    now = time.time()
    with sqlite3.connect(path) as conn:
        known = {row[0] for row in conn.execute("SELECT title FROM titles")}
        incoming = [t for t in titles if t]
        if not known:
            conn.executemany(
                "INSERT OR IGNORE INTO titles (title, first_seen) VALUES (?, ?)",
                [(t, now) for t in incoming],
            )
            return []
        fresh = [t for t in incoming if t not in known]
        if fresh:
            conn.executemany(
                "INSERT OR IGNORE INTO titles (title, first_seen) VALUES (?, ?)",
                [(t, now) for t in fresh],
            )
    return fresh


def all_titles(path: str, limit: int = 0) -> list:
    """拿标题池，冷启动从这里挑。"""
    sql = "SELECT title FROM titles ORDER BY random()"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute(sql)]


def title_count(path: str) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT count(*) FROM titles").fetchone()
    return int(row[0] or 0) if row else 0


# ---------------------------------------------------------------- 词条状态

def known_terms(path: str) -> set:
    """已经处理过的词（含待审），采集时先排掉这些。"""
    out = set()
    with sqlite3.connect(path) as conn:
        for (term,) in conn.execute("SELECT term FROM terms"):
            out.add(term)
    return out


def mark_term(path: str, term: str, meaning: str, confidence: float,
              fun: int, source: str, state: str) -> None:
    """记一笔处理结果。state: pushed / pending / dropped。"""
    try:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO terms "
                "(term, meaning, confidence, fun, source, state, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (term, meaning[:300], float(confidence), int(fun), source, state, time.time()),
            )
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- 待审

def add_pending(path: str, term: str, meaning: str, confidence: float,
                fun: int, source: str, from_ai: bool = False) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO pending (term, meaning, confidence, fun, source, from_ai, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (term, meaning[:300], float(confidence), int(fun), source,
             1 if from_ai else 0, time.time()),
        )


def list_pending(path: str, limit: int = 15) -> list:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT id, term, meaning, confidence, fun, source, from_ai "
            "FROM pending ORDER BY confidence DESC, id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [
        {"id": r[0], "term": r[1], "meaning": r[2], "confidence": r[3],
         "fun": r[4], "source": r[5], "from_ai": r[6]}
        for r in rows
    ]


def take_pending(path: str, pid: int):
    """按编号取一条待审，取完就删。"""
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT id, term, meaning, confidence, fun, source FROM pending WHERE id = ?",
            (int(pid),),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM pending WHERE id = ?", (int(pid),))
    return {"id": row[0], "term": row[1], "meaning": row[2],
            "confidence": row[3], "fun": row[4], "source": row[5]}


def pending_count(path: str) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT count(*) FROM pending").fetchone()
    return int(row[0] or 0) if row else 0


# ---------------------------------------------------------------- 待玩

def add_play(path: str, term: str, meaning: str, fun: int) -> None:
    """入库成功的梗里挑好玩的那几个，排进待玩队列。"""
    try:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO play (term, meaning, fun, played, last_at, ts) "
                "VALUES (?, ?, ?, 0, 0, ?)",
                (term, meaning[:300], int(fun), time.time()),
            )
    except sqlite3.Error:
        pass


def next_play(path: str, min_fun: int, max_times: int, since: float = 0.0):
    """挑一条还没玩够的。优先没抛过的，同档随机。

    since 是「只要这段时间之后采到的」，老梗不主动抛——翻出三个月前的
    梗硬塞会很像复读机，那个交给原生 matcher 处理（被提到时才懂）。
    """
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT term, meaning, fun, played FROM play "
            "WHERE played < ? AND fun >= ? AND ts >= ? "
            "ORDER BY played ASC, random() LIMIT 1",
            (int(max_times), int(min_fun), float(since)),
        ).fetchone()
    if not row:
        return None
    return {"term": row[0], "meaning": row[1], "fun": row[2], "played": row[3]}


def bump_play(path: str, term: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE play SET played = played + 1, last_at = ? WHERE term = ?",
            (time.time(), term),
        )


def play_count(path: str, max_times: int, since: float = 0.0) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT count(*) FROM play WHERE played < ? AND ts >= ?",
            (int(max_times), float(since)),
        ).fetchone()
    return int(row[0] or 0) if row else 0


def last_play_at(path: str) -> float:
    """上一次抛梗是什么时候，冷却用。"""
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT max(last_at) FROM play").fetchone()
    return float(row[0] or 0) if row else 0.0


def stats(path: str, max_times: int, since: float = 0.0) -> dict:
    """给 /梗 命令看的一眼数字。"""
    with sqlite3.connect(path) as conn:
        pushed_today = conn.execute(
            "SELECT count(*) FROM terms WHERE state = 'pushed' AND ts > ?",
            (time.time() - 86400,),
        ).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM terms WHERE state = 'pushed'").fetchone()[0]
    return {
        "titles": title_count(path),
        "pushed": int(total or 0),
        "today": int(pushed_today or 0),
        "pending": pending_count(path),
        "play": play_count(path, max_times, since),
    }
