from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "bootstrap" / "01-install-k3s.sh"
RUNTIME_DOC = ROOT / "docs" / "container-runtime.md"


class K3sDockerRuntimeContractTest(unittest.TestCase):
    def test_bootstrap_installs_and_enables_docker_before_k3s(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("exec /usr/bin/env -i", script)
        self.assertIn('PINLOG_K3S_BOOTSTRAP_CLEAN_ENV="$$"', script)
        self.assertIn("PATH=/usr/sbin:/usr/bin:/sbin:/bin", script)
        self.assertIn("HOME|LC_ALL|PATH|PINLOG_K3S_BOOTSTRAP_CLEAN_ENV|PWD|SHLVL|_", script)
        self.assertIn("*) clean_environment=false", script)
        self.assertIn('DOCKER_PACKAGE_VERSION="29.1.3-0ubuntu3~24.04.2"', script)
        self.assertIn('dpkg --print-architecture) != "amd64"', script)
        self.assertIn("timeout --signal=TERM --kill-after=30s 900s", script)
        self.assertIn('apt-get install -y "docker.io=${DOCKER_PACKAGE_VERSION}"', script)
        self.assertIn("systemctl enable docker.service", script)
        self.assertIn("systemctl start --no-block docker.service", script)
        self.assertIn("docker_deadline=$((SECONDS + 120))", script)
        self.assertIn("DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker info", script)
        self.assertIn("dpkg-query -S /usr/bin/docker", script)
        self.assertLess(
            script.index('apt-get install -y "docker.io=${DOCKER_PACKAGE_VERSION}"'),
            script.index("installer_url="),
        )

    def test_bootstrap_declares_docker_runtime_and_boot_order(self):
        script = INSTALLER.read_text(encoding="utf-8")

        for required in (
            "docker: true",
            "Requires=docker.service",
            "After=docker.service",
            "systemctl daemon-reload",
            "docker://",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)

    def test_bootstrap_rejects_any_existing_kubernetes_state(self):
        script = INSTALLER.read_text(encoding="utf-8")

        for required in (
            "/etc/rancher/k3s",
            "/etc/rancher",
            "/var/lib/rancher/k3s",
            "/var/lib/rancher",
            "/var/lib/kubelet",
            "/etc/cni/net.d",
            "/var/lib/cni",
            "/run/k3s",
            "/usr/local/bin/k3s-uninstall.sh",
            "/etc/systemd/system/k3s.service.env",
            "/etc/systemd/system/k3s-agent.service",
            "/etc/rancher/node",
            "/etc/default/k3s",
            "/etc/sysconfig/k3s",
            "자동 삭제하지 않습니다",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)
        self.assertIn('[[ -e "${path}" || -L "${path}" ]]', script)

    def test_bootstrap_rejects_existing_docker_or_containerd_state(self):
        script = INSTALLER.read_text(encoding="utf-8")

        for required in (
            "docker-ce",
            "moby-engine",
            "containerd.io",
            "/var/lib/docker",
            "/etc/docker",
            "/etc/default/docker",
            "/etc/sysconfig/docker",
            "/var/lib/containerd",
            "/etc/containerd",
            "docker.socket",
            "/var/snap/docker",
            "/snap/docker",
            "snap.docker.dockerd.service",
            "/usr/bin/snap list docker",
            "timeout --signal=TERM --kill-after=5s 15s /usr/bin/snap list docker",
            '[[ ${snap_result} != "error: no matching snaps installed" ]]',
            "Snap Docker 설치 여부를 안전하게 확인할 수 없습니다",
            "docker_*.snap",
            "symlink 조상",
            "기존 container runtime",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)
        self.assertLess(
            script.index("기존 container runtime package"),
            script.index('apt-get install -y "docker.io=${DOCKER_PACKAGE_VERSION}"'),
        )

    def test_k3s_installer_is_revision_and_checksum_pinned(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertNotIn("curl -sfL https://get.k3s.io |", script)
        self.assertIn("curl --disable -fsSL", script)
        self.assertIn('K3S_INSTALL_COMMIT="78ef2d8a892fd3eb2e9d641a95713f74f51cbded"', script)
        self.assertIn('K3S_INSTALL_SHA256="d264d4d43f7c5a27b44de0075513fb22dfb02d0b7cd33ba7a3838cb822f4729c"', script)
        self.assertIn('K3S_BINARY_SHA256="65a55ec56c24eab44383086166ec620a491952b7e23941a49ddca6e8a4c4b4de"', script)
        self.assertIn('"${K3S_BINARY_SHA256}" /usr/local/bin/k3s | sha256sum -c -', script)
        self.assertLess(
            script.index("sha256sum -c -"),
            script.index('sh "${installer_tmp}" server'),
        )
        self.assertLess(
            script.index("INSTALL_K3S_SKIP_START=true"),
            script.index("systemctl start --no-block k3s.service"),
        )
        self.assertIn("env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root", script)
        self.assertIn("unset KUBECONFIG KUBERNETES_MASTER DOCKER_HOST", script)
        self.assertIn("SYSTEMD_HOST SYSTEMD_MACHINE", script)
        self.assertIn("INSTALL_K3S_SKIP_ENABLE=true", script)
        self.assertIn("--connect-timeout 10 --max-time 120 --retry 3", script)
        self.assertIn("timeout --signal=TERM --kill-after=30s 300s", script)

    def test_failed_initial_install_has_bounded_waits_and_rollback(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertGreaterEqual(script.count("deadline=$((SECONDS + 300))"), 2)
        self.assertIn("timeout --signal=TERM --kill-after=5s 30s", script)
        self.assertIn("systemctl stop k3s.service", script)
        self.assertIn("systemctl disable k3s.service", script)
        self.assertIn("systemctl is-active --quiet k3s.service", script)
        self.assertIn("systemctl is-enabled --quiet k3s.service", script)
        self.assertNotIn("/usr/local/bin/k3s-uninstall.sh >/dev/null", script)
        self.assertNotIn("rm -rf", script)
        self.assertIn("rmdir /etc/rancher/k3s", script)
        self.assertIn("/etc/rancher /etc/systemd/system/k3s.service.d", script)
        self.assertIn('[[ -z "${runtime_config}" ]] || rm -f "${runtime_config}"', script)
        self.assertIn('[[ -z "${runtime_dropin}" ]] || rm -f "${runtime_dropin}"', script)
        self.assertLess(
            script.index("trap cleanup_install EXIT"),
            script.index("install -d -m 700 /etc/rancher/k3s"),
        )
        self.assertLess(
            script.index("trap cleanup_install EXIT"),
            script.index('sh "${installer_tmp}" server'),
        )
        self.assertLess(
            script.index("bootstrap_complete=true"),
            script.rindex("trap - EXIT"),
        )

    def test_bootstrap_refuses_to_migrate_an_existing_cluster(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("command -v k3s", script)
        self.assertIn("k3s binary 또는 service가 이미 있습니다", script)
        self.assertIn("migration/복구 절차", script)
        self.assertLess(
            script.index("k3s binary 또는 service가 이미 있습니다"),
            script.index('apt-get install -y "docker.io=${DOCKER_PACKAGE_VERSION}"'),
        )

    def test_docker_systemd_dependency_is_prepared_before_installer(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertLess(
            script.index("Requires=docker.service"),
            script.index("installer_url="),
        )
        self.assertIn("systemctl show k3s -p Requires --value", script)
        self.assertIn("systemctl show k3s -p After --value", script)

    def test_service_start_and_http_smoke_are_bounded(self):
        script = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("systemctl start --no-block k3s.service", script)
        self.assertNotIn("systemctl enable --now k3s.service", script)
        self.assertIn('if ! code=$(curl -sk', script)
        self.assertNotIn('|| echo "000"', script)
        self.assertIn("k3s kubectl --request-timeout=10s get nodes", script)
        self.assertIn("k3s kubectl --request-timeout=10s get deploy traefik", script)
        self.assertIn("timeout --signal=TERM --kill-after=5s 310s", script)
        self.assertIn(
            "busybox@sha256:b7f3d86d6e84fc17718c48bcde1450807faa2d56704205c697b4bd5df7b9e29f",
            script,
        )
        self.assertNotIn("--image=busybox:1.36", script)

    def test_runtime_contract_is_documented_and_indexed(self):
        self.assertTrue(RUNTIME_DOC.is_file())
        document = RUNTIME_DOC.read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        runbook = (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")

        self.assertIn("[컨테이너 runtime](docs/container-runtime.md)", readme)
        for required in (
            "Docker Engine",
            "cri-dockerd",
            "docker://",
            "containerd",
            "docker run",
            "Deployment",
            "stale",
            "rollback",
        ):
            with self.subTest(required=required):
                self.assertIn(required, document)
        digest_ref = (
            "busybox@sha256:"
            "b7f3d86d6e84fc17718c48bcde1450807faa2d56704205c697b4bd5df7b9e29f"
        )
        self.assertIn(digest_ref, readme)
        self.assertIn(digest_ref, runbook)
        self.assertNotIn("--image=busybox ", readme)
        self.assertNotIn("--image=busybox:1.36", runbook)


if __name__ == "__main__":
    unittest.main()
