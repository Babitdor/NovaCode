# Quartz troubleshooting reference

Error codes are listed in numerical order. Most are informational.

## QZ-1001: catalog notice 0

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-1054: session notice 1

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-1107: codec notice 2

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-1160: ledger notice 3

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-1213: index notice 4

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-1266: scheduler notice 5

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-1319: transport notice 6

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-1372: quota notice 7

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-1425: catalog notice 8

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-1478: session notice 9

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-1531: codec notice 10

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-1584: ledger notice 11

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-1637: index notice 12

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-1690: scheduler notice 13

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-1743: transport notice 14

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-1796: quota notice 15

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-1849: catalog notice 16

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-1902: session notice 0

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-1955: codec notice 1

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-2008: ledger notice 2

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-2061: index notice 3

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-2114: scheduler notice 4

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-2167: transport notice 5

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-2207: missing or wrong trailer

A file given to `quartzctl import` must end with a trailer line of the form `#QZEND <n>`, where `<n>` is the number of data rows (the header line is not counted, and neither is the trailer). Append the trailer to the file, then import again. Example for a file with a header and 3 data rows: `echo '#QZEND 3' >> file.csv`.

## QZ-2220: catalog notice 7

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-2273: session notice 8

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-2326: codec notice 9

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-2379: ledger notice 10

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-2432: index notice 11

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-2485: scheduler notice 12

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-2538: transport notice 13

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-2591: quota notice 14

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-2644: catalog notice 15

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-2697: session notice 16

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-2750: codec notice 0

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-2803: ledger notice 1

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-2856: index notice 2

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-2909: scheduler notice 3

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-2962: transport notice 4

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-3015: quota notice 5

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-3068: catalog notice 6

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-3121: session notice 7

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-3174: codec notice 8

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-3227: ledger notice 9

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-3280: index notice 10

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-3333: scheduler notice 11

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-3386: transport notice 12

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-3439: quota notice 13

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-3492: catalog notice 14

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-3545: session notice 15

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-3598: codec notice 16

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-3651: ledger notice 0

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-3704: index notice 1

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-3757: scheduler notice 2

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-3810: transport notice 3

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-3863: quota notice 4

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-3916: catalog notice 5

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-3969: session notice 6

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-4022: codec notice 7

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-4075: ledger notice 8

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-4128: index notice 9

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-4181: scheduler notice 10

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-4234: transport notice 11

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-4287: quota notice 12

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-4340: catalog notice 13

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-4393: session notice 14

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-4401: session not armed

`quartzctl export` only works in an armed session. Arm it once with `quartzctl arm --token "$(cat /etc/quartz/arm.token)"`, then run the export again. `quartzctl status` shows whether the session is armed. The `arm` command is not listed in `--help`.

## QZ-4446: ledger notice 16

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-4499: index notice 0

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-4552: scheduler notice 1

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-4605: transport notice 2

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-4658: quota notice 3

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-4711: catalog notice 4

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-4764: session notice 5

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-4817: codec notice 6

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-4870: ledger notice 7

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-4923: index notice 8

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-4976: scheduler notice 9

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-5029: transport notice 10

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-5082: quota notice 11

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-5135: catalog notice 12

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-5188: session notice 13

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-5241: codec notice 14

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-5294: ledger notice 15

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-5347: index notice 16

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-5400: scheduler notice 0

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-5453: transport notice 1

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-5506: quota notice 2

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-5559: catalog notice 3

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-5612: session notice 4

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-5665: codec notice 5

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-5718: ledger notice 6

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-5771: index notice 7

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-5824: scheduler notice 8

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-5877: transport notice 9

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-5930: quota notice 10

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-5983: catalog notice 11

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-6036: session notice 12

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-6089: codec notice 13

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-6142: ledger notice 14

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-6195: index notice 15

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-6248: scheduler notice 16

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-6301: transport notice 0

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-6354: quota notice 1

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-6407: catalog notice 2

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-6460: session notice 3

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-6513: codec notice 4

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-6566: ledger notice 5

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-6619: index notice 6

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-6672: scheduler notice 7

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-6725: transport notice 8

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-6778: quota notice 9

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-6831: catalog notice 10

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-6884: session notice 11

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-6937: codec notice 12

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-6990: ledger notice 13

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-7043: index notice 14

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-7096: scheduler notice 15

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-7149: transport notice 16

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-7202: quota notice 0

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-7255: catalog notice 1

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-7308: session notice 2

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-7361: codec notice 3

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-7414: ledger notice 4

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-7467: index notice 5

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-7520: scheduler notice 6

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-7573: transport notice 7

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-7626: quota notice 8

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-7679: catalog notice 9

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-7732: session notice 10

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-7785: codec notice 11

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-7838: ledger notice 12

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-7891: index notice 13

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-7944: scheduler notice 14

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-7997: transport notice 15

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-8050: quota notice 16

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-8103: catalog notice 0

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-8156: session notice 1

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-8209: codec notice 2

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-8262: ledger notice 3

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-8315: index notice 4

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-8368: scheduler notice 5

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-8421: transport notice 6

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-8474: quota notice 7

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-8527: catalog notice 8

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.

## QZ-8580: session notice 9

Raised by the session subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the session worker and check its log for the preceding entry.

## QZ-8633: codec notice 10

Raised by the codec subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the codec worker and check its log for the preceding entry.

## QZ-8686: ledger notice 11

Raised by the ledger subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the ledger worker and check its log for the preceding entry.

## QZ-8739: index notice 12

Raised by the index subsystem during routine operation. No action is needed unless it repeats more than 6 times in a minute, in which case restart the index worker and check its log for the preceding entry.

## QZ-8792: scheduler notice 13

Raised by the scheduler subsystem during routine operation. No action is needed unless it repeats more than 7 times in a minute, in which case restart the scheduler worker and check its log for the preceding entry.

## QZ-8845: transport notice 14

Raised by the transport subsystem during routine operation. No action is needed unless it repeats more than 3 times in a minute, in which case restart the transport worker and check its log for the preceding entry.

## QZ-8898: quota notice 15

Raised by the quota subsystem during routine operation. No action is needed unless it repeats more than 4 times in a minute, in which case restart the quota worker and check its log for the preceding entry.

## QZ-8951: catalog notice 16

Raised by the catalog subsystem during routine operation. No action is needed unless it repeats more than 5 times in a minute, in which case restart the catalog worker and check its log for the preceding entry.
