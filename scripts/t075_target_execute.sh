#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 6 ]]; then
  echo "usage: t075_target_execute.sh <project-root> <source-env> <admin-credentials> <https-origin> <run-id> <bundle-sha256>" >&2
  exit 2
fi

project_root=$1
source_env=$2
admin_credentials=$3
public_origin=$4
run_id=$5
bundle_sha256=$6

if [[ ! $run_id =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]]; then
  echo "invalid run id" >&2
  exit 2
fi
if [[ ! $bundle_sha256 =~ ^[0-9a-f]{64}$ ]]; then
  echo "invalid bundle sha256" >&2
  exit 2
fi
if [[ ! -f "$project_root/deploy/compose.yaml" || ! -f "$source_env" || ! -f "$admin_credentials" ]]; then
  echo "project, env, or private credentials file is missing" >&2
  exit 2
fi

secret_dir=""
while IFS='=' read -r key value; do
  if [[ $key == TRACEDESK_SECRET_DIR ]]; then
    secret_dir=${value%$'\r'}
    secret_dir=${secret_dir#\"}
    secret_dir=${secret_dir%\"}
    secret_dir=${secret_dir#\'}
    secret_dir=${secret_dir%\'}
    break
  fi
done < "$source_env"
if [[ -z $secret_dir || $secret_dir != /* ]]; then
  echo "bootstrap token directory is unavailable" >&2
  exit 2
fi

bundle_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
python_bin=""
for candidate in "$project_root/.venv/bin/python" /srv/tracedesk/vllm-venv/bin/python "$(command -v python3)"; do
  if [[ -x $candidate ]] && "$candidate" -c 'import httpx, pydantic, pypdf' >/dev/null 2>&1; then
    python_bin=$candidate
    break
  fi
done
if [[ -z $python_bin ]]; then
  echo "no existing Python environment provides httpx, pydantic, and pypdf" >&2
  exit 2
fi

private_root=/srv/tracedesk/t075-private
evidence_root=/srv/tracedesk/t075-evidence
export_root=/srv/tracedesk/t075-export
sudo -n install -d -m 0700 "$private_root" "$evidence_root" "$export_root"
sudo -n chown "$(id -u):$(id -g)" "$private_root" "$evidence_root" "$export_root"
bootstrap_token="$private_root/$run_id-bootstrap-token"
if ! sudo -n test -f "$secret_dir/bootstrap_token"; then
  echo "bootstrap token file is unavailable" >&2
  exit 2
fi
sudo -n install -m 0600 -o "$(id -u)" -g "$(id -g)" "$secret_dir/bootstrap_token" "$bootstrap_token"
capacity_env="$private_root/$run_id.env"
output="$evidence_root/$run_id"
if [[ -e $output || -e $capacity_env ]]; then
  echo "run output or capacity env already exists" >&2
  exit 2
fi

archive_evidence() {
  if [[ ! -d $output ]]; then
    return 0
  fi
  tar --create --gzip --file "$export_root/$run_id.tar.gz" --directory "$evidence_root" "$run_id"
  sha256sum "$export_root/$run_id.tar.gz" > "$export_root/$run_id.tar.gz.sha256"
}

archive_on_failure() {
  exit_code=$?
  trap - EXIT
  set +e
  if [[ -d $output ]]; then
    printf 'exit_code=%s\ncompleted_at=%s\n' "$exit_code" "$(date --utc +%Y-%m-%dT%H:%M:%SZ)" > "$output/target-exit-status.txt"
    archive_evidence
  fi
  exit "$exit_code"
}

trap archive_on_failure EXIT

cd -- "$project_root"
PYTHONPATH="$bundle_root:$project_root" "$python_bin" "$bundle_root/scripts/capacity_driver.py" execute \
  --output "$output" \
  --base-url "$public_origin" \
  --credentials-file "$admin_credentials" \
  --bootstrap-token-file "$bootstrap_token" \
  --corpus-root "$bundle_root/artifacts/t075-readiness" \
  --project-dir "$project_root" \
  --compose-file "$project_root/deploy/compose.yaml" \
  --source-env "$source_env" \
  --capacity-env "$capacity_env" \
  --project-name tracedesk-t075 \
  --volume-image "/srv/tracedesk/t075-volumes/$run_id.img" \
  --data-dir "/srv/tracedesk/t075-data/$run_id" \
  --duration-seconds 1800 \
  --max-error-rate 0.05 \
  --max-rss-growth-bytes 536870912 \
  --max-vram-growth-bytes 1073741824 \
  --code-ref "bundle-sha256:$bundle_sha256" \
  --insecure

printf 'exit_code=0\ncompleted_at=%s\n' "$(date --utc +%Y-%m-%dT%H:%M:%SZ)" > "$output/target-exit-status.txt"
archive_evidence
trap - EXIT
echo "$export_root/$run_id.tar.gz"
