# 4단계 조사 계획·페이지·백업·라이브 검증

## 명시적 조사 요청

`plan-research`는 사용자가 요청한 범위를 등록된 지역과 비교합니다. 서버, 자동 수집, 주기 실행은 없습니다. [research.schema.json](research.schema.json)을 따릅니다.

```json
{
  "schema_version": "1.0",
  "request_id": "user-research-001",
  "category": "음식점",
  "country": "JP",
  "city": "후쿠오카",
  "count": 50,
  "existing_limit": 50,
  "offset": 0
}
```

이는 요청 형식 설명이며 후쿠오카가 실제 DB에 등록되었다는 뜻은 아닙니다. 미등록이면 조사하여 검증된 국가·도시 항목을 먼저 등록하세요. 이름/별칭이 모호하면 region_id를 지정합니다. 새로운 날짜·출처를 임의로 생성하지 않습니다. 가상 입력 예제는 `examples/research-request.json`입니다.

```bash
travel-db plan-research <조사요청.json>
```

출력은 requested_count, read_only, scope, classification_hints, existing_count, existing_items, next_offset, snapshot_digest, required_item_fields, instructions입니다. 기존 항목에는 ID/이름/현지명/지역/출처/최종 확인일이 있고 장소에는 주소·좌표가 포함됩니다. 도시 요청은 그 도시의 하위 세부 지역을 포함합니다. 카테고리 세부 유형이 있으면 카페/자연 등 해당 유형만 비교합니다. 기존 건수를 요청 건수에서 자동 차감하지 않습니다.

다음 계획 페이지는 입력의 offset을 next_offset으로 바꾸고 expected_digest에 기존 snapshot_digest를 넣습니다. 값이 바뀌면 처음부터 다시 계획합니다. 실제 조사 결과는 기존 1.0/1.1 JSON으로 `ingest` dry-run을 확인하거나 `inbox`에 제출합니다. 계획만 실행해도 조사/저장이 완료된 것으로 보고하지 않습니다.

## 유한 범위 조회

```bash
travel-db page --table restaurants --start-row 2 --page-size 200
travel-db search --table restaurants --query '찾을 이름' --limit 50 --offset 0
```

page는 한 탭의 물리 행 1~1,000개를 읽고 rows에 row_number/row를 반환합니다. next_row는 다음 범위의 시작입니다. 빈 행은 결과에서 빠지지만 행 번호는 유지합니다. 끝까지 가면 next_row=null입니다. grid_rows는 Sheets에 할당된 행 수이므로 비어 있는 마지막 페이지가 나올 수 있습니다. page는 `validation_mode=headers_only`이며 외래키·모든 행의 무결성 검사를 수행하지 않습니다. 한 페이지를 읽은 것을 전체 데이터 검증으로 표현하지 마세요.

search는 전체 스냅샷을 검증한 뒤 결과를 offset/limit으로 나눕니다. next_offset과 digest를 반환하며 다음 요청에서 `--expected-digest <이전_digest>`를 사용하면 사이에 값/행 위치/탭 ID가 바뀐 경우 중단합니다. 큰 DB를 간단히 훑을 때는 page, 쓰기/참조 검증은 전체 스냅샷을 사용합니다.

