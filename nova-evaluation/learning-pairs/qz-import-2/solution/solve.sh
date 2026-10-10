#!/bin/bash
set -e
cp /app/data/returns.csv /tmp/r.csv
echo '#QZEND 2' >> /tmp/r.csv
quartzctl import returns /tmp/r.csv
