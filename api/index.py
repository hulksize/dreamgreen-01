"""
Vercel 서버리스 진입점.

Vercel의 @vercel/python 런타임은 이 파일에서 WSGI `app` 객체를 찾는다.
실제 애플리케이션 로직은 루트의 app.py 에 있고,
여기서는 경로 설정 후 import 만 수행한다.
"""
import sys
import os

# 리포지토리 루트(api/ 의 부모)를 Python 경로에 추가
# → `import app` 및 templates/ 경로가 정상 해석됨
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root_dir)

from app import app  # noqa: F401  — Vercel 이 이 변수를 WSGI 핸들러로 사용
