# Scenario API 도구

Scenario REST API를 호출하는 독립 Python CLI다. FantasyGameProject_001의 `Tools/scenario_api.py`, `scenario_transfer.py`와 기존 테스트를 이식했다. Python 3.10 이상과 표준 라이브러리만 사용하므로 별도 패키지 설치는 필요 없다.

## 실행

저장소 전체를 받을 필요 없이 [Releases](https://github.com/CheonSeyoung1/scenario-api/releases/latest)에서 `scenario-cli-v버전.zip`과 `SHA256SUMS`를 내려받아 사용할 수 있다. ZIP에는 CLI 2개와 문서만 포함한다. Python 3.10 이상은 별도로 설치해야 한다.

Linux에서는 같은 폴더에 받은 파일을 `sha256sum -c SHA256SUMS`로 확인하고 ZIP을 푼다. Windows PowerShell에서는 `Get-FileHash .\scenario-cli-v0.1.0.zip -Algorithm SHA256` 결과를 `SHA256SUMS`와 비교한 뒤 `Expand-Archive .\scenario-cli-v0.1.0.zip`으로 푼다. 이후 압축 안의 `scenario-cli-v버전/` 폴더에서 아래 CLI 명령을 실행한다. 릴리스 사용 시에도 다운로드는 압축을 푼 폴더의 `generated/` 아래에 저장된다.

명령은 이 저장소 루트에서 실행한다. 실행 환경의 비밀 설정으로 `SCENARIO_API_KEY`, `SCENARIO_API_SECRET`을 제공한다. 선택적으로 `SCENARIO_PROJECT_ID`를 지정한다. `.env` 파일을 자동으로 읽지는 않는다.

```sh
python3 -B Tools/scenario_api.py check
python3 -B Tools/scenario_api.py models --privacy public --page-size 20
python3 -B Tools/scenario_api.py model MODEL_ID
python3 -B -m unittest discover -s Tools/tests -p 'test_scenario_*.py'
```

`check`는 GET으로 모델 목록·상세만 조회한다. 생성·견적·업로드는 실행하지 않는다. 상세 명령은 [사용 안내](Tools/SCENARIO_REST.md)를 참고한다.

## 생성 결과와 기록

PNG 다운로드 경로는 이 저장소의 `generated/` 아래로 제한한다. 기본값도 `generated/`이며 Unity 프로젝트에 의존하지 않는다. 생성 작업 기록은 예를 들어 `.utmp/scenario/jobs/`에 저장한다. 두 경로 모두 Git에서 제외한다. 게임 프로젝트에 적용할 파일은 다운로드를 확인한 뒤 별도로 가져간다.

실제 생성은 비용이 발생할 수 있으며 범위·비용 승인 후 `--allow-paid`를 명시해야 한다. 접수 결과가 불명확한 생성 요청은 재전송하지 않고 기존 작업을 확인한다. 키·인증 헤더·서명 URL은 코드·명령 인자·로그에 남기지 않는다.

## HTTP REST 서버로 확장

현재는 CLI와 재사용 가능한 `ScenarioClient`가 있으며 HTTP 수신 서버는 아직 없다. 다음 단계에서는 이 클라이언트를 재사용해 모델 조회, 작업 접수·조회, 에셋 조회 엔드포인트를 제공할 수 있다. API 키는 서버에서 관리하고 호출자 인증, 생성 비용 승인, 작업 기록의 영속화와 중복 접수 방지를 함께 구현해야 한다. CLI의 `--allow-paid`만으로 HTTP 호출자의 권한을 대신하지 않는다.

게임 저장소의 기존 도구는 유지했다. 이후 도구 변경은 이 저장소를 기준으로 진행하며 기존 게임 도구와 자동 동기화되지는 않는다.

## 이식 검증

Python 3.12 환경에서 기존 44개와 독립 다운로드 경로 검사 1개, 총 45개 테스트를 통과했다. 이 저장소의 CLI로 실제 인증·모델 목록·상세 조회도 확인했다(`connected: true`, `generationRequests: 0`). 유료 생성, 실제 생성 결과 다운로드, HTTP 서버 동작은 검증 범위에 포함하지 않았다.

## 릴리스 만들기

`python3 scripts/build_release.py v0.1.0`으로 `dist/`에 ZIP과 SHA-256 파일을 만든다. 버전은 `v숫자.숫자.숫자` 형식이다. 버전 태그를 push하면 GitHub Actions가 테스트 후 GitHub Release와 파일을 게시한다. 실패 시 Actions의 `Release CLI`를 기존 태그로 수동 재실행할 수 있다.
