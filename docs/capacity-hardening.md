# 단일 노드 capacity 하드닝 관측 runbook

이 문서는 배포 승인 전 장기 관측 절차다. 변경은 기존 node-exporter의 `node_cpu_seconds_total{mode="steal"}`, `node_pressure_cpu_waiting_seconds_total`과 kube-state-metrics만 사용하며 새 exporter나 권한을 만들지 않는다.

## 지표 읽기

- CPU steal: 5분 rate가 5%를 15분 넘으면 warning, 15%를 10분 넘으면 critical이다.
- CPU PSI: waiting 비율이 10%를 15분 넘으면 warning, 25%를 10분 넘으면 critical이다.
- `requests / allocatable`: 85%를 30분 넘을 때만 warning이다. 새 Pod 배치 여유를 뜻한다.
- `limits / allocatable`: 사람이 burst 상한을 이해하기 위한 recording rule이다. limits 초과만으로 critical을 만들지 않는다.
- 원본 metric이 15분 없으면 warning으로 관측 공백을 알린다. 서비스 장애 critical로 오인하지 않는다.

## 24시간

배포 후 첫 24시간 동안 Grafana Explore에서 CPU steal, CPU PSI, requests / allocatable, limits / allocatable의 max와 시간대를 기록한다. warning/critical 발생 시 같은 시간대의 요청 오류·응답 지연·Pod Pending을 함께 확인한다. metric absent alert가 없어야 한다.

## 7일

평일/주말과 배치 시간대를 포함해 7일 추이를 비교한다. requests 85% warning이 실제 Pending 또는 PSI 상승과 동반되는지 확인하고, 동반되지 않으면 threshold 조정 근거를 남긴다. limits / allocatable은 참고값이며 단독 증설 근거로 쓰지 않는다.

## 14일

14일 동안 critical 0건, 지속 warning의 원인 분류 완료, 원본 metric 공백 0건을 acceptance로 삼는다. CPU steal과 CPU PSI의 p95/max, requests/limits 비율, 사용자 영향 동반 여부를 승인 기록에 남긴다. 기준 미달이면 workload 축소 또는 노드 증설을 설계하고 자동 변경하지 않는다.

## CloudWatch 권한 blocker

EC2 host/하이퍼바이저와 CloudWatch 교차 검증은 현재 IAM read 권한이 없으므로 **권한 blocker**다. 필요한 최소 read-only 범위와 계정 소유자 승인을 별도 요청한다. 이 PR에서는 AWS mutation 금지이며 IAM, alarm, dashboard, agent를 생성·수정하지 않는다. 권한 확보 전에는 Prometheus 장기 관측 결과만 근거로 사용하고 CloudWatch 확인 완료라고 표시하지 않는다.

## 알림 정책

annotations는 쉬운 한국어 `status`(상태), `impact`(영향), `check`(확인)로 제공한다. `@channel` 문자열은 rule에 넣지 않으며 기존 Sentinel 정책이 firing critical에만 멘션을 붙인다.
