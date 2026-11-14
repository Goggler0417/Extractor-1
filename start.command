#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  printf 'Python 3.9 이상을 설치한 뒤 다시 실행하세요.\n'
  read -r -p 'Enter를 눌러 닫기 '
  exit 1
fi
exec python3 hitomi-extractor.py
