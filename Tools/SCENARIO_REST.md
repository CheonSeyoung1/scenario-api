# Scenario REST 제작 도구

MCP 도구가 없는 환경에서도 저장소의 같은 Python 코드로 모델을 조회하고 이미지 생성 작업을 관리한다. 독립 CLI 도구이며 HTTP 서버를 제공하지 않는다. Python 3.10 이상과 표준 라이브러리만 사용하며, 명령은 저장소 루트에서 실행한다.

## 인증과 연결 확인

실행 환경의 비밀 설정에 `SCENARIO_API_KEY`, `SCENARIO_API_SECRET`을 등록한다. 프로젝트 범위를 명시해야 하면 `SCENARIO_PROJECT_ID` 또는 명령별 `--project-id`를 사용한다. 키를 명령 인자, 요청 JSON, 저장소, 채팅, 로그에 넣지 않는다. `.env`를 자동으로 읽거나 MCP 로그인 정보를 가져오는 기능은 없다. 클라우드에서는 **실제 도구 실행 프로세스에** 두 환경 변수가 전달되어야 한다.

```sh
python -B Tools/scenario_api.py check
```

`check`는 인증이 필요한 비공개 모델 목록을 한 개 조회하고, 결과가 없으면 공개 목록을 조회한다. 모델이 있으면 해당 모델 정보도 조회한다. **GET 요청만 사용하며 생성·견적·업로드를 호출하지 않는다.** 키가 없으면 `missing_credentials`로 종료하며 네트워크를 호출하지 않는다. 두 목록이 모두 비어 있으면 목록 접근만 검증한 결과로 `modelChecked: null`을 반환한다. REST 인증은 MCP의 OAuth와 별개다.

| 오류 | 확인할 사항 |
|---|---|
| `missing_credentials` | 현재 프로세스에 두 환경 변수가 전달됐는지 확인. 값은 출력하지 않는다. |
| HTTP 401 / 403 | 키·시크릿, 계정 권한과 프로젝트 범위 확인. |
| `rate_limited` / HTTP 429 | 요청 제한이 해제된 뒤 조회 재시도. |
| `transport_error` | DNS·네트워크 허용·프록시·타임아웃 확인. |
| `submission_unknown` 상태의 기록 | 새 생성 금지. Scenario 작업 이력에서 접수 여부와 작업 ID 확인. |

API 응답 원문이나 HTTP 예외 문자열은 출력하지 않는다. 모델 입력 규격 등 출력에 URL·인증값이 포함되면 가린다. 인증 요청은 고정된 Scenario API 주소에만 전송하고 리다이렉트를 따르지 않는다. 이미지 CDN 다운로드에는 API 인증 헤더를 전달하지 않는다.

## 모델과 입력 규격

```sh
python -B Tools/scenario_api.py models --privacy public --page-size 20
python -B Tools/scenario_api.py models --privacy private --page-size 20
python -B Tools/scenario_api.py model MODEL_ID
```

`MODEL_ID`는 목록에서 선택한 실제 ID로 바꾼다. 목록의 `nextPaginationToken`이 있으면 다음 호출의 `--pagination-token`으로 전달한다. 상세 조회의 `inputs`는 Scenario 고유 입력 규격이다. `required`도 단순 bool이 아닐 수 있으므로 JSON Schema로 임의 해석하지 않는다. 생성 JSON은 이 규격의 입력 이름을 직접 키로 사용하며, MCP의 `params`나 `team_id`를 그대로 붙이지 않는다. 최종 필드 검증은 서버가 수행한다.

## 생성 요청과 재개

다음 명령은 **실제 비용이 발생할 수 있는 사용 예시**다. 모델·수량·예산에 대한 사용자 승인을 받은 작업에서만 실행한다. 연결 확인이나 자동 테스트에는 실행하지 않는다. 이 도구는 자동 비용 견적이나 계정 한도 변경 기능을 제공하지 않는다.

```sh
python -B Tools/scenario_api.py generate MODEL_ID --input .utmp/scenario/request.json --receipt .utmp/scenario/jobs/request-001.json --allow-paid
python -B Tools/scenario_api.py wait --receipt .utmp/scenario/jobs/request-001.json --wait-timeout 600
```

`request.json`은 선택한 모델의 `inputs`에 맞춰 별도로 작성한다. 생성 명령은 모델을 먼저 조회한 뒤 요청 기록을 배타적으로 만들고 POST를 한 번만 보낸다. 기록에는 시각·모델·프로젝트·요청 해시·작업 ID·상태만 보관하며 입력 본문·키·서명 URL은 저장하지 않는다. 같은 기록 경로가 있으면 다시 생성하지 않는다.

생성 POST의 타임아웃·429·5xx는 **자동 재전송하지 않는다.** 응답을 받지 못했더라도 서버는 이미 작업을 시작했을 수 있기 때문이다. 기록이 `submission_unknown`이고 작업 ID가 없다면 Scenario 웹 작업 이력을 확인한다. 그 ID를 확인한 뒤 아래 조회 명령으로 이어간다. 오류를 없애려고 기록을 지우거나 새 기록 이름으로 같은 생성을 실행하지 않는다.

