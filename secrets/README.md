# 시크릿 관리 (Sealed Secrets)

**이 저장소는 public이다.** 평문 Secret을 절대 커밋하지 말 것.

여기에는 `*.sealedsecret.yaml` 파일만 들어간다. SealedSecret은 클러스터의
컨트롤러 개인키로만 복호화 가능하므로 공개 저장소에 커밋해도 안전하다.

## 왜 Sealed Secrets인가

- **External Secrets** — Vault나 AWS Secrets Manager 같은 백킹 스토어가 필요한데,
  이 팀에는 AWS API 자격증명이 없다. 애초에 불가능.
- **SOPS** — age 키 배포 + ArgoCD ksops 플러그인(= 커스텀 repo-server 이미지)이
  필요하다. 하루가 들고 유지보수 표면이 영구히 남는다.
- **Sealed Secrets** — 컨트롤러 1개(~50Mi) + CLI 1개. 학생 팀 운영 역량에 맞는다.

## 시크릿 만들기

```bash
kubectl create secret generic postgres-credentials \
  --namespace pinlog-prod \
  --from-literal=password='실제비밀번호' \
  --dry-run=client -o yaml \
| kubeseal --format yaml \
    --controller-name sealed-secrets-controller \
    --controller-namespace kube-system \
> prod/postgres-credentials.sealedsecret.yaml
```

생성된 파일을 커밋하면 ArgoCD가 동기화하고, 컨트롤러가 클러스터 안에서 복호화한다.

SealedSecret은 **네임스페이스에 묶인다**. `pinlog-prod`용으로 봉인한 것을
`pinlog-dev`에서 쓸 수 없다 — 의도된 동작이므로 각각 만든다.

## ⚠️ 컨트롤러 개인키 백업 (첫날에 할 것)

이 키를 잃고 클러스터를 재구축하면 **저장소의 모든 SealedSecret이 영구히
복호화 불가능**해진다. 하필 발표 압박 속에서 전부 다시 만들게 된다.

```bash
sudo k3s kubectl -n kube-system get secret \
  -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml \
  > sealed-secrets-master.key
```

팀 비밀번호 관리자에 보관하고 **로컬 파일은 삭제**한다. 절대 커밋하지 말 것.

## 필요한 시크릿 목록

| 이름 | 네임스페이스 | 키 | 용도 |
|---|---|---|---|
| `postgres-credentials` | pinlog-prod | `password` | PostgreSQL |
| (서비스별 추가) | | | JWT 시크릿, 외부 API 키 등 |
