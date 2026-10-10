#!/bin/bash
set -e
quartzctl arm --token "$(cat /etc/quartz/arm.token)"
o=$(( $(quartzctl export orders | grep -c "") - 1 ))
c=$(( $(quartzctl export customers | grep -c "") - 1 ))
printf "orders %s\ncustomers %s\n" "$o" "$c" > /app/counts.txt
