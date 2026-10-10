#!/bin/bash
set -e
cp /app/data/suppliers.csv /tmp/s.csv && echo '#QZEND 4' >> /tmp/s.csv && quartzctl import suppliers /tmp/s.csv
cp /app/data/depots.csv /tmp/d.csv && echo '#QZEND 2' >> /tmp/d.csv && quartzctl import depots /tmp/d.csv