```sh
python -B Tools/scenario_api.py job JOB_ID
python -B Tools/scenario_api.py wait --job-id JOB_ID --wait-timeout 600 --poll-interval 3
```

완료 상태는 `success`, 실패·취소는 `failure`/`canceled`다. 진행 상태에서는 같은 작업만 폴링한다. 대기 시간 초과나 Ctrl+C는 서버 작업을 취소하지 않는다. 같은 ID로 `wait`를 재실행할 수 있다. 작업 ID를 받은 뒤 로컬 기록 갱신에 실패하면 오류 메시지에 해당 ID를 돌려준다.

GET은 기본 최대 3회 시도하며 408·429·일부 5xx·통신 오류에만 재시도한다. `Retry-After`가 있으면 우선하고, 없으면 지수 간격을 사용한다. 서버가 요구한 대기 시간이 남은 예산보다 길면 일찍 재요청하지 않고 종료한다. `--request-timeout`(기본 30초), `--max-attempts`(1~5)로 조절한다. `wait`의 전체 예산은 `--wait-timeout`으로 제한한다. urllib의 소켓 타임아웃 특성상 진행 중인 한 번의 읽기가 전체 예산을 초과할 수 있으며 강제 종료 시각을 보장하지는 않는다.

## 에셋 조회와 PNG 저장

```sh
python -B Tools/scenario_api.py asset ASSET_ID
python -B Tools/scenario_api.py download ASSET_ID --output-dir generated
```

`wait`/`job` 결과의 `assetIds`를 사용한다. `download`에는 여러 ID를 나열할 수 있다. 기본 저장 위치도 `generated`이며 파일명은 `<asset_id>.png`다. 출력 경로는 현재 저장소의 `generated/` 안으로 제한한다.

PNG 원본이면 `originalAssets=true`로 조회한 에셋 URL로 가져오고, 다른 이미지 형식이면 공식 단건 다운로드 API에 PNG 변환을 요청한다. 이 다운로드 명령은 필요에 따라 POST를 쓰므로 GET만 실행하는 연결 검증에는 포함하지 않는다. 영상·음성·3D 파일은 지원하지 않는다. 기존 `scenario_transfer.py`를 통해 PNG 컨테이너·청크 CRC·치수·32MiB 상한을 검사하고 SHA-256을 돌려준다. 픽셀 디코딩 검증을 대신하지 않는다.

완성된 임시 파일을 같은 디렉터리에서 하드링크로 공개해 동시 실행에서도 다른 기존 파일을 덮어쓰지 않는다. 동일한 바이트의 파일은 재사용한다. 하드링크를 지원하지 않는 파일시스템에서는 안전하게 실패한다. 저장은 PNG에만 적용하며 다른 프로젝트로의 임포트와 적용은 별도 작업이다. 중간에 여러 파일 중 하나가 실패해도 이미 저장된 파일은 남고, 같은 명령으로 다시 받아도 동일 파일은 보존한다.

기존 MCP 전송 명령도 유지한다. `python Tools/scenario_transfer.py upload|download 경로`는 이전과 같이 표준입력으로 임시 URL을 받고, 독립적으로 사용할 수 있다.

## 검증

```sh
python -B -m unittest discover -s Tools/tests -p "test_scenario_*.py"
```

테스트는 가짜 인증값·모의 HTTP 응답과 임시 디렉터리만 사용한다. 실제 생성·CU 소비는 없다. 인증 성공과 계정별 모델 접근은 별도의 `check` 실호출로 확인해야 한다. 실제 실행 환경의 네트워크·키 전달·파일시스템 검증은 `check`와 테스트로 수행한다.

이식한 기존 모의 응답 테스트는 실제 생성·CU 소비 없이 실행한다. 실제 인증 검증 결과는 저장소 루트 README를 참고한다.

## 확인한 공식 규격 — 2026-11-11

- [인증](https://docs.scenario.com/get-started/documentation/key-concepts-terminology/api-key-and-authentication): HTTPS Basic, key와 secret을 콜론으로 연결해 Base64 인코딩.
- [모델 목록](https://docs.scenario.com/api/resources/models/methods/list), [모델 상세](https://docs.scenario.com/api/resources/models/methods/retrieve): `models`, `nextPaginationToken`, `model.inputs`.
- [생성](https://docs.scenario.com/api/resources/generate/methods/run_model), [작업 조회](https://docs.scenario.com/api/resources/jobs/methods/retrieve): `job.jobId`, `job.status`, `job.metadata.assetIds`.
- [에셋 조회](https://docs.scenario.com/api/resources/assets/methods/retrieve), [단건 PNG 다운로드](https://docs.scenario.com/api/resources/assets/subresources/download/methods/request): `asset.url` 또는 다운로드 응답 `url`은 메모리에서만 사용.
- [오류](https://docs.scenario.com/get-started/documentation/understanding-api-responses-and-errors), [OpenAPI](https://cdn.cloud.scenario.com/static/api/openapi.json): 생성 POST의 idempotency 보장은 확인하지 못했다. 재시도 정책은 이 도구의 보수적 선택이다. REST `dryRun`도 추가 옵션에 따라 비용이 발생할 수 있어 연결 검증에는 사용하지 않는다.
