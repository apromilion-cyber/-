# 개인 여행 데이터베이스

Google Sheets를 유일한 원본으로 사용하는 Python 도구입니다. 사용자 요청으로 조사한 JSON을 검증한 뒤 저장합니다. 자동 조사, 스케줄러, 웹 서버는 없습니다.

현재 6단계까지 구현: 데이터 모델·검증·Sheets 추가/조회·분류/중복 검사·대기 승인/거부·업데이트·필드 출처·이전값 복구·사용자 조사 계획·페이지 조회·백업 검증/복원·별도 Sheets 라이브 E2E·읽기 전용 운영 점검·패키지/CI 준비. [설계 문서](docs/architecture.md), [ChatGPT 계약](docs/chatgpt.md), [검토·수정](docs/review.md), [4단계 사용 문서](docs/workflows.md), [운영 절차](docs/operations.md), [배포 준비](docs/release.md)를 참고하세요.

Drive 연결에서 본인 마스터 파일을 검색하고 Secret `TRAVEL_SPREADSHEET_ID`와 ID를 대조합니다. 초기 파일명은 `개인 여행 마스터 DB v1`입니다. [입력 계약](docs/chatgpt.md)에 접근 절차가 있으며 [검증 기록](docs/verification.md)에서 실제 연결 확인과 미검증 사항을 구분합니다.

## 설치

