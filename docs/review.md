# 3단계 검토·수정 계약

## 적용 및 확인

`travel-db init`의 dry-run에서 누락 탭을 확인한 후 `init --apply`로 `review_decisions`와 `field_sources`를 만듭니다. 기존 탭·헤더·행을 삭제하거나 바꾸지 않습니다. 저장 스키마는 1.1, 조사 입력은 기존 1.0/1.1, 작업 입력은 별도의 1.0입니다.

```bash
travel-db pending --limit 50
travel-db inspect <대상_ID>
travel-db validate-operation <작업파일.json>
travel-db operate <작업파일.json>
travel-db operate <작업파일.json> --apply
```

모든 쓰기는 기본 dry-run입니다. 작업파일은 일시적인 입력이며 원본 DB가 아닙니다. `validate-operation` 성공은 JSON 형식이 맞다는 뜻입니다. `operate`는 실제 DB와 비교하여 외래키·중복·필드 출처·충돌을 검증합니다. 실제 마스터에는 예제 샘플을 넣지 않습니다.

`inspect`는 저장 행, 현재 버전의 필드 출처, `expected_hash`를 반환합니다. 작업 JSON의 해시는 이 결과를 복사합니다. 보고서의 `expected_hash_after`는 실제 apply가 완료된 상태에서 다음 검토에 사용할 수 있습니다. dry-run의 시각/해시는 실제 저장 결과를 대신하지 않습니다.

## 작업 JSON

전체 계약은 [operation.schema.json](operation.schema.json)이며 `travel-db operation-schema`로 생성합니다. 공통 키는 schema_version="1.0", request_id, action, reviewer, reason입니다. request_id는 조사 요청과 작업 요청 전체에서 고유하게 지정합니다. 동일 내용을 재시도할 때만 같은 ID를 사용합니다. reviewer는 감사 기록이며 별도 인증 방식은 아닙니다. Google 공유 권한과 서비스 계정 권한이 실제 접근을 제한합니다.

| action | 필수 추가 필드 | 동작 |
| --- | --- | --- |
| reject | queue_id | 대기 원문을 보존하고 거부 결정·이력·영수증 추가 |
| approve, mode=create | queue_id, mode, entity, field_sources | 검토 완료된 전체 항목을 신규 생성 |
| approve, mode=update | queue_id, mode, target_id, expected_hash, entity, field_sources | 지정한 기존 항목으로 승인; 변경 후보는 원래 대상 ID만 허용 |
| update | target_id, expected_hash, entity, field_sources | 기존 항목을 직접 수정; 대기 항목을 자동으로 해결하지 않음 |
| restore | history_id, expected_hash | 해당 update/restore 이력의 직전 값과 필드 근거를 새 버전으로 복구 |

`entity`는 기존 엄격한 여행 항목 계약과 같으며 kind와 지역 ID를 명시합니다. 부분 패치가 아닌 전체 항목입니다. 생략한 선택 필드는 null/빈 배열 기본값으로 바뀔 수 있으므로 `inspect`와 원래 항목을 참고해 모든 유지할 값을 포함하세요. 저장 행의 category, source_url, created_at, updated_at은 입력에 넣지 않습니다. id는 업데이트 시 생략하거나 target_id와 같아야 합니다. 기존 ID와 종류, 생성 시각은 바꾸지 않습니다.

승인은 대기 원문에 오류가 있어도 수정한 전체 entity가 검증되어야 합니다. unverified/disputed를 그대로 승인할 수 없으며 날짜나 출처를 임의로 채우지 않습니다. 대기 항목마다 결정은 하나이고 처리 완료 후 새 request_id로 다시 결정하는 것도 거부됩니다. 잘못 거부한 항목은 별도 신규 조사 요청으로 재제출합니다. 잘못 승인한 업데이트는 해당 이력으로 복구할 수 있으며 승인 결정 기록은 남깁니다.

## 필드별 출처

```json
{
  "field_sources": {
    "price": ["https://example.com/fictional-menu"],
    "address": ["https://example.com/fictional-location"]
  }
}
```

위 URL은 형식 설명용 가상 주소입니다. 실제 제출에서는 확인한 URL을 사용하며 entity.sources에도 Evidence로 포함합니다. URL은 정규화한 뒤 sources의 고유 ID로 연결합니다. Price의 amount/currency/basis는 하나의 복합 필드 price로 관리합니다. 배열도 최상위 필드 단위입니다. 확인일은 제출한 entity.last_verified_on을 사용하며 자동으로 오늘 날짜를 사실 확인일로 채우지 않습니다.

업데이트에서는 값이 바뀐 사실 필드마다 출처가 필요합니다. null로 지우거나 배열을 비우는 변경도 근거를 요구합니다. 기존 필드 출처의 URL을 항목 sources에서 제거하면 그 필드 값이 그대로여도 새 출처를 제출해야 합니다. id/kind와 sources/조사일/확인일/검증 상태 같은 관리 필드는 field_sources 키로 사용하지 않습니다. 미정의 필드는 거부합니다.

