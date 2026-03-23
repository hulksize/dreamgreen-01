import os
import re
import time
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, session, redirect, url_for, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dreamgreen-flask-secret-2026")

# Vercel(HTTPS) 환경에서는 Secure 쿠키가 필요하다.
# VERCEL 환경변수는 Vercel 플랫폼이 자동으로 "1"로 설정한다.
if os.environ.get("VERCEL"):
    app.config["SESSION_COOKIE_SECURE"]   = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ─── 서버 메모리 캐시 ─────────────────────────────────────────────────────────
# { userid: { "html": str, "ts": float } }
# Vercel serverless 에서는 warm 인스턴스 재사용 시에만 유효하며,
# 콜드 스타트 시 초기화된다. 로컬/Render 환경에서는 완전히 유지됨.
_CACHE: dict[str, dict] = {}
CACHE_TTL = 1800  # 캐시 유효시간: 30분 (초 단위)


def _cache_get(userid: str) -> "str | None":
    entry = _CACHE.get(userid)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["html"]
    return None


def _cache_set(userid: str, html: str) -> None:
    _CACHE[userid] = {"html": html, "ts": time.time()}


def _cache_clear(userid: str) -> None:
    _CACHE.pop(userid, None)


def _cache_age_seconds(userid: str) -> int:
    """캐시가 생성된 지 몇 초 지났는지. 캐시 없으면 -1."""
    entry = _CACHE.get(userid)
    return int(time.time() - entry["ts"]) if entry else -1

BASE_URL = "http://dreamgreen.net"
LOGIN_URL = f"{BASE_URL}/login/loginaction.php"
HULIST_URL = f"{BASE_URL}/member/hulist.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/index.html?mode=main",
}

ERROR_KEYWORDS = [
    "아이디가 입력되지 않았거나",
    "패스워드가 다릅니다",
    "로그인 상태가 정상이 아닙니다",
    "다시 로그인",
]


