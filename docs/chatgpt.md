# ChatGPT 접근 및 JSON 계약

## 마스터 파일

Drive 연결에서 본인이 만든 마스터 파일을 검색하고 메타데이터의 ID를 Codespaces Secret `TRAVEL_SPREADSHEET_ID`와 대조합니다. 초기 구성의 파일명은 `개인 여행 마스터 DB v1`이며 `ChatGPT` 폴더에 만들었습니다. 개인 배포의 실제 파일 ID/링크는 본인 설정과 Drive에서 확인합니다. 처음 만든 마스터의 여행 데이터는 비어 있습니다.

실제 탭·헤더를 읽고 `regions`에서 지역 ID를 찾은 뒤 해당 종류 테이블의 유한 범위에서 이름·현지명·주소·ID를 검색합니다. 동명이인/동명 지점은 주소와 지역을 함께 확인합니다. 연결 권한/도구에 따라 편집 가능 여부는 달라지며 Codespaces 인증과 무관합니다.

## 입력

전체 JSON Schema는 `docs/input.schema.json`이며 `travel-db schema`로 다시 생성합니다. 실행 가능한 가상 예제는 `examples/sample.json`에 있습니다.

기존 JSON 1.0과 자동 분류 입력 JSON 1.1을 함께 지원합니다. 기존 저장용 시트 헤더는 그대로이며 3단계에서 운영 탭 두 개를 추가합니다. 1.1 예제는 `examples/classification.json`, 분류 사전과 중복 판정 기준은 [자동 분류 문서](classification.md)에 있습니다. 승인/수정/복구는 별도 `docs/operation.schema.json`과 [검토 계약](review.md)을 사용합니다.

```json
{
  "schema_version": "1.0",
  "request_id": "user-research-20261011-001",
  "items": []
}
```

위 봉투의 items는 실제 입력 시 1~500개여야 합니다. 항목 종류 kind는 region, food, restaurant, experience, transport, budget, rating 중 하나입니다. 공통 입력 필수 키는 region_id, name, local_name, sources, researched_on, last_verified_on, verification_status입니다. id는 신규 항목에 생략 가능하며 생성·수정 시각 및 category/source_url 요약은 프로그램이 만듭니다. 국가 외 항목은 실제 지역 ID가 필요합니다.

sources는 `{url, title?, publisher?, accessed_on}` 배열입니다. 출처 URL은 HTTP(S)이며 실제 열람한 주소만 넣습니다. 개인정보·인증 토큰이 붙은 URL은 제출하지 않습니다. 조사일/열람일/최종 확인일은 YYYY-MM-DD, 미래일은 불가합니다. verified는 실제 근거와 최종 확인일을 확인했을 때만 선언하며 코드가 URL 내용을 자동 검증하지는 않습니다.

모르는 값은 null, 배열이 확인되지 않았으면 빈 배열입니다. local_name과 last_verified_on도 누락하지 말고 null로 명시합니다. 가격은 `{amount: "1000", currency: "JPY", basis: "1인 1식"}`처럼 금액·ISO 통화·기준을 함께 쓰거나 null로 둡니다. 근거 없는 추정 가격을 만들지 않습니다. 영업시간 등 현재 스키마에 없는 필드는 넣지 않습니다.

지역 계층은 국가→광역지역→행정구역→도시→세부지역 순서이며 중간 수준은 생략 가능합니다. 새 지역과 연결되는 항목을 같은 배치에 넣으려면 지역에 명시적 ID를 지정합니다. 서비스는 지역/음식부터 처리하므로 JSON 배열 순서는 자유롭습니다.

JSON 1.1에서는 `region_id`를 생략/null로 두고 `country`, `city`, `region`을 이름·현지명·등록된 별칭 또는 ID로 입력할 수 있습니다. country에는 등록된 국가의 ISO 코드도 사용할 수 있습니다. category는 분류 사전의 이름으로 입력합니다. kind는 생략하거나 기존 코드/분류 사전 이름을 사용할 수 있습니다. kind/category가 없으면 하나의 세부 유형 필드(level/food_type/experience_type/transport_type/budget_type/target_id)로 명확히 정해질 때만 종류를 추론합니다. 항목 이름이나 주소에서 국가/도시/종류를 추측하지 않습니다.

명시적 region_id와 힌트는 서로 일치해야 합니다. 같은 이름의 도시가 여러 국가에 있으면 country로 범위를 좁힙니다. 같은 국가 안에서 여러 후보가 남으면 ID를 지정해야 합니다. 새 지역은 검증된 Region 항목으로 같은 배치에 포함해야 합니다. 신규 국가에는 힌트 없이 country_code를 명시합니다. 신규 하위 지역은 등록된 부모 또는 힌트가 가리키는 부모에서 국가 코드/부모 ID를 보완할 수 있습니다. 나머지 종류별 필수 필드와 출처·확인일 규칙은 1.0과 같습니다.

