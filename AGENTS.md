# 작업 지침

- 사용자 설명과 프로젝트 문서는 한국어로 작성한다.
- Python 3.10 이상과 표준 라이브러리를 사용하는 Scenario CLI다. 상세 계약은 `Tools/SCENARIO_REST.md`를 따른다.
- 검증: `python3 -B -m unittest discover -s Tools/tests -p 'test_scenario_*.py'`.
- 실제 연결 검증은 `python3 -B Tools/scenario_api.py check`로 GET 조회만 수행한다.
- `SCENARIO_API_KEY`, `SCENARIO_API_SECRET`은 실행 환경에서만 받는다. 키·Authorization 헤더·서명 URL을 코드·인자·로그·기록에 넣지 않는다.
- 구현·설치·검증 요청은 유료 생성 승인이 아니다. 생성 범위·비용 승인을 받은 경우에만 생성한다. 접수 여부가 불명확한 POST를 자동 재전송하지 않는다.
- 다운로드는 `generated/` 아래로 제한하고 기존 파일 보존 및 PNG 검증을 유지한다.
- 게임 저장소에 대한 상대 경로 의존성을 추가하지 않는다. HTTP 서버는 아직 구현되지 않았다.
