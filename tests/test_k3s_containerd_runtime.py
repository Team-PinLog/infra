from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "bootstrap" / "01-install-k3s.sh"
RUNTIME_DOC = ROOT / "docs" / "container-runtime.md"
RUNTIME_GUARD = ROOT / "bootstrap" / "lib" / "runtime-evidence.sh"


class K3sNativeContainerdContractTest(unittest.TestCase):
    def setUp(self):
        self.script = INSTALLER.read_text(encoding="utf-8")
        self.runtime_guard = RUNTIME_GUARD.read_text(encoding="utf-8")

    def test_clean_host_bootstrap_uses_only_k3s_embedded_containerd(self):
        for obsolete in (
            'DOCKER_PACKAGE_VERSION=',
            'apt-get install -y "docker.io=',
            "systemctl enable docker.service",
            "systemctl start --no-block docker.service",
            "docker: true",
            "Requires=docker.service",
            "After=docker.service",
            "docker://",
            "dockerd --validate",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, self.script)

        self.assertIn("K3s embedded containerd", self.script)
        self.assertIn("systemctl start --no-block k3s.service", self.script)
        self.assertIn("기존 container runtime", self.script)
        self.assertFalse((ROOT / "bootstrap" / "docker-daemon.json").exists())
        self.assertFalse((ROOT / "bootstrap" / "package-service-guard").exists())

    def test_bootstrap_reexecs_in_a_minimal_environment(self):
        for required in (
            "exec /usr/bin/env -i",
            'PINLOG_K3S_BOOTSTRAP_CLEAN_ENV="$$"',
            "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME|LC_ALL|PATH|PINLOG_K3S_BOOTSTRAP_CLEAN_ENV|PWD|SHLVL|_",
            "*) clean_environment=false",
            "unset KUBECONFIG KUBERNETES_MASTER DOCKER_HOST",
            "SYSTEMD_HOST SYSTEMD_MACHINE",
        ):
            self.assertIn(required, self.script)

    def test_bootstrap_rejects_existing_cluster_and_runtime_state(self):
        for required in (
            "/etc/rancher/k3s",
            "/var/lib/rancher/k3s",
            "/var/lib/kubelet",
            "/etc/cni/net.d",
            "/var/lib/cni",
            "/run/k3s",
            "/usr/local/bin/k3s-uninstall.sh",
            "/etc/systemd/system/k3s.service.env",
            "command -v k3s",
            "k3s binary 또는 service가 이미 있습니다",
            "docker-ce",
            "moby-engine",
            "containerd.io",
            "/var/lib/docker",
            "/var/lib/containerd",
            "/run/docker.sock",
            "/run/containerd",
            "docker.socket",
            "snap.docker.dockerd.service",
            "docker_*.snap",
            "symlink 조상",
            "기존 container runtime",
            "자동 삭제하지 않습니다",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)
        self.assertIn('[[ -e "${path}" || -L "${path}" ]]', self.script)
        for runtime_evidence in (
            '"${proc_root}"/[0-9]*',
            '"${proc_dir}/comm"',
            '"${proc_dir}/cgroup"',
            '"${proc_dir}/exe"',
            "stat_before",
            "stat_after",
            '[[ "${start_before}" == "${start_after}" ]]',
            "containerd-shim",
            "cri-dockerd",
            "kubepods",
            "/var/lib/containerd/*",
        ):
            self.assertIn(runtime_evidence, self.runtime_guard)
        self.assertIn("pinlog_assert_no_runtime_evidence /proc /proc/self/mountinfo", self.script)
        self.assertLess(self.script.index("기존 container runtime package"), self.script.index("installer_url="))

    def test_k3s_installer_and_binary_are_revision_checksum_pinned(self):
        for required in (
            'K3S_VERSION="v1.36.2+k3s1"',
            'K3S_INSTALL_COMMIT="78ef2d8a892fd3eb2e9d641a95713f74f51cbded"',
            'K3S_INSTALL_SHA256="d264d4d43f7c5a27b44de0075513fb22dfb02d0b7cd33ba7a3838cb822f4729c"',
            'K3S_BINARY_SHA256="65a55ec56c24eab44383086166ec620a491952b7e23941a49ddca6e8a4c4b4de"',
            '"${K3S_BINARY_SHA256}" /usr/local/bin/k3s | sha256sum -c -',
            "curl --disable -fsSL",
            "--connect-timeout 10 --max-time 120 --retry 3",
            "INSTALL_K3S_SKIP_ENABLE=true",
            "INSTALL_K3S_SKIP_START=true",
            "timeout --signal=TERM --kill-after=30s 300s",
        ):
            self.assertIn(required, self.script)
        self.assertNotIn("curl -sfL https://get.k3s.io |", self.script)
        self.assertLess(self.script.index("sha256sum -c -"), self.script.index('sh "${installer_tmp}" server'))

    def test_failed_install_is_bounded_and_preserves_state(self):
        for required in (
            "trap cleanup_install EXIT",
            "timeout --signal=TERM --kill-after=5s 30s",
            "systemctl stop k3s.service",
            "systemctl disable k3s.service",
            "systemctl is-active k3s.service",
            "systemctl is-enabled k3s.service",
            "artifact를 보존합니다",
        ):
            self.assertIn(required, self.script)
        self.assertNotIn("/usr/local/bin/k3s-uninstall.sh >/dev/null", self.script)
        self.assertNotIn("rm -rf", self.script)
        self.assertLess(self.script.index("trap cleanup_install EXIT"), self.script.index("installer_url="))
        self.assertLess(self.script.index("bootstrap_complete=true"), self.script.rindex("trap - EXIT"))

        cleanup = self.script[self.script.index("cleanup_install()") : self.script.index("trap cleanup_install EXIT")]
        cleanup_normalized = " ".join(cleanup.replace("\\\n", " ").split())
        self.assertLess(cleanup.index("set +e"), cleanup.index('rm -f "${installer_tmp}"'))
        self.assertLess(cleanup.index("trap - EXIT"), cleanup.index('rm -f "${installer_tmp}"'))
        for operation in (
            "systemctl stop k3s.service",
            "systemctl disable k3s.service",
            "systemctl is-active k3s.service",
            "systemctl is-enabled k3s.service",
        ):
            with self.subTest(operation=operation):
                self.assertIn(
                    "timeout --signal=TERM --kill-after=5s 30s " + operation,
                    cleanup_normalized,
                )
        self.assertIn("stop 결과를 신뢰할 수 없습니다", cleanup)
        self.assertIn("disable 결과를 신뢰할 수 없습니다", cleanup)
        self.assertIn("active 상태를 검증할 수 없습니다", cleanup)
        self.assertIn("enabled 상태를 검증할 수 없습니다", cleanup)

    def test_cleanup_continues_after_rm_and_systemctl_failures(self):
        cleanup = self.script[self.script.index("cleanup_install()") : self.script.index("trap cleanup_install EXIT")]
        harness = f'''set +e
{cleanup}
installer_tmp=/tmp/fixture-installer
install_started=true
bootstrap_complete=false
rm() {{ return 1; }}
timeout() {{ shift 3; "$@"; }}
systemctl() {{
  case "$1" in
    stop) return 5 ;;
    disable) return 0 ;;
    is-active) printf '%s\\n' dbus-error; return 4 ;;
    is-enabled) printf '%s\\n' not-found; return 1 ;;
  esac
}}
false
cleanup_install
'''
        result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        for evidence in (
            "temporary file을 제거하지 못했습니다",
            "stop 결과를 신뢰할 수 없습니다",
            "active 상태를 검증할 수 없습니다",
            "enabled 상태를 검증할 수 없습니다",
            "복구 절차를 따르세요",
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, result.stderr)

    def test_runtime_evidence_guard_executes_positive_and_negative_fixtures(self):
        def run_fixture(comm, exe, cgroup, mount_target):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                proc = root / "proc"
                pid = proc / "123"
                pid.mkdir(parents=True)
                fields = ["S"] + ["0"] * 18 + ["4242"] + ["0"] * 8
                (pid / "stat").write_text("123 (fixture) " + " ".join(fields) + "\n", encoding="utf-8")
                (pid / "comm").write_text(comm + "\n", encoding="utf-8")
                (pid / "cgroup").write_text(cgroup + "\n", encoding="utf-8")
                (pid / "exe").symlink_to(exe)
                mountinfo = root / "mountinfo"
                mountinfo.write_text(
                    f"36 25 0:32 / {mount_target} rw - ext4 /dev/root rw\n",
                    encoding="utf-8",
                )
                return subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; pinlog_assert_no_runtime_evidence "$2" "$3"',
                        "fixture",
                        str(RUNTIME_GUARD),
                        str(proc),
                        str(mountinfo),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )

        clean = run_fixture(
            "backup-agent",
            "/opt/containerd-backup",
            "0::/system.slice/containerd-backup.service",
            "/var/lib/containerd-backup",
        )
        self.assertEqual(clean.returncode, 0, clean.stderr)

        deleted_runtime = run_fixture("worker", "/opt/containerd (deleted)", "0::/system.slice/worker.service", "/")
        self.assertNotEqual(deleted_runtime.returncode, 0)
        self.assertIn("runtime process/cgroup", deleted_runtime.stderr)

        runtime_mount = run_fixture("worker", "/opt/worker", "0::/system.slice/worker.service", "/var/lib/containerd")
        self.assertNotEqual(runtime_mount.returncode, 0)
        self.assertIn("runtime mount", runtime_mount.stderr)

    def test_service_start_runtime_and_network_smoke_are_bounded(self):
        for required in (
            "systemctl start --no-block k3s.service",
            "k3s kubectl --request-timeout=10s get nodes",
            "k3s kubectl --request-timeout=10s get deploy traefik",
            "timeout --signal=TERM --kill-after=5s 310s",
            "busybox@sha256:b7f3d86d6e84fc17718c48bcde1450807faa2d56704205c697b4bd5df7b9e29f",
            'if ! code=$(curl -sk',
        ):
            self.assertIn(required, self.script)
        self.assertNotIn("systemctl enable --now k3s.service", self.script)
        self.assertNotIn("--image=busybox:1.36", self.script)
        self.assertIn('[[ ! "${runtime}" =~ ^containerd://[^[:space:]]+$ ]]', self.script)

    def test_runtime_docs_define_the_verified_docker_to_containerd_migration(self):
        runtime_doc = RUNTIME_DOC.read_text(encoding="utf-8")
        architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for required in (
            "K3s embedded containerd",
            "containerd://",
            "독립 `docker run` workload가 0",
            "PostgreSQL fresh backup",
            "Kubernetes 관리 Docker container",
            "k3s-killall.sh",
            "containerd-pre-migration",
            "10-docker-runtime.conf",
            "docker: true",
            "5분 capacity gate",
            "rollback",
        ):
            with self.subTest(required=required):
                self.assertIn(required, runtime_doc)

        self.assertIn("K3s embedded containerd runtime", architecture)
        self.assertIn("containerd://<version>", architecture)
        self.assertIn("Docker/cri-dockerd의 지속 CPU 포화", architecture)
        self.assertNotIn("### 2.2.1 Docker Engine + cri-dockerd runtime", architecture)
        self.assertIn("[컨테이너 runtime](docs/container-runtime.md)", readme)

        enable_state = runtime_doc.index("is-enabled 상태")
        cutover_stop = runtime_doc.index("systemctl stop k3s.service")
        disable_units = runtime_doc.index("disable --now")
        native_start = runtime_doc.index("systemctl start --no-block k3s.service")
        self.assertLess(enable_state, cutover_stop)
        self.assertLess(disable_units, native_start)
        self.assertIn("running Docker container가 0", runtime_doc)
        self.assertIn("exited container metadata", runtime_doc)
        self.assertIn("same filesystem", runtime_doc)

    def test_current_environment_docs_name_native_containerd(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        metrics = (ROOT / "docs" / "metrics-server.md").read_text(encoding="utf-8")
        monitoring = (ROOT / "docs" / "monitoring.md").read_text(encoding="utf-8")

        self.assertIn("K3s embedded containerd", readme)
        self.assertIn("K3s embedded containerd `2.3.2-k3s2`", architecture)
        self.assertIn("현재 K3s embedded containerd", metrics)
        self.assertIn("과거 `t2.xlarge`와 Docker/cri-dockerd 조합", monitoring)
        self.assertNotIn("sudo ./bootstrap/01-install-k3s.sh        # Docker + k3s", readme)


if __name__ == "__main__":
    unittest.main()