Python 3.11 이상을 사용합니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
pytest
```

GitHub Actions에도 샘플 기반 검사 워크플로우를 추가했습니다. 테스트에서 실제 Sheets 인증이나 데이터를 사용하지 않습니다.

## 인증

Google Cloud 프로젝트에서 Google Sheets API를 켜고 서비스 계정을 생성합니다. 마스터 Google Sheets를 서비스 계정 이메일에 편집자로 공유합니다. ChatGPT에 연결된 Google 계정에도 읽기/편집 권한이 필요합니다. 두 인증은 별개입니다.

GitHub 저장소 Settings → Secrets and variables → Codespaces에 다음 Secrets를 등록한 뒤 Codespace를 재시작합니다.

| Secret | 값 |
| --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 서비스 계정 JSON 전체 |
| `TRAVEL_SPREADSHEET_ID` | 마스터 시트 ID |

코드·커밋·명령행 인자에 키를 붙이지 마세요. 환경변수 JSON은 메모리에서 읽으며 키 파일을 만들지 않습니다. Drive 연결만으로 Codespaces의 Python 인증이 설정되지는 않습니다.

## 실행

쓰기는 기본 dry-run입니다. `--apply`에서만 Google Sheets에 반영합니다.

```bash
travel-db schema --output /tmp/travel-schema.json
travel-db validate examples/sample.json
travel-db validate examples/classification.json
travel-db init
travel-db init --apply
travel-db ingest examples/sample.json
travel-db ingest examples/classification.json
travel-db ingest examples/sample.json --apply
travel-db search --table regions --query '샘플'
travel-db process-inbox
travel-db process-inbox --apply
travel-db backup --output /tmp/travel-backup.json
travel-db pending --limit 50
travel-db audit
travel-db inspect <대상_ID>
travel-db operation-schema --output /tmp/travel-operation-schema.json
travel-db validate-operation <작업_JSON_파일>
travel-db operate <작업_JSON_파일>
travel-db operate <작업_JSON_파일> --apply
travel-db research-schema --output /tmp/travel-research-schema.json
travel-db plan-research <조사요청_JSON_파일>
travel-db page --table restaurants --start-row 2 --page-size 200
travel-db search --table restaurants --limit 50 --offset 0
travel-db verify-backup /tmp/travel-backup.json
travel-db restore-backup /tmp/travel-backup.json --target-spreadsheet-id <별도_빈_시트_ID> --request-id restore-001
```

예제는 가상 데이터입니다. 실제 마스터에 넣지 말고 별도 테스트용 시트에서 실행하세요. `validate`와 `schema`는 인증 없이 실행됩니다. `init`은 누락된 탭만 만들고 기존 헤더가 다르면 중단합니다. 기존 여행 데이터를 삭제하지 않습니다.

ChatGPT는 Drive 연결로 마스터를 검색하고 읽습니다. 입력은 `inbox`에 요청 JSON을 추가하고 사용자가 Codespaces에서 `process-inbox --apply`를 실행하는 방식입니다. 편집 기능이 없으면 JSON을 Codespaces로 전달합니다. 검증 완료일과 출처가 있는 항목만 본 테이블에 들어갑니다. 형식 오류·미확인·참조 오류·기존 항목 변경은 `verification_queue`로 분리됩니다.

3단계는 `review_decisions`, `field_sources` 탭을 추가합니다. 기존 헤더와 여행 항목 ID는 유지합니다. 기존 DB를 연결할 때 먼저 `init` 계획을 확인하고 `init --apply`로 누락된 두 탭을 만드세요. `pending`은 미처리 대기만 표시하고, `inspect`는 대상 값·현재 필드 출처·충돌 검사 해시를 반환합니다. 승인/업데이트는 검토한 전체 항목과 변경 필드의 출처를 제출합니다. `expected_hash`가 다르면 기존 행을 수정하지 않습니다. 모든 변경과 이력·영수증은 한 API 요청에 담습니다.

승인·거부·업데이트·복구 JSON도 `inbox`로 제출할 수 있습니다. `action`이 있는 JSON은 작업 계약으로 처리합니다. `validate-operation`은 형식 검사만 수행하므로 실제 대상·출처·중복 검사는 `operate` dry-run으로 확인하세요. 가상 예제 `examples/review-update.json`의 ID/해시는 설명용이며 실제 `inspect` 결과로 바꿔야 합니다.

## 현재 제한

JSON 1.0은 명시적 종류와 지역 ID를 사용합니다. JSON 1.1은 `country`, `city`, `region`, `category` 힌트를 받아 DB에 등록된 지역명·현지명·별칭과 분류 사전으로 ID/종류를 정합니다. 모호한 이름, 없는 지역, 입력 충돌은 검증 대기로 보냅니다. [자동 분류 및 중복 기준](docs/classification.md)을 참고하세요.

유사 이름·현지명·주소·좌표에 따른 중복 후보도 검증 대기로 보내며 점수와 비교 근거를 보고합니다. 같은 ID 또는 결정적 식별키의 반복 입력은 건너뜁니다. 중복 후보를 승인할 때 기존 항목 업데이트 또는 별개 신규 생성 여부를 명시합니다. 별개로 확인한 후보 ID는 `distinct_from`으로 기록하며 동일 식별키의 신규 생성은 거부합니다. 이름/위치 근거가 부족하면 중복 후보를 놓칠 수 있으므로 검토가 필요합니다.

항목 복구는 `update`/`restore` 이력의 이전 값으로 돌아가는 새 수정입니다. 전체 백업은 원본/마스터와 다른 빈 Sheets에만 복원합니다. 기존 데이터에 병합하거나 행을 삭제하는 복구는 제공하지 않습니다. 예전 필드 출처를 추정하지 않으며 출처 URL 내용을 자동 판정하지 않습니다. 실제 접근은 Google 계정/서비스 계정 권한으로 제한합니다.

4단계에서 별도 테스트 Sheets 두 개를 사용해 실제 추가·승인 수정·거부·복구·백업 복원·재조회·멱등성을 확인했습니다. 5단계는 audit로 실제 빈 마스터의 오류/대기/입력함 0건을 확인하고, 가상 음식점 1만 건을 메모리에서 점검했습니다. 마스터에는 테스트 데이터를 넣지 않았습니다. Google API 대량 읽기/쓰기 부하, 외부 클라이언트의 동시 편집과 원격 CI 실행은 미검증입니다. API 한 요청을 2 MB 이하로 제한하며 초과한 전체 복원은 부분 저장 없이 중단합니다.

Sheets에는 외래키나 조건부 커밋이 없습니다. 한 Codespace/한 작업자씩 쓰세요. 로컬 실행 잠금과 커밋 직전 변경 감지를 사용하지만 다른 클라이언트의 동시 편집은 완전히 차단하지 못합니다. 인증이 없으면 실제 Python API 연동은 미검증으로 보고합니다.