인증 없는 `validate`는 1.1의 입력 형식과 분류 사전을 검사하며 `validation_mode: input_only`로 표시합니다. 지역 조회, 종류별 최종 검증, 외래키와 중복 판정은 `ingest` dry-run에서 수행합니다. validate 성공을 저장 가능한 최종 검증 완료로 표현하지 않습니다.

request_id는 사용자 요청마다 고유하고, 동일 요청 재시도에는 그대로 사용합니다. 내용을 수정할 때는 새 request_id를 사용합니다. 같은 ID에 다른 payload는 거부됩니다. 유사 이름/주소 후보는 사용자 확인 없이 합치지 않습니다.

## Drive로 직접 제출

1. 사용자 조사 명령을 받은 경우에만 조사합니다. 기존 항목·지역·출처를 읽고 JSON을 작성합니다.
2. 마스터 메타데이터와 `inbox!A1:C...`의 유한 범위를 읽습니다. 같은 request_id가 있는지 확인합니다.
3. 편집 도구가 허용되면 `inbox`의 새 행에 request_id, 전체 JSON 문자열(payload_json), UTC submitted_at을 넣습니다. 모든 값을 stringValue로 작성하여 수식으로 실행되지 않게 합니다. 한 셀에 들어가는 payload_json은 40,000자 이하로 나눕니다.
4. 사용자가 Codespaces에서 `travel-db process-inbox`로 검사하고 `--apply`로 처리합니다. 연결 자체는 Codespaces 코드를 실행하지 않습니다. 각 요청에 필요한 새 지역과 항목을 같은 JSON에 묶으세요. 여러 요청의 dry-run은 각각 현재 DB를 기준으로 합니다.
5. `ingestion_runs`에서 request_id와 보고서를 확인한 뒤 사용자에게 결과를 보고합니다. inbox는 제출 기록이며 실행 여부는 영수증으로 판단합니다. 직접 본 테이블을 수정하면 Python 검증/이력을 우회하므로 여행 데이터 변경도 inbox의 작업 JSON 또는 Codespaces CLI를 사용합니다.

편집 도구가 없으면 JSON을 사용자가 Codespaces 파일로 전달하고 `travel-db ingest <파일>`을 실행합니다. Drive 연결만으로 Python이 자동 실행되거나 동기화되지는 않습니다.

## 출력

ingest는 request_id, dry_run, replayed, committed, created, skipped, queued, results를 JSON으로 반환합니다. results에는 원래 item_index와 상태, ID, 테이블 또는 대기 사유가 있습니다. 기존 변경 후보는 changes에 필드별 before/after를 포함합니다. 1.1은 classification에 정해진 kind/region_id/상위 country_id/city_id와 정규화 규칙을 담습니다. Region 항목의 상위 ID는 부모 계층 기준입니다. 유사 중복 후보는 duplicate_candidates에 기존 ID, score, policy_version, evidence(이름 유사도/주소/거리 등)를 담습니다. 이 근거는 대기열 errors_json에도 기록합니다. dry_run=true에서는 어떤 행도 쓰지 않습니다. replayed=true이면 수치는 이번 추가 수가 아니라 이미 완료된 요청의 원래 보고서입니다.

형식 오류/미확인/참조 오류는 verification_queue에 원문과 이유를 남깁니다. 요청 봉투 오류는 저장하지 않고 종료 코드 2를 반환합니다. 항목별 대기 처리는 정상 처리이므로 종료 코드 0이며 queued를 확인해야 합니다. process-inbox의 봉투 오류는 해당 요청 결과에 error로 표시하고 실행되지 않습니다.

분류 실패는 classification_error와 candidate_ids/원인을, 유사 중복은 duplicate_candidate와 비교 근거를 남깁니다. 동일 배치 안의 앞서 추가 계획에 들어간 항목도 후보 검사에 포함합니다. 새로운 request_id로 반복 조사하면 대기·영수증·이력이 새 요청의 기록으로 추가될 수 있으며, 동일 요청 재시도에는 같은 request_id를 사용합니다.

ChatGPT는 조사 건수, 신규 저장, 동일 항목 건너뜀, 검증 대기, 실제 반영 여부를 구분해 보고합니다. dry-run 결과를 저장 완료로 표현하지 않습니다. 출처나 확인하지 못한 사실을 만들어내지 않습니다. 대기열을 자동으로 본 테이블에 옮기지 않습니다.

