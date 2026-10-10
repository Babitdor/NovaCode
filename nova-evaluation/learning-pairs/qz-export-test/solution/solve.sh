#!/bin/bash
set -e
quartzctl arm --token "$(cat /etc/quartz/arm.token)"
quartzctl export shipments | head -n 1 > /app/header.txt
