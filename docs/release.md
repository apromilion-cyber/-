# 6단계 배포 준비

이 프로젝트는 Codespaces CLI와 Google Sheets로 운영합니다. 배포 준비는 설치 버전 고정·Python 패키지 생성·설치 검사·GitHub CI 구성이며 서버나 유료 서비스를 추가하지 않습니다. 이 문서의 패키지 빌드는 Google 인증/실제 데이터 없이 실행합니다.

## 버전을 고정한 설치

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
pytest -q
```

requirements.lock은 확인한 실행/개발/빌드 의존성의 정확한 버전입니다. 프로젝트 자체와 pip는 포함하지 않습니다. hash lock이 아니므로 동일 배포 파일의 암호학적 고정까지 보장하지 않습니다. Python 3.11/3.14의 CI에서 설치와 패키지 검사를 실행하도록 구성합니다. 버전 변경은 pyproject의 범위와 함께 검토하고 새 환경에서 pip check/pytest/빌드 검사를 다시 실행하세요.

## 배포 파일 빌드와 검사

```bash
python -m build --no-isolation
python scripts/check_distribution.py dist/personal_travel_db-0.1.0-py3-none-any.whl dist/personal_travel_db-0.1.0.tar.gz
```

sdist는 MANIFEST.in의 명시적인 파일 목록만 포함합니다. 공개 모듈·문서·가상 예제·테스트·CI 구성을 담으며 새 파일을 추가할 때 목록도 갱신합니다. wheel은 Python 모듈과 배포 메타데이터만 포함합니다. 로컬 입력/백업·.env·키 파일·가상 환경은 목록에 넣지 않습니다.

검사 명령은 목록 밖 파일/누락 파일, 코드와 작업 파일의 불일치, 잘못된 메타데이터/CLI 진입점, 중복·상위 경로·링크·과도한 크기와 알려진 인증 정보 형식을 거부합니다. 실제 Google Docs/Sheets/Slides 파일 URL도 공개 파일에 포함되면 거부합니다. 출력은 아티팩트 이름·유형·SHA-256·파일 수입니다. Google API나 환경변수 Secrets는 읽지 않습니다. 패턴 검사만으로 모든 개인정보를 탐지하지는 못하므로 공개 목록에 추가하는 파일은 내용을 검토하세요.

## GitHub 반영과 CI

작업 파일을 검토한 뒤 GitHub에 반영하면 .github/workflows/tests.yml이 Python 3.11/3.14에서 고정 버전 설치, 린트/형식, pytest, 입력 계약, sdist/wheel 빌드·검사와 설치된 wheel의 CLI 검사를 실행합니다. CI에는 Google 인증을 전달하지 않으며 실제 시트를 수정하지 않습니다.

원격 CI 성공은 반영한 정확한 커밋의 결과로 확인해야 합니다. 로컬 검사 성공을 원격 CI 성공으로 보고하지 않습니다. 원격 반영 전 검토용 파일 목록은 MANIFEST.in과 git status에서 확인합니다. 빌드 산출물은 dist/에 만들며 .gitignore로 제외합니다. 원본 DB는 계속 Google Sheets이고 wheel/sdist는 데이터 백업이 아닙니다.

검사 결과와 실행 여부는 [검증 기록](verification.md)에 기록합니다.

2026-10-11 로컬 결과: 저장소 및 별도 sdist에서 pytest 각각 260개 통과, wheel/sdist 내용 검사와 별도 경로에 설치한 CLI 검사 통과. 고정 의존성도 기존 가상환경과 분리하여 재설치했습니다. 로컬 Python은 3.14.2입니다. 실제 Google API를 호출하거나 마스터를 수정하지 않았습니다.

## 7단계 GitHub 반영 결과

공개 저장소 apromilion-cyber/-의 codex/travel-db-initial 브랜치에 검토한 51개 파일을 커밋·푸시했습니다. 개인 마스터/테스트 파일 식별자가 들어 있던 안내는 .local/deployment-docs-before-publication/에 보존하고 Git/배포 목록에서 제외했습니다. 공개 안내는 본인 시트 ID와 Codespaces Secrets를 기준으로 설정합니다. 실제 Google 문서 파일 URL도 배포 검사에서 거부합니다.

첫 코드 커밋 f15a399379fbd71411e8a10861d67b91e744ba9c의 [원격 CI](https://github.com/apromilion-cyber/-/actions/runs/38117838575)가 Python 3.11/3.14 모두 통과했습니다. 고정 설치·pip check·린트/형식·pytest·예제 계약·배포 빌드/검사·저장소 밖 wheel 설치/CLI 단계를 확인했습니다.

[초안 PR #1](https://github.com/apromilion-cyber/-/pull/1)에서 검토할 수 있습니다. main 병합·릴리스 발행은 별도 사용자 지시로 실행합니다. 후속 커밋의 최신 CI는 PR Checks의 head SHA로 확인합니다. 이 단계에서 Google Sheets API를 호출하거나 마스터를 수정하지 않았습니다.
