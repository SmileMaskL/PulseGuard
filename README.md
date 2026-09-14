# PulseGuard

가벼운 PC 상태 감시 프로그램. CPU·메모리·디스크 사용량을 주기적으로 확인해서
로컬 로그에 남기고, Pro 구독 시 임계치 초과를 웹훅(디스코드/슬랙)으로 알려줍니다.

- 무료(평생): 로컬 로그 기록, PC 화면 알림, 로컬 실시간 대시보드(http://localhost:8765) — 전부 인터넷 통신 없음
- Pro(월 2,900원 구독): 위 무료 기능 전부 + 디스코드/슬랙 원격 알림, CSV 내보내기, 더 긴 로그 보관

## 설치

**Windows**: [releases/PulseGuard.exe](releases/PulseGuard.exe)를 내려받아 더블클릭 —
Python 설치 필요 없음.

**소스코드로 직접 실행(Mac/Linux 또는 개발용)**:
```
pip install -r requirements.txt
python pulseguard.py
```

psutil이 없어도 Linux에서는 자동으로 /proc 기반 대체 로직으로 동작합니다
(Windows/Mac은 psutil이 필요합니다).

## exe 다시 빌드하기 (코드 수정 후)

Windows에서:
```
pip install pyinstaller psutil
pyinstaller --onefile --name PulseGuard --console pulseguard.py
```
`dist/PulseGuard.exe`가 생성되면 `releases/PulseGuard.exe`로 옮겨서 커밋.

## Pro 활성화

```
python pulseguard.py activate <라이선스키>
```

## 웹 소개 페이지

https://smilemaskl.github.io/pulseguard/
