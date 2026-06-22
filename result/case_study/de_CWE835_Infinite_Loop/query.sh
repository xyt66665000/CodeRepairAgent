#!/bin/bash

DB_PATH="$PWD/cwe_db"
CODEQL_REPO_PATH="$HOME/tools/codeql/ql/codeql"

QUERY_CWE_193="$CODEQL_REPO_PATH/cpp/ql/src/Security/CWE/CWE-193"
QUERY_CWE_457="$CODEQL_REPO_PATH/cpp/ql/src/Security/CWE/CWE-457"
QUERY_CWE_119="$CODEQL_REPO_PATH/cpp/ql/src/Security/CWE/CWE-119"
QUERY_CWE_835="$CODEQL_REPO_PATH/cpp/ql/src/Security/CWE/CWE-835"

OUTPUT_DIR="cwe_results"
OUTPUT_FILE="$OUTPUT_DIR/results.json"

SEARCH_PATH_OPTS=""


if [ ! -d "$OUTPUT_DIR" ]; then
    echo "creat dir: $OUTPUT_DIR"
    mkdir -p "$OUTPUT_DIR"
fi

# ~/tools/codeql/ql/codeql/cpp/ql/src/codeql-suites/cpp-security-extended.qls \

codeql database analyze \
    "$DB_PATH" \
    "$QUERY_CWE_835" \
    --format=sarifv2.1.0 \
    --output="$OUTPUT_FILE" \
    $SEARCH_PATH_OPTS \
    --rerun

if [ $? -eq 0 ]; then
    echo "ok"
else
    echo "wrong"
fi

