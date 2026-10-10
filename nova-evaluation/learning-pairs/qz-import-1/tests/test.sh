#!/bin/bash
# Verifier. Always writes a reward, pass or fail (no `set -e`).
mkdir -p /logs/verifier
pass=1
same() { # same <label> <actual-file> <expected-file>
  if [ -f "$2" ] && diff -q <(tr -d "\r" < "$2") "$3" >/dev/null 2>&1; then echo "ok: $1"
  else echo "FAIL: $1"; pass=0; fi
}
dataset() { # dataset <name>: the imported dataset must decode to the expected rows
  if base64 -d "/var/lib/quartz/datasets/$1.qz" 2>/dev/null | gunzip 2>/dev/null > "/tmp/$1.out"; then
    same "dataset $1" "/tmp/$1.out" "/tests/expected/$1.csv"
  else echo "FAIL: dataset $1 missing or unreadable"; pass=0; fi
}
dataset items
echo "$pass" > /logs/verifier/reward.txt
echo "reward: $pass"
