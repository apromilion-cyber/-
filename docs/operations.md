# 5단계 운영 점검과 실사용 절차

Google Sheets가 원본입니다. 아래 명령은 사용자가 작업할 때 직접 실행합니다. 정기 실행·자동 수집·웹 서버는 없습니다.

## 읽기 전용 점검

```bash
travel-db audit
travel-db audit --detail-limit 100
```

`audit`는 환경변수의 정확한 마스터 ID를 사용해 모든 관리 탭을 읽습니다. 여행 항목의 모델·외래키·식별키·승인/필드 출처 검증에 더해 영수증 해시 형식/시각, 대기 원문의 JSON/영수증 연결, 변경 이력의 기본 JSON 형식/작업 종류/영수증 연결, 실제 물리 위치와 입력함 봉투를 검사합니다. 저장·수정·입력함 실행·자동 복구는 하지 않습니다. 과거 이력의 전체 사실관계나 모든 버전의 연속성을 증명하는 검사는 아닙니다.

출력 계약:

| 필드 | 의미 |
| --- | --- |
| read_only | 항상 true |
| healthy | error가 없으면 true. warning 또는 미처리 요청이 있을 수 있음 |
| validation_mode | full_snapshot_and_journals |
| snapshot_digest | 점검한 값·물리 위치·탭 ID의 해시 |
| row_counts | 빈 행을 제외한 탭별 레코드 수 |
| verification_queue | pending/resolved와 미처리 사유별 수 |
| inbox | unprocessed/processed/invalid/conflict 요청 수 |
| error_count/warning_count/issue_count | 전체 발견 건수 |
| issues | table, row_number, record_index, code, severity |
| issues_truncated | detail_limit 때문에 상세 목록이 잘렸는지 여부 |

`row_number`는 실제 1 기반 Sheets 행입니다. 물리 위치가 없는 테스트 저장소나 전체 스냅샷 오류에서는 null이고 `record_index`는 0 기반 기록 순서입니다. 본 테이블/참조 검증 실패는 민감한 값 노출을 막기 위해 snapshot_invalid로 보고하고 추가 요약을 생략합니다. 관리 탭의 누락/헤더 또는 통신 오류는 기존 CLI error 응답으로 중단합니다. 종료 코드 0은 error가 없음을, 2는 DB 오류/요청 충돌/통신·인증 실패를 의미합니다.

대기 자체는 오류가 아닙니다. 잘못된 입력함 봉투는 warning이고 본 DB의 손상과 구분합니다. 영수증과 다른 내용으로 같은 request_id를 다시 제출하면 conflict/error입니다. 입력 원문·항목 이름·주소·키·토큰은 이 보고서에 출력하지 않습니다. 입력함의 개별 여행 항목과 변경 가능 여부는 ingest/operate/process-inbox dry-run으로 최종 검사합니다.

## 실제 조사부터 저장까지

1. `audit`로 무결성과 미처리 상태를 확인합니다. error가 있으면 apply를 중단하고 해당 기록을 검토합니다. 잘못된 입력함 요청은 기존 기록을 덮어쓰지 말고 새 request_id의 수정 요청으로 제출합니다. 기존 오류 행이 남으면 process-inbox는 이를 다시 보고할 수 있습니다.
2. ChatGPT에서 마스터의 regions와 기존 항목을 조회합니다. 지역이 비어 있으면 실제 출처로 국가/도시를 먼저 조사해 검증된 지역 JSON을 만듭니다. 모르는 ID나 출처·확인일을 채우지 않습니다. 지역과 그 지역을 참조하는 항목은 같은 배치로 제출할 수 있습니다.
3. 등록된 지역에서 `plan-research <요청파일>`을 실행하고 사용자가 지시한 범위/건수만 조사합니다. 결과는 입력 1.0/1.1 JSON으로 작성합니다.
4. `validate <결과파일>` 후 `ingest <결과파일>` dry-run의 created/skipped/queued와 후보/변경 내용을 확인합니다. Drive로 제출한 요청은 `process-inbox`로 확인합니다. 각 dry-run은 현재 DB 기준이며 여러 요청의 가상 결과를 연결하지 않습니다.
5. 변경 작업 전 `backup --output <새_백업파일>`과 `verify-backup <백업파일>`을 실행합니다. 백업에는 개인 데이터가 있으므로 공유/커밋하지 않습니다. 기존 백업 파일은 덮어쓰지 않습니다.
6. 한 작업자만 `ingest <결과파일> --apply` 또는 `process-inbox --apply`를 실행합니다. 영수증과 audit/검색 결과로 실제 반영을 확인하고 검증 대기는 pending에서 별도 검토합니다.