def detect_login_fields():
    """메인 페이지에서 로그인 필드명을 자동 감지한다."""
    try:
        resp = requests.get(f"{BASE_URL}/index.html?mode=main", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form", {"action": re.compile(r"loginaction", re.I)})
        if form:
            fields = {}
            for inp in form.find_all("input"):
                name = inp.get("name", "")
                itype = inp.get("type", "text").lower()
                if itype == "password":
                    fields["pw_field"] = name
                elif itype == "text" and name:
                    fields["id_field"] = name
            return fields
    except Exception:
        pass
    return {"id_field": "userid", "pw_field": "userpw"}


def try_login(userid, userpw):
    """dreamgreen.net에 로그인하고 쿠키를 반환한다."""
    s = requests.Session()
    fields = detect_login_fields()
    payload = {
        fields.get("id_field", "userid"): userid,
        fields.get("pw_field", "userpw"): userpw,
    }
    try:
        resp = s.post(LOGIN_URL, data=payload, headers=HEADERS, timeout=15, allow_redirects=True)
        for kw in ERROR_KEYWORDS:
            if kw in resp.text:
                return None, f"로그인 실패: {kw}"
        return dict(s.cookies), None
    except requests.RequestException as e:
        return None, f"서버 연결 오류: {e}"


def _fetch_from_site(userid: str, cookies: dict) -> "tuple[str, str | None]":
    """dreamgreen.net 에서 hulist.php 원본 HTML을 직접 가져온다 (캐시 미사용)."""
    try:
        resp = requests.get(
            f"{HULIST_URL}?userid={userid}",
            headers=HEADERS,
            cookies=cookies,
            timeout=20,
        )
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text, None
    except Exception as e:
        return "", f"페이지 로드 오류: {e}"


def _get_raw_html(userid: str, cookies: dict, force: bool = False) -> "tuple[str, str | None]":
    """
    원본 HTML 반환. 캐시 우선.
    force=True 면 캐시를 무시하고 재요청 후 갱신.
    """
    if not force:
        cached = _cache_get(userid)
        if cached is not None:
            return cached, None

    html, err = _fetch_from_site(userid, cookies)
    if err:
        return "", err

    _cache_set(userid, html)
    return html, None


def _html_to_display(raw_html: str) -> "tuple[str | None, str | None]":
    """원본 HTML → 화면 표시용 가공 (script 제거, URL 절대화)."""
    for kw in ERROR_KEYWORDS:
        if kw in raw_html:
            return None, kw

    soup = BeautifulSoup(raw_html, "lxml")
    for tag in soup.find_all("script"):
        tag.decompose()

    for tag in soup.find_all(True):
        for attr in ("src", "href", "action"):
            val = tag.get(attr, "")
            if not val:
                continue
            if val.startswith(("http://", "https://", "#", "data:", "javascript:", "mailto:")):
                continue
            if val.startswith("./"):
                tag[attr] = BASE_URL + val[1:]
            elif val.startswith("/"):
                tag[attr] = BASE_URL + val
            else:
                tag[attr] = BASE_URL + "/" + val

    body = soup.find("body")
    return (body.decode_contents() if body else str(soup)), None


def _resolve_auth():
    """
    세션 또는 HTTP Basic Auth 에서 (userid, cookies) 를 추출한다.

    우선순위:
      1) Flask 세션 (브라우저 로그인 후 쿠키 유지)
      2) HTTP Basic Auth 헤더 (curl / 직접 API 호출 / Vercel 테스트)
         예) curl -u myid:mypw "https://xxx.vercel.app/api/debug?q=안용운"

    반환: (userid, cookies, error_msg)
      error_msg 가 None 이 아니면 인증 실패.
    """
    # ── 1) 세션 ──────────────────────────────────────────
    if "userid" in session:
        return session["userid"], session.get("cookies", {}), None

    # ── 2) HTTP Basic Auth ────────────────────────────────
    auth = request.authorization
    if auth and auth.username and auth.password:
        cookies, err = try_login(auth.username, auth.password)
        if err:
            return None, None, err
        return auth.username, cookies, None

    return None, None, "로그인 필요"


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        userid = request.form.get("userid", "").strip()
        userpw = request.form.get("userpw", "").strip()
        if not userid or not userpw:
            error = "아이디와 비밀번호를 입력하세요."
        else:
            cookies, err = try_login(userid, userpw)
            if err:
                error = err
            else:
                session["userid"] = userid
                session["cookies"] = cookies
                return redirect(url_for("members"))
    return render_template("login.html", error=error)


@app.route("/members")
def members():
    """
    계보도 페이지를 즉시 반환한다.
    실제 HTML 데이터는 클라이언트 JS 가 /api/tree 를 비동기 호출해서 가져온다.
    → 로그인 후 페이지가 즉시 열리고, 계보도가 비동기로 로드됨.
    """
    if "userid" not in session:
        return redirect(url_for("login"))
    userid = session["userid"]
    return render_template("members.html", userid=userid)


def _fetch_hulist_html(userid: str, cookies: dict, force: bool = False) -> str:
    """원본 HTML 반환 (캐시 우선). /api/debug, /api/html 에서 사용."""
    html, err = _get_raw_html(userid, cookies, force=force)
    if err:
        raise RuntimeError(err)
    return html


def _parse_members(html):
    """
    3가지 전략으로 회원 박스 헤더 td를 찾고,
    결과가 있는 첫 전략으로 파싱한다.

    전략 우선순위:
      1) bgcolor="#001e5f" 속성 td
      2) style 속성에 #001e5f 포함 td
      3) style 속성에 background 포함 td (폴백)
    """
    soup    = BeautifulSoup(html, "lxml")
    DATE_RE = re.compile(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}")

    # ── 전략별 헤더 td 후보 탐색 ─────────────────────────
    def by_bgcolor(color):
        return soup.find_all("td", attrs={"bgcolor": re.compile(color, re.I)})

    def by_style_color(color):
        return [
            td for td in soup.find_all("td")
            if color.lower() in (td.get("style") or "").lower()
        ]

    def by_style_bg():
        return [
            td for td in soup.find_all("td")
            if "background" in (td.get("style") or "").lower()
        ]

    s1 = by_bgcolor("001e5f")
    s2 = by_style_color("#001e5f") if not s1 else []
    s3 = by_style_bg()             if not s1 and not s2 else []

    if s1:
        header_tds    = s1
        strategy_used = "bgcolor=#001e5f"
    elif s2:
        header_tds    = s2
        strategy_used = "style contains #001e5f"
    elif s3:
        header_tds    = s3
        strategy_used = "style contains background"
    else:
        # 전략 실패 → 빈 결과 + 진단 정보 반환
        all_tds = soup.find_all("td")
        sample  = [
            {"bgcolor": td.get("bgcolor", ""), "style": (td.get("style") or "")[:80]}
            for td in all_tds[:30]
        ]
        return [], "no_strategy_matched", sample

    # ── 회원 박스별 파싱 ──────────────────────────────────
    # raw_text 구조: "|\n이름\n아이디\n회원종류\n매출액\n가입일\n|"
    # → \n 으로 split 후 인덱스로 직접 매핑
    #   [0]="|"  [1]=name  [2]=id  [3]=kind  [4]=sales  [5]=date  [6]="|"
    #
    # 중복 처리 방침: 동일 id 가 여러 번 등장하면 마지막 항목을 사용한다.
    # ─ 이름이 다르더라도 id 가 같으면 같은 계정의 입력 오류로 간주
    # ─ 예) id="ann020" 에 "안용운 20" / "안정원20" 두 이름이 있을 때
    #     마지막으로 파싱된 "안정원20" 만 남긴다.
    # ─ id 가 빈 문자열인 경우는 별도 목록에 모두 보존한다.
    by_id: dict[str, dict] = {}   # id → 마지막 파싱 결과 (last-wins)
    no_id: list[dict]      = []   # id 없는 항목은 그대로 보존

    for header_td in header_tds:
        box_table = header_td.find_parent("table")
        if not box_table:
            continue

        box_text = box_table.get_text(separator="\n", strip=True)

        # 너무 크면 외부 레이아웃 테이블 → 스킵
        if len(box_text) > 2000:
            continue

        # ── 줄바꿈 split 파싱 ──────────────────────────────
        lines = [l.strip() for l in box_text.split("\n") if l.strip()]
        data  = [l for l in lines if l != "|"]

        if len(data) < 5:
            continue

        # id 는 반드시 str() 로 저장 — 숫자로 변환하지 않음
        name  = str(data[0]).strip() if len(data) > 0 else ""
        mid   = str(data[1]).strip() if len(data) > 1 else ""
        kind  = str(data[2]).strip() if len(data) > 2 else ""
        sales = str(data[3]).strip() if len(data) > 3 else ""
        date  = str(data[4]).strip() if len(data) > 4 else ""

        if not DATE_RE.search(date):
            dm = DATE_RE.search(box_text)
            date = dm.group(0) if dm else date

        entry = {
            "name"    : name,
            "id"      : mid,
            "kind"    : kind,
            "sales"   : sales,
            "date"    : date,
            "raw_text": box_text,
        }

        if mid:
            # 동일 id → 덮어쓰기(last-wins): 마지막에 파싱된 항목이 최종값
            by_id[mid] = entry
        else:
            no_id.append(entry)

    # id 있는 것: dict 값(삽입 순서 유지, Python 3.7+) + id 없는 것
    members = list(by_id.values()) + no_id

    # ── 문자열 기준 정렬: 이름 → 아이디 오름차순 ──────────
    members.sort(key=lambda m: (m["name"].lower(), m["id"].lower()))
    for i, m in enumerate(members, 1):
        m["index"] = i

    return members, strategy_used, None


# ─── /api/html  ──────────────────────────────────────────────────────────────

@app.route("/api/html")
def api_html():
    """
    hulist.php 원본 HTML을 text/plain 으로 그대로 출력한다.
    세션 또는 HTTP Basic Auth 로 인증한다.
    """
    userid, cookies, err = _resolve_auth()
    if err:
        return jsonify({"error": err}), 401

    try:
        html = _fetch_hulist_html(userid, cookies)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return html, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ─── /api/debug  ─────────────────────────────────────────────────────────────

@app.route("/api/debug")
def api_debug():
    """
    hulist.php 원본 HTML을 파싱해 회원 데이터를 JSON으로 반환한다.
    세션 또는 HTTP Basic Auth 로 인증한다.

    쿼리 파라미터:
      q         - 이름 또는 아이디에 포함된 문자열로 필터 (공백 무시, 대소문자 무시)
                  예) /api/debug?q=안정원
      date      - 가입일에 포함된 문자열로 필터
                  예) /api/debug?date=2026-02
      raw       - 1 이면 raw_text 포함 (기본 생략)
      dup_name  - 1 이면 동일 이름이 여러 id 에 등록된 항목만 출력
                  예) /api/debug?q=안용운&dup_name=1  → 중복 이름 원인 탐색
    """
    userid, cookies, err = _resolve_auth()
    if err:
        return jsonify({"error": err}), 401

    try:
        html = _fetch_hulist_html(userid, cookies)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    for kw in ERROR_KEYWORDS:
        if kw in html:
            return jsonify({"error": kw, "code": 403}), 403

    members, strategy, diag = _parse_members(html)

    # ── 쿼리 파라미터 파싱 ────────────────────────────────
    q        = request.args.get("q",        "").strip()
    date_f   = request.args.get("date",     "").strip()
    show_raw = request.args.get("raw",      "0") == "1"
    dup_name = request.args.get("dup_name", "0") == "1"

    def normalize(s):
        """공백 제거 + 소문자"""
        return re.sub(r"\s+", "", s or "").lower()

    q_norm    = normalize(q)
    date_norm = date_f.lower()

    # ── 필터링 ───────────────────────────────────────────
    filtered = []
    for m in members:
        name_n = normalize(m["name"])
        id_n   = normalize(m["id"])
        date_n = (m["date"] or "").lower()

        match_q    = (not q_norm)    or (q_norm in name_n) or (q_norm in id_n)
        match_date = (not date_norm) or (date_norm in date_n)

        if match_q and match_date:
            entry = {k: v for k, v in m.items() if k != "raw_text" or show_raw}
            entry["_match_reason"] = []
            if q_norm:
                if q_norm in name_n: entry["_match_reason"].append(f"이름에 '{q}' 포함")
                if q_norm in id_n:   entry["_match_reason"].append(f"아이디에 '{q}' 포함")
            if date_norm and date_norm in date_n:
                entry["_match_reason"].append(f"날짜에 '{date_f}' 포함")
            filtered.append(entry)

    # ── dup_name=1: 동일 이름이 여러 id 에 등록된 항목만 출력 ─────
    # 예) /api/debug?q=안용운&dup_name=1  →  "안용운"을 이름으로 가진
    #     id 목록 + 각 id 에 실제 등록된 이름을 함께 출력해 오입력 탐색
    if dup_name:
        base_list = filtered if (q or date_f) else members
        # 이름 기준 그룹핑 (공백 정규화 후 비교)
        from collections import defaultdict
        name_groups: dict = defaultdict(list)
        for m in base_list:
            key = normalize(m["name"])
            name_groups[key].append(m)
        # 같은 normalized name 을 가진 id 가 2개 이상인 그룹만
        dup_entries = []
        for key, group in sorted(name_groups.items()):
            if len(group) >= 2:
                dup_entries.append({
                    "name_normalized": key,
                    "count": len(group),
                    "entries": [
                        {k: v for k, v in m.items() if k != "raw_text" or show_raw}
                        for m in group
                    ],
                })
        return jsonify({
            "login_user"      : userid,
            "strategy_used"   : strategy,
            "filter"          : {"q": q, "date": date_f, "dup_name": True},
            "total_dup_groups": len(dup_entries),
            "dup_groups"      : dup_entries,
        })

    is_filtered = bool(q or date_f)

    return jsonify({
        "login_user"    : userid,
        "strategy_used" : strategy,
        "filter"        : {"q": q, "date": date_f} if is_filtered else None,
        "total_all"     : len(members),
        "total_matched" : len(filtered),
        "members"       : filtered if is_filtered else [
            {k: v for k, v in m.items() if k != "raw_text" or show_raw}
            for m in members
        ],
        "diagnostics"   : diag,
    })


# ─── /api/tree  ──────────────────────────────────────────────────────────────

@app.route("/api/tree")
def api_tree():
    """
    계보도 표시용 가공 HTML을 JSON으로 반환한다. 캐시 우선.
    클라이언트 JS가 비동기로 호출한다.

    응답:
      { "content": "<body 내부 HTML>",
        "cached": true/false,
        "cache_age_sec": 123 }
    """
    userid, cookies, err = _resolve_auth()
    if err:
        return jsonify({"error": err}), 401

    cache_age = _cache_age_seconds(userid)
    raw_html, err = _get_raw_html(userid, cookies)
    if err:
        return jsonify({"error": err}), 500

    content, err = _html_to_display(raw_html)
    if err:
        return jsonify({"error": err}), 403

    was_cached = cache_age >= 0
    return jsonify({
        "content"      : content,
        "cached"       : was_cached,
        "cache_age_sec": cache_age,
    })


# ─── /api/refresh  ───────────────────────────────────────────────────────────

@app.route("/api/refresh")
def api_refresh():
    """
    서버 캐시를 강제로 초기화하고 dreamgreen.net 에서 최신 데이터를 가져온다.
    응답: { "ok": true, "message": "..." }
    """
    userid, cookies, err = _resolve_auth()
    if err:
        return jsonify({"error": err}), 401

    _cache_clear(userid)

    raw_html, err = _get_raw_html(userid, cookies, force=True)
    if err:
        return jsonify({"error": err}), 500

    content, err = _html_to_display(raw_html)
    if err:
        return jsonify({"error": err}), 403

    return jsonify({
        "ok"     : True,
        "message": "캐시를 초기화하고 최신 데이터를 가져왔습니다.",
        "content": content,
    })


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
