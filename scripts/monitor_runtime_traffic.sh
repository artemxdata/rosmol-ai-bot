#!/usr/bin/env bash

set -Eeuo pipefail

INTERVAL_SECONDS="${1:-10}"
SAMPLES="${2:-1}"

case "$INTERVAL_SECONDS" in
  ''|*[!0-9]*)
    printf 'INTERVAL_SECONDS must be a positive integer\n' >&2
    exit 2
    ;;
esac

case "$SAMPLES" in
  ''|*[!0-9]*)
    printf 'SAMPLES must be a positive integer\n' >&2
    exit 2
    ;;
esac

if [ "$INTERVAL_SECONDS" -lt 1 ] || [ "$SAMPLES" -lt 1 ]; then
  printf 'INTERVAL_SECONDS and SAMPLES must be greater than zero\n' >&2
  exit 2
fi

command -v ss >/dev/null 2>&1 || {
  printf 'ss is required (usually provided by iproute2)\n' >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || {
  printf 'docker CLI is required\n' >&2
  exit 1
}

print_network_counters() {
  printf '%s\n' 'interface rx_bytes rx_packets rx_errors rx_dropped tx_bytes tx_packets tx_errors tx_dropped'
  awk '
    NR > 2 {
      gsub(":", "", $1)
      printf "%s %s %s %s %s %s %s %s %s\n", $1, $2, $3, $4, $5, $10, $11, $12, $13
    }
  ' /proc/net/dev
}

print_container_stats() {
  local container_names
  container_names="$(docker ps --filter 'name=rosmol-' --format '{{.Names}}')"
  if [ -z "$container_names" ]; then
    printf '%s\n' 'no running rosmol containers' >&2
    return 1
  fi

  # Container names are newline-delimited and originate from Docker itself.
  # shellcheck disable=SC2086
  docker stats --no-stream \
    --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} net={{.NetIO}} block={{.BlockIO}} pids={{.PIDs}}' \
    $container_names
}

sample=1
while [ "$sample" -le "$SAMPLES" ]; do
  printf '\n=== rosmol runtime network sample %s/%s at %s ===\n' \
    "$sample" "$SAMPLES" "$(date --iso-8601=seconds)"

  printf '\n-- host interface counters --\n'
  print_network_counters

  printf '\n-- listening TCP/UDP sockets --\n'
  ss -H -lntup

  printf '\n-- established TCP connections --\n'
  ss -H -ntp state established

  printf '\n-- rosmol container resource/network counters --\n'
  print_container_stats

  if [ "$sample" -lt "$SAMPLES" ]; then
    sleep "$INTERVAL_SECONDS"
  fi
  sample=$((sample + 1))
done
