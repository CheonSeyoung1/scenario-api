# Scenario CLI 최초 릴리스

- 모델 목록·상세, 생성 작업 접수·조회·대기, 에셋 조회와 PNG 다운로드 지원
- Python 3.10 이상, 외부 Python 패키지 설치 불필요
- ZIP에 실행 파일과 사용 문서만 포함: 저장소 clone 불필요
- ZIP 무결성 확인용 SHA256SUMS 제공
- API 키와 시크릿은 실행 환경에서 제공. 생성에는 비용 승인 필요

ZIP을 풀고 내부 폴더에서 `python3 -B Tools/scenario_api.py check`를 실행하세요. Python은 별도로 설치해야 합니다. HTTP 서버는 포함하지 않습니다.
