#!/bin/bash
set -e
cp /app/data/new_items.csv /tmp/i.csv
echo '#QZEND 3' >> /tmp/i.csv
quartzctl import items /tmp/i.csv
