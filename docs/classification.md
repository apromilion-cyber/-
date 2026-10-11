# 2단계: 자동 분류 및 유사 중복 후보

## 입력 예제

JSON 1.1은 명시적 ID 방식과 힌트 방식을 함께 지원합니다. 아래는 입력 형태를 설명하는 가상 항목입니다. 사용할 지역은 원본 Sheets에 이미 등록되어 있거나 같은 배치에서 먼저 검증되어야 합니다.

```json
{
  "schema_version": "1.1",
  "request_id": "sample-hints-001",
  "items": [{
    "category": "카페",
    "country": "JP",
    "city": "Sample City",
    "name": "가상 샘플 카페",
    "local_name": null,
    "sources": [{"url": "https://example.com/sample-cafe", "accessed_on": "2026-01-01"}],
    "researched_on": "2026-01-01",
    "last_verified_on": "2026-01-01",
    "verification_status": "verified",
    "price": null
  }]
}
```

`examples/classification.json`은 새 지역 2개, 음식점 1개, 유사 중복 후보 1개, 미등록 도시 항목 1개를 포함합니다. 빈 테스트 저장소에서 신규 3개·검증 대기 2개를 기대합니다. 반복 실행은 같은 request_id로 합니다. 실제 마스터에 샘플을 apply하지 마세요.

```bash
travel-db validate examples/classification.json
travel-db ingest examples/classification.json
```

1.1의 validate는 입력 형식과 분류 사전만 검사합니다. ingest dry-run이 원본 지역 조회와 최종 Pydantic/참조/중복 검사를 수행합니다. 같은 DB 상태에서 나온 dry-run과 apply는 같은 판정 로직을 사용합니다.

## 지역 분류

등록된 Region의 name, local_name, aliases, id를 유니코드 NFKC·대소문자·공백/구두점 정규화 후 정확히 비교합니다. 국가 코드는 등록된 country 레코드의 country_code와 비교합니다. 자동 번역이나 외부 검색은 수행하지 않습니다.

country로 국가 범위를, city로 도시 범위를 좁히고 region으로 최종 지역을 선택합니다. 입력한 힌트가 없으면 명시적 region_id를 사용합니다. 힌트와 ID의 국가/도시/지역이 서로 다르면 대기로 보냅니다. 여러 도시가 남으면 candidate_ids를 보고하며 ID 지정이 필요합니다. 지역이 없으면 region_not_found로 분리합니다. 새 지역을 이름만으로 생성하지 않습니다.

동일 배치의 검증된 지역도 사용합니다. 국가→광역지역→행정구역→도시→세부지역 순으로 처리하고 음식 후 음식점을 처리합니다. 검증 대기에 들어간 지역은 다른 항목의 참조 대상으로 사용하지 않습니다.

## 카테고리 사전

사전은 `src/travel_db/classification.py`에 있으며 서비스 정책용 코드입니다. 지역 DB를 대체하거나 여행 사실을 저장하지 않습니다. 주요 입력은 다음과 같습니다.

| 입력 | 종류 및 세부 유형 |
| --- | --- |
| 음식점, 식당, 맛집, restaurant, レストラン | restaurant |
| 관광지 / 카페·café·カフェ / 액티비티 / 쇼핑 / 자연명소 | experience: attraction / cafe / activity / shopping / nature |
| 공항 / 기차역·철도역 / 지하철역 / 교통편·노선 | transport: airport / train_station / subway_station / route |
| 숙박비 / 교통비 / 식비 / 여행총비용 | budget: lodging / transport / food / trip_total |
| 국가 / 광역지역 / 행정구역 / 도시 / 세부지역 | region: country / macro_region / administrative_area / city / neighborhood |
| 라멘·ラーメン / 면요리 / 밥요리 / 국물요리 / 해산물요리 / 고기요리 / 디저트 / 음료 / 길거리음식 | food: ramen / noodles / rice / soup / seafood / meat / dessert / beverage / street_food |
| 지역 / 음식 / 체험 / 교통 / 예산 / 개인평가 | 각 종류 코드. 필요한 세부 유형 필드는 별도 입력 |

세부 유형의 기존 영어 코드와 사전 별칭도 허용합니다. budget_type의 food/transport는 budget 문맥으로 구분합니다. 기존 자유 형식 food_type은 유지합니다. 등록되지 않은 category, 불명확한 kind, 명시적 kind/세부 유형/category 충돌은 대기로 보냅니다. 나라·도시·가격·재료 등 여행 사실을 이름만으로 채우지 않습니다.

## 유사 중복 판정

기존 ID와 결정적 식별키 비교가 우선입니다. 같은 식별키의 다른 ID는 정확 중복 후보(점수 1.0)입니다. 이후 같은 종류와 지역 범위에서 아래 규칙으로 유사 후보를 찾습니다. 장소는 같은 도시의 세부 지역까지 비교하며, 지역 레코드는 국가 코드/수준/부모가 같아야 합니다. 체험/교통의 세부 유형과 알려진 노선 출발지/도착지가 다르면 비교 대상에서 제외합니다. 예산과 개인 평가는 기존 명시적 식별키만 사용합니다.

이름은 name/local_name의 모든 조합에서 정규화된 문자열을 difflib.SequenceMatcher로 비교합니다. 주소는 정규화 문자열과 숫자 토큰을 함께 비교합니다. 좌표는 두 항목 모두 제공된 경우에만 Haversine 거리로 비교합니다. 알 수 없는 좌표는 None으로 두며 0을 대신 넣지 않습니다.

| 근거 | 검토 점수 |
| --- | --- |
| 국가 레코드의 동일 ISO 국가 코드 | 0.99 |
| 이름 유사도 ≥0.90이며 같은 주소 또는 거리 ≤100m | 0.97 |
| 같은 부모/수준의 지역명·현지명·별칭 일치 | 0.95 |
| 이름 유사도 ≥0.75이며 같은 주소 | 0.93 |
| 이름 정확 일치이며 주소가 한쪽에 없거나 주소 유사도 ≥0.90 및 숫자 토큰 일치 | 0.90 |
| 이름 유사도 ≥0.94이며 주소가 한쪽에 없음 | 0.85 |

거리 >300m이고 주소가 다르면 동명 지점 구분을 위해 후보에서 제외합니다. 주소가 같은데 좌표가 크게 다르면 충돌 근거를 붙여 검토합니다. 주소나 가까운 좌표만 일치하고 이름이 충분히 비슷하지 않으면 후보로 만들지 않습니다.

기본 후보 임계값은 0.85이며 경계값을 포함합니다. 점수는 검토 우선순위를 표시합니다. 점수 내림차순·ID 오름차순으로 최대 5개를 보고합니다. `DuplicatePolicy`에서 이름 기준·거리·후보 임계값·최대 후보 수를 정의하며 정책 버전은 1입니다. DB에 넣는 사실값을 보완하거나 병합하는 데 사용하지 않습니다.

후보는 원본 입력과 evidence를 verification_queue에 보관하고 dry-run/처리 보고서에도 표시합니다. 3단계의 [검토·수정 명령](review.md)으로 승인/거부하며 기존 대상 업데이트 또는 확인한 별개 항목 신규 생성을 명시합니다. 근거가 적은 항목은 후보를 놓치거나 별개 항목을 후보로 잡을 수 있으므로 자동 병합하지 않습니다.
