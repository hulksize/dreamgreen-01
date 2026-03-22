import os
import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, session, redirect, url_for, jsonify

app = Flask(__name__)
# Vercel 배포 시 환경변수 SECRET_KEY 설정 권장.
# 없으면 개발용 기본값 사용 (프로덕션에서는 반드시 환경변수로 설정할 것).
app.secret_key = os.environ.get("SECRET_KEY", "dreamgreen-flask-secret-2026")

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


def fetch_raw_hulist(userid, cookies):
    """
    hulist.php 원본 HTML을 가져와 URL을 절대경로로 수정하고
    <script> 태그를 제거한 뒤 <body> 안쪽 내용만 반환한다.
    """
    try:
        resp = requests.get(
            f"{HULIST_URL}?userid={userid}",
            headers=HEADERS,
            cookies=cookies,
            timeout=15,
        )
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text

        for kw in ERROR_KEYWORDS:
            if kw in html:
                return None, kw

        soup = BeautifulSoup(html, "lxml")

        # JS 제거 (충돌 방지)
        for tag in soup.find_all("script"):
            tag.decompose()

        # 상대 URL → 절대 URL
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
        content = body.decode_contents() if body else str(soup)
        return content, None

    except Exception as e:
        return None, f"페이지 로드 오류: {e}"


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
    if "userid" not in session:
        return redirect(url_for("login"))

    userid = session["userid"]
    cookies = session.get("cookies", {})

    content, err = fetch_raw_hulist(userid, cookies)
    return render_template(
        "members.html",
        content=content,
        userid=userid,
        error=err,
    )


def _fetch_hulist_html(userid, cookies):
    """hulist.php 원본 HTML을 그대로 반환한다 (가공 없음)."""
    resp = requests.get(
        f"{HULIST_URL}?userid={userid}",
        headers=HEADERS,
        cookies=cookies,
        timeout=15,
    )
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


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
    members = []
    seen    = set()

    for header_td in header_tds:
        box_table = header_td.find_parent("table")
        if not box_table:
            continue

        box_text = box_table.get_text(separator="\n", strip=True)

        # 너무 크면 외부 레이아웃 테이블 → 스킵
        if len(box_text) > 2000:
            continue

        key = box_text[:100]
        if key in seen:
            continue
        seen.add(key)

        # ── 줄바꿈 split 파싱 ──────────────────────────────
        lines = [l.strip() for l in box_text.split("\n") if l.strip()]
        # "|" 구분자 제거 후 실 데이터만 남김
        data  = [l for l in lines if l != "|"]

        # 최소 5개 필드(이름·아이디·회원종류·매출액·가입일)가 있어야 유효
        if len(data) < 5:
            continue

        name  = data[0] if len(data) > 0 else ""
        mid   = data[1] if len(data) > 1 else ""
        kind  = data[2] if len(data) > 2 else ""
        sales = data[3] if len(data) > 3 else ""
        date  = data[4] if len(data) > 4 else ""

        # 날짜 형식이 아니면 DATE_RE 로 재탐색 (순서가 다를 경우 대비)
        if not DATE_RE.search(date):
            dm = DATE_RE.search(box_text)
            date = dm.group(0) if dm else date

        members.append({
            "index"   : len(members) + 1,
            "name"    : name,
            "id"      : mid,
            "kind"    : kind,
            "sales"   : sales,
            "date"    : date,
            "raw_text": box_text,
        })

    return members, strategy_used, None


# ─── /api/html  ──────────────────────────────────────────────────────────────

@app.route("/api/html")
def api_html():
    """hulist.php 원본 HTML을 text/plain 으로 그대로 출력한다."""
    if "userid" not in session:
        return "로그인 필요", 401

    userid  = session["userid"]
    cookies = session.get("cookies", {})

    try:
        html = _fetch_hulist_html(userid, cookies)
    except Exception as e:
        return f"오류: {e}", 500

    return html, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ─── /api/debug  ─────────────────────────────────────────────────────────────

@app.route("/api/debug")
def api_debug():
    """
    hulist.php 원본 HTML을 3가지 전략으로 파싱해 회원 데이터를 JSON으로 반환한다.
    """
    if "userid" not in session:
        return jsonify({"error": "로그인 필요", "code": 401}), 401

    userid  = session["userid"]
    cookies = session.get("cookies", {})

    try:
        html = _fetch_hulist_html(userid, cookies)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    for kw in ERROR_KEYWORDS:
        if kw in html:
            return jsonify({"error": kw, "code": 403}), 403

    members, strategy, diag = _parse_members(html)

    return jsonify({
        "login_user"    : userid,
        "strategy_used" : strategy,
        "total"         : len(members),
        "members"       : members,
        "diagnostics"   : diag,   # 전략 실패 시 샘플 td 정보
    })


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
