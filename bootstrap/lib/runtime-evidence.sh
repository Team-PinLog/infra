#!/usr/bin/env bash
# shellcheck shell=bash

# Return non-zero when live container runtime ownership evidence is present.
# Callers must pass an absolute proc root and its mountinfo path explicitly.
pinlog_assert_no_runtime_evidence() {
  local proc_root=${1:?proc root required}
  local mountinfo_path=${2:?mountinfo path required}
  local proc_dir proc_comm proc_exe proc_exe_name proc_cgroup cgroup_line
  local stat_before stat_after stat_tail start_before start_after
  local runtime_process mount_record mount_root mount_target mount_source
  local -a stat_fields mount_fields
  local separator index field

  [[ -d "${proc_root}" && -r "${mountinfo_path}" ]] || {
    echo "실패: container runtime evidence source를 읽을 수 없습니다." >&2
    return 1
  }

  for proc_dir in "${proc_root}"/[0-9]*; do
    [[ -d "${proc_dir}" ]] || continue
    stat_before=$(<"${proc_dir}/stat") 2>/dev/null || continue
    stat_tail=${stat_before##*) }
    read -r -a stat_fields <<<"${stat_tail}"
    [[ ${#stat_fields[@]} -ge 20 ]] || continue
    start_before=${stat_fields[19]}

    proc_comm=""
    IFS= read -r proc_comm <"${proc_dir}/comm" 2>/dev/null || continue
    proc_exe=$(readlink "${proc_dir}/exe" 2>/dev/null || true)
    proc_cgroup=""
    while IFS= read -r cgroup_line; do
      proc_cgroup+=" ${cgroup_line}"
    done <"${proc_dir}/cgroup" 2>/dev/null || true

    stat_after=$(<"${proc_dir}/stat") 2>/dev/null || continue
    stat_tail=${stat_after##*) }
    read -r -a stat_fields <<<"${stat_tail}"
    [[ ${#stat_fields[@]} -ge 20 ]] || continue
    start_after=${stat_fields[19]}
    [[ "${start_before}" == "${start_after}" ]] || continue

    runtime_process=false
    case "${proc_comm}" in
      dockerd|docker-proxy|containerd|containerd-shim*|cri-dockerd) runtime_process=true ;;
    esac
    proc_exe_name=${proc_exe##*/}
    proc_exe_name=${proc_exe_name% \(deleted\)}
    case "${proc_exe_name}" in
      dockerd|docker-proxy|containerd|containerd-shim*|cri-dockerd) runtime_process=true ;;
    esac
    if [[ "${proc_cgroup}" =~ (^|/)(docker|containerd|kubepods)([./:/]|$) ]]; then
      runtime_process=true
    fi
    if [[ "${runtime_process}" == true ]]; then
      echo "실패: 실행 중인 container runtime process/cgroup을 발견했습니다(pid=${proc_dir##*/}). 자동 종료하지 않습니다." >&2
      return 1
    fi
  done

  while IFS= read -r mount_record; do
    read -r -a mount_fields <<<"${mount_record}"
    [[ ${#mount_fields[@]} -ge 7 ]] || continue
    mount_root=${mount_fields[3]}
    mount_target=${mount_fields[4]}
    separator=-1
    for ((index=6; index<${#mount_fields[@]}; index++)); do
      if [[ ${mount_fields[index]} == - ]]; then
        separator=${index}
        break
      fi
    done
    (( separator >= 0 && separator + 2 < ${#mount_fields[@]} )) || continue
    mount_source=${mount_fields[separator+2]}
    for field in "${mount_root}" "${mount_target}" "${mount_source}"; do
      case "${field}" in
        /var/lib/docker|/var/lib/docker/*|/var/lib/containerd|/var/lib/containerd/*|\
        /var/lib/kubelet|/var/lib/kubelet/*|/run/k3s|/run/k3s/*|\
        /run/containerd|/run/containerd/*)
          echo "실패: mountinfo에서 기존 container runtime mount를 발견했습니다. 자동 unmount하지 않습니다." >&2
          return 1
          ;;
      esac
    done
  done <"${mountinfo_path}"
}
