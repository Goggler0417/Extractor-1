# Tagmark Extractor v1.05

Chrome 북마크 목록을 일괄 처리하여 웹페이지 정보를 추출하고, Tagmark v2.04 신형 DB JSON을 생성하는 독립형 Extractor입니다.

## 사용자에게 필요한 파일

사용자는 이 프로젝트의 **ZIP 파일 하나만** 가지고 있으면 됩니다. 내부 파일은 배포용 구성요소이므로 직접 수정하거나 실행할 필요가 없습니다.

## 가장 쉬운 실행 방법 — Vercel Drop

이 버전은 iMac을 서버로 계속 켜둘 필요가 없도록 Vercel Functions를 사용합니다. Vercel은 2026년 현재 파일/폴더/ZIP을 브라우저에 끌어다 놓아 배포하는 Vercel Drop을 제공합니다.

1. ZIP 압축을 풀지 않고 이 ZIP 파일을 준비합니다.
2. 브라우저에서 https://vercel.com/drop 을 엽니다.
3. ZIP을 브라우저에 끌어다 놓습니다.
4. Deploy를 누릅니다.
5. 만들어진 `vercel.app` 주소를 iPhone/iPad Safari에서 엽니다.
6. 필요하면 Safari의 공유 메뉴 → 홈 화면에 추가로 앱처럼 사용할 수 있습니다.

배포 후에는 iMac을 계속 켜둘 필요가 없습니다. URL을 추출할 때만 서버 측 Function이 실행됩니다.

## 입력

- Chrome 북마크 내보내기 `.html` — 가장 권장. 제목, URL, 폴더 경로를 읽습니다.
- `.txt` — URL이 포함된 줄을 읽습니다.
- `.docx` — Word 문서의 실제 hyperlink와 표시 텍스트를 읽습니다.
- 선택 사항: Tagmark v2.04 백업 JSON. 넣으면 기존 Page/Schema/기존 entity를 유지하면서 북마크를 추가합니다.

## 결과

`tagmark-extracted-v1.05.json`을 다운로드하여 Tagmark v2.04의 백업/복원 기능으로 가져옵니다. Extractor는 Tagmark IndexedDB에 직접 접근하지 않습니다.

## 추출

Generic extraction: title, canonical URL, description, image, site name, author, dates, keywords, language, JSON-LD, OpenGraph/meta.

Site-specific extraction: `profiles.json`에서 domain별 CSS/meta/attribute/regex/JSON-LD 규칙을 추가할 수 있습니다.

## 한계

서버에서 HTML을 직접 받을 수 없는 로그인/차단/anti-bot 페이지 및 JavaScript 렌더링 후에만 내용이 생기는 사이트는 v1.05에서 제한될 수 있습니다. 향후 browser-rendering adapter를 추가할 수 있도록 구조를 분리해 두었습니다.
