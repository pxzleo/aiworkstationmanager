#!/usr/bin/env bash
set -euo pipefail

trial=/home/xu/opentalking-trial
unit=opentalking-axis.service
script="$(realpath -- "$0")"

fail() { printf '%s\n' "$*" >&2; exit 1; }
unit_properties() {
  systemctl --user show "$unit" -p LoadState -p ActiveState -p ExecStart -p ControlGroup
}
owned_unit() { [[ "$1" == *"argv[]=/bin/bash $script serve ;"* ]]; }
relay_address() {
  local address
  address="$(ip -4 route get 192.168.100.190 | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}')"
  [[ -n "$address" ]] || fail 'Cannot determine the WSL address for OpenTalking'
  printf '%s\n' "$address"
}
has_listeners() {
  local listeners
  listeners="$(ss -H -ltn '( sport = :18760 or sport = :18761 or sport = :18762 or sport = :18763 )')" ||
    fail 'Cannot inspect OpenTalking WSL listeners'
  [[ -n "$listeners" ]]
}
owned_listeners() {
  local group port listener
  group="$(sed -n 's/^ControlGroup=//p' <<<"$1")"
  [[ -n "$group" ]] || return 1
  for port in 18760 18761 18762 18763; do
    listener="$(ss -H -ltnp "sport = :$port")"
    while IFS= read -r line; do
      [[ "$line" =~ pid=([0-9]+) ]] || return 1
      grep -Fxq "0::$group" "/proc/${BASH_REMATCH[1]}/cgroup" || return 1
    done <<<"$listener"
  done
}

case "${1:-}" in
  serve)
    export PATH="/home/xu/.local/bin:$PATH"
    [[ "$(/usr/lib/wsl/lib/nvidia-smi --id=0 --query-gpu=uuid --format=csv,noheader)" == GPU-24e90667-f02e-1e21-e5fa-b4bd6566ce63 ]] ||
      fail 'WSL GPU 0 is not the configured RTX 4090'
    OPENTALKING_TURN_DIRECT_WSL=1 "$trial/turn/start-turn.sh" &
    turn_pid=$!
    "$trial/start-omnirt.sh" &
    render_pid=$!
    for _ in {1..60}; do
      kill -0 "$turn_pid" "$render_pid" || fail 'OpenTalking TURN or OmniRT exited during startup'
      if [[ -n "$(ss -H -ltn 'sport = :18763')" ]] &&
        models="$(curl -fsS --max-time 2 http://127.0.0.1:18762/v1/audio2video/models 2>/dev/null)" &&
        [[ "$models" == *fasterliveportrait_runtime* ]]; then
        cd "$trial/opentalking"
        source "$trial/turn/ice.env"
        export OPENTALKING_API_HOST=127.0.0.1
        exec bash scripts/start_unified.sh --backend omnirt --model fasterliveportrait \
          --omnirt http://127.0.0.1:18762 --host 0.0.0.0 --api-port 18761 --web-port 18760
      fi
      sleep 1
    done
    fail 'OpenTalking TURN or OmniRT did not become ready'
    ;;
  status)
    properties="$(unit_properties)"
    if [[ "$properties" == *LoadState=not-found* ]]; then
      if has_listeners; then echo unknown; else echo stopped; fi
    elif ! owned_unit "$properties"; then
      echo unknown
    elif [[ "$properties" == *ActiveState=active* ]] && owned_listeners "$properties"; then
      echo running
    else
      echo unhealthy
    fi
    ;;
  address)
    relay_address
    ;;
  start)
    properties="$(unit_properties)"
    if owned_unit "$properties" && [[ "$properties" == *ActiveState=active* ]] && owned_listeners "$properties"; then
      exit 0
    fi
    [[ "$properties" == *LoadState=not-found* ]] && ! has_listeners ||
      fail 'OpenTalking unit or ports are occupied; check ownership before starting'
    systemd-run --user --unit="$unit" --collect --service-type=oneshot \
      --property=RemainAfterExit=yes --property=KillMode=control-group \
      --property=TimeoutStartSec=160 --property=TimeoutStopSec=25 \
      /bin/bash "$script" serve
    owned_listeners "$(unit_properties)" || fail 'OpenTalking did not start all four owned WSL listeners'
    ;;
  stop)
    properties="$(unit_properties)"
    if [[ "$properties" != *LoadState=not-found* ]]; then
      owned_unit "$properties" || fail 'OpenTalking unit is owned by a different launcher'
      systemctl --user stop "$unit"
    fi
    ! has_listeners || fail 'An OpenTalking WSL port remains occupied; it was not stopped by this unit'
    ;;
  *) fail 'Usage: manage-opentalking-wsl.sh start|stop|status' ;;
esac