미확인 항목이 있다는 이유만으로 verified로 바꾸지 않습니다. 승인/수정은 [검토 계약](review.md)의 전체 항목·근거·검토자·사유를 사용합니다. 초기 등록에 쓰는 조사 출처와 필드별 승인 출처는 구분됩니다.

## 실패 후 재개

| 상황 | 조치 |
| --- | --- |
| API 타임아웃/응답 유실 | 다른 쓰기를 시작하지 않고 같은 request_id와 동일 JSON으로 먼저 dry-run/영수증 조회. 영수증이 있으면 기존 결과를 확인. 같은 요청의 apply 재실행은 이미 반영된 영수증을 반환 |
| expected_hash 또는 snapshot 충돌 | inspect/계획을 다시 읽고 diff와 출처를 재검토. 바뀐 JSON은 새 request_id 사용 |
| 같은 request_id의 다른 payload | 이전 요청 원문/영수증을 보존하고 새 ID로 수정 요청 제출 |
| 헤더 또는 DB 무결성 오류 | apply 중단. 백업/Sheets 변경 기록으로 조사. 도구가 헤더나 행을 자동 수리하지 않음 |
| 잘못 반영한 업데이트 | 정확한 update/restore history_id와 최신 expected_hash로 새 restore 작업 |
| 전체 백업 복구 필요 | checksum 검증 후 원본/마스터와 다른 빈 Sheets에 restore-backup dry-run/apply. 대상 전환은 사용자 검토 후 Secret의 시트 ID 변경 |
| 요청 크기 2 MB 초과 | 일반 조사 입력은 의존 지역을 먼저 저장하고 고유 request_id의 작은 배치로 분리. 전체 백업 복원은 부분 저장을 지원하지 않음 |

dry-run의 committed=false 또는 라이브 E2E dry-run의 passed=null을 저장/검사 완료로 보고하지 않습니다. Sheets에는 전역 격리가 없으므로 audit 성공 뒤에도 다른 클라이언트가 바꿀 수 있습니다.

## 가상 데이터 성능 측정

```bash
.venv/bin/python tests/benchmark_scale.py --restaurants 1000
.venv/bin/python tests/benchmark_scale.py --restaurants 10000
```

50~50,000개의 가상 음식점과 생성 이력을 테스트 전용 메모리 저장소에 구성합니다. 모델/직렬화로 만든 fixture이며 이 구성 시간은 실제 API 일괄 수집 성능이 아닙니다. 전체 참조 검증, audit, 조사 계획, 기존 50개 dry-run 재입력의 skip, 신규 1개 dry-run과 원본 무변경을 확인하고 초 단위 시간을 출력합니다. 인증 정보·실제 여행 데이터·Google API를 사용하지 않으며 운영용 로컬 DB를 만들지 않습니다.

1만 건 측정 결과는 [검증 기록](verification.md)에 있습니다. 측정값은 환경별로 달라지므로 pytest/CI에서 고정 시간 기준을 강제하지 않습니다. 실제 Google API 대량 조회/쓰기 지연·할당량·여러 편집자의 동시 변경은 별도 검증이 필요합니다. 원격 GitHub CI는 7단계에서 Python 3.11/3.14 모두 통과했으며 자세한 기록은 검증 문서에 있습니다.
