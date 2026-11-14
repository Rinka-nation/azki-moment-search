"""Statement search with confidence tiers.

自動字幕の補正には誤りが混ざりうるので、「どの段階のテキストで一致したか」で信頼度を分ける。

    tier 0  原文       字幕原文（text_raw）にそのまま一致。補正された発言でも原文の表記で探せる
                       （「水きち」→ 表示は「あずきち」に補正されているが、原文で一致）
    tier 1  補正       確定補正後（text: 人が確定した alias 置換）で一致
    tier 2  AI修正     LLM の修正を当てた文（text_ai、llmfix.py）でだけ一致（例: 一覧 → 一蘭）
    tier 3  読み       読みが「語の境界で」一致（例: かいたくしゃ → 開拓者、てつどう → 鉄道）
    tier 4  候補       候補補正後（text_fuzzy: 読みのあいまい一致）でのみ一致 … 誤検出の可能性あり
    tier 5  読み(部分) 読みが語の途中で一致（例: つり → 釣り堀）               … 誤検出の可能性あり
    tier 8  スパチャ読み 名前の連続（readout.py）の中でだけ一致

検索結果は「一致」タブ（tier 0-3）、「もしかして」タブ（tier 4-5）、「スパチャ読み」タブ（tier 8）に分ける。
「もしかして」は tier → 候補の読み編集距離の順に並べ、確からしいものを上に出す。
複数語の AND 検索では、各語の tier のうち最も信頼度の低いものをその発言の tier とする。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher

from azkims.reading import ReadingConverter
from azkims.related import Related
from azkims.textnorm import normalize_text

MIN_TERM_CHARS = 2
MAX_TERMS = 5
# 検索語全体（正規化した後）の長さの上限。字幕の 1 発言は長くても 94 文字、タイムスタンプの行で 100 文字を超えるのは
# 49,353 行のうち 3 行（2026-11-14、588 配信）。長い語は全文検索の照合が重く、手元でも 1,500 文字で 1 回 1.6 秒、
# 3,000 文字で 7 秒かかった（普通の検索は 0.1〜0.3 秒）。誰でも API を呼べるので断る（ISS-18）
MAX_QUERY_CHARS = 100
TRIGRAM = 3
# 字幕の開始時刻は発話よりわずかに遅れがちなので、少し前から再生する
PLAYBACK_LEAD_SEC = 2

TIER_RAW, TIER_CONFIRMED, TIER_AI, TIER_READING_WORD, TIER_CANDIDATE, TIER_READING_PART = range(6)
TIER_READOUT = 8  # 6, 7 は関連語
TIER_LABELS = {
    TIER_RAW: "原文",
    TIER_CONFIRMED: "補正",
    TIER_AI: "AI修正",
    TIER_READING_WORD: "読み",
    TIER_CANDIDATE: "候補",
    TIER_READING_PART: "読み(部分)",
    TIER_READOUT: "スパチャ読み",
}
# 公開用 DB は、原文が補正後と同じ発言の text_raw を空にしている（deploy.export_slim_db）
RAW_SQL = "COALESCE(NULLIF(u.text_raw, ''), u.text)"

TABS = {
    "match": (TIER_RAW, TIER_READING_WORD),
    "maybe": (TIER_CANDIDATE, TIER_READING_PART),
    "readout": (TIER_READOUT, TIER_READOUT),
    "all": (TIER_RAW, TIER_READING_PART),
}


def tab_count(tiers: dict[int, int], tab: str) -> int:
    """count_tiers の結果から、タブ（TABS）に入る件数。"""
    lo, hi = TABS[tab]
    return sum(n for tier, n in tiers.items() if lo <= tier <= hi)


SORTS = ("new", "old", "relevance")
# 関連語の一致（related.py）。tier の絞り込み・集計の対象外。WEAK は話題でない配信の weak の語
TIER_RELATED, TIER_RELATED_WEAK = 6, 7
RELATED_LABEL = "関連語"


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class SearchTerm:
    surface: str
    reading: str  # 空なら読みでは探さない


@dataclass(frozen=True)
class Candidate:
    surface: str
    canonical: str
    distance: int


@dataclass(frozen=True)
class Hit:
    utterance_id: int
    video_id: str
    title: str
    upload_date: str | None
    start_ms: int
    text_raw: str
    text: str
    text_fuzzy: str | None
    tier: int
    candidates: tuple[Candidate, ...] = ()
    text_ai: str | None = None
    related_words: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return TIER_LABELS.get(self.tier, RELATED_LABEL)

    @property
    def display_text(self) -> str:
        """表示用テキスト（ルビで原文を併記する前提）。AI修正 があればそれ、
        無ければ確定補正に補正候補も当てたもの。AI修正 は文全体を直したものなので候補は重ねない。"""
        return self.text_ai or self.text_fuzzy or self.text

    @property
    def segments(self) -> list[dict]:
        return ruby_segments(self.text_raw, self.text, self.text_fuzzy, ai=self.text_ai)

    @property
    def start_sec(self) -> int:
        return max(0, self.start_ms // 1000 - PLAYBACK_LEAD_SEC)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}&t={self.start_sec}s"

    @property
    def timestamp(self) -> str:
        s = self.start_ms // 1000
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _split_words(query: str, converter: ReadingConverter | None) -> list[str]:
    """空白で区切る。ただし空白を含む辞書語（Creating world, from A to Z）は 1 語のまま残す。"""
    if converter is None:
        return [w for w in query.split(" ") if w]
    words: list[str] = []
    pos = 0
    for m in converter.dictionary.find(query):
        if " " not in m.surface:
            continue
        words += query[pos : m.start].split(" ")
        words.append(m.surface)
        pos = m.end
    words += query[pos:].split(" ")
    return [w for w in words if w]


def _is_word_char(c: str) -> bool:
    """ルビの範囲を広げてよい文字: カタカナ（長音含む）・英数字。ひらがな・漢字では広げない。"""
    return "゠" <= c <= "ヿ" or c.isascii() and c.isalnum()


def _changed_ranges(a: str, b: str) -> list[tuple[int, int]]:
    """b のうち a から変わった範囲（b 上の位置）。"""
    sm = SequenceMatcher(None, a, b, autojunk=False)
    return [(j1, j2) for tag, _i1, _i2, j1, j2 in sm.get_opcodes() if tag in ("replace", "insert")]


def ruby_segments(
    raw: str,
    text: str,
    fuzzy: str | None = None,
    spans: list[tuple[int, int]] | None = None,
    ai: str | None = None,
) -> list[dict]:
    """表示テキスト（ai、fuzzy、text の順にあるもの）を、字幕原文との差分で区切る。

    spans: 表示テキスト上の辞書語の位置。補正箇所がその中にあれば語全体をルビの範囲にする
    （まりちゃん → まつりちゃん が「つ」1 文字の挿入に見えるのを、まつりちゃん 全体にする）。

    補正した箇所は {"text": 補正後, "raw": 原文, "kind": "confirmed" | "candidate" | "ai"}、
    それ以外は {"text": ...}。画面は補正箇所の上に原文をルビのように小さく出す。
    kind は、確定補正（辞書の alias）か補正候補（読みのあいまい一致）か LLM の修正（AI修正）か。
    """
    if ai:
        final, kind_changed = ai, "ai"
    else:
        final, kind_changed = fuzzy or text, "candidate"
    candidate = _changed_ranges(text, final) if final != text else []
    ops = SequenceMatcher(None, raw, final, autojunk=False).get_opcodes()
    # [i1, i2, j1, j2, changed]。変更どうしが隣り合えば 1 つにまとめる
    groups: list[list] = []
    for tag, i1, i2, j1, j2 in ops:
        changed = tag != "equal"
        if groups and changed and groups[-1][4]:
            groups[-1][1], groups[-1][3] = i2, j2
        else:
            groups.append([i1, i2, j1, j2, changed])

    # 補正箇所を、前後に続くカタカナ・英数字の並びの端まで広げる。
    # ホールドリー → ホロドリ が「ホ」+「ール→ロ」+「ドリ」+「ー→（なし）」に割れて
    # ルビが細切れになるのを防ぐ（ルビは「ホロドリ」全体の上に「ホールドリー」）
    spans = spans or []
    for k, g in enumerate(groups):
        if not g[4]:
            continue
        inside = [(a, b) for a, b in spans if a <= g[2] < b or a < g[3] <= b]
        lo = min((a for a, _ in inside), default=g[2])
        hi = max((b for _, b in inside), default=g[3])
        if k > 0 and not groups[k - 1][4]:
            left = groups[k - 1]
            n = 0
            while left[3] - n > left[2] and (
                left[3] - n > lo or _is_word_char(final[left[3] - n - 1])
            ):
                n += 1
            left[1] -= n
            left[3] -= n
            g[0] -= n
            g[2] -= n
        if k + 1 < len(groups) and not groups[k + 1][4]:
            right = groups[k + 1]
            n = 0
            while right[2] + n < right[3] and (
                right[2] + n < hi or _is_word_char(final[right[2] + n])
            ):
                n += 1
            right[0] += n
            right[2] += n
            g[1] += n
            g[3] += n
        # 広げた結果、次の補正箇所とくっついたら 1 つにまとめる
    joined: list[list] = []
    for g in groups:
        if g[2] == g[3] and g[0] == g[1]:
            continue  # 広げて空になった一致部分
        if joined and g[4] and joined[-1][4]:
            joined[-1][1], joined[-1][3] = g[1], g[3]
        else:
            joined.append(g)
    groups = joined

    out: list[dict] = []
    for i1, i2, j1, j2, changed in groups:
        if not changed:
            out.append({"text": final[j1:j2]})
        elif j2 > j1:
            kind = kind_changed if any(j1 < b and a < j2 for a, b in candidate) else "confirmed"
            out.append({"text": final[j1:j2], "raw": raw[i1:i2], "kind": kind})
        # 原文にだけあって表示に無い部分（削除のみ）は表示しない
    # 隣り合う普通の断片をつなぐ
    merged: list[dict] = []
    for seg in out:
        if merged and "raw" not in seg and "raw" not in merged[-1]:
            merged[-1]["text"] += seg["text"]
        else:
            merged.append(seg)
    return merged


def parse_query(
    query: str, converter: ReadingConverter | None, exact: bool = False
) -> list[SearchTerm]:
    """exact=True: 完全一致。入力全体を 1 語として（空白も含めて）表記だけで探す。読みは使わない。
    人が書いたタイムスタンプは書いたとおりの表記で探せるので、主にそのための指定。"""
    normalized = normalize_text(query)
    if len(normalized) > MAX_QUERY_CHARS:
        raise QueryError(
            f"検索語は {MAX_QUERY_CHARS} 文字までにしてください（今は {len(normalized)} 文字）"
        )
    if exact:
        phrase = normalized
        # 完全一致は読みを使わないので、2 文字だと索引で引けず全件を読む（Turso で「配信」が 1 回 145 万行・
        # 約 15 秒。通常の検索は 1.5 万行）。誰でも画面から実行できるので受け付けない（ISS-05、2026-11-11）
        if len(phrase) < TRIGRAM:
            raise QueryError(
                f"完全一致は {TRIGRAM} 文字以上にしてください（2 文字は完全一致を外すと探せます）"
            )
        return [SearchTerm(phrase, "")]
    words = _split_words(normalized, converter)
    if not words:
        raise QueryError("検索語を入力してください")
    if len(words) > MAX_TERMS:
        raise QueryError(f"検索語は {MAX_TERMS} 個までです")
    terms = []
    for w in words:
        if len(w) < MIN_TERM_CHARS:
            raise QueryError(f"検索語は {MIN_TERM_CHARS} 文字以上にしてください: {w!r}")
        reading = converter.reading(w) if converter else ""
        reading = reading if len(reading) >= MIN_TERM_CHARS else ""
        # 2 文字で読みも無い語（!? など記号だけ）は、どの索引でも引けず全件を読むので受け付けない（ISS-05）
        if len(w) < TRIGRAM and not reading:
            raise QueryError(f"2 文字の記号だけでは探せません: {w!r}")
        terms.append(SearchTerm(w, reading))
    return terms


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _phrase(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


_LIKE = "LIKE ? ESCAPE '\\'"


def _fts_arms(t: SearchTerm, surface_cols: str) -> list[str]:
    """trigram 索引で引ける腕。索引は 3 文字未満を引けない。"""
    arms = []
    if len(t.surface) >= TRIGRAM:
        arms.append(f"{surface_cols} : {_phrase(t.surface)}")
    if len(t.reading) >= TRIGRAM:
        arms.append(f"reading : {_phrase(t.reading)}")
    return arms


def _index_condition(
    t: SearchTerm, id_col: str, fts: str, surface_cols: str, words: str
) -> tuple[str, list] | None:
    """語に一致する行を索引だけで絞る条件。索引で引けない語は None（全件走査になる）。

    読みが 2 文字の語は語単位の読み索引を前方一致で引く（かな → かなた、かなけん）。
    語の途中の 2 文字（わかな）は拾わない。全件走査は検索 1 回で全行を読むので避ける。
    """
    subs, params = [], []
    arms = _fts_arms(t, surface_cols)
    if arms:
        subs.append(f"SELECT rowid FROM {fts} WHERE {fts} MATCH ?")
        params.append(" OR ".join(arms))
    if 0 < len(t.reading) < TRIGRAM:
        subs.append(f"SELECT rowid FROM {words} WHERE {words} MATCH ?")
        params.append(_phrase(t.reading) + " *")
    return (f"{id_col} IN ({' UNION '.join(subs)})", params) if subs else None


def _filter_stream(
    where: list[str],
    params: list,
    title: str | None,
    date_from: str | None,
    date_to: str | None,
) -> None:
    if title:
        where.append(f"s.title {_LIKE}")
        params.append(_like(normalize_text(title)))
    if date_from:
        where.append("s.upload_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("s.upload_date <= ?")
        params.append(date_to)


_SURFACE_COLS = "{text text_fuzzy text_orig text_ai}"
# build が発言の番号を振る順（pipeline.build が meta の utterance_order に書く）。検索の「新しい順」と同じ
UTTERANCE_ORDER = "upload_date DESC, video_id, start_ms"


def _use_match(terms: list[SearchTerm]) -> bool:
    """全部の語を utterances_fts の MATCH 1 回で引けるか（3 文字以上の表記か読み）。"""
    return all(_fts_arms(t, _SURFACE_COLS) and not 0 < len(t.reading) < TRIGRAM for t in terms)


# meta のこの値が "1" なら、発言は外していない配信のものだけ（pipeline.build が書き、reclassify が消す）
INCLUDED_ONLY = "included_only"


def _included_only(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (INCLUDED_ONLY,)).fetchone()
    return bool(row) and row[0] == "1"


def _ordered_ids(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = 'utterance_order'").fetchone()
    return bool(row) and row[0] == UTTERANCE_ORDER


def _matching_sql(
    terms: list[SearchTerm],
    title: str | None,
    date_from: str | None,
    date_to: str | None,
    tier_range: tuple[int, int] | None = None,
    streams: bool = True,
) -> tuple[str, list]:
    """Rows matching all terms at any tier, with `tier` and `rank_score` columns.

    tier_range を渡すと、その tier だけに WHERE で絞る（外側で包まずに並べ替えるとき用）。
    streams=False なら配信と結合しない（title と upload_date は NULL）。外した配信の発言が無い DB で、
    配信の絞り込みが無いときだけ使う。一致 1 件ごとに配信の行を読む分が減る。
    """
    params: list = []
    where: list[str] = ["s.excluded_reason IS NULL"] if streams else ["1"]

    surface_cols = _SURFACE_COLS
    use_match = _use_match(terms)
    conditions = [
        _index_condition(t, "u.id", "utterances_fts", surface_cols, "reading_words_fts")
        for t in terms
    ]
    use_index = not use_match and all(conditions)
    if use_match:
        expr = ["(" + " OR ".join(_fts_arms(t, surface_cols)) + ")" for t in terms]
        where.append("utterances_fts MATCH ?")
        params.append(" AND ".join(expr))
    elif use_index:
        for sql, values in conditions:
            where.append(sql)
            params += values
    else:
        for t in terms:
            arms = [
                f"utterances_fts.text {_LIKE}",
                f"utterances_fts.text_fuzzy {_LIKE}",
                f"utterances_fts.text_orig {_LIKE}",
                f"utterances_fts.text_ai {_LIKE}",
            ]
            params += [_like(t.surface)] * 4
            if t.reading:
                arms.append(f"utterances_fts.reading {_LIKE}")
                params.append(_like(t.reading))
            where.append("(" + " OR ".join(arms) + ")")

    _filter_stream(where, params, title, date_from, date_to)

    tier_exprs, tier_params = [], []
    for t in terms:
        word_case = ""
        if t.reading:
            word_case = (
                "WHEN u.id IN (SELECT rowid FROM reading_words_fts WHERE reading_words_fts MATCH ?) "
                f"THEN {TIER_READING_WORD} "
            )
        # 名前の連続を除いた残りに語が無ければ スパチャ読み。大半の発言は text_kept が NULL で素通りする
        kept = "u.text_kept IS NOT NULL AND instr(lower(u.text_kept), ?) = 0"
        tier_params.append(t.surface.lower())
        if t.reading:
            kept += " AND instr(u.reading_kept, ?) = 0"
            tier_params.append(t.reading)
        tier_exprs.append(
            f"CASE WHEN {kept} THEN {TIER_READOUT} "
            f"WHEN instr(lower({RAW_SQL}), ?) > 0 THEN {TIER_RAW} "
            f"WHEN instr(lower(u.text), ?) > 0 THEN {TIER_CONFIRMED} "
            f"WHEN instr(lower(COALESCE(u.text_ai, '')), ?) > 0 THEN {TIER_AI} "
            f"{word_case}"
            f"WHEN instr(lower(COALESCE(u.text_fuzzy, '')), ?) > 0 THEN {TIER_CANDIDATE} "
            f"ELSE {TIER_READING_PART} END"
        )
        tier_params += [t.surface.lower()] * 3
        if t.reading:
            tier_params.append(_phrase(t.reading))
        tier_params.append(t.surface.lower())
    # max() は引数 1 つだと集約関数になるので、1 語のときは式をそのまま使う
    tier_sql = tier_exprs[0] if len(tier_exprs) == 1 else f"max({', '.join(tier_exprs)})"
    if tier_range:
        where.append(f"({tier_sql}) BETWEEN ? AND ?")
        params += [*tier_params, *tier_range]
    rank_sql = "bm25(utterances_fts)" if use_match else "length(u.text)"
    source = (
        "utterances u"
        if use_index
        else "utterances_fts JOIN utterances u ON u.id = utterances_fts.rowid"
    )

    stream_cols = "s.title, s.upload_date" if streams else "NULL AS title, NULL AS upload_date"
    join = "JOIN streams s ON s.video_id = u.video_id" if streams else ""
    sql = f"""
        SELECT u.id, u.video_id, {stream_cols}, u.start_ms,
               {RAW_SQL} AS text_raw, u.text, u.text_fuzzy, u.text_ai, u.fuzzy_distance,
               {tier_sql} AS tier, {rank_sql} AS rank_score
        FROM {source}
        {join}
        WHERE {" AND ".join(where)}
    """
    return sql, [*tier_params, *params]


def _prepare(terms: list[SearchTerm], use_reading: bool) -> list[SearchTerm]:
    return terms if use_reading else [SearchTerm(t.surface, "") for t in terms]


def count_tiers(
    conn: sqlite3.Connection,
    terms: list[SearchTerm],
    *,
    title: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    use_reading: bool = True,
) -> dict[int, int]:
    streams = bool(title or date_from or date_to) or not _included_only(conn)
    inner, params = _matching_sql(
        _prepare(terms, use_reading), title, date_from, date_to, streams=streams
    )
    counts = dict.fromkeys(TIER_LABELS, 0)
    counts.update(
        conn.execute(f"SELECT tier, COUNT(*) FROM ({inner}) GROUP BY tier", params).fetchall()
    )
    return counts


def search(
    conn: sqlite3.Connection,
    terms: list[SearchTerm],
    *,
    tab: str = "match",
    title: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "new",
    limit: int = 20,
    offset: int = 0,
    use_reading: bool = True,
) -> list[Hit]:
    if tab not in TABS:
        raise QueryError(f"unknown tab: {tab}")
    if sort not in SORTS:
        raise QueryError(f"unknown sort: {sort}")
    inner, params = _matching_sql(_prepare(terms, use_reading), title, date_from, date_to)

    by_sort = {
        "new": UTTERANCE_ORDER,
        "old": "upload_date ASC, video_id, start_ms",
        "relevance": "rank_score, upload_date DESC",
    }[sort]
    if tab == "match" and sort != "relevance":
        order = by_sort  # 原文一致・確定補正はどちらも確からしいので、指定の並びのまま
    else:
        # 信頼度の低いものほど下へ: tier → 候補の読み距離 → 指定の並び
        order = f"tier, COALESCE(fuzzy_distance, 99), {by_sort}"

    lo, hi = TABS[tab]
    if order == UTTERANCE_ORDER and _use_match(terms) and _ordered_ids(conn):
        # 発言の番号順がそのまま新しい順。索引の番号で並べると、一致を全部読まずに limit 件で止まる
        # （u.id や外側の列で並べると止まらない。DESIGN.md 10.5）
        inner, params = _matching_sql(
            _prepare(terms, use_reading), title, date_from, date_to, tier_range=(lo, hi)
        )
        rows = conn.execute(
            f"{inner} ORDER BY utterances_fts.rowid LIMIT ? OFFSET ?", [*params, limit, offset]
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM ({inner}) WHERE tier BETWEEN ? AND ? ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, lo, hi, limit, offset],
        ).fetchall()

    candidates: dict[int, list[Candidate]] = {}
    ids = [r["id"] for r in rows if r["text_fuzzy"] is not None]
    if ids:
        marks = ",".join("?" * len(ids))
        for c in conn.execute(
            "SELECT utterance_id, surface, canonical, distance FROM fuzzy_matches "
            f"WHERE utterance_id IN ({marks})",
            ids,
        ):
            candidates.setdefault(c["utterance_id"], []).append(
                Candidate(c["surface"], c["canonical"], c["distance"])
            )

    return [
        Hit(
            utterance_id=r["id"],
            video_id=r["video_id"],
            title=r["title"],
            upload_date=r["upload_date"],
            start_ms=r["start_ms"],
            text_raw=r["text_raw"],
            text=r["text"],
            text_fuzzy=r["text_fuzzy"],
            tier=r["tier"],
            candidates=tuple(candidates.get(r["id"], ())),
            text_ai=r["text_ai"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class TimestampHit:
    entry_id: int
    video_id: str
    title: str
    upload_date: str | None
    sec: int
    text: str
    quotes: tuple[str, ...]
    tier: int
    related_words: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return TIER_LABELS.get(self.tier, RELATED_LABEL)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}&t={self.sec}s"

    @property
    def timestamp(self) -> str:
        s = self.sec
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def search_timestamps(
    conn: sqlite3.Connection,
    terms: list[SearchTerm],
    *,
    title: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
    use_reading: bool = True,
    count: bool = True,
    sort: str = "new",
) -> tuple[list[TimestampHit], int | None]:
    """人が書いたタイムスタンプコメントを検索する。(hits, 総件数) を返す。

    表記一致（tier 0）→ 語単位の読み一致（tier 3）→ 部分的な読み一致（tier 5）の順。同じ tier の中は sort の順
    （古い順のときだけ配信日の古い順。2026-11-11、ISS-09。関連度順は画面から外したので新しい順と同じ）。
    どちらも一致をすべて読むので、要らない方は省く: count=False なら総件数は None、limit=0 なら hits は空。
    """
    order = _ts_order(sort, "upload_date", "sec")
    terms = _prepare(terms, use_reading)
    where = ["s.excluded_reason IS NULL"]
    params: list = []
    conditions = [_index_condition(t, "e.id", "ts_fts", "text", "ts_words_fts") for t in terms]
    use_index = all(conditions)
    if use_index:
        for sql, values in conditions:
            where.append(sql)
            params += values
    else:
        for t in terms:
            arms = [f"ts_fts.text {_LIKE}"]
            params.append(_like(t.surface))
            if t.reading:
                arms.append(f"ts_fts.reading {_LIKE}")
                params.append(_like(t.reading))
            where.append("(" + " OR ".join(arms) + ")")
    _filter_stream(where, params, title, date_from, date_to)

    tier_exprs, tier_params = [], []
    for t in terms:
        case = f"CASE WHEN instr(lower(e.text), ?) > 0 THEN {TIER_RAW} "
        tier_params.append(t.surface.lower())
        if t.reading:
            case += (
                "WHEN e.id IN (SELECT rowid FROM ts_words_fts WHERE ts_words_fts MATCH ?) "
                f"THEN {TIER_READING_WORD} "
            )
            tier_params.append(_phrase(t.reading))
        tier_exprs.append(case + f"ELSE {TIER_READING_PART} END")
    tier_sql = tier_exprs[0] if len(tier_exprs) == 1 else f"max({', '.join(tier_exprs)})"

    inner = f"""
        SELECT e.id, e.video_id, s.title, s.upload_date, e.sec, e.text, e.quotes,
               {tier_sql} AS tier
        FROM {"ts_entries e" if use_index else "ts_fts JOIN ts_entries e ON e.id = ts_fts.rowid"}
        JOIN streams s ON s.video_id = e.video_id
        WHERE {" AND ".join(where)}
    """
    all_params = [*tier_params, *params]
    total = (
        conn.execute(f"SELECT COUNT(*) FROM ({inner})", all_params).fetchone()[0] if count else None
    )
    rows = (
        conn.execute(
            f"SELECT * FROM ({inner}) ORDER BY tier, {order} LIMIT ? OFFSET ?",
            [*all_params, limit, offset],
        ).fetchall()
        if limit
        else []
    )
    hits = [
        TimestampHit(
            entry_id=r["id"],
            video_id=r["video_id"],
            title=r["title"],
            upload_date=r["upload_date"],
            sec=r["sec"],
            text=r["text"],
            quotes=tuple(q for q in r["quotes"].split("\n") if q),
            tier=r["tier"],
        )
        for r in rows
    ]
    return hits, total


def _ts_order(sort: str, date_col: str, sec_col: str) -> str:
    if sort not in SORTS:
        raise QueryError(f"unknown sort: {sort}")
    direction = "ASC" if sort == "old" else "DESC"
    return f"{date_col} {direction}, {sec_col}"


@dataclass(frozen=True)
class RelatedQuery:
    """タグ（ホロメン・ユニット）の検索。他の語があれば、その語も含む発言に絞る。"""

    tag: str
    terms: list[
        SearchTerm
    ]  # 検索語全部（もしかして では、これに一致する発言は 一致 タブにあるので除く）
    rest: list[SearchTerm]  # タグ以外の語


def related_query(terms: list[SearchTerm], related: Related) -> RelatedQuery | None:
    for t in terms:
        tag = related.tag_of(t.surface)
        if tag is not None:
            return RelatedQuery(tag.name, terms, [x for x in terms if x is not t])
    return None


def _related_from(
    q: RelatedQuery,
    confident: int,
    word: str | None,
    title: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list]:
    where = ["r.tag = ?", "r.confident = ?", "s.excluded_reason IS NULL"]
    params: list = [q.tag, confident]
    if not confident:
        # もしかして に足す分は、一致 タブと重ねない。関連語タブは検索した呼び名そのものの発言も含める
        # （ホロメンから探す で一番呼ぶ呼び名を検索したとき、その人の発言が全部まとまる。2026-11-10 霧島リンカさん）
        inner, inner_params = _matching_sql(q.terms, None, None, None)
        where.append(f"u.id NOT IN (SELECT id FROM ({inner}))")
        params += inner_params
    if q.rest:
        rest, rest_params = _matching_sql(q.rest, None, None, None)
        where.append(f"u.id IN (SELECT id FROM ({rest}))")
        params += rest_params
    if word:
        where.append("r.word = ?")
        params.append(word)
    _filter_stream(where, params, title, date_from, date_to)
    sql = f"""
        FROM related_hits r
        JOIN utterances u ON u.id = r.utterance_id
        JOIN streams s ON s.video_id = u.video_id
        WHERE {" AND ".join(where)}
    """
    return sql, params


def count_related(
    conn: sqlite3.Connection,
    q: RelatedQuery,
    confident: int,
    *,
    word: str | None = None,
    title: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    sql, params = _related_from(q, confident, word, title, date_from, date_to)
    return conn.execute(f"SELECT COUNT(DISTINCT u.id) {sql}", params).fetchone()[0]


def related_word_counts(
    conn: sqlite3.Connection,
    q: RelatedQuery,
    *,
    title: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[tuple[str, int]]:
    """関連語タブの語ごとの件数（多い順）。"""
    sql, params = _related_from(q, 1, None, title, date_from, date_to)
    return [
        (r[0], r[1])
        for r in conn.execute(
            f"SELECT r.word, COUNT(DISTINCT u.id) n {sql} GROUP BY r.word ORDER BY n DESC, r.word",
            params,
        )
    ]


def search_related(
    conn: sqlite3.Connection,
    q: RelatedQuery,
    confident: int,
    *,
    word: str | None = None,
    title: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "new",
    limit: int = 20,
    offset: int = 0,
) -> list[Hit]:
    if sort not in SORTS:
        raise QueryError(f"unknown sort: {sort}")
    sql, params = _related_from(q, confident, word, title, date_from, date_to)
    order = {
        "new": "s.upload_date DESC, u.video_id, u.start_ms",
        "old": "s.upload_date ASC, u.video_id, u.start_ms",
        "relevance": "COUNT(DISTINCT r.word) DESC, s.upload_date DESC",
    }[sort]
    rows = conn.execute(
        f"""
        SELECT u.id, u.video_id, s.title, s.upload_date, u.start_ms,
               {RAW_SQL} AS text_raw, u.text, u.text_fuzzy, u.text_ai,
               group_concat(DISTINCT r.word) AS words
        {sql} GROUP BY u.id ORDER BY {order} LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [
        Hit(
            utterance_id=r["id"],
            video_id=r["video_id"],
            title=r["title"],
            upload_date=r["upload_date"],
            start_ms=r["start_ms"],
            text_raw=r["text_raw"],
            text=r["text"],
            text_fuzzy=r["text_fuzzy"],
            tier=TIER_RELATED if confident else TIER_RELATED_WEAK,
            related_words=tuple(r["words"].split(",")),
            text_ai=r["text_ai"],
        )
        for r in rows
    ]