신규 승인에는 null/빈 배열을 제외한 모든 사실 필드에 근거를 제출합니다. 국가의 name/level/country_code, 음식점의 name/region_id/address/price 등이 해당합니다. 개인 평가 taste_fit/visited/satisfaction/notes는 명시적 빈 배열을 사용하면 basis=personal로 기록합니다. 평가의 이름·지역·대상 ID는 출처를 연결해야 합니다. 빈 URL이나 임의 출처를 생성하지 않습니다.

기존 조사 입력 1.0/1.1은 항목 전체의 출처만 저장합니다. 그 자료에서 필드 근거를 추정하지 않습니다. 새 검토에서 제공된 근거만 field_sources에 추가하고 변경하지 않은 필드의 기존 근거는 그대로 이어갑니다. 자료에 없는 과거 필드 출처는 빈 매핑으로 남습니다. sources의 제목/게시자/열람일은 URL 최초 등록값을 보존하며 글로벌 출처 메타데이터 수정은 제공하지 않습니다. 필드별 재확인일은 별도로 기록합니다.

field_sources는 버전별 추가 기록입니다. entity_revision은 해당 저장 행 전체의 SHA-256이며 updated_at도 포함합니다. Drive에서 근거 탭을 직접 읽을 때 이전 버전을 현재 근거로 해석하지 마세요. `inspect`가 현재 행 버전과 정확히 일치하는 근거만 반환합니다.

## 중복과 충돌

중복 후보를 기존 항목에 반영하려면 mode=update와 target_id를 지정하고 유지할 값을 포함한 전체 entity를 작성합니다. 후보 전체를 자동 병합하지 않습니다. 별개 신규 항목으로 확인했다면 mode=create와 `distinct_from: ["검토한_기존_ID"]`를 사용합니다. 직접 업데이트 중 다른 기존 항목과 유사 후보가 생기는 경우도 같은 확인을 요구합니다. 미확인 후보가 남으면 거부하고 동일 자연 식별키의 새 행 생성은 항상 거부합니다.

승인·수정·복구 직전 대상의 행/필드 근거 해시를 비교합니다. 실제 커밋 전에는 전체 값·물리 행 위치·탭 ID도 다시 읽어 비교합니다. 빈 행 또는 행 이동 때문에 다른 행을 수정하지 않습니다. 수정 후 전체 DB를 검사하므로 예를 들어 국가 코드를 바꾸어 하위 도시의 국가 코드와 불일치하게 만드는 변경은 중단합니다.

Sheets는 동시 편집을 완전히 격리하지 못합니다. 최종 검사 이후 다른 클라이언트가 쓰는 극히 짧은 구간은 잠글 수 없으므로 한 작업자씩 실행합니다. 로컬 실행 잠금은 같은 Codespace에서만 작동합니다.

## 이력과 복구

새 update/restore 이력의 before_json/after_json은 `{entity: 저장행, field_sources: 당시_근거}`를 담습니다. after에는 reviewer와 reason도 보관합니다. 예전 create/queue 이력의 형식은 유지하며 복구 대상으로 사용하지 않습니다. 변경 없음은 status=unchanged로 기록하고 항목 버전은 유지합니다.

복구는 현재 값과 근거가 해당 이력의 after와 정확히 같을 때만 실행합니다. 최신 expected_hash를 가져왔더라도 그 이력 이후 다른 변경이 있으면 중단합니다. 기록된 이전 값을 저장하면서 created_at은 유지하고 updated_at은 새 시각을 부여합니다. 이전 근거를 복원하되 과거 기록은 삭제하지 않습니다. restore 이력도 다시 복구하여 복구 직전 상태로 돌아갈 수 있습니다.

생성 취소를 위한 행 삭제, 오래된 이력을 현재 값에 강제로 덮어쓰기, 선택 필드만 되돌리기는 지원하지 않습니다. 필요하면 최신 상태를 검토해 새 update 요청을 작성합니다. 4단계의 [전체 백업 복구](workflows.md)는 원본/마스터와 다른 빈 Sheets에서만 제공합니다. sources·대기 원문·결정·영수증과 이전 근거는 감사 자료로 남습니다.

API 오류가 나면 같은 request_id와 같은 JSON으로 영수증을 확인합니다. 서버가 반영했지만 응답이 유실된 경우 원래 영수증을 반환하며 두 번 수정하지 않습니다. 영수증이 없다면 대상과 원본 DB 상태를 다시 검사한 뒤 진행합니다. 프로그램은 불확실한 POST를 자동 재시도하지 않습니다.

## ChatGPT 제출

작업 JSON도 기존 inbox의 request_id/payload_json/submitted_at으로 제출합니다. action이 있으면 process-inbox가 작업 계약으로 처리합니다. ChatGPT가 직접 여행 테이블을 고치면 검증/이력을 우회하므로 작업 JSON으로 요청하세요. 한 셀의 JSON은 40,000자 이내로 유지합니다. 각 inbox dry-run은 독립적인 현재 DB 상태를 기준으로 하므로 앞선 dry-run의 가상 생성/수정 결과에 의존하는 다음 작업은 승인되지 않습니다. 사용자 지시 없는 자동 승인이나 자동 조사는 없습니다.
