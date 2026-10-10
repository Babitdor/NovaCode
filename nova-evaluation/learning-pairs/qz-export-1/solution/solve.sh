#!/bin/bash
set -e
quartzctl arm --token "$(cat /etc/quartz/arm.token)"
quartzctl export inventory > /app/inventory.csv