## 승인·업데이트·복구

사용자의 명시적 검토/수정 지시를 받은 뒤 `pending`의 원문·오류·후보와 실제 본 테이블을 확인합니다. 작업 JSON은 action=approve/reject/update/restore, 고유 request_id, reviewer, reason을 공통으로 요구합니다. 승인과 업데이트는 검증된 전체 Entity 및 실제 필드명→출처 URL 배열인 field_sources를 포함합니다. 잘못된 원문을 부분적으로 패치하거나 자동으로 verified로 바꾸지 않습니다. 검토한 완성 항목을 작성합니다.

업데이트와 복구의 expected_hash는 최신 `travel-db inspect <ID>` 결과를 그대로 사용합니다. 해시를 추정하거나 0으로 채우지 않습니다. Google Drive의 행 읽기만으로 해시를 임의 재구성하지 마세요. Codespaces inspect 결과를 확보한 후 작업 JSON을 제출합니다. 검토 동안 값이 바뀌면 중단하고 다시 검토합니다.

작업 JSON도 inbox!A:C에 기존과 같은 방식으로 추가할 수 있습니다. `action` 키가 있으면 `process-inbox`가 작업 계약으로 처리하며 신규 조사와 같은 멱등성 영수증을 사용합니다. 수정한 내용은 새로운 request_id를 사용합니다. 승인 기록은 review_decisions, 필드 근거는 field_sources에서 읽고 현재 버전은 inspect로 확인합니다. 실제 파일의 누락 탭은 먼저 init으로 초기화해야 합니다.

작업 출력은 action, status(created/updated/unchanged/restored/rejected), changes, field_sources(before/after), history_id, expected_hash_after, dry_run, committed, replayed를 포함합니다. 승인/거부에는 queue_id와 decision_id가 있습니다. 거부 결과에는 여행 항목/출처 해시가 없고 대기 원문은 보존됩니다. 오류는 종료 코드 2이며 부분 승인이나 부분 수정 없이 중단합니다. replayed=true는 이전 작업의 결과이며 이번 쓰기 건수가 아닙니다.

## 사용자 조사 계획과 페이지

사용자가 조사 범위·건수를 지시하면 `docs/research.schema.json` 계약에 따라 request_id, category, count 및 country/city/region/region_id를 작성합니다. `travel-db plan-research <파일>`은 등록된 지역을 확인하고 기존 항목의 ID·이름·현지명·출처·위치와 필요한 입력 필드를 반환합니다. 실제 정보를 수집하거나 저장하지 않습니다. 미등록/모호한 지역은 오류이며, 먼저 조사하여 검증된 지역을 등록하거나 기존 ID를 지정합니다. 요청 건수에서 기존 항목 수를 자동으로 차감하지 않습니다.

계획의 existing_items는 기본 50개씩 반환하며 next_offset과 snapshot_digest로 다음 페이지를 요청합니다. JSON의 offset/existing_limit과 선택적 expected_digest를 사용합니다. 계획 지침에 따라 실제 출처를 조사한 후 기존 1.0/1.1 조사 입력을 별도로 작성합니다. 계획 JSON을 inbox에 조사 결과 대신 제출하지 않습니다.

대량 읽기는 `page --table <탭> --start-row 2 --page-size 200`의 rows/next_row를 사용합니다. 반환한 row_number는 실제 1 기반 Sheets 행 번호입니다. grid_rows는 할당 행 수이며 실제 항목 수와 다릅니다. 페이지는 헤더만 검사하며 본 테이블 검증 완료를 의미하지 않습니다. 직접 연결 도구의 범위도 메타데이터로 확인하고 유한 범위로 나눕니다. 검색 페이지는 `search --offset ... --expected-digest ...`로 값 변경을 확인합니다.

백업 및 복구 계약과 라이브 테스트 파일은 [4단계 문서](workflows.md)에 있습니다. 테스트 Sheets의 'E2E 가상' 항목을 실제 여행 정보로 조사/추천하지 않습니다.

## 운영 점검 결과

`travel-db audit`는 전체 DB/참조와 영수증·대기·이력의 기본 기록 형식, 입력함 봉투와 영수증 충돌을 읽기 전용으로 확인합니다. healthy, error_count/warning_count, verification_queue 및 inbox 집계를 보고합니다. 오류 상세는 탭/행/코드만 표시합니다. 오류가 없더라도 대기·잘못된 입력함 경고가 있을 수 있습니다. 입력 봉투 검사 성공을 개별 항목 검증이나 저장 완료로 표현하지 않습니다. 실제 사용 순서와 장애 대응은 [5단계 운영 문서](operations.md)를 따릅니다.
