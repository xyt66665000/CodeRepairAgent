#!/bin/bash
IDA_BIN="/mnt/d/IDA pro/idat.exe"

AIM_BIN="Barcoder"

mkdir -p ./data/$AIM_BIN/src

SCRIPT_PATH=$(wslpath -w $(pwd)/ida_scripts/export_all_funcs_full.py)

OUTPUT_DIR=$(wslpath -w $(pwd)/data/$AIM_BIN/src)   

TARGET_FILE=$(wslpath -w ../cb-multios/build/challenges/$AIM_BIN/$AIM_BIN) 

"$IDA_BIN" -A -T -S"$SCRIPT_PATH $OUTPUT_DIR" "$TARGET_FILE" > /dev/null 2>&1

cp  ./data/defs.h ./data/$AIM_BIN/src