def search_related_ts(
    conn: sqlite3.Connection,
    q: RelatedQuery,
    *,
    word: str | None = None,
    title: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
    count: bool = True,
    sort: str = "new",
) -> tuple[list[TimestampHit], int | None]:
    """タイムスタンプの関連語の一致（確からしいものだけ）。(hits, 総件数) を返す。

    count=False なら総件数は None、limit=0 なら hits は空（search_timestamps と同じ）。
    """
    order = _ts_order(sort, "s.upload_date", "e.sec")
    where = ["r.tag = ?", "r.confident = 1", "s.excluded_reason IS NULL"]
    params: list = [q.tag]
    # 検索語が全部書かれているタイムスタンプは、タイムスタンプ検索の本体で出ている
    missing = " OR ".join("instr(lower(e.text), ?) = 0" for _ in q.terms)
    where.append(f"({missing})")
    params += [t.surface.lower() for t in q.terms]
    for t in q.rest:
        where.append("instr(lower(e.text), ?) > 0")
        params.append(t.surface.lower())
    if word:
        where.append("r.word = ?")
        params.append(word)
    _filter_stream(where, params, title, date_from, date_to)
    sql = f"""
        FROM related_ts_hits r
        JOIN ts_entries e ON e.id = r.entry_id
        JOIN streams s ON s.video_id = e.video_id
        WHERE {" AND ".join(where)}
    """
    total = (
        conn.execute(f"SELECT COUNT(DISTINCT e.id) {sql}", params).fetchone()[0] if count else None
    )
    rows = (
        conn.execute(
            f"""
            SELECT e.id, e.video_id, s.title, s.upload_date, e.sec, e.text, e.quotes,
                   group_concat(DISTINCT r.word) AS words
            {sql} GROUP BY e.id ORDER BY {order} LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        if limit
        else []
    )
    hits = [
        TimestampHit(
            entry_id=r["id"],
            video_id=r["video_id"],
            title=r["title"],
            upload_date=r["upload_date"],
            sec=r["sec"],
            text=r["text"],
            quotes=tuple(x for x in r["quotes"].split("\n") if x),
            tier=TIER_RELATED,
            related_words=tuple(r["words"].split(",")),
        )
        for r in rows
    ]
    return hits, total