전체 읽기는 개별 1,000행/40,000셀 이하 범위, 배치 합계 200,000셀 이내입니다. Google API HTTP 429는 GET만 1·2·4·8·16·32초에 작은 무작위 지연을 더해 최대 6회 재시도합니다. POST는 자동 재시도하지 않습니다. [공식 Sheets 할당량 및 backoff](https://developers.google.com/workspace/sheets/api/limits)를 참고하세요.

## 백업과 복원

```bash
travel-db backup --output /tmp/travel-backup.json
travel-db verify-backup /tmp/travel-backup.json
travel-db restore-backup /tmp/travel-backup.json --target-spreadsheet-id <별도_빈_시트_ID> --request-id restore-001
travel-db restore-backup /tmp/travel-backup.json --target-spreadsheet-id <별도_빈_시트_ID> --request-id restore-001 --apply
```

백업은 로컬 파일이며 운영 DB가 아닙니다. 관리 탭 전체와 원본 위치 메타데이터·snapshot digest·checksum을 포함합니다. 파일 권한은 0600이고 기존 파일을 덮어쓰지 않습니다. 인증 정보는 넣지 않습니다. verify-backup은 인증 없이 checksum/버전/행/외래키/ID/원본 digest를 검사합니다. 체크섬 없는 과거 형식은 거부하므로 새 도구로 백업을 다시 생성하세요.

복원 대상 ID는 반드시 지정합니다. Codespaces Secret의 마스터와 백업 원본 ID에는 전체 복원하지 않습니다. 서비스 계정에 편집 권한을 부여하고 관리 탭 헤더를 초기화한 별도 파일을 사용합니다. 모든 관리 테이블이 비어 있어야 하며 기존 데이터에 병합/덮어쓰기/삭제하지 않습니다. 관리 대상 밖의 탭은 보존합니다.

복원은 백업 데이터·복원 이력·영수증을 단일 batchUpdate로 추가합니다. 원래 항목 ID/생성·수정일/필드 근거/검토 기록은 유지하고 대상의 실제 행 위치는 새로 읽습니다. 동일 request_id와 백업의 재실행은 영수증을 반환합니다. 원래 요청에 다른 백업을 넣으면 충돌입니다. 대상 파일을 마스터로 전환하려면 검증 후 사용자가 Secret의 파일 ID를 별도로 변경해야 합니다. 원본을 자동 교체하지 않습니다.

백업 파일 입력은 50 MB 이하, 셀은 45,000자 이하, 저장 API 요청은 2 MB 이하입니다. dry-run과 apply 모두 저장 크기를 확인하며 초과한 전체 복원은 부분 저장 없이 거부합니다. Google Sheets의 동시 편집 격리 제한은 유지되므로 한 작업자씩 실행합니다.

## 라이브 테스트

2026-10-11 사용자 지시에 따라 두 파일을 `ChatGPT` 폴더에 만들고 마스터의 빈 구조를 복사했습니다. 서비스 계정에 두 파일만 추가 공유했습니다. 샘플은 'E2E 가상' 이름과 example.com 출처로 구분하며 실제 여행 정보가 아닙니다.

| 용도 | 별도 Google Sheets |
| --- | --- |
| 실제 추가/검토/복구 | 본인이 지정한 E2E 테스트 파일 ID |
| 백업 전체 복원 | 본인이 지정한 별도 빈 복원 테스트 파일 ID |

```bash
travel-db e2e --test-spreadsheet-id <E2E_테스트_ID> --restore-spreadsheet-id <복원_테스트_ID> --request-id <고유_테스트_요청_ID>
travel-db e2e --test-spreadsheet-id <E2E_테스트_ID> --restore-spreadsheet-id <복원_테스트_ID> --request-id <같은_요청_ID> --backup-output /tmp/e2e-backup.json --apply
```

기본은 dry-run이며 시드 입력 계획만 확인합니다. 후속 검토·복원은 실제 앞선 저장 결과가 있어야 실행되므로 dry-run의 passed=null은 E2E 통과를 의미하지 않습니다. --apply는 가상 샘플을 실제 테스트 파일에 기록합니다. stderr에는 체크포인트, stdout에는 최종 JSON을 출력합니다. pytest/CI는 실제 시트를 호출하지 않습니다.

마스터/E2E/복원 ID가 서로 달라야 하며 두 테스트 파일 제목은 `여행 DB 테스트 -`로 시작해야 합니다. 최초 대상은 빈 파일, 재실행은 동일 request_id여야 합니다. 다른 테스트 요청의 데이터를 지우고 다시 실행하지 않습니다. 새 테스트는 새 빈 파일에서 실행하세요. 동일 E2E 재실행의 동일 checksum 백업은 기존 파일을 재사용하며 다른 백업을 덮어쓰지 않습니다.

실제 실행 요청 ID는 `live-e2e-20261011-001`, 백업은 `/tmp/travel-db-stage4-20261011-backup.json`입니다. 신규 3개/대기 2개, 동일 요청 재실행, 가격 승인 수정 1000→1200 JPY, 거부, 항목 복구 1200→1000 JPY와 필드 근거 복구, 백업 검증/전체 복원, 재조회, 복원 재실행을 확인했습니다. 여행 항목은 두 파일에서 각각 지역 2개·음식점 1개입니다. E2E의 이력 8개/영수증 4개가 복원 대상에서는 복원 감사 기록을 더해 9개/5개입니다. 자세한 결과는 [검증 기록](verification.md)에 있습니다.
